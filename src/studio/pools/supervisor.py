"""worker 进程守护（T1.6 · §04.5.1）。

职责
----
1. 拉起四池 worker 子进程（每池可多 slot）；
2. 每轮 ``poll_once()``：判死超时心跳 → **杀掉僵进程** → 按指数退避重启；
3. 重启次数超限 ⇒ **停止重启并告警**（防"重启风暴"把机器拖垮）。

不在这里做的事（归 T4.11）
--------------------------
磁盘/显存水位门禁、任务级 ``manual_pool``、24h 无人值守验收 —— 那些是策略，
本模块只提供"发现进程死了就把它拉起来"的机制。

``poll_once()`` 为什么不阻塞
---------------------------
每轮都是"读一次心跳 + ``poll()`` 一遍子进程"，没有 ``wait()``：supervisor 自己
也必须能被 Ctrl+C 立刻打断。真正等进程退出的是 :meth:`Supervisor.stop_all`。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from studio.core.clock import format_iso, now_iso, parse_iso, utc_now
from studio.core.errors import WorkerError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.lease import backoff_delay_ms
from studio.pools.heartbeat import (
    HEARTBEAT_TIMEOUT_SEC,
    HeartbeatStore,
    WorkerIdentity,
)

__all__ = [
    "WORKER_RESTART_HALTED_CODE",
    "ProcessHandle",
    "Supervisor",
    "SupervisorTick",
    "WorkerSlotState",
    "WorkerSpec",
]

logger = get_logger("studio.pools.supervisor")

#: 重启次数超限的日志 ``payload_json.code``（同 ``WORKER_DEAD``：非 alert 枚举，走 error 日志）
WORKER_RESTART_HALTED_CODE: Final[str] = "WORKER_RESTART_HALTED"

#: 默认 tick：与心跳周期同量级 —— 判死阈值 15s，5s 一轮 ⇒ 最迟 20s 内发现并重启
DEFAULT_TICK_SEC: Final[float] = 5.0

#: 终止子进程的宽限期（先 terminate，超时才 kill）
DEFAULT_GRACE_SEC: Final[float] = 10.0


class ProcessHandle(Protocol):
    """子进程句柄（``subprocess.Popen`` 结构兼容；测试可注入假实现）。"""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawner = Callable[["WorkerSpec"], ProcessHandle]
ConnectionFactory = Callable[[], sqlite3.Connection]


def _default_spawn(spec: WorkerSpec) -> ProcessHandle:
    """真拉起一个子进程（Windows 上建独立进程组，便于整组终止）。"""
    env = dict(os.environ)
    if spec.env:
        env.update(spec.env)
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        list(spec.argv),
        cwd=None if spec.cwd is None else str(spec.cwd),
        env=env,
        creationflags=creation_flags,
        close_fds=True,
    )


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """一个待守护的 worker 进程。"""

    pool: str
    slot: int
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    restart_base_ms: int = 5_000
    restart_max_ms: int = 300_000
    restart_limit: int = 20

    @property
    def label(self) -> str:
        return f"{self.pool}#{self.slot}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "pool": self.pool,
            "slot": self.slot,
            "argv": list(self.argv),
            "restart_limit": self.restart_limit,
        }


@dataclass(frozen=True, slots=True)
class WorkerSlotState:
    """一个 slot 的当前状态。"""

    spec: WorkerSpec
    pid: int | None = None
    started_at: str | None = None
    restarts: int = 0
    next_restart_at: str | None = None
    last_exit_code: int | None = None
    halted: bool = False

    @property
    def label(self) -> str:
        return self.spec.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "pool": self.spec.pool,
            "slot": self.spec.slot,
            "pid": self.pid,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "next_restart_at": self.next_restart_at,
            "last_exit_code": self.last_exit_code,
            "halted": self.halted,
        }


@dataclass(frozen=True, slots=True)
class SupervisorTick:
    """一轮守护的结论。"""

    at: str
    dead_marked: tuple[str, ...] = ()
    exited: tuple[str, ...] = ()
    restarted: tuple[str, ...] = ()
    halted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "dead_marked": list(self.dead_marked),
            "exited": list(self.exited),
            "restarted": list(self.restarted),
            "halted": list(self.halted),
        }


class Supervisor:
    """四池 worker 的进程守护（崩溃 ⇒ 退避重启 ⇒ 超限停止并告警）。"""

    def __init__(
        self,
        *,
        specs: tuple[WorkerSpec, ...],
        paths: StudioPaths,
        connection_factory: ConnectionFactory | None = None,
        spawn: Spawner | None = None,
        heartbeat_timeout_sec: float = HEARTBEAT_TIMEOUT_SEC,
        grace_sec: float = DEFAULT_GRACE_SEC,
    ) -> None:
        labels = [spec.label for spec in specs]
        if len(set(labels)) != len(labels):
            raise WorkerError(
                f"worker 规格有重复 label：{sorted(labels)}",
                context={"labels": labels},
                remediation="每个 (pool, slot) 只能出现一次",
            )
        self._specs = specs
        self._paths = paths
        self._connection_factory = connection_factory or (lambda: connect(paths.db_file))
        self._spawn = spawn or _default_spawn
        self._heartbeat_timeout_sec = heartbeat_timeout_sec
        self._grace_sec = grace_sec
        self._states: dict[str, WorkerSlotState] = {spec.label: WorkerSlotState(spec=spec) for spec in specs}
        self._handles: dict[str, ProcessHandle] = {}
        self._stopping = False

    # ── 只读 ────────────────────────────────────────────────────────────
    @property
    def states(self) -> tuple[WorkerSlotState, ...]:
        return tuple(self._states[label] for label in sorted(self._states))

    def state(self, label: str) -> WorkerSlotState:
        return self._states[label]

    # ── 拉起 ────────────────────────────────────────────────────────────
    def start_all(self) -> tuple[WorkerSlotState, ...]:
        """拉起全部 slot（已在跑的跳过）。"""
        self._stopping = False
        for spec in self._specs:
            if self._handles.get(spec.label) is not None:
                continue
            self._handles[spec.label] = self._spawn(spec)
            state = self._states[spec.label]
            self._states[spec.label] = replace(state, pid=self._handles[spec.label].pid, started_at=now_iso())
            logger.info(
                "supervisor.started",
                label=spec.label,
                pid=self._handles[spec.label].pid,
                argv=list(spec.argv),
            )
        return self.states

    # ── 守护 ────────────────────────────────────────────────────────────
    def poll_once(self, *, now: datetime | None = None) -> SupervisorTick:
        """一轮巡检：判死超时心跳 ⇒ 杀僵进程 ⇒ 退避重启（幂等，可反复调用）。"""
        moment = now or utc_now()
        connection = self._connection_factory()
        try:
            dead = HeartbeatStore(connection).reap_stale(now=moment, timeout_sec=self._heartbeat_timeout_sec)
        finally:
            connection.close()

        exited: list[str] = []
        restarted: list[str] = []
        halted: list[str] = []
        for label in sorted(self._states):
            state = self._states[label]
            if state.halted:
                continue
            handle = self._handles.get(label)
            exit_code = None if handle is None else handle.poll()
            if exit_code is not None:
                exited.append(label)
                self._states[label] = replace(state, last_exit_code=exit_code, pid=None)

            heartbeat_dead = self._slot_was_reaped(state, dead)
            if exit_code is None and not heartbeat_dead:
                continue

            reason = "heartbeat_timeout" if heartbeat_dead else f"exit_code={exit_code}"
            outcome = self._restart(label, reason=reason, now=moment)
            if outcome is None:
                continue
            if outcome:
                restarted.append(label)
            else:
                halted.append(label)
        tick = SupervisorTick(
            at=format_iso(moment),
            dead_marked=dead,
            exited=tuple(exited),
            restarted=tuple(restarted),
            halted=tuple(halted),
        )
        if restarted or halted or dead:
            logger.info("supervisor.tick", **tick.to_dict())
        return tick

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_ticks: int | None = None,
        tick_sec: float = DEFAULT_TICK_SEC,
    ) -> tuple[SupervisorTick, ...]:
        """守护循环（``stop_event`` 置位即退出；``max_ticks`` 供测试用）。"""
        event = stop_event or threading.Event()
        ticks: list[SupervisorTick] = []
        self.start_all()
        while not event.is_set():
            ticks.append(self.poll_once())
            if max_ticks is not None and len(ticks) >= max_ticks:
                break
            if event.wait(tick_sec):
                break
        return tuple(ticks)

    def stop_all(self, *, grace_sec: float | None = None) -> None:
        """优雅停掉全部子进程（先 ``terminate``，超宽限期再 ``kill``）。"""
        self._stopping = True
        limit = self._grace_sec if grace_sec is None else grace_sec
        for label in sorted(self._handles):
            handle = self._handles[label]
            if handle.poll() is None:
                _terminate(handle, grace_sec=limit)
            logger.info("supervisor.stopped", label=label, pid=handle.pid)
        self._handles.clear()

    # ── 内部 ────────────────────────────────────────────────────────────
    def _slot_was_reaped(self, state: WorkerSlotState, dead: tuple[str, ...]) -> bool:
        """该 slot 的 worker 是否刚被判死（按 ``<pool>#<slot>@`` 前缀匹配）。"""
        if state.pid is None:
            return False
        expected = WorkerIdentity(pool=state.spec.pool, slot=state.spec.slot, pid=state.pid).worker_id
        return expected in dead

    def _restart(self, label: str, *, reason: str, now: datetime) -> bool | None:
        """重启一个 slot。

        :return: ``True`` 已重启 / ``False`` 重启次数超限已停止 / ``None`` 仍在退避窗口内
        """
        if self._stopping:
            return None
        state = self._states[label]
        spec = state.spec
        if state.restarts >= spec.restart_limit:
            self._states[label] = replace(state, halted=True, pid=None)
            self._log_halted(state, reason=reason)
            return False
        if state.next_restart_at is not None and now < parse_iso(state.next_restart_at):
            return None

        handle = self._handles.pop(label, None)
        if handle is not None and handle.poll() is None:
            logger.warning("supervisor.killing_stuck", label=label, pid=handle.pid, reason=reason)
            _terminate(handle, grace_sec=self._grace_sec)

        restarts = state.restarts + 1
        delay_ms = backoff_delay_ms(
            restarts, base_ms=spec.restart_base_ms, max_ms=spec.restart_max_ms, seed=label
        )
        new_handle = self._spawn(spec)
        self._handles[label] = new_handle
        self._states[label] = replace(
            state,
            pid=new_handle.pid,
            started_at=now_iso(),
            restarts=restarts,
            next_restart_at=format_iso(now + timedelta(milliseconds=delay_ms)),
        )
        logger.warning(
            "supervisor.restarted",
            label=label,
            pid=new_handle.pid,
            restarts=restarts,
            reason=reason,
            backoff_ms=delay_ms,
        )
        return True

    def _log_halted(self, state: WorkerSlotState, *, reason: str) -> None:
        message = (
            f"worker 重启次数超限（{state.restarts}/{state.spec.restart_limit}），已停止重启："
            f"{state.label}（最后原因：{reason}）"
        )
        payload = {
            "code": WORKER_RESTART_HALTED_CODE,
            "severity": "error",
            "message": message,
            "hint": "查 logs/ 与死信区定位根因；修好后重启 supervisor（或在 WebUI 恢复该池）",
            "label": state.label,
            "pool": state.spec.pool,
            "restarts": state.restarts,
            "reason": reason,
        }
        connection = self._connection_factory()
        try:
            connection.execute(
                """
                INSERT INTO system_logs(level, source, stage, message, payload_json)
                VALUES ('error', ?, ?, ?, ?)
                """,
                (
                    f"pool.{state.spec.pool}",
                    state.spec.pool,
                    message,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        finally:
            connection.close()
        logger.error("supervisor.halted", label=state.label, restarts=state.restarts, reason=reason)


def _terminate(handle: ProcessHandle, *, grace_sec: float) -> None:
    """先礼后兵：``terminate`` ⇒ 等宽限期 ⇒ ``kill``。"""
    try:
        handle.terminate()
    except OSError as exc:
        logger.warning("supervisor.terminate_failed", pid=handle.pid, error=str(exc))
    try:
        handle.wait(timeout=grace_sec)
    except Exception as exc:
        logger.warning("supervisor.terminate_timeout", pid=handle.pid, error=str(exc))
        try:
            handle.kill()
            handle.wait(timeout=grace_sec)
        except Exception as inner:
            logger.warning("supervisor.kill_failed", pid=handle.pid, error=str(inner))
