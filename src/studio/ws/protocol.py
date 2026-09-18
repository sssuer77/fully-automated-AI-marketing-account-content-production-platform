"""WS 实时推送协议契约（T1.7 · §04.4）—— 通道 / 信封 / 事件表 / 背压阈值的唯一真相。

为什么集中在一个文件
--------------------
前端（T4.x）、Hub、合并器、背压缓冲、契约测试都要引用同一套枚举与阈值。散落各处
就会出现"前端订阅 `logs`、服务端叫 `log`"这类只在联调时才暴露的错位。

四条不变量（施工裁定 45–48）
----------------------------
1. **日志广播的唯一真相是 `system_logs.id`**：Hub 只广播**已落库**的行 ⇒
   "先落库再广播"从"纪律"变成"结构"（陷阱 #13）。副作用同样是关键收益：
   worker 进程写的 `pool.*` / `tts.*` / `render.*` 日志**无需 IPC** 就能到前端。
2. **`system.alert` 永不合并、永不限流、永不丢弃**（§04.4.6）。
3. **`seq` 是"每连接单调"**（不是全局）：有通道过滤 + 合并时，全局计数必然产生假缺口；
   每连接自增才能让"缺口 ⇒ resync"这条规则真正可用（§04.4.2 的"服务端全局"按此修正）。
4. **合并与限流在 Hub 侧全局生效**（不是每连接各算一遍）：限流的意义是"别让某个任务
   刷爆前端"，全局算一次即可，且天然一致。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field

from studio.core.errors import ErrorCode, StudioError
from studio.core.proto import EventKind, Severity

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
    "SNAPSHOT_LOGS",
    "TAIL_INTERVAL_SEC",
    "WS_PATH",
    "WS_PROTOCOL_VERSION",
    "Channel",
    "Envelope",
    "EventKind",
    "FrameType",
    "KindPolicy",
    "Subscription",
    "WsSubscriptionError",
    "level_at_least",
    "parse_subscription",
    "policy_for",
    "rate_bucket",
]

#: 协议版本（信封 `v` 字段）；不兼容变更时 +1
WS_PROTOCOL_VERSION: Final[int] = 1

#: WS 端点路径（§04.4.1）
WS_PATH: Final[str] = "/ws/ui"

#: 合并窗口（§04.4.6）：同 channel + 同实体在窗口内合并为最后一条
COALESCE_WINDOW_SEC: Final[float] = 0.1

#: 每条连接的待发环形缓冲容量（§04.4.6）
RING_CAPACITY: Final[int] = 200

#: 慢客户端判定：缓冲**满**且持续这么久 ⇒ 断开（§04.4.6，见 `backpressure` 的说明）
SLOW_SUSTAIN_SEC: Final[float] = 10.0

#: 日志 tail 周期（秒）—— 兜底轮询；`LogService.append()` 会主动 `wake()` 把延迟压到 ~0
TAIL_INTERVAL_SEC: Final[float] = 0.25

#: `since_id` 单次补发上限（§04.4.6）；超出 ⇒ 改发 snapshot（最近 `SNAPSHOT_LOGS` 条）
REPLAY_LIMIT: Final[int] = 2000

#: 握手 / 截断时下发的日志条数（§04.4.6）
SNAPSHOT_LOGS: Final[int] = 500

#: 日志级别默认下限（§04.4.1）：`debug` 默认丢弃
DEFAULT_MIN_LEVEL: Final[str] = "info"


class Channel(StrEnum):
    """八个通道（§04.4.1 · 与规格书逐字一致，契约测试锁死）。"""

    TASKS = "tasks"
    LOGS = "logs"
    POOLS = "pools"
    METRICS = "metrics"
    SYSTEM = "system"
    CONTROL = "control"
    TOPICS = "topics"
    PUBLISH = "publish"


CHANNELS: Final[tuple[str, ...]] = tuple(channel.value for channel in Channel)
CHANNEL_SET: Final[frozenset[str]] = frozenset(CHANNELS)


class FrameType(StrEnum):
    """信封 `type` 字段（§04.4.2）。"""

    SNAPSHOT = "snapshot"
    EVENT = "event"
    ACK = "ack"
    ERROR = "error"
    PONG = "pong"


#: 级别序（与 `system_logs.level` 的 CHECK 一致；只用于比较，不落库）
SEVERITY_ORDER: Final[Mapping[str, int]] = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "error": 40,
    "fatal": 50,
}


def level_at_least(level: str, minimum: str) -> bool:
    """`level` 是否达到 `minimum`（未知级别按 `info` 处理，不因脏值丢日志）。"""
    return SEVERITY_ORDER.get(level, 20) >= SEVERITY_ORDER.get(minimum, 20)


@dataclass(frozen=True, slots=True)
class KindPolicy:
    """一种事件的推送策略（合并 / 限流 / 是否可丢）—— 一份表喂三个消费者。

    :param channel: 归属通道
    :param merge_field: 合并键所在的 `data` 字段名（`None` ⇒ 不按实体合并）
    :param rate_hz: 频率上限（`None` ⇒ 事件驱动，不限流）
    :param never_drop: 背压时**永不丢弃**（告警）
    :param never_merge: 跳过合并窗口（告警：每条都要看得见）
    """

    channel: Channel
    merge_field: str | None = None
    rate_hz: float | None = None
    never_drop: bool = False
    never_merge: bool = False


#: 事件 → 策略（§04.4.3 的"频率上限"列逐条落地）
KIND_POLICY: Final[Mapping[EventKind, KindPolicy]] = {
    EventKind.TASK_CREATED: KindPolicy(Channel.TASKS, merge_field="id"),
    EventKind.TASK_UPDATED: KindPolicy(Channel.TASKS, merge_field="id", rate_hz=2.0),
    EventKind.TASK_TRANSITION: KindPolicy(Channel.TASKS, merge_field="id"),
    EventKind.TASK_PROGRESS_DETAIL: KindPolicy(Channel.TASKS, merge_field="id", rate_hz=2.0),
    EventKind.TASK_COMPLETED: KindPolicy(Channel.TASKS, merge_field="id"),
    EventKind.TASK_FAILED: KindPolicy(Channel.TASKS, merge_field="id"),
    EventKind.SENTENCE_UPDATED: KindPolicy(Channel.TASKS, merge_field="id", rate_hz=5.0),
    EventKind.REVIEW_SCORED: KindPolicy(Channel.TASKS, merge_field="task_id"),
    EventKind.SCRIPT_EDITING: KindPolicy(Channel.TASKS, merge_field="task_id"),
    EventKind.DEGRADED: KindPolicy(Channel.TASKS, merge_field="task_id"),
    EventKind.APPROVAL_REQUESTED: KindPolicy(Channel.TASKS, merge_field="task_id"),
    EventKind.APPROVAL_DECIDED: KindPolicy(Channel.TASKS, merge_field="task_id"),
    EventKind.DIRECTION_BATCH_READY: KindPolicy(Channel.TOPICS, merge_field="batch_id"),
    EventKind.TOPIC_BATCH_READY: KindPolicy(Channel.TOPICS, merge_field="direction_id"),
    EventKind.TOPIC_SELECTED: KindPolicy(Channel.TOPICS, merge_field="topic_id"),
    EventKind.TOPIC_DEDUP_WARN: KindPolicy(Channel.TOPICS, merge_field="topic_id"),
    EventKind.PUBLISH_QUEUED: KindPolicy(Channel.PUBLISH, merge_field="publication_id"),
    EventKind.PUBLISH_PROGRESS: KindPolicy(Channel.PUBLISH, merge_field="publication_id", rate_hz=1.0),
    EventKind.PUBLISH_DONE: KindPolicy(Channel.PUBLISH, merge_field="publication_id"),
    EventKind.PUBLISH_FAILED: KindPolicy(Channel.PUBLISH, merge_field="publication_id"),
    EventKind.PUBLISH_MANUAL_REQUIRED: KindPolicy(Channel.PUBLISH, merge_field="publication_id"),
    EventKind.METRICS_UPDATED: KindPolicy(Channel.PUBLISH, merge_field="publication_id"),
    EventKind.PUBLISH_SCHEDULED: KindPolicy(Channel.PUBLISH, merge_field="schedule_id"),
    EventKind.PUBLISH_SCHEDULE_FIRED: KindPolicy(Channel.PUBLISH, merge_field="schedule_id"),
    EventKind.LOG_APPENDED: KindPolicy(Channel.LOGS, rate_hz=2.0),
    EventKind.POOL_STATS: KindPolicy(Channel.POOLS, merge_field="pool", rate_hz=1.0),
    EventKind.POOL_WORKER_STATUS: KindPolicy(Channel.POOLS, merge_field="worker_id", rate_hz=1.0),
    EventKind.METRICS_TICK: KindPolicy(Channel.METRICS, rate_hz=0.2),
    EventKind.SYSTEM_ALERT: KindPolicy(Channel.SYSTEM, never_drop=True, never_merge=True),
    EventKind.SYSTEM_DOCTOR: KindPolicy(Channel.SYSTEM),
    EventKind.SYSTEM_PERSONA_CHANGED: KindPolicy(Channel.SYSTEM, merge_field="persona_id"),
    EventKind.COMMAND_RESULT: KindPolicy(Channel.CONTROL, merge_field="request_id"),
}

#: 未登记事件的兜底策略：**不丢**（宁可多推一条，也不静默吞掉新事件）
_FALLBACK_POLICY: Final[KindPolicy] = KindPolicy(Channel.TASKS, merge_field="id")


def policy_for(kind: str) -> KindPolicy:
    """取事件的推送策略；未登记 ⇒ 兜底（不合并、不限流、可丢）。"""
    try:
        return KIND_POLICY[EventKind(kind)]
    except ValueError:
        return _FALLBACK_POLICY


def rate_bucket(kind: str, data: Mapping[str, Any]) -> str:
    """限流桶：同一实体的高频更新共用一个桶（§04.4.6"单任务限流"）。"""
    policy = policy_for(kind)
    if policy.rate_hz is None:
        return ""
    field = policy.merge_field
    value = data.get(field) if field else None
    if value is None:
        value = data.get("task_id") or data.get("id") or "-"
    return f"{kind}:{value}"


class Envelope(BaseModel):
    """统一报文信封（§04.4.2）。`data.kind` 见 :class:`EventKind`。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: int = Field(default=WS_PROTOCOL_VERSION)
    type: FrameType
    channel: Channel
    seq: int = Field(default=1, ge=1)
    ts: str
    data: dict[str, Any]

    def to_json(self) -> str:
        return self.model_dump_json()


class WsSubscriptionError(StudioError):
    """订阅参数非法（`channels` / `min_level` / `since_id` 取值不合法）。"""

    default_code = ErrorCode.WS_SUBSCRIPTION_INVALID


@dataclass(frozen=True, slots=True)
class Subscription:
    """一条连接的订阅面（§04.4.1 的查询参数）。"""

    channels: frozenset[str]
    task_ids: frozenset[str] | None
    min_level: Severity
    since_id: int | None
    token: str | None

    @property
    def wants_logs(self) -> bool:
        return Channel.LOGS.value in self.channels or Channel.SYSTEM.value in self.channels

    def accepts_task(self, task_id: str | None) -> bool:
        """任务过滤：未订阅任务 ⇒ 不因任务被过滤；无 `task_id` 的事件一律放行。"""
        if self.task_ids is None or task_id is None:
            return True
        return task_id in self.task_ids

    def accepts_level(self, level: str) -> bool:
        return level_at_least(level, self.min_level)


def _split_csv(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_subscription(params: Mapping[str, str]) -> Subscription:
    """解析握手查询参数；任何非法取值 ⇒ :class:`WsSubscriptionError`（拒绝连接）。"""
    raw_channels = _split_csv(params.get("channels"))
    unknown = [item for item in raw_channels if item not in CHANNEL_SET]
    if unknown:
        raise WsSubscriptionError(
            f"未知通道：{', '.join(unknown)}",
            context={"unknown": unknown, "allowed": list(CHANNELS)},
            remediation=f"channels 只能取：{','.join(CHANNELS)}",
        )
    channels = frozenset(raw_channels) if raw_channels else frozenset(CHANNELS)

    raw_min = (params.get("min_level") or DEFAULT_MIN_LEVEL).strip()
    if raw_min not in SEVERITY_ORDER:
        raise WsSubscriptionError(
            f"非法日志级别：{raw_min}",
            context={"min_level": raw_min, "allowed": list(SEVERITY_ORDER)},
        )

    raw_since = (params.get("since_id") or "").strip()
    since_id: int | None = None
    if raw_since:
        try:
            since_id = int(raw_since)
        except ValueError as exc:
            raise WsSubscriptionError(
                f"since_id 必须是整数：{raw_since}",
                context={"since_id": raw_since},
            ) from exc
        if since_id < 0:
            raise WsSubscriptionError(
                f"since_id 不能为负：{since_id}",
                context={"since_id": since_id},
            )

    task_ids = frozenset(_split_csv(params.get("task_ids"))) or None
    token = (params.get("token") or "").strip() or None
    return Subscription(
        channels=channels,
        task_ids=task_ids,
        min_level=cast(Severity, raw_min),
        since_id=since_id,
        token=token,
    )
