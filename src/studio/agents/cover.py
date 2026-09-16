"""封面文案（Cover Agent）· T5.1 · §04.1.8 / §06.3。

它只写"封面上的那几个字"，不画图
----------------------------------
一次封面落地分成**两件不同的事**：写什么字（本模块）与把字画到图上
（:mod:`studio.publish.cover`，那一步是 ffmpeg 子进程）。拆开的理由与规格书一致：
文案可以被审稿与禁区扫描，合成可以被单测。把两者塞进一个 Agent，
模型输出就会和"这张图长什么样"绑死，改一次排版要重跑一次 LLM。

为什么路由是 local 优先
-----------------------
``config/llm.yaml`` 给 ``cover`` 配的是 ``{profile: local, fallback: cloud}``。
封面一共 20 个字，本地模型完全够用，而云端那条通道的预算应该留给写稿 ——
这是 R16（本地优先）在本项目里最没有争议的一处。

禁区为什么在这里再扫一遍
------------------------
提示词里已经写了"绝不触碰禁区"，但**提示词不是保证**：模型可能没照做。
而封面文案会随封面图一起发出去（合规红线 · R12），所以拿到结果之后
必须用**同一份规则通道**（:func:`studio.domain.script.find_forbidden`）再判一次。

判出命中 ⇒ **不重试**，直接把 ``ok`` 置假交回调用方。理由与 Writer 的禁区处理一致：
"改一处、犯另一处"是重试的常见结局，而封面的正确动作不是再赌一次，
是**退回规则兜底文案**（:func:`studio.domain.cover.rule_cover_output`）——
封面是可选装饰，它不值得让一条能发的片子卡在这里。
"""

from __future__ import annotations

from studio.agents.base import AgentContext, AgentResult, BaseAgent
from studio.core.errors import ErrorCode
from studio.domain.cover import CoverInput, CoverOutput, default_frame_at_ms
from studio.domain.script import find_forbidden

__all__ = ["CoverAgent"]


class CoverAgent(BaseAgent[CoverInput, CoverOutput]):
    """给这一期视频写封面主/次文案，并挑一个抽帧时刻。"""

    name = "cover"
    profile_key = "cover"
    schema_name = "cover_result"
    output_model = CoverOutput

    async def run(self, ctx: AgentContext, payload: CoverInput) -> AgentResult[CoverOutput]:
        result = await self._invoke(
            ctx,
            # 模型只需要知道"片子有多长"（把抽帧点钳在片子内）与"默认抽哪一帧"，
            # 不需要全文 —— 封面只有 20 字，给全文只会让"摘要"变成"缩写"。
            duration_ms=str(max(0, payload.duration_ms)),
            default_frame_ms=str(default_frame_at_ms(payload.hook_start_ms)),
            # 三个空串会让模板渲染出"标题：（空）"，模型很可能顺着这个空格往下编。
            # 显式写一句"（无标题）"，它才知道这是"这一期没写标题"而不是"忘了填"。
            script_title=payload.title or "（无标题）",
            script_hook=payload.hook or "（无钩子）",
            script_cta=payload.cta or "（无 CTA）",
        )
        if not result.ok or result.data is None:
            return result

        # 词表以调用方给的为准，**没给就退回 persona** —— 提示词里注入的是
        # ``persona.forbidden``（``persona_variables``），事后扫的若与之不是同一份，
        # 就会出现"提示词说了不许写、事后却没判"的错位，而那种错位没人看得出来。
        terms = list(payload.forbidden) or list(ctx.persona.forbidden)
        hits = find_forbidden(
            f"{result.data.title_text}{result.data.sub_text or ''}",
            terms,
        )
        if not hits:
            return result
        # ``model_copy`` 而不是新建一个 AgentResult：这一次调用**真的发生了**
        # （token 花了、账记了），把它抹成一份"没调过"的结果会让成本对不上账。
        return result.model_copy(
            update={
                "ok": False,
                "error_code": str(ErrorCode.PRECHECK_BANNED),
                "error_message": "封面文案命中禁区词：" + "、".join(hits),
                "warnings": [*result.warnings, "cover_banned:" + "、".join(hits)],
            }
        )
