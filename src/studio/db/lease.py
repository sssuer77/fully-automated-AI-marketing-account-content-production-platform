"""租约 / 退避的时间算术（§01.4.2 · §03.4.3 · 纯函数、无 I/O）。

为什么单独成模块
----------------
这些算式是**队列正确性的一部分**（租约太长 ⇒ 崩溃后长时间空转；退避太短 ⇒
同批失败单元形成重试尖峰），但它们与数据库无关，因此放在这里做成纯函数，
好让 ``tests/unit/db/test_lease.py`` 用边界值逐条钉死，而不是埋在 SQL 里。

时间格式
--------
所有时间戳都由 :mod:`studio.core.clock` 生成（UTC ISO-8601 **毫秒**），
与 DDL 的 ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` 同格式 ⇒ 可直接字典序比较。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Final

from studio.core.clock import format_iso, now_iso, parse_iso, utc_now

__all__ = [
    "JITTER_RATIO",
    "POLL_BACKOFF_BASE_MS",
    "POLL_BACKOFF_MAX_MS",
    "backoff_delay_ms",
    "heartbeat_interval_sec",
    "is_expired",
    "jitter_ms",
    "lease_expires_at",
    "not_before_at",
    "poll_delay_ms",
]

#: 退避抖动上限（占退避时长的比例）—— ``not_before = now + min(base·2^(n-1), cap) + jitter(0..20%)``
JITTER_RATIO: Final[float] = 0.20

#: 空池轮询退避：200ms → 2s（§03.4.2 "空转策略"）
POLL_BACKOFF_BASE_MS: Final[int] = 200
POLL_BACKOFF_MAX_MS: Final[int] = 2000

#: 位移上限：防止 ``base << (attempts-1)`` 在大 attempts 下溢出/变成天文数字
_MAX_SHIFT: Final[int] = 32


def jitter_ms(delay_ms: int, *, seed: str, ratio: float = JITTER_RATIO) -> int:
    """确定性抖动 ``0..ratio×delay_ms``（由 ``seed`` 派生，可复现）。

    为什么不用 ``random``：同一批失败单元若各自随机，重启后重算会得到不同结果，
    "为什么这个任务 3 秒后重试、那个 11 秒后"就无从解释了。用 ``blake2s(seed)``
    既能打散尖峰，又能按 ``(job_id, attempts)`` 精确复现。
    """
    if delay_ms <= 0 or ratio <= 0:
        return 0
    span = int(delay_ms * ratio)
    if span <= 0:
        return 0
    digest = hashlib.blake2s(seed.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (span + 1)


def backoff_delay_ms(
    attempts: int,
    *,
    base_ms: int,
    max_ms: int,
    seed: str = "",
    jitter_ratio: float = JITTER_RATIO,
) -> int:
    """``min(base × 2^(attempts-1), cap) + jitter(0..20%)``（§01.4.2 第 5 条）。

    :param attempts: **已尝试次数**（``jobs.attempts``，认领时 +1，故首次失败传 1）
    """
    if attempts < 1:
        raise ValueError(f"attempts 必须 ≥ 1（认领时已 +1）：{attempts}")
    if base_ms <= 0 or max_ms <= 0:
        raise ValueError(f"退避参数必须为正：base_ms={base_ms} max_ms={max_ms}")
    shift = min(attempts - 1, _MAX_SHIFT)
    capped = min(base_ms << shift, max_ms)
    return capped + jitter_ms(capped, seed=seed, ratio=jitter_ratio)


def not_before_at(
    attempts: int,
    *,
    base_ms: int,
    max_ms: int,
    seed: str = "",
    now: datetime | None = None,
    jitter_ratio: float = JITTER_RATIO,
) -> str:
    """退避落点时间戳（写 ``jobs.not_before``）。"""
    moment = now or utc_now()
    delay_ms = backoff_delay_ms(
        attempts, base_ms=base_ms, max_ms=max_ms, seed=seed, jitter_ratio=jitter_ratio
    )
    return format_iso(moment + timedelta(milliseconds=delay_ms))


def lease_expires_at(lease_sec: int, *, now: datetime | None = None) -> str:
    """租约到期时间戳（写 ``jobs.lease_expires_at``）。"""
    if lease_sec <= 0:
        raise ValueError(f"lease_sec 必须为正：{lease_sec}")
    return format_iso((now or utc_now()) + timedelta(seconds=lease_sec))


def heartbeat_interval_sec(lease_sec: int) -> float:
    """续租周期 = ``lease_sec / 3``（§03.4.3）。

    与 ``worker_heartbeats`` 的 5s 心跳**职责不同**：这个续的是 **job 租约**。
    """
    if lease_sec <= 0:
        raise ValueError(f"lease_sec 必须为正：{lease_sec}")
    return lease_sec / 3


def is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """租约是否已过期（``None`` 视为"没有租约"⇒ 过期）。"""
    if expires_at is None:
        return True
    return parse_iso(expires_at) < (now or utc_now())


def poll_delay_ms(
    empty_rounds: int, *, base_ms: int = POLL_BACKOFF_BASE_MS, max_ms: int = POLL_BACKOFF_MAX_MS
) -> int:
    """空池轮询退避：连续 ``empty_rounds`` 次没认到东西就睡久一点（上限 ``max_ms``）。

    §03.4.2 明令**禁止 busy-loop** —— 4 个 worker 空转会把 CPU 吃满并加剧写锁竞争。
    """
    if empty_rounds < 0:
        raise ValueError(f"empty_rounds 不能为负：{empty_rounds}")
    shift = min(empty_rounds, _MAX_SHIFT)
    return min(base_ms << shift, max_ms)


def now_timestamp() -> str:
    """当前时间戳（毫秒 UTC ISO-8601）。"""
    return now_iso()
