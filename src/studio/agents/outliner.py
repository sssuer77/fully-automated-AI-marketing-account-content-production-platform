"""Outliner（选题定稿人）· 文案三级流水线的**第二级**。

一次 ``run`` = 一个选题 → 一条「视频标题 + 核心论点」
---------------------------------------------------
```
① 组装输入块（选题标题 + 角度 + 为什么做它）—— 空变量也要显式给值，不留白
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ 返回 OutlineOutput（结构由 JSON Schema 与 pydantic 两道闸门兜住）
```

为什么这一级**现在有**规则重试（2026-09-23 追加）
--------------------------------------------
原先的理由是"除了长度没有可机检的东西"。现在多了一条**当场可判的事实**：
标题里不得出现说话人标签（用户口径：这个账号只是借这两个角色的口讨论社会问题，
观众能看到的字里不该出现"谁在说"）。有可机检的规则就要接闸 —— 否则那一条判据
只是一句注释，真机那条「…，熊大：凉的不是他一个人的心」照样会落库。

⚠️ 但"标题够不够得体、够不够书面"仍然是**主观**判断，这一级不装那道假闸门：
那一条归提示词与 Reviewer。
"""

from __future__ import annotations

from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent
from studio.domain.script import (
    FACTS_UNSET,
    OutlineInput,
    OutlineOutput,
    OutlineReport,
    check_outline_title,
)

__all__ = ["TITLE_RETRIES", "OutlinerAgent"]

#: 标题不合规时的重写次数（与 Director 的 ``OUTLINE_RETRIES`` 同口径）
TITLE_RETRIES: int = 2


class OutlinerAgent(BaseAgent[OutlineInput, OutlineOutput]):
    """把一条选题收成「视频标题 + 核心论点」。"""

    name = "outliner"
    profile_key = "outliner"
    schema_name = "outline_result"
    output_model = OutlineOutput

    title_retries: ClassVar[int] = TITLE_RETRIES

    async def run(self, ctx: AgentContext, payload: OutlineInput) -> AgentResult[OutlineOutput]:
        topic = payload.topic
        hint = ""
        last: AgentResult[OutlineOutput] | None = None
        for _ in range(1 + self.title_retries):
            result = await self._invoke(
                ctx,
                topic_title=topic.title,
                topic_angle=payload.angle or topic.angle,
                topic_reason=topic.reason,
                facts=payload.facts or FACTS_UNSET,
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result
            report = check_outline_title(result.data.title, names=tuple(ctx.persona.speaker_names))
            last = result.model_copy(update={"warnings": [*result.warnings, *_title_warnings(report)]})
            if report.ok:
                return last
            hint = "上一版标题不合格：" + report.describe() + "。请重写完整 JSON，不要解释。"

        assert last is not None  # 循环至少执行一次
        return last


def _title_warnings(report: OutlineReport) -> list[str]:
    return [f"outline_title:{problem}" for problem in report.problems]
