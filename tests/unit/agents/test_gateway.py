"""LLM 双通道网关的验收测试（T1.8 · §01.2.4 / §04.1.1）。

四条验收（todolist T1.8）
------------------------
① schema 不合法 ⇒ 修复重试 ≤3 后返回 ``LLM_SCHEMA_INVALID``
② 云端 5xx / 超时 ⇒ 自动切本地兜底
③ 超预算 ⇒ 按 ``on_exceed`` 处理
④ 每次调用写 ``llm_calls``（token / 成本 / 耗时 / model / prompt_version）

外加：熔断跳过、非可重试错误直切兜底、硬超时取小、密钥只从环境变量读、
路由缺失、schema 文件缺失、日志出口。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentContext, AgentResult
from studio.agents.circuit import CircuitBreaker
from studio.agents.cost import CallStatus, LlmCallRecord, LlmCallStore
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.agents.prompts import PromptLibrary
from studio.core.clock import now_iso
from studio.core.config import LlmConfig
from studio.core.errors import ErrorCode, LlmError
from studio.core.proto import Severity
from tests.unit.agents.fakes import (
    INVALID_TEXT,
    VALID_TEXT,
    DemoOutput,
    Reply,
    ScriptedTransport,
    build_gateway,
    demo_prompt,
    http_error,
    llm_config,
    make_context,
    make_db,
    make_prompts,
    parse_demo,
    timeout_error,
)

# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@dataclass
class Harness:
    """一套可测装配（临时库 + 脚本化传输 + 临时提示词/schema）。"""

    gateway: LlmGateway
    transport: ScriptedTransport
    store: LlmCallStore
    prompts: PromptLibrary
    #: 由 :func:`_seed_spend` 预埋的历史行数（断言只看"本次运行"产生的行）
    seeded: int = 0

    async def run(self, *, agent: str = "demo", ctx: AgentContext | None = None) -> AgentResult[DemoOutput]:
        return await self.gateway.complete(
            agent=agent,
            ctx=ctx or make_context(),
            prompt=demo_prompt(self.prompts),
            schema_name="demo",
            parse=parse_demo,
        )

    def rows(self) -> list[LlmCallRecord]:
        """本次运行产生的调用记录，**按时间正序**。

        ``LlmCallStore.recent`` 是倒序（成本面板要看最新的），但断言"第几次调用
        是什么状态"要正序，所以这里翻回来（``recent`` 用 rowid 定序 ⇒ 翻转即插入序）；
        ``_seed_spend`` 预埋的历史行直接跳过。
        """
        return list(reversed(self.store.recent(limit=100)))[self.seeded :]

    def statuses(self) -> list[str]:
        return [str(row.status) for row in self.rows()]


@pytest.fixture
def harness_factory(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """构造 Harness 的工厂（自动关连接，避免测试泄漏 sqlite 句柄）。"""
    connections: list[sqlite3.Connection] = []

    def _build(
        *,
        replies: list[Reply] | None = None,
        config: LlmConfig | None = None,
        settings: GatewaySettings | None = None,
        with_budget: bool = False,
        env: dict[str, str] | None = None,
        log: Any = None,
        breaker: CircuitBreaker | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Harness:
        connection = make_db(tmp_path)
        connections.append(connection)
        transport = ScriptedTransport(replies=list(replies) if replies else [Reply()])
        gateway = build_gateway(
            tmp_path,
            transport,
            connection=connection,
            config=config,
            settings=settings,
            with_budget=with_budget,
            env=env,
            log=log,
            breaker=breaker,
            schema=schema,
        )
        return Harness(
            gateway=gateway,
            transport=transport,
            store=LlmCallStore(connection),
            prompts=make_prompts(tmp_path),
        )

    yield _build
    for connection in connections:
        connection.close()


def _seed_spend(harness: Harness, *, task_id: str = "t1", tokens: int = 0, cost: float = 0.0) -> None:
    """往 ``llm_calls`` 里塞一行历史消耗（用于触发预算闸门）。"""
    harness.seeded += 1
    harness.store.record(
        LlmCallRecord(
            task_id=task_id,
            agent="demo",
            engine="oi_compatible",
            model="cloud-model",
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=cost,
            status=CallStatus.OK,
            created_at=now_iso(),
        )
    )


# ══════════════════════════════════════════════════════════════════════
# ① 结构化输出：修复重试 ≤3
# ══════════════════════════════════════════════════════════════════════


async def test_schema_invalid_repairs_three_times_then_fails(
    harness_factory: Callable[..., Harness],
) -> None:
    """两个通道都修不好 ⇒ 每通道 4 次、共 8 次（``attempts`` 跨通道累计，裁定 58）。"""
    harness = harness_factory(replies=[Reply(text=INVALID_TEXT)])
    result = await harness.run()

    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_SCHEMA_INVALID)
    assert result.attempts == 8, "云端 1+3 次 + 本地 1+3 次"
    assert harness.transport.profiles == ["cloud"] * 4 + ["local"] * 4
    assert len(harness.transport.calls) == 8
    assert harness.statuses() == ["schema_invalid"] * 8, "每次尝试都要记账"
    assert result.profile == "local", "最终停在最后一个通道"
    assert result.raw_text is not None and INVALID_TEXT in result.raw_text


async def test_schema_invalid_single_channel_stops_after_one_plus_three(
    harness_factory: Callable[..., Harness],
) -> None:
    """裁定 55：修复重试按**重试次数**计 ⇒ 单通道上限 1 次原始 + 3 次修复 = 4 次。"""
    harness = harness_factory(
        config=llm_config(routing={"demo": {"profile": "cloud", "fallback": None}}),
        replies=[Reply(text=INVALID_TEXT)],
    )
    result = await harness.run()

    assert result.ok is False
    assert result.attempts == 4
    assert harness.transport.profiles == ["cloud"] * 4, "无兜底通道 ⇒ 到此为止"
    assert harness.statuses() == ["schema_invalid"] * 4
    assert result.warnings.count("repair:cloud:1") == 1
    assert "repair:cloud:3" in result.warnings
    assert "repair:cloud:4" not in result.warnings, "第 4 次修复越界"


async def test_repair_retry_succeeds_on_second_attempt(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(replies=[Reply(text=INVALID_TEXT), Reply(text=VALID_TEXT)])
    result = await harness.run()

    assert result.ok is True
    assert result.attempts == 2
    assert result.data is not None and result.data.title == "熊大熊二跑酷"
    assert harness.statuses() == ["schema_invalid", "ok"]
    assert any(item.startswith("repair:cloud:1") for item in result.warnings)


async def test_repair_message_feeds_back_errors_and_previous_output(
    harness_factory: Callable[..., Harness],
) -> None:
    """修复重试 = 追加两条消息（模型上次输出 + 校验错误），不是"重发同一请求"。"""
    harness = harness_factory(replies=[Reply(text=INVALID_TEXT), Reply(text=VALID_TEXT)])
    await harness.run()

    first = harness.transport.calls[0][1]
    second = harness.transport.calls[1][1]
    assert len(first) == 2, "system + user"
    assert len(second) == 4, "system + user + assistant(上次输出) + user(校验错误)"
    assert second[2].role == "assistant"
    assert second[2].content == INVALID_TEXT
    assert second[3].role == "user"
    assert "$.title" in second[3].content, "错误里要带 JSON 路径，模型才知道改哪"


async def test_schema_invalid_falls_back_to_other_channel(
    harness_factory: Callable[..., Harness],
) -> None:
    """云端 4 次都不合格 ⇒ 换本地再试（脚本第 5 次才给好 JSON）。"""
    harness = harness_factory(replies=[Reply(text=INVALID_TEXT)] * 4 + [Reply(text=VALID_TEXT)])
    result = await harness.run()

    assert result.ok is True
    assert result.profile == "local"
    assert result.attempts == 5
    assert harness.transport.profiles == ["cloud"] * 4 + ["local"]


async def test_pydantic_model_violation_also_repairs(
    harness_factory: Callable[..., Harness],
) -> None:
    """schema 放行但输出模型不认（多字段）⇒ 同样走修复重试，而不是抛异常。"""
    permissive = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
    harness = harness_factory(
        schema=permissive,
        replies=[
            Reply(text='{"title": "ok", "extra": 1}'),
            Reply(text=VALID_TEXT),
        ],
    )
    result = await harness.run()

    assert result.ok is True
    assert result.attempts == 2
    assert harness.statuses() == ["schema_invalid", "ok"]


async def test_non_json_output_is_schema_invalid(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(replies=[Reply(text="这是一段散文，不是 JSON")])
    result = await harness.run()
    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_SCHEMA_INVALID)


# ══════════════════════════════════════════════════════════════════════
# ② 通道兜底：5xx / 超时 ⇒ 本地
# ══════════════════════════════════════════════════════════════════════


async def test_cloud_5xx_retries_then_switches_to_local(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_retries=1),
        replies=[
            Reply(error=http_error(500)),
            Reply(error=http_error(503)),
            Reply(text=VALID_TEXT),
        ],
    )
    result = await harness.run()

    assert result.ok is True
    assert result.profile == "local"
    assert harness.transport.profiles == ["cloud", "cloud", "local"]
    assert "transport_retry:cloud:1" in result.warnings
    assert harness.statuses() == ["http_error", "http_error", "ok"]


async def test_cloud_timeout_switches_to_local_and_records_timeout_status(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_retries=0),
        replies=[Reply(error=timeout_error()), Reply(text=VALID_TEXT)],
    )
    result = await harness.run()

    assert result.ok is True
    assert harness.transport.profiles == ["cloud", "local"]
    assert harness.statuses() == ["timeout", "ok"]


async def test_non_retryable_401_goes_straight_to_fallback(
    harness_factory: Callable[..., Harness],
) -> None:
    """4xx（非 429）重试不会变好 ⇒ 不浪费配额，直接切兜底。"""
    harness = harness_factory(
        config=llm_config(cloud_retries=3),
        replies=[Reply(error=http_error(401)), Reply(text=VALID_TEXT)],
    )
    result = await harness.run()

    assert result.ok is True
    assert harness.transport.profiles == ["cloud", "local"], "401 不重试"
    assert result.attempts == 2


async def test_rate_limit_is_retried_and_marked(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_retries=2),
        replies=[Reply(error=http_error(429)), Reply(text=VALID_TEXT)],
    )
    result = await harness.run()

    assert result.ok is True
    assert harness.statuses()[0] == "http_error"
    assert harness.rows()[0].error_message == "HTTP 429"


async def test_all_channels_failed_reports_last_error_and_attempt_count(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_retries=1, local_retries=0),
        replies=[Reply(error=timeout_error())],
    )
    result = await harness.run()

    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_TIMEOUT)
    assert result.attempts == 3, "云端 1+1 次 + 本地 1 次"
    assert harness.transport.profiles == ["cloud", "cloud", "local"]
    assert result.raw_text is None


async def test_route_without_fallback_stays_on_primary(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(replies=[Reply(text=VALID_TEXT)])
    result = await harness.run(agent="local_only")
    assert result.ok is True
    assert harness.transport.profiles == ["local"]


async def test_missing_route_returns_error_without_calling_anything(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory()
    result = await harness.run(agent="nobody")
    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_ROUTE_MISSING)
    assert harness.transport.calls == []
    assert harness.rows() == []


async def test_hard_timeout_caps_profile_timeout(
    harness_factory: Callable[..., Harness],
) -> None:
    """§04.1.1 硬约束 3：单次调用硬超时 120s（profile 写 900 也只给 120）。"""
    harness = harness_factory(
        config=llm_config(cloud_timeout=900, local_timeout=300),
        replies=[Reply(text=VALID_TEXT)],
        settings=GatewaySettings(hard_timeout_sec=120.0, backoff_base_sec=0.0),
    )
    await harness.run()
    assert harness.transport.calls[0][0].timeout_sec == 120.0


async def test_api_key_comes_from_env_and_seed_is_passed(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        replies=[Reply(text=VALID_TEXT), Reply(text=VALID_TEXT)],
        env={"STUDIO_LLM_API_KEY": "from-env"},
    )
    await harness.run()
    cloud_call = harness.transport.calls[0][0]
    assert cloud_call.api_key == "from-env"
    assert cloud_call.seed == 7
    assert cloud_call.json_mode is True

    await harness.run(agent="local_only")
    local_call = harness.transport.calls[1][0]
    assert local_call.api_key is None, "本地通道不需要密钥"


async def test_missing_schema_file_raises_config_error(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory()
    with pytest.raises(LlmError) as excinfo:
        await harness.gateway.complete(
            agent="demo",
            ctx=make_context(),
            prompt=demo_prompt(harness.prompts),
            schema_name="absent",
            parse=parse_demo,
        )
    assert excinfo.value.code is ErrorCode.LLM_SCHEMA_INVALID


# ══════════════════════════════════════════════════════════════════════
# ③ 预算闸门（R16）
# ══════════════════════════════════════════════════════════════════════


async def test_budget_under_limit_allows_cloud(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(with_budget=True, replies=[Reply(text=VALID_TEXT)])
    _seed_spend(harness, tokens=1_000)
    result = await harness.run()
    assert result.ok is True
    assert harness.transport.profiles == ["cloud"]


async def test_budget_switch_to_local_reroutes_to_local(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(on_exceed="switch_to_local", per_task_token_limit=1_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=1_000)
    result = await harness.run()

    assert result.ok is True
    assert harness.transport.profiles == ["local"], "超预算 ⇒ 换本地继续干"
    assert any(item.startswith("budget_exceeded:") for item in result.warnings)


async def test_budget_fail_task_stops_without_calling_provider(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(on_exceed="fail_task", per_task_token_limit=1_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=1_000)
    result = await harness.run()

    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_BUDGET_EXCEEDED)
    assert harness.transport.calls == [], "超预算就不要再问了"
    assert harness.statuses() == ["budget_exceeded"], "失败也要留痕"


async def test_budget_alert_only_keeps_cloud_and_warns(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(on_exceed="alert_only", per_task_token_limit=1_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=1_000)
    result = await harness.run()

    assert result.ok is True
    assert harness.transport.profiles == ["cloud"]
    assert any("budget_exceeded" in item for item in result.warnings)


async def test_day_cost_limit_triggers_reroute(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(on_exceed="switch_to_local", per_day_cost_usd_limit=0.01),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=10, cost=0.05)
    result = await harness.run()

    assert harness.transport.profiles == ["local"]
    assert any("当日成本" in item for item in result.warnings)


async def test_budget_remaining_hint_blocks_when_upstream_says_no_quota_left(
    harness_factory: Callable[..., Harness],
) -> None:
    """上游提示"没额度了"（``remaining=0``）⇒ 即使配置上限 60k 也要拦。

    §04.1.1：``limit = min(配置上限, 已花 + 剩余)``；``remaining=0`` ⇒ ``limit=已花``
    ⇒ 立刻判定超限（提示只可能收紧闸门，不可能放宽）。
    """
    harness = harness_factory(
        config=llm_config(on_exceed="fail_task", per_task_token_limit=60_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=500)
    result = await harness.run(ctx=make_context(remaining=0))

    assert result.ok is False
    assert result.error_code == str(ErrorCode.LLM_BUDGET_EXCEEDED)
    assert harness.transport.calls == [], "提示没额度 ⇒ 一次都别问"


async def test_budget_remaining_hint_still_allows_while_quota_left(
    harness_factory: Callable[..., Harness],
) -> None:
    """还剩 100 ⇒ 闸门收紧到 500+100=600，但已花 500 < 600 ⇒ 照常放行。"""
    harness = harness_factory(
        config=llm_config(on_exceed="fail_task", per_task_token_limit=60_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
    )
    _seed_spend(harness, tokens=500)
    result = await harness.run(ctx=make_context(remaining=100))

    assert result.ok is True
    assert harness.transport.profiles == ["cloud"]
    assert result.warnings == [], "没超限就不该有预算告警"


# ══════════════════════════════════════════════════════════════════════
# ④ 记账（llm_calls）
# ══════════════════════════════════════════════════════════════════════


async def test_successful_call_is_recorded_with_usage_and_cost(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_cost_in=1.0, cloud_cost_out=2.0),
        replies=[Reply(text=VALID_TEXT, input_tokens=10, output_tokens=20)],
    )
    result = await harness.run()

    assert len(harness.rows()) == 1
    row = harness.rows()[0]
    assert row.task_id == "t1"
    assert row.job_id == "j1"
    assert row.agent == "demo"
    assert row.engine == "oi_compatible"
    assert row.model == "cloud-model"
    assert row.is_local is False
    assert row.prompt_version == harness.prompts.prompt_version("demo")
    assert (row.input_tokens, row.output_tokens) == (10, 20)
    assert row.cost_usd == pytest.approx(0.05), "10/1000*1.0 + 20/1000*2.0"
    assert row.latency_ms == 12
    assert row.status is CallStatus.OK
    assert result.total_tokens == 30


async def test_local_call_is_marked_local_and_costs_zero(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(replies=[Reply(text=VALID_TEXT)])
    await harness.run(agent="local_only")

    row = harness.rows()[0]
    assert row.is_local is True
    assert row.engine == "ollama"
    assert row.cost_usd == 0.0


async def test_transport_failure_records_row_without_usage(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(
        config=llm_config(cloud_retries=0),
        replies=[Reply(error=http_error(500)), Reply(text=VALID_TEXT)],
    )
    await harness.run()

    failed = harness.rows()[0]
    assert failed.status is CallStatus.HTTP_ERROR
    assert failed.input_tokens is None
    assert failed.cost_usd is None
    assert failed.error_message == "HTTP 500"


async def test_prompt_version_is_written_on_every_attempt(
    harness_factory: Callable[..., Harness],
) -> None:
    harness = harness_factory(replies=[Reply(text=INVALID_TEXT)])
    await harness.run()
    expected = harness.prompts.prompt_version("demo")
    assert {row.prompt_version for row in harness.rows()} == {expected}


# ══════════════════════════════════════════════════════════════════════
# ⑤ 熔断 + 日志出口
# ══════════════════════════════════════════════════════════════════════


async def test_circuit_opens_after_repeated_failures_and_skips_cloud(
    harness_factory: Callable[..., Harness],
) -> None:
    """云端连续 3 次失败 ⇒ 第 4 次直接走本地，**不再撞云端**。"""
    replies = [
        Reply(error=http_error(401)),
        Reply(text=VALID_TEXT),
        Reply(error=http_error(401)),
        Reply(text=VALID_TEXT),
        Reply(error=http_error(401)),
        Reply(text=VALID_TEXT),
        Reply(text=VALID_TEXT),
    ]
    harness = harness_factory(
        config=llm_config(cloud_retries=0),
        replies=replies,
        breaker=CircuitBreaker(fail_threshold=3, open_sec=60.0),
    )
    for _ in range(3):
        assert (await harness.run()).ok is True

    cloud_calls = harness.transport.profiles.count("cloud")
    result = await harness.run()

    assert result.ok is True
    assert harness.transport.profiles.count("cloud") == cloud_calls, "熔断后不再尝试云端"
    assert any(item.startswith("circuit_open:cloud") for item in result.warnings)


async def test_log_sink_receives_circuit_and_failure_events(
    harness_factory: Callable[..., Harness],
) -> None:
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def sink(
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Any = None,
    ) -> None:
        events.append((str(level), source, message, dict(payload or {})))

    harness = harness_factory(
        config=llm_config(on_exceed="alert_only", per_task_token_limit=1_000),
        with_budget=True,
        replies=[Reply(text=VALID_TEXT)],
        log=sink,
    )
    _seed_spend(harness, tokens=1_000)
    await harness.run()

    assert any(source == "llm.budget" for _, source, _, _ in events)
    budget_event = next(item for item in events if item[1] == "llm.budget")
    assert budget_event[3]["code"] == str(ErrorCode.LLM_BUDGET_EXCEEDED)


async def test_failure_emits_error_log(
    harness_factory: Callable[..., Harness],
) -> None:
    events: list[tuple[str, str]] = []

    def sink(*, level: Severity, source: str, message: str, **rest: Any) -> None:
        events.append((str(level), source))

    harness = harness_factory(
        config=llm_config(cloud_retries=0, local_retries=0),
        replies=[Reply(error=timeout_error())],
        log=sink,
    )
    await harness.run()

    assert ("error", "llm.gateway") in events
