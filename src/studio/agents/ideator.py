"""Ideator（批量选题）· T1.9 · §04.1.3 / 原文 §2.2②。

一次 ``run`` = **一个方向**的选题
--------------------------------
§04.1.3：「单个方向失败**不**影响其他方向」。所以这里刻意只做"一个方向 → 3–5 个选题"，
"5–8 个方向"的循环与"哪个方向失败了"的记账在 ``services/topic_service.py``。
把循环塞进 Agent 会让"某一方向失败"变成一个内部细节 —— 而它必须能被单独重试。

与 ``PlannerAgent`` 的输入差异：这里带 ``existing_titles``
--------------------------------------------------------
R15（选题同质化）要在**生成时**就把"已有的"告诉模型，不能只靠事后去重：
事后去重只能丢弃/降分，而模型完全有能力换个角度重写。
"""

from __future__ import annotations

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.agents.planner import direction_grounding_text
from studio.domain.topics import IdeatorInput, IdeatorOutput

__all__ = ["IdeatorAgent"]


class IdeatorAgent(BaseAgent[IdeatorInput, IdeatorOutput]):
    """围绕一个方向产出 3–5 个选题（默认目标 4 个）。"""

    name = "ideator"
    profile_key = "ideator"
    schema_name = "ideator_result"
    output_model = IdeatorOutput

    async def run(self, ctx: AgentContext, payload: IdeatorInput) -> AgentResult[IdeatorOutput]:
        direction = payload.direction
        return await self._invoke(
            ctx,
            direction_title=direction.title,
            direction_rationale=direction.rationale,
            direction_grounding=direction_grounding_text(direction),
            existing_block=bullet_block(payload.existing_titles, empty="（暂无，放心写）"),
            per_direction=str(payload.per_direction),
        )
