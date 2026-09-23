"""智能体层（T1.8 起）：LLM 网关 + 通用 Agent 契约。

依赖方向（§02.1）：``agents`` 只依赖 ``core`` / ``domain``，
**不得** import ``tts`` / ``render`` / ``services`` / ``app``。
"""

from __future__ import annotations

from studio.agents.base import (
    DEFAULT_SYSTEM_BLOCKS,
    DEFAULT_USER_BLOCKS,
    RAW_TEXT_LIMIT,
    AgentContext,
    AgentResult,
    BaseAgent,
    bullet_block,
    persona_variables,
)
from studio.agents.budget import BudgetAction, BudgetVerdict, TokenBudget
from studio.agents.circuit import (
    CIRCUIT_FAIL_THRESHOLD,
    CIRCUIT_OPEN_SEC,
    CircuitBreaker,
    CircuitState,
)
from studio.agents.cost import CallStatus, LlmCallRecord, LlmCallStore, estimate_cost
from studio.agents.director import OUTLINE_RETRIES, DirectorAgent
from studio.agents.editor import EditorAgent
from studio.agents.feedback_classifier import FeedbackClassifierAgent
from studio.agents.gateway import (
    DEFAULT_REPAIR_ATTEMPTS,
    GatewaySettings,
    LlmGateway,
    LogSink,
)
from studio.agents.ideator import IdeatorAgent
from studio.agents.json_guard import (
    MAX_REPAIR_ERRORS,
    SchemaGuard,
    parse_json,
    repair_prompt,
)
from studio.agents.llm_client import (
    ChatMessage,
    Engine,
    HttpLlmTransport,
    LlmCall,
    LlmResponse,
    LlmTransport,
    LlmUsage,
    estimate_tokens,
    is_retryable_status,
)
from studio.agents.news_scout import NewsScoutAgent
from studio.agents.planner import PLANNER_RULE_RETRIES, PlannerAgent
from studio.agents.prompts import (
    PromptEntry,
    PromptLibrary,
    PromptManifest,
    RenderedPrompt,
    render_template,
)
from studio.agents.reviewer import MISMATCH_THRESHOLD, ReviewerAgent
from studio.agents.writer import WriterAgent

__all__ = [
    "CIRCUIT_FAIL_THRESHOLD",
    "CIRCUIT_OPEN_SEC",
    "DEFAULT_REPAIR_ATTEMPTS",
    "DEFAULT_SYSTEM_BLOCKS",
    "DEFAULT_USER_BLOCKS",
    "MAX_REPAIR_ERRORS",
    "MISMATCH_THRESHOLD",
    "OUTLINE_RETRIES",
    "PLANNER_RULE_RETRIES",
    "RAW_TEXT_LIMIT",
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "BudgetAction",
    "BudgetVerdict",
    "CallStatus",
    "ChatMessage",
    "CircuitBreaker",
    "CircuitState",
    "DirectorAgent",
    "EditorAgent",
    "Engine",
    "FeedbackClassifierAgent",
    "GatewaySettings",
    "HttpLlmTransport",
    "IdeatorAgent",
    "LlmCall",
    "LlmCallRecord",
    "LlmCallStore",
    "LlmGateway",
    "LlmResponse",
    "LlmTransport",
    "LlmUsage",
    "LogSink",
    "NewsScoutAgent",
    "PlannerAgent",
    "PromptEntry",
    "PromptLibrary",
    "PromptManifest",
    "RenderedPrompt",
    "ReviewerAgent",
    "SchemaGuard",
    "TokenBudget",
    "WriterAgent",
    "bullet_block",
    "estimate_cost",
    "estimate_tokens",
    "is_retryable_status",
    "parse_json",
    "persona_variables",
    "render_template",
    "repair_prompt",
]
