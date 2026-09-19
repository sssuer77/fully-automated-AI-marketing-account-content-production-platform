"""配音熔断器（T2.3 · §04.3.3）—— 连续几句念不出来就别再一句句白等了。

为什么要有半开
--------------
只有 CLOSED / OPEN 两个状态的话，时间一到就**全放行** —— 而"引擎其实还没起来"
会让整整一批句子同时撞上去（真机冷加载 20s，那批句子会一起超时）。HALF_OPEN
**只放一次**探测，所以下面有一条用例专门数那个名额。

时间用**注入的假钟**推，不 ``sleep``：5 分钟的窗口不该让测试跑 5 分钟。
"""

from __future__ import annotations

import pytest

from studio.tts.circuit import (
    DEFAULT_OPEN_SEC,
    DEFAULT_THRESHOLD,
    CircuitBreaker,
    CircuitState,
)


class FakeClock:
    """可推的单调钟（``CircuitBreaker`` 的注入点）。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(clock=clock)


def _trip(breaker: CircuitBreaker) -> None:
    """把闸门拉开（三句念不出来）。"""
    for key in ("s1", "s2", "s3"):
        breaker.record_failure(key)


# ══════════════════════════════════════════════════════════════════════
# 计数：按**句**，不按**次**
# ══════════════════════════════════════════════════════════════════════


def test_the_defaults_match_the_spec() -> None:
    """§04.3.3：连续失败 ≥3 句 ⇒ 熔断；池暂停 5min。"""
    assert DEFAULT_THRESHOLD == 3
    assert DEFAULT_OPEN_SEC == 300.0


def test_a_fresh_breaker_lets_everything_through(breaker: CircuitBreaker) -> None:
    snapshot = breaker.snapshot()
    assert snapshot.state == CircuitState.CLOSED.value
    assert snapshot.consecutive_failures == 0
    assert snapshot.trips == 0
    assert breaker.allow() is True
    assert breaker.blocking() is False


def test_two_failed_sentences_are_not_enough(breaker: CircuitBreaker) -> None:
    assert breaker.record_failure("s1") is False
    assert breaker.record_failure("s2") is False
    assert breaker.allow() is True
    assert breaker.snapshot().consecutive_failures == 2


def test_three_distinct_sentences_open_the_gate(breaker: CircuitBreaker) -> None:
    assert breaker.record_failure("s1") is False
    assert breaker.record_failure("s2") is False
    assert breaker.record_failure("s3") is True, "拉开的那一拍必须返回 True（调用方据此发告警）"
    snapshot = breaker.snapshot()
    assert snapshot.state == CircuitState.OPEN.value
    assert snapshot.trips == 1
    assert snapshot.opened_at is not None
    assert breaker.allow() is False


def test_one_stubborn_sentence_cannot_open_the_gate(breaker: CircuitBreaker) -> None:
    """★ 同一句重试三次**只是一个信号**（"这段文本这台引擎念不出来"）。

    按**次**数计的话，一句难念的台词自己就能把闸门拉开，后果是**整条片子剩下的
    句子全部被静音** —— 而那正是 ``RETRY_SIMPLIFIED`` 想避免的事。
    """
    assert [breaker.record_failure("s1") for _ in range(3)] == [False, False, False]
    assert breaker.snapshot().consecutive_failures == 1
    assert breaker.allow() is True


def test_a_success_resets_the_streak(breaker: CircuitBreaker) -> None:
    """ "**连续**"才成立：中间念出来过一句，前面的账就清了。"""
    breaker.record_failure("s1")
    breaker.record_failure("s2")
    breaker.record_success()
    assert breaker.snapshot().consecutive_failures == 0
    assert breaker.record_failure("s1") is False, "归零之后同一句又能算一次"
    assert breaker.snapshot().consecutive_failures == 1


def test_it_does_not_alert_twice_for_the_same_trip(breaker: CircuitBreaker) -> None:
    """``==`` 而不是 ``>=``：越线之后每一次失败都返回 ``True`` 就是**重复告警**
    （与 T4.10 的自动降并发同一条，陷阱 #79）。"""
    _trip(breaker)
    assert breaker.record_failure("s4") is False
    assert breaker.record_failure("s5") is False
    assert breaker.snapshot().trips == 1


# ══════════════════════════════════════════════════════════════════════
# 闸门：开够久 ⇒ 半开 ⇒ **只放一次**探测
# ══════════════════════════════════════════════════════════════════════


def test_the_gate_blocks_until_the_window_is_over(breaker: CircuitBreaker, clock: FakeClock) -> None:
    _trip(breaker)
    assert breaker.allow() is False
    clock.advance(DEFAULT_OPEN_SEC - 1)
    assert breaker.allow() is False, "差一秒也不行"
    clock.advance(1)
    assert breaker.allow() is True
    assert breaker.snapshot().state == CircuitState.HALF_OPEN.value


def test_half_open_lets_exactly_one_probe_through(breaker: CircuitBreaker, clock: FakeClock) -> None:
    _trip(breaker)
    clock.advance(DEFAULT_OPEN_SEC)
    assert breaker.allow() is True
    assert breaker.allow() is False
    assert breaker.allow() is False


def test_a_successful_probe_closes_the_gate(breaker: CircuitBreaker, clock: FakeClock) -> None:
    _trip(breaker)
    clock.advance(DEFAULT_OPEN_SEC)
    assert breaker.allow() is True
    breaker.record_success()
    snapshot = breaker.snapshot()
    assert snapshot.state == CircuitState.CLOSED.value
    assert snapshot.consecutive_failures == 0
    assert snapshot.opened_at is None
    assert breaker.allow() is True


def test_a_failed_probe_reopens_and_restarts_the_window(breaker: CircuitBreaker, clock: FakeClock) -> None:
    _trip(breaker)
    clock.advance(DEFAULT_OPEN_SEC)
    assert breaker.allow() is True
    assert breaker.record_failure("s4") is True
    assert breaker.snapshot().state == CircuitState.OPEN.value
    assert breaker.allow() is False
    clock.advance(DEFAULT_OPEN_SEC)
    assert breaker.allow() is True, "重新计时之后还会再放一次"


def test_blocking_does_not_spend_the_probe(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """``blocking()`` 必须**没有副作用**：失败路径上判的是"现在该不该再做点什么"，
    那一步不该顺手把半开的名额用掉 —— 名额是留给**下一次真的调用引擎**的。"""
    _trip(breaker)
    clock.advance(DEFAULT_OPEN_SEC)
    assert breaker.blocking() is True
    assert breaker.blocking() is True
    assert breaker.allow() is True, "名额还在"


def test_the_snapshot_counts_down(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """面板与告警读它（"还要等多久"）。"""
    assert breaker.snapshot().remaining_ms == 0
    _trip(breaker)
    assert breaker.snapshot().remaining_ms == round(DEFAULT_OPEN_SEC * 1000)
    clock.advance(60)
    assert breaker.snapshot().remaining_ms == round((DEFAULT_OPEN_SEC - 60) * 1000)


def test_the_snapshot_does_not_change_the_state(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """读快照**不能**把闸门推进到半开（否则面板每刷一次就多放一个探测）。"""
    _trip(breaker)
    clock.advance(DEFAULT_OPEN_SEC)
    breaker.snapshot()
    breaker.snapshot()
    assert breaker.snapshot().state == CircuitState.OPEN.value
    assert breaker.allow() is True


def test_reset_puts_it_back(breaker: CircuitBreaker) -> None:
    """人工清零（运维：修完引擎不必等那 5 分钟）。"""
    _trip(breaker)
    breaker.reset()
    snapshot = breaker.snapshot()
    assert snapshot.state == CircuitState.CLOSED.value
    assert snapshot.consecutive_failures == 0
    assert breaker.allow() is True


@pytest.mark.parametrize(("threshold", "open_sec"), [(0, 300.0), (-1, 300.0), (3, 0.0), (3, -1.0)])
def test_nonsense_settings_are_rejected(threshold: int, open_sec: float) -> None:
    """阈值 0 会让闸门**一起步就是打开的**；``open_sec=0`` 会让半开变成每句都放行。"""
    with pytest.raises(ValueError):
        CircuitBreaker(threshold=threshold, open_sec=open_sec)
