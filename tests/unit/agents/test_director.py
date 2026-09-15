"""DirectorAgent（T1.10 · §04.1.4）。

大纲的规则闸在 ``domain/script.py`` 里已经有穷举用例；这里只测 **Agent 的行为**：
重试几次、什么时候降级、失败怎么表达（**不抛裸异常**）。

提示词与 schema 用的是**仓库真实文件**：契约漂移在这里就会红。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentResult
from studio.agents.director import OUTLINE_RETRIES, DirectorAgent
from studio.agents.gateway import LlmGateway
from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.script import DirectorInput, DirectorOutput
from studio.domain.topics import TopicSpec
from tests.unit.agents.fakes import (
    Reply,
    ScriptedTransport,
    agent_gateway,
    make_context,
    persona,
    timeout_error,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")


@pytest.fixture
def connection(tmp_path: Path, paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


def topic() -> TopicSpec:
    return TopicSpec(
        title="MC跑酷最难的一跳",
        hook_type="suspense",
        angle="只讲那一跳",
        exec_feasible=True,
        score=8.5,
        reason="钩子够硬",
    )


def director_json(*, segments: int = 3, chars: int = 200, **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "hook_3s": "熊大又整活了",
        "segments": [
            {
                "seq": index,
                "point": f"要点{index}",
                "visual": f"画面{index}",
                "mood": "兴奋",
                "est_chars": chars,
            }
            for index in range(1, segments + 1)
        ],
        "cta": "点个关注看下集",
        "est_duration_ms": 140_000,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def build(
    transport: ScriptedTransport, paths: StudioPaths, connection: sqlite3.Connection
) -> tuple[DirectorAgent, LlmGateway]:
    gateway = agent_gateway(paths, connection, transport, agents=["director"])
    agent = DirectorAgent(gateway, PromptLibrary.load(paths.prompts_dir))
    return agent, gateway


async def run(agent: DirectorAgent, **kwargs: Any) -> AgentResult[DirectorOutput]:
    payload = DirectorInput(topic=topic(), **kwargs)
    return await agent.run(make_context(), payload)


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_valid_outline_passes_first_try(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=director_json())])
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert len(result.data.segments) == 3
        assert result.warnings == []
        assert len(transport.calls) == 1

    async def test_prompt_carries_the_topic_and_persona(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=director_json())])
        agent, _ = build(transport, paths, connection)
        await run(agent, target_duration_ms=150_000)
        _, messages = transport.calls[0]
        user = messages[-1].content
        assert "MC跑酷最难的一跳" in user
        assert "只讲那一跳" in user
        assert "150000" in user  # 目标时长（毫秒）
        assert persona().name in messages[0].content

    async def test_five_segments_are_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=director_json(segments=5, chars=150))])
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert len(result.data.segments) == 5


# ══════════════════════════════════════════════════════════════════════
# 规则闸：重试与降级
# ══════════════════════════════════════════════════════════════════════


class TestRuleGate:
    async def test_bad_outline_is_retried_then_degraded(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        bad = director_json(segments=3, chars=80)  # Σ=240，远低于 600
        transport = ScriptedTransport(replies=[Reply(text=bad)])
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok  # 降级返回：有结构就用
        assert len(transport.calls) == 1 + OUTLINE_RETRIES
        assert any(item.startswith("outline:") for item in result.warnings)

    async def test_retry_hint_is_sent_back_to_the_model(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=director_json(chars=80)), Reply(text=director_json())]
        )
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.warnings == []
        assert len(transport.calls) == 2
        _, second = transport.calls[1]
        assert "上一版大纲不合格" in second[0].content  # 重试提示在 system 侧

    async def test_recovers_on_the_second_attempt(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[
                Reply(text=director_json(chars=80)),
                Reply(text=director_json(chars=80)),
                Reply(text=director_json()),
            ]
        )
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert len(transport.calls) == 3


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_transport_failure_is_reported_not_raised(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(error=timeout_error())])
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.error_code == "LLM_TIMEOUT"
        assert result.data is None

    async def test_schema_violation_is_repaired_not_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """段数 2 ⇒ JSON Schema 当场拒绝（修复重试），不会流到规则闸。"""
        transport = ScriptedTransport(replies=[Reply(text=director_json(segments=2))])
        agent, _ = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.error_code is not None
