"""无人值守守护与故障自愈（T4.11 · §01.2.1 / §03.7.5 / §1.7）。

一轮 `tick()` 干四件事
--------------------
① **进程守护**：五个常驻进程里死掉的按指数退避重启；窗口内重启次数超限 ⇒
   **停手 + 告警**（防"重启风暴"把机器拖垮）；
② **磁盘水位门禁**：`free_D < 门禁` ⇒ 暂停 `render` / `publish` 认领；水位恢复 ⇒
   **只恢复自己暂停的那几个**；
③ **显存自动回升**：`recover_after_min` 内没有新的 OOM、显存低于
   `gpu_mem_high_ratio`、且最近一次并发变更**是自动降级** ⇒ 并发 +1。
   T4.10 只做了"降"，这里把"升"补上（§03.3.17 末尾那条挂账）；
④ **任务收尾清扫**：单元死信 ⇒ 任务 `failed`；`attempt_count ≥ 3` ⇒ `manual_pool`
   （§03.4.3「任务级失败」与 §1.7「反复失败进入人工池」）。

三条"不做什么"（与上面同等重要）
------------------------------
- **不重启自己**。守护跑在 `api` 进程里，`api` 死了没有任何进程内机制能把它拉起来。
  所以 `api` 在报告里恒标 `self_guarded`，并写明"由 `ops/start_all.ps1` 那条启动路径
  守护" —— 宁可如实说守不住，也不假装守住了（P4）。
- **不碰人工暂停**。`paused_by != 'auto:disk_low'` 的池，水位恢复后**一个字都不写**：
  人按下的暂停只有人能取消。
- **不驳回人工覆盖**。人把门禁暂停的池手动放开 ⇒ 这一拍**不再暂停它**（见
  `_disk_override`）：阈值是机器给的，决定权在人。覆盖状态从 `audit_ops` 推导，
  水位恢复后写一条"解除覆盖"的留痕自清 —— 否则一次覆盖会变成**永久豁免**。
- **不猜**。没采过磁盘、没有显存读数、`config/pools.yaml` 读不到 ⇒ 对应分支整体跳过，
  并在报告里说明原因。基于猜测去暂停别人的池，比不暂停坏得多。

为什么判死靠 PID 台账而不是 15s 心跳
------------------------------------
心跳是**给面板看的**（`worker_heartbeats` 超 15s ⇒ `dead`），它慢是刻意的：容忍连丢
两拍，不误杀卡在长 I/O 上的进程。但"进程还在不在"这个问题 `ServiceManager` 的 PID
台账 + `psutil` 能**立刻**回答，而验收要求是"杀任一 worker ⇒ 15s 内被重启"。
用 5s 一拍 + PID 判死，最坏 5s 就发现、再花几秒拉起 —— 余量留给"重启后恢复认领"。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final

from studio.core.clock import format_iso, parse_iso, utc_now
from studio.core.config import (
    PoolsConfig,
    RuntimeSettings,
    WatchdogConfig,
    concurrency_bounds,
)
from studio.core.errors import StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import JobStore
from studio.db.lease import backoff_delay_ms
from studio.db.repositories import AuditRepo
from studio.domain.enums import TaskStatus
from studio.domain.task_service import TaskService
from studio.pools.runner import POOL_NAMES
from studio.services.log_service import LogService
from studio.services.metrics_service import MetricsService
from studio.services.overview_service import OverviewService
from studio.services.service_manager import (
    ServiceManager,
    ServiceReadiness,
    ServiceStatus,
    StartReport,
)

__all__ = [
    "AUTO_ACTOR",
    "AUTO_PAUSE_ACTOR",
    "DEFAULT_TICK_SEC",
    "LOG_SOURCE",
    "DiskGateOutcome",
    "ManualTick",
    "ServiceGuard",
    "TaskSweep",
    "WatchdogService",
    "WatchdogTick",
]

logger = get_logger("studio.services.watchdog")

#: `system_logs.source`（§04.5.2 命名空间；前端按它过滤）
LOG_SOURCE: Final[str] = "watchdog"

#: `audit_ops.actor` / `.source` —— 只允许 DDL 的四个字面量之一（自动动作都算 `auto`）
AUTO_ACTOR: Final[str] = "auto"

#: `pool_settings.paused_by` 的**归属标记**：只有它标出来的暂停，水位恢复后才由自动流程解除。
#: 为什么不用 `actor='auto'` 兼任：`paused_by` 是给人看的一句话，写清"谁因为什么暂停的"
#: 才能让人在面板上一眼分辨"这是磁盘门禁干的"还是"这是我刚才按的"。
AUTO_PAUSE_ACTOR: Final[str] = "auto:disk_low"

#: 重启日志的 `payload_json.code`。**不是** `system.alert.code`：那 8 个值被 §04.5.2 锁死
#: （与 T1.6 的 `WORKER_DEAD` / `WORKER_RESTART_HALTED` 同一处理）。
RESTARTED_CODE: Final[str] = "SERVICE_RESTARTED"
RESTART_HALTED_CODE: Final[str] = "SERVICE_RESTART_HALTED"
GATE_APPLIED_CODE: Final[str] = "DISK_GATE_APPLIED"
GATE_RELEASED_CODE: Final[str] = "DISK_GATE_RELEASED"
AUTORECOVER_CODE: Final[str] = "POOL_AUTORECOVERED"
MANUAL_TICK_CODE: Final[str] = "WATCHDOG_MANUAL_TICK"
OVERRIDDEN_CODE: Final[str] = "DISK_GATE_OVERRIDDEN"
OVERRIDE_CLEARED_CODE: Final[str] = "DISK_GATE_OVERRIDE_CLEARED"

#: 默认 tick（配置读不到时的兜底；正常一律走 `pools.yaml → watchdog.tick_sec`）
DEFAULT_TICK_SEC: Final[float] = 5.0

#: `snapshot()` 里带回的人工池条数（首屏够看就行；再多请去任务列表翻页）
MANUAL_POOL_ROWS: Final[int] = 20

#: 已经"活到头"的任务状态：清扫时一律不动它们（`failed` / `manual_pool` 也在内 ——
#: 它们本身是清扫的**产物**，再进一次就会来回横跳）
_TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    "completed",
    "published",
    "discarded",
    "canceled",
    "failed",
    "manual_pool",
)

#: 会改变"这个池的并发是谁定的"的审计动作（自动回升的判据与冷却锚点）
_CONCURRENCY_ACTIONS: Final[tuple[str, ...]] = (
    "pool.autodegrade",
    "pool.autorecover",
    "pool.set_concurrency",
)


@dataclass(slots=True)
class _GuardState:
    """一个被守护进程的**进程内**重启状态（滑动窗口计数 + 退避落点）。"""

    restarts: int = 0
    window_started_at: str | None = None
    next_restart_at: str | None = None
    halted: bool = False


@dataclass(frozen=True, slots=True)
class ServiceGuard:
    """一个被守护进程的当前结论（面板直接展示这个）。"""

    name: str
    guarded: bool
    running: bool
    pid: int | None = None
    restarts: int = 0
    halted: bool = False
    next_restart_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "guarded": self.guarded,
            "running": self.running,
            "pid": self.pid,
            "restarts": self.restarts,
            "halted": self.halted,
            "next_restart_at": self.next_restart_at,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DiskGateOutcome:
    """一次磁盘水位门禁的结论。"""

    low: bool | None = None
    enabled: bool = True
    applied: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "enabled": self.enabled,
            "applied": list(self.applied),
            "released": list(self.released),
            "skipped": list(self.skipped),
            "overridden": list(self.overridden),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TaskSweep:
    """一次任务收尾清扫的结论（`failed` / `manual_pool` 都是任务 id）。"""

    failed: tuple[str, ...] = ()
    manual_pool: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed": list(self.failed),
            "manual_pool": list(self.manual_pool),
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True, slots=True)
class WatchdogTick:
    """一轮守护的结论（`errors` 是**如实汇报**的单项失败，不是"整轮崩了"）。"""

    at: str
    enabled: bool = True
    services: tuple[ServiceGuard, ...] = ()
    restarted: tuple[str, ...] = ()
    halted: tuple[str, ...] = ()
    disk_gate: DiskGateOutcome = field(default_factory=DiskGateOutcome)
    recovered: tuple[str, ...] = ()
    sweep: TaskSweep = field(default_factory=TaskSweep)
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "enabled": self.enabled,
            "services": [item.to_dict() for item in self.services],
            "restarted": list(self.restarted),
            "halted": list(self.halted),
            "disk_gate": self.disk_gate.to_dict(),
            "recovered": list(self.recovered),
            "sweep": self.sweep.to_dict(),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class ManualTick:
    """一次**人工触发**的守护结论（`tick` 是那一轮本身，`audit_id` 是留痕编号）。"""

    tick: WatchdogTick
    audit_id: int | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = self.tick.to_dict()
        payload["audit_id"] = self.audit_id
        payload["note"] = self.note
        return payload


class WatchdogService:
    """无人值守守护的一轮实现（**纯同步**：`app/watchdog.py` 负责把它丢进线程池）。

    :param connection_factory: 按线程取连接（与 `LogService` 同一手法）。守护跑在
        线程池里，每次可能是不同线程，而 `sqlite3` 连接线程亲和。
    :param manager: 五进程编排器（PID 台账 + 就绪判定 + 拉起）
    :param metrics: 采样器（磁盘水位与显存读数的**唯一**来源；`None` ⇒ 两道门禁都跳过）
    :param settings: 运行期配置（`pause_pools_on_low` 是门禁的**总开关**）
    :param pools_config: `config/pools.yaml`；`None` ⇒ 守护整体停用（并如实说明原因）
    :param self_name: 守护者自己所在的那个进程名（恒不重启，标 `self_guarded`）
    """

    def __init__(
        self,
        *,
        paths: StudioPaths,
        connection_factory: Callable[[], sqlite3.Connection],
        manager: ServiceManager,
        log: LogService,
        metrics: MetricsService | None = None,
        settings: RuntimeSettings | None = None,
        pools_config: PoolsConfig | None = None,
        self_name: str | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._connection_factory = connection_factory
        self._manager = manager
        self._log = log
        self._metrics = metrics
        self._settings = settings
        self._pools = pools_config
        self._self_name = self_name
        self._now = now
        self._config: WatchdogConfig | None = None if pools_config is None else pools_config.watchdog
        self._states: dict[str, _GuardState] = {}
        self._override_logged: set[str] = set()
        self._seeded = False
        #: 同一时刻只允许一轮守护在跑。**两路**会同时进来：周期泵（`asyncio.to_thread`）
        #: 与 WebUI 的"立即自检"。两者都可能在"判死 ⇒ 拉起"之间穿插，结果是同一个池被
        #: 拉起两次（多出来的 worker 会按同一个并发上限抢单元）。锁在这里而不是路由里：
        #: 泵根本不经过路由。
        self._lock = threading.Lock()
        self._last: WatchdogTick | None = None

    # ── 只读 ────────────────────────────────────────────────────────────

    @property
    def config(self) -> WatchdogConfig | None:
        return self._config

    @property
    def enabled(self) -> bool:
        """配置开着**且** `config/pools.yaml` 读得到。"""
        return self._config is not None and self._config.enabled

    @property
    def runnable(self) -> bool:
        """现在起一轮守护**有意义**吗。

        `workers/` 目录不在 ⇒ 一个池 worker 都拉不起来（`readiness` 恒为
        `entry_missing`），此时起守护只是每 5s 白读一次进程表。集成测试的临时家目录
        正好落在这个分支上 —— 所以"默认不动手"既是省事也是安全：测试用例的
        `tasks` / `jobs` 不会被后台线程悄悄改掉。
        """
        return self.enabled and self._paths.workers_dir.is_dir()

    @property
    def tick_sec(self) -> float:
        return DEFAULT_TICK_SEC if self._config is None else self._config.tick_sec

    @property
    def last_tick(self) -> WatchdogTick | None:
        return self._last

    def snapshot(self) -> dict[str, Any]:
        """给总览台读的守护状态（**不触发任何动作**，也不碰进程表）。

        ``restarted_total`` 是**窗口内自愈总次数**（各进程 ``restarts`` 之和），不是
        上一拍的重启数：面板上那个数字要回答的是"这台机器今天自愈了几次"，而
        "上一拍重启了几个"在时间轴上几乎恒为 0（重启是稀疏事件）。
        """
        last = self._last
        return {
            "enabled": self.enabled,
            "runnable": self.runnable,
            "self_name": self._self_name,
            "tick_sec": self.tick_sec,
            "manual_pool_after": 0 if self._config is None else self._config.manual_pool_after,
            "last_tick_at": None if last is None else last.at,
            "services": [] if last is None else [item.to_dict() for item in last.services],
            "halted": [] if last is None else list(last.halted),
            "restarted_total": 0 if last is None else sum(item.restarts for item in last.services),
            "disk_gate": (DiskGateOutcome() if last is None else last.disk_gate).to_dict(),
            "manual_pool": self.manual_pool_tasks(),
            "detail": None if self._config is not None else "config/pools.yaml 读不到 ⇒ 守护停用",
        }

    def manual_pool_tasks(self) -> list[dict[str, Any]]:
        """人工池里的任务（**T4.11 验收明文要求"可见"**）。

        为什么读在守护服务里而不是前端拼：`manual_pool` 是**守护清扫的产物**，
        让它自己报"我清扫出了哪几条"是唯一不会两处口径分叉的做法（前端按
        `tasks.status='manual_pool'` 再查一次，就会多出一份没人维护的 SQL）。

        读失败 ⇒ 空列表 + 一条 warning：面板少一块，总好过整页 500。
        """
        try:
            rows = (
                self._connection_factory()
                .execute(
                    "SELECT id, title, status, attempt_count, retry_from, error_code,"
                    "       stage_detail, updated_at"
                    "  FROM tasks WHERE status = 'manual_pool'"
                    " ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (MANUAL_POOL_ROWS,),
                )
                .fetchall()
            )
        except sqlite3.Error as exc:
            logger.warning("watchdog.manual_pool_read_failed", error=str(exc))
            return []
        return [
            {
                "task_id": str(row["id"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "attempt_count": int(row["attempt_count"]),
                "retry_from": None if row["retry_from"] is None else str(row["retry_from"]),
                "error_code": None if row["error_code"] is None else str(row["error_code"]),
                "stage_detail": None if row["stage_detail"] is None else str(row["stage_detail"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def manual_tick(
        self,
        *,
        reason: str | None = None,
        actor: str = "user",
        actor_ref: str | None = None,
        source: str = "webui",
    ) -> ManualTick:
        """人工触发一轮（WebUI 的「立即自检」），并留一条 `audit_ops`。

        为什么值得有这条路径：无人值守的前提是"平时不用管"，而"不用管"一旦出问题，
        人要能**立刻验证守护还在干活** —— 否则只能等下一个拍子，或者去翻日志。
        它跑的是**同一个** `tick()`：多一条旁路就等于多一套没人测过的行为。

        留痕照写：这是"人按了按钮"的动作，哪怕守护当前停用（`enabled=False`）也写
        —— 审计要回答的是"谁在什么时候动过这台机器"，不是"这个动作有没有效果"。
        """
        tick = self.tick()
        audit_id = self._record_manual_tick(
            tick, reason=reason, actor=actor, actor_ref=actor_ref, source=source
        )
        note = _manual_note(tick)
        self._emit(
            level="info",
            message=note,
            payload={
                "code": MANUAL_TICK_CODE,
                "audit_id": audit_id,
                "restarted": list(tick.restarted),
                "halted": list(tick.halted),
                "recovered": list(tick.recovered),
            },
        )
        logger.info("watchdog.manual_tick", restarted=list(tick.restarted), halted=list(tick.halted))
        return ManualTick(tick=tick, audit_id=audit_id, note=note)

    def _record_manual_tick(
        self,
        tick: WatchdogTick,
        *,
        reason: str | None,
        actor: str,
        actor_ref: str | None,
        source: str,
    ) -> int | None:
        """写一条留痕。**写不进去也不能把按钮变成 500**，但绝不静默（记 error 日志）。"""
        try:
            row = AuditRepo(self._connection_factory()).record(
                actor=actor,
                actor_ref=actor_ref,
                action="watchdog.tick",
                target_type="watchdog",
                before={"enabled": tick.enabled},
                after={
                    "restarted": list(tick.restarted),
                    "halted": list(tick.halted),
                    "recovered": list(tick.recovered),
                    "disk_gate": tick.disk_gate.to_dict(),
                    "sweep": tick.sweep.to_dict(),
                },
                reason=reason or "WebUI 人工触发一轮守护自检",
                source=source,
            )
        except (StudioError, sqlite3.Error, ValueError) as exc:
            logger.exception("watchdog.manual_tick_audit_failed", error=str(exc))
            return None
        return row.id

    # ── 一轮 ────────────────────────────────────────────────────────────

    def tick(self, *, now: datetime | None = None) -> WatchdogTick:
        """跑一轮守护（**串行**：并发的第二路会等第一路跑完）。

        **单项失败不影响其余三项**（各自的异常在内部消化）。
        """
        with self._lock:
            return self._tick(now=now)

    def _tick(self, *, now: datetime | None = None) -> WatchdogTick:
        moment = now or self._now()
        if not self.enabled:
            result = WatchdogTick(at=format_iso(moment), enabled=False)
            self._last = result
            return result

        connection = self._connection_factory()
        self._seed(connection, moment)
        errors: list[str] = []
        services, restarted, halted = self._guard_services(moment, errors)
        gate = self._apply_disk_gate(connection, moment, errors)
        recovered = self._recover_concurrency(connection, moment, errors)
        sweep = self._sweep_tasks(connection, moment, errors)
        result = WatchdogTick(
            at=format_iso(moment),
            enabled=True,
            services=services,
            restarted=restarted,
            halted=halted,
            disk_gate=gate,
            recovered=recovered,
            sweep=sweep,
            errors=tuple(errors),
        )
        self._last = result
        return result

    # ── ① 进程守护 ──────────────────────────────────────────────────────

    def _guard_services(
        self, moment: datetime, errors: list[str]
    ) -> tuple[tuple[ServiceGuard, ...], tuple[str, ...], tuple[str, ...]]:
        config = self._config
        assert config is not None  # tick() 已判过 enabled
        statuses = {item.name: item for item in self._safe_status(errors)}
        guards: list[ServiceGuard] = []
        restarted: list[str] = []
        halted: list[str] = []
        for name in config.guard_services:
            state = self._roll_window(name, self._states.setdefault(name, _GuardState()), moment)
            status = statuses.get(name)
            if name == self._self_name:
                guards.append(
                    ServiceGuard(
                        name=name,
                        guarded=False,
                        running=bool(status is not None and status.alive),
                        pid=None if status is None else status.pid,
                        restarts=state.restarts,
                        halted=state.halted,
                        next_restart_at=state.next_restart_at,
                        detail="守护者守不了自己所在的进程；它的守护者是 ops/start_all.ps1",
                    )
                )
                continue
            if status is None:
                guards.append(
                    ServiceGuard(
                        name=name,
                        guarded=False,
                        running=False,
                        detail=f"不是已知进程（合法：{sorted(statuses)}）",
                    )
                )
                continue
            if status.alive:
                guards.append(
                    ServiceGuard(
                        name=name,
                        guarded=True,
                        running=True,
                        pid=status.pid,
                        restarts=state.restarts,
                        halted=state.halted,
                        next_restart_at=state.next_restart_at,
                    )
                )
                continue
            guard, verdict = self._revive(name, state, moment, errors)
            guards.append(guard)
            if verdict == "restarted":
                restarted.append(name)
            elif verdict == "halted":
                halted.append(name)
        return tuple(guards), tuple(restarted), tuple(halted)

    def _safe_status(self, errors: list[str]) -> tuple[ServiceStatus, ...]:
        """读一次进程表；**读失败不掀翻整轮**（其余三项照跑）。"""
        try:
            return self._manager.status()
        except (StudioError, OSError) as exc:
            logger.warning("watchdog.status_failed", error=str(exc))
            errors.append(f"进程表读取失败：{exc}")
            return ()

    def _roll_window(self, name: str, state: _GuardState, moment: datetime) -> _GuardState:
        """滑动窗口：窗口外的重启次数不再算数（"十分钟前崩过"不该压死现在）。"""
        config = self._config
        assert config is not None
        if state.window_started_at is None:
            return state
        elapsed = (moment - parse_iso(state.window_started_at)).total_seconds()
        if elapsed <= config.restart_window_sec:
            return state
        fresh = _GuardState()
        self._states[name] = fresh
        return fresh

    def _revive(
        self, name: str, state: _GuardState, moment: datetime, errors: list[str]
    ) -> tuple[ServiceGuard, str]:
        """重启一个已死的进程。

        :return: ``(结论, 'restarted' | 'halted' | 'waiting' | 'degraded' | 'failed')``
        """
        config = self._config
        assert config is not None
        if state.halted:
            return (
                ServiceGuard(
                    name=name,
                    guarded=True,
                    running=False,
                    restarts=state.restarts,
                    halted=True,
                    detail="重启次数超限，已停手（修好后重启守护，或手工拉起该进程）",
                ),
                "halted",
            )
        if state.next_restart_at is not None and moment < parse_iso(state.next_restart_at):
            return (
                ServiceGuard(
                    name=name,
                    guarded=True,
                    running=False,
                    restarts=state.restarts,
                    next_restart_at=state.next_restart_at,
                    detail=f"退避中（{state.next_restart_at} 之后重试）",
                ),
                "waiting",
            )

        readiness = self._readiness(name)
        if readiness is not None and not readiness.ready:
            # **不静默起一个空转 worker**（裁定 103）：未就绪 ⇒ 记在报告里，不重启。
            return (
                ServiceGuard(
                    name=name,
                    guarded=True,
                    running=False,
                    restarts=state.restarts,
                    detail=f"未就绪（{readiness.readiness}）：{readiness.detail}",
                ),
                "degraded",
            )

        attempt = state.restarts + 1
        if attempt > config.restart_limit:
            self._states[name] = replace(state, halted=True, next_restart_at=None)
            self._halt(name, self._states[name])
            return (
                ServiceGuard(
                    name=name,
                    guarded=True,
                    running=False,
                    restarts=state.restarts,
                    halted=True,
                    detail=f"{config.restart_window_sec}s 内已重启 {state.restarts} 次，停止重启",
                ),
                "halted",
            )

        delay_ms = backoff_delay_ms(
            attempt, base_ms=config.restart_base_ms, max_ms=config.restart_max_ms, seed=name
        )
        window_started = state.window_started_at or format_iso(moment)
        retry_at = format_iso(moment + timedelta(milliseconds=delay_ms))
        report, failure = self._try_start(name, config, errors)
        if report is None:
            self._states[name] = replace(
                state, restarts=attempt, window_started_at=window_started, next_restart_at=retry_at
            )
            return (
                ServiceGuard(
                    name=name,
                    guarded=True,
                    running=False,
                    restarts=attempt,
                    next_restart_at=retry_at,
                    detail=f"重启失败：{failure}",
                ),
                "failed",
            )

        landed = name in report.started or name in report.ready
        next_at = None if landed else retry_at
        self._states[name] = replace(
            state, restarts=attempt, window_started_at=window_started, next_restart_at=next_at
        )
        detail = _restart_detail(report, name)
        self._emit(
            level="warn",
            message=(
                f"{name} 进程已死，第 {attempt} 次重启"
                + ("" if landed else f"（未就绪，退避 {delay_ms}ms 后重试）")
            ),
            payload={
                "code": RESTARTED_CODE,
                "service": name,
                "restarts": attempt,
                "backoff_ms": delay_ms,
                "landed": landed,
                "detail": detail,
            },
        )
        logger.warning("watchdog.restarted", service=name, restarts=attempt, landed=landed)
        return (
            ServiceGuard(
                name=name,
                guarded=True,
                running=landed,
                restarts=attempt,
                next_restart_at=next_at,
                detail=detail,
            ),
            "restarted",
        )

    def _try_start(
        self, name: str, config: WatchdogConfig, errors: list[str]
    ) -> tuple[StartReport | None, str | None]:
        """拉起一个进程（**窄口**：只有这里会真的动进程表）。"""
        try:
            return (
                self._manager.start(
                    only=[name],
                    open_browser=False,
                    doctor_gate=False,
                    ready_timeout_sec=config.restart_ready_timeout_sec,
                ),
                None,
            )
        except (StudioError, OSError) as exc:
            logger.warning("watchdog.restart_failed", service=name, error=str(exc))
            errors.append(f"{name} 重启失败：{exc}")
            return None, str(exc)

    def _readiness(self, name: str) -> ServiceReadiness | None:
        try:
            return self._manager.readiness(self._manager.spec(name))
        except (StudioError, OSError) as exc:
            logger.warning("watchdog.readiness_failed", service=name, error=str(exc))
            return None

    def _halt(self, name: str, state: _GuardState) -> None:
        config = self._config
        assert config is not None
        self._emit(
            level="error",
            message=(
                f"{name} 在 {config.restart_window_sec}s 内已重启 {state.restarts} 次，"
                "已停止自动重启（防重启风暴）"
            ),
            payload={
                "code": RESTART_HALTED_CODE,
                "service": name,
                "restarts": state.restarts,
                "window_sec": config.restart_window_sec,
                "hint": "看 data/logs/<service>.log 定位根因；修好后重启守护（或手工拉起该进程）",
            },
        )
        logger.error("watchdog.halted", service=name, restarts=state.restarts)

    def _seed(self, connection: sqlite3.Connection, moment: datetime) -> None:
        """从 `system_logs` 里把窗口内的重启次数读回来。

        为什么不能只靠内存：守护进程自己重启（改配置、升级、手工重启 API）会让计数
        归零，于是一个 30 秒崩一次的 worker 可以永远重启下去 —— "超限停止"这条防线
        就被一次重启抹掉了。留痕本来就在表里，读回来只是顺手。

        窗口起点取**最早那条留痕的时刻**，不是 `cutoff`：`cutoff` 恰好是一个窗口
        之前，而 `_roll_window` 的判据是 ``elapsed <= window`` —— 差 1 毫秒就会把
        刚读回来的计数当成"过期"清零，于是这道防线又没了（同一个 bug 换了个位置）。
        """
        config = self._config
        if self._seeded or config is None:
            return
        self._seeded = True
        cutoff = format_iso(moment - timedelta(seconds=config.restart_window_sec))
        try:
            rows = connection.execute(
                "SELECT ts, payload_json FROM system_logs"
                " WHERE source = ? AND ts >= ? AND payload_json LIKE ?",
                (LOG_SOURCE, cutoff, f"%{RESTARTED_CODE}%"),
            ).fetchall()
        except sqlite3.Error as exc:  # 表还没建 / 列名对不上 ⇒ 不是致命错误
            logger.warning("watchdog.seed_failed", error=str(exc))
            return
        for row in rows:
            payload = _loads(row["payload_json"])
            name = payload.get("service")
            if not isinstance(name, str) or name not in config.guard_services:
                continue
            state = self._states.setdefault(name, _GuardState())
            state.restarts += 1
            seen = str(row["ts"])
            if state.window_started_at is None or seen < state.window_started_at:
                state.window_started_at = seen
        if self._states:
            logger.info(
                "watchdog.seeded",
                restarts={name: item.restarts for name, item in self._states.items()},
            )

    # ── ② 磁盘水位门禁 ──────────────────────────────────────────────────

    def _apply_disk_gate(
        self, connection: sqlite3.Connection, moment: datetime, errors: list[str]
    ) -> DiskGateOutcome:
        config = self._config
        assert config is not None
        low = self._disk_low()
        if self._settings is None or not self._settings.pause_pools_on_low:
            return DiskGateOutcome(
                low=low,
                enabled=False,
                detail="config/app.yaml 的 disk_gate.pause_pools_on_low = false ⇒ 只告警不暂停",
            )
        if low is None:
            # 还没采过资源 ⇒ **不猜**：拿"没数据"当"水位正常"会把暂停过的池提前放开。
            return DiskGateOutcome(low=None, detail="还没有资源采样 ⇒ 本拍不动门禁")
        store = JobStore(connection)
        service = OverviewService(connection, paths=self._paths, settings=self._settings)
        applied: list[str] = []
        released: list[str] = []
        skipped: list[str] = []
        overridden: list[str] = []
        for pool in config.disk_gate_pools:
            if pool not in POOL_NAMES:  # 配置层已校验，这里是纵深防御
                continue
            try:
                runtime = store.pool_runtime(pool)
            except StudioError as exc:
                errors.append(f"{pool} 池参数读取失败：{exc}")
                continue
            if low:
                if runtime.paused:
                    # 人工暂停**不碰**：水位恢复后也不该把它放开（那是人在等的活）。
                    if runtime.paused_by != AUTO_PAUSE_ACTOR:
                        skipped.append(pool)
                    continue
                if _disk_override(connection, pool):
                    # 人在门禁之后手动放开过它 ⇒ 这一拍不驳回（阈值是机器给的，决定权在人）
                    overridden.append(pool)
                    self._note_override(pool, moment)
                    continue
                service.set_pool_paused(
                    pool=pool,
                    paused=True,
                    actor=AUTO_ACTOR,
                    actor_ref=AUTO_PAUSE_ACTOR,
                    source="auto",
                    reason="磁盘水位低于门禁（自动暂停认领，水位恢复后自动放开）",
                )
                applied.append(pool)
            elif _disk_override(connection, pool):
                # 水位恢复 ⇒ 解除人工覆盖（留痕自清）。不写这一条，覆盖就成了永久豁免：
                # 下一次水位低于门禁时，推导仍会认为"人放开过"，于是永不暂停。
                self._clear_override(connection, pool, moment)
            elif runtime.paused and runtime.paused_by == AUTO_PAUSE_ACTOR:
                service.set_pool_paused(
                    pool=pool,
                    paused=False,
                    actor=AUTO_ACTOR,
                    actor_ref=AUTO_PAUSE_ACTOR,
                    source="auto",
                    reason="磁盘水位已恢复（自动放开）",
                )
                released.append(pool)
        if applied:
            self._emit(
                level="warn",
                message=f"磁盘水位低于门禁：已暂停 {'/'.join(applied)} 池认领（在途单元跑完为止）",
                payload={"code": GATE_APPLIED_CODE, "pools": applied, **_disk_payload(self._metrics)},
            )
        if released:
            self._emit(
                level="info",
                message=f"磁盘水位已恢复：已放开 {'/'.join(released)} 池认领",
                payload={"code": GATE_RELEASED_CODE, "pools": released, **_disk_payload(self._metrics)},
            )
        return DiskGateOutcome(
            low=low,
            enabled=True,
            applied=tuple(applied),
            released=tuple(released),
            skipped=tuple(skipped),
            overridden=tuple(overridden),
        )

    def _disk_low(self) -> bool | None:
        if self._metrics is None:
            return None
        snapshot = self._metrics.last
        return None if snapshot is None else snapshot.disk_low

    def _note_override(self, pool: str, moment: datetime) -> None:
        """覆盖生效时**只报一次**（每 5s 报一次会把日志面板刷成瀑布）。"""
        if pool in self._override_logged:
            return
        self._override_logged.add(pool)
        self._emit(
            level="warn",
            message=f"磁盘水位仍低于门禁，但 {pool} 池已被人手动放开 ⇒ 尊重人工决定，本拍不暂停它",
            payload={"code": OVERRIDDEN_CODE, "pool": pool, "at": format_iso(moment)},
        )
        logger.warning("watchdog.disk_override", pool=pool)

    def _clear_override(self, connection: sqlite3.Connection, pool: str, moment: datetime) -> None:
        """水位恢复 ⇒ 写一条"解除覆盖"的留痕（推导的输入变了，覆盖自然失效）。"""
        try:
            AuditRepo(connection).record(
                actor=AUTO_ACTOR,
                actor_ref=AUTO_PAUSE_ACTOR,
                action="pool.override_cleared",
                target_type="pool",
                target_id=pool,
                before={"override": True, "low": False},
                after={"override": False},
                reason="磁盘水位已恢复 ⇒ 解除人工覆盖（下次低于门禁时门禁重新生效）",
                source="auto",
            )
        except (StudioError, sqlite3.Error, ValueError) as exc:
            logger.warning("watchdog.override_clear_failed", pool=pool, error=str(exc))
            return
        self._override_logged.discard(pool)
        self._emit(
            level="info",
            message=f"磁盘水位已恢复：解除 {pool} 池的人工覆盖（下次低于门禁时重新生效）",
            payload={"code": OVERRIDE_CLEARED_CODE, "pool": pool, "at": format_iso(moment)},
        )

    # ── ③ 显存自动回升 ──────────────────────────────────────────────────

    def _recover_concurrency(
        self, connection: sqlite3.Connection, moment: datetime, errors: list[str]
    ) -> tuple[str, ...]:
        if self._pools is None or self._metrics is None:
            return ()
        auto = self._pools.auto_concurrency
        if not auto.enabled:
            return ()
        snapshot = self._metrics.last
        if snapshot is None or not snapshot.gpu_mem_total_mb:
            return ()  # 没有显存读数 ⇒ 不猜
        ratio = (snapshot.gpu_mem_used_mb or 0) / snapshot.gpu_mem_total_mb
        if ratio >= auto.gpu_mem_high_ratio:
            return ()
        store = JobStore(connection)
        recovered: list[str] = []
        for pool in POOL_NAMES:
            try:
                runtime = store.pool_runtime(pool)
            except StudioError as exc:
                errors.append(f"{pool} 池参数读取失败：{exc}")
                continue
            if runtime.paused or runtime.consecutive_oom:
                continue
            ceiling = min(self._pools.pools[pool].concurrency, concurrency_bounds(pool)[1])
            if runtime.concurrency >= ceiling:
                continue
            anchor = self._concurrency_anchor(connection, pool)
            if anchor is None:
                continue  # 最近一次并发变更不是自动降级 ⇒ 那是人定的，不动
            if (moment - anchor).total_seconds() < auto.recover_after_min * 60:
                continue
            try:
                store.set_concurrency(
                    pool=pool,
                    concurrency=runtime.concurrency + 1,
                    actor=AUTO_ACTOR,
                    actor_ref="auto:watchdog",
                    source="auto",
                    action="pool.autorecover",
                    reason=(
                        f"显存占用 {ratio:.0%} < {auto.gpu_mem_high_ratio:.0%} 且已 "
                        f"{auto.recover_after_min} 分钟无 OOM，自动回升并发"
                    ),
                )
            except StudioError as exc:
                errors.append(f"{pool} 并发回升失败：{exc}")
                continue
            recovered.append(pool)
            self._emit(
                level="info",
                message=(
                    f"{pool} 池并发 {runtime.concurrency} → {runtime.concurrency + 1}"
                    f"（显存 {ratio:.0%}，{auto.recover_after_min} 分钟无 OOM）"
                ),
                payload={
                    "code": AUTORECOVER_CODE,
                    "pool": pool,
                    "from": runtime.concurrency,
                    "to": runtime.concurrency + 1,
                    "gpu_mem_ratio": round(ratio, 4),
                },
            )
        return tuple(recovered)

    def _concurrency_anchor(self, connection: sqlite3.Connection, pool: str) -> datetime | None:
        """最近一次"是谁定的并发"的时刻。

        - 最新一条是 `pool.autodegrade` / `pool.autorecover` ⇒ **可回升**，锚点是它的时刻
          （每次 +1 都要再等一个 `recover_after_min`，否则一拍就能一路涨回顶）；
        - 最新一条是 `pool.set_concurrency` ⇒ **人**刚定过，返回 `None`（不动）。
        """
        try:
            row = connection.execute(
                "SELECT action, at FROM audit_ops"
                " WHERE target_type = 'pool' AND target_id = ? AND action IN (?, ?, ?)"
                " ORDER BY at DESC, id DESC LIMIT 1",
                (pool, *_CONCURRENCY_ACTIONS),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("watchdog.anchor_failed", pool=pool, error=str(exc))
            return None
        if row is None or str(row["action"]) not in ("pool.autodegrade", "pool.autorecover"):
            return None
        return parse_iso(str(row["at"]))

    # ── ④ 任务收尾清扫 ──────────────────────────────────────────────────

    def _sweep_tasks(self, connection: sqlite3.Connection, moment: datetime, errors: list[str]) -> TaskSweep:
        del moment  # 状态迁移自带时间戳；这里只为签名一致
        return TaskSweep(
            failed=self._fail_tasks_of_dead_jobs(connection, errors),
            manual_pool=self._move_to_manual_pool(connection, errors),
        )

    def _fail_tasks_of_dead_jobs(self, connection: sqlite3.Connection, errors: list[str]) -> tuple[str, ...]:
        """单元死信 ⇒ 任务 `failed`（§03.4.3「任务级失败」）。

        只处理"还活着"的任务：已经 `failed` / `manual_pool` / `completed` 的任务再失败
        一次只会多写一条无意义的事件。
        """
        placeholders = ", ".join("?" for _ in _TERMINAL_STATUSES)
        try:
            rows = connection.execute(
                "SELECT j.task_id AS task_id, j.pool AS pool, j.unit_type AS unit_type,"
                "       j.error_code AS error_code, j.error_message AS error_message"
                "  FROM jobs j JOIN tasks t ON t.id = j.task_id"
                " WHERE j.status = 'dead' AND j.task_id IS NOT NULL"
                f"   AND t.status NOT IN ({placeholders})"
                " ORDER BY j.finished_at ASC, j.id ASC",
                _TERMINAL_STATUSES,
            ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"死信扫描失败：{exc}")
            return ()
        service = TaskService(connection)
        done: list[str] = []
        for row in rows:
            task_id = str(row["task_id"])
            if task_id in done:
                continue
            code = str(row["error_code"] or "JOB_DEAD")
            try:
                service.transition(
                    task_id,
                    TaskStatus.FAILED,
                    actor="auto:watchdog",
                    reason=f"{row['pool']}/{row['unit_type']} 单元进入死信（{code}）",
                    error_code=code,
                    error_message=str(row["error_message"] or "单元进入死信"),
                )
            except StudioError as exc:
                # 状态机不允许（比如任务已经跑到别处了）⇒ 如实记一条，不硬来
                logger.info("watchdog.task_fail_skipped", task_id=task_id, error=str(exc))
                continue
            done.append(task_id)
        if done:
            logger.warning("watchdog.tasks_failed", tasks=done)
        return tuple(done)

    def _move_to_manual_pool(self, connection: sqlite3.Connection, errors: list[str]) -> tuple[str, ...]:
        """`attempt_count ≥ N` 的失败任务 ⇒ `manual_pool`（§1.7「反复失败进入人工池」）。"""
        config = self._config
        assert config is not None
        try:
            rows = connection.execute(
                "SELECT id, attempt_count, error_code, error_message FROM tasks"
                " WHERE status = 'failed' AND attempt_count >= ?"
                " ORDER BY attempt_count DESC, id ASC",
                (config.manual_pool_after,),
            ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"人工池扫描失败：{exc}")
            return ()
        service = TaskService(connection)
        moved: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            attempts = int(row["attempt_count"])
            # 人工池是**错误态**（`_ERROR_STATUSES`）：进来的人第一眼要看到"为什么"，
            # 所以把失败态上的错误码原样带过去（不清零 —— 清零那条规矩管的是"离开
            # 失败态重新跑"，不是"停在这里等人"）。
            code = None if row["error_code"] is None else str(row["error_code"])
            message = None if row["error_message"] is None else str(row["error_message"])
            try:
                service.transition(
                    task_id,
                    TaskStatus.MANUAL_POOL,
                    actor="auto:watchdog",
                    reason=f"连续失败 {attempts} 次（阈值 {config.manual_pool_after}），转人工处置",
                    error_code=code,
                    error_message=message,
                )
            except StudioError as exc:
                logger.info("watchdog.manual_pool_skipped", task_id=task_id, error=str(exc))
                continue
            moved.append(task_id)
        if moved:
            logger.warning("watchdog.manual_pool", tasks=moved)
        return tuple(moved)

    # ── 出口 ────────────────────────────────────────────────────────────

    def _emit(self, *, level: str, message: str, payload: Mapping[str, Any]) -> None:
        """落一行 `source='watchdog'` 的日志（"先落库再广播"由 `LogService` 保证）。"""
        self._log.append(
            level=level,  # type: ignore[arg-type]
            source=LOG_SOURCE,
            message=message,
            payload=dict(payload),
        )


def _disk_override(connection: sqlite3.Connection, pool: str) -> bool:
    """人是否在门禁暂停之后**显式放开**过这个池。

    判据是 `audit_ops` 里相邻的两条留痕：``[自动暂停, 人工恢复]``。为什么必须是
    "相邻"：

    - 只看"最近一条是人工恢复"会把**任何一次**手动恢复都当成覆盖 —— 而人因为别的原因
      恢复一个池（比如它本来被人工暂停着）不该顺手豁免掉磁盘门禁；
    - 判据落在留痕上（而不是内存里），API 进程重启后覆盖依然成立 —— 人在面板上按下的
      决定，不该被一次重启抹掉（与重启计数 `_seed` 同一条理由）。

    判据**可自清**：水位恢复时 `_clear_override` 写一条 `pool.override_cleared`，
    下一次推导就不再成立 —— 覆盖是"这一轮水位期间的决定"，不是永久豁免。
    """
    try:
        rows = connection.execute(
            "SELECT action, actor, actor_ref FROM audit_ops"
            " WHERE target_type = 'pool' AND target_id = ?"
            "   AND action IN ('pool.pause', 'pool.resume', 'pool.override_cleared')"
            " ORDER BY at DESC, id DESC LIMIT 2",
            (pool,),
        ).fetchall()
    except sqlite3.Error as exc:  # 表还没建 / 列名对不上 ⇒ 当作"没有覆盖"
        logger.warning("watchdog.override_read_failed", pool=pool, error=str(exc))
        return False
    if len(rows) < 2:
        return False
    latest, previous = rows[0], rows[1]
    return (
        str(latest["action"]) == "pool.resume"
        and str(latest["actor"]) != AUTO_ACTOR
        and str(previous["action"]) == "pool.pause"
        and str(previous["actor_ref"]) == AUTO_PAUSE_ACTOR
    )


def _manual_note(tick: WatchdogTick) -> str:
    """人工触发那一句「做了什么」（面板直接显示，不让人自己去拼数字）。"""
    if not tick.enabled:
        return "守护已停用（config/pools.yaml 读不到），本轮没有做任何事"
    parts = [
        f"重启 {len(tick.restarted)} 个进程",
        f"停手 {len(tick.halted)} 个",
        f"恢复并发 {len(tick.recovered)} 个池",
    ]
    if tick.disk_gate.applied:
        parts.append(f"磁盘门禁暂停 {'/'.join(tick.disk_gate.applied)}")
    if tick.disk_gate.released:
        parts.append(f"磁盘门禁恢复 {'/'.join(tick.disk_gate.released)}")
    if tick.disk_gate.overridden:
        parts.append(f"人工覆盖 {'/'.join(tick.disk_gate.overridden)}")
    if tick.sweep.failed or tick.sweep.manual_pool:
        parts.append(f"清扫 {len(tick.sweep.failed)} 个失败 / {len(tick.sweep.manual_pool)} 个转人工")
    return "守护自检完成：" + "，".join(parts)


def _restart_detail(report: StartReport, name: str) -> str | None:
    """从 `StartReport` 里挑出"这次重启到底成没成"的那一句话。"""
    if name in report.ready:
        return "已就绪"
    for bucket, label in (
        (report.degraded, "降级"),
        (report.port_busy, "端口被占"),
        (report.failed, "启动失败"),
    ):
        if name in bucket:
            return label
    return None


def _disk_payload(metrics: MetricsService | None) -> dict[str, Any]:
    if metrics is None or metrics.last is None:
        return {}
    snapshot = metrics.last
    return {
        "disk_free_gb": snapshot.disk_free_d_gb,
        "disk_free_d_min_gb": snapshot.disk_free_d_min_gb,
        "disk_drive": snapshot.disk_drive,
    }


def _loads(raw: Any) -> Mapping[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}
