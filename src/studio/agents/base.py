"""通用 Agent 契约（T1.8 · §04.1.1）。

分层与职责
----------
```
BaseAgent（本模块）    ← 渲染提示词 + 声明 schema / 路由键 / 输出模型
        │
        ▼
LlmGateway（gateway.py）← 预算闸门 · 通道选择 · 修复重试 · 熔断 · 记账
        │
        ▼
LlmTransport（llm_client.py）← 一次 HTTP 往返
```

统一实现在 :meth:`BaseAgent._invoke`（**禁止各 Agent 重复实现**）：
1) 渲染 ``system`` + ``user``（注入 persona；模板变更 ⇒ ``prompt_version`` 变化）
2) 检查 token 预算（R16）⇒ 超限按 ``budget.on_exceed`` 处理
3) 按 ``llm.yaml`` routing 选通道（cloud → local 兜底）
4) ``json_guard`` 校验 schema；失败 ⇒ 修复重试 ≤3（把错误回灌给模型）
5) 通道失败 ⇒ fallback 通道；仍失败 ⇒ 返回 ``error_code``（**不抛裸异常**）
6) 写 ``llm_calls``（token / 成本 / 耗时 / model / prompt_version）+ 日志

硬约束
------
- Agent **不得**直接写 ``tasks.status``（只能返回结果，由 pipeline stage 决定迁移）。
- ``raw_text`` 只留 8KB 截断（排障用，不入库全文）。
- 校验是**两层**：JSON Schema（``schemas/*.schema.json``，与 §04 条款逐条对应的契约）
  + 输出模型（pydantic，运行期类型）。两层都在网关的**同一个修复循环**里跑，
  所以"schema 过了但模型不过"也会带着错误回灌重试，而不是直接抛异常。

与 §04.1.1 的差异（T1.8 裁定 56）
--------------------------------
``AgentResult`` 增补两个**只增不改**的字段：``profile``（哪条通道给的答案）与
``error_message``（人类可读原因）。原字段一个都没动，前端与契约测试照旧。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Final, cast

from pydantic import BaseModel, ConfigDict, Field

from studio.agents.prompts import PromptLibrary
from studio.core.config import PersonaConfig

if TYPE_CHECKING:  # 运行期是 gateway → base 的单向依赖，反向只用于类型标注
    from studio.agents.gateway import LlmGateway

__all__ = [
    "DEFAULT_SYSTEM_BLOCKS",
    "DEFAULT_USER_BLOCKS",
    "RAW_TEXT_LIMIT",
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "bullet_block",
    "persona_variables",
]

#: ``AgentResult.raw_text`` 的截断长度（§04.1.1）
RAW_TEXT_LIMIT: int = 8 * 1024

#: 所有 Agent 默认注入的**系统块**（通用输出纪律）
DEFAULT_SYSTEM_BLOCKS: Final[tuple[str, ...]] = ("shared.json_contract",)

#: 所有 Agent 默认注入的**用户块**（频道定位；manifest 注明"user 消息前缀"）
DEFAULT_USER_BLOCKS: Final[tuple[str, ...]] = ("shared.persona_block",)


def persona_variables(persona: PersonaConfig) -> dict[str, str]:
    """persona → 提示词变量（字段名与 ``prompts/shared/persona_block.md`` 逐字对应）。

    由 :meth:`BaseAgent._invoke` **自动合并**，Agent 不需要自己拼 ——
    漏传一个变量会让 :func:`render_template` 直接报错（不静默渲染成空串），
    与其每个 Agent 各踩一次，不如统一注入。
    """
    return {
        "persona_name": persona.name,
        "role_desc": persona.role_desc,
        "tone": persona.tone,
        "audience": persona.audience,
        "catchphrases": "、".join(persona.catchphrases),
        "forbidden": "、".join(persona.forbidden),
        "speaker_names": "、".join(persona.speaker_names) or "（未配置）",
        "style_hint": persona.style_hint or "（无特别要求）",
    }


def bullet_block(lines: Sequence[str], *, empty: str) -> str:
    """把若干行拼成提示词里的列表块。

    ``empty`` 是**必填**的：空白块会让模型以为"输入是空的"，而实际上
    可能是"这一类输入本来就没有"。显式写一句"（暂无热点）"才不会误导。
    """
    if not lines:
        return empty
    return "\n".join(f"- {line}" for line in lines)


class AgentContext(BaseModel):
    """所有 Agent 的公共输入（persona 与预算**必须**注入，避免提示词与校验脱节）。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    job_id: str | None = None
    persona: PersonaConfig
    trace_id: str
    seed: int | None = None
    token_budget_remaining: int | None = None


class AgentResult[TOut: BaseModel](BaseModel):
    """统一返回（成功与否都返回它，**不抛裸异常**）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: TOut | None = None
    raw_text: str | None = None
    engine: str = ""
    model: str = ""
    prompt_version: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 0
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    # ── 以下两个字段是对 §04.1.1 的**增补**（只增不改，见模块 docstring）──
    profile: str = ""
    error_message: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BaseAgent[TIn: BaseModel, TOut: BaseModel](ABC):
    """所有 Agent 的基类（子类只声明元数据 + ``run``）。"""

    #: Agent 名（也是 ``prompts`` 注册名；落 ``llm_calls.agent`` 用 ``profile_key``）
    name: ClassVar[str]
    #: ``config/llm.yaml`` 的 ``routing`` 键
    profile_key: ClassVar[str]
    #: ``schemas/<schema_name>.schema.json``
    schema_name: ClassVar[str]
    #: 输出模型（运行期类型；与 schema 文件同一份契约的两面）
    output_model: ClassVar[type[BaseModel]]
    #: 注入到 ``system`` 前部的公共块（默认通用输出纪律）
    shared_system_blocks: ClassVar[tuple[str, ...]] = DEFAULT_SYSTEM_BLOCKS
    #: 注入到 ``user`` 前部的公共块（默认频道定位）
    shared_user_blocks: ClassVar[tuple[str, ...]] = DEFAULT_USER_BLOCKS

    def __init__(self, gateway: LlmGateway, prompts: PromptLibrary) -> None:
        self._gateway = gateway
        self._prompts = prompts

    @property
    def gateway(self) -> LlmGateway:
        return self._gateway

    @property
    def prompts(self) -> PromptLibrary:
        return self._prompts

    @abstractmethod
    async def run(self, ctx: AgentContext, payload: TIn) -> AgentResult[TOut]:
        """执行该 Agent（子类实现：组装变量 → 调 :meth:`_invoke` → 后置校验）。"""

    def _parse(self, data: object) -> TOut:
        """把校验过的 JSON 转成输出模型（失败 ⇒ pydantic 报错 ⇒ 网关回灌重试）。"""
        return cast("TOut", self.output_model.model_validate(data))

    async def _invoke(
        self,
        ctx: AgentContext,
        *,
        prompt_name: str | None = None,
        **variables: str,
    ) -> AgentResult[TOut]:
        """统一调用路径：渲染提示词 → 网关（预算 / 通道 / 重试 / 记账）→ 结果。

        persona 变量在这里**自动合并**（见 :func:`persona_variables`），
        所以子类只需要传自己模板里的额外变量。
        """
        rendered = self._prompts.compose(
            prompt_name or self.name,
            system_blocks=self.shared_system_blocks,
            user_blocks=self.shared_user_blocks,
            **persona_variables(ctx.persona),
            **variables,
        )
        return await self._gateway.complete(
            agent=self.profile_key,
            ctx=ctx,
            prompt=rendered,
            schema_name=self.schema_name,
            parse=self._parse,
        )
