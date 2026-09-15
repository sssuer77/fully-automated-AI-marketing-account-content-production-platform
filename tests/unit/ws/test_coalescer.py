"""合并窗口与限流的单元测试（T1.7 · §04.4.6）。

时间由测试注入（`now=`）⇒ "2Hz 限流"是**确定性**验证，不靠 `sleep` 碰运气。
"""

from __future__ import annotations

from studio.ws.coalescer import Coalescer
from studio.ws.protocol import Envelope, EventKind
from tests.unit.ws.helpers import event


def _progress(progress: int, task_id: str = "t1") -> Envelope:
    return event(EventKind.TASK_UPDATED.value, id=task_id, progress=progress)


def test_window_merges_to_last_value() -> None:
    coalescer = Coalescer(window_sec=0.1)
    for progress in (10, 50, 90):
        coalescer.submit(_progress(progress), now=0.0)
    assert coalescer.drain(now=0.05) == []  # 窗口未到
    emitted = coalescer.drain(now=0.11)
    assert len(emitted) == 1
    assert emitted[0].data["progress"] == 90, "窗口内只保留最后一条"
    assert coalescer.merged == 2


def test_rate_limit_holds_then_emits_latest() -> None:
    """2Hz ⇒ 相邻两条至少隔 500ms；被押后的那条发出时一定是最新值。"""
    coalescer = Coalescer(window_sec=0.1)
    coalescer.submit(_progress(1), now=0.0)
    assert len(coalescer.drain(now=0.11)) == 1

    coalescer.submit(_progress(2), now=0.15)
    assert coalescer.drain(now=0.26) == [], "窗口过了但限流不允许"
    coalescer.submit(_progress(3), now=0.30)
    assert coalescer.drain(now=0.40) == []

    emitted = coalescer.drain(now=0.62)
    assert len(emitted) == 1
    assert emitted[0].data["progress"] == 3


def test_sentence_updates_use_five_hz() -> None:
    coalescer = Coalescer(window_sec=0.1)
    coalescer.submit(event(EventKind.SENTENCE_UPDATED.value, id="s1", seq=1), now=0.0)
    assert len(coalescer.drain(now=0.11)) == 1
    coalescer.submit(event(EventKind.SENTENCE_UPDATED.value, id="s1", seq=2), now=0.12)
    assert coalescer.drain(now=0.23) == []  # 5Hz ⇒ 需等 200ms
    assert len(coalescer.drain(now=0.33)) == 1


def test_alert_bypasses_window_and_rate_limit() -> None:
    coalescer = Coalescer(window_sec=0.1)
    for _ in range(5):
        coalescer.submit(
            event(EventKind.SYSTEM_ALERT.value, code="JOB_DEAD", message="死了", task_id="t1"),
            now=0.0,
        )
    emitted = coalescer.drain(now=0.0)
    assert len(emitted) == 5, "告警既不合并也不限流"


def test_distinct_entities_do_not_share_a_bucket() -> None:
    coalescer = Coalescer(window_sec=0.1)
    coalescer.submit(_progress(1, "t1"), now=0.0)
    coalescer.submit(_progress(1, "t2"), now=0.0)
    emitted = coalescer.drain(now=0.11)
    assert {frame.data["id"] for frame in emitted} == {"t1", "t2"}


def test_pending_counts_held_events() -> None:
    coalescer = Coalescer(window_sec=0.1)
    coalescer.submit(_progress(1), now=0.0)
    assert coalescer.pending == 1
    coalescer.drain(now=0.11)
    assert coalescer.pending == 0
