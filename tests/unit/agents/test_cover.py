"""CoverAgent（T5.1 · §04.1.8 / §06.3）。

只测 **Agent 的行为**：变量注入对不对、命中禁区之后怎么表达。
排版的规则在 ``tests/unit/domain/test_cover.py``，合成的规则在
``tests/unit/publish/test_cover.py`` —— 这里一个 ffmpeg 都不跑。

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
from studio.agents.cover import CoverAgent
from studio.agents.prompts import PromptLibrary
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.cover import CoverInput, CoverOutput
from tests.unit.agents.fakes import Reply, ScriptedTransport, agent_gateway, make_context

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


@pytest.fixture
def prompts(paths: StudioPaths) -> PromptLibrary:
    return PromptLibrary.load(paths.prompts_dir)


def cover_json(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "title_text": "离谱跑酷地图",
        "sub_text": "点个关注",
        "highlight_words": ["跑酷"],
        "frame_at_ms": 500,
        "banned_checked": True,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _all_text(messages: Any) -> str:
    """system + user 两段拼起来（提示词纪律与这次的具体输入分居两处）。"""
    return chr(10).join(message.content for message in messages)


def cover_input(**overrides: Any) -> CoverInput:
    data: dict[str, Any] = {
        "task_id": "t1",
        "title": "离谱跑酷地图",
        "hook": "今天我们来看一张特别离谱的跑酷地图",
        "cta": "点个关注",
        "hook_start_ms": 0,
        "duration_ms": 17_482,
    }
    data.update(overrides)
    return CoverInput.model_validate(data)


async def _run(
    paths: StudioPaths,
    connection: sqlite3.Connection,
    prompts: PromptLibrary,
    transport: ScriptedTransport,
    payload: CoverInput | None = None,
) -> AgentResult[CoverOutput]:
    gateway = agent_gateway(paths, connection, transport, agents=["cover"])
    agent = CoverAgent(gateway, prompts)
    return await agent.run(make_context(), payload or cover_input())


class TestMetadata:
    def test_name_and_route(self) -> None:
        assert CoverAgent.name == "cover"
        assert CoverAgent.profile_key == "cover"
        assert CoverAgent.schema_name == "cover_result"

    def test_route_exists_in_the_shipped_config(self) -> None:
        """``config/llm.yaml`` 里必须有 ``cover`` 这条路由 —— 缺了网关直接 ``LLM_ROUTE_MISSING``。"""
        text = (REPO_ROOT / "config" / "llm.yaml").read_text(encoding="utf-8")
        assert "cover:" in text


class TestPromptWiring:
    async def test_happy_path_returns_the_output_model(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        result = await _run(paths, connection, prompts, ScriptedTransport([Reply(text=cover_json())]))
        assert result.ok is True
        assert isinstance(result.data, CoverOutput)
        assert result.data.title_text == "离谱跑酷地图"

    async def test_duration_and_default_frame_reach_the_prompt(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """模型要靠这两个数把抽帧点钳在片子里 —— 少一个它就只能瞎猜。"""
        transport = ScriptedTransport([Reply(text=cover_json())])
        await _run(paths, connection, prompts, transport, cover_input(hook_start_ms=1200, duration_ms=17_482))
        # 硬约束写在 ``cover/system.md`` 里（角色与纪律），稿件摘要写在 ``user.jinja`` 里
        # —— 两段都要看：只看 user 会以为"这两个数没传进去"。
        text = _all_text(transport.calls[0][1])
        assert "17482" in text
        assert "1700" in text  # 1200 + 500

    async def test_missing_fields_are_spelled_out_not_left_blank(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """空标题渲染成"标题："会让模型顺着空格往下编 ⇒ 显式写"（无标题）"。"""
        transport = ScriptedTransport([Reply(text=cover_json())])
        await _run(paths, connection, prompts, transport, cover_input(title="", hook="", cta=""))
        user = _all_text(transport.calls[0][1])
        assert "（无标题）" in user
        assert "（无钩子）" in user
        assert "（无 CTA）" in user

    async def test_persona_forbidden_is_injected(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """禁区词由 ``persona_variables`` 自动注入（fake 的 persona 是"脏话、政治"）。"""
        transport = ScriptedTransport([Reply(text=cover_json())])
        await _run(paths, connection, prompts, transport)
        assert "脏话" in _all_text(transport.calls[0][1])


class TestForbiddenRecheck:
    async def test_clean_text_passes_through(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        result = await _run(paths, connection, prompts, ScriptedTransport([Reply(text=cover_json())]))
        assert result.ok is True

    async def test_hit_in_title_fails_without_retrying(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """提示词里写了"绝不触碰禁区"，但**提示词不是保证** ⇒ 拿到结果之后必须再判一次。

        判出命中**不重试**：改一处、犯另一处是重试的常见结局，而封面的正确动作
        是退回规则兜底（调用方的事），不是再赌一次。
        """
        transport = ScriptedTransport([Reply(text=cover_json(title_text="脏话连篇的跑酷"))])
        result = await _run(paths, connection, prompts, transport)
        assert result.ok is False
        assert result.error_code == ErrorCode.PRECHECK_BANNED.value
        assert "脏话" in (result.error_message or "")
        assert len(transport.calls) == 1  # 只调了一次

    async def test_hit_in_sub_text_also_fails(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        transport = ScriptedTransport([Reply(text=cover_json(sub_text="政治相关的次文案"))])
        result = await _run(paths, connection, prompts, transport)
        assert result.ok is False
        assert result.error_code == ErrorCode.PRECHECK_BANNED.value

    async def test_the_call_is_still_accounted_for(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """这一次调用**真的发生了**（token 花了）⇒ 结果里要留着用量，别抹成"没调过"。"""
        transport = ScriptedTransport([Reply(text=cover_json(title_text="脏话连篇的跑酷"))])
        result = await _run(paths, connection, prompts, transport)
        assert result.input_tokens > 0
        assert result.output_tokens > 0
        assert result.model

    async def test_warning_names_the_word(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        transport = ScriptedTransport([Reply(text=cover_json(title_text="脏话连篇的跑酷"))])
        result = await _run(paths, connection, prompts, transport)
        assert any("cover_banned" in item for item in result.warnings)


class TestForbiddenRecheckFallsBackToPersona:
    async def test_empty_payload_terms_use_the_persona_list(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """调用方没给词表 ⇒ 用 persona 的。提示词里注入的是同一份，两边不能各说各话。"""
        transport = ScriptedTransport([Reply(text=cover_json(title_text="脏话连篇的跑酷"))])
        result = await _run(paths, connection, prompts, transport, cover_input(forbidden=[]))
        assert result.ok is False
        assert result.error_code == ErrorCode.PRECHECK_BANNED.value

    async def test_payload_terms_take_precedence(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """调用方给了词表就**只用它**（全站词表由服务层合并，Agent 不自己再去读一遍）。"""
        transport = ScriptedTransport([Reply(text=cover_json(title_text="脏话连篇的跑酷"))])
        result = await _run(paths, connection, prompts, transport, cover_input(forbidden=["别的词"]))
        assert result.ok is True


class TestSchemaViolations:
    async def test_too_long_title_is_repaired_not_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """21 字超契约 ⇒ 走网关的修复重试（而不是悄悄截断后当成功）。"""
        transport = ScriptedTransport(
            [Reply(text=cover_json(title_text="一" * 21)), Reply(text=cover_json())]
        )
        result = await _run(paths, connection, prompts, transport)
        assert result.ok is True
        assert result.attempts >= 2

    async def test_garbage_output_is_repaired(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        transport = ScriptedTransport([Reply(text="这不是 JSON"), Reply(text=cover_json())])
        result = await _run(paths, connection, prompts, transport)
        assert result.ok is True

    async def test_all_attempts_fail_returns_error_not_exception(
        self, paths: StudioPaths, connection: sqlite3.Connection, prompts: PromptLibrary
    ) -> None:
        """**不抛裸异常**：调用方拿到 ``ok=False``，然后退规则兜底。"""
        transport = ScriptedTransport([Reply(text="这不是 JSON")])
        result = await _run(paths, connection, prompts, transport)
        assert result.ok is False
        assert result.error_code
        assert result.data is None
