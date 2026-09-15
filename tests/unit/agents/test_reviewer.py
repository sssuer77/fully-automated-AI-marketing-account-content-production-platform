"""ReviewerAgent（T1.11 · §04.1.6 / 原文 §2.2⑤）。

规则通道本身在 ``tests/unit/domain/test_scoring.py``；这里测 **Agent 的行为**：

- 只产出六维度 + issues（``total`` / ``grade`` 由服务端算，裁定 89）；
- "低分却没列问题"只留痕（``review:score_issue_mismatch``），不拦、不重试；
- 提示词里带上了服务端已算好的事实（字数 / 禁区 / 重复度）；
- 失败走 ``AgentResult``（``ok=False`` + ``error_code``），不抛裸异常。
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
from studio.agents.reviewer import MISMATCH_THRESHOLD, ReviewerAgent, banned_summary
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.scoring import (
    LLM_DIMENSIONS,
    BannedHit,
    ChannelItem,
    ReviewerInput,
    ReviewerOutput,
    RuleChannelDetail,
)
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


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ 提示词与 schema 用的是**生产那两份**。"""
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


def script() -> WriterOutput:
    return WriterOutput(
        title="MC跑酷最难的一跳",
        hook="熊大又整活了",
        body_md="这不科学，俺寻思也是。" + "熊" * 691,
        cta="点个关注看下集",
        sentences=[
            SentenceSpec(
                seq=index,
                text="这不科学，熊大又跑起来了。",
                speaker="bigbear" if index % 2 else "littlebear",
                emotion="兴奋",
                pause_after_ms=200,
            )
            for index in range(1, 21)
        ],
        est_duration_ms=140_000,
        catchphrases_used=["这不科学", "俺寻思"],
    )


def rule_detail(*, banned: bool = False) -> RuleChannelDetail:
    return RuleChannelDetail(
        length=ChannelItem(score=10.0, comment="700 字（要求 600–800）"),
        banned_hits=[BannedHit(term="脏话", count=3)] if banned else [],
        opening_ok=True,
        ending_ok=True,
        paragraph_dup_ratio=0.0,
        chars=700,
        est_duration_ms=140_000,
        catchphrases_hit=2,
    )


def channel(score: float) -> ChannelItem:
    return ChannelItem(score=score, comment="测试评语")


def reviewer_json(*scores: float, issues: list[dict[str, Any]] | None = None) -> str:
    """一份合法的 Reviewer 回应（六维度同分时传 1 个值即可）。"""
    values = list(scores) * (6 // len(scores)) if len(scores) in {1, 2, 3} else list(scores)
    return json.dumps(
        {
            "llm_detail": {
                name: {"score": score, "comment": "测试评语"}
                for name, score in zip(LLM_DIMENSIONS, values, strict=True)
            },
            "issues": issues if issues is not None else [],
        },
        ensure_ascii=False,
    )


ISSUE: dict[str, Any] = {
    "code": "weak_hook",
    "severity": "major",
    "target": "hook",
    "detail": "开场太平",
    "suggestion": "把结果前置",
}


def build(transport: ScriptedTransport, paths: StudioPaths, connection: sqlite3.Connection) -> ReviewerAgent:
    gateway = agent_gateway(paths, connection, transport, agents=["reviewer"])
    return ReviewerAgent(gateway, PromptLibrary.load(paths.prompts_dir))


def payload(**overrides: Any) -> ReviewerInput:
    data: dict[str, Any] = {
        "topic": topic(),
        "script": script(),
        "outline": outline(),
        "rule_detail": rule_detail(),
        "round_no": 1,
    }
    data.update(overrides)
    return ReviewerInput(**data)


async def run(agent: ReviewerAgent, **kwargs: Any) -> AgentResult[ReviewerOutput]:
    return await agent.run(make_context(), payload(**kwargs))


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_six_dimensions_and_issues_come_back(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert result.data.llm_detail.scores() == [8.0] * 6
        assert [item.target for item in result.data.issues] == ["hook"]
        assert len(transport.calls) == 1

    async def test_the_model_is_not_asked_for_a_total_or_a_grade(self) -> None:
        """裁定 89：让模型报总分 = 让考生自己判卷。"""
        assert set(ReviewerOutput.model_fields) == {"llm_detail", "issues"}
        assert "total" not in ReviewerOutput.model_fields
        assert "grade" not in ReviewerOutput.model_fields

    async def test_the_rule_detail_is_rendered_into_the_prompt(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        await run(build(transport, paths, connection))
        _, messages = transport.calls[0]
        assert "700 字" in messages[-1].content
        assert "未命中" in messages[-1].content
        assert "第 1 轮" in messages[-1].content

    async def test_banned_hits_are_named_with_their_counts(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        await run(build(transport, paths, connection), rule_detail=rule_detail(banned=True))
        _, messages = transport.calls[0]
        assert "脏话×3" in messages[-1].content

    async def test_the_persona_block_leads_the_user_message(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """人物是**随时可改**的 ⇒ 每次调用都要现读一份 persona，不能烘死在提示词里。"""
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        await run(build(transport, paths, connection))
        _, messages = transport.calls[0]
        assert "熊大熊二" in messages[-1].content
        assert "这不科学" in messages[-1].content
        assert "脏话" in messages[-1].content

    async def test_the_script_body_reaches_the_model(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        await run(build(transport, paths, connection))
        _, messages = transport.calls[0]
        assert "这不科学，俺寻思也是。" in messages[-1].content
        assert "熊大又整活了" in messages[-1].content

    async def test_prompt_version_is_recorded(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(8.0, issues=[ISSUE]))])
        result = await run(build(transport, paths, connection))
        assert result.prompt_version.startswith("1+")
        assert result.model == "cloud-model"


# ══════════════════════════════════════════════════════════════════════
# 分数与问题对不上：只留痕
# ══════════════════════════════════════════════════════════════════════


class TestScoreIssueMismatch:
    def test_the_threshold_is_six(self) -> None:
        assert MISMATCH_THRESHOLD == 6.0

    async def test_a_low_dimension_without_issues_is_flagged(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(4.0))])
        result = await run(build(transport, paths, connection))
        assert result.ok
        assert result.warnings == [f"review:score_issue_mismatch:{name}" for name in LLM_DIMENSIONS]

    async def test_issues_silence_the_flag(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(4.0, issues=[ISSUE]))])
        result = await run(build(transport, paths, connection))
        assert result.warnings == []

    async def test_high_scores_are_not_flagged(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(9.0))])
        result = await run(build(transport, paths, connection))
        assert result.warnings == []

    async def test_only_the_low_dimensions_are_named(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(9.0, 9.0, 9.0, 4.0, 9.0, 9.0))])
        result = await run(build(transport, paths, connection))
        assert result.warnings == ["review:score_issue_mismatch:emotion_rhythm"]

    async def test_a_flagged_run_still_returns_its_data(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """留痕不是失败：分数照用（为它加一轮重试烧的是真钱）。"""
        transport = ScriptedTransport(replies=[Reply(text=reviewer_json(4.0))])
        result = await run(build(transport, paths, connection))
        assert result.data is not None
        assert len(transport.calls) == 1


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_invalid_json_is_repaired_by_the_gateway(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        transport = ScriptedTransport(
            replies=[Reply(text="{不是 JSON"), Reply(text=reviewer_json(8.0, issues=[ISSUE]))]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2

    async def test_an_out_of_range_score_is_repaired(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        bad = json.dumps(
            {
                "llm_detail": {name: {"score": 42, "comment": "越界"} for name in LLM_DIMENSIONS},
                "issues": [],
            },
            ensure_ascii=False,
        )
        transport = ScriptedTransport(
            replies=[Reply(text=bad), Reply(text=reviewer_json(8.0, issues=[ISSUE]))]
        )
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

    async def test_a_missing_dimension_is_repaired(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        partial = json.dumps(
            {"llm_detail": {"hook_opening": {"score": 8, "comment": "缺了五维"}}, "issues": []},
            ensure_ascii=False,
        )
        transport = ScriptedTransport(
            replies=[Reply(text=partial), Reply(text=reviewer_json(8.0, issues=[ISSUE]))]
        )
        result = await run(build(transport, paths, connection))
        assert result.ok and result.data is not None
        assert len(transport.calls) == 2


# ══════════════════════════════════════════════════════════════════════
# 模块级小工具
# ══════════════════════════════════════════════════════════════════════


class TestBannedSummary:
    def test_clean_detail_says_so(self) -> None:
        assert banned_summary(rule_detail()) == "未命中"

    def test_hits_are_listed_with_their_counts(self) -> None:
        assert banned_summary(rule_detail(banned=True)) == "脏话×3"

    def test_multiple_terms_are_joined(self) -> None:
        detail = rule_detail().model_copy(
            update={"banned_hits": [BannedHit(term="脏话", count=1), BannedHit(term="政治", count=2)]}
        )
        assert banned_summary(detail) == "脏话×1、政治×2"
