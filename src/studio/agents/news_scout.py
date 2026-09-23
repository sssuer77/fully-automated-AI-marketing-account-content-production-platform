"""今日新闻侦察（T5.12 · 「经模型评测有写稿价值就保留在选题方向里」）。

为什么单独一个 Agent，而不是把这 50 条塞给 Planner
--------------------------------------------------
1. **去路不同**：Planner 产的是"这一批要做什么"的整体方向，且**另起一个批次**；这里产的是
   逐条判定 —— 哪几条新闻值得写，然后以「人工写方向」的同一条路落进**当前批次**，与手写
   的、模型产的方向排在一起（T4.3 裁定 130 的口径）。
2. **省钱**：评测只看标题 + 来源（几十条短文本），比 Planner 那次"读热点 + 读反馈 + 想
   5–8 个方向"轻得多。混在一起会让"只想看看今天有什么值得写"也付一次完整 Planner 的钱。
3. **可降级**：评测失败不该动到库里已有的方向（只有拿到 ``keep`` 才写库）。

喂进去的是"标题 + 源站摘要"
---------------------------
源站自己给的摘要（中新网 RSS 的 ``description``，正文第一段）一并喂给模型：事件总结要
**有据可依**，只给标题等于让它照着标题猜"到底发生了什么"，而猜出来的东西会被下游当成
事实用。头条热榜没有这个字段 ⇒ 那一条就只有标题，模型该老实写"信息不足"。

``ref`` 回抄纪律
----------------
``ref`` 用抓取侧生成的 ``n01`` / ``n02``…，模型必须原样回抄。服务层按 ``ref`` 对齐，对不上
的条目**丢弃而不是猜**（同 ``feedback_classifier``）：猜错会把"这条不值得写"当成"值得写"，
而代价是实打实多写一篇稿。
"""

from __future__ import annotations

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.domain.topics import NewsBatch, NewsItemSpec, NewsScoutResult

__all__ = ["NewsScoutAgent"]


class NewsScoutAgent(BaseAgent[NewsBatch, NewsScoutResult]):
    """逐条判定今日新闻值不值得写。"""

    name = "news_scout"
    #: 与 Planner 同通道：都是"读中文、判内容"的活，不该走最便宜的小模型
    profile_key = "planner"
    schema_name = "news_scout"
    output_model = NewsScoutResult

    async def run(self, ctx: AgentContext, payload: NewsBatch) -> AgentResult[NewsScoutResult]:
        if not payload.items:
            # 空批次不烧钱（也不该被当成失败）
            return AgentResult[NewsScoutResult](
                ok=True,
                data=NewsScoutResult(),
                warnings=["empty_batch"],
            )
        return await self._invoke(
            ctx,
            item_count=str(len(payload.items)),
            items_block=bullet_block(
                [_render_item(item) for item in payload.items],
                empty="（无）",
            ),
        )


def _render_item(item: NewsItemSpec) -> str:
    """一条新闻 → 提示词里的一行（有源站摘要就带上）。

    摘要**只来自源站**，抓不到就不写这一截 —— 不拿标题去凑一段"摘要"，那等于把幻觉
    提前到评测这一步（模型会以为那是原文）。
    """
    line = f"[{item.ref}]（{item.source}）{item.title}"
    return f"{line}｜摘要：{item.summary}" if item.summary else line
