"""发布限频的**策略层**（T5.3 · §03.4.4 ⑥ · §06.10 · R13）。

这里只做一件事：把"额度用完了"翻译成"**到几点再来**"
--------------------------------------------------
**计数**不在这里。``≤3 条/天/账号`` 那个数字由 :meth:`~studio.db.queue.JobStore.rate_limit_state`
数出来（它要读 ``publications``，而读表是 ``db/`` 的活）。本模块拿到的是那份**结论**
（``allowed`` / ``used_today`` / ``next_allowed_at`` / ``reason``），负责把它变成一条
可执行的顺延指令。

为什么顺延要加抖动
------------------
额度是**按本地日**清零的。不加抖动的话，三个账号会在每天 00:00 齐刷刷地各发一条 ——
那正是 R13 要避免的机器特征（"这个号每天半夜准点发"比"每天不定时发"更像脚本）。
抖动由 ``blake2s(account_id + 日期)`` 派生，所以**同一天问多少次都是同一个时刻**：
随机的话，每次被限频都会把 ``not_before`` 往后推一点，那条作业会永远等不到自己。

为什么 ``min_gap`` 那条路不加抖动
---------------------------------
间隔是从**上一次真实发布时刻**算起的，那个时刻本身就已经是散的；再叠一层抖动只是
让"30 分钟"变成一个说不清的数。日额度那条不一样：它的锚点是**零点**，一个所有人
共享的常数。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from studio.core.clock import format_iso, parse_iso

__all__ = [
    "DAILY_LIMIT_REASON",
    "JITTER_MAX_MIN",
    "MIN_GAP_REASON",
    "RateDecision",
    "daily_rollover_at",
    "decide",
]

#: 次日顺延的抖动上限（分钟）。取 30 是因为它与 ``min_gap_min`` 同量级 ——
#: 抖动比最小间隔还大，会让"今天已经发过了"这件事在面板上看起来像配置写错了。
JITTER_MAX_MIN: Final[int] = 30

#: :meth:`~studio.db.queue.JobStore.rate_limit_state` 用的两个 ``reason`` 取值。
DAILY_LIMIT_REASON: Final[str] = "daily_limit"
MIN_GAP_REASON: Final[str] = "min_gap"


@dataclass(frozen=True, slots=True)
class RateDecision:
    """一次限频判定的结论（``allowed=False`` 时 ``not_before`` 必有值）。"""

    allowed: bool
    used_today: int
    daily_limit: int
    not_before: str | None = None
    reason: str | None = None
    hint: str | None = None

    @property
    def is_daily_limit(self) -> bool:
        return self.reason == DAILY_LIMIT_REASON


def daily_rollover_at(
    *,
    next_allowed_at: str,
    account_id: str,
    jitter_max_min: int = JITTER_MAX_MIN,
) -> str:
    """次日零点（``next_allowed_at``）**加一段确定性抖动**。

    ``next_allowed_at`` 由 ``rate_limit_state`` 算好（本地零点，转成 UTC 的 ISO 串），
    这里只加偏移 —— 时区那一段算术只有一处，改的时候不会漏。
    """
    if jitter_max_min <= 0:
        return next_allowed_at
    moment = parse_iso(next_allowed_at)
    # 种子带**日期**：同一账号在不同的日子拿到不同的偏移，而"今天"永远是同一个。
    seed = f"{account_id}|{moment.strftime('%Y-%m-%d')}"
    digest = hashlib.blake2s(seed.encode("utf-8"), digest_size=8).digest()
    minutes = int.from_bytes(digest, "big") % (jitter_max_min + 1)
    return format_iso(moment + timedelta(minutes=minutes))


def decide(
    *,
    allowed: bool,
    used_today: int,
    daily_limit: int,
    next_allowed_at: str | None,
    reason: str | None,
    account_id: str,
    now: datetime,
    jitter_max_min: int = JITTER_MAX_MIN,
) -> RateDecision:
    """把限频结论翻成一条**可执行**的顺延指令（或"放行"）。

    :param now: 只在提示语里出现（"距顺延还有 N 分钟"），**不参与算术** ——
        算术的锚点是 ``next_allowed_at``，那个值由数据库那一侧算。
    """
    if allowed:
        return RateDecision(allowed=True, used_today=used_today, daily_limit=daily_limit)

    if next_allowed_at is None:
        # 说不清"什么时候能发"就不许发：放行一条额度已满的发布是不可逆的（R14），
        # 而"顺延到 None"会让作业永远停在 pending 且没人知道为什么。
        return RateDecision(
            allowed=False,
            used_today=used_today,
            daily_limit=daily_limit,
            reason=reason or DAILY_LIMIT_REASON,
            hint=(
                f"账号 {account_id} 的发布额度已满（{used_today}/{daily_limit}），"
                "但队列没给出下一个可用时刻 —— 检查 publications 的 published_at"
            ),
        )

    if reason == MIN_GAP_REASON:
        not_before = next_allowed_at
        hint = (
            f"账号 {account_id} 距上次发布不足最小间隔，顺延到 "
            f"{_local_clock(not_before)}（{_minutes_until(not_before, now)} 分钟后）"
        )
    else:
        not_before = daily_rollover_at(
            next_allowed_at=next_allowed_at, account_id=account_id, jitter_max_min=jitter_max_min
        )
        hint = (
            f"账号 {account_id} 今天已发 {used_today}/{daily_limit} 条，顺延到 "
            f"{_local_clock(not_before)}（次日额度 + 抖动）"
        )

    return RateDecision(
        allowed=False,
        used_today=used_today,
        daily_limit=daily_limit,
        not_before=not_before,
        reason=reason or DAILY_LIMIT_REASON,
        hint=hint,
    )


def _local_clock(stamp: str) -> str:
    """UTC 时间戳 → 本地 ``MM-DD HH:MM``（给人看的，不参与任何判断）。"""
    from studio.core.clock import local_tz  # noqa: PLC0415 —— 与 precheck 同一条：避免导入期读环境

    return parse_iso(stamp).astimezone(local_tz()).strftime("%m-%d %H:%M")


def _minutes_until(stamp: str, now: datetime) -> int:
    delta = parse_iso(stamp) - now
    return max(int(delta.total_seconds() // 60), 0)
