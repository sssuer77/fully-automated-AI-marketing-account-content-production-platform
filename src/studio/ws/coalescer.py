"""合并窗口与限流（T1.7 · §04.4.6）。

三条规则（都在这里，不许散到 Hub 里）
------------------------------------
1. **合并窗口 100ms**：同 channel + 同实体的事件在窗口内**只留最后一条**。
   `task.updated` 从 0% 到 100% 刷 60 次 ⇒ 前端只收到最后那一条。
2. **限流**：`KIND_POLICY.rate_hz` 不为空的事件，同一桶内相邻两条至少间隔 `1/rate`。
   超限的**不丢**，只是"押后到下一个允许的时刻"—— 合并的语义是"留最后一条"，
   所以押后之后发出去的一定是最新值（不会出现"进度退回到旧值"）。
3. **告警直通**：`system.alert` 不进窗口、不限流（§04.4.6）。

时间从哪来
----------
`now` 由调用方（Hub）注入 —— 生产是 `time.monotonic()`，测试给确定性数值。
这样"2Hz 限流"能被**确定性**地验证，而不是靠 `sleep` 碰运气。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final

from studio.ws.protocol import COALESCE_WINDOW_SEC, Envelope, policy_for, rate_bucket

__all__ = ["Coalescer"]

#: 限流桶的清理水位（防止长跑后 `_last_emit` 无限增长）
_BUCKET_GC_SIZE: Final[int] = 4096
_BUCKET_GC_AGE_SEC: Final[float] = 60.0


@dataclass(slots=True)
class _Held:
    """被窗口押住的一条事件（同一实体的后来者会覆盖 `envelope`，保留 `first_at`）。"""

    envelope: Envelope
    first_at: float
    bucket: str
    min_interval: float


class Coalescer:
    """把"高频更新流"压成"低频但最新"的推送流。"""

    def __init__(self, *, window_sec: float = COALESCE_WINDOW_SEC) -> None:
        self._window = window_sec
        self._ready: deque[Envelope] = deque()
        self._held: dict[str, _Held] = {}
        self._last_emit: dict[str, float] = {}
        self._merged = 0
        self._emitted = 0

    # ── 写入 ────────────────────────────────────────────────────────
    def submit(self, envelope: Envelope, *, now: float) -> None:
        """接收一条待推送事件（`system.alert` 直接进就绪队列）。"""
        kind = str(envelope.data.get("kind", ""))
        policy = policy_for(kind)
        if policy.never_merge and policy.rate_hz is None:
            self._ready.append(envelope)
            return

        key = self._key(kind, envelope)
        previous = self._held.get(key)
        first_at = now if previous is None else previous.first_at
        if previous is not None:
            self._merged += 1
        rate = policy.rate_hz
        self._held[key] = _Held(
            envelope=envelope,
            first_at=first_at,
            bucket=rate_bucket(kind, envelope.data),
            min_interval=(1.0 / rate) if rate else 0.0,
        )

    # ── 读取 ────────────────────────────────────────────────────────
    def drain(self, *, now: float) -> list[Envelope]:
        """取出本轮可以出队的事件（窗口已过 **且** 限流允许）。"""
        out = list(self._ready)
        self._ready.clear()
        for key, held in list(self._held.items()):
            if now - held.first_at < self._window:
                continue
            if not self._rate_allows(held, now=now):
                continue
            out.append(held.envelope)
            del self._held[key]
            if held.bucket:
                self._last_emit[held.bucket] = now
            self._emitted += 1
        self._gc_buckets(now=now)
        return out

    @property
    def pending(self) -> int:
        """还在窗口 / 限流里押着的事件数（含告警就绪队列）。"""
        return len(self._held) + len(self._ready)

    @property
    def merged(self) -> int:
        return self._merged

    @property
    def emitted(self) -> int:
        return self._emitted

    # ── 内部 ────────────────────────────────────────────────────────
    @staticmethod
    def _key(kind: str, envelope: Envelope) -> str:
        policy = policy_for(kind)
        field = policy.merge_field
        value = envelope.data.get(field) if field else None
        if value is None:
            value = envelope.data.get("task_id") or "-"
        return f"{envelope.channel.value}:{kind}:{value}"

    def _rate_allows(self, held: _Held, *, now: float) -> bool:
        if not held.bucket or held.min_interval <= 0.0:
            return True
        last = self._last_emit.get(held.bucket)
        return last is None or (now - last) >= held.min_interval

    def _gc_buckets(self, *, now: float) -> None:
        if len(self._last_emit) <= _BUCKET_GC_SIZE:
            return
        self._last_emit = {
            bucket: moment for bucket, moment in self._last_emit.items() if now - moment < _BUCKET_GC_AGE_SEC
        }
