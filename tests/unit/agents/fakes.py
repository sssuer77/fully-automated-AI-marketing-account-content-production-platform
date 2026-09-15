"""LLM 网关测试共用的假件与构造器（T1.8）。

为什么放独立模块而不是 ``conftest.py``
------------------------------------
这些是**数据构造器**（造配置、造库、造脚本化传输），测试里当普通函数用最直观；
``tests/unit/agents/__init__.py`` 保证模块名唯一（T1.7 裁定 53）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from studio.agents.base import AgentContext
from studio.agents.budget import TokenBudget
from studio.agents.cost import LlmCallStore
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.agents.llm_client import ChatMessage, LlmCall, LlmResponse, LlmUsage, is_retryable_status
from studio.agents.prompts import PromptLibrary, RenderedPrompt
from studio.core.config import LlmConfig, PersonaConfig
from studio.core.errors import ErrorCode, LlmTransportError
from studio.core.ids import sha256_hex
from studio.core.paths import StudioPaths
from studio.db import connect, migrate

__all__ = [
    "INVALID_TEXT",
    "SCHEMA",
    "VALID_TEXT",
    "DemoOutput",
    "FakeClock",
    "ScriptedTransport",
    "agent_gateway",
    "build_gateway",
    "demo_prompt",
    "http_error",
    "llm_config",
    "make_context",
    "make_db",
    "make_prompts",
    "parse_demo",
    "persona",
    "timeout_error",
    "write_schema",
]

#: 一个最小的"输出契约"：必须有 ``title`` 字符串
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1}},
    "required": ["title"],
    "additionalProperties": False,
}

VALID_TEXT = '{"title": "熊大熊二跑酷"}'
INVALID_TEXT = '{"title": 123}'


class DemoOutput(BaseModel):
    """测试用输出模型（与 :data:`SCHEMA` 同一份契约）。"""

    model_config = ConfigDict(extra="forbid")

    title: str


def parse_demo(data: object) -> DemoOutput:
    return DemoOutput.model_validate(data)


def demo_prompt(prompts: PromptLibrary, *, topic: str = "MC跑酷") -> RenderedPrompt:
    return prompts.render("demo", persona_name="熊大熊二", topic=topic)


def http_error(
    status: int,
    *,
    retryable: bool | None = None,
    code: ErrorCode | None = None,
) -> LlmTransportError:
    """构造一个传输错误（``retryable`` 省略时按状态码判定，与生产同规则）。"""
    return LlmTransportError(
        f"HTTP {status}",
        code=code or (ErrorCode.LLM_RATE_LIMIT if status == 429 else ErrorCode.LLM_UPSTREAM),
        retryable=is_retryable_status(status) if retryable is None else retryable,
        status=status,
    )


def timeout_error() -> LlmTransportError:
    return LlmTransportError("超时", code=ErrorCode.LLM_TIMEOUT, retryable=True)


def write_schema(root: Path, name: str = "demo", schema: dict[str, Any] | None = None) -> Path:
    """在 ``root`` 下写一份 ``<name>.schema.json``。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.schema.json"
    payload = schema if schema is not None else SCHEMA
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def persona() -> PersonaConfig:
    """一套合法人物（字段长度满足 ``PersonaConfig`` 校验）。"""
    return PersonaConfig(
        id="persona_default",
        name="熊大熊二·MC跑酷",
        role_desc="两只熊的跑酷解说搭档",
        tone="嘴碎、互相拆台",
        audience="中小学生与怀旧玩家",
        catchphrases=["这不科学", "俺寻思"],
        forbidden=["脏话", "政治"],
    )


def llm_config(
    *,
    cloud_retries: int = 3,
    local_retries: int = 2,
    cloud_timeout: int = 120,
    local_timeout: int = 300,
    cloud_cost_in: float = 0.0,
    cloud_cost_out: float = 0.0,
    on_exceed: str = "switch_to_local",
    per_task_token_limit: int = 60_000,
    per_day_cost_usd_limit: float = 5.0,
    routing: dict[str, dict[str, str | None]] | None = None,
) -> LlmConfig:
    """一份可测的双通道配置（云端 ``http://cloud.test/v1`` / 本地 ``http://127.0.0.1:11434``）。"""
    return LlmConfig.model_validate(
        {
            "schema_version": "1.0",
            "default_profile": "cloud",
            "profiles": {
                "cloud": {
                    "engine": "oi_compatible",
                    "base_url": "http://cloud.test/v1",
                    "api_key_env": "STUDIO_LLM_API_KEY",
                    "model": "cloud-model",
                    "timeout_sec": cloud_timeout,
                    "max_retries": cloud_retries,
                    "temperature": 0.8,
                    "json_mode": True,
                    "cost_per_1k_in": cloud_cost_in,
                    "cost_per_1k_out": cloud_cost_out,
                },
                "local": {
                    "engine": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "api_key_env": None,
                    "model": "qwen2.5:7b",
                    "timeout_sec": local_timeout,
                    "max_retries": local_retries,
                    "json_mode": True,
                },
            },
            "routing": routing
            or {
                "demo": {"profile": "cloud", "fallback": "local"},
                "local_only": {"profile": "local", "fallback": None},
            },
            "budget": {
                "per_task_token_limit": per_task_token_limit,
                "per_day_cost_usd_limit": per_day_cost_usd_limit,
                "on_exceed": on_exceed,
            },
            "probe_local_on_start": False,
        }
    )


class FakeClock:
    """单调递增的假时钟（每次读取 +``step``）⇒ 延迟与退避都可断言。"""

    def __init__(self, step: float = 0.05) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


@dataclass(slots=True)
class Reply:
    """脚本里的一条回应：要么给文本，要么抛传输错误。"""

    text: str = VALID_TEXT
    input_tokens: int | None = 10
    output_tokens: int | None = 20
    error: LlmTransportError | None = None


@dataclass(slots=True)
class ScriptedTransport:
    """按脚本依次回应的假传输（脚本用尽 ⇒ 复用最后一条）。

    记录每次调用的 ``(call, messages)``，用来断言"通道 / 密钥 / 超时 / 修复消息"。
    """

    replies: list[Reply] = field(default_factory=lambda: [Reply()])
    calls: list[tuple[LlmCall, list[ChatMessage]]] = field(default_factory=list)
    closed: bool = False

    async def chat(self, call: LlmCall, messages: Sequence[ChatMessage]) -> LlmResponse:
        self.calls.append((call, list(messages)))
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        reply = self.replies[index]
        if reply.error is not None:
            raise reply.error
        return LlmResponse(
            text=reply.text,
            model=call.model,
            usage=LlmUsage(
                input_tokens=reply.input_tokens if reply.input_tokens is not None else 0,
                output_tokens=reply.output_tokens if reply.output_tokens is not None else 0,
                estimated=reply.input_tokens is None or reply.output_tokens is None,
            ),
            finish_reason="stop",
            latency_ms=12,
        )

    async def aclose(self) -> None:
        self.closed = True

    @property
    def profiles(self) -> list[str]:
        return [call.profile for call, _ in self.calls]


def make_db(tmp_path: Path) -> sqlite3.Connection:
    """建一个已迁移的临时库并返回连接。"""
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    return connect(paths.db_file)


def make_prompts(root: Path, *, name: str = "demo") -> PromptLibrary:
    """造一份最小提示词库（manifest 的 sha256 用与生产同一算法算出）。"""
    prompts_dir = root / "prompts"
    (prompts_dir / name).mkdir(parents=True, exist_ok=True)
    system_rel = f"{name}/system.md"
    user_rel = f"{name}/user.jinja"
    system_text = "你是{{persona_name}}的编剧。\n"
    user_text = "题目：{{topic}}\n"
    (prompts_dir / system_rel).write_text(system_text, encoding="utf-8")
    (prompts_dir / user_rel).write_text(user_text, encoding="utf-8")
    digest = sha256_hex(system_rel, system_text, user_rel, user_text)
    manifest = (
        'schema_version: "1.0"\n'
        "prompts:\n"
        f"  {name}:\n"
        f"    system: {system_rel}\n"
        f"    user: {user_rel}\n"
        '    version: "1"\n'
        f"    sha256: {digest}\n"
        "    description: 测试用\n"
    )
    (prompts_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return PromptLibrary.load(prompts_dir)


def make_context(*, task_id: str | None = "t1", remaining: int | None = None) -> AgentContext:
    return AgentContext(
        task_id=task_id,
        job_id="j1",
        persona=persona(),
        trace_id="trace-1",
        seed=7,
        token_budget_remaining=remaining,
    )


def agent_gateway(
    paths: StudioPaths,
    connection: sqlite3.Connection,
    transport: ScriptedTransport,
    *,
    agents: Sequence[str],
) -> LlmGateway:
    """用**仓库真实的** ``schemas/`` 与 ``prompts/`` 装配网关。

    Agent 单测与集成测试共用：契约（schema 文件）一旦和 pydantic 模型漂移，
    这里会第一个红 —— 而用假 schema 是测不出来的。
    """
    return LlmGateway(
        config=llm_config(routing={name: {"profile": "cloud", "fallback": "local"} for name in agents}),
        transport=transport,
        calls=LlmCallStore(connection),
        budget=None,
        breaker=None,
        settings=GatewaySettings(backoff_base_sec=0.0),
        schema_root=paths.schemas_dir,
        log=None,
        clock=FakeClock(),
        env={"STUDIO_LLM_API_KEY": "test-key"},
    )


def build_gateway(
    tmp_path: Path,
    transport: ScriptedTransport,
    *,
    connection: sqlite3.Connection,
    config: LlmConfig | None = None,
    settings: GatewaySettings | None = None,
    with_budget: bool = False,
    env: dict[str, str] | None = None,
    log: Any = None,
    breaker: Any = None,
    schema: dict[str, Any] | None = None,
) -> LlmGateway:
    """装配一个可测网关（schema 与提示词都在 tmp 目录里）。"""
    write_schema(tmp_path / "schemas", schema=schema)
    store = LlmCallStore(connection)
    budget = TokenBudget(config.budget, store) if (with_budget and config is not None) else None
    return LlmGateway(
        config=config or llm_config(),
        transport=transport,
        calls=store,
        budget=budget,
        breaker=breaker,
        settings=settings or GatewaySettings(backoff_base_sec=0.0),
        schema_root=tmp_path / "schemas",
        log=log,
        clock=FakeClock(),
        env=env if env is not None else {"STUDIO_LLM_API_KEY": "test-key"},
    )
