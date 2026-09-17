"""配音操作面 REST（T2.9 · §04.3 / T4.5）。

五个端点 = 面板上能做的五件事
-----------------------------
看逐句状态（``GET /sentences``）、看有哪些音色（``GET /voices``）、重配一句
（``POST /sentences/{id}/resynth``）、试听一句（``GET /media/{path}``）、
换音色（``PATCH /tasks/{id}/voice_map``）。

为什么"重配"与"换音色"是**两个**端点
------------------------------------
它们的**代价**不一样：重配一句是几秒，换音色是"重配 N 句"（几十秒到几分钟）。
合成一个端点会让面板无法把"点了立刻有反应"与"要弹确认框"这两种交互分开 ——
而二次确认（``VOICE_MAP_CONFIRM_REQUIRED``）恰恰是换音色最该有的那一步：
服务端**先算代价再抛**，面板拿 ``context.affected`` 弹框，用户点头才带
``confirm=true`` 重发。

试听为什么走独立的 ``/media/{path}``
------------------------------------
``<audio src>`` 要的是一个**稳定、可缓存、支持 Range** 的 url，而它的路径就是盘上
那份文件的名字（``s00N.wav``，由 ``seq`` 决定）。用句子 id 做路径会让"url ↔ 文件"
多一层映射，排查"这一句播的是哪个文件"要跳两次（与渲染面板的
``/render/videos/{name}`` 同一条）。

路径安全：``{path}`` 必须**完全匹配** ``voice/<task_id>/s00N.wav`` 这条形状，再去
库里找那一行。``../`` 在正则那一步就被拒了，而不是靠 ``resolve()`` 之后的比较兜底
（与 ``assets.py::get_asset_media`` 同一条手法）。真正的文件路径也**不从请求里取**：
它由 ``StudioPaths.sentence_wav`` 或缓存键拼出来（见 ``voice_service.preview_audio``）。

试听**不触发合成**：点一下试听就排一次合成，等于把"看一眼"变成"重跑一遍"，
而重跑还会覆盖交付产物（§05 T2.9 的硬要求）。
"""

from __future__ import annotations

import mimetypes
import re
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from studio.app.deps import AppState
from studio.app.schemas.voice import (
    NO_VOICE_HINT,
    PROFILE_UNSPEAKABLE_HINT,
    ResynthResponse,
    SentenceVoiceList,
    VoiceMapRequest,
    VoiceMapResponse,
    VoiceOption,
    VoiceOptions,
    progress_view,
    sentence_voice_view,
)
from studio.core.errors import ErrorCode, StudioError
from studio.db.repositories import SentenceRepo, VoiceProfileRepo
from studio.domain.task_service import TaskService
from studio.services.voice_service import (
    preview_audio,
    read_timeline_total_ms,
    resynth_sentence,
    set_voice_map,
    speakable_voices,
    usable_voices,
)

__all__ = ["router"]

router = APIRouter(tags=["voice"])

#: 句子 id 的形状（ULID，Crockford base32）。写成 ``PathParam(pattern=...)`` 而不是
#: "一个字符串默认值"：后者一个字节都不校验。句子 id 永远由服务端生成、面板只负责回传，
#: 所以这里可以卡死。
_SENTENCE_ID = PathParam(pattern=r"^[0-9A-Za-z]{1,64}$")

#: 任务 id 的形状。**不能只认 ULID**：任务号是**调用方起的名**
#: （``RenderJobRequest.task_id`` 只限长度、不限字符集），渲染面板默认给的就是
#: ``ui-20260915-120000`` —— 按 ``[0-9A-Za-z]`` 卡的话，面板自己创建的任务号一进
#: 配音面板就整屏 422，而 422 报的是"路径不合法"，排查的人会去查任务号存不存在，
#: 不会想到是这条正则（陷阱 #122）。
#: 放开的边界：字母 / 数字 / ``-`` / ``_``。其它字符（空格、点、中文）在**换音色**与
#: **试听**这两条路径上仍然进不来 —— 它们要拼进 URL 路径。真要放开，落点是把这里的
#: 字符集与 ``RenderJobRequest.task_id`` 的校验**合并成一处**，而不是各自放宽。
_TASK_ID = PathParam(pattern=r"^[0-9A-Za-z_-]{1,64}$")

#: 媒资键：``voice/<task_id>/s00N.wav``。**必须整串匹配** —— 这一条同时挡住了
#: ``../``、绝对路径（``C:\...``）与盘上其它任何文件。
_MEDIA_KEY = re.compile(r"^voice/(?P<task_id>[0-9A-Za-z_-]{1,64})/s(?P<seq>[0-9]{3,6})\.wav$", re.IGNORECASE)


def _media_url(task_id: str, seq: int) -> str:
    """句子音频的 url（形状与 :data:`_MEDIA_KEY` 是同一份契约的两面）。"""
    return f"/api/v1/media/voice/{task_id}/s{seq:03d}.wav"


@router.get("/api/v1/voices", response_model=VoiceOptions)
def list_voice_options(
    request: Request, task_id: Annotated[str | None, Query(description="顺带带上这条任务的映射")] = None
) -> VoiceOptions:
    """音色下拉框（本机现在选得出来的那些）。

    ``task_id`` 可给可不给：下拉框本身与任务无关，但面板一打开要同时画"有哪些音色"
    与"这条任务现在用哪个" —— 分两次请求会让第一帧显示成"没选音色"。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    available = usable_voices(connection)
    # 候选（`usable_voices`）与"念得出来"（`speakable_voices`）是两件事：前者回答
    # "这台机器上有什么"，后者回答"当前引擎认不认"。两个都要发给面板 —— 只发候选，
    # 用户会选中一个注定发不出声的音色；只发能念的，用户会以为"我刚入库的音色丢了"。
    speakable = set(speakable_voices(connection))
    profiles = {row.id for row in VoiceProfileRepo(connection).list_all(enabled_only=True)}
    voice_map: dict[str, str] = {}
    if task_id:
        voice_map = dict(TaskService(connection).get(task_id).payload.voice_map)
    return VoiceOptions(
        voices=[
            VoiceOption(
                id=name,
                source="profile" if name in profiles else "sapi",
                speakable=name in speakable,
            )
            for name in available
        ],
        task_id=task_id,
        voice_map=voice_map,
        note=_voices_note(available, speakable),
    )


def _voices_note(available: tuple[str, ...], speakable: set[str]) -> str | None:
    """下拉框旁边那句话（三种情形各一句，都不说就等于把决定权藏起来）。"""
    if not available:
        return NO_VOICE_HINT
    if any(name not in speakable for name in available):
        return PROFILE_UNSPEAKABLE_HINT
    return None


@router.get("/api/v1/sentences", response_model=SentenceVoiceList)
def list_sentences(
    request: Request, task_id: Annotated[str, Query(min_length=1, description="任务 id")]
) -> SentenceVoiceList:
    """一条任务的逐句配音状态（面板的主列表）。

    逐句的 ``audio_url`` 只在**盘上真有那一份**时才给（``preview_audio`` 判的），
    否则给 ``null``：发一个注定 404 的 url，面板上就是一个点了没反应的播放键。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    task = TaskService(connection).get(task_id)
    repo = SentenceRepo(connection)
    rows = repo.list_for_task(task_id)
    progress = repo.progress(task_id)
    total_ms = read_timeline_total_ms(state.paths, task_id)
    sentences = []
    for row in rows:
        found = preview_audio(paths=state.paths, row=row)
        sentences.append(
            sentence_voice_view(
                row,
                audio_url=None if found is None else _media_url(task_id, row.seq),
                audio_source=None if found is None else found[1],
            )
        )
    return SentenceVoiceList(
        task_id=task_id,
        task_status=task.status.value,
        progress=progress_view(progress),
        sentences=sentences,
        voice_map=dict(task.payload.voice_map),
        timeline_total_ms=total_ms,
        timeline_stale=total_ms is not None and progress.outstanding > 0,
    )


@router.post("/api/v1/sentences/{sentence_id}/resynth", response_model=ResynthResponse)
def resynth_sentence_endpoint(request: Request, sentence_id: str = _SENTENCE_ID) -> ResynthResponse:
    """单句重配：这一句退回待办 + 它的作业排回 voice 池（**立刻返回**）。

    真正念的是 voice 池 —— 这个端点只投递（与 T2.8 裁定 224 的"投递 / 排空两步"
    同一条）。面板随后轮询 ``GET /sentences`` 看它什么时候念完。
    """
    state: AppState = request.app.state.studio
    report = resynth_sentence(connection=state.connections.get(), sentence_id=sentence_id, paths=state.paths)
    return ResynthResponse.model_validate(report.to_dict())


@router.patch("/api/v1/tasks/{task_id}/voice_map", response_model=VoiceMapResponse)
def patch_voice_map(request: Request, body: VoiceMapRequest, task_id: str = _TASK_ID) -> VoiceMapResponse:
    """任务级换音色：写映射 + 受影响的句子全部失效并重排。

    不带 ``confirm`` 时**先算代价**：会重配 N 句 ⇒ 抛 ``VOICE_MAP_CONFIRM_REQUIRED``
    （409，``context.affected`` / ``context.sentences`` 给面板弹框）。带 ``confirm``
    才真写 —— 一次点击就重配几十句，这个代价要人点头。
    """
    state: AppState = request.app.state.studio
    report = set_voice_map(
        connection=state.connections.get(),
        task_id=task_id,
        voice_map=body.voice_map,
        confirm=body.confirm,
        paths=state.paths,
    )
    return VoiceMapResponse.model_validate(report.to_dict())


@router.get("/api/v1/media/{path:path}")
def get_media(request: Request, path: str) -> FileResponse:
    """单句试听（``<audio>`` 直接取这个 url，Range 由 Starlette 处理）。

    **只发盘上已经有的那一份**，一次合成都不触发。
    """
    state: AppState = request.app.state.studio
    matched = _MEDIA_KEY.match(path)
    if matched is None:
        raise StudioError(
            f"不是合法的媒资键：{path}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"path": path},
            remediation="媒资键形如 voice/<task_id>/s003.wav（逐句列表里的 audio_url 就是它）",
        )
    task_id = matched.group("task_id")
    seq = int(matched.group("seq"))
    row = SentenceRepo(state.connections.get()).get_by_seq(task_id, seq)
    if row is None:
        raise StudioError(
            f"任务 {task_id} 没有第 {seq} 句",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id, "seq": seq},
            remediation="刷新逐句列表：这一句可能已经随改稿被删掉了",
        )
    found = preview_audio(paths=state.paths, row=row)
    if found is None:
        raise StudioError(
            f"第 {seq} 句现在没有音频可播（tts_status={row.tts_status}）",
            code=ErrorCode.PATH_MISSING,
            context={"task_id": task_id, "seq": seq, "tts_status": row.tts_status},
            remediation=(
                "先跑配音（studio pipeline run <task_id> --until queued_render），或点这一句的「重配」"
            ),
        )
    media_type, _ = mimetypes.guess_type(found[0].name)
    return FileResponse(found[0], media_type=media_type or "audio/wav")
