# ruff: noqa: N818  -- 异常名沿用契约用词（UnitAborted / UnitTimeout），不带 Error 后缀
"""四池 worker 通用框架（T1.6 · §04.5.1 / §03.4）。

六条设计要点
------------
1. **认领循环**：``claim → run → ack``，空池按 ``lease.poll_delay_ms`` 退避（**禁 busy-loop**）。
2. **停止语义 = draining**：``request_stop()`` 只置标志，**当前单元必须跑完**。
   循环只在"单元之间"检查标志 ⇒ 不会出现"做到一半被丢下"（P2 断点续传的前提）。
3. **脉冲线程**：一个后台线程干两件事 —— 每 5s 报心跳（进程存活）+ 每 ``lease/3`` 续 job 租约。
   周期不同但共用一条 tick：少一个线程、少一个连接、少一处"忘了续租"。
4. **租约丢失 ⇒ 丢弃产物**：``renew()`` 返回 ``False`` 说明租约已被 sweeper 回收或被抢走，
   worker **不得** ``succeed()``（否则会用过期产物覆盖别人的成果）。
5. **硬杀保护**：产物先写 ``*.partial`` 再 :func:`commit_partial` 原子改名（陷阱 #9）。
6. **单元超时是协作式的**：``pools.yaml: unit_timeout_sec`` 由脉冲线程置 ``timed_out``，
   handler 可 ``ctx.check_alive()`` 主动退出；跑完才发现超时 ⇒ 按 ``UNIT_TIMEOUT`` 失败重排。
   "硬杀进程"是 supervisor 的事（T4.11），不是本层的职责。

为什么 handler 拿不到 ``JobStore``
----------------------------------
:class:`UnitContext` 刻意**不**暴露队列写入口：handler 只能"产出结果 + 抛异常"，
收尾（succeed / fail / 退避 / 死信 / 告警）一律由 :class:`PoolWorker` 统一负责。
否则每个池都会长出一份自己的重试逻辑，重试语义必然漂移。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from studio.core.clock import now_iso, utc_now
from studio.core.config import AutoConcurrencyConfig, PoolConfig
from studio.core.errors import ErrorCode, StudioError, WorkerError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import Job, JobStore, PoolRuntime, connect
from studio.db.lease import POLL_BACKOFF_MAX_MS, poll_delay_ms
from studio.pools.heartbeat import (
    HEARTBEAT_INTERVAL_SEC,
    HeartbeatStore,
    ProcessStats,
    RssLeakWatch,
    WorkerIdentity,
)

__all__ = [
    "NON_RETRYABLE_CODES",
    "PoolWorker",
    "UnitAborted",
    "UnitContext",
    "UnitHandler",
    "UnitTimeout",
    "WorkerRunReport",
    "commit_partial",
]

logger = get_logger("studio.pools.worker")

#: 单元收尾结果（内部用字符串，避免为三个值引入一个枚举）
_SUCCEEDED: Final[str] = "succeeded"
_FAILED: Final[str] = "failed"
_ABORTED: Final[str] = "aborted"

#: 这些错误码**重试不会变好**（缺字体、缺水印、配置写错……）⇒ 直接死信 + 告警，
#: 而不是耗完 ``max_attempts`` 再死信 —— 早一分钟报警就早一分钟能修。
NON_RETRYABLE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.CONFIG_INVALID,
        ErrorCode.CONFIG_MISSING,
        ErrorCode.CONFIG_PERSONA_INCOMPLETE,
        ErrorCode.CONFIG_UNKNOWN_KEY,
        ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
        ErrorCode.PATH_MISSING,
        ErrorCode.FFMPEG_NOT_FOUND,
        ErrorCode.FFMPEG_FILTER_MISSING,
        ErrorCode.FONT_MISSING,
        ErrorCode.RENDER_WATERMARK_MISSING,
        ErrorCode.RENDER_BROLL_MISSING,
        ErrorCode.DB_SCHEMA_DRIFT,
        ErrorCode.PUBLISH_DISABLED,
    }
)


# ── 异常 ────────────────────────────────────────────────────────────────


class UnitAborted(StudioError):
    """单元被中止（租约丢失 / 超时）—— handler 应尽快返回，**不要**写产物。"""

    default_code = ErrorCode.JOB_LEASE_LOST


class UnitTimeout(UnitAborted):
    """单元超过 ``pools.yaml: unit_timeout_sec``（协作式超时）。"""

    default_code = ErrorCode.UNIT_TIMEOUT


# ── 产物：先 .partial 再原子改名 ────────────────────────────────────────


def commit_partial(partial: Path, final: Path) -> Path:
    """``*.partial`` → 正式产物的**原子**改名（陷阱 #9：防"假完成"）。

    为什么必须这样：直接写目标文件时，进程在写到一半被硬杀，留下一个**文件名正确、
    内容残缺**的产物；下游只看"文件在不在"就会当成成品继续用。先写 ``.partial``
    再 :meth:`Path.replace`（同一卷上的原子改名），崩溃时最坏是留下一个 ``.partial``，
    永远不会出现"半成品冒充成品"。
    """
    if not partial.is_file():
        raise StudioError(
            f"临时产物不存在，无法提交：{partial}",
            code=ErrorCode.INTERNAL,
            context={"partial": str(partial), "final": str(final)},
            remediation="handler 必须先完整写出 .partial 文件再调用 ctx.commit()",
        )
    with partial.open("rb+") as handle:  # 刷盘：让内容真正落到设备，而不只是 OS 页缓存
        handle.flush()
        os.fsync(handle.fileno())
    final.parent.mkdir(parents=True, exist_ok=True)
    partial.replace(final)
    return final


# ── handler 契约 ────────────────────────────────────────────────────────


class UnitHandler(Protocol):
    """池单元处理器（每池一个实现，T4.11 / T2.x / T3.x / T5.x 落地）。

    异常语义（由 :class:`PoolWorker` 翻译成队列动作）：

    - 正常返回 ``None`` / dict ⇒ ``succeed(result=...)``
    - :class:`UnitAborted` ⇒ **不收尾**（租约没了，让别人重做）
    - :class:`StudioError` ⇒ ``fail``；错误码在 :data:`NON_RETRYABLE_CODES` ⇒ 直接死信
    - 其它异常 ⇒ ``fail(INTERNAL)`` 且可重试（退避重排，绝不让异常穿出循环）
    """

    unit_types: frozenset[str]

    def run(self, ctx: UnitContext) -> Mapping[str, Any] | None: ...


# ── 执行上下文 ──────────────────────────────────────────────────────────


class UnitContext:
    """一个已认领单元的执行上下文（handler 的**唯一**输入面）。"""

    def __init__(
        self,
        *,
        job: Job,
        pool: str,
        worker_id: str,
        paths: StudioPaths,
        timeout_sec: int,
        pulse: _Pulse,
        renew: Callable[[], bool],
    ) -> None:
        self._job = job
        self._pool = pool
        self._worker_id = worker_id
        self._paths = paths
        self._timeout_sec = timeout_sec
        self._pulse = pulse
        self._renew = renew
        self._started_at = utc_now()

    # ── 身份 ────────────────────────────────────────────────────────────
    @property
    def job(self) -> Job:
        return self._job

    @property
    def job_id(self) -> str:
        return self._job.id

    @property
    def task_id(self) -> str:
        return self._job.task_id

    @property
    def pool(self) -> str:
        return self._pool

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def unit_type(self) -> str:
        return self._job.unit_type

    @property
    def unit_ref(self) -> str:
        return self._job.unit_ref

    @property
    def attempt(self) -> int:
        """本次是第几次尝试（``jobs.attempts``，认领时已 +1）。"""
        return self._job.attempts

    @property
    def payload(self) -> Mapping[str, Any]:
        return self._job.payload

    @property
    def paths(self) -> StudioPaths:
        return self._paths

    # ── 时间与存活 ──────────────────────────────────────────────────────
    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def deadline(self) -> datetime:
        return self._started_at + timedelta(seconds=self._timeout_sec)

    @property
    def remaining_sec(self) -> float:
        return max(0.0, (self.deadline - utc_now()).total_seconds())

    @property
    def lease_lost(self) -> bool:
        return self._pulse.lease_lost

    @property
    def timed_out(self) -> bool:
        return self._pulse.timed_out

    def renew(self) -> bool:
        """主动续租（长单元在关键节点调用）；``False`` ⇒ 租约已丢，立即收工。"""
        return self._renew()

    def check_alive(self) -> None:
        """长循环里主动检查：租约丢了 ⇒ :class:`UnitAborted`；超时 ⇒ :class:`UnitTimeout`。"""
        if self._pulse.lease_lost:
            raise UnitAborted(
                f"租约已丢失（job {self.job_id}），放弃本单元并丢弃产物",
                context={"job_id": self.job_id, "worker_id": self._worker_id},
                remediation="无需处理：sweeper 会回收租约并重排，本单元会被重做",
            )
        if self._pulse.timed_out:
            raise UnitTimeout(
                f"单元超时（>{self._timeout_sec}s，job {self.job_id}）",
                context={"job_id": self.job_id, "timeout_sec": self._timeout_sec},
                remediation="提高 pools.yaml 的 unit_timeout_sec，或把单元切得更小",
            )

    # ── 产物 ────────────────────────────────────────────────────────────
    def work_dir(self) -> Path:
        """任务工作目录 ``data/work/<task_id>/``（幂等创建）。"""
        directory = self._paths.work_dir_for(self.task_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def partial_path(self, name: str) -> Path:
        """临时产物路径（``<name>.partial``）—— 写完必须走 :meth:`commit`。"""
        return self.work_dir() / f"{name}.partial"

    @staticmethod
    def commit(partial: Path, final: Path) -> Path:
        """原子提交产物（见 :func:`commit_partial`）。"""
        return commit_partial(partial, final)


# ── 脉冲线程：心跳 + 续租 + 超时判定 ────────────────────────────────────


class _Pulse:
    """心跳 + 续租的后台脉冲（一个线程干四件事：报活、续租、判超时、看关停标志）。

    为什么用**线程**而不是 asyncio：``db`` 层全同步（T1.5 施工裁定 27），
    单元处理器也是同步的（跑 ffmpeg / 阻塞 HTTP）。为了续租引入事件循环，
    会把"同步 worker"这条最简单的路径复杂化。线程 + 每线程独立连接足够。

    关停标志（T1.12 · 裁定 102）
    ---------------------------
    ``stop_flag`` 是一个**文件路径**：文件出现 ⇒ 请求优雅退出。用文件而不是信号，
    因为 ``GenerateConsoleCtrlEvent`` 只能作用于**同控制台**的进程组 ——
    ``停止.bat`` 是另一个控制台，从那里发的 Ctrl-Break 会**静默失效**
    （返回 TRUE 但目标收不到），最后只能 ``terminate()`` 硬杀 ⇒ 丢进度。
    标志文件与"谁在哪个控制台发命令"无关，从 WebUI 也照样能用。
    """

    #: 默认 tick（秒）—— 心跳 5s、续租 lease/3，1s 的粒度都够
    DEFAULT_TICK_SEC: Final[float] = 1.0

    def __init__(
        self,
        *,
        identity: WorkerIdentity,
        connection_factory: Callable[[], sqlite3.Connection],
        version: str | None,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
        tick_sec: float = 1.0,
        stop_flag: Path | None = None,
        on_stop_requested: Callable[[], None] | None = None,
        auto_concurrency: AutoConcurrencyConfig | None = None,
    ) -> None:
        self._identity = identity
        self._connection_factory = connection_factory
        self._version = version
        self._beat_interval = heartbeat_interval_sec
        self._tick_sec = tick_sec
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flight: tuple[str, int] | None = None  # (job_id, lease_sec)
        self._deadline: datetime | None = None
        self._status = "idle"
        #: 自动降并发参数（T4.10）：脉冲线程也要能读它 —— `JobStore` 在
        #: `fail()` 里靠它判断「连续 N 次 TTS_OOM ⇒ 降并发」，而 pulse 线程
        #: 与认领循环各自建 `JobStore`（裁定 149）。
        self._auto_concurrency = auto_concurrency or AutoConcurrencyConfig()
        self._lease_lost = False
        self._timed_out = False
        self._rss = RssLeakWatch()
        self._started_at = now_iso()
        self._last_beat: datetime | None = None
        self._last_renew: datetime | None = None
        self._stop_flag = stop_flag
        self._on_stop_requested = on_stop_requested
        self._stop_flag_fired = False

    # ── 生命周期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._loop,
            name=f"pulse:{self._identity.worker_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("worker.pulse_stop_timeout", worker_id=self._identity.worker_id)

    # ── 共享状态（读写都在锁内，跨线程安全）─────────────────────────────
    @property
    def lease_lost(self) -> bool:
        with self._lock:
            return self._lease_lost

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def current_job_id(self) -> str | None:
        with self._lock:
            flight = self._flight
        return None if flight is None else flight[0]

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def mark_lease_lost(self) -> None:
        with self._lock:
            self._lease_lost = True

    def begin_unit(self, *, job_id: str, lease_sec: int, timeout_sec: int) -> None:
        with self._lock:
            self._flight = (job_id, lease_sec)
            self._deadline = utc_now() + timedelta(seconds=timeout_sec)
            self._status = "busy"
            self._lease_lost = False
            self._timed_out = False
        self._last_renew = None  # 下一拍立刻续租（宁可早，不可晚）

    def end_unit(self) -> None:
        with self._lock:
            self._flight = None
            self._deadline = None
            self._status = "idle"
        self._last_renew = None

    # ── 线程主体 ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        connection: sqlite3.Connection | None = None
        beats: HeartbeatStore | None = None
        try:
            connection = self._connection_factory()
            store = JobStore(connection, auto_concurrency=self._auto_concurrency)
            beats = HeartbeatStore(connection)
            while True:
                self._check_stop_flag()
                try:
                    self._tick(store, beats)
                except Exception as exc:  # 脉冲线程绝不能死：死了就"看着还活着但不再续租"
                    logger.warning(
                        "worker.pulse_tick_failed",
                        worker_id=self._identity.worker_id,
                        error=str(exc),
                    )
                if self._stop.wait(self._tick_sec):
                    break
        except Exception:
            logger.exception("worker.pulse_died", worker_id=self._identity.worker_id)
        finally:
            if beats is not None:
                try:
                    beats.forget(self._identity.worker_id)
                except Exception as exc:
                    logger.warning(
                        "worker.pulse_forget_failed",
                        worker_id=self._identity.worker_id,
                        error=str(exc),
                    )
            if connection is not None:
                connection.close()

    def _check_stop_flag(self) -> None:
        """关停标志出现 ⇒ 通知一次（幂等；**异常一律吞掉**）。

        读标志是"额外保险"，不能让一个权限错误把脉冲线程带走 ——
        脉冲线程死了比不响应关停更危险（看着还活着但不再续租）。
        """
        if self._stop_flag is None or self._stop_flag_fired:
            return
        try:
            present = self._stop_flag.is_file()
        except OSError as exc:
            logger.warning(
                "worker.stop_flag_unreadable",
                worker_id=self._identity.worker_id,
                path=str(self._stop_flag),
                error=str(exc),
            )
            return
        if not present:
            return
        self._stop_flag_fired = True
        logger.info(
            "worker.stop_flag_seen",
            worker_id=self._identity.worker_id,
            path=str(self._stop_flag),
        )
        callback = self._on_stop_requested
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            logger.warning(
                "worker.stop_flag_callback_failed",
                worker_id=self._identity.worker_id,
                error=str(exc),
            )

    def _tick(self, store: JobStore, beats: HeartbeatStore) -> None:
        moment = utc_now()
        with self._lock:
            flight = self._flight
            status = self._status
            deadline = self._deadline
            lost = self._lease_lost

        if flight is not None and deadline is not None and moment >= deadline:
            with self._lock:
                first = not self._timed_out
                self._timed_out = True
            if first:
                logger.warning(
                    "worker.unit_timeout",
                    worker_id=self._identity.worker_id,
                    job_id=flight[0],
                )

        if self._last_beat is None or (moment - self._last_beat).total_seconds() >= self._beat_interval:
            stats = ProcessStats.sample()
            if self._rss.observe(stats.rss_mb):
                logger.warning(
                    "worker.rss_growth",
                    worker_id=self._identity.worker_id,
                    rss_mb=stats.rss_mb,
                    baseline_mb=self._rss.baseline_mb,
                    hint="疑似内存泄漏；跑满当前单元后建议重启该 worker（T4.11）",
                )
            beats.upsert(
                worker_id=self._identity.worker_id,
                pool=self._identity.pool,
                status=status,
                current_job_id=None if flight is None else flight[0],
                pid=self._identity.pid,
                cpu_percent=stats.cpu_percent,
                rss_mb=stats.rss_mb,
                started_at=self._started_at,
                version=self._version,
                now=moment,
            )
            self._last_beat = moment

        if flight is not None and not lost:
            interval = flight[1] / 3.0
            due = self._last_renew is None or (moment - self._last_renew).total_seconds() >= interval
            if due:
                self._last_renew = moment
                if not store.renew(
                    job_id=flight[0],
                    worker_id=self._identity.worker_id,
                    lease_sec=flight[1],
                    now=moment,
                ):
                    with self._lock:
                        self._lease_lost = True
                    logger.warning(
                        "worker.lease_lost",
                        worker_id=self._identity.worker_id,
                        job_id=flight[0],
                        hint="租约已被回收或抢走；本单元产物必须丢弃，不写 succeed",
                    )


# ── 运行报告 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkerRunReport:
    """一次 ``run()`` 的结论（日志 / 测试 / WebUI 共用）。"""

    worker_id: str
    pool: str
    stop_reason: str
    units_done: int
    units_failed: int
    units_aborted: int
    empty_rounds: int
    started_at: str
    finished_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "pool": self.pool,
            "stop_reason": self.stop_reason,
            "units_done": self.units_done,
            "units_failed": self.units_failed,
            "units_aborted": self.units_aborted,
            "empty_rounds": self.empty_rounds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ── worker ──────────────────────────────────────────────────────────────


class PoolWorker:
    """单池 worker：认领 → 执行 → 收尾，直到 ``request_stop()``。"""

    def __init__(
        self,
        *,
        pool: str,
        handler: UnitHandler,
        pool_config: PoolConfig,
        paths: StudioPaths,
        slot: int = 1,
        worker_id: str | None = None,
        connection: sqlite3.Connection | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        version: str | None = None,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
        pulse_tick_sec: float = _Pulse.DEFAULT_TICK_SEC,
        unit_timeout_sec: int | None = None,
        stop_flag: Path | None = None,
        auto_concurrency: AutoConcurrencyConfig | None = None,
    ) -> None:
        """``unit_timeout_sec`` / ``pulse_tick_sec`` 是**注入点**：生产一律取
        ``pools.yaml``（前者）与默认 tick（后者），测试与一次性排障才覆盖。

        ``stop_flag`` 是**文件路径**（不是信号）：文件出现 ⇒ ``request_stop()``。
        生产由 ``workers/run_*.py`` 从 ``STUDIO_STOP_FLAG`` 环境变量读出后传入
        （T1.12 · 裁定 102），不传就是老行为（只认信号）。
        """
        self.pool = pool
        self._handler = handler
        self._config = pool_config
        self._paths = paths
        self._slot = slot
        identity = WorkerIdentity.parse(worker_id) if worker_id else WorkerIdentity.current(pool, slot)
        self._identity = identity
        self.worker_id = identity.worker_id
        self._connection = connection
        self._auto_concurrency = auto_concurrency or AutoConcurrencyConfig()
        self._owns_connection = connection is None
        self._connection_factory = connection_factory or (lambda: connect(paths.db_file))
        self._version = version
        self._unit_timeout_sec = unit_timeout_sec
        self._pulse = _Pulse(
            identity=identity,
            connection_factory=self._connection_factory,
            version=version,
            heartbeat_interval_sec=heartbeat_interval_sec,
            tick_sec=pulse_tick_sec,
            stop_flag=stop_flag,
            on_stop_requested=lambda: self.request_stop(reason="stop_flag"),
            auto_concurrency=self._auto_concurrency,
        )
        self._stop = threading.Event()
        self._draining = False
        self._stop_reason = "draining"
        self._empty_rounds = 0
        self._running = False

    # ── 只读 ────────────────────────────────────────────────────────────
    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def status(self) -> str:
        return self._pulse.status

    @property
    def current_job_id(self) -> str | None:
        return self._pulse.current_job_id

    @property
    def unit_types(self) -> frozenset[str]:
        return self._handler.unit_types

    # ── 停止 ────────────────────────────────────────────────────────────
    def request_stop(self, reason: str = "signal") -> None:
        """请求优雅退出（**draining**）：只置标志 —— 当前单元必须跑完（§04.5.1）。

        在途单元的租约照常续，不会因为"正在退出"就被 sweeper 回收走。
        """
        if self._draining:
            return
        self._draining = True
        self._stop_reason = reason
        self._pulse.set_status("draining")
        self._stop.set()
        logger.info("worker.draining", worker_id=self.worker_id, pool=self.pool, reason=reason)

    # ── 主循环 ──────────────────────────────────────────────────────────
    def run(
        self,
        *,
        max_units: int | None = None,
        max_empty_rounds: int | None = None,
    ) -> WorkerRunReport:
        """跑认领循环直到 ``request_stop()`` / ``max_units`` / ``max_empty_rounds``。

        :param max_units: 跑满这么多个单元就退出（测试与"一次性补跑"用）
        :param max_empty_rounds: 连续这么多轮没认到东西就退出（默认永不退出，只退避）
        """
        if self._running:
            raise WorkerError(
                f"worker {self.worker_id} 已在运行（禁止同一实例并发 run）",
                context={"worker_id": self.worker_id},
            )
        self._running = True
        started = now_iso()
        connection = self._connection if self._connection is not None else connect(self._paths.db_file)
        self._connection = connection
        store = JobStore(connection, auto_concurrency=self._auto_concurrency)
        units_done = units_failed = units_aborted = 0
        stop_reason = "draining"
        self._pulse.start()
        logger.info("worker.started", worker_id=self.worker_id, pool=self.pool, pid=self._identity.pid)
        try:
            while True:
                if self._draining:
                    stop_reason = self._stop_reason
                    break
                runtime = store.pool_runtime(self.pool)
                job = store.claim(
                    pool=self.pool,
                    worker_id=self.worker_id,
                    lease_sec=runtime.lease_sec,
                    now=utc_now(),
                )
                if job is None:
                    self._empty_rounds += 1
                    if max_empty_rounds is not None and self._empty_rounds >= max_empty_rounds:
                        stop_reason = "idle"
                        break
                    if self._stop.wait(self._empty_delay_ms() / 1000.0):
                        stop_reason = self._stop_reason
                        break
                    continue
                self._empty_rounds = 0
                outcome = self._run_unit(store, job, runtime)
                units_done += 1
                if outcome == _FAILED:
                    units_failed += 1
                elif outcome == _ABORTED:
                    units_aborted += 1
                try:
                    store.unlock_dependents(pool=self.pool)
                except StudioError as exc:
                    logger.warning("worker.unlock_failed", worker_id=self.worker_id, error=str(exc))
                if max_units is not None and units_done >= max_units:
                    stop_reason = "max_units"
                    break
        finally:
            self._pulse.stop()
            if self._owns_connection:
                connection.close()
                self._connection = None
            self._running = False

        report = WorkerRunReport(
            worker_id=self.worker_id,
            pool=self.pool,
            stop_reason=stop_reason,
            units_done=units_done,
            units_failed=units_failed,
            units_aborted=units_aborted,
            empty_rounds=self._empty_rounds,
            started_at=started,
            finished_at=now_iso(),
        )
        logger.info("worker.stopped", **report.to_dict())
        return report

    # ── 内部 ────────────────────────────────────────────────────────────
    def _empty_delay_ms(self) -> int:
        """空池退避：以池配置 ``poll_ms`` 为基数做指数退避，封顶 2s（§03.4.2）。

        ``publish`` 池 ``poll_ms=5000`` ⇒ 上限取 ``max(5000, 2000) = 5000``，
        也就是"退避不缩水、也不比配置更激进"。
        """
        base = self._config.poll_ms
        return poll_delay_ms(self._empty_rounds, base_ms=base, max_ms=max(base, POLL_BACKOFF_MAX_MS))

    def _unit_timeout(self) -> int:
        """本单元的超时秒数（注入值优先于 ``pools.yaml``）。"""
        return self._config.unit_timeout_sec if self._unit_timeout_sec is None else self._unit_timeout_sec

    def _renew(self, store: JobStore, job_id: str, lease_sec: int) -> bool:
        ok = store.renew(job_id=job_id, worker_id=self.worker_id, lease_sec=lease_sec, now=utc_now())
        if not ok:
            self._pulse.mark_lease_lost()
        return ok

    def _run_unit(self, store: JobStore, job: Job, runtime: PoolRuntime) -> str:
        pulse = self._pulse
        pulse.begin_unit(job_id=job.id, lease_sec=runtime.lease_sec, timeout_sec=self._unit_timeout())
        ctx = UnitContext(
            job=job,
            pool=self.pool,
            worker_id=self.worker_id,
            paths=self._paths,
            timeout_sec=self._unit_timeout(),
            pulse=pulse,
            renew=lambda: self._renew(store, job.id, runtime.lease_sec),
        )
        logger.info(
            "worker.unit_start",
            worker_id=self.worker_id,
            pool=self.pool,
            job_id=job.id,
            task_id=job.task_id,
            unit_type=job.unit_type,
            unit_ref=job.unit_ref,
            attempt=f"{job.attempts}/{job.max_attempts}",
        )
        started = time.monotonic()
        try:
            result = self._handler.run(ctx)
        except UnitTimeout as exc:
            # handler 自己发现超时 ⇒ 可重试失败（**不是** abort：租约还在自己手上）
            return self._fail(
                store,
                job,
                code=ErrorCode.UNIT_TIMEOUT,
                message=exc.message,
                retryable=True,
            )
        except UnitAborted as exc:
            logger.warning(
                "worker.unit_aborted",
                worker_id=self.worker_id,
                job_id=job.id,
                error=str(exc),
            )
            return _ABORTED
        except StudioError as exc:
            return self._fail(
                store,
                job,
                code=exc.code,
                message=exc.message,
                retryable=exc.code not in NON_RETRYABLE_CODES,
                trace=None,
            )
        except Exception as exc:
            return self._fail(
                store,
                job,
                code=ErrorCode.INTERNAL,
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
                trace=traceback.format_exc(),
            )
        finally:
            pulse.end_unit()

        return self._finalize(store, job, result=result, started=started)

    def _finalize(
        self,
        store: JobStore,
        job: Job,
        *,
        result: Mapping[str, Any] | None,
        started: float,
    ) -> str:
        """handler 正常返回后的收尾：超时 ⇒ 失败重排；租约丢 ⇒ 丢弃产物；否则 succeed。"""
        duration_ms = int((time.monotonic() - started) * 1000)
        if self._pulse.timed_out:
            return self._fail(
                store,
                job,
                code=ErrorCode.UNIT_TIMEOUT,
                message=f"单元超过 {self._unit_timeout()}s 仍未结束（协作式超时）",
                retryable=True,
            )
        if self._pulse.lease_lost:
            logger.warning(
                "worker.result_discarded",
                worker_id=self.worker_id,
                job_id=job.id,
                duration_ms=duration_ms,
                hint="租约已丢，结果不写库（sweeper 会重排该单元）",
            )
            return _ABORTED
        try:
            done = store.succeed(
                job_id=job.id,
                worker_id=self.worker_id,
                result=dict(result or {}),
                now=utc_now(),
            )
        except StudioError as exc:
            logger.warning("worker.succeed_rejected", worker_id=self.worker_id, job_id=job.id, error=str(exc))
            return _ABORTED
        if not done:
            logger.warning("worker.succeed_rejected", worker_id=self.worker_id, job_id=job.id)
            return _ABORTED
        logger.info(
            "worker.unit_done",
            worker_id=self.worker_id,
            pool=self.pool,
            job_id=job.id,
            unit_type=job.unit_type,
            unit_ref=job.unit_ref,
            duration_ms=duration_ms,
        )
        return _SUCCEEDED

    def _fail(
        self,
        store: JobStore,
        job: Job,
        *,
        code: ErrorCode,
        message: str,
        retryable: bool,
        trace: str | None = None,
    ) -> str:
        try:
            outcome = store.fail(
                job_id=job.id,
                worker_id=self.worker_id,
                error_code=str(code),
                error_message=message,
                retryable=retryable,
                error_trace=trace,
                now=utc_now(),
            )
        except StudioError as exc:
            logger.warning("worker.fail_rejected", worker_id=self.worker_id, job_id=job.id, error=str(exc))
            return _ABORTED
        logger.warning(
            "worker.unit_failed",
            worker_id=self.worker_id,
            pool=self.pool,
            job_id=job.id,
            unit_type=job.unit_type,
            unit_ref=job.unit_ref,
            error_code=str(code),
            retryable=retryable,
            status=outcome.status,
            attempts=f"{outcome.attempts}/{outcome.max_attempts}",
        )
        return _FAILED
