"""渲染面板 REST 面（T4.6 · §04.2.8）。

五个端点对应面板上的五件事
--------------------------
看首屏（``GET /render/console``）、开一条（``POST /render/jobs``）、看进度
（``GET /render/jobs/{id}``）、叫停（``POST /render/jobs/{id}/cancel``）、
看成片（``GET /render/videos/{name}``）。

为什么进度是**轮询**而不是 WS 推送
----------------------------------
WS 通道（``ws/``）是**只读且不碰数据库**的契约，它的快照 provider 在应用层注册。
把一个"进程内的任务进度"塞进去要动 Hub 的协议与快照注册表，而这条链路的进度是
**粗粒度**的（配音第 N 句 / 渲染中 / 完成），不是每帧都在动的东西 —— 1 秒一次的轮询
完全够，且断线重连天然正确（重新 GET 一次就是最新状态）。

真要改成推送时，落点是"给 Hub 加一路 ``render.progress``"，而不是把轮询周期调小。

为什么视频用 ``FileResponse`` 而不是读进内存
-------------------------------------------
一支 1080×1920 的成片十几 MB。``FileResponse`` 会带上 ``Range`` 支持，``<video>``
才能拖动进度条；一次性读进内存既占着不放，也没法 seek。

路径安全：``name`` **只允许**"一个不含路径分隔符的文件名"，再与 ``videos_dir`` 拼。
这样 ``../../`` 这类东西在正则那一步就被拒了，而不是靠 ``resolve()`` 之后的比较兜底。
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from studio.app.deps import AppState
from studio.app.schemas.render import (
    MAX_SPEECH_CHARS,
    RenderConsoleResponse,
    RenderJobModel,
    RenderJobRequest,
)
from studio.core.config import OutputsConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.render.profiles import resolve_profile
from studio.render.sticker import StickerPlan, plan_stickers
from studio.render.subtitle import resolve_font_dir
from studio.render.watermark import plan_watermark
from studio.services.render_job_service import RenderJob, RenderJobService, list_videos
from studio.services.render_service import ProduceRequest
from studio.services.voice_service import voice_engine_info

__all__ = ["router"]

router = APIRouter(tags=["render"])

#: 任务 id 的形状（``RenderJobService`` 生成的就是它）。
#:
#: **必须写成 `PathParam(pattern=...)`，不能写成 `job_id: str = "^r[0-9]{4,}$"`** ——
#: 后者只是一个字符串默认值，一个字节的校验都不做（`/jobs/not-a-job` 会一路进到服务里）。
#: 写成参数对象之后，非法 id 在路由层就是 422。
_JOB_ID = PathParam(pattern=r"^r[0-9]{4,}$")

#: 成片文件名：**不含路径分隔符**、以 ``.mp4`` 结尾。
#: 这一条同时挡住了 ``../`` 与绝对路径（Windows 的 ``\`` 也在排除之列）。
_VIDEO_NAME = re.compile(r"^[^/\\:*?\"<>|]+\.mp4$", re.IGNORECASE)

_NO_VOICE_HINT = (
    "本机既没有可用的常驻配音引擎，也没有可用的系统语音包，配音这一步会直接失败。"
    "装一个中文语音包（「设置 → 时间和语言 → 语音」，如 Microsoft Huihui）"
    "或按 docs/runbook/tts_models.md 把常驻服务跑起来，然后重开面板。"
)


def _service(state: AppState) -> RenderJobService:
    """取**进程内**的出片服务。

    必须是 ``AppState`` 上那一份（不是每次现造）：任务登记表与工作线程都是内存状态，
    现造一份等于"刚提交的任务下一次请求就查不到了"。
    """
    return state.render_jobs


def _subtitle_status(outputs: OutputsConfig, paths: StudioPaths) -> tuple[bool, str | None]:
    """首屏的字幕结论：**关不关**由配置说，**能不能烧**由字体说。

    与渲染路径调的是同一个 :func:`resolve_font_dir`，所以"面板说会烧字幕"与
    "成片真的有字幕"不会对不上 —— 这正是 T4.6 那条契约的要点。
    """
    if not outputs.subtitle.enabled:
        return False, "配置里关掉了字幕（outputs.yaml → subtitle.enabled=false）"

    try:
        font = resolve_font_dir(paths.templates_dir)
    except StudioError as exc:
        return False, f"找不到字体，字幕会被跳过：{exc.message}"

    if font.note:
        return True, font.note
    return True, None


def _stickers_hint(
    applied: tuple[str, ...],
    skipped: list[StickerPlan],
    swapping: list[str],
) -> str | None:
    """贴图这一句提示（``None`` ⇒ 没有要说的话，面板不显示横幅）。

    与水印那条"贴 / 不贴"不同，这里要说的是**哪几层上了、哪几层没上**：
    配置里开着三层而只上了一层，是"图没放进盘上"这类问题最直接的表现 ——
    只报一个布尔的话，人只会看到"有贴图"，然后对着成片找那两层不存在的图。

    ``swapping`` 是"**配齐了讲话图**的层"（不是"一定会换图的层"）。措辞刻意写成
    "配了讲话图（出片时按稿子逐句说话人换图）"：这一屏手上**没有稿子、也没有时间
    轴**，谁在哪句讲话只有出片那一步知道。写成"会换图"就是替渲染路径下结论 ——
    面板绿灯、成片不换，正是这个项目里最贵的一种谎话（陷阱 223）。
    """
    if not applied and not skipped and not swapping:
        return None
    parts: list[str] = []
    if applied:
        parts.append(f"会贴上 {'、'.join(applied)}")
    if swapping:
        parts.append(f"{'、'.join(swapping)} 配了讲话图（出片时按稿子逐句说话人换图）")
    for item in skipped:
        if item.spec.enabled:
            parts.append(f"{item.spec.name} 跳过（{item.skipped_reason}）")
    return "；".join(parts) if parts else None


@router.get("/api/v1/render/console", response_model=RenderConsoleResponse)
def get_console(request: Request) -> RenderConsoleResponse:
    """面板首屏：一次拿全（可选参数 + 在跑的任务 + 最近任务 + 成片列表）。"""
    state: AppState = request.app.state.studio
    service = _service(state)
    outputs = state.outputs.current().config

    engine = voice_engine_info(state.connections.get(), paths=state.paths)
    profile = resolve_profile(outputs)
    watermark = plan_watermark(
        outputs.watermark,
        canvas_width=profile.width,
        canvas_height=profile.height,
        home=state.paths.home,
    )
    subtitle_enabled, subtitle_hint = _subtitle_status(outputs, state.paths)
    stickers = plan_stickers(
        outputs.stickers,
        canvas_width=profile.width,
        canvas_height=profile.height,
        home=state.paths.home,
    )
    applied = tuple(item.spec.name for item in stickers if item.applied)
    skipped = [item for item in stickers if not item.applied]
    # "配齐了讲话图"的判据是 `speaking_problem is None`（配置齐 + 讲话图可用），
    # **不是** `swaps` —— `swaps` 还要"有讲话区间"，而这一屏算不出来（没读稿子）。
    # 于是这里给的是"这一层配好了，换不换看稿子"，不是"这一层会换图"。
    swapping = [
        item.spec.name
        for item in stickers
        if item.applied and item.spec.speaker and item.speaking_problem is None
    ]

    return RenderConsoleResponse.model_validate(
        {
            "engine": engine.name,
            "engine_ready": engine.ready,
            "engine_hint": engine.hint or (None if engine.ready else _NO_VOICE_HINT),
            "profiles": [_profile_option(outputs, name) for name in outputs.profiles],
            "default_profile": outputs.default_profile,
            "voices": [{"name": name, "is_default": index == 0} for index, name in enumerate(engine.voices)],
            "running": _job_dict(service.active()),
            "jobs": [_job_dict(job) for job in service.recent(limit=10)],
            "videos": [video.to_dict() for video in list_videos(state.paths)],
            "max_speech_chars": MAX_SPEECH_CHARS,
            "watermark_enabled": watermark.enabled,
            "watermark_hint": watermark.skipped_reason,
            "subtitle_enabled": subtitle_enabled,
            "subtitle_hint": subtitle_hint,
            "stickers_applied": list(applied),
            "stickers_hint": _stickers_hint(applied, skipped, swapping),
        }
    )


@router.post("/api/v1/render/jobs", response_model=RenderJobModel)
def create_job(request: Request, body: RenderJobRequest) -> RenderJobModel:
    """开一条出片任务（**立刻返回**，活在工作线程里跑）。

    文案规则与 CLI 同一条：``text`` 非空就用它，否则按 ``task_id`` 读库里那一版生效稿件。
    """
    state: AppState = request.app.state.studio
    job = _service(state).submit(
        ProduceRequest(
            task_id=body.task_id,
            text=body.text,
            profile_name=body.profile,
            voice=body.voice,
            reuse_voice=body.reuse_voice,
            seed=body.seed,
            threads=body.threads,
            subtitle=body.subtitle,
        )
    )
    return RenderJobModel.model_validate(job.to_dict())


@router.get("/api/v1/render/jobs/{job_id}", response_model=RenderJobModel)
def get_job(request: Request, job_id: str = _JOB_ID) -> RenderJobModel:
    """查一条任务的进度 / 结果（面板轮询的就是它）。"""
    state: AppState = request.app.state.studio
    job = _service(state).get(job_id)
    if job is None:
        raise _no_such_job(job_id)
    return RenderJobModel.model_validate(job.to_dict())


@router.post("/api/v1/render/jobs/{job_id}/cancel", response_model=RenderJobModel)
def cancel_job(request: Request, job_id: str = _JOB_ID) -> RenderJobModel:
    """叫停一条任务。

    **协作式**：排队中的立刻作废；已经在跑的会在下一次进度回调处停下。
    ffmpeg 一旦跑起来要等它自己结束 —— 面板上也是这么写的，不能让人以为按了立刻停。
    """
    state: AppState = request.app.state.studio
    job = _service(state).cancel(job_id)
    if job is None:
        raise _no_such_job(job_id)
    return RenderJobModel.model_validate(job.to_dict())


@router.get("/api/v1/render/videos/{name}")
def get_video(request: Request, name: str) -> FileResponse:
    """播放 / 下载一支成片（``<video>`` 直接取这个 url，Range 由 Starlette 处理）。"""
    state: AppState = request.app.state.studio
    if not _VIDEO_NAME.match(name):
        # 入参问题 ⇒ 422（`VALIDATION_FAILED` 已在 `app/errors.py` 的映射表里）。
        # **不能**用 `RENDER_FAILED`：那个码没登记 ⇒ 500，前端会把"名字写错了"
        # 当成"服务端崩了"去重试。
        raise StudioError(
            f"不是合法的成片文件名：{name}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"name": name},
            remediation="成片名形如 20260915-203246_<task_id>_final.mp4（见成片列表）",
        )
    path: Path = state.paths.videos_dir / name
    if not path.is_file():
        # "盘上该有的东西不在" ⇒ 404（`PATH_MISSING` 已登记），不是 500。
        raise StudioError(
            f"成片不在盘上：{name}",
            code=ErrorCode.PATH_MISSING,
            context={"name": name, "dir": state.paths.videos_dir.as_posix()},
            remediation="刷新成片列表；文件可能已被媒资回收（studio gc run）清掉",
        )
    guessed, _ = mimetypes.guess_type(name)
    return FileResponse(path, media_type=guessed or "video/mp4")


def _no_such_job(job_id: str) -> StudioError:
    """登记表里没有这条任务（两个端点共用一句话，免得两处措辞各写一遍）。"""
    return StudioError(
        f"没有这条出片任务：{job_id}",
        code=ErrorCode.JOB_NOT_FOUND,
        context={"job_id": job_id},
        remediation="任务登记表在进程内存里：API 重启过，或这条已经滚出最近 50 条",
    )


def _profile_option(outputs: OutputsConfig, name: str) -> dict[str, Any]:
    """下拉框的一行：名字 + 面板要显示的画布 / 质量 / 平台。

    **经过 `resolve_profile` 而不是直接读配置**：质量参数写到哪个键（libx264 用 ``crf``、
    NVENC 用 ``cq``）只有那一处知道。面板上显示 "CRF 21" 还是 "CQ 21" 必须与真正喂给
    ffmpeg 的是同一个数 —— 否则用户会对着一个不对的旋钮调半天。
    """
    profile = resolve_profile(outputs, name)
    return {
        "name": profile.name,
        "width": profile.width,
        "height": profile.height,
        "fps": profile.fps,
        "vcodec": profile.vcodec,
        "quality_field": profile.quality_field,
        "quality": profile.quality,
        "platforms": list(profile.platforms),
        "is_default": name == outputs.default_profile,
    }


def _job_dict(job: RenderJob | None) -> dict[str, Any] | None:
    """``RenderJob | None`` ⇒ ``dict | None``（``None`` 原样传下去，别编一个空任务）。"""
    return None if job is None else job.to_dict()
