"""发布前准备：封面 + 二次校验（T5.1 · §06.3 / §06.4）。

本模块是**缝合层**，与 `render_service` 同一个位置、同一条理由
-----------------------------------------------------------
`studio.publish.cover` 只认 ffmpeg 与一张图；`studio.publish.precheck` 只认"要发什么"
与"成片怎么样"。它们都不该知道"任务表里那条任务的稿子写了什么"。把"从库里捞出这一期的
标题、时间轴、成片路径"缝起来，是服务层的职责（§02.1 的依赖方向）。

它**不发布**（R14）
-------------------
`publish.enabled=false` 是出厂状态（`config/publish.yaml`），本模块一个字节都不往平台送。
它做的是发布前那两步：把封面画出来，把"能不能发"判出来。真正点下"发布"是 T5.2/T5.3。

成片路径为什么以 `manifest.json` 为准
------------------------------------
`{stamp}_{task_id}_final.mp4` 这个名字里带时间戳，**同一个任务可以有好几个**（重合成一次
就多一个）。挑哪一个不能靠"哪个名字大"，要靠**这条任务的 manifest 自己写的那个**
（它记录的是"这一版成片是用什么渲出来的"）。只有 manifest 丢了才退回按名字找最新的 ——
那种情况下"最新"是唯一还能用的线索。

`quality_json` 与 `manifest.json` 的关系
----------------------------------------
`quality_json` 是契约字段（§03.5.3），`manifest.json` 是**同一次渲染自己写的原始记录**。
`quality_json` 会被后一次渲染整体覆盖，而 manifest 跟着那一版成片走。发布门禁要判的是
"**盘上那个文件**贴没贴水印"，所以两者都要读，优先契约字段。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, cast

from studio.agents.base import AgentContext
from studio.core.clock import format_iso, utc_now
from studio.core.config import (
    AccountConfig,
    OutputsConfig,
    PlatformCode,
    PlatformConfig,
    PublishConfig,
    StickerConfig,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.queue import Job, JobStore
from studio.db.repositories.artifact_repo import ArtifactRepo
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import (
    MANUAL_REQUIRED,
    PublicationRepo,
    PublicationRow,
)
from studio.domain.cover import COVER_HEIGHT, COVER_WIDTH, CoverInput, CoverOutput, rule_cover_output
from studio.domain.enums import UnitType
from studio.domain.publish import (
    build_caption,
    fit_text,
    parse_unit_ref,
    render_tags,
    unit_ref,
)
from studio.domain.script import find_forbidden
from studio.domain.task_service import TaskService
from studio.publish.base import (
    REHEARSAL_PUBLISHER,
    Publisher,
    PublisherContext,
    PublishHealth,
    PublishRequest,
    PublishResult,
    get_publisher,
)
from studio.publish.cover import (
    CoverSticker,
    build_cover,
    resolve_frame_at_ms,
    write_cover_atomically,
)
from studio.publish.precheck import (
    PrecheckReport,
    PublishContent,
    load_banned_words,
    run_precheck,
)
from studio.publish.selectors import load_selector_pack
from studio.render.png_probe import probe_png
from studio.render.sticker import place_sticker, resolve_sticker
from studio.services.script_service import read_active_script

__all__ = [
    "AUDIT_CANCEL",
    "AUDIT_MANUAL_DONE",
    "AUDIT_RETRY",
    "CALIBRATED",
    "CALIBRATION_BROKEN",
    "CALIBRATION_NA",
    "COVER_KIND",
    "FIXTURE_ACCOUNT_ID",
    "PUBLISH_POOL",
    "TARGETS",
    "TTL_COVER",
    "UNCALIBRATED",
    "CoverReport",
    "CoverRequest",
    "DryRunReport",
    "DryRunRequest",
    "EnqueueReport",
    "PlatformOption",
    "PublicationBoard",
    "PublishService",
    "build_board",
    "cancel_publication",
    "default_platforms",
    "default_targets",
    "enqueue_publications",
    "health_payload",
    "mark_manual_done",
    "platform_options",
    "publisher_for",
    "read_json",
    "resolve_account",
    "resolve_accounts",
    "resolve_final_video",
    "resolve_platform",
    "retry_publication",
]

logger = get_logger("studio.services.publish")

#: ``artifacts.kind``（§03.3.11 的产物类型表）。
COVER_KIND: Final[str] = "cover"

#: 封面的 TTL 提示。封面与成片同寿（它跟着成片一起发出去），不参与回收 ——
#: 成片被 GC 删掉时封面会跟着成片一起走，不需要单独一条规则。
TTL_COVER: Final[str] = "forever"


@dataclass(frozen=True, slots=True)
class CoverRequest:
    """一次封面生成的请求。"""

    task_id: str
    #: 要不要走 Cover Agent。``False`` = 直接用规则兜底文案（离线 / 省钱 / 复现同一张图）。
    use_agent: bool = True
    #: 文件名里的时间戳（复现 / 测试用；``None`` = 现在）。
    stamp: str | None = None


@dataclass(frozen=True, slots=True)
class CoverReport:
    """一次封面生成的结果（CLI 与面板读同一份）。"""

    task_id: str
    ok: bool
    cover_path: Path | None
    frame_at_ms: int
    #: 文案从哪来：``agent``（模型）/ ``rule``（规则兜底）。
    source: str
    plan: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: 抽帧失败、退成纯色底了吗（§06.3 的第一级降级）。
    fallback_background: bool = False
    #: 模型那条路为什么没走通（``None`` = 走通了 / 没试）。
    agent_error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "cover_path": None if self.cover_path is None else self.cover_path.as_posix(),
            "frame_at_ms": self.frame_at_ms,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "fallback_background": self.fallback_background,
            "agent_error": self.agent_error,
            "warnings": list(self.warnings),
            "plan": self.plan,
        }


#: ``--target`` 的取值（T5.2）。``live`` = 真平台（要登录态）；``fixture`` = 本地靶页。
TARGETS: Final[tuple[str, ...]] = ("live", "fixture")

#: 靶页用的账号代号（**不是真实账号**，也不会落库：``platforms/fixture.py`` 拒绝非 dry_run）。
FIXTURE_ACCOUNT_ID: Final[str] = "_fixture"


@dataclass(frozen=True, slots=True)
class DryRunRequest:
    """一次发布演练的请求（T5.2 · §06.5.3，停在第 ⑥ 步之前）。"""

    task_id: str
    platform: str
    #: ``None`` ⇒ 取该平台第一个 ``enabled`` 账号（D1：默认单账号）。
    account_id: str | None = None
    #: ``live``（真平台）/ ``fixture``（本地靶页，不需要账号与网络）。
    target: str = "live"
    headless: bool = True
    #: 靶页的故障注入查询串，形如 ``"?logged_out=1"``。
    probe: str = ""
    #: 三个**覆盖项**：给了就用给的，没给就按稿件推导（见 :meth:`PublishService.dry_run`）。
    title: str | None = None
    caption: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """一次演练的完整结论（CLI 与面板读同一份）。"""

    task_id: str
    platform: str
    account_id: str
    target: str
    publisher: str
    selectors_version: str
    title: str
    caption: str
    tags: tuple[str, ...]
    video_path: Path | None
    cover_path: Path | None
    health: PublishHealth
    #: 没走到"点发布"这一步时是 ``None``（比如登录态就没过 —— 那时连页面都没进）。
    result: PublishResult | None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """演练成功 = 真的走到了第 ⑥ 步之前。

        ``result is None``（没跑）与 ``result.ok=False``（跑了但失败）**都不是成功**：
        前者是"没验到"，后者是"验出来有问题"，两者都不该让退出码变成 0。
        """
        return self.result is not None and self.result.ok

    @property
    def stopped_before_publish(self) -> bool:
        return self.result is not None and self.result.ok and self.result.url is None

    def to_dict(self) -> dict[str, Any]:
        evidence = self.result.evidence if self.result is not None else None
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "target": self.target,
            "publisher": self.publisher,
            "selectors_version": self.selectors_version,
            "ok": self.ok,
            "title": self.title,
            "caption": self.caption,
            "tags": list(self.tags),
            "video_path": None if self.video_path is None else self.video_path.as_posix(),
            "cover_path": None if self.cover_path is None else self.cover_path.as_posix(),
            "health": health_payload(self.health),
            "result": None
            if self.result is None
            else {
                "ok": self.result.ok,
                "status": str(self.result.status),
                "url": self.result.url,
                "platform_post_id": self.result.platform_post_id,
                "error_code": self.result.error_code,
                "error_message": self.result.error_message,
                "elapsed_ms": self.result.elapsed_ms,
                "stage": None if evidence is None else evidence.stage,
                "screenshot_path": None
                if evidence is None or evidence.screenshot_path is None
                else evidence.screenshot_path.as_posix(),
                "dom_snapshot_path": None
                if evidence is None or evidence.dom_snapshot_path is None
                else evidence.dom_snapshot_path.as_posix(),
            },
            "warnings": list(self.warnings),
        }


def resolve_final_video(task_id: str, paths: StudioPaths) -> Path | None:
    """这条任务的成片在哪（``manifest.json`` 优先，目录里按名字兜底）。

    两个来源都答不出来 ⇒ ``None``。**不抛**：调用方要区分"这条任务还没渲染"与
    "渲染了但文件被挪走"，而那是它自己的判断（一个报错文案的差别），不是这里的事。
    """
    payload = read_json(paths.manifest_json(task_id))
    if isinstance(payload, dict):
        value = payload.get("final")
        if isinstance(value, str) and value:
            candidate = Path(value)
            if candidate.is_file():
                return candidate
    directory = paths.videos_dir
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob(f"*_{task_id}_final*.mp4"))
    return matches[-1] if matches else None


def read_json(path: Path) -> Any:
    """读一个 JSON 文件；不存在 / 读不了 / 不是 JSON ⇒ ``None``（**不抛**）。

    ``timeline.json`` 与 ``manifest.json`` 都是"有更好、没有也能继续"的输入：
    时间轴缺失只影响抽帧点（退回默认值），manifest 缺失只影响成片路径的取法。
    为它们各抛一次异常，只会让"这条任务还没渲染"变成一个需要 try/except 的常态。

    **公开**（2026-09-22）：成片库也要读 ``manifest.json``（拿时长），而它是同一件
    事 —— "读一个可能不存在的 JSON"。各写一份的代价是两边对"读不出来"的处理慢慢
    分叉（一边退回默认值、一边抛），而它们读的是**同一个文件**。
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("publish.json_unreadable", path=path.as_posix())
        return None


def health_payload(health: PublishHealth) -> dict[str, Any]:
    """登录态探测结果 ⇒ 过 JSON 边界的那一份（T6.4 起**两处**共用）。

    演练报告（``DryRunReport.to_dict``）与账号面板的「检测登录态 / 扫码登录」说的是
    同一个东西。各拼一份的代价是"面板上多一个字段、CLI 的 ``--json`` 里少一个" ——
    而两边单看都没错。
    """
    return {
        "ready": health.ready,
        "logged_in": health.logged_in,
        "account_name": health.account_name,
        "hint": health.hint,
        "last_check_at": health.last_check_at,
    }


def _first_start_ms(timeline: Any) -> int:
    """时间轴第一句的 ``start_ms``（算不出来 ⇒ 0）。"""
    if not isinstance(timeline, dict):
        return 0
    sentences = timeline.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        return 0
    first = sentences[0]
    if not isinstance(first, dict):
        return 0
    value = first.get("start_ms")
    return value if isinstance(value, int) else 0


def cover_sticker_for(
    outputs: OutputsConfig | None,
    *,
    home: Path,
) -> tuple[CoverSticker | None, str | None]:
    """从合成配置里挑出封面的**主体贴图**，并算好它摆在哪儿。

    返回 ``(贴图, 为什么没有)``：两者**互斥**（有贴图就没有原因，反之亦然）。
    "没有"照样是一句话 —— 与渲染路径同一条口径：配了却没出现，必须有人能说出原因，
    否则用户看到的就是"我明明开了贴图，封面上怎么没有"。

    挑哪一层
    --------
    ``cover.sticker`` 点名了就**只认那一层**（点名而它关着 / 图坏了 ⇒ 如实报，不偷偷
    换一层 —— 偷偷换的结果是"我明明关掉了它，封面还是它"）；没点名 ⇒ 第一个
    "开着"的层。

    为什么摆放与成片不同
    --------------------
    成片里人物缩在右下角是给字幕让位；封面上没有字幕，人物是**主体**，所以居中。
    高度占比仍取这一层自己的 ``height_ratio`` —— 素材与大小是同一份配置，只有位置不同。
    """
    name, spec, problem = _pick_cover_layer(outputs)
    sticker: CoverSticker | None = None
    if problem is None and name is not None and spec is not None:
        sticker, problem = _place_cover_sticker(name, spec, home=home)
    return sticker, problem


def _pick_cover_layer(
    outputs: OutputsConfig | None,
) -> tuple[str | None, StickerConfig | None, str | None]:
    """挑出封面要用的那一层贴图 ⇒ ``(名字, 配置, 为什么挑不出来)``。

    两个问题分开答（"用哪一层"与"这一层摆得下吗"），是因为它们各自会失败：
    配置里没这一层、这一层关着、图坏了、放不进画布 —— 四句话分别对应四种修法。
    """
    if outputs is None:
        return None, None, "没有读到合成配置（config/outputs.yaml）"
    if not outputs.stickers:
        return None, None, "合成配置里没有 stickers 段"

    wanted = outputs.cover.sticker
    if wanted:
        spec = outputs.stickers.get(wanted)
        if spec is None:
            known = "、".join(outputs.stickers)
            return None, None, f"cover.sticker 点名的「{wanted}」不在 stickers 里（现有：{known}）"
    else:
        picked = next(((key, item) for key, item in outputs.stickers.items() if item.enabled), None)
        if picked is None:
            return None, None, "stickers 里没有一层是开着的"
        wanted, spec = picked

    if not spec.enabled:
        return None, None, f"「{wanted}」在 stickers 里是关着的"
    return wanted, spec, None


def _place_cover_sticker(
    name: str,
    spec: StickerConfig,
    *,
    home: Path,
) -> tuple[CoverSticker | None, str | None]:
    """把一层贴图摆到封面上 ⇒ ``(贴图, 为什么摆不上)``。"""
    resolved = resolve_sticker(spec, name=name, canvas_height=COVER_HEIGHT, home=home)
    # 位置换成 ``center``：见 :func:`cover_sticker_for` 的"为什么摆放与成片不同"。
    # 其余参数（高度 / 透明度 / 素材）一个不动 —— 换位置是**封面这一件事**，
    # 不该顺手把别的也改了。
    placed = resolved.spec.model_copy(update={"position": "center"})
    asset = probe_png(placed.image_path)
    if not asset.usable:
        return None, f"「{name}」的图不能用：{asset.problem or '没有透明通道'}"

    box = place_sticker(placed, asset, canvas_width=COVER_WIDTH, canvas_height=COVER_HEIGHT)
    if box is None:
        return None, f"「{name}」放不进封面画布（{COVER_WIDTH}×{COVER_HEIGHT}）"
    return (
        CoverSticker(
            name=name,
            path=placed.image_path,
            x=box.x,
            y=box.y,
            width_px=box.width_px,
            height_px=box.height_px,
            opacity=placed.opacity,
        ),
        None,
    )


def _clamp_frame(frame_at_ms: int, *, duration_ms: int) -> int:
    """把模型挑的抽帧点钳在片子里（§06.3："必须在 0–duration 之间"）。

    为什么是 ``duration_ms - 1`` 而不是 ``duration_ms``：``-ss`` 取的是**这一时刻之后**
    的第一帧，落在片尾那一毫秒上会解不出帧 ⇒ 整张封面降级成纯色底。
    宁可少取 1 毫秒。
    """
    if duration_ms <= 0:
        return max(0, frame_at_ms)
    return max(0, min(frame_at_ms, duration_ms - 1))


class PublishService:
    """发布前准备（封面 + 二次校验）。

    ``publish`` / ``persona`` / ``outputs`` 由**入口**注入（与 `ScriptService` 同一手法）：
    服务只认数据，不去读配置文件 —— 那会让每个测试都得先铺一份 config。

    ``outputs``（合成配置）只被封面那一条路用到：封面的**主体贴图**是
    ``config/outputs.yaml`` 的 ``stickers`` 里的一层（"合成配置里的贴图"），
    样式也来自它的 ``cover`` 一节。不给 ⇒ 封面照出，只是不贴人物。
    """

    def __init__(
        self,
        connection: Any,
        *,
        paths: StudioPaths,
        publish: PublishConfig | None = None,
        persona: Any = None,
        outputs: OutputsConfig | None = None,
        cover_agent: Any = None,
        build: Callable[..., Any] | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._publish = publish
        self._persona = persona
        self._outputs = outputs
        self._cover_agent = cover_agent
        self._build = build or build_cover
        self._tasks = TaskService(connection)

    # ── 封面 ────────────────────────────────────────────────────────

    async def make_cover(self, request: CoverRequest) -> CoverReport:
        """给这条任务出一张封面（§06.3）。

        失败**不抛**：封面是可选装饰，"没有封面发布"是契约里写明的合法结局。
        调用方拿到 ``ok=False`` 与一份 ``plan``（含失败原因），自己决定要不要提示人。
        """
        task = self._tasks.get(request.task_id)  # 任务不存在 ⇒ TaskNotFound（这是用法错误）
        script_payload = read_active_script(self._connection, request.task_id)
        script = script_payload[0] if script_payload else None

        timeline = read_json(self._paths.timeline_json(request.task_id))
        manifest = read_json(self._paths.manifest_json(request.task_id))
        duration_ms = _duration_ms(timeline, manifest)
        frame_at_ms = resolve_frame_at_ms(timeline)

        cover_input = CoverInput(
            task_id=request.task_id,
            title=(script.title if script else "") or task.title or "",
            hook=(script.hook if script else "") or "",
            cta=(script.cta if script else "") or "",
            hook_start_ms=_first_start_ms(timeline),
            duration_ms=duration_ms,
            forbidden=list(self.forbidden_terms()),
        )
        output, source, agent_error = await self._cover_text(request, cover_input, frame_at_ms)
        output = output.model_copy(
            update={"frame_at_ms": _clamp_frame(output.frame_at_ms, duration_ms=duration_ms)}
        )

        # 封面主体：合成配置里的一层贴图（T5.1 追加）。挑不出来也照出封面 ——
        # 与水印 / 贴图在成片里同一条"装饰不阻塞"的口径。
        sticker, sticker_note = cover_sticker_for(self._outputs, home=self._paths.home)

        target = self._paths.cover_image(request.task_id, stamp=request.stamp)
        # ``.partial`` 插在扩展名**之前**：``ffmpeg`` 按扩展名挑封装器，
        # ``xxx.jpg.partial`` 它会当成一种没见过的格式直接失败（成片那条用的是
        # ``_final.partial.mp4``，同一个理由）。
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        result = self._build(
            output=output,
            target=partial,
            paths=self._paths,
            source=resolve_final_video(request.task_id, self._paths),
            sticker=sticker,
            style=None if self._outputs is None else self._outputs.cover,
        )
        warnings = [*result.warnings]
        if sticker_note is not None:
            warnings.append(f"封面没有人物贴图：{sticker_note}")
        if agent_error:
            warnings.append(f"封面文案走了规则兜底：{agent_error}")

        cover_path = write_cover_atomically(result, target) if result.ok else None
        if cover_path is None:
            warnings.append("封面没出来，按 §06.3 走无封面发布（平台会用首帧）")

        plan = dict(result.plan)
        plan["source"] = source
        plan["sticker_note"] = sticker_note
        plan["target"] = target.as_posix()
        plan["final_frame_at_ms"] = output.frame_at_ms
        plan["fallback_background"] = result.fallback_background
        plan["ok"] = cover_path is not None
        self._tasks.set_cover_path(request.task_id, cover_path=cover_path, plan=plan)
        if cover_path is not None:
            self._record_artifact(request.task_id, cover_path, plan=plan)

        return CoverReport(
            task_id=request.task_id,
            ok=cover_path is not None,
            cover_path=cover_path,
            frame_at_ms=output.frame_at_ms,
            source=source,
            plan=plan,
            warnings=warnings,
            fallback_background=result.fallback_background,
            agent_error=agent_error,
            duration_ms=duration_ms,
        )

    async def _cover_text(
        self,
        request: CoverRequest,
        cover_input: CoverInput,
        frame_at_ms: int,
    ) -> tuple[CoverOutput, str, str | None]:
        """拿封面文案：先试模型，不行就退规则。

        返回 ``(文案, 来源, 模型那条路失败的原因)``。
        """
        agent_error: str | None = None
        if request.use_agent and self._cover_agent is not None and self._persona is not None:
            ctx = AgentContext(
                task_id=request.task_id,
                persona=self._persona,
                trace_id=new_ulid(),
            )
            result = await self._cover_agent.run(ctx, cover_input)
            if result.ok and result.data is not None:
                return result.data, "agent", None
            agent_error = result.error_message or result.error_code or "模型没有返回可用结果"
            logger.info("publish.cover_agent_failed", task_id=request.task_id, reason=agent_error)
        elif request.use_agent:
            agent_error = "没接上 Cover Agent（缺 persona / 通道）"

        output = rule_cover_output(cover_input, frame_at_ms=frame_at_ms)
        hits = find_forbidden(
            f"{output.title_text}{output.sub_text or ''}",
            list(self.forbidden_terms()),
        )
        if hits:
            # 规则兜底用的是**稿件自己的标题**，它命中了禁区词 ⇒ 不能假装没看见。
            # 但也不抛：封面是装饰，一条能发的片子不该因为它卡住。
            # 把 ``banned_checked`` 如实置假，让读这份 plan 的人看得见。
            output = output.model_copy(update={"banned_checked": False})
            logger.warning("publish.cover_text_banned", task_id=request.task_id, hits=hits)
        return output, "rule", agent_error

    def _record_artifact(self, task_id: str, cover_path: Path, *, plan: dict[str, Any]) -> None:
        """记一条 ``artifacts(kind='cover')``（§06.3 的留痕要求）。

        ``path`` 由 repo 自己转成相对 ``data/`` 的形式（``ArtifactRepo.record`` 的契约）。
        """
        ArtifactRepo(self._connection, data_dir=self._paths.data_dir).record(
            task_id=task_id,
            kind=COVER_KIND,
            path=cover_path,
            ttl_hint=TTL_COVER,
            meta={
                "frame_at_ms": plan.get("frame_at_ms"),
                "title_text": plan.get("title_text"),
                "source": plan.get("source"),
                "title_font_size": plan.get("title_font_size"),
                "title_truncated": plan.get("title_truncated"),
                "fallback_background": plan.get("fallback_background"),
            },
        )

    # ── 二次校验 ────────────────────────────────────────────────────

    def forbidden_terms(self) -> tuple[str, ...]:
        """禁区词 = ``persona.forbidden`` + ``prompts/shared/banned_words.yaml``。

        顺序固定（persona 在前）：命中文案的报错要**先报频道自定义的那几个词** ——
        那是这个人自己定的规矩，比通用词表更需要被看见。
        去重保留首次出现，避免同一个词在报告里出现两遍。
        """
        persona_terms = tuple(getattr(self._persona, "forbidden", ()) or ())
        merged: list[str] = []
        for term in (*persona_terms, *load_banned_words(self._paths)):
            if term and term not in merged:
                merged.append(term)
        return tuple(merged)

    def precheck(
        self,
        task_id: str,
        *,
        caption: str = "",
        tags: Sequence[str] = (),
        measure: Callable[[Path], tuple[float, float] | None] | None = None,
    ) -> PrecheckReport:
        """跑三道门禁 + 禁区扫描（§06.4）。

        ``caption`` / ``tags`` 现在恒为空：发布文案是 T5.2 的产物，T5.1 还没有它。
        留参数而不是写死空值，是为了让 T5.2 接进来时**只加调用、不改签名**。
        """
        task = self._tasks.get(task_id)
        video = resolve_final_video(task_id, self._paths)
        if video is None:
            raise StudioError(
                f"任务 {task_id} 在盘上找不到成片，无法做发布前校验",
                code=ErrorCode.RENDER_FAILED,
                context={"task_id": task_id, "videos_dir": self._paths.videos_dir.as_posix()},
                remediation="先跑 `studio render make --task-id …` 或 `studio pipeline run --task …`",
            )
        script_payload = read_active_script(self._connection, task_id)
        script = script_payload[0] if script_payload else None
        title = (script.title if script else None) or task.title or task_id
        cover_path = task.context.get("cover_path")
        content = PublishContent(
            task_id=task_id,
            title=title,
            video_path=video,
            caption=caption,
            tags=list(tags),
            cover_path=Path(cover_path) if isinstance(cover_path, str) and cover_path else None,
        )
        return run_precheck(
            content,
            config=getattr(self._publish, "precheck", None),
            quality=task.quality,
            forbidden_terms=self.forbidden_terms(),
            measure=measure,
            manifest_applied=_manifest_watermark(self._paths, task_id),
            av_sync_offset_ms=task.quality.av_sync_offset_ms,
        )

    # ── 发布演练（T5.2 · §06.5.3）──────────────────────────────────

    async def dry_run(self, request: DryRunRequest) -> DryRunReport:
        """走完 §06.5.3 的前七步，**停在第 ⑥ 步之前**（一个字节都不发出去）。

        为什么这条链路**不看** ``publish.enabled``
        ----------------------------------------
        它的全部意义就是"在开关还关着的时候，验证链路是通的"。要求先打开 ``enabled``
        才能演练，等于让人拿**真发布**当验证手段 —— 那正是 R14 要防的事。
        真正的保护在下面这一行：``PublishRequest(dry_run=True)`` ⇒ 发布器走到第 ⑥ 步
        直接返回，**发布按钮一次都没被点**（本地靶页把这件事做成了可断言的事实）。

        为什么登录态不过就**不往下跑**
        ----------------------------
        第 ① 步本来也会查登录，再起一次浏览器只会拿到同一个答案 —— 而"未登录"
        这个答案本身就已经是**可照做的**结论了（去扫码）。省下的那次启动，
        换来的是排障时不用等两遍 30s 的导航超时。
        """
        config = self._publish
        if config is None:
            raise StudioError(
                "没有注入发布配置，无法演练",
                code=ErrorCode.CONFIG_INVALID,
                remediation="入口层把 config/publish.yaml 的 publish 段传进来",
            )
        if request.target not in TARGETS:
            raise StudioError(
                f"未知的演练目标：{request.target}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"target": request.target, "known": list(TARGETS)},
            )
        # ``request.platform`` 到这一行之前只是个字符串（CLI 传什么就是什么）；
        # 它是不是真平台，由下面这次查表回答 —— 查不到就抛，不会走到 cast。
        platform = cast("PlatformCode", request.platform)
        platform_cfg = config.platforms.get(platform)
        if platform_cfg is None:
            raise StudioError(
                f"config/publish.yaml 里没有平台 {request.platform}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"platform": request.platform, "known": sorted(config.platforms)},
                remediation="在 config/publish.yaml 的 platforms 里补一段",
            )

        account = self._dry_run_account(config, request)
        task = self._tasks.get(request.task_id)
        video = resolve_final_video(request.task_id, self._paths)
        if video is None:
            raise StudioError(
                f"任务 {request.task_id} 在盘上找不到成片，无法演练",
                code=ErrorCode.RENDER_FAILED,
                context={"task_id": request.task_id},
                remediation="先跑 `studio render make --task-id …`",
            )

        script_payload = read_active_script(self._connection, request.task_id)
        script = script_payload[0] if script_payload else None
        title = request.title if request.title is not None else _default_title(script, task, request.task_id)
        raw_caption = request.caption if request.caption is not None else _default_caption(script)
        tag_plan = render_tags(request.tags, syntax=platform_cfg.tag_syntax, max_tags=platform_cfg.tag_max)
        caption_plan = build_caption(raw_caption, tag_plan, caption_max=platform_cfg.caption_max)
        cover_path = _cover_path(task)
        warnings = _text_warnings(
            title, caption_plan, platform_cfg=platform_cfg, platform=request.platform, cover=cover_path
        )

        # ``fixture`` 目标用靶页的发布器；其余走配置里指的那个平台实现。
        publisher_code = "fixture" if request.target == "fixture" else platform_cfg.publisher
        context = PublisherContext(
            paths=self._paths,
            account=account,
            platform=platform_cfg,
            headless=request.headless,
            probe=request.probe,
        )
        publisher = get_publisher(publisher_code)(context)

        health = await publisher.health()
        result: PublishResult | None = None
        if health.ready:
            result = await publisher.publish(
                PublishRequest(
                    task_id=request.task_id,
                    platform=request.platform,
                    account_id=account.account_id,
                    video_path=video,
                    title=title,
                    caption=caption_plan.text,
                    tags=caption_plan.tags,
                    cover_path=cover_path,
                    dry_run=True,
                )
            )
        else:
            warnings.append(f"登录态没过，没进创作页：{health.hint or '未登录'}")

        return DryRunReport(
            task_id=request.task_id,
            platform=request.platform,
            account_id=account.account_id,
            target=request.target,
            publisher=publisher_code,
            selectors_version=str(getattr(publisher, "selectors_version", "") or ""),
            title=title,
            caption=caption_plan.text,
            tags=caption_plan.tags,
            video_path=video,
            cover_path=cover_path,
            health=health,
            result=result,
            warnings=warnings,
        )

    def _dry_run_account(self, config: PublishConfig, request: DryRunRequest) -> AccountConfig:
        """演练用哪个账号。

        ``fixture`` 目标**现造**一个：靶页不需要登录态，而"为了演练先在配置里
        加一个假账号"会让配置里出现一条永远发不出去的东西。现造的那个代号是
        ``_fixture``，profile 落在 ``data/browser_profile/_fixture/`` ——
        与真实账号的登录态**物理隔离**（不然演练会把真账号的 cookie 弄脏）。
        """
        if request.target == "fixture":
            return AccountConfig(
                account_id=FIXTURE_ACCOUNT_ID,
                platform=cast("PlatformCode", request.platform),
                display_name="本地靶页",
                profile_dir=self._paths.browser_profile_dir / FIXTURE_ACCOUNT_ID,
                enabled=True,
            )
        enabled = list(config.enabled_accounts)
        if request.account_id:
            for account in enabled:
                if account.account_id == request.account_id:
                    return account
            raise StudioError(
                f"没有启用的账号 {request.account_id}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"account_id": request.account_id, "enabled": [a.account_id for a in enabled]},
                remediation="在 config/publish.yaml 的 accounts 里加一条并 enabled: true",
            )
        for account in enabled:
            if account.platform == request.platform:
                return account
        raise StudioError(
            f"平台 {request.platform} 没有启用的账号",
            code=ErrorCode.CONFIG_INVALID,
            context={"platform": request.platform},
            remediation="在 config/publish.yaml 的 accounts 里加一条（首次发布前需人工扫码登录）",
        )


# ── 投递：把一条任务排进发布池（T5.3 · §03.3.9 / §03.3.10）──────────────

#: 发布池名（与 DDL 的 ``jobs.pool`` CHECK 一致）
PUBLISH_POOL: Final[str] = "publish"

#: 三个人工处置动作的留痕名（§06.10 不变量 3：**处置动作必须写 audit_ops**）。
AUDIT_RETRY: Final[str] = "publish.retry"
AUDIT_CANCEL: Final[str] = "publish.cancel"
AUDIT_MANUAL_DONE: Final[str] = "publish.manual_done"


@dataclass(frozen=True, slots=True)
class EnqueueReport:
    """一次投递的结论（CLI / 面板 / 定时器读同一份）。

    ``skipped`` 与 ``queued`` 分开报，而不是只回一个数字：投递这条路有三种"没投出去"
    —— 平台没启用、这条早投过了、开关关着 —— 它们的**处置动作完全不同**
    （改配置 / 什么都不用做 / 打开开关）。合成一个 0 之后，操作员只能靠猜。
    """

    task_id: str
    platforms: tuple[str, ...]
    queued: int
    #: 没投出去的平台及原因（``"douyin：已经投过"``）。
    skipped: tuple[str, ...] = ()
    #: 其中"**这条早就投过**"（幂等命中）的平台。**单独列出来**是因为它的处置动作与
    #: 其余跳过完全不同：其余跳过要人去改配置，而这一种**什么都不用做** —— 让调用方
    #: 去猜那句中文，等于把"这不是故障"这件事绑在一句文案上（定时器要按它决定记不记失败）。
    duplicates: tuple[str, ...] = ()
    #: 任务**不存在**时也走这条路（返回而不是抛）：投递常由"任务完成"的事件触发，
    #: 那时任务一定存在；而人工点一次不存在的任务号，报错比静默好。见 ``missing``。
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "platforms": list(self.platforms),
            "queued": self.queued,
            "skipped": list(self.skipped),
            "duplicates": list(self.duplicates),
            "missing": self.missing,
        }


def resolve_platform(config: PublishConfig, platform: str) -> PlatformConfig:
    """平台配置；``config/publish.yaml`` 里没有 ⇒ 抛（**不猜**）。

    **不看 ``enabled``**：这条函数回答的是"有没有这个平台"，而"能不能发"是调用方
    自己的判断 —— 演练（T5.2）与真发布（T5.3）对 ``enabled`` 的处置**不一样**
    （演练放行、真发拒绝），把它塞进这里会让两种语义挤在一个返回值里。
    """
    cfg = config.platforms.get(cast("PlatformCode", platform))
    if cfg is None:
        raise StudioError(
            f"config/publish.yaml 里没有平台 {platform or '(空)'}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"platform": platform, "known": sorted(config.platforms)},
            remediation="平台代号只能是 config/publish.yaml 的 platforms 键",
        )
    return cfg


def next_metric_at(config: PublishConfig) -> str | None:
    """数据回收的**第一个**时点（``metrics_schedule_hours`` 里最小的那个）。

    发布成功那一刻写进 ``publications.next_metric_at``；到点由回收泵去读一次。
    配成空 / 全是 0 ⇒ ``None``（"这条不回收数据"，而不是"立刻就回收"）。

    为什么是**共用**的一份（T5.3 原本写在 ``publish_worker`` 里）
    --------------------------------------------------------------
    "发出去之后什么时候去看它一眼"这件事，自动发布（worker）与人工过验证
    （``publish_assist_service``）回答的必须是同一个数 —— 两处各算一遍的代价是
    某一天其中一处忘了跟着配置改，而症状是"面板发的那些永远不回收数据"。
    """
    hours = [int(hour) for hour in config.metrics_schedule_hours if int(hour) > 0]
    if not hours:
        return None
    return format_iso(utc_now() + timedelta(hours=min(hours)))


def publisher_for(
    paths: StudioPaths,
    config: PublishConfig,
    account: AccountConfig,
    *,
    headless: bool = True,
    await_manual_verify: bool = False,
) -> Publisher:
    """按**一个账号**装配一个 Publisher（T6.4：面板上的「检测登录态 / 扫码登录」用）。

    一个账号一个实例：``PublisherContext.profile_dir`` 是
    ``data/browser_profile/<account_id>``，两个账号共用一个实例等于共用一份登录态
    （§06.2.4「登录态隔离」）。

    ``headless``：探测用无头（看不见、跑得快），扫码登录必须可见 —— 无头窗口里
    没有人能扫那个码。

    ``await_manual_verify``：**有人在场**（T6.4 的「人工过验证」）。它默认 False，
    因为"窗口可见"与"窗口前面有人"是两件事（见 ``PublisherContext`` 那段）。
    这两个参数**互相独立**：一个可见窗口配 ``await_manual_verify=False`` 是完全
    合法的组合（排障时人走开了）。
    """
    platform_cfg = resolve_platform(config, account.platform)
    context = PublisherContext(
        paths=paths,
        account=account,
        platform=platform_cfg,
        headless=headless,
        await_manual_verify=await_manual_verify,
    )
    return get_publisher(platform_cfg.publisher)(context)


def resolve_accounts(
    config: PublishConfig,
    *,
    platform: str,
    account_ids: Sequence[str] | None = None,
) -> tuple[AccountConfig, ...]:
    """这个平台上要发到**哪几个账号**（T5.8 · §06.2.4）。

    ``account_ids`` 缺省 ⇒ 该平台**全部**启用账号（矩阵运营的默认意图：同一期内容
    铺满这个平台的所有号）；显式给了 ⇒ 只发点名的那些（顺序按 ``accounts`` 的书写顺序，
    与面板上的顺序一致）。

    ``account_ids`` 是一份**全局**名单，这里取它与本平台启用账号的**交集**
    ------------------------------------------------------------------
    面板上的账号勾选框是分平台画的，而请求体里只有一份 ``account_ids``（T5.10 的
    投递区块）。取交集是唯一能让"勾了 douyin 的 acc_b、同时也要发 kuaishou"这种
    组合表达出来的读法：``account_ids=['acc_b', 'ks_main']`` 落到 douyin 就是
    ``['acc_b']``、落到 kuaishou 就是 ``['ks_main']``。

    严格版（名单里出现任何一个不属于本平台的账号就整条报错）在这里是**错的**：
    它会把"另一个平台的账号"读成"这个平台的误配"，于是面板上勾了两个平台、
    点一次投递，其中一个平台被整条跳过 —— 而用户什么都没做错。

    **交集为空才算错**（这个平台上点名的账号一个都不存在）：那才是真的误配，
    文案里带上这个平台**有哪些**账号，操作员一眼能看出是名字写错了还是账号没启用。

    为什么默认是"全部"而不是"第一个"
    --------------------------------
    "配了第二个账号，但得手动点名才发"这件事**失败起来是静默的** —— 面板上有一条
    ``published``，看起来完全正常，要等到人自己去平台上数才发现少了一半。默认全发，
    少发就变成一个必须显式表达的动作（传 ``account_ids``）。

    **演练台不在这里排除**：排除归调用方（:func:`default_targets`），因为"显式点名
    演练台"是一条正经用法（T5.9），把它写进这里会让那条路也走不通。
    """
    enabled = [a for a in config.enabled_accounts if a.platform == platform]
    if account_ids:
        wanted = list(account_ids)
        picked = [a for a in enabled if a.account_id in wanted]
        if not picked:
            raise StudioError(
                f"平台 {platform} 上没有启用的账号 {'、'.join(wanted)}",
                code=ErrorCode.VALIDATION_FAILED,
                context={
                    "platform": platform,
                    "account_ids": wanted,
                    "enabled": [a.account_id for a in enabled],
                },
                remediation="在 config/publish.yaml 的 accounts 里加一条并 enabled: true",
            )
        return tuple(picked)
    if not enabled:
        raise StudioError(
            f"平台 {platform} 没有启用的账号",
            code=ErrorCode.CONFIG_INVALID,
            context={"platform": platform},
            remediation="在 config/publish.yaml 的 accounts 里加一条（首次发布前需人工扫码登录）",
        )
    return tuple(enabled)


def resolve_account(
    config: PublishConfig,
    *,
    platform: str,
    account_id: str | None = None,
) -> AccountConfig:
    """这条发布用哪个账号：指定的那个 ⇒ 它；没指定 ⇒ 该平台**唯一**启用的那个。

    **单数版**，给"手里只有一条作业、必须问出是哪个账号"的场景用（发布池的
    worker 就是这一种）。投递侧（一条任务可以铺多个账号）用
    :func:`resolve_accounts`。

    "该平台有多个启用账号 + 没指定是哪个" ⇒ **报错**而不是挑第一个：那时候
    调用方手里那条作业**说不出**它属于哪个账号，挑第一个会让它安静地发错号。
    T5.8 之后新投的作业都把账号写进了 ``unit_ref``（``platform:account_id``），
    所以这一条只会被**加第二个账号之前投的老作业**撞上 —— 报错文案因此直接告诉
    操作员"重投一次即可"（重投会按新格式生成作业）。
    """
    enabled = [a for a in config.enabled_accounts if a.platform == platform]
    if account_id:
        for account in enabled:
            if account.account_id == account_id:
                return account
        raise StudioError(
            f"平台 {platform} 上没有启用的账号 {account_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"account_id": account_id, "enabled": [a.account_id for a in enabled]},
            remediation="在 config/publish.yaml 的 accounts 里加一条并 enabled: true",
        )
    if not enabled:
        raise StudioError(
            f"平台 {platform} 没有启用的账号",
            code=ErrorCode.CONFIG_INVALID,
            context={"platform": platform},
            remediation="在 config/publish.yaml 的 accounts 里加一条（首次发布前需人工扫码登录）",
        )
    if len(enabled) > 1:
        raise StudioError(
            f"平台 {platform} 有 {len(enabled)} 个启用账号，而这条作业没说它是发给谁的",
            code=ErrorCode.CONFIG_INVALID,
            context={"platform": platform, "accounts": [a.account_id for a in enabled]},
            remediation=(
                "这条作业是加第二个账号之前投的（unit_ref 里没有账号）—— "
                "重投一次即可：新的作业会写成 platform:account_id"
            ),
        )
    return enabled[0]


def enqueue_publications(
    *,
    connection: sqlite3.Connection,
    task_id: str,
    config: PublishConfig,
    platforms: Sequence[str] | None = None,
    account_ids: Sequence[str] | None = None,
    dry_run: bool | None = None,
    scheduled_at: str | None = None,
) -> EnqueueReport:
    """把这条任务排进发布池（幂等；返回**这次真投出去**的条数）。

    目标 = ``平台 × 账号``（T5.8 · §06.2.4）
    --------------------------------------
    ``platforms`` 缺省 = :func:`default_targets`（每个启用账号一个目标，演练台除外）；
    ``account_ids`` 缺省 = 这些平台下的**全部**启用账号。所以"出厂配置 + 什么都不传"
    仍然只投一条（douyin 一个账号），而"加了第二个账号 + 什么都不传"会投两条 ——
    后者正是矩阵运营要的，且它**不需要调用方改一个字**。

    ``platforms`` 给定时，未启用的平台落进 ``skipped`` 而**不抛**：二线平台
    ``enabled=false`` 是出厂状态（Q9），一条"把这些平台都发一遍"的批量指令不该整批失败。
    点名了某个平台下**不存在**的账号，同样落 ``skipped``（文案里带上那个平台有哪些账号）
    —— 同一个理由：批量指令里"有一路发不出去"是常态，把它做成异常，调用方就得为
    "投一批"写 try/except。

    ``publications`` 那一行**不在这里建**：它要的是"这一版成片的路径与哈希、这一版的
    标题与文案"，而那是发布**那一刻**的事实（worker 手里才有）。这里只投作业。

    幂等由 ``JobStore.enqueue`` 保证（``(task_id, pool, unit_type, unit_ref)`` 上有唯一
    约束，冲突即 ``DO NOTHING``）—— 所以"任务完成后自动投一遍 + 人工再点一遍"是安全的。
    单元标识是 ``platform:account_id``（T5.8），因此**同一条任务在两个账号上是两条作业**，
    互不顶掉（§06.2.4 的幂等键里也算进了账号）。
    """
    store = JobStore(connection)
    tasks = TaskService(connection)
    try:
        tasks.get(task_id)
    except StudioError:
        logger.info("publish.enqueue_missing_task", task_id=task_id)
        return EnqueueReport(task_id=task_id, platforms=(), queued=0, missing=True)

    wanted: list[str] = []
    for platform in platforms if platforms is not None else default_platforms(config):
        if platform not in wanted:
            wanted.append(platform)

    queued = 0
    skipped: list[str] = []
    duplicates: list[str] = []
    for platform in wanted:
        platform_cfg = resolve_platform(config, platform)
        if not platform_cfg.enabled:
            skipped.append(f"{platform}：平台未启用（§06.2.1 · Q9）")
            continue
        try:
            accounts = resolve_accounts(config, platform=platform, account_ids=account_ids)
        except StudioError as exc:
            # 只可能是"点名的账号在这个平台下不存在"（``resolve_accounts`` 的另一种抛法
            # 是"这个平台一个启用账号都没有"，那一条同样该跳过 —— 判据与面板上的
            # ``selectable`` 是同一套：没有账号的平台点不动）。
            skipped.append(f"{platform}：{exc.message}")
            continue
        for account in accounts:
            payload: dict[str, Any] = {"account_id": account.account_id}
            if dry_run is not None:
                payload["dry_run"] = bool(dry_run)
            if scheduled_at:
                payload["scheduled_at"] = scheduled_at
            job_id = store.enqueue(
                task_id=task_id,
                pool=PUBLISH_POOL,
                unit_type=UnitType.PUBLISH.value,
                unit_ref=unit_ref(platform, account.account_id),
                payload=payload,
            )
            if job_id is None:
                # 幂等命中的条目**带账号**：同一个平台上两个账号，一个是"早就发过"、
                # 另一个是"刚投出去"，只写平台代号的话这两件事在面板上长得一样。
                skipped.append(f"{platform}/{account.account_id}：已经投过（幂等命中）")
                duplicates.append(f"{platform}/{account.account_id}")
                continue
            queued += 1
    logger.info(
        "publish.enqueued",
        task_id=task_id,
        queued=queued,
        platforms=wanted,
        skipped=skipped,
    )
    return EnqueueReport(
        task_id=task_id,
        platforms=tuple(wanted),
        queued=queued,
        skipped=tuple(skipped),
        duplicates=tuple(duplicates),
    )


# ── 投递面板的选项清单（T5.10）─────────────────────────────────────────

#: 面板上"校准"那一列的四个状态（T5.14）。**服务端算**，面板只负责画 ——
#: 与 ``selectable`` / ``note`` 同一条理由：面板自己判一遍，就有第二种真相。
#:
#: ``calibrated``  这个平台的 pack 在真机上逐条验过（``selectors/<x>.yaml`` 的 ``calibrated``）
#: ``uncalibrated`` 是真实现，但那些 CSS **没在真机上验过**（七份里六份都是这一档）
#: ``broken``       选择器包**装不起来**（少了文件 / yaml 写坏了）—— 这个平台现在一定发不出去
#: ``n/a``          不是平台（本地演练台）："真机校准"对它没有意义
CALIBRATED: Final[str] = "calibrated"
UNCALIBRATED: Final[str] = "uncalibrated"
CALIBRATION_BROKEN: Final[str] = "broken"
CALIBRATION_NA: Final[str] = "n/a"


@dataclass(frozen=True, slots=True)
class PlatformOption:
    """投递面板上的一个平台选项（**面板要的那份真相**）。"""

    code: str
    publisher: str
    enabled: bool
    rehearsal: bool
    accounts: tuple[str, ...]
    #: 点得动吗 —— 判据与投递期**同一套**（平台启用 且 有启用的账号）。
    selectable: bool
    #: 一句人话：点了会怎样 / 为什么点不动。
    note: str
    #: 见上面四个常量。
    calibration: str
    #: "校准"那一列的人话（服务端算，面板不拼句子）。
    calibration_note: str
    #: 这份 pack **已经确认没做**的部分（比如 B 站的必选分区）。
    #:
    #: 与 ``calibration`` 是**两件事**：校准回答"这些选择器对不对"，这一条回答
    #: "还有没有一段流程压根没写"。只显示前者会让人以为"校准完就能发了"。
    known_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "publisher": self.publisher,
            "enabled": self.enabled,
            "rehearsal": self.rehearsal,
            "accounts": list(self.accounts),
            "selectable": self.selectable,
            "note": self.note,
            "calibration": self.calibration,
            "calibration_note": self.calibration_note,
            "known_gaps": list(self.known_gaps),
        }


def _calibration(code: str, *, rehearsal: bool) -> tuple[str, str, tuple[str, ...]]:
    """这个平台的选择器**验过没有** ⇒ ``(状态, 一句人话, 已知缺口)``。

    为什么读不到 pack 要报 ``broken`` 而不是抛：这一列画在**投递面板**上，而"某个
    平台的选择器包装不起来"恰恰是最该让人看见的一件事 —— 为它把整个面板打成 500，
    等于把一个平台的问题变成"面板坏了"（其余六个平台也跟着看不见）。
    """
    if rehearsal:
        return CALIBRATION_NA, "本地靶页：没有「真机校准」这回事", ()
    try:
        pack = load_selector_pack(code)
    except StudioError as exc:
        return CALIBRATION_BROKEN, f"选择器包装不起来：{exc.message}", ()
    if pack.calibrated:
        return CALIBRATED, f"已真机校准（{pack.calibrated_at}）", pack.known_gaps
    return (
        UNCALIBRATED,
        "选择器**没在真机上验过**（CSS 是照着抖音那份的形状猜的）"
        f" —— 先跑 `studio publish calibrate --platform {code}`",
        pack.known_gaps,
    )


def platform_options(config: PublishConfig) -> tuple[PlatformOption, ...]:
    """能投到哪儿、投了会怎样 —— 面板的选项清单（顺序 = ``config/publish.yaml`` 的书写顺序）。

    **为什么这份清单从服务端来**：面板自己列一遍平台，等于把 ``config/publish.yaml``
    抄了第二份 —— 加一个平台要改两处，而漏改的那一处表现为"这个平台在面板上不存在"，
    没有人会去报这个 bug。这里连"点了会怎样"也一起算出来（``selectable`` / ``note``），
    因为"平台未启用"与"这个平台没有启用的账号"正是投递期会**跳过**它的那两条判据
    （见 :func:`enqueue_publications`）—— 面板显示"点得动"而投递期默默跳过，就是骗人。

    **演练台照旧列出来**（T5.9）：它是默认目标之外唯一能"真发一条"的通道，面板上
    藏起来的话，没有真账号的操作员就只能去命令行。
    """
    by_platform: dict[str, list[str]] = {}
    for account in config.enabled_accounts:
        by_platform.setdefault(account.platform, []).append(account.account_id)

    out: list[PlatformOption] = []
    for code, platform_cfg in config.platforms.items():
        accounts = tuple(by_platform.get(code, ()))
        rehearsal = platform_cfg.publisher == REHEARSAL_PUBLISHER
        calibration, calibration_note, known_gaps = _calibration(str(code), rehearsal=rehearsal)
        if not platform_cfg.enabled:
            note = f"平台未启用（config/publish.yaml → platforms.{code}.enabled: false）⇒ 投了会被跳过"
        elif not accounts:
            note = "这个平台没有启用的账号 ⇒ 投了会被跳过"
        elif rehearsal:
            note = "本地演练台：发到本机靶页，不是真平台（发完照样能采数、沉淀）"
        elif not config.enabled:
            # 出厂就是这一档（R14）。**必须说"死信"而不是"转人工"**：worker 的开关守卫
            # 抛的是不可重试的 PUBLISH_DISABLED ⇒ 作业直接进死信，`publications` 那一行
            # 根本不会建 ⇒ 发布面板上什么都不出现（§04-contracts ① 的开关守卫那条）。
            # 说成"转人工"会让人去「待人工」区块里找一个永远不会出现的记录。
            note = (
                "真平台：发布开关是关的（config/publish.yaml → enabled: false）"
                "⇒ 投了会直接死信（在「四池调度」的死信里能看到），发布面板上不会出现记录"
            )
        elif calibration in (UNCALIBRATED, CALIBRATION_BROKEN):
            # 这一档是 T5.14 新加的：**平台能投、开关开着、账号也有，但选择器没验过**。
            # 不提示的话，操作员看到的就是一行和抖音长得一模一样的平台 —— 而它投出去
            # 大概率发不出去（或者更糟：发出去一半，结果判不出来，见陷阱 #228）。
            note = f"真平台：要登录态，发出去不可撤销。⚠️ {calibration_note}"
        else:
            note = "真平台：要登录态，发出去不可撤销"
        out.append(
            PlatformOption(
                code=str(code),
                publisher=platform_cfg.publisher,
                enabled=platform_cfg.enabled,
                rehearsal=rehearsal,
                accounts=accounts,
                selectable=platform_cfg.enabled and bool(accounts),
                note=note,
                calibration=calibration,
                calibration_note=calibration_note,
                known_gaps=known_gaps,
            )
        )
    return tuple(out)


def default_targets(config: PublishConfig) -> tuple[tuple[str, str], ...]:
    """出厂口径的目标 = **每个启用账号**（顺序按 ``accounts`` 的书写顺序）。

    T5.8 起返回的是 ``(平台, 账号)`` 对而不是平台代号：一个平台上配了两个账号时，
    默认就该发两条（§06.2.4「同任务可安全分发到多账号」）—— 只发第一个会让矩阵运营
    每天安静地少发一半，而面板上那条 ``published`` 看起来完全正常。

    **演练台不在默认目标里**（T5.9）：``platforms.other`` 的发布器是
    :data:`~studio.publish.base.REHEARSAL_PUBLISHER`，它发的是本地靶页。把它算进默认
    目标，每一条任务投递完都会**顺手多出一条"演练发布"** —— 那条记录在面板上与真发布
    长得一样（只差平台代号），而"我没让它发，它自己发了一条"是最难解释的一类问题。
    要演练就显式点名（``platforms=("other",)``），见 ``docs/runbook`` 与 T5.9 的说明。
    """
    out: list[tuple[str, str]] = []
    for account in config.enabled_accounts:
        platform_cfg = config.platforms.get(account.platform)
        if platform_cfg is not None and platform_cfg.publisher == REHEARSAL_PUBLISHER:
            continue
        out.append((str(account.platform), account.account_id))
    return tuple(out)


def default_platforms(config: PublishConfig) -> tuple[str, ...]:
    """默认目标里的**平台**（去重、保持书写顺序）—— 面板勾选默认值用。

    与 :func:`default_targets` 同源：面板上"默认勾哪几个平台"与投递期"默认发到哪儿"
    必须是同一份答案，分成两份算迟早会出现"面板勾着的平台，投出去没发"。
    """
    out: list[str] = []
    for platform, _account_id in default_targets(config):
        if platform not in out:
            out.append(platform)
    return tuple(out)


# ── 面板数据（T5.3 先供"待人工"一处，七区块归 T5.5）────────────────────


@dataclass(frozen=True, slots=True)
class PublicationBoard:
    """发布面板的一份快照（计数 + 每个状态的最新若干条）。

    **六个状态各取一份**而不是"一个列表加筛选"：面板的区块是固定的六块，一次请求
    全部拿到，第一帧就不会是"先画一半、再补一半"。``counts`` 是**全量**计数
    （不带 limit）—— 区块标题上的数字必须是总数，跟着 limit 变小会让人以为记录没了。
    """

    counts: dict[str, int]
    by_status: dict[str, tuple[PublicationRow, ...]]

    def rows(self, status: str) -> tuple[PublicationRow, ...]:
        return self.by_status.get(status, ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "by_status": {status: [row.to_dict() for row in rows] for status, rows in self.by_status.items()},
        }


def build_board(connection: sqlite3.Connection, *, limit: int = 50) -> PublicationBoard:
    """读一份发布面板快照（六个状态各 ``limit`` 条）。"""
    repo = PublicationRepo(connection)
    per_status: dict[str, tuple[PublicationRow, ...]] = {}
    for status in repo.counts():
        per_status[status] = repo.list_by_status(status, limit=limit)
    return PublicationBoard(counts=repo.counts(), by_status=per_status)


# ── 人工处置（§06.10 不变量 3：三者均写 audit_ops）─────────────────────


def retry_publication(
    *,
    connection: sqlite3.Connection,
    publication_id: str,
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    source: str = "webui",
) -> PublicationRow:
    """人工重试一条发布：``publications`` 回 ``queued`` + 把作业重排进待办。

    两件事**都要做**，缺一个就是"点了没反应"：
    - 只改 ``publications`` ⇒ 队列那条作业已经 ``succeeded``，永远没有 worker 再看它
      （与 T2.9 的单句重配同一条陷阱）；
    - 只重排作业 ⇒ worker 一看 ``publications`` 是 ``manual_required``，会把这一轮
      又记成一次失败。

    ``attempt_count`` 归零（``reset_for_retry``）：人工重试的意思是"给它一次完整的
    机会"，接着上一轮的第 3 次算的话，下一次失败就直接又转人工了。
    """
    repo = PublicationRepo(connection)
    before = repo.get(publication_id)
    if before is None:
        raise StudioError(
            f"发布记录不存在：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
            remediation="刷新发布面板，确认这条记录还在（任务被删会级联删掉它）",
        )
    row = repo.reset_for_retry(publication_id)
    requeued = _requeue_publication_job(connection, row)
    _audit(
        connection,
        action=AUDIT_RETRY,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason=reason or "人工重试",
        before={"status": before.status, "attempt_count": before.attempt_count},
        after={"status": row.status, "job_requeued": requeued},
        request_id=request_id,
        ip=ip,
        source=source,
    )
    return row


def cancel_publication(
    *,
    connection: sqlite3.Connection,
    publication_id: str,
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    source: str = "webui",
) -> PublicationRow:
    """人工取消一条发布：``canceled`` + 把还没认领的作业作废。

    ``publications`` 已 ``published`` 的**不能**取消（``PublicationRepo.cancel`` 会抛）：
    平台上已经有了那条作品，本系统这一侧删掉记录只会让"发过什么"变成一笔糊涂账。
    """
    repo = PublicationRepo(connection)
    before = repo.get(publication_id)
    row = repo.cancel(publication_id, reason=reason or "人工取消")
    job_canceled = _cancel_publication_job(connection, row, actor=actor, actor_ref=actor_ref)
    _audit(
        connection,
        action=AUDIT_CANCEL,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason=reason or "人工取消",
        before=None if before is None else {"status": before.status},
        after={"status": row.status, "job_canceled": job_canceled},
        request_id=request_id,
        ip=ip,
        source=source,
    )
    return row


def mark_manual_done(
    *,
    connection: sqlite3.Connection,
    publication_id: str,
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str,
    request_id: str | None = None,
    ip: str | None = None,
    source: str = "webui",
) -> PublicationRow:
    """人工标记"这条我处理完了"（§06.10 不变量 2 的第三个动作）。

    ``reason`` **必填**：这条动作的全部信息量就是那句人话。允许空串的话，面板上会
    留下一排"已处理"而没有任何一条说得出为什么 —— 那比不记还糟。

    落点为什么是 ``canceled`` 而不是 ``published``
    ---------------------------------------------
    人把片子发到平台上之后，那条作品是**人发的**：本系统手上没有 ``url``、
    没有 ``platform_post_id``，写成 ``published`` 等于伪造一条发布记录
    （T5.4 会拿着空 post id 去抓数据；限频守卫会把它算成"今天发过一条"）。
    这一侧的语义只是"从自动链路上摘下来，别再管它了"，那就是 ``canceled``。
    人发出去的那一条要不要占额度，记在 ``audit_ops`` 里由人自己看 —— 见 §06.10 的
    人工兜底说明。
    """
    if not reason.strip():
        raise StudioError(
            "标记已人工处理必须写一句说明",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
            remediation="在 reason 里写清楚是人工发出去了，还是决定不发（会进 audit_ops）",
        )
    repo = PublicationRepo(connection)
    before = repo.get(publication_id)
    if before is None:
        raise StudioError(
            f"发布记录不存在：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
        )
    if before.status != MANUAL_REQUIRED:
        raise StudioError(
            f"这条发布不在「待人工」队列里（当前：{before.status}）",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"publication_id": publication_id, "status": before.status},
            remediation="「标记已人工处理」只对待人工的那几条有意义",
        )
    row = repo.cancel(publication_id, reason=f"人工已处理：{reason}")
    _cancel_publication_job(connection, row, actor=actor, actor_ref=actor_ref)
    _audit(
        connection,
        action=AUDIT_MANUAL_DONE,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason=reason,
        before={"status": before.status},
        after={"status": row.status},
        request_id=request_id,
        ip=ip,
        source=source,
    )
    return row


def _requeue_publication_job(connection: sqlite3.Connection, row: PublicationRow) -> bool:
    """把这条发布的作业重排进待办；没有这条作业 ⇒ 现投一条。

    单元标识由 ``row`` 反推（``platform:account_id``）—— 这条发布记录**自己知道**
    它属于哪个账号，所以重投时不需要任何人再告诉它一次。
    """
    store = JobStore(connection)
    job = _find_publication_job(connection, row)
    if job is None:
        store.enqueue(
            task_id=row.task_id,
            pool=PUBLISH_POOL,
            unit_type=UnitType.PUBLISH.value,
            unit_ref=unit_ref(row.platform, row.account_id),
            payload={"account_id": row.account_id},
        )
        return True
    store.requeue_unit(
        task_id=row.task_id,
        pool=PUBLISH_POOL,
        unit_type=UnitType.PUBLISH.value,
        unit_ref=unit_ref(row.platform, row.account_id),
        payload={"account_id": row.account_id},
    )
    return True


def _cancel_publication_job(
    connection: sqlite3.Connection,
    row: PublicationRow,
    *,
    actor: str,
    actor_ref: str | None,
) -> bool:
    """作废还没被认领的作业；已经在跑的返回 ``False``（由它自己收工）。"""
    job = _find_publication_job(connection, row)
    if job is None:
        return False
    return JobStore(connection).cancel(
        job_id=job.id,
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=actor_ref,
        reason=f"发布记录已{row.status}",
    )


def _find_publication_job(connection: sqlite3.Connection, row: PublicationRow) -> Job | None:
    """找这条发布对应的作业（``(task_id, publish, publish, platform:account_id)``）。

    先按**带账号**的单元标识找；找不到再退回只认平台的老格式（T5.8 之前投的作业行
    长那样）。不退回的话，"加第二个账号之前投的那条作业"会永远找不到 —— 表现出来是
    重投时**又投一条新的**，而那一条与老的会一起去跑同一条发布。
    """
    want = unit_ref(row.platform, row.account_id)
    jobs = JobStore(connection).list_jobs(pool=PUBLISH_POOL, task_id=row.task_id, limit=100)
    for job in jobs:
        if job.unit_type != UnitType.PUBLISH.value:
            continue
        if job.unit_ref == want:
            return job
    for job in jobs:
        if job.unit_type != UnitType.PUBLISH.value:
            continue
        platform, account_id = parse_unit_ref(job.unit_ref)
        if platform == row.platform and account_id is None:
            return job
    return None


def _audit(
    connection: sqlite3.Connection,
    *,
    action: str,
    row: PublicationRow,
    actor: str,
    actor_ref: str | None,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    request_id: str | None,
    ip: str | None,
    source: str,
) -> None:
    """写一条人工处置留痕（§06.10 不变量 3）。

    ``actor`` 白名单外的值落 ``user``：留痕的 CHECK 会拒掉非法 actor，而"传错了"不该
    把一次已经生效的处置变成 500 —— 处置本身已经落库了，留痕只是补记。
    """
    AuditRepo(connection).record(
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=actor_ref,
        action=action,
        target_type="publication",
        target_id=row.id,
        task_id=row.task_id,
        before=before,
        after=after,
        result="ok",
        reason=reason,
        request_id=request_id,
        ip=ip,
        source=source,
    )


def _default_title(script: Any, task: Any, task_id: str) -> str:
    """演练用的标题：稿件标题 ⇒ 任务标题 ⇒ 任务号。"""
    return (getattr(script, "title", "") or "") or (getattr(task, "title", "") or "") or task_id


def _default_caption(script: Any) -> str:
    """演练用的文案：稿件的 CTA（"点个关注"那句）。

    为什么是 CTA 而不是整篇稿子：发布文案是**给人扫一眼**的，把 700 字口播稿
    整段贴上去是内容事故。真正的发布文案生成（按平台裁剪、配话题）归发布面板，
    而这一步要的是"有一段像样的、能验回读的文本"。
    """
    return (getattr(script, "cta", "") or "") or ""


def _cover_path(task: Any) -> Path | None:
    """任务上下文里的封面路径（T5.1 的 ``set_cover_path`` 写进去的那一条）。"""
    value = (getattr(task, "context", None) or {}).get("cover_path")
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _text_warnings(
    title: str,
    caption_plan: Any,
    *,
    platform_cfg: Any,
    platform: str,
    cover: Path | None,
) -> list[str]:
    """把"装不下"的三件事各报一条（§06.2.2 第 2 条：**超长 ⇒ warn，不静默截断**）。"""
    warnings: list[str] = []
    fit = fit_text(title, limit=platform_cfg.title_max)
    if not fit.ok:
        warnings.append(
            f"标题超出 {platform} 上限 {fit.over_by} 字（上限 {fit.limit}）："
            "平台会自己截，这里只提示（§06.2.2）"
        )
    if caption_plan.dropped_tags:
        warnings.append(f"话题超过上限被丢弃：{'、'.join(caption_plan.dropped_tags)}")
    if caption_plan.truncated_by:
        warnings.append(f"文案装不下，截掉了 {caption_plan.truncated_by} 字")
    if platform_cfg.cover_required and cover is None:
        warnings.append(f"{platform} 要求自定义封面，但这条任务还没出封面（先跑 publish cover）")
    return warnings


def _duration_ms(timeline: Any, manifest: Any) -> int:
    """成片时长（毫秒）：时间轴优先，manifest 兜底，都没有 ⇒ 0。

    时间轴优先是因为它是**逐句实测**的结果（T2.7），而 manifest 记的是当初合成时
    算出来的时长 —— 重合成之后 manifest 会被新的一版覆盖，时间轴不会。
    """
    for source, key in ((timeline, "total_ms"), (manifest, "duration_ms")):
        if isinstance(source, dict):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 0


def _manifest_watermark(paths: StudioPaths, task_id: str) -> bool | None:
    """``manifest.json`` 里的水印判据（读不到 ⇒ ``None``）。

    ``None`` 与 ``False`` 是两件事：前者是"没记录"，后者是"记录了没贴"。
    ``precheck._watermark_gate`` 会把两者分开处理，所以这里不能顺手把 ``None`` 折成假。
    """
    payload = read_json(paths.manifest_json(task_id))
    if not isinstance(payload, dict):
        return None
    watermark = payload.get("watermark")
    if not isinstance(watermark, dict):
        return None
    enabled = watermark.get("enabled")
    return enabled if isinstance(enabled, bool) else None
