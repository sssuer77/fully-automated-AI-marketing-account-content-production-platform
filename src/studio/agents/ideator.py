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

标题闸（2026-09-23 追加）
------------------------
候选标题就是**最终的视频标题**，所以它和二级、三级吃同一条纪律：**观众看到的字里
不许出现角色名**。真机踩到的形态是「护工钱谁掏？熊二问完这句，熊大把账分成了两笔」
—— 那是一条"关于两个角色"的标题，而不是"关于这件事"的标题。

处理分两步：① 带问题重写（≤2 轮，与 Director 同口径）；② 仍带名字的那几条**丢掉**
并留痕 —— 一条标题里挂着角色名的选题，运营还得手改一次，不如让位给别的候选。
"""

from __future__ import annotations

from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.agents.planner import direction_grounding_text
from studio.domain.script import check_outline_title
from studio.domain.topics import IdeatorInput, IdeatorOutput

__all__ = ["TITLE_RETRIES", "IdeatorAgent"]

#: 标题不合规时的重写次数（与 Director 的 ``OUTLINE_RETRIES`` 同口径）
TITLE_RETRIES: int = 2


class IdeatorAgent(BaseAgent[IdeatorInput, IdeatorOutput]):
    """围绕一个方向产出 3–5 个选题（默认目标 4 个）。"""

    name = "ideator"
    profile_key = "ideator"
    schema_name = "ideator_result"
    output_model = IdeatorOutput

    title_retries: ClassVar[int] = TITLE_RETRIES

    async def run(self, ctx: AgentContext, payload: IdeatorInput) -> AgentResult[IdeatorOutput]:
        direction = payload.direction
        names = tuple(ctx.persona.speaker_names)
        hint = ""
        last: AgentResult[IdeatorOutput] | None = None
        for _ in range(1 + self.title_retries):
            result = await self._invoke(
                ctx,
                direction_title=direction.title,
                direction_rationale=direction.rationale,
                direction_grounding=direction_grounding_text(direction),
                existing_block=bullet_block(payload.existing_titles, empty="（暂无，放心写）"),
                per_direction=str(payload.per_direction),
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result
            offenders = [
                item.title
                for item in result.data.topics
                if not check_outline_title(item.title, names=names).ok
            ]
            if not offenders:
                return result
            last = result
            hint = (
                "上一版这些标题不合格："
                + "；".join(offenders)
                + '。标题里不许出现角色名，也不许写成"谁说了什么"。请重写完整 JSON，不要解释。'
            )

        assert last is not None  # 循环至少执行一次
        return _drop_offenders(last, names)


def _drop_offenders(result: AgentResult[IdeatorOutput], names: tuple[str, ...]) -> AgentResult[IdeatorOutput]:
    """重写用尽仍带角色名 ⇒ **把那几条丢掉**（运营还得手改一次，不如让位给别的候选）。

    ⚠️ 但**不许丢到少于 3 条**：``IdeatorOutput.topics`` 的下限就是 3，丢破了下限
    等于把"标题里有名字"升级成"这一批选题没了"。真到那一步就整批留着 + 每条一个
    警告，让面板把问题摆出来 —— 少给两条是静默失败，留着才是可处理的失败。
    """
    data = result.data
    if data is None:  # pragma: no cover - 调用点已保证非空
        return result
    keep = [item for item in data.topics if check_outline_title(item.title, names=names).ok]
    dropped = [item.title for item in data.topics if not check_outline_title(item.title, names=names).ok]
    if len(keep) < 3:
        warnings = [*result.warnings, *(f"ideator:name_leak:{title}" for title in dropped)]
        return result.model_copy(update={"warnings": warnings})
    warnings = [*result.warnings, *(f"ideator:dropped:{title}" for title in dropped)]
    return result.model_copy(update={"data": data.model_copy(update={"topics": keep}), "warnings": warnings})
