"""Planner（方向分析）· T1.9 · §04.1.2 / 原文 §2.2①。

一次 ``run`` 的完整流程
----------------------
```
① 组装输入块（热点 / 反馈摘要）── 空输入也要显式写"（暂无）"，不留白
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ plan_directions 过四条规则 ── 丢弃跑偏 / 降权被吐槽 / 校验 persona·want·hot
④ 不合规 ⇒ 带着"哪里不合规"重试（≤2 轮，§04.1.2「丢弃并重试 ≤2」）
⑤ 仍不合规 ⇒ **降级返回**（有方向就用），把违规写进 warnings
```

为什么第 ⑤ 步是"降级返回"而不是"整批失败"
------------------------------------------
运营打开选题面板要的是"今天能挑的方向"，不是"一次完美的 Planner 运行"。
4 个合规方向 + 一条 `rule_violation:want` 告警，比"0 个方向 + 一条错误"有用得多
（DoD 6：可降级、无静默失败 —— 告警就是"不静默"）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.domain.topics import (
    DIRECTION_COUNT_MIN,
    DirectionSpec,
    FeedbackDigest,
    HotItemSpec,
    PlannerInput,
    PlannerOutput,
    plan_directions,
)

__all__ = ["PLANNER_RULE_RETRIES", "PlannerAgent"]

#: §04.1.2：「丢弃并重试 ≤2」
PLANNER_RULE_RETRIES: int = 2


class PlannerAgent(BaseAgent[PlannerInput, PlannerOutput]):
    """方向分析（5–8 个方向 + 四条规则机器化校验）。"""

    name = "planner"
    profile_key = "planner"
    schema_name = "planner_result"
    output_model = PlannerOutput

    #: 规则不合规时的重试次数（可在测试里调小以免多跑几轮）
    rule_retries: ClassVar[int] = PLANNER_RULE_RETRIES

    async def run(self, ctx: AgentContext, payload: PlannerInput) -> AgentResult[PlannerOutput]:
        hot = [item for item in payload.hot_items if item.parse_ok]
        digest = payload.feedback_digest
        hot_block = _render_hot(hot)
        feedback_block = _render_feedback(digest)
        feedback_available = digest is not None and not digest.is_empty

        hint = ""
        degraded: AgentResult[PlannerOutput] | None = None
        for _ in range(1 + self.rule_retries):
            result = await self._invoke(
                ctx,
                hot_block=hot_block,
                feedback_block=feedback_block,
                count_min=str(payload.count_min),
                count_max=str(payload.count_max),
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result

            kept, report = plan_directions(
                result.data.directions,
                hot_available=bool(hot),
                feedback_available=feedback_available,
            )
            warnings = [*result.warnings]
            if report.dropped:
                warnings.append(f"directions_dropped:{len(report.dropped)}")
            if report.demoted:
                warnings.append(f"directions_demoted:{len(report.demoted)}")
            if report.low_grounding:
                warnings.append("low_grounding")
            warnings.extend(f"rule_violation:{item.rule}" for item in report.violations)

            degraded = result.model_copy(
                update={
                    "data": PlannerOutput(directions=kept, rule_report=report),
                    "warnings": warnings,
                }
            )
            if report.ok and len(kept) >= DIRECTION_COUNT_MIN:
                return degraded
            hint = report.describe()

        assert degraded is not None  # 循环至少执行一次
        return degraded


def _render_hot(items: Sequence[HotItemSpec]) -> str:
    """热点块（**坏行不进来**：调用方已按 ``parse_ok`` 过滤）。"""
    return bullet_block(
        [
            item.title
            + (f"（热度 {item.heat_raw}）" if item.heat_raw else "")
            + (f"（{item.platform}）" if item.platform else "")
            for item in items
        ],
        empty="（本次没有导入热点）",
    )


def _render_feedback(digest: FeedbackDigest | None) -> str:
    """反馈摘要块（分桶 + 高频词，都是机器算好的，**不额外调 LLM**）。"""
    if digest is None or digest.is_empty:
        return "（本次没有历史反馈）"
    parts = [
        f"共 {digest.total} 条",
        "【用户想要】\n" + bullet_block([item.text for item in digest.wants], empty="（无）"),
        "【用户吐槽】\n" + bullet_block([item.text for item in digest.complaints], empty="（无）"),
    ]
    if digest.trends:
        parts.append("【趋势提及】\n" + bullet_block([item.text for item in digest.trends], empty="（无）"))
    if digest.top_topics:
        parts.append("【高频词】" + "、".join(f"{word}×{count}" for word, count in digest.top_topics))
    return "\n".join(parts)


def direction_grounding_text(direction: DirectionSpec) -> str:
    """把 ``grounded_on`` 渲染成一行行"依据"（Ideator 的输入块复用）。"""
    return bullet_block(
        [
            {
                "persona": "账号定位",
                "hot": "热点",
                "feedback": "历史反馈",
            }[ref.type]
            + (f"/{ref.kind}" if ref.kind else "")
            + (f"：{ref.quote}" if ref.quote else "")
            for ref in direction.grounded_on
        ],
        empty="（无）",
    )
