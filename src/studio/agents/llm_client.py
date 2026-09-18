"""LLM 传输层：双通道 wire format（T1.8 · §01.2.4）。

职责边界（别混）
----------------
- **只管一次 HTTP 往返**：组请求 → 发 → 解析出 ``text`` + ``usage``。
- **不管**：路由选择 / 修复重试 / 预算闸门 / 熔断 / 记账 —— 全在 ``gateway.py``。
- **不抛裸异常**：一律 :class:`studio.core.errors.LlmTransportError`，
  带 ``code``（``LLM_TIMEOUT`` / ``LLM_RATE_LIMIT`` / ``LLM_UPSTREAM``）与
  ``retryable`` 标记，让上层决定"重试 / 切通道 / 放弃"。

三个 engine 的差异（一处收口，禁止散落在调用方）
----------------------------------------------
| engine | 端点 | 请求体 | JSON 约束 | usage 字段 |
| --- | --- | --- | --- | --- |
| ``oi_compatible`` | ``POST {base}/chat/completions`` | [OI] 标准 | ``response_format`` | ``prompt_tokens`` |
| ``llama_cpp`` | 同上（llama.cpp server 兼容该端点） | 同上 | 同上 | 同上 |
| ``ollama`` | ``POST {base}/api/chat`` | Ollama 原生 | ``format: <schema>`` | ``prompt_eval_count`` |

**超时是"单次尝试"的硬上限**：网关会取 ``min(profile.timeout_sec, app.timeouts.llm_sec)``
后传进来（§04.1.1 硬约束 3：单次 LLM 调用硬超时 120s）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from studio.core.errors import ErrorCode, LlmTransportError
from studio.core.logging import get_logger

__all__ = [
    "RETRYABLE_STATUSES",
    "ChatMessage",
    "Engine",
    "HttpLlmTransport",
    "LlmCall",
    "LlmResponse",
    "LlmTransport",
    "LlmUsage",
    "estimate_tokens",
    "is_local_endpoint",
    "is_retryable_status",
]

logger = get_logger("studio.agents.llm_client")

#: 值得重试的 HTTP 状态码（§01.2.4「httpx + 重试 + 记账」）
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429, *range(500, 600)})

#: 三个 engine（与 ``config/llm.yaml`` 的 ``engine`` 取值**逐字一致**）
type Engine = Literal["oi_compatible", "ollama", "llama_cpp"]

_OI_ENGINES: frozenset[str] = frozenset({"oi_compatible", "llama_cpp"})


def is_retryable_status(status: int) -> bool:
    """5xx / 429 / 408 / 409 / 425 ⇒ 可重试；其余 4xx ⇒ 不可重试。"""
    return status in RETRYABLE_STATUSES


def is_local_endpoint(*, engine: str, base_url: str) -> bool:
    """本地通道判定（不烧钱、可离线）。

    判据是"**ollama 引擎**或**回环地址**"，而不是 profile 的名字 ——
    名字可以随便改（``local`` / ``offline`` / ``qwen_local``），
    但"请求发到 127.0.0.1"这件事改不了。
    """
    if engine == "ollama":
        return True
    return "127.0.0.1" in base_url or "localhost" in base_url or "::1" in base_url


class ChatMessage(BaseModel):
    """一条对话消息（[OI] 与 Ollama 的公共子集）。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class LlmCall(BaseModel):
    """一次调用的**静态参数**（由 ``LlmProfileConfig`` 展开 + 密钥注入）。

    密钥在这里是**运行时值**（从环境变量读出），绝不回写配置 / 日志。
    """

    model_config = ConfigDict(extra="forbid")

    profile: str
    engine: Engine
    base_url: str
    model: str
    api_key: str | None = None
    temperature: float = 0.8
    json_mode: bool = True
    timeout_sec: float = 120.0
    seed: int | None = None
    #: 该 Agent 的 JSON Schema（网关从 ``SchemaGuard`` 取，engine 无关的**契约事实**）。
    #: 传输层按 engine 决定怎么用：Ollama 直接当 ``format``（语法约束解码），
    #: 云端仍走 ``json_object``（要真 Key 才能验，另开一刀）。
    response_schema: Mapping[str, Any] | None = None

    @property
    def is_local(self) -> bool:
        """本地通道（``ollama`` / 回环地址）不计成本、可离线。"""
        return is_local_endpoint(engine=self.engine, base_url=self.base_url)

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/api/chat" if self.engine == "ollama" else f"{base}/chat/completions"


class LlmUsage(BaseModel):
    """token 用量。``estimated=True`` ⇒ 服务端没给 usage，由本地估算兜底。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated: bool = False


class LlmResponse(BaseModel):
    """一次成功的往返结果。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    usage: LlmUsage
    finish_reason: str | None = None
    latency_ms: int = Field(ge=0)


@runtime_checkable
class LlmTransport(Protocol):
    """传输层协议（网关只依赖它 ⇒ 测试注入假件，不发真请求）。"""

    async def chat(self, call: LlmCall, messages: Sequence[ChatMessage]) -> LlmResponse: ...

    async def aclose(self) -> None: ...


# ══════════════════════════════════════════════════════════════════════
# token 估算（仅在服务端未返回 usage 时兜底）
# ══════════════════════════════════════════════════════════════════════


def _is_wide(char: str) -> bool:
    """CJK / 全角字符（按 1 token/字估算）。"""
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F  # CJK 标点
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 基本区
        or 0xF900 <= code <= 0xFAFF  # 兼容表意
        or 0xFF00 <= code <= 0xFFEF  # 全角
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 数（**误差 ±20% 量级**）。

    规则：CJK/全角 ≈ 1 token/字；ASCII ≈ 4 字符/token；其余 ≈ 2 字符/token。
    够用来兜底预算闸门（R16），**不足以用来对账** —— 所以只在服务端
    没有返回 usage 时使用，并在 ``llm_calls`` 行上留 ``usage_estimated`` 痕迹。
    """
    wide = sum(1 for char in text if _is_wide(char))
    rest = len(text) - wide
    return max(1, wide + (rest + 3) // 4) if text else 0


# ══════════════════════════════════════════════════════════════════════
# 响应体解析（对外部 JSON 做**结构校验**，不信任任何字段）
# ══════════════════════════════════════════════════════════════════════


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _malformed(call: LlmCall, detail: str) -> LlmTransportError:
    """响应体不符合预期（代理截断 / 网关改写 / 端点填错）。

    判定为**可重试**：截断的响应往往是瞬时的；若端点真的填错，
    连续失败会触发熔断（``LLM_CIRCUIT_OPEN``），不会无限重试。
    """
    return LlmTransportError(
        f"LLM 响应体不符合 {call.engine} 契约：{detail}",
        code=ErrorCode.LLM_UPSTREAM,
        retryable=True,
        context={"profile": call.profile, "endpoint": call.endpoint(), "detail": detail},
        remediation="确认 base_url 指向正确的服务端点（云端 /v1 或本地 11434）",
    )


def _parse_oi(call: LlmCall, payload: object, prompt_text: str) -> tuple[str, LlmUsage, str | None]:
    body = _as_mapping(payload)
    if body is None:
        raise _malformed(call, "顶层不是对象")
    choices = body.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise _malformed(call, "缺少 choices[0]")
    first = _as_mapping(choices[0])
    if first is None:
        raise _malformed(call, "choices[0] 不是对象")
    message = _as_mapping(first.get("message"))
    if message is None:
        raise _malformed(call, "choices[0].message 不是对象")
    content = _as_str(message.get("content"))
    if content is None:
        raise _malformed(call, "choices[0].message.content 不是字符串")

    usage_map = _as_mapping(body.get("usage")) or {}
    input_tokens = _as_int(usage_map.get("prompt_tokens"))
    output_tokens = _as_int(usage_map.get("completion_tokens"))
    estimated = input_tokens is None or output_tokens is None
    usage = LlmUsage(
        input_tokens=input_tokens if input_tokens is not None else estimate_tokens(prompt_text),
        output_tokens=output_tokens if output_tokens is not None else estimate_tokens(content),
        estimated=estimated,
    )
    return content, usage, _as_str(first.get("finish_reason"))


def _parse_ollama(call: LlmCall, payload: object, prompt_text: str) -> tuple[str, LlmUsage, str | None]:
    body = _as_mapping(payload)
    if body is None:
        raise _malformed(call, "顶层不是对象")
    message = _as_mapping(body.get("message"))
    if message is None:
        raise _malformed(call, "缺少 message")
    content = _as_str(message.get("content"))
    if content is None:
        raise _malformed(call, "message.content 不是字符串")

    input_tokens = _as_int(body.get("prompt_eval_count"))
    output_tokens = _as_int(body.get("eval_count"))
    estimated = input_tokens is None or output_tokens is None
    usage = LlmUsage(
        input_tokens=input_tokens if input_tokens is not None else estimate_tokens(prompt_text),
        output_tokens=output_tokens if output_tokens is not None else estimate_tokens(content),
        estimated=estimated,
    )
    return content, usage, _as_str(body.get("done_reason"))


# ══════════════════════════════════════════════════════════════════════
# 请求体构造
# ══════════════════════════════════════════════════════════════════════


def build_body(call: LlmCall, messages: Sequence[ChatMessage]) -> dict[str, object]:
    """按 engine 组请求体（公开给契约测试比对 wire format）。"""
    payload = [{"role": message.role, "content": message.content} for message in messages]
    if call.engine == "ollama":
        options: dict[str, object] = {"temperature": call.temperature}
        if call.seed is not None:
            options["seed"] = call.seed
        body: dict[str, object] = {
            "model": call.model,
            "messages": payload,
            "stream": False,
            "options": options,
        }
        if call.json_mode:
            # 有 schema 就交给解码器做**语法约束**：像"5–8 条"这种数量约束
            # 也在语法里，小模型没机会少写几条（T1.9 本地兜底实测：
            # qwen2.5:7b 只给 "json" 时稳定只吐 1 条方向，给 schema 后 5–8 条）。
            body["format"] = call.response_schema if call.response_schema is not None else "json"
        return body

    body = {
        "model": call.model,
        "messages": payload,
        "temperature": call.temperature,
        "stream": False,
    }
    if call.seed is not None:
        body["seed"] = call.seed
    if call.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def build_headers(call: LlmCall) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if call.api_key:
        headers["Authorization"] = f"Bearer {call.api_key}"
    return headers


# ══════════════════════════════════════════════════════════════════════
# httpx 实现
# ══════════════════════════════════════════════════════════════════════


class HttpLlmTransport:
    """基于 ``httpx.AsyncClient`` 的传输实现（云端 + 本地共用一条连接池）。"""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def chat(self, call: LlmCall, messages: Sequence[ChatMessage]) -> LlmResponse:
        client = await self._ensure_client()
        started = self._now()
        try:
            response = await client.post(
                call.endpoint(),
                json=build_body(call, messages),
                headers=build_headers(call),
                timeout=httpx.Timeout(call.timeout_sec),
            )
        except httpx.TimeoutException as exc:
            raise LlmTransportError(
                f"LLM 调用超时（{call.timeout_sec:.0f}s，profile={call.profile}）",
                code=ErrorCode.LLM_TIMEOUT,
                retryable=True,
                context={"profile": call.profile, "endpoint": call.endpoint()},
                remediation="调大 llm.yaml 的 timeout_sec，或换更快的模型 / 通道",
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmTransportError(
                f"LLM 连接失败：{type(exc).__name__}",
                code=ErrorCode.LLM_UPSTREAM,
                retryable=True,
                context={"profile": call.profile, "endpoint": call.endpoint()},
                remediation="检查网络 / 本地服务是否已启动（`studio llm probe`）",
            ) from exc

        latency_ms = int((self._now() - started) * 1000)
        if response.status_code >= 400:
            raise _http_error(call, response)

        try:
            payload: object = response.json()
        except ValueError as exc:
            raise _malformed(call, "响应不是合法 JSON") from exc

        prompt_text = "".join(message.content for message in messages)
        parser = _parse_ollama if call.engine == "ollama" else _parse_oi
        text, usage, finish_reason = parser(call, payload, prompt_text)
        if usage.estimated:
            logger.debug("llm.usage_estimated", profile=call.profile, model=call.model)
        return LlmResponse(
            text=text,
            model=call.model,
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _http_error(call: LlmCall, response: httpx.Response) -> LlmTransportError:
    status = response.status_code
    body = response.text[:500]
    if status == 429:
        code = ErrorCode.LLM_RATE_LIMIT
        message = f"LLM 被限流（429，profile={call.profile}）"
    else:
        code = ErrorCode.LLM_UPSTREAM
        message = f"LLM 返回 HTTP {status}（profile={call.profile}）"
    return LlmTransportError(
        message,
        code=code,
        retryable=is_retryable_status(status),
        status=status,
        context={"profile": call.profile, "endpoint": call.endpoint(), "body": body},
        remediation=(
            "限流 ⇒ 降并发 / 换通道；4xx ⇒ 检查 api_key 与 base_url（重试不会变好）"
            if status < 500
            else "上游 5xx ⇒ 网关会自动切 fallback 通道"
        ),
    )
