"""WriterAgent（T1.10 · §04.1.5）。

规则闸本身在 ``tests/unit/domain/test_script.py``；这里测 **Agent 的行为**：
切分不重写、禁区直接 block、重写 ≤2 轮、重写用尽后挑"最省人工"的一版。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentResult
from studio.agents.prompts import PromptLibrary
from studio.agents.writer import WriterAgent
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.script import (
    REWRITE_LIMIT,
    DirectorOutput,
    ScriptSegment,
    WriterInput,
    WriterOutput,
    build_draft,
)
from studio.domain.text import count_chars
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

#: 一句同时命中两个口癖的口播（9 个口播字）
CATCH_LINE = "这不科学，俺寻思也是。"


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


def outline() -> DirectorOutput:
    return DirectorOutput(
        hook_3s="熊大又整活了",
        segments=[
            ScriptSegment(seq=index, point=f"要点{index}", visual=f"画面{index}", mood="兴奋", est_chars=200)
            for index in range(1, 4)
        ],
        cta="点个关注看下集",
        est_duration_ms=140_000,
    )


def sentences(count: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "seq": index,
            "text": "这不科学，熊大又跑起来了。",
            "speaker": "bigbear" if index % 2 else "littlebear",
            "emotion": "兴奋",
            "pause_after_ms": 200,
        }
        for index in range(1, count + 1)
    ]


def writer_json(*, word_count: int = 700, **overrides: Any) -> str:
    body = CATCH_LINE + "熊" * max(0, word_count - count_chars(CATCH_LINE))
    payload: dict[str, Any] = {
        "title": "标题",
        "hook": "开场第一句",
        "body_md": body,
        "cta": "关注",
        "sentences": sentences(),
        "est_duration_ms": 140_000,
        "catchphrases_used": ["这不科学", "俺寻思"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def build(transport: ScriptedTransport, paths: StudioPaths, connection: sqlite3.Connection) -> WriterAgent:
    gateway = agent_gateway(paths, connection, transport, agents=["writer"])
    agent = WriterAgent(gateway, PromptLibrary.load(paths.prompts_dir))
    return agent


async def run(agent: WriterAgent, **kwargs: Any) -> AgentResult[WriterOutput]:
    return await agent.run(make_context(), WriterInput(topic=topic(), outline=outline(), **kwargs))


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_clean_script_passes_first_try(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=writer_json())])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert count_chars(result.data.body_md) == 700
        assert result.warnings == []
        assert len(transport.calls) == 1

    async def test_prompt_carries_the_outline(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=writer_json())])
        agent = build(transport, paths, connection)
        await run(agent)
        _, messages = transport.calls[0]
        assert "熊大又整活了" in messages[-1].content
        assert "点个关注看下集" in messages[-1].content
        assert "这不科学" in messages[0].content  # 口癖清单在 system 侧

    async def test_revision_notes_are_rendered(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=writer_json())])
        agent = build(transport, paths, connection)
        await run(agent, revision_notes=["开头太平", "第二段太快"])
        _, messages = transport.calls[0]
        assert "开头太平" in messages[-1].content


# ══════════════════════════════════════════════════════════════════════
# 切分闸：切分而不是重写
# ══════════════════════════════════════════════════════════════════════


class TestSentenceSplit:
    async def test_long_sentence_is_split_without_rewriting(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        rows = sentences(19)
        rows.append({"seq": 20, "text": "熊大说这不科学，" * 5, "speaker": "bigbear"})
        transport = ScriptedTransport(replies=[Reply(text=writer_json(sentences=rows))])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert len(transport.calls) == 1  # **没有重写**
        assert any(item.startswith("sentence_split:") for item in result.warnings)
        # Agent 返回的是**原始** WriterOutput；切分发生在 build_draft（服务层再调一次）
        draft, report = build_draft(
            outline=outline(),
            output=result.data,
            catchphrases=list(persona().catchphrases),
            forbidden=list(persona().forbidden),
        )
        assert report.split_count >= 1
        assert all(len(item.text) <= 28 for item in draft.sentences)


# ══════════════════════════════════════════════════════════════════════
# 禁区：直接 block
# ══════════════════════════════════════════════════════════════════════


class TestForbidden:
    async def test_forbidden_hit_blocks_without_rewriting(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=writer_json(word_count=700))])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok  # 先确认基线是干净的

        blocked = ScriptedTransport(
            replies=[Reply(text=writer_json(word_count=700, body_md="说点脏话" + "熊" * 690))]
        )
        blocked_agent = build(blocked, paths, connection)
        outcome = await run(blocked_agent)
        assert not outcome.ok
        assert outcome.error_code == str(ErrorCode.SCRIPT_FORBIDDEN)
        assert len(blocked.calls) == 1  # **不重写**：合规问题必须让人看见
        assert any(item.startswith("forbidden:") for item in outcome.warnings)


# ══════════════════════════════════════════════════════════════════════
# 重写闸
# ══════════════════════════════════════════════════════════════════════


class TestRewrite:
    async def test_short_script_is_rewritten_once(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=writer_json(word_count=500)), Reply(text=writer_json())]
        )
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2
        assert count_chars(result.data.body_md) == 700

    async def test_rewrite_hint_names_the_problem(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=writer_json(word_count=500)), Reply(text=writer_json())]
        )
        agent = build(transport, paths, connection)
        await run(agent)
        _, second = transport.calls[1]
        assert "上一版不合格" in second[0].content
        assert "字数" in second[0].content

    async def test_rewrite_is_capped_and_best_effort_is_returned(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=writer_json(word_count=500))])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok  # 有稿就用（不整批失败）
        assert len(transport.calls) == 1 + REWRITE_LIMIT
        assert any(item.startswith("rewrite:") for item in result.warnings)
        assert any(item.startswith("script:字数") for item in result.warnings)

    async def test_closest_version_wins(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        """三版都越界 ⇒ 取**离合格最近**的那一版（500 与 900 距离相同 ⇒ 保留先出现的）。"""
        transport = ScriptedTransport(
            replies=[
                Reply(text=writer_json(word_count=500)),
                Reply(text=writer_json(word_count=900)),
                Reply(text=writer_json(word_count=900)),
            ]
        )
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok and result.data is not None
        assert count_chars(result.data.body_md) == 500

    async def test_catchphrase_shortfall_triggers_rewrite(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        weak = writer_json(word_count=700, body_md="这不科学。" + "熊" * 694)
        transport = ScriptedTransport(replies=[Reply(text=weak), Reply(text=writer_json())])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok
        assert len(transport.calls) == 2
        assert "口癖" in transport.calls[1][1][0].content


# ══════════════════════════════════════════════════════════════════════
# 留痕与失败
# ══════════════════════════════════════════════════════════════════════


class TestWarningsAndFailures:
    async def test_self_reported_catchphrase_is_rechecked(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=writer_json(catchphrases_used=["这不科学", "俺寻思", "编的口癖"]))]
        )
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert any("catchphrase_self_report:编的口癖" in item for item in result.warnings)

    async def test_single_speaker_is_only_a_warning(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        rows = [dict(item, speaker="bigbear") for item in sentences()]
        transport = ScriptedTransport(replies=[Reply(text=writer_json(sentences=rows))])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert result.ok
        assert any(item.startswith("single_speaker:") for item in result.warnings)

    async def test_transport_failure_is_reported_not_raised(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(error=timeout_error())])
        agent = build(transport, paths, connection)
        result = await run(agent)
        assert not result.ok
        assert result.error_code == "LLM_TIMEOUT"
