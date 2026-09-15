"""四池调度控制台服务（T4.10 · §03.4.4）—— 池的读、并发旋钮、死信重投。

控制台要回答三个问题，一个模型对一块
------------------------------------
① "四个池各自什么配置、现在什么状态？" ⇒ :class:`PoolConsoleCard`
   （配置来自 ``pools.yaml``，状态来自 ``jobs`` / ``pool_settings``，**并排摆出来**）；
② "把并发调一下" ⇒ :meth:`PoolService.set_concurrency`（写 DB + 留痕，无需重启）；
③ "这几个死信我想再跑一次" ⇒ :meth:`PoolService.requeue_dead`（逐条如实返回）。

为什么把"配置值"和"运行值"摆在一起
----------------------------------
`pools.yaml` 里的 ``concurrency: 1`` 与 ``pool_settings.concurrency`` 是**两个数**：
前者是出厂值，后者是当前值（0006_seed 顶部写明"DB 值优先"）。只显示一个，就会
出现"我明明把 YAML 改成 3 了，面板还是 1"这种查不完的悬案。两个都显示，
差异一眼可见 —— 这正是 `config/pools.yaml` 与 DB 双真相的唯一体面解法。

暂停 / 恢复**不在**这里
----------------------
它已经在 T4.2 落地（``POST /api/v1/overview/pools``，`OverviewService.set_pool_paused`）。
本服务不再提供第二个写入口：同一件事两条写路径，审计 / 事件 / 幂等判据迟早在
两处之间分叉（T4.10 裁定 144）。四池控制台是它的**第二个视图**。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from studio.core.clock import format_iso, utc_now
from studio.core.config import (
    AutoConcurrencyConfig,
    PoolName,
    PoolsConfig,
    concurrency_bounds,
)
from studio.core.errors import StudioError
from studio.core.logging import get_logger
from studio.core.proto import Severity
from studio.db import Job, JobStore
from studio.pools.runner import POOL_NAMES
from studio.services.log_service import LogSink

__all__ = [
    "DEAD_LETTER_PAGE",
    "ConcurrencyOutcome",
    "DeadLetterCard",
    "PoolConsoleCard",
    "PoolConsoleSnapshot",
    "PoolService",
    "RequeueFailure",
    "RequeueOutcome",
]

logger = get_logger("studio.services.pool")

#: 每个池卡片带回的死信条数（死信是"要人管"的东西，不是瀑布流；
#: 与 §03.4.6 规则 4"永远带 LIMIT"一致）
DEAD_LETTER_PAGE: int = 20


# ══════════════════════════════════════════════════════════════════════
# 读 · 数据形状
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DeadLetterCard:
    """一条死信（``jobs.status='dead'``）。"""

    job_id: str
    task_id: str
    unit_type: str
    unit_ref: str
    attempts: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    finished_at: str | None

    @classmethod
    def from_job(cls, job: Job) -> DeadLetterCard:
        return cls(
            job_id=job.id,
            task_id=job.task_id,
            unit_type=job.unit_type,
            unit_ref=job.unit_ref,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            error_code=job.error_code,
            error_message=job.error_message,
            finished_at=job.finished_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "unit_type": self.unit_type,
            "unit_ref": self.unit_ref,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class PoolConsoleCard:
    """一个池在控制台上的全部信息（配置 + 运行值 + 死信）。"""

    pool: str
    # ── 配置（pools.yaml）────────────────────────────────────────────
    unit_type: str | None
    priority: int | None
    poll_ms: int | None
    unit_timeout_sec: int | None
    #: ``pools.yaml`` 里的**出厂并发**。它必须和下面的 ``concurrency``（当前值）
    #: 一起上桌：两个数长得一样的时候，人才敢确认「我改的 YAML 到底生效没有」。
    config_concurrency: int | None
    # ── 运行值（pool_settings）───────────────────────────────────────
    concurrency: int
    concurrency_min: int
    concurrency_max: int
    paused: bool
    paused_at: str | None
    paused_by: str | None
    lease_sec: int
    max_attempts: int
    # ── 状态（jobs）─────────────────────────────────────────────────
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    canceled: int
    running: int
    oldest_pending_age_sec: int | None
    # ── 自动降并发（T4.10）──────────────────────────────────────────
    consecutive_oom: int
    oom_threshold: int
    auto_degrade_enabled: bool
    #: 池参数缺失 / 配置读不到时的降级说明（有值 ⇒ 除 `pool` 外都不可信）
    error: str | None = None
    dead_letters: tuple[DeadLetterCard, ...] = ()

    @property
    def backlog(self) -> int:
        """积压 = 待认领 + 依赖未满足（"还要跑多少个"）。"""
        return self.pending + self.blocked

    @property
    def at_concurrency_ceiling(self) -> bool:
        return self.concurrency >= self.concurrency_max

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "unit_type": self.unit_type,
            "priority": self.priority,
            "poll_ms": self.poll_ms,
            "unit_timeout_sec": self.unit_timeout_sec,
            "config_concurrency": self.config_concurrency,
            "concurrency": self.concurrency,
            "concurrency_min": self.concurrency_min,
            "concurrency_max": self.concurrency_max,
            "at_concurrency_ceiling": self.at_concurrency_ceiling,
            "paused": self.paused,
            "paused_at": self.paused_at,
            "paused_by": self.paused_by,
            "lease_sec": self.lease_sec,
            "max_attempts": self.max_attempts,
            "pending": self.pending,
            "blocked": self.blocked,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "dead": self.dead,
            "canceled": self.canceled,
            "running": self.running,
            "oldest_pending_age_sec": self.oldest_pending_age_sec,
            "backlog": self.backlog,
            "consecutive_oom": self.consecutive_oom,
            "oom_threshold": self.oom_threshold,
            "auto_degrade_enabled": self.auto_degrade_enabled,
            "dead_letters": [item.to_dict() for item in self.dead_letters],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PoolConsoleSnapshot:
    """控制台一次读全（``GET /api/v1/pools`` 的响应体）。"""

    generated_at: str
    pools: tuple[PoolConsoleCard, ...]
    auto_degrade_enabled: bool
    oom_threshold: int
    config_path: str
    config_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "pools": [card.to_dict() for card in self.pools],
            "auto_degrade_enabled": self.auto_degrade_enabled,
            "oom_threshold": self.oom_threshold,
            "config_path": self.config_path,
            "config_error": self.config_error,
        }


# ══════════════════════════════════════════════════════════════════════
# 写 · 结论
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ConcurrencyOutcome:
    """一次并发调整的结论（``note`` 直接给人看）。"""

    pool: str
    concurrency: int
    previous: int
    changed: bool
    running: int
    pending: int
    paused: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "concurrency": self.concurrency,
            "previous": self.previous,
            "changed": self.changed,
            "running": self.running,
            "pending": self.pending,
            "paused": self.paused,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RequeueFailure:
    """重投失败的一条（带 ``code`` + ``remediation``，与批量通过同一取舍）。"""

    job_id: str
    code: str
    message: str
    remediation: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class RequeueOutcome:
    """一次死信重投的结论（逐条如实返回，**部分失败不回滚**）。"""

    requested: int
    requeued: tuple[str, ...]
    failed: tuple[RequeueFailure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "requeued": list(self.requeued),
            "failed": [item.to_dict() for item in self.failed],
        }


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class PoolService:
    """四池调度控制台（**同步**：与 ``OverviewService`` 同手法）。

    :param connection: 当前线程的连接（``ThreadLocalConnections`` 提供）
    :param config: ``config/pools.yaml`` 的解析结果；``None`` ⇒ 读不到，
        卡片会**显式**带上 ``error``（而不是编一个默认优先级出来）
    :param config_path: 展示用（"配置到底读的哪一份"）
    :param log: 日志出口（并发调整写一条 ``info``；死信重投写 ``warn``/``info``）
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        config: PoolsConfig | None = None,
        config_path: str = "",
        log: LogSink | None = None,
    ) -> None:
        self._connection = connection
        self._config = config
        self._config_path = config_path
        self._log = log

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, *, now: datetime | None = None) -> PoolConsoleSnapshot:
        """一次读全（面板首屏 + WS `pool.stats` 到达后重拉共用同一个形状）。"""
        moment = now or utc_now()
        store = JobStore(self._connection)
        return PoolConsoleSnapshot(
            generated_at=format_iso(moment),
            pools=tuple(self._card(store, name, moment) for name in POOL_NAMES),
            auto_degrade_enabled=self._auto_degrade_enabled,
            oom_threshold=self._oom_threshold,
            config_path=self._config_path,
            config_error=None if self._config is not None else self._config_error,
        )

    @property
    def _auto_degrade_enabled(self) -> bool:
        return True if self._config is None else self._config.auto_concurrency.enabled

    @property
    def _oom_threshold(self) -> int:
        if self._config is None:
            return AutoConcurrencyConfig().oom_threshold
        return self._config.auto_concurrency.oom_threshold

    @property
    def _config_error(self) -> str:
        path = self._config_path or "config/pools.yaml"
        return f"读不到 {path}：优先级 / 单元类型 / OOM 阈值都不可信"

    def _card(self, store: JobStore, pool: PoolName, moment: datetime) -> PoolConsoleCard:
        """一个池的卡片；**单池失败不拖垮整页**（与总览台同取舍）。"""
        low, high = concurrency_bounds(pool)
        pool_config = None if self._config is None else self._config.pools.get(pool)
        try:
            stats = store.stats(pool=pool, now=moment)
            runtime = store.pool_runtime(pool)
            dead = store.dead_letters(pool=pool, limit=DEAD_LETTER_PAGE)
        except StudioError as exc:
            logger.warning("pool.console_card_failed", pool=pool, error=str(exc))
            return PoolConsoleCard(
                pool=pool,
                unit_type=None,
                priority=None,
                poll_ms=None,
                unit_timeout_sec=None,
                config_concurrency=None,
                concurrency=0,
                concurrency_min=low,
                concurrency_max=high,
                paused=False,
                paused_at=None,
                paused_by=None,
                lease_sec=0,
                max_attempts=0,
                pending=0,
                blocked=0,
                claimed=0,
                succeeded=0,
                failed=0,
                dead=0,
                canceled=0,
                running=0,
                oldest_pending_age_sec=None,
                consecutive_oom=0,
                oom_threshold=self._oom_threshold,
                auto_degrade_enabled=self._auto_degrade_enabled,
                error=exc.message,
            )
        return PoolConsoleCard(
            pool=pool,
            unit_type=None if pool_config is None else pool_config.unit_type,
            priority=None if pool_config is None else pool_config.priority,
            poll_ms=None if pool_config is None else pool_config.poll_ms,
            unit_timeout_sec=None if pool_config is None else pool_config.unit_timeout_sec,
            config_concurrency=None if pool_config is None else pool_config.concurrency,
            concurrency=runtime.concurrency,
            concurrency_min=low,
            concurrency_max=high,
            paused=runtime.paused,
            paused_at=runtime.paused_at,
            paused_by=runtime.paused_by,
            lease_sec=runtime.lease_sec,
            max_attempts=runtime.max_attempts,
            pending=stats.pending,
            blocked=stats.blocked,
            claimed=stats.claimed,
            succeeded=stats.succeeded,
            failed=stats.failed,
            dead=stats.dead,
            canceled=stats.canceled,
            running=stats.running,
            oldest_pending_age_sec=stats.oldest_pending_age_sec,
            consecutive_oom=runtime.consecutive_oom,
            oom_threshold=self._oom_threshold,
            auto_degrade_enabled=self._auto_degrade_enabled,
            error=None if pool_config is not None else self._config_error,
            dead_letters=tuple(DeadLetterCard.from_job(job) for job in dead),
        )

    # ── 写 · 并发旋钮 ───────────────────────────────────────────────────

    def set_concurrency(
        self,
        *,
        pool: str,
        concurrency: int,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> ConcurrencyOutcome:
        """改并发（写 ``pool_settings`` + ``audit_ops``，**生效无需重启** · §03.4.4）。"""
        store = JobStore(self._connection)
        change = store.set_concurrency(
            pool=pool, concurrency=concurrency, actor=actor, source=source, reason=reason
        )
        pending = store.stats(pool=pool).pending
        if not change.changed:
            note = f"「{pool}」并发本来就是 {concurrency}（没有改动）"
        elif change.running > concurrency:
            note = (
                f"并发 {change.previous} → {concurrency}：在途 {change.running} 个会跑完，"
                f"之后收敛到 {concurrency}（**不是**立刻掐掉）"
            )
        else:
            note = f"并发 {change.previous} → {concurrency}（下一轮认领即生效）"
        self._emit(
            level="info",
            message=f"{pool} 池并发调整为 {concurrency}（原 {change.previous}）",
            payload={
                "code": "POOL_CONCURRENCY_SET",
                "pool": pool,
                "concurrency": concurrency,
                "previous": change.previous,
                "running": change.running,
            },
        )
        return ConcurrencyOutcome(
            pool=pool,
            concurrency=change.concurrency,
            previous=change.previous,
            changed=change.changed,
            running=change.running,
            pending=pending,
            paused=change.paused,
            note=note,
        )

    # ── 写 · 死信重投 ───────────────────────────────────────────────────

    def requeue_dead(
        self,
        *,
        job_ids: tuple[str, ...],
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> RequeueOutcome:
        """批量重投死信（``dead → pending`` + ``attempts`` 归零 + ``audit_ops``）。

        逐条独立：一条不是死信（或已被别人重投过）不影响其余（与批量通过同一取舍）。
        ``requeue_dead`` 自带"只有 dead 能重投"的守卫，这里**不**抄第二遍判断
        —— 业务不变量只有一个权威落点（§04.4.4 不变量 3 的同一条理由）。

        **不清零连续 OOM 计数**：计数器量的是"显存能不能装下"，与某条作业的进度无关；
        清零只会让"人手动重投一条死信"顺手掩盖掉正在恶化的容量问题。
        """
        store = JobStore(self._connection)
        done: list[str] = []
        failed: list[RequeueFailure] = []
        for job_id in job_ids:
            try:
                store.requeue_dead(job_id=job_id, actor=actor, actor_ref=actor, source=source, reason=reason)
            except StudioError as exc:
                failed.append(
                    RequeueFailure(
                        job_id=job_id,
                        code=str(exc.code),
                        message=exc.message,
                        remediation=exc.remediation,
                    )
                )
                continue
            done.append(job_id)
        self._emit(
            level="info" if not failed else "warn",
            message=(
                f"死信重投：成功 {len(done)} 条"
                + (f"，失败 {len(failed)} 条（逐条原因见下）" if failed else "")
            ),
            payload={
                "code": "JOB_REQUEUED",
                "requeued": done,
                "failed": [item.job_id for item in failed],
            },
        )
        return RequeueOutcome(requested=len(job_ids), requeued=tuple(done), failed=tuple(failed))

    # ── 内部 ────────────────────────────────────────────────────────────

    def _emit(self, *, level: Severity, message: str, payload: dict[str, Any]) -> None:
        """写一条日志（没注入出口就只留 structlog 一行 —— 与 `OverviewService` 同手法）。

        ``self._log`` 是 :class:`~studio.services.log_service.LogSink`（一个 **callable**，
        不是 ``LogService`` 本身）：直接调它，而不是 ``.append(...)``。
        """
        if self._log is not None:
            self._log(level=level, source="pool.console", message=message, payload=dict(payload))
        logger.info("pool.console", level=level, message=message, **payload)
