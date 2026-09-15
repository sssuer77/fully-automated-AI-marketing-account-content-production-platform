"""跨层协议与类型别名（core 层只放"所有层都要用"的抽象）。

这里**不定义业务契约**（那些属于 §04-contracts），只放结构性协议：
凡是能被"序列化 / 自检 / 关闭"的东西，都通过这里的 Protocol 表达，
从而让上层在不 import 具体实现的前提下做鸭子类型编程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, runtime_checkable

__all__ = [
    "ALERT_SEVERITY",
    "EVENT_PAYLOAD_KEY",
    "AlertCode",
    "AlertSeverity",
    "CheckStatus",
    "Checkable",
    "EventKind",
    "JsonDict",
    "JsonValue",
    "Serializable",
    "Severity",
]

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonDict = dict[str, JsonValue]

#: 自检结论：``ok`` 通过 / ``warn`` 可降级 / ``fail`` 阻塞启动 / ``skip`` 未适用
type CheckStatus = Literal["ok", "warn", "fail", "skip"]

#: 日志级别 —— 必须与 ``system_logs.level`` 的 CHECK **逐字一致**
#: （``debug|info|warn|error|fatal``；T1.5 施工裁定 25：此前误写成
#: ``warning``/``critical``，会让写入直接撞 CHECK）。
type Severity = Literal["debug", "info", "warn", "error", "fatal"]

#: 告警级别（``system.alert.severity``）：是 :data:`Severity` 的子集 ——
#: 告警是"需要人看"的日志，不允许 ``debug``/``info``。
type AlertSeverity = Literal["warn", "error", "fatal"]


class AlertCode(StrEnum):
    """``system.alert.code`` 枚举（§04.5.2 · 唯一真相）。

    ⚠️ T1.5 施工裁定：§1.4.4 写的 ``POOL_CONCURRENCY_REDUCED`` 与本节枚举冲突，
    以本节为准（即 :attr:`POOL_AUTODEGRADED`），§1.4.4 已同步修正。
    """

    DISK_LOW = "DISK_LOW"
    DISK_CRITICAL = "DISK_CRITICAL"
    TTS_CIRCUIT_OPEN = "TTS_CIRCUIT_OPEN"
    JOB_DEAD = "JOB_DEAD"
    POOL_AUTODEGRADED = "POOL_AUTODEGRADED"
    PUBLISH_LOGIN_EXPIRED = "PUBLISH_LOGIN_EXPIRED"
    DUP_AUDIT_WARN = "DUP_AUDIT_WARN"
    BROLL_EMPTY = "BROLL_EMPTY"


#: 告警码 → 落 ``system_logs.level`` 的级别（一处定义，禁止各处硬编码）。
ALERT_SEVERITY: Final[Mapping[AlertCode, AlertSeverity]] = {
    AlertCode.DISK_LOW: "warn",
    AlertCode.DISK_CRITICAL: "fatal",
    AlertCode.TTS_CIRCUIT_OPEN: "error",
    AlertCode.JOB_DEAD: "error",
    AlertCode.POOL_AUTODEGRADED: "warn",
    AlertCode.PUBLISH_LOGIN_EXPIRED: "warn",
    AlertCode.DUP_AUDIT_WARN: "warn",
    AlertCode.BROLL_EMPTY: "warn",
}


class EventKind(StrEnum):
    """下行事件种类（§04.4.3 事件表的唯一真相）。

    **为什么住在 core 而不是 `ws/`**：这个枚举有两个方向的消费者 —— `ws/` 用它
    把事件扇出到通道，`services/` 用它**声明**一行日志要额外扇出哪条事件
    （见 :data:`EVENT_PAYLOAD_KEY`）。分层方向是 ``app → ws → services → db``，
    `services` 不能 import `ws`；放进 core（"所有层都要用的抽象"）两边都能引用，
    而真相仍然只有一份（`ws/protocol.py` 原样 re-export，导入路径不变）。
    """

    # tasks
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_TRANSITION = "task.transition"
    TASK_PROGRESS_DETAIL = "task.progress_detail"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    SENTENCE_UPDATED = "sentence.updated"
    REVIEW_SCORED = "review.scored"
    SCRIPT_EDITING = "script.editing"
    DEGRADED = "degraded"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    # topics
    DIRECTION_BATCH_READY = "direction.batch_ready"
    TOPIC_BATCH_READY = "topic.batch_ready"
    TOPIC_SELECTED = "topic.selected"
    TOPIC_DEDUP_WARN = "topic.dedup_warn"
    # publish
    PUBLISH_QUEUED = "publish.queued"
    PUBLISH_PROGRESS = "publish.progress"
    PUBLISH_DONE = "publish.done"
    PUBLISH_FAILED = "publish.failed"
    PUBLISH_MANUAL_REQUIRED = "publish.manual_required"
    METRICS_UPDATED = "metrics.updated"
    # logs / pools / metrics / system / control
    LOG_APPENDED = "log.appended"
    POOL_STATS = "pool.stats"
    POOL_WORKER_STATUS = "pool.worker_status"
    METRICS_TICK = "metrics.tick"
    SYSTEM_ALERT = "system.alert"
    SYSTEM_DOCTOR = "system.doctor"
    SYSTEM_PERSONA_CHANGED = "system.persona_changed"
    COMMAND_RESULT = "command.result"


#: 日志 ``payload`` 里用来**声明要额外扇出的 WS 事件**的键（T4.4 · §04.4.4）。
#:
#: 为什么需要它：worker 是独立进程，它写的日志没有 IPC 能推给 API 进程的 Hub；
#: 而"表 → 推送"这条路跨进程天然可用（T1.7 裁定）。于是约定 —— 日志行若在
#: ``payload`` 里带这个键，Hub 除了 ``log.appended`` **再扇出一条**同名
#: :class:`EventKind` 事件（走它自己的通道与合并策略）。``approval.requested``
#: 这类"面板要立刻反应"的事件因此不需要第二套跨进程机制。
#:
#: ⚠️ 键名**不能**叫 ``event``：structlog 的第一个位置参数就是 ``event``，
#: ``logger.info(message, **payload)`` 会直接撞成 ``TypeError``。
EVENT_PAYLOAD_KEY: Final[str] = "event_kind"


@runtime_checkable
class Serializable(Protocol):
    """可序列化为 JSON 安全字典（落库、WS 推送、API 响应共用）。"""

    def to_dict(self) -> JsonDict: ...


@runtime_checkable
class Checkable(Protocol):
    """自检项（doctor / health 路由共用）。

    ``name`` 形如 ``ffmpeg.filters``；``blocking`` 决定失败时是否拒绝启动。
    """

    name: str
    blocking: bool

    def run(self) -> Mapping[str, Any]: ...


def merge_sequence(*groups: Sequence[str]) -> tuple[str, ...]:
    """按序合并多个字符串序列并去重（保序）。"""
    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item, None)
    return tuple(seen)
