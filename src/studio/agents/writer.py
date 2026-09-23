"""Writer（写稿）· T1.10 · §04.1.5 / 原文 §2.2④。

一次 ``run`` = 一份大纲 → 一份可念的稿子 + 逐句表
-------------------------------------------------
```
① 组装输入块（选题 + 大纲 + 改稿意见）
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ build_draft：**强制切分**超长句 ⇒ 再算字数 / 口癖 / 禁区 / 单人占比
④ 禁区命中 ⇒ **直接 block**（不重写、不进评分 —— 这是合规红线，不是文风问题）
⑤ 字数越界或口癖不足 ⇒ 带着"哪里不合格"重写（≤2 轮）
⑥ 仍不合格 ⇒ 取**最接近合格**的那一版返回 + warnings（不静默、不整批失败）
```

为什么"最接近"先按"还剩几项不合格"挑、再按字数距离挑
--------------------------------------------------
字数越界只有两种修法：重写或人工删改。既然重写额度用完了，就得挑一版**最省
人工**的：差 30 字比差 200 字好改。

**为什么不能只比字数**（真机 2026-09-23 暴露）：重写轮次里，模型常常把**别的**问题
修好、字数却没动 —— 两版的字数距离**相同**，而旧的"距离相同保留先出现"会把
**已经修好的那一版丢掉、留下旧的那一版**（"结尾不得提问"正好是这类问题：
改与不改都不动字数）。所以排序先看还剩几项不合格，字数差只在**同一档**里比。
同档同距才保留先出现的 —— "最后一次尝试"并不比第一次更可信。

为什么 block 不走重写
--------------------
禁区是**合规**问题（R12 封号风险）。重写有可能"改一处、犯另一处"，而运营看到的
必须是一句确定的"这稿不能用、因为踩了 X"，而不是一个被悄悄修过的稿子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.core.errors import ErrorCode
from studio.domain.script import (
    FACTS_UNSET,
    OUTLINE_UNSET,
    REWRITE_LIMIT,
    DirectorOutput,
    ScriptReport,
    ScriptRules,
    WriterInput,
    WriterOutput,
    build_draft,
    rewrite_hint,
)

__all__ = ["WriterAgent"]


class WriterAgent(BaseAgent[WriterInput, WriterOutput]):
    """把大纲写成 600–800 字口播稿，并给出逐句表。"""

    name = "writer"
    profile_key = "writer"
    schema_name = "script_result"
    output_model = WriterOutput

    #: 重写次数上限（§04.1.5：重写 ≤2 次；测试里可调小）
    rewrite_limit: ClassVar[int] = REWRITE_LIMIT

    async def run(self, ctx: AgentContext, payload: WriterInput) -> AgentResult[WriterOutput]:
        # 口癖与禁区**不在这里拼进提示词**：``persona_variables`` 已经把它们
        # 作为 ``{{catchphrases}}`` / ``{{forbidden}}`` 注入了（base.py）。
        # 再传一次会撞成 "got multiple values for keyword argument" —— 而且是
        # 两个真相（一处改 persona、一处改模板变量）。（T1.10 施工记录）
        rules = ScriptRules.from_persona(ctx.persona)
        catchphrases = list(ctx.persona.catchphrases)
        forbidden = list(ctx.persona.forbidden)
        topic = payload.topic

        hint = ""
        attempts: list[str] = []
        best: _Candidate | None = None
        for attempt in range(1 + self.rewrite_limit):
            result = await self._invoke(
                ctx,
                topic_title=topic.title,
                topic_angle=topic.angle,
                outline_hook=payload.outline.hook_3s,
                outline_segments=_render_segments(payload.outline),
                outline_cta=payload.outline.cta,
                outline_title=payload.outline_title or OUTLINE_UNSET,
                core_argument=payload.core_argument or OUTLINE_UNSET,
                facts=payload.facts or FACTS_UNSET,
                word_count_min=str(rules.word_count_min),
                word_count_max=str(rules.word_count_max),
                catchphrase_min_hits=str(rules.catchphrase_min_hits),
                revision_block=bullet_block(payload.revision_notes, empty="（本轮是新写，不是改稿）"),
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result

            _, report = build_draft(
                outline=payload.outline,
                output=result.data,
                catchphrases=catchphrases,
                forbidden=forbidden,
                rules=rules,
            )
            if report.blocked:
                return _blocked(result, report)

            candidate = _Candidate(
                result=result, output=result.data, report=report, distance=_distance(report, rules)
            )
            if best is None or candidate.distance < best.distance:
                best = candidate
            if report.ok:
                break
            attempts.append(f"rewrite:{attempt + 1}:{report.describe()}")
            hint = rewrite_hint(report)

        assert best is not None  # 循环至少执行一次
        report = best.report
        warnings = [
            *best.result.warnings,
            *attempts,
            *(f"script:{problem}" for problem in report.problems),
            *report.warnings,
        ]
        if report.split_count:
            warnings.append(f"sentence_split:{report.split_count}")
        warnings.extend(_self_report_warnings(best.output, report))
        return best.result.model_copy(update={"data": best.output, "warnings": warnings})


@dataclass(frozen=True, slots=True)
class _Candidate:
    """一次尝试的候选（按 :func:`_distance` 挑最省人工的一版）。"""

    result: AgentResult[WriterOutput]
    output: WriterOutput
    report: ScriptReport
    distance: tuple[int, int]


def _distance(report: ScriptReport, rules: ScriptRules) -> tuple[int, int]:
    """离合格有多远：``(还剩几项不合格, 字数差)`` —— **元组按字典序比**。

    先比"还剩几项"是必须的：只比字数时，"把别的问题修好了、字数没动"的那一版
    与旧版距离相同 ⇒ 会被"保留先出现的"丢掉（见模块 docstring）。
    """
    if report.word_count < rules.word_count_min:
        gap = rules.word_count_min - report.word_count
    elif report.word_count > rules.word_count_max:
        gap = report.word_count - rules.word_count_max
    else:
        gap = 0
    return (len(report.problems), gap)


def _blocked(result: AgentResult[WriterOutput], report: ScriptReport) -> AgentResult[WriterOutput]:
    """禁区命中 ⇒ 判失败（**不重写**：合规问题必须让人看见，不能悄悄修掉）。"""
    return result.model_copy(
        update={
            "ok": False,
            "error_code": str(ErrorCode.SCRIPT_FORBIDDEN),
            "error_message": "禁区命中：" + "、".join(report.forbidden_hits),
            "warnings": [*result.warnings, *(f"forbidden:{term}" for term in report.forbidden_hits)],
        }
    )


def _self_report_warnings(output: WriterOutput, report: ScriptReport) -> list[str]:
    """模型自报的口癖没被服务端复核到 ⇒ 记一笔（不当作失败，只是留痕）。"""
    reported = {item.strip() for item in output.catchphrases_used if item.strip()}
    extra = sorted(reported - set(report.catchphrase_hits))
    return [f"catchphrase_self_report:{term}" for term in extra]


def _render_segments(outline: DirectorOutput) -> str:
    return bullet_block(
        [
            f"第 {segment.seq} 段｜要点：{segment.point}｜画面：{segment.visual}"
            f"｜情绪：{segment.mood}｜约 {segment.est_chars} 字"
            for segment in outline.segments
        ],
        empty="（大纲没有段落）",
    )
