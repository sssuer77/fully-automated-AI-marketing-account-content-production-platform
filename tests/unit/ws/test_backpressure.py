"""环形缓冲与慢客户端判定的单元测试（T1.7 · §04.4.6）。"""

from __future__ import annotations

from studio.core.proto import Severity
from studio.ws.backpressure import OutboundBuffer, SlowClientWatch, envelope_level, is_alert
from studio.ws.protocol import Channel, Envelope, EventKind, FrameType
from tests.unit.ws.helpers import event


def _log(level: Severity, log_id: int) -> Envelope:
    return event(EventKind.LOG_APPENDED.value, level=level, id=log_id, channel=Channel.LOGS)


def _alert(index: int) -> Envelope:
    return event(EventKind.SYSTEM_ALERT.value, code="JOB_DEAD", id=index, channel=Channel.SYSTEM)


def test_push_keeps_until_capacity() -> None:
    buffer = OutboundBuffer(capacity=3)
    for index in range(3):
        assert buffer.push(_log("info", index)).kept is True
    assert len(buffer) == 3
    assert buffer.dropped == 0


def test_overflow_drops_debug_first() -> None:
    buffer = OutboundBuffer(capacity=3)
    buffer.push(_log("warn", 1))
    buffer.push(_log("debug", 2))
    buffer.push(_log("warn", 3))
    outcome = buffer.push(_log("info", 4))
    assert outcome.dropped == 1
    remaining = [frame.data["id"] for frame in buffer.pop_all()]
    assert 2 not in remaining, "先丢 debug"


def test_overflow_drops_info_when_no_debug() -> None:
    buffer = OutboundBuffer(capacity=2)
    buffer.push(_log("warn", 1))
    buffer.push(_log("info", 2))
    assert buffer.push(_log("warn", 3)).dropped == 1
    assert 2 not in [frame.data["id"] for frame in buffer.pop_all()]


def test_overflow_merges_same_kind_when_nothing_droppable() -> None:
    """丢不动（没有 debug/info）时才走"合并同类"——这是 §04.4.6 的丢弃顺序。"""
    buffer = OutboundBuffer(capacity=2)
    buffer.push(event(EventKind.TASK_UPDATED.value, id="t1", progress=1, level="warn"))
    buffer.push(event(EventKind.TASK_UPDATED.value, id="t1", progress=2, level="warn"))
    outcome = buffer.push(event(EventKind.TASK_UPDATED.value, id="t1", progress=3, level="warn"))
    assert outcome.merged is True
    assert buffer.merged == 1
    frames = buffer.pop_all()
    assert [frame.data["progress"] for frame in frames] == [2, 3]


def test_alerts_are_never_dropped() -> None:
    buffer = OutboundBuffer(capacity=2, alert_overflow=5)
    for index in range(7):
        buffer.push(_alert(index))
    ids = [frame.data["id"] for frame in buffer.pop_all()]
    assert len(ids) == 7, "告警允许溢出，但一条都不能丢"


def test_alert_overflow_flag_signals_backpressure() -> None:
    buffer = OutboundBuffer(capacity=1, alert_overflow=1)
    buffer.push(_alert(1))
    assert buffer.push(_alert(2)).overloaded is True
    assert buffer.overloaded is True


def test_pop_all_clears_and_resets_overflow() -> None:
    buffer = OutboundBuffer(capacity=1, alert_overflow=1)
    buffer.push(_alert(1))
    buffer.push(_alert(2))
    buffer.pop_all()
    assert len(buffer) == 0
    assert buffer.overloaded is False


def test_envelope_level_defaults_to_info() -> None:
    frame = event(EventKind.TASK_UPDATED.value, id="t1")
    assert envelope_level(frame) == "info"
    assert is_alert(frame) is False


def test_slow_client_watch_requires_sustained_backlog() -> None:
    watch = SlowClientWatch(sustain_sec=10.0)
    assert watch.observe(backlog_full=True, now=0.0) is False
    assert watch.observe(backlog_full=True, now=9.9) is False
    assert watch.observe(backlog_full=True, now=10.0) is True


def test_slow_client_watch_resets_when_backlog_clears() -> None:
    watch = SlowClientWatch(sustain_sec=10.0)
    watch.observe(backlog_full=True, now=0.0)
    watch.observe(backlog_full=False, now=5.0)
    assert watch.observe(backlog_full=True, now=14.0) is False, "中间清空过 ⇒ 重新计时"
    assert watch.observe(backlog_full=True, now=24.1) is True


def test_snapshot_frames_are_not_alerts() -> None:
    frame = Envelope(type=FrameType.SNAPSHOT, channel=Channel.TASKS, ts="t", data={"tasks": []})
    assert is_alert(frame) is False
    assert envelope_level(frame) == "info"
