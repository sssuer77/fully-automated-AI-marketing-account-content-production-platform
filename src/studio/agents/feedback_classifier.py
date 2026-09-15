"""反馈分类（T1.9 · §04.1.7「情感 / 诉求分类由 Planner 阶段批量做一次」）。

为什么单独一个 Agent，而不是塞进 Planner 的一次调用
--------------------------------------------------
1. **契约要求**：§04.1.2 的 ``PlannerInput.feedback_digest`` 是 Planner 的**输入**
   ⇒ 摘要必须在 Planner 之前就存在；
2. **可降级**：分类失败不该让"出方向"跟着失败。分成两次调用，分类挂了还能退到
   规则兜底（``services/topic_service.py`` 的关键词表），Planner 照跑；
3. **省钱**：没有反馈时**一次都不调**（本地优先 · R16）。

``ref`` 回抄纪律
----------------
``ref`` 用 ``feedback_items.id``，模型必须原样回抄。服务层按 ``ref`` 对齐回填，
对不上的条目**丢弃而不是猜** —— 猜错会把"想看的"写成"吐槽的"，
比少填一条严重得多。
"""

from __future__ import annotations

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.domain.topics import FeedbackBatch, FeedbackClassification

__all__ = ["FeedbackClassifierAgent"]


class FeedbackClassifierAgent(BaseAgent[FeedbackBatch, FeedbackClassification]):
    """批量判定情感 + 抽取"想要 / 吐槽"。"""

    name = "feedback_classifier"
    #: 与 Planner 同通道：都是"读中文、想内容"的活，且都不该走最便宜的小模型
    profile_key = "planner"
    schema_name = "feedback_classification"
    output_model = FeedbackClassification

    async def run(self, ctx: AgentContext, payload: FeedbackBatch) -> AgentResult[FeedbackClassification]:
        if not payload.items:
            # 空批次不烧钱（也不该被当成失败）
            return AgentResult[FeedbackClassification](
                ok=True,
                data=FeedbackClassification(),
                warnings=["empty_batch"],
            )
        return await self._invoke(
            ctx,
            item_count=str(len(payload.items)),
            items_block=bullet_block(
                [
                    f"[{item.ref}]" + (f"（{item.platform}）" if item.platform else "") + f" {item.text}"
                    for item in payload.items
                ],
                empty="（无）",
            ),
        )
