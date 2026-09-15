"""EditorAgent（T1.11 · §04.1.6 / 原文 §2.2⑥）。

``check_edit`` 本身在 ``tests/unit/domain/test_scoring.py``；这里测 **Agent 的行为**：

- 越界 ⇒ 带提示重试 **1 次**；
- 仍越界 ⇒ **照收 + warnings**（不判失败：照收一个多润色一句的稿子仍然能用，
  判失败却白烧一轮并把任务拖进 ``failed``）；
- 整份回写而不是 diff（裁定 98）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentResult
from studio.agents.editor import EditorAgent
from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.scoring import EDIT_RETRIES, EditorInput, EditorOutput, ReviewIssue
from studio.domain.script import DirectorOutput, ScriptSegment, SentenceSpec, WriterOutput
from studio.domain.topics import TopicSpec
from tests.unit.agents.fakes import (
    Reply,
    ScriptedTransport,
    agent_gateway,
    make_context,
    timeout_error,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 每一句都长这样（改没改一眼可辨）
LINE = "这不科学，熊大又跑起来了。"


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


# ══════════════════════════════════════════════════════════════════════
# 构造器
# ══════════════════════════════════════════════════════════════════════


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


def sentence_rows() -> list[dict[str, Any]]:
    return [
        {
            "seq": index,
            "text": LINE,
            "speaker": "bigbear" if index % 2 else "littlebear",
            "emotion": "兴奋",
            "pause_after_ms": 200,
        }
        for index in range(1, 21)
    ]


def script() -> WriterOutput:
    return WriterOutput(
        title="MC跑酷最难的一跳",
        hook="熊大又整活了",
        body_md="这不科学，俺寻思也是。" + "熊" * 691,
        cta="点个关注看下集",
        sentences=[SentenceSpec.model_validate(item) for item in sentence_rows()],
        est_duration_ms=140_000,
        catchphrases_used=["这不科学", "俺寻思"],
    )


def issue(target: str = "hook") -> ReviewIssue:
    return ReviewIssue(
        code="weak_hook",
        severity="major",
        target=target,
        detail="开场太平",
        suggestion="把结果前置",
    )


def edited(index: int, text: str) -> list[dict[str, Any]]:
    """把第 ``index`` 句（1-based）换成 ``text`` 的句子表。"""
    return [{**item, "text": text} if item["seq"] == index else item for item in sentence_rows()]


def editor_json(
    *,
    sentences: list[dict[str, Any]] | None = None,
    hook: str = "熊大又整活了",
    cta: str = "点个关注看下集",
    changes: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "script": {
                "title": "MC跑酷最难的一跳",
                "hook": hook,
                "body_md": "这不科学，俺寻思也是。" + "熊" * 691,
                "cta": cta,
                "sentences": sentences if sentences is not None else sentence_rows(),
                "est_duration_ms": 140_000,
                "catchphrases_used": ["这不科学", "俺寻思"],
            },
            "changes": changes if changes is not None else ["把开场换成结果前置"],
        },
        ensure_ascii=False,
    )


def payload(*, issues: list[ReviewIssue] | None = None, round_no: int = 1) -> EditorInput:
    return EditorInput(
        topic=topic(),
        script=script(),
        outline=outline(),
        issues=issues if issues is not None else [issue()],
        round_no=round_no,
    )


def build(transport: ScriptedTransport, paths: StudioPaths, connection: sqlite3.Connection) -> EditorAgent:
    gateway = agent_gateway(paths, connection, transport, agents=["editor"])
    return EditorAgent(gateway, PromptLibrary.load(paths.prompts_dir))


async def run(agent: EditorAgent, **kwargs: Any) -> AgentResult[EditorOutput]:
    return await agent.run(make_context(), payload(**kwargs))


def prompt_text(transport: ScriptedTransport, index: int = 0) -> str:
    """第 ``index`` 次调用的全部消息文本（system + user 拼起来查断言）。"""
    _, messages = transport.calls[index]
    return "\n".join(message.content for message in messages)


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_an_in_scope_edit_comes_back_clean(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=editor_json(sentences=edited(1, "熊大这一跳直接封神。")))]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert result.data.script.sentences[0].text == "熊大这一跳直接封神。"
        assert result.data.changes == ["把开场换成结果前置"]
        assert result.warnings == []
        assert len(transport.calls) == 1

    async def test_an_untouched_script_is_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        result = await run(build(transport, paths, connection))
        assert result.ok and result.warnings == []

    async def test_the_round_no_reaches_the_model(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        await run(build(transport, paths, connection), round_no=2)
        assert "第 2 轮" in prompt_text(transport)

    async def test_the_issues_are_rendered_with_their_targets(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        await run(build(transport, paths, connection), issues=[issue("segment:2")])
        text = prompt_text(transport)
        assert "`segment:2`" in text
        assert "开场太平" in text
        assert "把结果前置" in text

    async def test_the_sentence_table_reaches_the_model(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        await run(build(transport, paths, connection))
        assert f"第 1 句（bigbear）：{LINE}" in prompt_text(transport)

    async def test_no_issues_says_change_nothing(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        await run(build(transport, paths, connection), issues=[])
        assert "一个字都不要改" in prompt_text(transport)

    async def test_the_persona_is_rendered(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        await run(build(transport, paths, connection))
        assert "熊大熊二" in prompt_text(transport)

    async def test_prompt_version_is_recorded(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json())])
        result = await run(build(transport, paths, connection))
        assert result.prompt_version.startswith("1+")


# ══════════════════════════════════════════════════════════════════════
# 越界：重试 1 次 ⇒ 照收 + 留痕
# ══════════════════════════════════════════════════════════════════════


class TestOutOfScope:
    def test_the_retry_budget_is_one(self) -> None:
        assert EDIT_RETRIES == 1

    async def test_an_out_of_scope_edit_is_retried_once(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[
                Reply(text=editor_json(sentences=edited(8, "顺手改了第8句。"))),
                Reply(text=editor_json(sentences=edited(1, "熊大这一跳直接封神。"))),
            ]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2
        assert result.data.script.sentences[0].text == "熊大这一跳直接封神。"

    async def test_the_retry_hint_names_the_offending_sentence(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[
                Reply(text=editor_json(sentences=edited(8, "顺手改了第8句。"))),
                Reply(text=editor_json(sentences=edited(1, "熊大这一跳直接封神。"))),
            ]
        )
        await run(build(transport, paths, connection))
        assert "第8句" in prompt_text(transport, 1)

    async def test_the_retry_is_recorded_in_the_warnings(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[
                Reply(text=editor_json(sentences=edited(8, "顺手改了第8句。"))),
                Reply(text=editor_json(sentences=edited(1, "熊大这一跳直接封神。"))),
            ]
        )
        result = await run(build(transport, paths, connection))
        assert result.warnings == ["edit:retry:edited:out_of_scope:第8句"]

    async def test_a_persistently_out_of_scope_edit_is_accepted_with_warnings(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """裁定 96：照收 + 有人看得见的标记，好过白烧一轮再判失败。"""
        transport = ScriptedTransport(
            replies=[Reply(text=editor_json(sentences=edited(8, "顺手改了第8句。")))]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 1 + EDIT_RETRIES
        assert "edit:retry:edited:out_of_scope:第8句" in result.warnings
        assert "edit:edited:out_of_scope:第8句" in result.warnings
        assert result.data.script.sentences[7].text == "顺手改了第8句。"

    async def test_an_unauthorised_hook_change_is_reported(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text=editor_json(hook="换了个新开场", sentences=edited(1, "新开场句。")))]
        )
        result = await run(build(transport, paths, connection), issues=[issue("cta")])
        assert result.ok
        assert any("edited:out_of_scope:hook" in item for item in result.warnings)

    async def test_an_unauthorised_cta_change_is_reported(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=editor_json(cta="换了个新结尾"))])
        result = await run(build(transport, paths, connection), issues=[issue("hook")])
        assert result.ok
        assert any("edited:out_of_scope:cta" in item for item in result.warnings)

    async def test_a_global_issue_authorises_everything(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[
                Reply(
                    text=editor_json(
                        hook="新开场",
                        cta="新结尾",
                        sentences=edited(20, "重写了最后一句。"),
                    )
                )
            ]
        )
        result = await run(build(transport, paths, connection), issues=[issue("global")])
        assert result.ok and result.warnings == []
        assert len(transport.calls) == 1


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_invalid_json_is_repaired_by_the_gateway(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text="{不是 JSON"), Reply(text=editor_json())])
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2

    async def test_a_timeout_is_returned_not_raised(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(error=timeout_error())])
        result = await run(build(transport, paths, connection))
        assert not result.ok and result.data is None
        assert result.error_code == "LLM_TIMEOUT"

    async def test_a_short_sentence_table_is_repaired(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        short = json.loads(editor_json())
        short["script"]["sentences"] = short["script"]["sentences"][:3]
        transport = ScriptedTransport(
            replies=[Reply(text=json.dumps(short, ensure_ascii=False)), Reply(text=editor_json())]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2
