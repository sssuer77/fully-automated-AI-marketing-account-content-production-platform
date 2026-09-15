"""Editor（改稿编辑）· T1.11 · §04.1.6 / 原文 §2.2⑥。

一次 ``run`` = 稿件 + 审稿意见 → 改后的完整稿件
---------------------------------------------
```
① 把 issues 渲染成"只改这些"的清单（target 逐条列出）
② 网关调用
③ check_edit 逐句比对：没被指到的句子改了 ⇒ 越界
④ 越界 ⇒ 带"你改了不该改的第 N 句"重试 ≤1 次
⑤ 仍越界 ⇒ **照收**，把问题写进 warnings 交给人工
```

为什么越界是"重试 1 次后照收"而不是"直接失败"
--------------------------------------------
两种错法的代价不对称：

- 照收一个"多润色了一句"的稿子：人看面板时能看到 ``edited:out_of_scope`` 标记，
  大不了退回重改 —— 稿子本身**能用**；
- 直接判失败：这一轮改稿白烧，任务掉进 ``failed``，而问题可能只是模型顺手改了个标点。

DoD 6（可降级、无静默失败）要的正是这个：**降级 + 留痕**，而不是"要么完美要么崩"。

为什么整份回写而不是返回 diff
-----------------------------
diff 的合并逻辑（"第 3 句的第 5 个字换成 X"）需要一套自己的补丁格式与冲突处理，
而稿子只有 600–800 字：整份回写的 token 成本可以接受，换来的是"**改后的稿子就是
一个完整、可校验的 WriterOutput**"—— 它能直接过 schema、直接落库、直接喂 TTS。
"""

from __future__ import annotations

from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent, bullet_block
from studio.domain.scoring import (
    EDIT_RETRIES,
    EditorInput,
    EditorOutput,
    ReviewIssue,
    check_edit,
)
from studio.domain.script import DirectorOutput, ScriptRules, WriterOutput

__all__ = ["EditorAgent"]


class EditorAgent(BaseAgent[EditorInput, EditorOutput]):
    """按审稿意见定点改稿（≤2 轮，轮次由服务层把关）。"""

    name = "editor"
    profile_key = "editor"
    schema_name = "editor_result"
    output_model = EditorOutput

    #: 越界改动后的纠正重试次数（轮次预算另算，见 ``scoring.REVISION_LIMIT``）
    edit_retries: ClassVar[int] = EDIT_RETRIES

    async def run(self, ctx: AgentContext, payload: EditorInput) -> AgentResult[EditorOutput]:
        rules = ScriptRules.from_persona(ctx.persona)
        outline = payload.outline
        topic = payload.topic
        hint = ""
        warnings: list[str] = []
        last: AgentResult[EditorOutput] | None = None

        for _ in range(1 + self.edit_retries):
            result = await self._invoke(
                ctx,
                topic_title=topic.title,
                round_no=str(payload.round_no),
                outline_hook=outline.hook_3s,
                outline_segments=_render_segments(outline),
                outline_cta=outline.cta,
                issue_block=_render_issues(payload.issues),
                script_title=payload.script.title,
                script_hook=payload.script.hook,
                script_body=payload.script.body_md,
                script_cta=payload.script.cta,
                sentence_block=_render_sentences(payload.script),
                word_count_min=str(rules.word_count_min),
                word_count_max=str(rules.word_count_max),
                catchphrase_min_hits=str(rules.catchphrase_min_hits),
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result

            report = check_edit(
                payload.script,
                result.data.script,
                payload.issues,
                segments=len(outline.segments),
            )
            last = result.model_copy(
                update={
                    "warnings": [
                        *result.warnings,
                        *warnings,
                        *report.warnings,
                        *(f"edit:{problem}" for problem in report.problems),
                    ]
                }
            )
            if report.ok:
                return last
            warnings = [f"edit:retry:{report.describe()}"]
            hint = "上一版改了不该改的地方：" + report.describe() + "。只改被指出的问题，重发完整 JSON。"

        assert last is not None  # 循环至少执行一次
        return last


def _render_issues(issues: list[ReviewIssue]) -> str:
    """把 issues 渲染成"只改这些"的清单（``target`` 必须显式写出来）。"""
    return bullet_block(
        [
            f"`{issue.target}`｜[{issue.severity}] {issue.code}：{issue.detail} ⇒ 建议：{issue.suggestion}"
            for issue in issues
        ],
        empty="（本轮没有指出任何问题 —— 那就原样交回，一个字都不要改）",
    )


def _render_sentences(script: WriterOutput) -> str:
    return bullet_block(
        [f"第 {item.seq} 句（{item.speaker}）：{item.text}" for item in script.sentences],
        empty="（没有逐句表）",
    )


def _render_segments(outline: DirectorOutput) -> str:
    return bullet_block(
        [f"第 {segment.seq} 段｜要点：{segment.point}｜情绪：{segment.mood}" for segment in outline.segments],
        empty="（大纲没有段落）",
    )
