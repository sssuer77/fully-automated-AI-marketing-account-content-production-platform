"""OutlinerAgent（文案三级 · 二级：视频标题 + 核心论点）。

这一级刻意**没有规则闸**（产物只有两个自由文本字段，除了长度没有可机检的东西，
而长度已经由 JSON Schema 卡住）。所以这里测的是接线本身：输入块怎么拼、产物怎么回、
坏 JSON 与传输挂掉分别是什么下场 —— 提示词与 schema 用**仓库真实的**那两份。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentResult
from studio.agents.outliner import OutlinerAgent
from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.script import OUTLINE_TITLE_MAX, OutlineInput, OutlineOutput
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

#: 一份合法的二级产物（标题 12 字 / 论点 18 字，都在上限内）
TITLE = "这一跳为什么没人跳得过去"
ARGUMENT = "新手翻车都在起跳前多按了一下空格"


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ ``prompts/`` 与 ``schemas/`` 用的是**生产那两份**。"""
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


def topic(*, angle: str = "只讲那一跳") -> TopicSpec:
    return TopicSpec(
        title="MC跑酷最难的一跳",
        hook_type="suspense",
        angle=angle,
        exec_feasible=True,
        score=8.5,
        reason="钩子够硬",
    )


def outliner_json(*, title: str = TITLE, core_argument: str = ARGUMENT) -> str:
    return json.dumps({"title": title, "core_argument": core_argument}, ensure_ascii=False)


def build(transport: ScriptedTransport, paths: StudioPaths, connection: sqlite3.Connection) -> OutlinerAgent:
    gateway = agent_gateway(paths, connection, transport, agents=["outliner"])
    return OutlinerAgent(gateway, PromptLibrary.load(paths.prompts_dir))


async def run(agent: OutlinerAgent, **kwargs: Any) -> AgentResult[OutlineOutput]:
    return await agent.run(make_context(), OutlineInput(topic=topic(), **kwargs))


def user_message(transport: ScriptedTransport) -> str:
    """第 ``0`` 次调用的 user 消息（``render`` 把 system 放前、user 放后）。"""
    return transport.calls[0][1][-1].content


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_a_clean_reply_lands_on_the_first_try(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=outliner_json())])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert result.data.title == TITLE
        assert result.data.core_argument == ARGUMENT
        assert result.warnings == []
        # 这一级没有规则重试：一条合格回复就该只用掉一次调用
        assert len(transport.calls) == 1
        assert result.prompt_version  # 溯源：落 ``topic_outlines.prompt_version`` 用

    async def test_prompt_carries_the_topic_angle_and_reason(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=outliner_json())])
        agent = build(transport, paths, connection)
        await run(agent)
        user = user_message(transport)
        assert "MC跑酷最难的一跳" in user
        assert "只讲那一跳" in user
        assert "钩子够硬" in user
        assert persona().name in transport.calls[0][1][0].content

    async def test_the_callers_angle_wins_over_the_topics_own(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """调用方显式给了角度 ⇒ 用它（选题自带的角度只是缺位时的兜底）。"""
        transport = ScriptedTransport(replies=[Reply(text=outliner_json())])
        agent = build(transport, paths, connection)
        await run(agent, angle="调用方给的角度")
        user = user_message(transport)
        assert "调用方给的角度" in user
        assert "只讲那一跳" not in user


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_transport_failure_is_reported_not_raised(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(error=timeout_error())])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.error_code == "LLM_TIMEOUT"
        assert result.data is None

    async def test_an_overlong_title_is_repaired_not_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """标题超 60 字 ⇒ JSON Schema 当场拒绝（走网关的修复重试），不会流到库里。"""
        transport = ScriptedTransport(
            replies=[Reply(text=outliner_json(title="熊" * (OUTLINE_TITLE_MAX + 1)))]
        )
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.error_code is not None

    async def test_a_missing_core_argument_is_not_papered_over(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """少一个字段就是不合契约：不拿空串顶上（空论点等于三级没有主线）。"""
        transport = ScriptedTransport(replies=[Reply(text=json.dumps({"title": TITLE}, ensure_ascii=False))])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.data is None
