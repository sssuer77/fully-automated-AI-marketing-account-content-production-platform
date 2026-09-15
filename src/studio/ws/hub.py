"""WS Hub：连接注册、扇出、日志 tail、补发与背压（T1.7 · §04.4）。

一张图看懂数据怎么流
--------------------
```
写日志的一侧                         推送的一侧
─────────────                        ─────────────
LogService.append() ─┐
JobStore._alert()    ├─▶ system_logs（表 = 唯一真相）
heartbeat._log_dead()┘        │
                              │  Hub._run() 每 250ms（或被 wake 立刻）
                              ▼
                     id > cursor 的行 ─▶ Coalescer（合并 100ms / 限流）
                                              │
                                              ▼
                                        _fanout() ─▶ 每条连接的环形缓冲
                                                          │
                                                    _send_loop() ─▶ WebSocket
```

为什么 tail 而不是"写完直接推"
-------------------------------
worker 是独立进程，它写的日志没有 IPC 能推给 API 进程的 Hub；而"表 → 推送"这条路
**跨进程天然可用**，且顺手把"先落库再广播"变成结构约束（陷阱 #13）。

慢客户端判定放在 Hub 的 tick 里（不在发送循环里）
-------------------------------------------------
发送循环可能正卡在 `await send_text` 上（对端不读就是会卡），此时它根本没机会检查
自己的积压。所以判定必须在**独立的 tick** 上做：`ClientConnection.backlog_full()` 同时
看"缓冲满"与"这一帧发了太久"，任一为真且持续 `SLOW_SUSTAIN_SEC` ⇒ 断开（1008）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode
from studio.core.logging import get_logger
from studio.core.proto import EVENT_PAYLOAD_KEY
from studio.services.log_service import SystemLog
from studio.ws.backpressure import OutboundBuffer, SlowClientWatch
from studio.ws.coalescer import Coalescer
from studio.ws.protocol import (
    COALESCE_WINDOW_SEC,
    KIND_POLICY,
    REPLAY_LIMIT,
    RING_CAPACITY,
    SLOW_SUSTAIN_SEC,
    SNAPSHOT_LOGS,
    TAIL_INTERVAL_SEC,
    Channel,
    Envelope,
    EventKind,
    FrameType,
    Subscription,
)

__all__ = [
    "ClientConnection",
    "FrameSink",
    "Hub",
    "HubSettings",
    "LogReader",
    "SnapshotProvider",
]

logger = get_logger("studio.ws.hub")

#: 每轮 tail 最多捞多少行（一轮捞完就再跑一轮，不丢行）
TAIL_BATCH: Final[int] = 200

#: 正常关闭（客户端主动断开 / 服务端收工）
CLOSE_NORMAL: Final[int] = 1000

#: 策略性关闭（慢客户端）：前端应 resync
CLOSE_POLICY: Final[int] = 1008

#: 内部错误
CLOSE_INTERNAL: Final[int] = 1011


class FrameSink(Protocol):
    """一条连接的出口（生产是 `WebSocket`，测试是假件 ⇒ 不需要真 socket）。"""

    async def send_text(self, payload: str) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...


class LogReader(Protocol):
    """Hub 需要的**只读**日志能力（`LogService` 实现；测试可给假件）。"""

    def latest_id(self) -> int: ...

    def since(
        self,
        log_id: int,
        *,
        limit: int,
        min_level: str | None = None,
        task_id: str | None = None,
    ) -> Sequence[SystemLog]: ...

    def recent(
        self,
        *,
        limit: int,
        min_level: str | None = None,
        task_id: str | None = None,
    ) -> Sequence[SystemLog]: ...


class SnapshotProvider(Protocol):
    """握手 / resync 时的状态快照（`app/deps.py` 注入真实数据源）。"""

    def has(self, channel: str) -> bool: ...

    def __call__(self, channel: str, *, task_ids: frozenset[str] | None) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Hub 的可调参数（默认值全部来自 §04.4.6）。"""

    tail_interval_sec: float = TAIL_INTERVAL_SEC
    coalesce_window_sec: float = COALESCE_WINDOW_SEC
    ring_capacity: int = RING_CAPACITY
    slow_sustain_sec: float = SLOW_SUSTAIN_SEC
    replay_limit: int = REPLAY_LIMIT
    snapshot_logs: int = SNAPSHOT_LOGS


@dataclass(slots=True)
class ClientConnection:
    """一条 `/ws/ui` 连接的全部状态。"""

    conn_id: str
    subscription: Subscription
    sink: FrameSink
    buffer: OutboundBuffer
    watch: SlowClientWatch
    seq: int = 0
    sent: int = 0
    last_log_id: int = 0
    closed: bool = False
    sending_since: float | None = None
    sender: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    # ── 过滤 ────────────────────────────────────────────────────────
    def accepts(self, envelope: Envelope) -> bool:
        """通道 / 任务 / 级别三重过滤（告警不受级别过滤 —— 它本来就是最高优先级）。"""
        if envelope.channel.value not in self.subscription.channels:
            return False
        if envelope.type is FrameType.SNAPSHOT:
            return True
        task_id = envelope.data.get("task_id")
        if not self.subscription.accepts_task(task_id if isinstance(task_id, str) else None):
            return False
        if envelope.data.get("kind") == EventKind.SYSTEM_ALERT.value:
            return True
        level = envelope.data.get("level")
        if isinstance(level, str):
            return self.subscription.accepts_level(level)
        return True

    # ── 入队 ────────────────────────────────────────────────────────
    def enqueue(self, envelope: Envelope) -> bool:
        """过滤 → 分配本连接的 `seq` → 入环形缓冲；返回是否真的入队。

        日志帧按 `log_id`（= `system_logs.id`）去重：tail 与 `since_id` 补发可能撞上同一行。
        注意字段名是 `log_id` 而**不是** `id` —— 业务事件（`task.updated` 等）的 `id` 是实体
        主键，两者混用会让"去重"误伤正常事件（T1.7 实测踩到）。
        """
        if self.closed or not self.accepts(envelope):
            return False
        if envelope.data.get("kind") in {EventKind.LOG_APPENDED.value, EventKind.SYSTEM_ALERT.value}:
            log_id = envelope.data.get("log_id")
            if isinstance(log_id, int):
                if log_id <= self.last_log_id:
                    return False
                self.last_log_id = log_id
        self.seq += 1
        self.buffer.push(envelope.model_copy(update={"seq": self.seq}))
        self.wake.set()
        return True

    def send_direct(self, envelope: Envelope) -> bool:
        """**绕过订阅过滤**直发一条帧（`pong` / `ack` / `error`）。

        上行回执是对"这条连接刚才那句话说"的答复，与它订阅了哪些通道无关 ——
        若也走通道过滤，`channels=logs` 的连接发 `ping` 就永远收不到 `pong`
        （T1.7 实测踩到：客户端一直等，服务端以为已经回了）。
        """
        if self.closed:
            return False
        self.seq += 1
        self.buffer.push(envelope.model_copy(update={"seq": self.seq}))
        self.wake.set()
        return True

    # ── 背压信号 ────────────────────────────────────────────────────
    def backlog_full(self, *, now: float, sustain_sec: float) -> bool:
        """是否处于"发不出去"状态：缓冲满 / 正在丢帧 / 单帧发了太久。"""
        if len(self.buffer) >= self.buffer.capacity or self.buffer.overloaded:
            return True
        since = self.sending_since
        return since is not None and (now - since) >= sustain_sec


class Hub:
    """实时推送中枢（每进程一个；由 `app/lifespan.py` 启动）。"""

    def __init__(
        self,
        *,
        logs: LogReader,
        snapshots: SnapshotProvider,
        settings: HubSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logs = logs
        self._snapshots = snapshots
        self._settings = settings or HubSettings()
        self._clock = clock
        self._coalescer = Coalescer(window_sec=self._settings.coalesce_window_sec)
        self._connections: dict[str, ClientConnection] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._cursor = 0
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._published = 0

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self) -> None:
        """启动 tail 循环（必须在事件循环里调用）。"""
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._cursor = self._logs.latest_id()
        self._task = asyncio.create_task(self._run(), name="ws-hub")
        logger.info("ws.hub_started", cursor=self._cursor)

    async def stop(self) -> None:
        """停 tail + 关掉所有连接（幂等）。"""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        self._loop = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for conn in list(self._connections.values()):
            await self.close_connection(conn, code=CLOSE_NORMAL, reason="server shutting down")
        logger.info("ws.hub_stopped")

    def wake(self) -> None:
        """叫醒 tail 循环（`LogService.append` 落库后调用）。

        **跨线程安全**：`asyncio.Event.set()` 只能在自己那个循环的线程里调；而写日志的
        可能是线程池里的同步路由、也可能是主线程（测试）。所以这里统一走
        `call_soon_threadsafe`；循环还没起 / 已经关了 ⇒ 静默返回（下一拍轮询会兜住）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._wake.set)

    # ── 连接 ────────────────────────────────────────────────────────
    def connect(self, *, conn_id: str, subscription: Subscription, sink: FrameSink) -> ClientConnection:
        """注册一条连接：下发快照 / 补发 ⇒ 再起发送任务（顺序不可颠倒）。"""
        conn = ClientConnection(
            conn_id=conn_id,
            subscription=subscription,
            sink=sink,
            buffer=OutboundBuffer(capacity=self._settings.ring_capacity),
            watch=SlowClientWatch(sustain_sec=self._settings.slow_sustain_sec),
        )
        self._connections[conn_id] = conn
        self._prime(conn)
        conn.sender = asyncio.create_task(self._send_loop(conn), name=f"ws-send:{conn_id}")
        logger.info(
            "ws.connected",
            conn_id=conn_id,
            channels=sorted(subscription.channels),
            since_id=subscription.since_id,
            min_level=subscription.min_level,
            backlog=len(conn.buffer),
        )
        return conn

    async def close_connection(self, conn: ClientConnection, *, code: int, reason: str) -> None:
        """摘掉连接（幂等）：停发送任务 ⇒ 关 socket。"""
        if conn.closed:
            return
        conn.closed = True
        self._connections.pop(conn.conn_id, None)
        sender = conn.sender
        conn.sender = None
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()
        try:
            await conn.sink.close(code, reason)
        except Exception as exc:
            logger.warning("ws.close_failed", conn_id=conn.conn_id, error=str(exc))
        logger.info("ws.disconnected", conn_id=conn.conn_id, sent=conn.sent, dropped=conn.buffer.dropped)

    # ── 推送 ────────────────────────────────────────────────────────
    def publish(self, envelope: Envelope) -> None:
        """投递一条事件（同步、非阻塞）：进合并器 + 叫醒循环。

        **跨线程安全**（T4.2）：`_coalescer.submit` 是纯内存操作，哪个线程都能调；
        但叫醒必须走 :meth:`wake` —— `asyncio.Event.set()` 只能在自己那个循环的
        线程里调，而从**别的线程**调会顺着 `Future.set_result` 走到
        `loop.call_soon`，那会抛
        `RuntimeError: Non-thread-safe operation invoked on an event loop other
        than the current one`。

        这条路径在 T4.2 之前从没被真正走过（三个 `pool.*` / `metrics.*` 事件一直
        只有策略表、没有发布方），所以这个隐患一直没露头；采样泵一上（`tick()`
        跑在 `asyncio.to_thread` 的工作线程里）它立刻就是必踩项。
        """
        self._published += 1
        self._coalescer.submit(envelope, now=self._clock())
        self.wake()

    def _fanout(self, envelope: Envelope) -> int:
        delivered = 0
        for conn in list(self._connections.values()):
            if conn.enqueue(envelope):
                delivered += 1
        return delivered

    def stats(self) -> dict[str, Any]:
        return {
            "connections": len(self._connections),
            "published": self._published,
            "emitted": self._coalescer.emitted,
            "merged": self._coalescer.merged,
            "pending": self._coalescer.pending,
            "cursor": self._cursor,
            "clients": [
                {
                    "conn_id": conn.conn_id,
                    "sent": conn.sent,
                    "seq": conn.seq,
                    "backlog": len(conn.buffer),
                    "dropped": conn.buffer.dropped,
                }
                for conn in self._connections.values()
            ],
        }

    # ── 上行 ────────────────────────────────────────────────────────
    def handle_uplink(self, conn: ClientConnection, raw: str) -> None:
        """处理一条上行报文（`ping` / `resync`）；非法 ⇒ 回 `error` 帧。"""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            conn.send_direct(
                self.error_frame(code=str(ErrorCode.WS_PROTOCOL_VIOLATION), message="不是合法 JSON")
            )
            return
        if not isinstance(payload, dict):
            conn.send_direct(
                self.error_frame(code=str(ErrorCode.WS_PROTOCOL_VIOLATION), message="报文必须是对象")
            )
            return
        command = payload.get("type")
        if command == "ping":
            conn.send_direct(self._frame(FrameType.PONG, Channel.CONTROL, {"kind": "control.pong"}))
            return
        if command == "resync":
            conn.send_direct(self._frame(FrameType.ACK, Channel.CONTROL, {"kind": "control.resync"}))
            self._prime(conn)
            return
        conn.send_direct(
            self.error_frame(
                code=str(ErrorCode.WS_PROTOCOL_VIOLATION),
                message=f"未知上行类型：{command!r}",
            )
        )

    # ── 内部：握手 / 补发 ───────────────────────────────────────────
    def _prime(self, conn: ClientConnection) -> None:
        """下发快照 + 日志补发（顺序：先快照，再增量）。"""
        subscription = conn.subscription
        for channel in sorted(subscription.channels):
            if channel == Channel.LOGS.value:
                self._prime_logs(conn)
                continue
            if channel == Channel.SYSTEM.value or not self._snapshots.has(channel):
                continue  # 没注册 provider 的通道（control / metrics / topics / publish）先不发空快照
            payload = self._snapshots(channel, task_ids=subscription.task_ids)
            conn.enqueue(self._frame(FrameType.SNAPSHOT, Channel(channel), dict(payload)))

    def _prime_logs(self, conn: ClientConnection) -> None:
        subscription = conn.subscription
        task_id = _single_task(subscription.task_ids)
        since_id = subscription.since_id
        if since_id is None:
            rows = self._logs.recent(
                limit=self._settings.snapshot_logs,
                min_level=subscription.min_level,
                task_id=task_id,
            )
            conn.enqueue(
                self._frame(
                    FrameType.SNAPSHOT,
                    Channel.LOGS,
                    {"logs": [row.to_dict() for row in rows], "truncated": False},
                )
            )
            conn.last_log_id = max((row.id for row in rows), default=conn.last_log_id)
            return

        rows = self._logs.since(
            since_id,
            limit=self._settings.replay_limit + 1,
            min_level=subscription.min_level,
            task_id=task_id,
        )
        if len(rows) > self._settings.replay_limit:
            recent = self._logs.recent(
                limit=self._settings.snapshot_logs,
                min_level=subscription.min_level,
                task_id=task_id,
            )
            conn.enqueue(
                self._frame(
                    FrameType.SNAPSHOT,
                    Channel.LOGS,
                    {
                        "logs": [row.to_dict() for row in recent],
                        "truncated": True,
                        "hint": f"断线超过 {self._settings.replay_limit} 条，已改发最近快照；"
                        "更早日志请走 REST 分页（GET /api/v1/logs）",
                    },
                )
            )
            conn.last_log_id = max((row.id for row in recent), default=since_id)
            return

        for row in rows:
            for envelope in self.envelopes_for(row):
                conn.enqueue(envelope)

    # ── 内部：tail 循环 ─────────────────────────────────────────────
    async def _run(self) -> None:
        """tail 循环：捞新日志 ⇒ 出合并队列 ⇒ 扇出 ⇒ 检查慢客户端 ⇒ 等下一拍。"""
        while not self._stopping:
            try:
                self._tail_once()
                for envelope in self._coalescer.drain(now=self._clock()):
                    self._fanout(envelope)
                await self._reap_slow()
            except Exception as exc:  # 推送层绝不能把异常穿出去（会静默停掉整个 Hub）
                logger.warning("ws.tick_failed", error=str(exc))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._settings.tail_interval_sec)

    def _tail_once(self) -> None:
        rows = self._logs.since(self._cursor, limit=TAIL_BATCH)
        for row in rows:
            self._cursor = row.id
            for envelope in self.envelopes_for(row):
                self.publish(envelope)

    async def _reap_slow(self) -> None:
        now = self._clock()
        for conn in list(self._connections.values()):
            full = conn.backlog_full(now=now, sustain_sec=self._settings.slow_sustain_sec)
            if conn.watch.observe(backlog_full=full, now=now):
                logger.warning(
                    "ws.client_slow",
                    conn_id=conn.conn_id,
                    backlog=len(conn.buffer),
                    dropped=conn.buffer.dropped,
                    sent=conn.sent,
                )
                await self.close_connection(
                    conn,
                    code=CLOSE_POLICY,
                    reason=f"{ErrorCode.WS_CLIENT_SLOW}: 待发积压持续超时，已断开，请重连并 resync",
                )

    async def _send_loop(self, conn: ClientConnection) -> None:
        try:
            while not conn.closed:
                for frame in conn.buffer.pop_all():
                    conn.sending_since = self._clock()
                    await conn.sink.send_text(frame.to_json())
                    conn.sending_since = None
                    conn.sent += 1
                conn.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(conn.wake.wait(), timeout=self._settings.tail_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ws.send_failed", conn_id=conn.conn_id, error=str(exc))
            conn.closed = True
            self._connections.pop(conn.conn_id, None)

    # ── 内部：帧构造 ────────────────────────────────────────────────
    @staticmethod
    def _frame(frame_type: FrameType, channel: Channel, data: Mapping[str, Any]) -> Envelope:
        return Envelope(type=frame_type, channel=channel, ts=now_iso(), data=dict(data))

    @staticmethod
    def envelopes_for(row: SystemLog) -> tuple[Envelope, ...]:
        """一行日志 → 一到两条帧（``log.appended`` / ``system.alert`` ＋ 可选结构化事件）。

        为什么"事件搭日志的车"
        ----------------------
        worker 是独立进程，它没有 IPC 能把事件推给 API 进程的 Hub；而"表 → 推送"
        这条路跨进程天然可用（T1.7 裁定 45）。于是约定：日志行在 ``payload`` 里
        带 :data:`~studio.core.proto.EVENT_PAYLOAD_KEY`，Hub 除常规日志帧外**再**扇出
        一条同名 :class:`EventKind` 事件 —— 走它自己的通道与合并策略。

        ``approval.requested`` 正是这样到达前端的：确认闸跑在写稿池进程里，
        而面板在 API 进程的另一头（§04.4.4）。

        声明了但认不出的事件名**不能静默吞掉**：记一条 warn 再照常发日志帧，
        否则"面板少了一类事件"会变成查不出来的悬案。
        """
        primary = Hub.log_envelope(row)
        declared = row.payload.get(EVENT_PAYLOAD_KEY)
        if not isinstance(declared, str):
            return (primary,)
        try:
            kind = EventKind(declared)
        except ValueError:
            logger.warning("ws.unknown_event_kind", kind=declared, log_id=row.id)
            return (primary,)
        policy = KIND_POLICY[kind]
        if policy.channel is primary.channel and primary.data.get("kind") == kind.value:
            return (primary,)  # 声明的是自己（`log.appended` / `system.alert`）⇒ 不重复发
        data: dict[str, Any] = {key: value for key, value in row.payload.items() if key != EVENT_PAYLOAD_KEY}
        # 行上的字段只在 payload 缺席时兜底：`_emit` 把 `task_id` 放在 payload 里，
        # 用行上的值去覆盖它会把事件的路由键抹成 None。
        for name, value in (("task_id", row.task_id), ("source", row.source)):
            if data.get(name) is None:
                data[name] = value
        data["kind"] = kind.value
        data["log_id"] = row.id
        data["ts"] = row.ts
        return (primary, Hub._frame(FrameType.EVENT, policy.channel, data))

    @staticmethod
    def log_envelope(row: SystemLog) -> Envelope:
        """日志行 → 帧（告警走 `system` 通道，普通日志走 `logs` 通道）。"""
        if row.is_alert:
            return Hub._frame(
                FrameType.EVENT,
                Channel.SYSTEM,
                {
                    "kind": EventKind.SYSTEM_ALERT.value,
                    "log_id": row.id,
                    "code": row.alert_code,
                    "severity": row.level,
                    "message": row.message,
                    "hint": row.hint,
                    "source": row.source,
                    "task_id": row.task_id,
                    "job_id": row.job_id,
                    "ts": row.ts,
                },
            )
        return Hub._frame(
            FrameType.EVENT,
            Channel.LOGS,
            {
                "kind": EventKind.LOG_APPENDED.value,
                "log_id": row.id,
                "level": row.level,
                "source": row.source,
                "message": row.message,
                "task_id": row.task_id,
                "job_id": row.job_id,
                "stage": row.stage,
                "unit_ref": row.unit_ref,
                "worker_id": row.worker_id,
                "duration_ms": row.duration_ms,
                "payload": dict(row.payload),
                "ts": row.ts,
            },
        )

    @staticmethod
    def error_frame(*, code: str, message: str) -> Envelope:
        return Hub._frame(
            FrameType.ERROR,
            Channel.CONTROL,
            {"kind": "control.error", "code": code, "message": message},
        )


def _single_task(task_ids: frozenset[str] | None) -> str | None:
    """订阅了**唯一**一个任务时才做 SQL 侧过滤（多任务时交给帧过滤，避免 SQL 拼 IN）。"""
    if task_ids is None or len(task_ids) != 1:
        return None
    return next(iter(task_ids))
