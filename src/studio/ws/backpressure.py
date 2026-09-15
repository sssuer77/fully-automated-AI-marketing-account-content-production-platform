"""环形缓冲与慢客户端判定（T1.7 · §04.4.6）。

两条规则
--------
1. **环形缓冲 200 条/连接**：满了按 `debug` → `info` → 合并同类 → 丢最旧的
   `warn`/`error` 顺序腾位置；**`system.alert` 永不丢**。
2. **慢客户端**：积压持续满 `SLOW_SUSTAIN_SEC`(10s) ⇒ 断开并让前端 resync。

⚠️ 与 §04.4.6 的一处冲突（施工裁定 46）
---------------------------------------
规格书写的是"队列 > 500 条且持续 10s ⇒ 断开"，但同一张表里环形缓冲只有 **200** 条
—— 200 条的上限**永远**到不了 500。二者只能取一个：以缓冲容量为准，把慢客户端判定
改成"**缓冲满且持续 10s**"（即"我们正在为它丢帧"），这才是真正可观测的信号。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final

from studio.ws.protocol import RING_CAPACITY, SLOW_SUSTAIN_SEC, Envelope

__all__ = ["OutboundBuffer", "PushOutcome", "SlowClientWatch", "envelope_level", "is_alert"]

#: 先丢这些级别（§04.4.6："满则丢弃 debug → info"）
DROP_ORDER: Final[tuple[str, ...]] = ("debug", "info")

#: 告警允许的额外溢出（缓冲全是告警时的最后手段，2 倍容量后仍满 ⇒ 丢最旧告警）
ALERT_OVERFLOW: Final[int] = RING_CAPACITY


def is_alert(envelope: Envelope) -> bool:
    """是否 `system.alert`（背压的唯一"免死金牌"）。"""
    return envelope.data.get("kind") == "system.alert"


def envelope_level(envelope: Envelope) -> str:
    """帧的日志级别（非日志帧一律按 `info` 计，只有 debug/info 会被优先丢弃）。"""
    level = envelope.data.get("level")
    return level if isinstance(level, str) else "info"


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """一次入队的结论（测试与观测用）。"""

    kept: bool
    dropped: int = 0
    merged: bool = False
    overloaded: bool = False


class OutboundBuffer:
    """一条连接的待发帧环形缓冲。"""

    def __init__(self, *, capacity: int = RING_CAPACITY, alert_overflow: int = ALERT_OVERFLOW) -> None:
        self._capacity = capacity
        self._alert_overflow = alert_overflow
        self._items: deque[Envelope] = deque()
        self._dropped = 0
        self._merged = 0
        self._overflow = False

    # ── 写入 ────────────────────────────────────────────────────────
    def push(self, envelope: Envelope) -> PushOutcome:
        """入队一条帧；满则按策略腾位置（告警永不丢）。"""
        if len(self._items) < self._capacity:
            self._items.append(envelope)
            self._overflow = False
            return PushOutcome(kept=True)

        victim = self._find_droppable()
        if victim is not None:
            self._items.remove(self._items[victim])
            self._items.append(envelope)
            self._dropped += 1
            return PushOutcome(kept=True, dropped=1)

        merge_index = self._find_mergeable(envelope)
        if merge_index is not None:
            del self._items[merge_index]
            self._items.append(envelope)
            self._merged += 1
            return PushOutcome(kept=True, merged=True)

        oldest = self._find_oldest_disposable()
        if oldest is not None:
            del self._items[oldest]
            self._items.append(envelope)
            self._dropped += 1
            return PushOutcome(kept=True, dropped=1)

        # 缓冲里全是告警：绝不丢告警 ⇒ 允许溢出（并在 2 倍容量后丢最旧告警）
        self._overflow = True
        if len(self._items) >= self._capacity + self._alert_overflow:
            self._items.popleft()
            self._dropped += 1
            return PushOutcome(kept=True, dropped=1, overloaded=True)
        self._items.append(envelope)
        return PushOutcome(kept=True, overloaded=True)

    # ── 读取 ────────────────────────────────────────────────────────
    def pop_all(self) -> list[Envelope]:
        items = list(self._items)
        self._items.clear()
        self._overflow = False
        return items

    def __len__(self) -> int:
        return len(self._items)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def merged(self) -> int:
        return self._merged

    @property
    def overloaded(self) -> bool:
        """最近一次入队时是否发生"全是告警"的溢出（慢客户端判定的输入之一）。"""
        return self._overflow

    # ── 内部 ────────────────────────────────────────────────────────
    def _find_droppable(self) -> int | None:
        for level in DROP_ORDER:
            for index, item in enumerate(self._items):
                if not is_alert(item) and envelope_level(item) == level:
                    return index
        return None

    def _find_mergeable(self, envelope: Envelope) -> int | None:
        kind = envelope.data.get("kind")
        if kind is None:
            return None
        for index, item in enumerate(self._items):
            if is_alert(item):
                continue
            if item.data.get("kind") == kind:
                return index
        return None

    def _find_oldest_disposable(self) -> int | None:
        for index, item in enumerate(self._items):
            if not is_alert(item):
                return index
        return None


class SlowClientWatch:
    """积压持续满阈值 ⇒ 判定慢客户端（§04.4.6）。"""

    def __init__(self, *, sustain_sec: float = SLOW_SUSTAIN_SEC) -> None:
        self._sustain = sustain_sec
        self._since: float | None = None

    def observe(self, *, backlog_full: bool, now: float) -> bool:
        """每拍调用一次；返回 `True` 表示"已持续超时，该断开它了"。"""
        if not backlog_full:
            self._since = None
            return False
        if self._since is None:
            self._since = now
            return False
        return (now - self._since) >= self._sustain

    def reset(self) -> None:
        self._since = None
