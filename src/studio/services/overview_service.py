"""总览台服务（T4.2 · §04.4.5 第 1 行）—— 四种读 + 三种写。

读：四池卡片（含 worker 心跳）/ 今日产量 / 资源占用 / 服务就绪 + 当前策略；
写：暂停某池、恢复某池、切自动放行策略（"一键全自动"）。

为什么"暂停"写 `pool_settings` 而**不**重启 worker
-------------------------------------------------
`worker_base.run()` 每一轮认领前都读一次 `store.pool_runtime(pool)`
（`worker_base.py` 的认领循环），而 `JobStore.claim()` 开头就是
`if runtime.paused: return None`。于是"暂停"只要改一行 DB：新单元不再被认领，
**在途单元照常跑完** —— 这正好是 T4.2 验收要的语义。重启 worker 反而会把在途
单元的租约丢掉、等 sweeper 回收后重排，白白多跑一遍（裁定 138）。

生效延迟 ≤ 一个空转退避周期（200ms–2s，指数退避），**不是"立刻"**。
所以面板上写的是"已暂停（在途 N 个跑完后停）"，而不是"已停止"。

为什么"一键全自动"改的是 YAML 而不是另开一张表
---------------------------------------------
`approval.auto_approve_policy` 的真相源在 `config/app.yaml`（§04.4.4），
消费方是写稿池里的 `ReviewService`。再建一张表就等于有**两处真相**，
分叉的那一天没人说得清哪份算数。所以这里做三件事：写盘 + 同步内存 + 留痕。

`paused_at` / `paused_by` 恢复时**清空**
---------------------------------------
列名是 `paused_at`（"处于暂停态的时间戳"）；留一个"并不在暂停"的时间戳只会让
人误判。而"谁在什么时候暂停过"本来就在 `audit_ops` 里永久保留，
不需要这两个列兼职做历史（裁定 138）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from studio.core.clock import format_iso, parse_iso, utc_now
from studio.core.config import AutoApprovePolicy, RuntimeSettings, set_auto_approve_policy
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import JobStore, WorkerHeartbeat
from studio.db.repositories import AuditRepo, StatsRepo, TodayOutput
from studio.pools.heartbeat import HEARTBEAT_TIMEOUT_SEC
from studio.pools.runner import POOL_NAMES
from studio.services.log_service import LogSink
from studio.services.metrics_service import MetricsService, ResourceSnapshot
from studio.services.service_manager import ServiceManager, ServiceReadiness

if TYPE_CHECKING:  # 只给类型检查看：`watchdog_service` 反过来 import 本模块
    from studio.services.watchdog_service import WatchdogService

__all__ = [
    "OverviewService",
    "OverviewSnapshot",
    "PolicyOutcome",
    "PoolCard",
    "PoolPauseOutcome",
    "WorkerCard",
]

logger = get_logger("studio.services.overview")


# ══════════════════════════════════════════════════════════════════════
# 读 · 数据形状
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class WorkerCard:
    """一个 worker 的心跳（`pool.worker_status` 的展示面 · §04.5.1）。"""

    worker_id: str
    pool: str
    status: str
    current_job_id: str | None
    gpu_mem_mb: int | None
    rss_mb: int | None
    last_seen_at: str
    silent_sec: int
    #: 超过 `HEARTBEAT_TIMEOUT_SEC`（15s）且**未**标死 ⇒ 疑似猝死。
    #: 已标 `dead` 的不算"疑似" —— 那是已经确认的事实，用另一个色。
    stale: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "pool": self.pool,
            "status": self.status,
            "current_job_id": self.current_job_id,
            "gpu_mem_mb": self.gpu_mem_mb,
            "rss_mb": self.rss_mb,
            "last_seen_at": self.last_seen_at,
            "silent_sec": self.silent_sec,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class PoolCard:
    """一个池的卡片（`pool.stats` 的展示面 · §04.4.3）。"""

    pool: str
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    canceled: int
    concurrency: int
    running: int
    paused: bool
    paused_at: str | None
    paused_by: str | None
    oldest_pending_age_sec: int | None
    workers: tuple[WorkerCard, ...]
    #: 池参数缺失 / 表结构漂移时的降级说明（有值 ⇒ 这张卡片除了 `pool` 都不可信）
    error: str | None = None

    @property
    def backlog(self) -> int:
        """积压 = 待认领 + 依赖未满足（"还要跑多少个"）。"""
        return self.pending + self.blocked

    @property
    def alive_workers(self) -> int:
        return sum(1 for beat in self.workers if beat.status != "dead")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "pending": self.pending,
            "blocked": self.blocked,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "dead": self.dead,
            "canceled": self.canceled,
            "concurrency": self.concurrency,
            "running": self.running,
            "paused": self.paused,
            "paused_at": self.paused_at,
            "paused_by": self.paused_by,
            "oldest_pending_age_sec": self.oldest_pending_age_sec,
            "backlog": self.backlog,
            "alive_workers": self.alive_workers,
            "workers": [beat.to_dict() for beat in self.workers],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    """总览台一次读全（`GET /api/v1/overview` 的响应体）。"""

    generated_at: str
    pools: tuple[PoolCard, ...]
    today: TodayOutput
    resources: ResourceSnapshot | None
    resources_age_sec: float | None
    settings: RuntimeSettings
    services: tuple[ServiceReadiness, ...]
    #: 无人值守守护的摘要（T4.11 · `WatchdogService.snapshot()` 的原文）。
    #: `None` ⇒ 这套依赖没接守护（单测 / 极简家目录），面板那一块整个不画。
    watchdog: Mapping[str, Any] | None = None

    @property
    def worker_total(self) -> int:
        return sum(len(card.workers) for card in self.pools)

    @property
    def worker_alive(self) -> int:
        return sum(card.alive_workers for card in self.pools)

    @property
    def disk_low(self) -> bool | None:
        return None if self.resources is None else self.resources.disk_low

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "pools": [card.to_dict() for card in self.pools],
            "today": self.today.to_dict(),
            "resources": None if self.resources is None else self.resources.to_dict(),
            "resources_age_sec": self.resources_age_sec,
            "settings": self.settings.to_dict(),
            "services": [item.to_dict() for item in self.services],
            "watchdog": None if self.watchdog is None else dict(self.watchdog),
            "worker_total": self.worker_total,
            "worker_alive": self.worker_alive,
            "disk_low": self.disk_low,
        }


# ══════════════════════════════════════════════════════════════════════
# 写 · 结论
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PoolPauseOutcome:
    """一次暂停 / 恢复的结论（含"在途几个" —— 那才是用户真正在问的事）。"""

    pool: str
    paused: bool
    changed: bool
    running: int
    pending: int
    paused_at: str | None
    paused_by: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "paused": self.paused,
            "changed": self.changed,
            "running": self.running,
            "pending": self.pending,
            "paused_at": self.paused_at,
            "paused_by": self.paused_by,
            "note": (
                f"在途 {self.running} 个单元会跑完，之后不再认领新单元"
                if self.paused
                else f"已恢复认领（当前积压 {self.pending} 个）"
            ),
        }


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """一次策略切换的结论。"""

    policy: str
    previous: str
    changed: bool
    config_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "previous": self.previous,
            "changed": self.changed,
            "config_path": self.config_path,
        }


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class OverviewService:
    """总览台的读与写。

    :param settings: **共享的**运行期配置（`AppState` 持有）。"一键全自动"改的就是它，
        所以必须是引用而不是拷贝 —— 否则改完这一份，下一次请求构造的服务
        又拿到旧值，面板会"改了但没变"。
    :param watchdog: 守护服务（T4.11）。**共享引用**：它的重启计数是内存状态，
        现造一份等于每次刷新都从零开始。`None` ⇒ 响应里不出现 `watchdog` 段。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        paths: StudioPaths,
        settings: RuntimeSettings,
        metrics: MetricsService | None = None,
        watchdog: WatchdogService | None = None,
        log: LogSink | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._settings = settings
        self._metrics = metrics
        self._watchdog = watchdog
        self._log = log
        self._stats = StatsRepo(connection)
        self._audit = AuditRepo(connection)

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, *, now: datetime | None = None) -> OverviewSnapshot:
        """一次读全（面板首屏 + 5s 轮询共用同一个形状）。"""
        moment = now or utc_now()
        store = JobStore(self._connection)
        pools = tuple(self._pool_card(store, name, moment) for name in POOL_NAMES)
        snapshot = self._metrics.last if self._metrics is not None else None
        return OverviewSnapshot(
            generated_at=format_iso(moment),
            pools=pools,
            today=self._stats.today_output(now=moment),
            resources=snapshot,
            resources_age_sec=None if snapshot is None else _age_sec(snapshot.sampled_at, moment),
            settings=self._settings,
            services=ServiceManager(self._paths).readiness_all(),
            watchdog=None if self._watchdog is None else self._watchdog.snapshot(),
        )

    def _pool_card(self, store: JobStore, pool: str, moment: datetime) -> PoolCard:
        """一个池的卡片；**单池失败不拖垮整页**（与握手快照的 `_pools_provider` 同取舍）。"""
        try:
            stats = store.stats(pool=pool, now=moment)
            runtime = store.pool_runtime(pool)
        except StudioError as exc:
            logger.warning("overview.pool_failed", pool=pool, error=str(exc))
            return PoolCard(
                pool=pool,
                pending=0,
                blocked=0,
                claimed=0,
                succeeded=0,
                failed=0,
                dead=0,
                canceled=0,
                concurrency=0,
                running=0,
                paused=False,
                paused_at=None,
                paused_by=None,
                oldest_pending_age_sec=None,
                workers=(),
                error=exc.message,
            )
        return PoolCard(
            pool=pool,
            pending=stats.pending,
            blocked=stats.blocked,
            claimed=stats.claimed,
            succeeded=stats.succeeded,
            failed=stats.failed,
            dead=stats.dead,
            canceled=stats.canceled,
            concurrency=stats.concurrency,
            running=stats.running,
            paused=stats.paused,
            paused_at=runtime.paused_at,
            paused_by=runtime.paused_by,
            oldest_pending_age_sec=stats.oldest_pending_age_sec,
            workers=tuple(_worker_card(beat, moment) for beat in stats.workers),
        )

    # ── 写 · 暂停 / 恢复 ────────────────────────────────────────────────

    def set_pool_paused(
        self,
        *,
        pool: str,
        paused: bool,
        reason: str | None = None,
        actor: str = "user",
        actor_ref: str | None = None,
        source: str = "webui",
    ) -> PoolPauseOutcome:
        """暂停 / 恢复一个池（写 `pool_settings` + `audit_ops`，**不重启 worker**）。

        :param actor: `audit_ops.actor` —— 只允许 DDL 的四个字面量（`user` / `system` /
            `auto` / `worker`）。**自动门禁也传 `auto`**：审计要能按"人干的/机器干的"
            聚合，那是它存在的意义。
        :param actor_ref: `pool_settings.paused_by` 的**归属标记**（`audit_ops.actor_ref`
            同值）。写 `auto:disk_low` 这类具体来源，面板上才能一眼分辨"这是磁盘门禁
            暂停的"还是"这是我刚才按的" —— 而水位恢复时，自动流程**只**放开属于它的
            那几个（T4.11）。留 `None` ⇒ 退回 `actor`。
        """
        _require_pool(pool)
        store = JobStore(self._connection)
        runtime = store.pool_runtime(pool)
        running = store.running_count(pool)
        pending = store.stats(pool=pool).pending
        owner = actor_ref or actor
        if runtime.paused == paused:
            # 幂等：重复点"暂停"不该再写一条留痕（那会把审计表刷成一串"又暂停了一次"）
            return PoolPauseOutcome(
                pool=pool,
                paused=paused,
                changed=False,
                running=running,
                pending=pending,
                paused_at=runtime.paused_at,
                paused_by=runtime.paused_by,
            )

        moment = utc_now()
        stamp = format_iso(moment)
        self._connection.execute(
            """
            UPDATE pool_settings
               SET paused = ?, paused_at = ?, paused_by = ?, updated_at = ?, updated_by = ?
             WHERE pool = ?
            """,
            (1 if paused else 0, stamp if paused else None, owner if paused else None, stamp, actor, pool),
        )
        self._audit.record(
            actor=actor,
            actor_ref=actor_ref,
            action="pool.pause" if paused else "pool.resume",
            target_type="pool",
            target_id=pool,
            before={"paused": runtime.paused, "paused_at": runtime.paused_at, "paused_by": runtime.paused_by},
            after={"paused": paused, "running": running, "pending": pending},
            reason=reason or ("WebUI 暂停池认领" if paused else "WebUI 恢复池认领"),
            source=source,
        )
        self._emit(
            level="info",
            message=(
                f"{pool} 池已{'暂停' if paused else '恢复'}认领"
                f"（在途 {running} 个跑完{'后不再认领新单元' if paused else ''}）"
            ),
            payload={"code": "POOL_PAUSED" if paused else "POOL_RESUMED", "pool": pool, "running": running},
        )
        logger.info("overview.pool_paused", pool=pool, paused=paused, running=running)
        return PoolPauseOutcome(
            pool=pool,
            paused=paused,
            changed=True,
            running=running,
            pending=pending,
            paused_at=stamp if paused else None,
            paused_by=owner if paused else None,
        )

    # ── 写 · 一键全自动 ────────────────────────────────────────────────

    def set_auto_approve_policy(
        self,
        *,
        policy: AutoApprovePolicy,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PolicyOutcome:
        """切自动放行策略（写 `config/app.yaml` + 同步内存 + `audit_ops`）。

        **顺序要紧**：先写盘（会回读校验，失败就抛），成功后才改内存与留痕。
        反过来会出现"面板显示已全自动、文件里还是 grade_a"—— 而重启之后
        一切回到 grade_a，没人查得出为什么（裁定 139）。
        """
        _require_policy(policy)
        previous = self._settings.auto_approve_policy
        if previous == policy:
            return PolicyOutcome(
                policy=policy,
                previous=previous,
                changed=False,
                config_path=str(self._paths.config_dir / "app.yaml"),
            )

        path = set_auto_approve_policy(self._paths, policy)
        self._settings.auto_approve_policy = policy
        self._audit.record(
            actor=actor,
            action="approval.policy_change",
            target_type="config",
            target_id="approval.auto_approve_policy",
            before={"auto_approve_policy": previous},
            after={"auto_approve_policy": policy},
            reason=reason or ("一键全自动（A+B 自动放行）" if policy == "grade_ab" else f"切回 {policy}"),
            source=source,
        )
        self._emit(
            level="warn" if policy == "grade_ab" else "info",
            message=(
                f"自动放行策略 {previous} → {policy}"
                + ("（确认闸只留 C 级；写稿池 worker 下一单元生效）" if policy == "grade_ab" else "")
            ),
            payload={"code": "APPROVAL_POLICY_CHANGED", "previous": previous, "policy": policy},
        )
        logger.warning("overview.policy_changed", previous=previous, policy=policy)
        return PolicyOutcome(policy=policy, previous=previous, changed=True, config_path=str(path))

    # ── 内部 ────────────────────────────────────────────────────────────

    def _emit(self, *, level: Severity, message: str, payload: Mapping[str, Any]) -> None:
        """落一行操作日志（`source=system`；`log=None` ⇒ 静默，单测可以直接省掉）。"""
        if self._log is None:
            return
        self._log(level=level, source="system", message=message, payload=dict(payload))


def _worker_card(beat: WorkerHeartbeat, moment: datetime) -> WorkerCard:
    silent = int(max(0.0, (moment - parse_iso(beat.last_seen_at)).total_seconds()))
    return WorkerCard(
        worker_id=beat.worker_id,
        pool=beat.pool,
        status=beat.status,
        current_job_id=beat.current_job_id,
        gpu_mem_mb=beat.gpu_mem_mb,
        rss_mb=beat.rss_mb,
        last_seen_at=beat.last_seen_at,
        silent_sec=silent,
        stale=beat.status != "dead" and silent > HEARTBEAT_TIMEOUT_SEC,
    )


def _age_sec(sampled_at: str, moment: datetime) -> float:
    return round(max(0.0, (moment - parse_iso(sampled_at)).total_seconds()), 1)


def _require_pool(pool: str) -> None:
    """纵深防御：路由层的 `Literal` 已经拦过，这里再拦一次（服务也可能被 CLI 调）。"""
    if pool not in POOL_NAMES:
        raise StudioError(
            f"未知的池：{pool}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"pool": pool, "valid": list(POOL_NAMES)},
            remediation=f"池名只能是 {' / '.join(POOL_NAMES)}",
        )


def _require_policy(policy: AutoApprovePolicy) -> None:
    if policy not in ("off", "grade_a", "grade_ab"):
        raise StudioError(
            f"未知的放行策略：{policy}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"policy": policy, "valid": ["off", "grade_a", "grade_ab"]},
            remediation="策略只能是 off / grade_a / grade_ab",
        )
