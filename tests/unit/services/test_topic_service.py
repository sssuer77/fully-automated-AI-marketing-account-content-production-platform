"""``TopicService`` 服务层（T1.9）—— 降级路径与编排纪律。

集成测试（``tests/integration/test_topic_pool.py``）跑的是**真实链路**；
这里用假 Agent 把"出岔子时怎么办"单独钉死：

- 分类 Agent 缺失 / 失败 ⇒ 关键词兜底，**Planner 照样拿得到反馈摘要**；
- 没有批次就选题 ⇒ 报 ``TOPIC_BATCH_NOT_FOUND``（而不是产出一批空数据）。

假 Agent 只需要实现 ``run`` —— 服务层依赖的是 Protocol，不是具体类。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentContext, AgentResult
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import DirectionRepo, FeedbackItemRepo
from studio.domain.topics import (
    DirectionSpec,
    FeedbackBatch,
    FeedbackClassification,
    GroundingRef,
    IdeatorInput,
    IdeatorOutput,
    PlannerInput,
    PlannerOutput,
    TopicSpec,
)
from studio.services import InputService, TopicService
from studio.services.topic_service import classify_by_keywords, db_sentiment
from tests.unit.agents.fakes import persona

FEEDBACK_TEXT = """想看MC跑酷新手教程，求更新
这个标题太吵了
"""

# ══════════════════════════════════════════════════════════════════════
# 纯函数：关键词兜底分类
# ══════════════════════════════════════════════════════════════════════


class TestClassifyByKeywords:
    def test_picks_wants(self) -> None:
        sentiment, wants, complaints = classify_by_keywords("想看MC跑酷合集")
        assert sentiment == "pos"
        assert wants == ["想看MC跑酷合集"]
        assert complaints == []

    def test_picks_complaints(self) -> None:
        sentiment, wants, complaints = classify_by_keywords("这个标题太吵了")
        assert sentiment == "neg"
        assert wants == []
        assert complaints == ["这个标题太吵了"]

    def test_neutral_when_nothing_matches(self) -> None:
        assert classify_by_keywords("今天天气不错") == ("neu", [], [])

    def test_clauses_are_deduplicated(self) -> None:
        _, wants, _ = classify_by_keywords("想看跑酷。想看跑酷。")
        assert wants == ["想看跑酷"]


class TestDbSentiment:
    def test_maps_ddl_vocabulary_to_domain_shortform(self) -> None:
        assert db_sentiment("positive") == "pos"
        assert db_sentiment("negative") == "neg"
        assert db_sentiment("neutral") == "neu"
        assert db_sentiment("unknown") == "unknown"

    def test_unknown_value_degrades_to_unknown(self) -> None:
        assert db_sentiment("weird") == "unknown"
        assert db_sentiment(None) == "unknown"


# ══════════════════════════════════════════════════════════════════════
# 假 Agent
# ══════════════════════════════════════════════════════════════════════


def _direction(title: str) -> DirectionSpec:
    return DirectionSpec(
        title=title,
        rationale=f"{title} 的理由",
        grounded_on=[GroundingRef(type="persona"), GroundingRef(type="hot", ref_id="h1")],
        priority=100,
        risk_flags=[],
        fit_score=8,
    )


def _topic(title: str) -> TopicSpec:
    return TopicSpec(title=title, hook_type="suspense", angle="角度", score=8.0, reason="理由")


class FakePlanner:
    """记录收到的 ``PlannerInput``（断言"摘要真的传下去了"）。"""

    def __init__(self, titles: list[str] | None = None) -> None:
        self.titles = titles or ["方向1", "方向2", "方向3", "方向4", "方向5"]
        self.inputs: list[PlannerInput] = []

    async def run(self, ctx: AgentContext, payload: PlannerInput) -> AgentResult[PlannerOutput]:
        self.inputs.append(payload)
        return AgentResult[PlannerOutput](
            ok=True, data=PlannerOutput(directions=[_direction(title) for title in self.titles])
        )


class FakeIdeator:
    def __init__(self, titles: tuple[str, ...] = ("选题甲", "选题乙", "选题丙")) -> None:
        self.titles = titles
        self.inputs: list[IdeatorInput] = []

    async def run(self, ctx: AgentContext, payload: IdeatorInput) -> AgentResult[IdeatorOutput]:
        self.inputs.append(payload)
        return AgentResult[IdeatorOutput](
            ok=True, data=IdeatorOutput(topics=[_topic(t) for t in self.titles])
        )


class FailingClassifier:
    """分类失败（超时）—— 服务层必须退到关键词，而不是把摘要清空。"""

    async def run(self, ctx: AgentContext, payload: FeedbackBatch) -> AgentResult[FeedbackClassification]:
        return AgentResult[FeedbackClassification](
            ok=False, error_code=ErrorCode.LLM_TIMEOUT, error_message="超时"
        )


@dataclass
class Ctx:
    paths: StudioPaths
    connection: sqlite3.Connection


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[Ctx]:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.feedback_dir.mkdir(parents=True, exist_ok=True)
    (paths.feedback_dir / "0913.md").write_text(FEEDBACK_TEXT, encoding="utf-8")
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    try:
        yield Ctx(paths=paths, connection=connection)
    finally:
        connection.close()


def _service(ctx: Ctx, *, planner: Any, ideator: Any, classifier: Any = None) -> TopicService:
    return TopicService(
        ctx.connection,
        planner=planner,
        ideator=ideator,
        classifier=classifier,
        paths=ctx.paths,
        input_service=InputService(ctx.connection, paths=ctx.paths),
    )


# ══════════════════════════════════════════════════════════════════════
# 分类降级
# ══════════════════════════════════════════════════════════════════════


async def test_missing_classifier_falls_back_to_keywords(ctx: Ctx) -> None:
    planner = FakePlanner()
    service = _service(ctx, planner=planner, ideator=FakeIdeator())

    report = await service.run_planner(persona=persona())

    assert report.ok is True
    assert "feedback_classified_by_keywords" in report.warnings
    # 兜底结果**回填**：下次运行不必再兜一次
    rows = FeedbackItemRepo(ctx.connection).list_recent()
    assert {row.sentiment for row in rows} == {"positive", "negative"}
    # 关键：摘要没有空掉，Planner 依旧拿得到"用户想要"
    digest = planner.inputs[0].feedback_digest
    assert digest is not None
    assert [item.text for item in digest.wants] == ["想看MC跑酷新手教程，求更新"]


async def test_failed_classifier_falls_back_and_warns(ctx: Ctx) -> None:
    planner = FakePlanner()
    service = _service(ctx, planner=planner, ideator=FakeIdeator(), classifier=FailingClassifier())

    report = await service.run_planner(persona=persona())

    assert report.ok is True
    assert any(warning.startswith("feedback_classify_failed:") for warning in report.warnings)
    rows = FeedbackItemRepo(ctx.connection).list_recent()
    assert all(row.sentiment is not None for row in rows)


async def test_directions_are_persisted_with_grounding(ctx: Ctx) -> None:
    planner = FakePlanner()
    service = _service(ctx, planner=planner, ideator=FakeIdeator())

    report = await service.run_planner(persona=persona())

    assert report.direction_count == 5
    assert report.batch_id is not None
    first = report.directions[0]
    assert first.grounded_on[0]["type"] == "persona"
    assert first.status == "open"


# ══════════════════════════════════════════════════════════════════════
# 选题前置条件
# ══════════════════════════════════════════════════════════════════════


async def test_ideator_without_a_batch_raises(ctx: Ctx) -> None:
    service = _service(ctx, planner=FakePlanner(), ideator=FakeIdeator())
    with pytest.raises(StudioError) as excinfo:
        await service.run_ideator(persona=persona())
    assert excinfo.value.code == ErrorCode.TOPIC_BATCH_NOT_FOUND


async def test_ideator_marks_directions_selected(ctx: Ctx) -> None:
    planner = FakePlanner(titles=["方向A", "方向B"])
    service = _service(ctx, planner=planner, ideator=FakeIdeator())
    planned = await service.run_planner(persona=persona())
    assert planned.batch_id is not None

    report = await service.run_ideator(persona=persona(), batch_id=planned.batch_id)

    # 两个方向吐同一批标题 ⇒ 第二个方向全部命中一级去重。
    # 这正是"共享池子"的意义：撞车在**写入前**就被拦下，不靠事后清理。
    assert report.inserted == 3
    assert report.dropped == 3
    assert report.ok is True
    assert [outcome.title for outcome in report.outcomes] == ["方向A", "方向B"]
    # 第二个方向一条都没进池 ⇒ 状态回写 dropped（可人工重跑，不是静默丢弃）
    statuses = {row.title: row.status for row in DirectionRepo(ctx.connection).list_batch(planned.batch_id)}
    assert statuses == {"方向A": "selected", "方向B": "dropped"}


async def test_ideator_unknown_direction_id_is_an_empty_selection(ctx: Ctx) -> None:
    planner = FakePlanner(titles=["方向A"])
    service = _service(ctx, planner=planner, ideator=FakeIdeator())
    planned = await service.run_planner(persona=persona())

    with pytest.raises(StudioError) as excinfo:
        await service.run_ideator(
            persona=persona(), batch_id=planned.batch_id, direction_ids=["not-a-direction"]
        )
    assert excinfo.value.code == ErrorCode.TOPIC_DIRECTION_EMPTY
