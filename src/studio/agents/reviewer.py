"""Reviewer（审稿人）· T1.11 · §04.1.6 / 原文 §2.2⑤。

一次 ``run`` = 一份稿件 → 六维度评分 + 问题清单
---------------------------------------------
```
① 组装输入块（稿件全文 + 大纲 + 服务端已算好的规则明细）
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ 六维度各 0–10 + issues（target 是机器可读落点）
④ 结构合法性由 schema 保证；"低分却没列问题"只留痕，不拦
```

为什么 Agent 只返回六维度与 issues
----------------------------------
``total`` / ``grade`` / ``verdict`` **不在这里**（裁定 89）：

- 规则通道是服务端算的（可复算的事实），模型看不到全貌，凭什么报总分；
- 一旦让模型报总分，"0.3×规则 + 0.7×LLM"这条硬契约就没人守 —— 它写 9.5
  而实际算出来是 6.3 时，我们只能二选一，而两个都不对。

所以 :class:`ReviewerOutput` 只有 ``llm_detail`` 与 ``issues``；定级在
``services/review_service.py`` 里由 :func:`studio.domain.scoring.compute_score` 完成。

为什么"分数与 issues 对不上"不做硬闸
------------------------------------
"某维度打了 4 分却一条问题都没列"确实是模型偷懒，但它**不影响正确性**：总分照算，
人看面板时分数与问题清单并排显示，矛盾一眼可见。为它加一轮重试，烧的是真钱（R16）。
故只记 ``review:score_issue_mismatch:<维度>`` 留痕。
"""

from __future__ import annotations

from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.domain.scoring import (
    LLM_DIMENSIONS,
    ReviewerInput,
    ReviewerOutput,
    RuleChannelDetail,
)
from studio.domain.script import DirectorOutput, ScriptRules

__all__ = ["MISMATCH_THRESHOLD", "ReviewerAgent"]

#: 低于这个分数的维度**应当**在 issues 里有对应问题（只留痕，不拦）
MISMATCH_THRESHOLD: float = 6.0


class ReviewerAgent(BaseAgent[ReviewerInput, ReviewerOutput]):
    """给一份口播稿打六维度分并列出问题。"""

    name = "reviewer"
    profile_key = "reviewer"
    schema_name = "review_result"
    output_model = ReviewerOutput

    #: 六维度里"该有意见却没意见"的判定线（测试里可调）
    mismatch_threshold: ClassVar[float] = MISMATCH_THRESHOLD

    async def run(self, ctx: AgentContext, payload: ReviewerInput) -> AgentResult[ReviewerOutput]:
        rules = ScriptRules.from_persona(ctx.persona)
        outline = payload.outline
        script = payload.script
        detail = payload.rule_detail
        result = await self._invoke(
            ctx,
            topic_title=payload.topic.title,
            topic_angle=payload.topic.angle,
            round_no=str(payload.round_no),
            segment_count=str(len(outline.segments)),
            outline_hook=outline.hook_3s,
            outline_segments=_render_segments(outline),
            outline_cta=outline.cta,
            script_title=script.title,
            script_hook=script.hook,
            script_body=script.body_md,
            script_cta=script.cta,
            chars=str(detail.chars),
            word_count_min=str(rules.word_count_min),
            word_count_max=str(rules.word_count_max),
            catchphrase_hits=str(detail.catchphrases_hit),
            catchphrase_min_hits=str(rules.catchphrase_min_hits),
            dup_ratio=f"{detail.paragraph_dup_ratio:.2f}",
            banned_summary=banned_summary(detail),
        )
        if not result.ok or result.data is None:
            return result

        warnings = [*result.warnings, *_mismatch_warnings(result.data, self.mismatch_threshold)]
        return result.model_copy(update={"warnings": warnings})


def banned_summary(detail: RuleChannelDetail) -> str:
    """禁区扫描结论（人话；命中就把词与次数列出来 —— "未命中"与"命中 3 次"不是一回事）。"""
    if not detail.banned_hits:
        return "未命中"
    return "、".join(f"{item.term}×{item.count}" for item in detail.banned_hits)


def _mismatch_warnings(output: ReviewerOutput, threshold: float) -> list[str]:
    """低分维度却**一条问题都没列**的留痕（有 issues 就不管，见模块 docstring）。"""
    if output.issues:
        return []
    return [
        f"review:score_issue_mismatch:{name}"
        for name in LLM_DIMENSIONS
        if getattr(output.llm_detail, name).score < threshold
    ]


def _render_segments(outline: DirectorOutput) -> str:
    return bullet_block(
        [
            f"第 {segment.seq} 段｜要点：{segment.point}｜画面：{segment.visual}"
            f"｜情绪：{segment.mood}｜约 {segment.est_chars} 字"
            for segment in outline.segments
        ],
        empty="（大纲没有段落）",
    )
