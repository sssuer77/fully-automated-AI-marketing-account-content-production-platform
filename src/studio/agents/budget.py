"""token / 成本预算闸门（T1.8 · R16）。

两个上限，两条独立判定
----------------------
| 上限 | 口径 | 数据源 |
| --- | --- | --- |
| ``per_task_token_limit`` | **单任务**累计 token | ``llm_calls`` 按 ``task_id`` 求和 |
| ``per_day_cost_usd_limit`` | **当日**累计成本 | ``llm_calls`` 按 ``created_at`` 求和 |

超限后的动作由 ``on_exceed`` 决定（``config/llm.yaml`` 可编辑）：

- ``switch_to_local``（默认）：**云端**已超限 ⇒ 换本地通道继续干（本地不烧钱）；
  若当前路由本就是本地 ⇒ 放行（没有更便宜的地方可去）。
- ``fail_task``：直接失败，落 ``LLM_BUDGET_EXCEEDED``。
- ``alert_only``：放行 + 记一条 ``warn`` 日志（让人知道钱在烧，但不打断生产）。

为什么以 ``llm_calls`` 聚合为权威
--------------------------------
``AgentContext.token_budget_remaining`` 是**上游提示**（pipeline 可能已扣减），
两者取**更严格**的那个（``limit = min(配置上限, 已花 + 剩余)``）——
任何一侧算错都不会让预算闸门失效。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from studio.agents.cost import LlmCallStore
from studio.core.clock import format_iso
from studio.core.config import LlmBudgetConfig

__all__ = [
    "BudgetAction",
    "BudgetVerdict",
    "TokenBudget",
]


class BudgetAction(StrEnum):
    """预算判定结果。"""

    ALLOW = "allow"
    SWITCH_TO_LOCAL = "switch_to_local"
    FAIL_TASK = "fail_task"
    ALERT_ONLY = "alert_only"


#: ``on_exceed`` 的字符串 → 动作枚举（取值与 ``LlmBudgetConfig.on_exceed`` **逐字一致**）
_MAP: Final[dict[str, BudgetAction]] = {
    "switch_to_local": BudgetAction.SWITCH_TO_LOCAL,
    "fail_task": BudgetAction.FAIL_TASK,
    "alert_only": BudgetAction.ALERT_ONLY,
}


def _utc_day_start() -> str:
    """今天 00:00:00Z 的 ISO 字符串（"今日花费"的下界）。"""
    now = datetime.now(UTC)
    return format_iso(now.replace(hour=0, minute=0, second=0, microsecond=0))


class BudgetVerdict(BaseModel):
    """一次预算判定的完整依据（落 ``system_logs`` 时直接展开）。"""

    model_config = ConfigDict(extra="forbid")

    action: BudgetAction
    exceeded: bool
    reason: str
    spent_tokens: int
    token_limit: int
    spent_cost_usd: float
    cost_limit_usd: float


class TokenBudget:
    """预算闸门（读 ``llm_calls`` 聚合，纯判定、无副作用）。"""

    def __init__(
        self,
        config: LlmBudgetConfig,
        store: LlmCallStore,
        *,
        day_start: Callable[[], str] = _utc_day_start,
    ) -> None:
        self._config = config
        self._store = store
        self._day_start = day_start

    @property
    def config(self) -> LlmBudgetConfig:
        return self._config

    def check(self, *, task_id: str | None, remaining_hint: int | None = None) -> BudgetVerdict:
        """判定是否放行。无 ``task_id`` ⇒ 跳过任务级 token 判定（仅看当日成本）。"""
        spent_tokens = self._store.tokens_for_task(task_id) if task_id else 0
        token_limit = self._config.per_task_token_limit
        if task_id and remaining_hint is not None:
            token_limit = min(token_limit, spent_tokens + max(0, remaining_hint))

        spent_cost = self._store.cost_usd_since(self._day_start())
        cost_limit = self._config.per_day_cost_usd_limit

        over_tokens = bool(task_id) and spent_tokens >= token_limit
        over_cost = cost_limit > 0 and spent_cost >= cost_limit
        exceeded = over_tokens or over_cost

        if not exceeded:
            return BudgetVerdict(
                action=BudgetAction.ALLOW,
                exceeded=False,
                reason="ok",
                spent_tokens=spent_tokens,
                token_limit=token_limit,
                spent_cost_usd=spent_cost,
                cost_limit_usd=cost_limit,
            )

        reason = _describe(
            over_tokens=over_tokens,
            over_cost=over_cost,
            spent_tokens=spent_tokens,
            token_limit=token_limit,
            spent_cost=spent_cost,
            cost_limit=cost_limit,
        )
        return BudgetVerdict(
            action=_MAP[str(self._config.on_exceed)],
            exceeded=True,
            reason=reason,
            spent_tokens=spent_tokens,
            token_limit=token_limit,
            spent_cost_usd=spent_cost,
            cost_limit_usd=cost_limit,
        )


def _describe(
    *,
    over_tokens: bool,
    over_cost: bool,
    spent_tokens: int,
    token_limit: int,
    spent_cost: float,
    cost_limit: float,
) -> str:
    parts: list[str] = []
    if over_tokens:
        parts.append(f"任务 token {spent_tokens} ≥ {token_limit}")
    if over_cost:
        parts.append(f"当日成本 {spent_cost:.4f} ≥ {cost_limit:.4f} USD")
    return "；".join(parts) or "预算超限"
