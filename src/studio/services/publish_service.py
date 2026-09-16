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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from studio.agents.base import AgentContext
from studio.core.config import AccountConfig, PlatformCode, PublishConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.repositories.artifact_repo import ArtifactRepo
from studio.domain.cover import CoverInput, CoverOutput, rule_cover_output
from studio.domain.publish import build_caption, fit_text, render_tags
from studio.domain.script import find_forbidden
from studio.domain.task_service import TaskService
from studio.publish.base import (
    PublisherContext,
    PublishHealth,
    PublishRequest,
    PublishResult,
    get_publisher,
)
from studio.publish.cover import build_cover, resolve_frame_at_ms, write_cover_atomically
from studio.publish.precheck import (
    PrecheckReport,
    PublishContent,
    load_banned_words,
    run_precheck,
)
from studio.services.script_service import read_active_script

__all__ = [
    "COVER_KIND",
    "FIXTURE_ACCOUNT_ID",
    "TARGETS",
    "TTL_COVER",
    "CoverReport",
    "CoverRequest",
    "DryRunReport",
    "DryRunRequest",
    "PublishService",
    "resolve_final_video",
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
            "health": {
                "ready": self.health.ready,
                "logged_in": self.health.logged_in,
                "account_name": self.health.account_name,
                "hint": self.health.hint,
                "last_check_at": self.health.last_check_at,
            },
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
    payload = _read_json(paths.manifest_json(task_id))
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


def _read_json(path: Path) -> Any:
    """读一个 JSON 文件；不存在 / 读不了 / 不是 JSON ⇒ ``None``（**不抛**）。

    ``timeline.json`` 与 ``manifest.json`` 都是"有更好、没有也能继续"的输入：
    时间轴缺失只影响抽帧点（退回默认值），manifest 缺失只影响成片路径的取法。
    为它们各抛一次异常，只会让"这条任务还没渲染"变成一个需要 try/except 的常态。
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("publish.json_unreadable", path=path.as_posix())
        return None


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

    ``publish`` / ``persona`` 由**入口**注入（与 `ScriptService` 同一手法）：
    服务只认数据，不去读配置文件 —— 那会让每个测试都得先铺一份 config。
    """

    def __init__(
        self,
        connection: Any,
        *,
        paths: StudioPaths,
        publish: PublishConfig | None = None,
        persona: Any = None,
        cover_agent: Any = None,
        build: Callable[..., Any] | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._publish = publish
        self._persona = persona
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

        timeline = _read_json(self._paths.timeline_json(request.task_id))
        manifest = _read_json(self._paths.manifest_json(request.task_id))
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
        )
        warnings = [*result.warnings]
        if agent_error:
            warnings.append(f"封面文案走了规则兜底：{agent_error}")

        cover_path = write_cover_atomically(result, target) if result.ok else None
        if cover_path is None:
            warnings.append("封面没出来，按 §06.3 走无封面发布（平台会用首帧）")

        plan = dict(result.plan)
        plan["source"] = source
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
    payload = _read_json(paths.manifest_json(task_id))
    if not isinstance(payload, dict):
        return None
    watermark = payload.get("watermark")
    if not isinstance(watermark, dict):
        return None
    enabled = watermark.get("enabled")
    return enabled if isinstance(enabled, bool) else None
