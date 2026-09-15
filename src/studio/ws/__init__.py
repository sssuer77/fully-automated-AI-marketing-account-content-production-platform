"""实时推送层（T1.7 · §04.4）。

- `protocol.py`   通道 / 信封 / 事件表 / 阈值（唯一真相）
- `coalescer.py`  合并窗口 100ms + 限流（2Hz 进度 / 5Hz 句）
- `backpressure.py` 环形缓冲 200 条 + 慢客户端判定
- `hub.py`        连接注册、扇出、日志 tail、`since_id` 补发
- `snapshots.py`  握手快照的数据源注册表（真实 provider 由 `app/deps.py` 注入）
"""

from __future__ import annotations

from studio.ws.backpressure import OutboundBuffer, PushOutcome, SlowClientWatch, envelope_level, is_alert
from studio.ws.coalescer import Coalescer
from studio.ws.hub import ClientConnection, FrameSink, Hub, HubSettings, LogReader, SnapshotProvider
from studio.ws.protocol import (
    CHANNEL_SET,
    CHANNELS,
    COALESCE_WINDOW_SEC,
    DEFAULT_MIN_LEVEL,
    KIND_POLICY,
    REPLAY_LIMIT,
    RING_CAPACITY,
    SEVERITY_ORDER,
    SLOW_SUSTAIN_SEC,
    SNAPSHOT_LOGS,
    TAIL_INTERVAL_SEC,
    WS_PATH,
    WS_PROTOCOL_VERSION,
    Channel,
    Envelope,
    EventKind,
    FrameType,
    KindPolicy,
    Subscription,
    WsSubscriptionError,
    level_at_least,
    parse_subscription,
    policy_for,
    rate_bucket,
)
from studio.ws.snapshots import SNAPSHOT_CHANNELS, TASK_SNAPSHOT_ROWS, SnapshotRegistry

__all__ = [
    "CHANNELS",
    "CHANNEL_SET",
    "COALESCE_WINDOW_SEC",
    "DEFAULT_MIN_LEVEL",
    "KIND_POLICY",
    "REPLAY_LIMIT",
    "RING_CAPACITY",
    "SEVERITY_ORDER",
    "SLOW_SUSTAIN_SEC",
    "SNAPSHOT_CHANNELS",
    "SNAPSHOT_LOGS",
    "TAIL_INTERVAL_SEC",
    "TASK_SNAPSHOT_ROWS",
    "WS_PATH",
    "WS_PROTOCOL_VERSION",
    "Channel",
    "ClientConnection",
    "Coalescer",
    "Envelope",
    "EventKind",
    "FrameSink",
    "FrameType",
    "Hub",
    "HubSettings",
    "KindPolicy",
    "LogReader",
    "OutboundBuffer",
    "PushOutcome",
    "SlowClientWatch",
    "SnapshotProvider",
    "SnapshotRegistry",
    "Subscription",
    "WsSubscriptionError",
    "envelope_level",
    "is_alert",
    "level_at_least",
    "parse_subscription",
    "policy_for",
    "rate_bucket",
]
