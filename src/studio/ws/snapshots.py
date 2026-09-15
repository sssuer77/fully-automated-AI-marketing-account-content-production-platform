"""握手快照的数据源注册表（T1.7 · §04.4.1）。

`ws` 层**不碰数据库**（契约测试锁死"ws 只读、不写表"）：它只调用注册进来的 provider。
真实 provider 在 `app/deps.py` 里组装（那里是允许访问 db 的地方）。

未注册的通道 ⇒ 返回 `{}`（前端拿到空快照而不是报错，T4.x 各面板再逐个补上）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

__all__ = ["SnapshotFn", "SnapshotRegistry"]

#: 任务快照条数（§04.4.1 的 "tasks:[…20 条…]"）
TASK_SNAPSHOT_ROWS: Final[int] = 20

#: 快照通道（`logs` 由 Hub 自己从 `system_logs` 出，`system` 无状态）
SNAPSHOT_CHANNELS: Final[tuple[str, ...]] = ("tasks", "pools", "metrics", "topics", "publish", "control")

SnapshotFn = Callable[[frozenset[str] | None], Mapping[str, Any]]


class SnapshotRegistry:
    """通道 → provider 的映射（`app/deps.py` 注册，Hub 调用）。"""

    def __init__(self) -> None:
        self._providers: dict[str, SnapshotFn] = {}

    def register(self, channel: str, provider: SnapshotFn) -> None:
        self._providers[channel] = provider

    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def has(self, channel: str) -> bool:
        """该通道是否有真实数据源（没有 ⇒ 不发空快照）。"""
        return channel in self._providers

    def __call__(self, channel: str, *, task_ids: frozenset[str] | None) -> Mapping[str, Any]:
        provider = self._providers.get(channel)
        if provider is None:
            return {}
        return provider(task_ids)
