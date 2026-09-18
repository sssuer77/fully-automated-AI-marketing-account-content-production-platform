"""发布池的单元处理器（``publish/publish`` · T5.3 · §06.5.3 / §06.5.4 / §06.10）。

``publish/publish`` 单元 = 「把这一期发到某个平台的那个账号上」
--------------------------------------------------------------
```text
claim(publish/publish, unit_ref = <平台代号>)
   ├─ 解析账号（payload.account_id ⇒ 该平台上第一个启用的账号）
   ├─ 限频守卫：≤3 条/天/账号 + 间隔 ≥30min
   │     └─ 不过 ⇒ UnitDeferred（回 pending + not_before，**不计 attempts**）
   ├─ 幂等登记 publications（sha256(task_id|platform|account_id) 上有 UNIQUE）
   │     └─ 已经是 published / canceled ⇒ 直接收工（重投一次不会发第二条）
   ├─ 登录态探测：不 ready ⇒ manual_required（**不自动登录**，R13）
   ├─ 发布器八步（§06.5.3）⇒ 按结果落 published / failed / manual_required
   └─ 失败**不回退任务状态**（§06.5.4）：成片仍在，可下载后人工发
```

为什么单元处理器放在 ``pools/`` 而不是 ``services/``
--------------------------------------------------
与 ``voice_worker`` / ``render_worker`` 同一条理由：它实现的是
:class:`~studio.pools.worker_base.UnitHandler` 协议 —— 那是**池**的契约。
它只做三件事：把认领到的单元翻成服务调用、把失败翻成 ``StudioError``（好让队列的
重试 / 退避 / 死信 / 告警接手）、把结果压成可留痕的摘要。

三条纪律
--------
1. **不自己收尾**。``succeed`` / ``fail`` / 退避 / 死信一律由 :class:`PoolWorker` 负责。
   本处理器只有两处"自己判成功"：**限频顺延**（``UnitDeferred``）与**转人工**
   （``manual_required``）—— 它们不是收尾，而是"这条链路走完了、接下来等人"这个业务结论。
2. **可重入**。同一条单元被重投时先查幂等键：已 ``published`` / ``canceled`` 的直接收工，
   否则重发一遍 —— 失败重试要的正是后者。
3. **不在单元里开池**。配置 / 发布器 / 日志由进程入口装配一次（:func:`build_publish_handler`）。

为什么"限频触顶"要顺延而不是失败
--------------------------------
见 :meth:`~studio.db.queue.JobStore.defer`：把"今天额度用完了"记成一次失败，三天后一条
**从没真正试过**的作业会自己进死信并告警 —— 半夜被叫起来看一条没跑过的发布。
判据本身（``≤3 条/天`` 与 ``≥30min``）在 ``JobStore.rate_limit_state``，这里只把它的
结论翻成"到几点再来"（``publish/ratelimit.py``）。

为什么"转人工"要让 job **成功**
-------------------------------
``manual_required`` 的语义是"自动这条路走完了，接下来要人做决定"（§06.10）。
让它以失败收场，会把同一条记录既送进待人工队列、又送进死信告警，而两者的排障动作
完全不同。所以判到转人工 ⇒ 落 ``publications`` + 写 ``audit_ops`` + **正常返回**。

为什么按错误码分"重试 / 转人工"，而不是照抄 ``PublishResult.status``
------------------------------------------------------------------
``PublishResult.failure`` 的缺省状态是 ``manual_required``（T5.2 裁定：宁可转人工，
别把发不出去的片子重试到天亮），而**发布器不知道还剩几次机会** —— 那是队列的事。
所以判据放在这里：错误码在 :data:`RETRYABLE_CODES` 里、或发布器**明确**标了 ``FAILED``
⇒ 交回队列退避重试；到 ``max_attempts`` 仍失败 ⇒ 转人工（§06.10「重试 ≥3 仍失败」一行）。

为什么 ``publish.enabled=false`` 时**演练仍然放行**
--------------------------------------------------
``dry_run`` 的全部意义就是"在开关还关着的时候验证链路是通的"（T5.2 的
``PublishService.dry_run`` 已经把这条写死了）。要求先打开 ``enabled`` 才能演练，
等于让人拿**真发布**当验证手段 —— 那正是 R14 要防的事。真发布（``dry_run=False``）
在 ``enabled=false`` 时一律 ``PUBLISH_DISABLED`` 死信，见 :meth:`_guard_switch`。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, NoReturn

from studio.core.clock import format_iso, now_iso, utc_now
from studio.core.config import (
    AccountConfig,
    PlatformConfig,
    PoolConfig,
    PublishConfig,
    load_config,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.files import file_sha256
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import connect
from studio.db.queue import JobStore
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import (
    PUBLISHED,
    PublicationRepo,
    PublicationRow,
)
from studio.domain.enums import UnitType
from studio.domain.publish import build_caption, fit_text, render_tags
from studio.domain.task_service import TaskService
from studio.pools.worker_base import UnitContext, UnitDeferred
from studio.publish.base import (
    REHEARSAL_PUBLISHER,
    PublisherContext,
    PublishEvidence,
    PublishRequest,
    PublishResult,
    PublishStatus,
    get_publisher,
)
from studio.publish.ratelimit import DAILY_LIMIT_REASON, RateDecision, decide
from studio.services.log_service import LogService
from studio.services.publish_service import resolve_account, resolve_final_video, resolve_platform
from studio.services.script_service import read_active_script

__all__ = [
    "LOGIN_HINT",
    "PUBLISH_UNIT_TYPES",
    "RETRYABLE_CODES",
    "PublishOutcome",
    "PublishPlatformHandler",
    "build_publish_handler",
]

logger = get_logger("studio.pools.publish")

#: 本处理器认领的单元类型（``jobs.unit_type``）
PUBLISH_UNIT_TYPES: Final[frozenset[str]] = frozenset({UnitType.PUBLISH.value})

#: **重试有可能变好**的错误码（网络抖了 / 上传中断 / 超时）⇒ 走队列退避重试，
#: 到 ``max_attempts`` 才转人工。不在表里的（登录态失效 / 选择器失效 / 审核不通过）
#: 重试多少次都是同一个答案 ⇒ 立刻转人工（§06.10）。
RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {
        ErrorCode.PUBLISH_UPLOAD_FAILED.value,
        ErrorCode.PUBLISH_TIMEOUT.value,
        ErrorCode.PUBLISH_UNKNOWN.value,
        ErrorCode.PUBLISH_FAILED.value,
    }
)

#: 登录态不 ready 时给操作员的那句话（§06.10 第一行：**不自动登录**）。
LOGIN_HINT: Final[str] = "需人工扫码登录（本系统不自动登录、不绕过验证码）"

#: 进度说明写进 ``result_json`` 时截断到多少字符
_NOTE_CHARS: Final[int] = 120

#: 日志来源（``system_logs.source``）
_LOG_SOURCE: Final[str] = "studio.pools.publish"

#: 一条发布记录进人工队列时写的留痕动作（§06.10 不变量 3）
AUDIT_MANUAL: Final[str] = "publish.manual_required"


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """一次 ``publish/publish`` 单元的结论（进 ``jobs.result_json``，面板直接读它）。

    ``used_today`` / ``daily_limit`` 一并带上：面板要回答"这个号今天还能发几条"，
    而那个答案只有顺延那一刻才拿得到（顺延之后作业不在 ``jobs`` 里了）。
    """

    task_id: str
    job_id: str
    platform: str
    account_id: str
    publication_id: str
    stage: str
    status: str
    note: str
    started_at: str
    dry_run: bool
    url: str | None
    platform_post_id: str | None
    attempts: int
    max_attempts: int
    used_today: int
    daily_limit: int
    error_code: str | None
    error_message: str | None
    evidence: Mapping[str, Any]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "publication_id": self.publication_id,
            "stage": self.stage,
            "status": self.status,
            "note": self.note,
            "started_at": self.started_at,
            "dry_run": self.dry_run,
            "url": self.url,
            "platform_post_id": self.platform_post_id,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "used_today": self.used_today,
            "daily_limit": self.daily_limit,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "evidence": dict(self.evidence),
            "warnings": list(self.warnings),
        }


class PublishPlatformHandler:
    """``publish/publish`` 单元的处理器（**进程内一个实例**，跨单元复用）。"""

    unit_types: frozenset[str] = PUBLISH_UNIT_TYPES

    def __init__(
        self,
        *,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        publish: PublishConfig,
        pool: PoolConfig | None = None,
        log: LogService | None = None,
        runner: Callable[[Any], Any] = asyncio.run,
        headless: bool = True,
    ) -> None:
        """装配一个发布池处理器。

        :param runner: 跑协程的实现（缺省 ``asyncio.run``；测试注入假件，不碰事件循环）
            —— 与 ``DraftTaskHandler`` 同一手法：``Publisher`` 是 ``async`` 的
            （§4.6.1 的签名就是 ``async``），而队列那侧全是同步调用。
        :param headless: 默认无头。首次扫码登录要可见浏览器，那是**人工一次性**动作
            （``studio publish dry-run --headed``），常驻 worker 不开窗口。
        """
        self._paths = paths
        self._connection = connection
        self._publish = publish
        self._pool = pool
        self._log = log
        self._runner = runner
        self._headless = headless
        self._dry_run_default = bool(publish.dry_run)
        self._store = JobStore(connection)
        self._repo = PublicationRepo(connection)
        self._audit = AuditRepo(connection)
        self._tasks = TaskService(connection)

    # ── 单元 ────────────────────────────────────────────────────────────

    def run(self, ctx: UnitContext) -> Mapping[str, Any]:
        """发一条：限频 ⇒ 幂等登记 ⇒ 登录态 ⇒ 八步 ⇒ 落库。"""
        if ctx.unit_type not in self.unit_types:
            raise StudioError(
                f"{type(self).__name__} 不认领单元类型 {ctx.unit_type}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"unit_type": ctx.unit_type, "accepted": sorted(self.unit_types)},
            )
        started_at = now_iso()
        platform = (ctx.unit_ref or "").strip()
        platform_cfg = self._platform_config(platform)
        account = self._account(platform, ctx.payload)

        # 幂等短路排在限频**之前**：已经发过的记录再去问「今天额度够不够」没有意义，
        # 而额度用满时它会先被顺延 —— 一次重投变成一次白等。判定只读，不改库。
        settled = self._settled(ctx, platform=platform, account=account, started_at=started_at)
        if settled is not None:
            return settled

        decision = self._rate_decision(account)
        if not decision.allowed:
            self._log_line(ctx, "info", f"publish 顺延：{decision.hint or '触发限频'}")
            self._halt_on_ratelimit(decision, account)

        dry_run = self._dry_run_default or _truthy(ctx.payload.get("dry_run"))
        self._guard_switch(dry_run=dry_run, platform_cfg=platform_cfg, ctx=ctx)

        task = self._tasks.get(ctx.task_id)
        video = resolve_final_video(ctx.task_id, self._paths)
        if video is None:
            raise StudioError(
                f"任务 {ctx.task_id} 在盘上找不到成片，无法发布",
                code=ErrorCode.RENDER_FAILED,
                context={"task_id": ctx.task_id, "videos_dir": self._paths.videos_dir.as_posix()},
                remediation="先跑 `studio render make --task-id …` 或 `studio pipeline run --task …`",
            )
        title, caption, tags, cover, warnings = self._content(
            task, platform_cfg=platform_cfg, payload=ctx.payload
        )

        row, created = self._repo.create(
            task_id=ctx.task_id,
            platform=platform,
            account_id=account.account_id,
            video_path=video.as_posix(),
            video_sha256=file_sha256(video),
            title=title,
            caption=caption,
            tags=tags,
            cover_path=None if cover is None else cover.as_posix(),
            profile_key=platform_cfg.profile,
            dry_run=dry_run,
            scheduled_at=_opt_str(ctx.payload, "scheduled_at"),
            max_attempts=ctx.job.max_attempts,
        )
        self._log_line(
            ctx,
            "info",
            f"publish 开始：{platform}/{account.account_id}"
            + ("（幂等命中已有记录）" if not created else ""),
        )
        self._report(
            ctx,
            {
                "task_id": ctx.task_id,
                "platform": platform,
                "account_id": account.account_id,
                "publication_id": row.id,
                "stage": "start",
                "note": f"开始发布到 {platform}（{row.status}）",
                "started_at": started_at,
            },
        )

        # 双保险：上面那次只读短路之后、这里之间没有别的写入口，走到这里说明
        # ``create`` 刚返回了一条终态行（例如同进程内并发投递），结论与上面一致。
        if row.is_terminal:
            return self._outcome(
                ctx,
                row,
                account=account,
                stage="settled",
                started_at=started_at,
                decision=decision,
                warnings=warnings,
                note=(
                    "这条内容已经发过，不重复发布" if row.status == PUBLISHED else "这条内容已被取消，不发布"
                ),
            ).to_dict()

        # ``row.dry_run`` 而不是刚算出来的 ``dry_run``：幂等命中时**库里那一行**才是
        # 事实（上一次是怎么登记的，这一次就怎么走）。两者不一致会让"演练"变成"真发"。
        return self._publish_row(
            ctx,
            row,
            platform_cfg=platform_cfg,
            account=account,
            video=video,
            title=title,
            caption=caption,
            tags=tags,
            cover=cover,
            decision=decision,
            started_at=started_at,
            warnings=warnings,
        )

    # ── 发布 ────────────────────────────────────────────────────────────

    def _publish_row(
        self,
        ctx: UnitContext,
        row: PublicationRow,
        *,
        platform_cfg: PlatformConfig,
        account: AccountConfig,
        video: Path,
        title: str,
        caption: str,
        tags: tuple[str, ...],
        cover: Path | None,
        decision: RateDecision,
        started_at: str,
        warnings: list[str],
    ) -> Mapping[str, Any]:
        """第 ②～⑧ 步：上传 → 回读 → 发布 → 落库。"""
        self._repo.mark_uploading(row.id)
        publisher = get_publisher(platform_cfg.publisher)(
            PublisherContext(
                paths=self._paths,
                account=account,
                platform=platform_cfg,
                headless=self._headless,
            )
        )
        health = self._runner(publisher.health())
        if not health.ready:
            fresh = self._repo.mark_manual_required(
                row.id,
                error_code=ErrorCode.PUBLISH_LOGIN_EXPIRED.value,
                error_message=health.hint or "登录态不可用",
                evidence={"stage": "health", "logged_in": health.logged_in},
            )
            self._record_manual(
                fresh,
                ctx,
                reason=health.hint or "登录态不可用",
                hint=LOGIN_HINT,
            )
            return self._outcome(
                ctx,
                fresh,
                account=account,
                stage="manual_required",
                started_at=started_at,
                decision=decision,
                warnings=[*warnings, LOGIN_HINT],
                note=f"登录态不过，转人工：{health.hint or '未登录'}",
            ).to_dict()

        ctx.check_alive()
        result = self._runner(
            publisher.publish(
                PublishRequest(
                    task_id=ctx.task_id,
                    platform=row.platform,
                    account_id=account.account_id,
                    video_path=video,
                    title=title,
                    caption=caption,
                    tags=tags,
                    cover_path=cover,
                    scheduled_at=row.scheduled_at,
                    dry_run=row.dry_run,
                )
            )
        )
        ctx.check_alive()
        return self._settle(
            ctx,
            row,
            account=account,
            result=result,
            started_at=started_at,
            decision=decision,
            warnings=warnings,
        ).to_dict()

    def _settle(
        self,
        ctx: UnitContext,
        row: PublicationRow,
        *,
        account: AccountConfig,
        result: PublishResult,
        started_at: str,
        decision: RateDecision,
        warnings: list[str],
    ) -> PublishOutcome:
        """把 ``PublishResult`` 翻成库里那一行 + 单元的结论。"""
        evidence = _evidence(result.evidence)
        if result.ok and result.status is PublishStatus.PUBLISHED:
            fresh = self._repo.mark_published(
                row.id,
                url=result.url,
                platform_post_id=result.platform_post_id,
                evidence=evidence,
                next_metric_at=self._next_metric_at(),
            )
            self._log_line(
                ctx,
                "info",
                f"publish 成功：{result.url or result.platform_post_id or '已发布'}",
            )
            return self._outcome(
                ctx,
                fresh,
                account=account,
                stage="published",
                started_at=started_at,
                decision=decision,
                warnings=warnings,
                note="已发布",
            )

        if result.ok:
            # ``ok=True`` 而状态不是 ``published``：dry-run 停在第 ⑥ 步之前（§06.5.3）。
            fresh = self._repo.mark_dry_run(row.id, evidence=evidence)
            return self._outcome(
                ctx,
                fresh,
                account=account,
                stage="dry_run",
                started_at=started_at,
                decision=decision,
                warnings=warnings,
                note="演练完成（发布按钮**没有被点**），记录回 queued",
            )

        return self._settle_failure(
            ctx,
            row,
            account=account,
            result=result,
            evidence=evidence,
            started_at=started_at,
            decision=decision,
            warnings=warnings,
        )

    def _settle_failure(
        self,
        ctx: UnitContext,
        row: PublicationRow,
        *,
        account: AccountConfig,
        result: PublishResult,
        evidence: Mapping[str, Any],
        started_at: str,
        decision: RateDecision,
        warnings: list[str],
    ) -> PublishOutcome:
        """失败：要么交回队列重试，要么转人工（§06.10）。"""
        code = result.error_code or ErrorCode.PUBLISH_FAILED.value
        message = result.error_message or "发布失败（平台没给出原因）"
        retryable = result.status is PublishStatus.FAILED or code in RETRYABLE_CODES
        exhausted = ctx.attempt >= ctx.job.max_attempts

        if retryable and not exhausted:
            self._repo.mark_failed(row.id, error_code=code, error_message=message, evidence=evidence)
            raise StudioError(
                message,
                code=_as_code(code),
                context={
                    "task_id": ctx.task_id,
                    "platform": row.platform,
                    "account_id": account.account_id,
                    "attempt": ctx.attempt,
                    "max_attempts": ctx.job.max_attempts,
                    "publication_id": row.id,
                },
                remediation="队列会按指数退避重排；到 max_attempts 仍失败 ⇒ 转人工",
            )

        fresh = self._repo.mark_manual_required(
            row.id, error_code=code, error_message=message, evidence=evidence
        )
        note = f"重试 {ctx.attempt} 次仍失败，转人工：{message}" if retryable else f"转人工：{message}"
        self._record_manual(fresh, ctx, reason=message)
        self._log_line(ctx, "warn", f"publish 转人工：{message}")
        return self._outcome(
            ctx,
            fresh,
            account=account,
            stage="manual_required",
            started_at=started_at,
            decision=decision,
            warnings=warnings,
            note=note,
        )

    # ── 守卫 ────────────────────────────────────────────────────────────

    def _platform_config(self, platform: str) -> PlatformConfig:
        """平台配置；未知平台 / 未启用平台都抛（**不猜**）。

        查表本身复用服务层的 :func:`~studio.services.publish_service.resolve_platform`：
        投递侧（``enqueue_publications``）与执行侧对"有没有这个平台"必须是**同一句**回答，
        抄成两份之后，一边报"未知平台"、一边报"未启用"，同一条配置错会被当成两个问题查。
        """
        cfg = resolve_platform(self._publish, platform)
        if not cfg.enabled:
            raise StudioError(
                f"平台 {platform} 一期未启用（§06.2.1 · Q9）",
                code=ErrorCode.PUBLISH_NOT_IMPLEMENTED,
                context={"platform": platform},
                remediation=(
                    "一期只实现 douyin / kuaishou / shipinhao；二线平台先把 platforms 里那段 enabled 打开"
                ),
            )
        return cfg

    def _account(self, platform: str, payload: Mapping[str, Any]) -> AccountConfig:
        """payload 指定 ⇒ 那个账号；否则该平台**唯一**启用的那个（出厂单账号）。

        与投递侧共用 :func:`~studio.services.publish_service.resolve_account`：
        两边对"该平台配了几个账号"必须给出同一个结论 —— 一边挑第一个、一边报错的话，
        投递时看着没事，执行时作业直接失败。
        """
        return resolve_account(self._publish, platform=platform, account_id=_opt_str(payload, "account_id"))

    def _guard_switch(self, *, dry_run: bool, platform_cfg: PlatformConfig, ctx: UnitContext) -> None:
        """``publish.enabled=false`` ⇒ 真发布一律拒绝（R14）；演练与**演练台**放行。

        判据是"这一条会不会发到真实平台上去"，而不是"它是不是 dry-run"：
        ``platforms.other`` 的发布器是 :class:`~studio.publish.platforms.fixture.FixturePublisher`，
        它只认本地靶页（见那个模块的三道保护）⇒ 它**发不出去任何东西**，因此不该被
        "真发布开关"挡住。

        反过来，如果拿 ``enabled`` 去挡它，就会出现一件很别扭的事：想验证"链路是不是
        通的"，得先把**真发布**开关打开 —— 那正是 R14 要防的事。``dry_run`` 当初被
        放行就是这个理由（见模块 docstring），演练台只是把同一件事往后延了一段：
        连"点下发布之后"的那半条链路也一起验。
        """
        if self._publish.enabled or dry_run or platform_cfg.publisher == REHEARSAL_PUBLISHER:
            return
        raise StudioError(
            "发布开关是关的（config/publish.yaml → enabled: false）",
            code=ErrorCode.PUBLISH_DISABLED,
            context={"task_id": ctx.task_id, "platform": ctx.unit_ref},
            remediation="确认要真发之后再打开 enabled；只验证链路请用 dry-run",
        )

    def _settled(
        self,
        ctx: UnitContext,
        *,
        platform: str,
        account: AccountConfig,
        started_at: str,
    ) -> dict[str, Any] | None:
        """只读的幂等短路：已有终态记录 ⇒ 直接给结论（不碰限频、不碰发布器）。

        ``published`` / ``canceled`` 是「这件事已经有结论了」，重投一次不该再发一条，
        也不该被顺延 30 分钟 —— 顺延的语义是「还没轮到」，而这条早就轮完了。
        返回 ``None`` 表示没有终态记录，继续往下走（新登记或失败重试）。
        """
        row = self._repo.find(task_id=ctx.task_id, platform=platform, account_id=account.account_id)
        if row is None or not row.is_terminal:
            return None
        self._log_line(ctx, "info", f"publish 幂等命中终态：{platform}/{row.status}")
        return self._outcome(
            ctx,
            row,
            account=account,
            stage="settled",
            started_at=started_at,
            decision=RateDecision(allowed=True, used_today=0, daily_limit=self._daily_limit(account)),
            warnings=(),
            note=("这条内容已经发过，不重复发布" if row.status == PUBLISHED else "这条内容已被取消，不发布"),
        ).to_dict()

    def _rate_decision(self, account: AccountConfig) -> RateDecision:
        """限频判定：计数在 ``JobStore``，这里只把结论翻成顺延指令。"""
        state = self._store.rate_limit_state(
            account_id=account.account_id,
            daily_limit=self._daily_limit(account),
            min_gap_min=self._min_gap(account),
        )
        return decide(
            allowed=state.allowed,
            used_today=state.used_today,
            daily_limit=state.daily_limit,
            next_allowed_at=state.next_allowed_at,
            reason=state.reason,
            account_id=account.account_id,
            now=utc_now(),
        )

    def _daily_limit(self, account: AccountConfig) -> int:
        """池级为准、账号级兜底（§03.4.4 ⑥ 的守卫读 ``pool_settings.rate_limit_json``，
        而那张表由 ``pools.yaml`` 的 ``daily_limit_per_account`` 落种）。"""
        pool_value = None if self._pool is None else self._pool.daily_limit_per_account
        return account.daily_limit if pool_value is None else int(pool_value)

    def _min_gap(self, account: AccountConfig) -> int:
        pool_value = None if self._pool is None else self._pool.min_gap_min
        return account.min_gap_min if pool_value is None else int(pool_value)

    def _halt_on_ratelimit(self, decision: RateDecision, account: AccountConfig) -> NoReturn:
        """限频触顶 ⇒ **顺延**（不是失败）。"""
        if decision.not_before is None:
            # ``decide`` 说"说不清什么时候能发就不许发"。放行一条额度已满的发布不可逆（R14），
            # 而"顺延到 None"会让作业永远停在 pending 且没人知道为什么 ⇒ 如实报错。
            raise StudioError(
                decision.hint or "限频守卫没有给出下一个可用时刻",
                code=ErrorCode.PUBLISH_RATELIMIT,
                context={
                    "account_id": account.account_id,
                    "used_today": decision.used_today,
                    "daily_limit": decision.daily_limit,
                    "reason": decision.reason,
                },
                remediation="检查 publications 的 published_at 与 pool_settings.rate_limit_json",
            )
        raise UnitDeferred(
            decision.hint or f"账号 {account.account_id} 触发限频，顺延",
            not_before=decision.not_before,
            reason=decision.reason or DAILY_LIMIT_REASON,
        )

    # ── 内容 ────────────────────────────────────────────────────────────

    def _content(
        self,
        task: Any,
        *,
        platform_cfg: PlatformConfig,
        payload: Mapping[str, Any],
    ) -> tuple[str, str, tuple[str, ...], Path | None, list[str]]:
        """标题 / 文案 / 话题 / 封面 + 提示（§06.2.2）。

        payload 优先、稿件兜底：投递方（发布面板 / 定时器 / CLI）可以**替这一条**定标题，
        没给就回到稿件自己的标题与 CTA —— 与 ``PublishService.dry_run`` 同一口径，
        两条路给出不同的默认标题会让"演练通过、真发不一样"。
        """
        task_id = str(getattr(task, "id", "") or "")
        script_payload = read_active_script(self._connection, task_id)
        script = script_payload[0] if script_payload else None

        title = _opt_str(payload, "title") or (script.title if script else None) or task.title or task_id
        raw_caption = _opt_str(payload, "caption")
        if raw_caption is None:
            raw_caption = (script.cta if script else None) or ""
        tag_plan = render_tags(_tags(payload), syntax=platform_cfg.tag_syntax, max_tags=platform_cfg.tag_max)
        caption_plan = build_caption(raw_caption, tag_plan, caption_max=platform_cfg.caption_max)
        cover = _cover_path(task)

        warnings: list[str] = []
        fit = fit_text(title, limit=platform_cfg.title_max)
        if not fit.ok:
            warnings.append(
                f"标题超出上限 {fit.over_by} 字（上限 {fit.limit}）：平台会自己截，这里只提示（§06.2.2）"
            )
        if caption_plan.dropped_tags:
            warnings.append(f"话题超过上限被丢弃：{'、'.join(caption_plan.dropped_tags)}")
        if caption_plan.truncated_by:
            warnings.append(f"文案装不下，截掉了 {caption_plan.truncated_by} 字")
        if platform_cfg.cover_required and cover is None:
            warnings.append(
                f"{platform_cfg.publisher} 要求自定义封面，但这条任务还没出封面（先跑 publish cover）"
            )
        return title, caption_plan.text, caption_plan.tags, cover, warnings

    def _next_metric_at(self) -> str | None:
        """数据回收的第一个时点（T+1h，``metrics_schedule_hours`` 的第一项）。"""
        hours = [int(h) for h in self._publish.metrics_schedule_hours if int(h) > 0]
        if not hours:
            return None
        return format_iso(utc_now() + timedelta(hours=min(hours)))

    # ── 留痕 ────────────────────────────────────────────────────────────

    def _record_manual(
        self,
        row: PublicationRow,
        ctx: UnitContext,
        *,
        reason: str,
        hint: str | None = None,
    ) -> None:
        """转人工写一条 ``audit_ops``（§06.10 不变量 3：处置动作必须留痕）。

        失败**不抛**：留痕写不进去不该让"已经转人工"这个结论翻掉 —— 那会让面板与
        库里那一行对不上，而操作员看的正是面板。
        """
        try:
            self._audit.record(
                actor="worker",
                action=AUDIT_MANUAL,
                target_type="publication",
                target_id=row.id,
                task_id=row.task_id,
                actor_ref=ctx.worker_id,
                after={
                    "status": row.status,
                    "platform": row.platform,
                    "account_id": row.account_id,
                    "error_code": row.error_code,
                },
                result="ok",
                reason=_clip(f"{reason}{'；' + hint if hint else ''}"),
                source="worker",
            )
        except Exception as exc:  # 留痕失败不改变业务结论
            logger.warning("publish.audit_failed", publication_id=row.id, error=str(exc))

    def _report(self, ctx: UnitContext, state: Mapping[str, Any]) -> None:
        """把进度写进 ``jobs.result_json``（写不进去只说明租约丢了，**不是错误**）。"""
        payload = dict(state)
        payload.setdefault("job_id", ctx.job_id)
        self._store.report_progress(job_id=ctx.job_id, worker_id=ctx.worker_id, result=payload)

    def _log_line(self, ctx: UnitContext, level: Severity, message: str) -> None:
        if self._log is None:
            return
        self._log.append(
            level=level,
            source=_LOG_SOURCE,
            message=message,
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            worker_id=ctx.worker_id,
        )

    def _outcome(
        self,
        ctx: UnitContext,
        row: PublicationRow,
        *,
        account: AccountConfig,
        stage: str,
        started_at: str,
        decision: RateDecision,
        warnings: Sequence[str],
        note: str,
    ) -> PublishOutcome:
        """把库里那一行压成一份面板直接可读的结论。"""
        return PublishOutcome(
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            platform=row.platform,
            account_id=account.account_id,
            publication_id=row.id,
            stage=stage,
            status=row.status,
            note=_clip(note),
            started_at=started_at,
            dry_run=row.dry_run,
            url=row.url,
            platform_post_id=row.platform_post_id,
            attempts=ctx.attempt,
            max_attempts=ctx.job.max_attempts,
            used_today=decision.used_today,
            daily_limit=decision.daily_limit,
            error_code=row.error_code,
            error_message=row.error_message,
            evidence=row.evidence,
            warnings=tuple(warnings),
        )


def build_publish_handler(
    *,
    paths: StudioPaths,
    log: LogService | None = None,
    connection: sqlite3.Connection | None = None,
    publish: PublishConfig | None = None,
    pool: PoolConfig | None = None,
    runner: Callable[[Any], Any] = asyncio.run,
    headless: bool = True,
) -> PublishPlatformHandler:
    """发布池进程入口的装配（``workers/run_publish.py`` 调它）。

    连接**默认自己开一条、故意不关**：它挂着进度写入、``publications`` 与留痕，
    而池 worker 是"起一次、跑到关停"的常驻进程（与 ``build_voice_handler`` 同一条）。

    配置缺省**在这里读一次**（``load_config`` 会强校验）：平台 / 账号 / 限频 / 演练开关
    都是"这次进程启动时定的环境事实"。放进单元里就是"每发一条先读八份 yaml"，
    而其中任何一份写错都会在**第一次发布**时才炸 —— 那时作业已经在跑发布流程了。
    """
    resolved = connection if connection is not None else connect(paths.db_file)
    bundle = load_config(paths).bundle
    return PublishPlatformHandler(
        paths=paths,
        connection=resolved,
        publish=publish if publish is not None else bundle.publish,
        pool=pool if pool is not None else bundle.pools.pools["publish"],
        log=log if log is not None else LogService(resolved),
        runner=runner,
        headless=headless,
    )


def _as_code(raw: str) -> ErrorCode:
    """错误码字符串 → 枚举；认不出来就落 ``PUBLISH_FAILED``（**绝不抛**）。

    这里**不能**抛：它的调用点已经在处理一次发布失败，把"错误码不认识"升级成异常
    只会用第二条异常盖掉第一条，而第一条才是操作员要看的。
    """
    try:
        return ErrorCode(raw)
    except ValueError:
        return ErrorCode.PUBLISH_FAILED


def _evidence(evidence: PublishEvidence | None) -> dict[str, Any]:
    """取证压成 ``evidence_json`` 的形状（``None`` 的字段**不写**，别塞一堆 null）。"""
    if evidence is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in (
        ("screenshot_path", evidence.screenshot_path),
        ("dom_snapshot_path", evidence.dom_snapshot_path),
        ("stderr_tail", evidence.stderr_tail),
        ("selector_version", evidence.selector_version),
        ("platform_text", evidence.platform_text),
        ("stage", evidence.stage),
    ):
        if value is None:
            continue
        out[key] = value.as_posix() if isinstance(value, Path) else value
    return out


def _cover_path(task: Any) -> Path | None:
    """任务上下文里的封面路径（T5.1 的 ``set_cover_path`` 写进去的那一条）。"""
    value = (getattr(task, "context", None) or {}).get("cover_path")
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _opt_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value != "" else None


def _tags(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("tags")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item.strip())


def _truthy(value: Any) -> bool:
    """``payload`` 里的开关：字符串 ``"0"`` / ``"false"`` 也算假（它来自 JSON 与 CLI）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _clip(note: str) -> str:
    """说明截断（``result_json`` 是每行作业都要存一份的东西，不装长文本）。"""
    flat = " ".join(note.split())
    if len(flat) <= _NOTE_CHARS:
        return flat
    return flat[: _NOTE_CHARS - 1] + "…"
