"""Director（内容导演）· T1.10 · §04.1.4 / 原文 §2.2③。

一次 ``run`` = 一个选题 → 一份大纲
----------------------------------
```
① 组装输入块（选题 + 角度 + 目标时长）── 空变量也要显式给值，不留白
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ check_outline 过规则闸（段数 seq 连续 / 段字数合计 600–800 / 钩子与 CTA 非空）
④ 不合规 ⇒ 带着"哪里不合规"重试（≤2 轮）
⑤ 仍不合规 ⇒ **降级返回**（有结构就用），把问题写进 warnings
```

为什么第 ⑤ 步是"降级返回"
--------------------------
和 Planner 同一条理由：运营要的是"今天能拍的段子"，不是"一次完美的大纲"。
结构合法但字数差 30 字的大纲，比"0 段大纲 + 一条错误"有用得多 ——
而且字数最终由 Writer 决定，这里只是预分配（DoD 6：可降级、无静默失败）。
"""

from __future__ import annotations

from typing import ClassVar

from studio.agents.base import AgentContext, AgentResult, BaseAgent
from studio.domain.script import (
    FACTS_UNSET,
    OUTLINE_UNSET,
    DirectorInput,
    DirectorOutput,
    OutlineReport,
    check_outline,
)

__all__ = ["OUTLINE_RETRIES", "DirectorAgent"]

#: 大纲不合规时的重试次数（与 §04.1.2「丢弃并重试 ≤2」同一口径）
OUTLINE_RETRIES: int = 2


class DirectorAgent(BaseAgent[DirectorInput, DirectorOutput]):
    """把一个选题拆成"钩子 + 3–5 段 + CTA"的口播大纲。"""

    name = "director"
    profile_key = "director"
    schema_name = "director_result"
    output_model = DirectorOutput

    #: 规则不合规时的重试次数（测试里调小以免多跑几轮）
    outline_retries: ClassVar[int] = OUTLINE_RETRIES

    async def run(self, ctx: AgentContext, payload: DirectorInput) -> AgentResult[DirectorOutput]:
        topic = payload.topic
        hint = ""
        degraded: AgentResult[DirectorOutput] | None = None
        for _ in range(1 + self.outline_retries):
            result = await self._invoke(
                ctx,
                topic_title=topic.title,
                topic_angle=payload.angle or topic.angle,
                topic_reason=topic.reason,
                target_duration_ms=str(payload.target_duration_ms),
                target_seconds=str(round(payload.target_duration_ms / 1000)),
                outline_title=payload.outline_title or OUTLINE_UNSET,
                core_argument=payload.core_argument or OUTLINE_UNSET,
                facts=payload.facts or FACTS_UNSET,
                retry_hint=hint,
            )
            if not result.ok or result.data is None:
                return result

            report = check_outline(result.data)
            degraded = result.model_copy(update={"warnings": [*result.warnings, *_outline_warnings(report)]})
            if report.ok:
                return degraded
            hint = _retry_hint(report)

        assert degraded is not None  # 循环至少执行一次
        return degraded


def _outline_warnings(report: OutlineReport) -> list[str]:
    return [f"outline:{problem}" for problem in report.problems]


def _retry_hint(report: OutlineReport) -> str:
    return "上一版大纲不合格：" + report.describe() + "。请重写完整 JSON，不要解释。"
