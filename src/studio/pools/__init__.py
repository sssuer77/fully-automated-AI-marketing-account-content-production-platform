"""四池 worker 框架（T1.6 · §04.5.1）。"""

from __future__ import annotations

from studio.pools.heartbeat import (
    HEARTBEAT_INTERVAL_SEC,
    HEARTBEAT_TIMEOUT_SEC,
    WORKER_DEAD_CODE,
    WORKER_STATUSES,
    HeartbeatStore,
    ProcessStats,
    RssLeakWatch,
    WorkerIdentity,
)
from studio.pools.runner import (
    HANDLER_MODULES,
    HANDLERS,
    POOL_NAMES,
    build_supervisor,
    build_worker,
    handler_for,
    install_signal_handlers,
    register_handler,
    restore_signal_handlers,
    run_pool,
)
from studio.pools.supervisor import (
    WORKER_RESTART_HALTED_CODE,
    Supervisor,
    SupervisorTick,
    WorkerSlotState,
    WorkerSpec,
)
from studio.pools.worker_base import (
    NON_RETRYABLE_CODES,
    PoolWorker,
    UnitAborted,
    UnitContext,
    UnitHandler,
    UnitTimeout,
    WorkerRunReport,
    commit_partial,
)

__all__ = [
    "HANDLERS",
    "HANDLER_MODULES",
    "HEARTBEAT_INTERVAL_SEC",
    "HEARTBEAT_TIMEOUT_SEC",
    "NON_RETRYABLE_CODES",
    "POOL_NAMES",
    "WORKER_DEAD_CODE",
    "WORKER_RESTART_HALTED_CODE",
    "WORKER_STATUSES",
    "HeartbeatStore",
    "PoolWorker",
    "ProcessStats",
    "RssLeakWatch",
    "Supervisor",
    "SupervisorTick",
    "UnitAborted",
    "UnitContext",
    "UnitHandler",
    "UnitTimeout",
    "WorkerIdentity",
    "WorkerRunReport",
    "WorkerSlotState",
    "WorkerSpec",
    "build_supervisor",
    "build_worker",
    "commit_partial",
    "handler_for",
    "install_signal_handlers",
    "register_handler",
    "restore_signal_handlers",
    "run_pool",
]
