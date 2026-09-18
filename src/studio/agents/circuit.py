"""通道熔断（T1.8 · §01.2.4「云端故障 ⇒ 自动切本地」）。

为什么需要它
------------
云端 5xx / 超时往往是**分钟级**故障（供应商抖动）。没有熔断的话，
每个 Agent 调用都要先撞一次超时（最长 120s）才切本地 —— 一条稿子 6 个 Agent，
就是十几分钟的纯等待。熔断把"已经知道坏了的通道"直接跳过。

状态机（时间由调用方注入 ⇒ 测试确定性）
--------------------------------------
```
CLOSED ──连续失败 ≥ fail_threshold──▶ OPEN ──open_sec 到点──▶ HALF_OPEN
   ▲                                   ▲                        │
   └────────record_success─────────────┴────record_failure──────┘
```
- ``OPEN`` 期间 :meth:`CircuitBreaker.allow` 返回 ``False`` ⇒ 网关直接走 fallback。
- ``HALF_OPEN`` 只放**一次**探针通过；探针成功 ⇒ 回 ``CLOSED``，失败 ⇒ 重新 ``OPEN``。
- 熔断是**每通道**的（云端独立于本地），且**不落告警码**：§04.5.2 把
  ``system.alert.code`` 是枚举（T1.5 裁定 31，T5.6 起 9 值），故熔断只写 ``system_logs`` 的
  ``warn``/``error`` 行 + ``payload_json.code='LLM_CIRCUIT_OPEN'``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "CIRCUIT_FAIL_THRESHOLD",
    "CIRCUIT_OPEN_SEC",
    "CircuitBreaker",
    "CircuitState",
]

#: 连续失败几次判定"这个通道坏了"
CIRCUIT_FAIL_THRESHOLD: Final[int] = 3

#: 熔断保持时长（到点后放一个半开探针）
CIRCUIT_OPEN_SEC: Final[float] = 60.0


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _ProfileState:
    failures: int = 0
    open_until: float | None = None
    probing: bool = False


@dataclass(slots=True)
class CircuitBreaker:
    """每通道熔断器（进程内状态，**不持久化**：重启后重新探测是合理的）。"""

    fail_threshold: int = CIRCUIT_FAIL_THRESHOLD
    open_sec: float = CIRCUIT_OPEN_SEC
    _states: dict[str, _ProfileState] = field(default_factory=dict)

    def _state_of(self, profile: str) -> _ProfileState:
        return self._states.setdefault(profile, _ProfileState())

    def state(self, profile: str, *, now: float) -> CircuitState:
        state = self._state_of(profile)
        if state.open_until is None:
            return CircuitState.CLOSED
        if now >= state.open_until:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allow(self, profile: str, *, now: float) -> bool:
        """该通道现在是否可尝试。半开态只放行**一次**探针。"""
        current = self.state(profile, now=now)
        if current is CircuitState.CLOSED:
            return True
        if current is CircuitState.OPEN:
            return False
        state = self._state_of(profile)
        if state.probing:
            return False
        state.probing = True
        return True

    def record_success(self, profile: str, *, now: float) -> None:
        """成功 ⇒ 立刻恢复（并清零连续失败）。"""
        state = self._state_of(profile)
        state.failures = 0
        state.open_until = None
        state.probing = False

    def record_failure(self, profile: str, *, now: float) -> None:
        """失败 ⇒ 累加；达阈值或半开探针失败 ⇒ 重新 OPEN。"""
        state = self._state_of(profile)
        was_probing = state.probing
        state.probing = False
        state.failures += 1
        if was_probing or state.failures >= self.fail_threshold:
            state.open_until = now + self.open_sec

    def open_until(self, profile: str) -> float | None:
        """该通道的熔断到点时刻（``None`` = 未熔断）；供日志与测试断言。"""
        return self._state_of(profile).open_until

    def snapshot(self, *, now: float) -> dict[str, str]:
        """各通道当前状态（健康检查 / 总览台用）。"""
        return {profile: str(self.state(profile, now=now)) for profile in sorted(self._states)}
