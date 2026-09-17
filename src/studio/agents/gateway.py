"""LLM 双通道网关（T1.8 · §01.2.4 / §04.1.1）。

一次 ``complete()`` 的完整决策链
-------------------------------
```
① 预算闸门（R16）── 超限 ⇒ 按 on_exceed：切本地 / 失败 / 仅告警
② 选通道        ── routing[agent].profile ⇒ fallback（熔断打开的通道直接跳过）
③ 传输重试      ── 5xx / 429 / 超时：指数退避，最多 profile.max_retries 次
④ 修复重试      ── JSON / Schema 不合法：把错误回灌给模型，最多 3 次
⑤ 记账          ── **每一次尝试**写一行 llm_calls（失败也记，否则重试能绕开预算）
⑥ 熔断          ── 连续失败 ≥3 或半开探针失败 ⇒ 打开，到点后放一个探针
```

两类重试**不是一回事**（别合并）
--------------------------------
- **传输重试**：请求根本没拿到有效响应（网络 / 5xx / 超时）⇒ 退避后**重发同一请求**。
- **修复重试**：拿到了响应但内容不合契约 ⇒ **追加两条消息**（模型上次输出 + 校验错误）
  再问一次 —— 这不是"重发"，而是"带着错误信息再问"。

次数上限
--------
``repair_attempts = 3`` ⇒ 每个通道最多 ``1 + 3 = 4`` 次调用（§01.2.4「失败重试 ≤3」
按**重试次数**计，见 T1.8 裁定 55）。

为什么失败不抛异常
------------------
§04.1.1 硬约束 5：通道失败 ⇒ 返回 ``error_code``。抛异常会把"降级"变成"崩溃"，
而 pipeline 需要的是一个可判定失败的 :class:`AgentResult`。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from studio.agents.base import RAW_TEXT_LIMIT, AgentContext, AgentResult
from studio.agents.budget import BudgetAction, TokenBudget
from studio.agents.circuit import CircuitBreaker
from studio.agents.cost import CallStatus, LlmCallRecord, LlmCallStore, estimate_cost
from studio.agents.json_guard import SchemaGuard, parse_json, repair_prompt
from studio.agents.llm_client import (
    ChatMessage,
    LlmCall,
    LlmTransport,
    LlmUsage,
    is_local_endpoint,
)
from studio.agents.prompts import RenderedPrompt
from studio.core.config import LlmConfig, LlmProfileConfig
from studio.core.errors import ErrorCode, LlmError, LlmTransportError
from studio.core.logging import get_logger
from studio.core.proto import Severity

__all__ = [
    "DEFAULT_REPAIR_ATTEMPTS",
    "GatewaySettings",
    "LlmGateway",
    "LogSink",
    "ParseFn",
    "SecretLookup",
    "Sleeper",
]

#: 按**环境变量名**取密钥（持久化来源：`config/secrets.yaml`）。
#: ``None`` ⇒ 只认环境变量（老口径，测试与既有调用方都不受影响）。
type SecretLookup = Callable[[str], str | None]

logger = get_logger("studio.agents.gateway")

#: 修复重试次数上限（§01.2.4「失败重试 ≤3」）
DEFAULT_REPAIR_ATTEMPTS: int = 3

type ParseFn[TOut: BaseModel] = Callable[[object], TOut]
type Sleeper = Callable[[float], Awaitable[None]]

_LEVELS: Mapping[Severity, str] = {
    "debug": "debug",
    "info": "info",
    "warn": "warning",
    "error": "error",
    "fatal": "critical",
}


class LogSink(Protocol):
    """日志出口（由 services 层注入 ``LogService.append``；不注入则只走 structlog）。

    ⚠️ 分层：``agents`` 在 ``services`` 之下，**不得**直接 import ``LogService``
    （§02.1 依赖方向）⇒ 用这个回调把"落库 + 推送 WS"的能力注入进来。
    """

    def __call__(
        self,
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """网关策略（生产取默认值；测试注入小退避以免真的等）。"""

    #: 修复重试次数（"带着错误再问一次"的次数）
    repair_attempts: int = DEFAULT_REPAIR_ATTEMPTS
    #: 传输重试的指数退避基数与上限（秒）
    backoff_base_sec: float = 0.5
    backoff_max_sec: float = 8.0
    #: 单次调用硬超时上限（§04.1.1 硬约束 3：120s）；与 profile.timeout_sec 取小
    hard_timeout_sec: float = 120.0


@dataclass(slots=True)
class _Tally:
    """累计量（跨通道汇总 ⇒ 失败结果里也能看到"总共花了多少"）。"""

    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""
    engine: str = ""
    model: str = ""
    profile: str = ""
    error_code: ErrorCode = ErrorCode.LLM_UPSTREAM
    error_message: str = ""

    def add_usage(self, usage: LlmUsage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

    def observe(self, *, engine: str, model: str, profile: str, raw_text: str) -> None:
        self.engine = engine
        self.model = model
        self.profile = profile
        self.raw_text = raw_text[:RAW_TEXT_LIMIT]


class LlmGateway:
    """双通道网关（每进程一个；自身无状态，熔断状态在 :class:`CircuitBreaker`）。"""

    def __init__(
        self,
        *,
        config: LlmConfig,
        transport: LlmTransport,
        calls: LlmCallStore,
        budget: TokenBudget | None = None,
        breaker: CircuitBreaker | None = None,
        settings: GatewaySettings | None = None,
        schema_root: Path | None = None,
        log: LogSink | None = None,
        sleeper: Sleeper | None = None,
        clock: Callable[[], float] = time.monotonic,
        env: Mapping[str, str] | None = None,
        secrets: SecretLookup | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._calls = calls
        self._budget = budget
        self._breaker = breaker or CircuitBreaker()
        self._settings = settings or GatewaySettings()
        self._schema_root = schema_root
        self._log = log
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._clock = clock
        self._env = env
        self._secrets = secrets
        self._guards: dict[str, SchemaGuard] = {}

    # ── 对外唯一入口 ────────────────────────────────────────────────
    async def complete[TOut: BaseModel](
        self,
        *,
        agent: str,
        ctx: AgentContext,
        prompt: RenderedPrompt,
        schema_name: str,
        parse: ParseFn[TOut],
    ) -> AgentResult[TOut]:
        """跑一次"提示词 → 结构化结果"（含预算 / 熔断 / 重试 / 记账）。"""
        tally = _Tally()
        route = self._config.routing.get(agent)
        if route is None:
            return self._failure(
                prompt,
                tally,
                code=ErrorCode.LLM_ROUTE_MISSING,
                message=f"llm.yaml 未给 Agent {agent!r} 配置 routing",
            )

        channels = self._channels_for(agent, route.profile, route.fallback, ctx, tally)
        guard = self._guard(schema_name)
        messages = _initial_messages(prompt)

        for channel in channels:
            if not self._breaker.allow(channel, now=self._clock()):
                tally.warnings.append(f"circuit_open:{channel}")
                self._emit(
                    "warn",
                    "llm.circuit",
                    f"通道 {channel} 处于熔断，跳过",
                    task_id=ctx.task_id,
                    payload={"code": str(ErrorCode.LLM_CIRCUIT_OPEN), "profile": channel},
                )
                continue
            result = await self._run_channel(
                channel,
                agent=agent,
                ctx=ctx,
                prompt_version=prompt.prompt_version,
                messages=messages,
                guard=guard,
                parse=parse,
                tally=tally,
            )
            if result is not None:
                self._breaker.record_success(channel, now=self._clock())
                return result
            self._breaker.record_failure(channel, now=self._clock())

        return self._failure(prompt, tally, code=tally.error_code, message=tally.error_message)

    # ── 通道选择（含预算裁决）──────────────────────────────────────
    def _channels_for(
        self,
        agent: str,
        primary: str,
        fallback: str | None,
        ctx: AgentContext,
        tally: _Tally,
    ) -> list[str]:
        channels = [primary]
        if fallback is not None and fallback != primary:
            channels.append(fallback)
        if self._budget is None:
            return channels

        verdict = self._budget.check(task_id=ctx.task_id, remaining_hint=ctx.token_budget_remaining)
        if not verdict.exceeded:
            return channels

        tally.warnings.append(f"budget_exceeded:{verdict.reason}")
        self._emit(
            "warn",
            "llm.budget",
            f"预算超限：{verdict.reason}（on_exceed={self._config.budget.on_exceed}）",
            task_id=ctx.task_id,
            payload={
                "code": str(ErrorCode.LLM_BUDGET_EXCEEDED),
                "agent": agent,
                "spent_tokens": verdict.spent_tokens,
                "token_limit": verdict.token_limit,
                "spent_cost_usd": verdict.spent_cost_usd,
                "cost_limit_usd": verdict.cost_limit_usd,
                "action": str(verdict.action),
            },
        )
        if verdict.action is BudgetAction.SWITCH_TO_LOCAL:
            local = self._local_profile(channels)
            return [local] if local is not None else channels
        if verdict.action is BudgetAction.ALERT_ONLY:
            return channels

        # FAIL_TASK：先记一行账（否则"超预算"这个事实在库里无痕），再空手而归
        profile = self._config.profiles[primary]
        self._calls.record(
            LlmCallRecord(
                task_id=ctx.task_id,
                job_id=ctx.job_id,
                agent=agent,
                engine=profile.engine,
                model=profile.model,
                is_local=self._is_local(profile),
                status=CallStatus.BUDGET_EXCEEDED,
                error_message=verdict.reason,
            )
        )
        tally.error_code = ErrorCode.LLM_BUDGET_EXCEEDED
        tally.error_message = verdict.reason
        return []

    def _local_profile(self, channels: Sequence[str]) -> str | None:
        """挑一个本地通道：候选通道里优先，其次 profiles 里任意本地通道。"""
        for name in channels:
            if self._is_local(self._config.profiles[name]):
                return name
        for name, profile in self._config.profiles.items():
            if self._is_local(profile):
                return name
        return None

    def _is_local(self, profile: LlmProfileConfig) -> bool:
        return is_local_endpoint(engine=profile.engine, base_url=profile.base_url)

    # ── 单通道执行：传输重试 + 修复重试 ─────────────────────────────
    async def _run_channel[TOut: BaseModel](
        self,
        channel: str,
        *,
        agent: str,
        ctx: AgentContext,
        prompt_version: str,
        messages: Sequence[ChatMessage],
        guard: SchemaGuard,
        parse: ParseFn[TOut],
        tally: _Tally,
    ) -> AgentResult[TOut] | None:
        profile = self._config.profiles[channel]
        call = self._build_call(channel, profile, ctx)
        conversation = list(messages)
        transport_failures = 0
        repairs = 0

        while True:
            tally.attempts += 1
            started = self._clock()
            try:
                response = await self._transport.chat(call, conversation)
            except LlmTransportError as exc:
                latency = int((self._clock() - started) * 1000)
                tally.latency_ms += latency
                tally.error_code = exc.code
                tally.error_message = exc.message
                self._record(
                    agent=agent,
                    ctx=ctx,
                    profile=profile,
                    prompt_version=prompt_version,
                    status=_status_for(exc.code),
                    usage=None,
                    latency_ms=latency,
                    error_message=exc.message,
                )
                if exc.retryable and transport_failures < profile.max_retries:
                    delay = self._backoff(transport_failures)
                    transport_failures += 1
                    tally.warnings.append(f"transport_retry:{channel}:{transport_failures}")
                    await self._sleep(delay)
                    continue
                return None

            latency = response.latency_ms
            tally.latency_ms += latency
            tally.add_usage(response.usage)
            tally.observe(
                engine=call.engine,
                model=response.model,
                profile=channel,
                raw_text=response.text,
            )

            parsed, errors = _evaluate(guard, parse, response.text)
            self._record(
                agent=agent,
                ctx=ctx,
                profile=profile,
                prompt_version=prompt_version,
                status=CallStatus.OK if not errors else CallStatus.SCHEMA_INVALID,
                usage=response.usage,
                latency_ms=latency,
                error_message=None if not errors else "；".join(errors[:3]),
            )
            if not errors:
                return AgentResult[TOut](
                    ok=True,
                    data=parsed,
                    raw_text=response.text[:RAW_TEXT_LIMIT],
                    engine=call.engine,
                    model=response.model,
                    profile=channel,
                    prompt_version=prompt_version,
                    input_tokens=tally.input_tokens,
                    output_tokens=tally.output_tokens,
                    latency_ms=tally.latency_ms,
                    attempts=tally.attempts,
                    warnings=list(tally.warnings),
                )

            tally.error_code = ErrorCode.LLM_SCHEMA_INVALID
            tally.error_message = "；".join(errors[:3])
            if repairs >= self._settings.repair_attempts:
                return None
            repairs += 1
            tally.warnings.append(f"repair:{channel}:{repairs}")
            conversation = [
                *conversation,
                ChatMessage(role="assistant", content=response.text),
                ChatMessage(role="user", content=repair_prompt(errors, response.text)),
            ]

    # ── 记账 / 日志 / 退避 ─────────────────────────────────────────
    def _build_call(self, channel: str, profile: LlmProfileConfig, ctx: AgentContext) -> LlmCall:
        return LlmCall(
            profile=channel,
            engine=profile.engine,
            base_url=profile.base_url,
            model=profile.model,
            api_key=self._api_key(profile),
            temperature=profile.temperature,
            json_mode=profile.json_mode,
            timeout_sec=min(profile.timeout_sec, self._settings.hard_timeout_sec),
            seed=ctx.seed,
        )

    def _api_key(self, profile: LlmProfileConfig) -> str | None:
        """密钥只在**运行时**取：环境变量 > 持久化密钥文件（§01.2.4 密钥铁律）。

        为什么多这一路：面板里填的 Key 落在 ``config/secrets.yaml``，而网关**每次
        调用都现取一次** —— 填完不需要重启 API 与 4 个 worker。环境变量仍然优先
        （容器 / CI 的用法，且它要能压过盘上那一份）。
        """
        if profile.api_key_env is None:
            return None
        source = self._env if self._env is not None else os.environ
        value = source.get(profile.api_key_env)
        if value:
            return value
        if self._secrets is None:
            return None
        return self._secrets(profile.api_key_env)

    def _record(
        self,
        *,
        agent: str,
        ctx: AgentContext,
        profile: LlmProfileConfig,
        prompt_version: str | None,
        status: CallStatus,
        usage: LlmUsage | None,
        latency_ms: int,
        error_message: str | None,
    ) -> None:
        self._calls.record(
            LlmCallRecord(
                task_id=ctx.task_id,
                job_id=ctx.job_id,
                agent=agent,
                engine=profile.engine,
                model=profile.model,
                is_local=self._is_local(profile),
                prompt_version=prompt_version,
                input_tokens=None if usage is None else usage.input_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                cost_usd=None
                if usage is None
                else estimate_cost(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_per_1k_in=profile.cost_per_1k_in,
                    cost_per_1k_out=profile.cost_per_1k_out,
                ),
                latency_ms=latency_ms,
                status=status,
                error_message=error_message,
            )
        )

    def _backoff(self, attempt: int) -> float:
        delay = self._settings.backoff_base_sec * (2**attempt)
        return float(min(delay, self._settings.backoff_max_sec))

    def _guard(self, schema_name: str) -> SchemaGuard:
        """按名缓存 Schema（``schemas/<name>.schema.json``）。"""
        cached = self._guards.get(schema_name)
        if cached is not None:
            return cached
        if self._schema_root is None:
            raise LlmError(
                "网关未配置 schema_root，无法加载 JSON Schema",
                code=ErrorCode.LLM_SCHEMA_INVALID,
                remediation="构造 LlmGateway 时传 schema_root=StudioPaths.from_env().schemas_dir",
            )
        guard = SchemaGuard.from_file(self._schema_root / f"{schema_name}.schema.json")
        self._guards[schema_name] = guard
        return guard

    def _failure[TOut: BaseModel](
        self,
        prompt: RenderedPrompt,
        tally: _Tally,
        *,
        code: ErrorCode,
        message: str,
    ) -> AgentResult[TOut]:
        if not message:
            message = f"所有通道均失败（{code}）"
        self._emit(
            "error",
            "llm.gateway",
            f"Agent 调用失败：{message}",
            payload={"code": str(code), "attempts": tally.attempts, "profile": tally.profile},
        )
        return AgentResult[TOut](
            ok=False,
            data=None,
            raw_text=tally.raw_text or None,
            engine=tally.engine,
            model=tally.model,
            profile=tally.profile,
            prompt_version=prompt.prompt_version,
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
            latency_ms=tally.latency_ms,
            attempts=tally.attempts,
            warnings=list(tally.warnings),
            error_code=str(code),
            error_message=message,
        )

    def _emit(
        self,
        level: Severity,
        source: str,
        message: str,
        *,
        task_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        if self._log is not None:
            self._log(level=level, source=source, message=message, task_id=task_id, payload=payload)
            return
        log_method = getattr(logger, _LEVELS[level])
        log_method(message, source=source, **(payload or {}))


def _initial_messages(prompt: RenderedPrompt) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    if prompt.system:
        messages.append(ChatMessage(role="system", content=prompt.system))
    messages.append(ChatMessage(role="user", content=prompt.user))
    return messages


def _evaluate[TOut: BaseModel](
    guard: SchemaGuard, parse: ParseFn[TOut], text: str
) -> tuple[TOut | None, list[str]]:
    """JSON 解析 + JSON Schema 校验 + 目标模型校验，统一成"错误字符串列表"。"""
    try:
        data = parse_json(text)
    except LlmError as exc:
        return None, [exc.message]
    errors = guard.validate(data)
    if errors:
        return None, errors
    try:
        return parse(data), []
    except ValidationError as exc:
        return None, [_pydantic_error(item) for item in exc.errors()[:10]]
    except LlmError as exc:
        return None, [exc.message]


def _pydantic_error(item: Mapping[str, object]) -> str:
    location = item.get("loc") or ()
    path = ".".join(str(part) for part in location) if isinstance(location, tuple) else str(location)
    return f"{path or '$'}: {item.get('msg', 'invalid')}"


def _status_for(code: ErrorCode) -> CallStatus:
    if code is ErrorCode.LLM_TIMEOUT:
        return CallStatus.TIMEOUT
    return CallStatus.HTTP_ERROR
