"""``db/lease.py`` 单测：租约 / 退避的时间算术（§01.4.2 · §03.4.3）。

这些算式是队列正确性的一部分，所以按边界值逐条钉死，而不是靠集成测试"顺带覆盖"。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from studio.core.clock import format_iso, parse_iso
from studio.db.lease import (
    JITTER_RATIO,
    POLL_BACKOFF_BASE_MS,
    POLL_BACKOFF_MAX_MS,
    backoff_delay_ms,
    heartbeat_interval_sec,
    is_expired,
    jitter_ms,
    lease_expires_at,
    not_before_at,
    poll_delay_ms,
)

T0 = datetime(2026, 9, 13, 6, 0, 0, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# 租约
# ══════════════════════════════════════════════════════════════════════


def test_lease_expires_at_is_utc_millisecond() -> None:
    text = lease_expires_at(90, now=T0)
    assert text == "2026-09-13T06:01:30.000Z"
    assert len(text) == 24


@pytest.mark.parametrize("lease_sec", [0, -1])
def test_lease_expires_at_rejects_non_positive(lease_sec: int) -> None:
    with pytest.raises(ValueError):
        lease_expires_at(lease_sec, now=T0)


def test_heartbeat_interval_is_third_of_lease() -> None:
    """§03.4.3：续租周期 = ``lease_sec / 3``（与 5s 进程心跳职责不同）。"""
    assert heartbeat_interval_sec(90) == pytest.approx(30.0)
    assert heartbeat_interval_sec(600) == pytest.approx(200.0)


def test_is_expired_boundaries() -> None:
    deadline = lease_expires_at(90, now=T0)
    assert not is_expired(deadline, now=T0)
    assert not is_expired(deadline, now=T0 + timedelta(seconds=89))
    assert is_expired(deadline, now=T0 + timedelta(seconds=91))


def test_is_expired_treats_none_as_expired() -> None:
    """没有租约 = 可以认领，不该被当成"永不过期"。"""
    assert is_expired(None, now=T0)


# ══════════════════════════════════════════════════════════════════════
# 退避
# ══════════════════════════════════════════════════════════════════════


def test_backoff_is_exponential_and_capped() -> None:
    """``min(base × 2^(n-1), cap)`` + jitter(0..20%)。"""
    base, cap = 1000, 8000
    seen = []
    for attempts in (1, 2, 3, 4, 5):
        delay = backoff_delay_ms(attempts, base_ms=base, max_ms=cap, seed="x")
        seen.append(delay)
        ideal = min(base << (attempts - 1), cap)
        assert ideal <= delay <= ideal * (1 + JITTER_RATIO)
    assert seen[0] < seen[1] < seen[2] < seen[3]
    assert seen[4] <= cap * (1 + JITTER_RATIO)


def test_backoff_jitter_is_deterministic_per_seed() -> None:
    """同 ``(job_id, attempts)`` ⇒ 同退避时长（重启后可复现，不靠运气解释）。"""
    first = backoff_delay_ms(3, base_ms=1000, max_ms=8000, seed="job-1:3")
    second = backoff_delay_ms(3, base_ms=1000, max_ms=8000, seed="job-1:3")
    assert first == second


def test_backoff_jitter_spreads_different_seeds() -> None:
    """不同种子要真的散开 —— 否则"避免重试尖峰"就是空话。"""
    delays = {backoff_delay_ms(3, base_ms=1000, max_ms=8000, seed=f"job-{i}:3") for i in range(40)}
    assert len(delays) > 20


def test_backoff_zero_jitter_ratio_is_exact() -> None:
    assert backoff_delay_ms(1, base_ms=5000, max_ms=30000, seed="s", jitter_ratio=0.0) == 5000


def test_jitter_is_zero_for_non_positive_delay() -> None:
    assert jitter_ms(0, seed="s") == 0
    assert jitter_ms(-5, seed="s") == 0


@pytest.mark.parametrize("attempts", [0, -1])
def test_backoff_rejects_attempts_below_one(attempts: int) -> None:
    """``attempts`` 在认领时已 +1，传 0 说明调用方搞错了语义。"""
    with pytest.raises(ValueError):
        backoff_delay_ms(attempts, base_ms=1000, max_ms=8000)


@pytest.mark.parametrize(("base", "cap"), [(0, 8000), (1000, 0), (-1, 8000)])
def test_backoff_rejects_non_positive_params(base: int, cap: int) -> None:
    with pytest.raises(ValueError):
        backoff_delay_ms(1, base_ms=base, max_ms=cap)


def test_backoff_does_not_overflow_on_huge_attempts() -> None:
    """``attempts`` 很大时位移不能炸成天文数字（死循环任务见过 attempts=10^6）。"""
    delay = backoff_delay_ms(10**6, base_ms=1000, max_ms=8000, seed="s")
    assert delay <= 8000 * (1 + JITTER_RATIO)


def test_not_before_at_is_now_plus_backoff() -> None:
    text = not_before_at(1, base_ms=5000, max_ms=30000, seed="s", jitter_ratio=0.0, now=T0)
    assert text == format_iso(T0 + timedelta(seconds=5))
    assert parse_iso(text) > T0


def test_not_before_at_is_lexicographically_comparable() -> None:
    """退避落点必须能直接与 SQL 里的 ``now`` 比字符串（毫秒定宽）。"""
    early = not_before_at(1, base_ms=1000, max_ms=8000, seed="a", jitter_ratio=0.0, now=T0)
    late = not_before_at(1, base_ms=5000, max_ms=8000, seed="a", jitter_ratio=0.0, now=T0)
    assert early < late
    assert len(early) == len(late) == 24


# ══════════════════════════════════════════════════════════════════════
# 空池轮询退避
# ══════════════════════════════════════════════════════════════════════


def test_poll_delay_grows_then_caps() -> None:
    """200ms → 400 → 800 → 1600 → 2000（封顶），**禁止 busy-loop**。"""
    assert [poll_delay_ms(n) for n in range(5)] == [200, 400, 800, 1600, 2000]
    assert poll_delay_ms(50) == POLL_BACKOFF_MAX_MS
    assert POLL_BACKOFF_BASE_MS == 200
    assert POLL_BACKOFF_MAX_MS == 2000


def test_poll_delay_rejects_negative_rounds() -> None:
    with pytest.raises(ValueError):
        poll_delay_ms(-1)
