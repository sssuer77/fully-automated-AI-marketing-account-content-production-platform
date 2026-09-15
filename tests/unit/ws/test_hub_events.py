"""Hub 的结构化事件扇出（T4.4 · §04.4.4）。

为什么单独一个文件
------------------
``log.appended`` / ``system.alert`` 的扇出规则在 ``test_ws_replay.py`` 里已经覆盖；
这里只测 T4.4 新加的那一层：**日志行声明的额外事件**。它的失败模式完全不同 ——
漏了它，日志照常显示，只有确认闸面板永远不刷新（最难看出来的一种坏）。

分层上它也是"事件搭日志的车"这条约定的唯一断言点：worker 进程没有 IPC 能把
``approval.requested`` 推给 API 进程的 Hub，只能靠日志表的 payload 捎带。
"""

from __future__ import annotations

from studio.core.proto import EVENT_PAYLOAD_KEY
from studio.services.log_service import SystemLog
from studio.ws.hub import Hub

TS = "2026-09-14T00:00:00+00:00"


def row(
    *,
    log_id: int = 7,
    level: str = "info",
    source: str = "review.pipeline",
    message: str = "审稿第 1 轮：7.2 分（B 级）",
    task_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> SystemLog:
    return SystemLog(
        id=log_id,
        ts=TS,
        level=level,
        source=source,
        message=message,
        task_id=task_id,
        payload=payload or {},
    )


def test_plain_row_yields_exactly_one_log_frame() -> None:
    frames = Hub.envelopes_for(row())
    assert len(frames) == 1
    assert frames[0].channel.value == "logs"
    assert frames[0].data["kind"] == "log.appended"


def test_alert_row_yields_one_system_frame() -> None:
    frames = Hub.envelopes_for(
        row(level="warn", source="doctor", payload={"code": "DISK_LOW", "severity": "warn"})
    )
    assert len(frames) == 1
    assert frames[0].channel.value == "system"
    assert frames[0].data["kind"] == "system.alert"


def test_declared_event_adds_a_second_frame_on_its_own_channel() -> None:
    frames = Hub.envelopes_for(
        row(
            task_id="t1",
            payload={
                EVENT_PAYLOAD_KEY: "approval.requested",
                "task_id": "t1",
                "approval_id": "a1",
                "grade": "B",
                "score_total": 7.2,
                "revision_round": 2,
            },
        )
    )
    assert len(frames) == 2
    assert frames[0].data["kind"] == "log.appended"
    event = frames[1]
    assert event.channel.value == "tasks"
    assert event.data["kind"] == "approval.requested"


def test_declared_event_carries_the_three_things_the_spec_demands() -> None:
    """§04.4.4：`approval.requested` 必须携带 grade / score_total / revision_round。"""
    frames = Hub.envelopes_for(
        row(
            task_id="t1",
            payload={
                EVENT_PAYLOAD_KEY: "approval.requested",
                "task_id": "t1",
                "approval_id": "a1",
                "grade": "B",
                "score_total": 7.2,
                "revision_round": 2,
            },
        )
    )
    data = frames[1].data
    assert (data["grade"], data["score_total"], data["revision_round"]) == ("B", 7.2, 2)
    assert data["task_id"] == "t1"
    assert data["log_id"] == 7


def test_the_declaration_key_itself_never_leaks_into_the_event() -> None:
    """`event_kind` 是内部约定，不该出现在给前端的载荷里。"""
    frames = Hub.envelopes_for(row(payload={EVENT_PAYLOAD_KEY: "approval.decided", "task_id": "t1"}))
    assert EVENT_PAYLOAD_KEY not in frames[1].data


def test_payload_task_id_wins_over_the_null_column() -> None:
    """`_emit` 把 `task_id` 放在 payload 里；用行上的 NULL 覆盖它会把路由键抹掉。"""
    frames = Hub.envelopes_for(
        row(task_id=None, payload={EVENT_PAYLOAD_KEY: "approval.requested", "task_id": "t9"})
    )
    assert frames[1].data["task_id"] == "t9"


def test_column_task_id_backfills_when_payload_has_none() -> None:
    frames = Hub.envelopes_for(row(task_id="t3", payload={EVENT_PAYLOAD_KEY: "approval.requested"}))
    assert frames[1].data["task_id"] == "t3"


def test_unknown_declared_event_is_ignored_not_fatal() -> None:
    """认不出的事件名 ⇒ 照常发日志帧（静默吞掉会让"面板少一类事件"变成悬案）。"""
    frames = Hub.envelopes_for(row(payload={EVENT_PAYLOAD_KEY: "no.such.event"}))
    assert len(frames) == 1
    assert frames[0].data["kind"] == "log.appended"


def test_declaring_its_own_kind_does_not_duplicate() -> None:
    frames = Hub.envelopes_for(row(payload={EVENT_PAYLOAD_KEY: "log.appended"}))
    assert len(frames) == 1


def test_declaring_the_alert_kind_does_not_duplicate() -> None:
    frames = Hub.envelopes_for(
        row(
            level="warn",
            payload={"code": "DISK_LOW", "severity": "warn", EVENT_PAYLOAD_KEY: "system.alert"},
        )
    )
    assert len(frames) == 1
    assert frames[0].data["kind"] == "system.alert"


def test_non_string_declaration_is_ignored() -> None:
    frames = Hub.envelopes_for(row(payload={EVENT_PAYLOAD_KEY: 42}))
    assert len(frames) == 1
