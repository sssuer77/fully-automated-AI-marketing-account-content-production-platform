"""Outliner（选题定稿人）· 文案三级流水线的**第二级**。

一次 ``run`` = 一个选题 → 一条「视频标题 + 核心论点」
---------------------------------------------------
```
① 组装输入块（选题标题 + 角度 + 为什么做它）—— 空变量也要显式给值，不留白
② 网关调用（预算 → 通道 → 修复重试 → 记账）
③ 返回 OutlineOutput（结构由 JSON Schema 与 pydantic 两道闸门兜住）
```

为什么这一级**不做**规则重试
----------------------------
Director 有 ``outline_retries``（段数 / 字数合计 / 时长区间全是可校验的硬规则），
这一级的产物只有两个自由文本字段 —— 除了长度没有可机检的东西，而长度已经由
schema 卡住（超长会走网关的修复重试）。再包一层"不合规就重写"只是在给
"标题不够好"这种**主观**判断装一个假的客观闸门。
"""

from __future__ import annotations

from studio.agents.base import AgentContext, AgentResult, BaseAgent
from studio.domain.script import OutlineInput, OutlineOutput

__all__ = ["OutlinerAgent"]


class OutlinerAgent(BaseAgent[OutlineInput, OutlineOutput]):
    """把一条选题收成「视频标题 + 核心论点」。"""

    name = "outliner"
    profile_key = "outliner"
    schema_name = "outline_result"
    output_model = OutlineOutput

    async def run(self, ctx: AgentContext, payload: OutlineInput) -> AgentResult[OutlineOutput]:
        topic = payload.topic
        return await self._invoke(
            ctx,
            topic_title=topic.title,
            topic_angle=payload.angle or topic.angle,
            topic_reason=topic.reason,
        )
