"""发布限频的策略层（T5.3 · §03.4.4 ⑥ · §06.10）。

测什么、不测什么
----------------
**计数**（今天发了几条）不在这里 —— 它在 ``JobStore.rate_limit_state`` 里，由
``tests/integration/test_queue_lease.py`` 用真库验。这里验的是"把结论翻成顺延指令"
那一步，四件事各自都可能**静默走偏**：

① 额度满 ⇒ 顺延到**次日零点之后**（不是"现在就重试"）；
② 抖动**确定性**：同一天问多少次都是同一个时刻（随机的话，每次被限频都会把
   ``not_before`` 往后推一点，那条作业永远等不到自己）；
③ 最小间隔那条路**不加抖动**（锚点是上次真实发布时刻，本身就是散的）；
④ 说不清"什么时候能发"时**不许发**（放行一条额度已满的发布不可逆 · R14）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from studio.core.clock import parse_iso
from studio.publish.ratelimit import (
    DAILY_LIMIT_REASON,
    MIN_GAP_REASON,
    RateDecision,
    daily_rollover_at,
    decide,
)

#: 本地时间 2026-09-16 14:00（北京时间）—— 顺延算的是**本地零点**，所以固定一个时刻
NOW = datetime(2026, 9, 16, 6, 0, 0, tzinfo=UTC)

#: ``rate_limit_state`` 给的"次日本地零点"（UTC 串）
MIDNIGHT = "2026-09-16T16:00:00.000Z"


def test_allowed_passes_through() -> None:
    """放行时不带任何顺延信息（面板据此显示"额度还够"）。"""
    decision = decide(
        allowed=True,
        used_today=1,
        daily_limit=3,
        next_allowed_at=None,
        reason=None,
        account_id="acc_main",
        now=NOW,
    )

    assert decision.allowed is True
    assert decision.not_before is None
    assert decision.reason is None
    assert decision.used_today == 1
    assert decision.daily_limit == 3


def test_daily_limit_defers_to_next_midnight_with_jitter() -> None:
    """额度满 ⇒ 顺延到次日零点**之后**（抖动是正的，不会提前到零点之前）。"""
    decision = decide(
        allowed=False,
        used_today=3,
        daily_limit=3,
        next_allowed_at=MIDNIGHT,
        reason=DAILY_LIMIT_REASON,
        account_id="acc_main",
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.is_daily_limit is True
    assert decision.not_before is not None
    at = parse_iso(decision.not_before)
    assert at >= parse_iso(MIDNIGHT)
    assert at <= parse_iso(MIDNIGHT) + timedelta(minutes=30)
    assert decision.hint is not None and "3/3" in decision.hint


def test_daily_jitter_is_deterministic_per_account_and_day() -> None:
    """同一天、同一个账号 ⇒ 同一个时刻；换账号 ⇒ 大概率不同（R13 去机器特征）。"""

    def rollover(account_id: str) -> str:
        return daily_rollover_at(next_allowed_at=MIDNIGHT, account_id=account_id, jitter_max_min=30)

    first = rollover("acc_main")
    second = rollover("acc_main")
    assert first == second

    others = {rollover(f"acc_{index}") for index in range(8)}
    assert len(others) > 1, "八个账号拿到同一个时刻 ⇒ 抖动没生效"


def test_jitter_never_exceeds_the_cap() -> None:
    """抖动上限是硬边界：换 20 个账号，没有一个越过 ``jitter_max_min``。"""
    ceiling = parse_iso(MIDNIGHT) + timedelta(minutes=5)
    for index in range(20):
        stamp = daily_rollover_at(next_allowed_at=MIDNIGHT, account_id=f"acc_{index}", jitter_max_min=5)
        assert parse_iso(MIDNIGHT) <= parse_iso(stamp) <= ceiling


def test_zero_jitter_returns_the_anchor_untouched() -> None:
    """``jitter_max_min=0`` ⇒ 原样返回（关掉抖动的开关必须是**真的**关掉）。"""
    assert daily_rollover_at(next_allowed_at=MIDNIGHT, account_id="acc_main", jitter_max_min=0) == MIDNIGHT


def test_min_gap_is_deferred_without_jitter() -> None:
    """最小间隔那条路：``not_before`` 就是锚点本身（再叠抖动会让"30 分钟"说不清）。"""
    anchor = "2026-09-16T06:30:00.000Z"
    decision = decide(
        allowed=False,
        used_today=1,
        daily_limit=3,
        next_allowed_at=anchor,
        reason=MIN_GAP_REASON,
        account_id="acc_main",
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.is_daily_limit is False
    assert decision.not_before == anchor
    assert decision.hint is not None and "30" in decision.hint


def test_unknown_next_allowed_at_blocks_instead_of_guessing() -> None:
    """说不清什么时候能发 ⇒ **不放行**，且 ``not_before`` 保持 ``None``。

    这一支接的是"限频说不过、但没给出时刻"这种自相矛盾的输入。放行不可逆（R14）；
    而"顺延到 None"会让作业永远停在 pending 且没人知道为什么 —— 所以两者都不做，
    把 ``hint`` 交出去让人去查 ``publications``。
    """
    decision = decide(
        allowed=False,
        used_today=3,
        daily_limit=3,
        next_allowed_at=None,
        reason=None,
        account_id="acc_main",
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.not_before is None
    assert decision.reason == DAILY_LIMIT_REASON
    assert decision.hint is not None and "acc_main" in decision.hint


def test_decision_is_frozen() -> None:
    """冻结 dataclass：顺延指令一旦算出来就不该被半路改写。"""
    decision = RateDecision(allowed=True, used_today=0, daily_limit=3)
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]
