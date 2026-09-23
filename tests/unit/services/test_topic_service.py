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
from studio.core.ids import new_ulid
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.models import FeedbackItemRow
from studio.db.repositories import AuditRepo, DirectionRepo, FeedbackItemRepo, TopicRepo
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
from studio.services.topic_service import _mark_auto_refs, classify_by_keywords, db_sentiment
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


def _service(
    ctx: Ctx, *, planner: Any, ideator: Any, classifier: Any = None, log: Any = None
) -> TopicService:
    return TopicService(
        ctx.connection,
        planner=planner,
        ideator=ideator,
        classifier=classifier,
        paths=ctx.paths,
        log=log,
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


async def test_visual_words_in_a_topic_are_warned_not_dropped(ctx: Ctx) -> None:
    """模型把**画面**写进选题（"熊大用跑酷台阶算给你看"）⇒ 发 warn，**不**丢、**不**改。

    真机踩到：跑酷只是底片，与选题无关，而候选里一直冒跑酷（§04.1.3 落地口径 6）。
    这里钉两件事：① 这条 warn 真的发得出来；② 发 warn 之后那条选题**照旧入库** ——
    拦掉会连带丢掉一条题材可能没问题的选题，而人只要看一眼日志就知道该改哪个词。
    """
    logs: list[dict[str, Any]] = []
    service = _service(
        ctx,
        planner=FakePlanner(titles=["方向A"]),
        ideator=FakeIdeator(titles=("熊大用跑酷台阶算给你看", "老楼装电梯这钱谁掏", "500万的房子该买还是租")),
        log=lambda **entry: logs.append(entry),
    )
    planned = await service.run_planner(persona=persona())
    assert planned.batch_id is not None

    report = await service.run_ideator(persona=persona(), batch_id=planned.batch_id)

    assert report.inserted == 3, "发 warn 不等于丢掉这条选题"
    warnings = [entry for entry in logs if entry["level"] == "warn"]
    assert len(warnings) == 1
    assert "跑酷" in warnings[0]["message"]
    assert warnings[0]["payload"]["words"] == ["跑酷"]


async def test_a_clean_topic_says_nothing(ctx: Ctx) -> None:
    """干净的一批**一条 warn 都不发** —— 否则这条判据会被噪声淹掉，然后被无视。"""
    logs: list[dict[str, Any]] = []
    service = _service(
        ctx,
        planner=FakePlanner(titles=["方向A"]),
        ideator=FakeIdeator(titles=("老楼装电梯这钱谁掏", "500万的房子该买还是租", "29.9元月饼为啥卖爆")),
        log=lambda **entry: logs.append(entry),
    )
    planned = await service.run_planner(persona=persona())
    assert planned.batch_id is not None

    await service.run_ideator(persona=persona(), batch_id=planned.batch_id)

    assert [entry for entry in logs if entry["level"] == "warn"] == []


# ══════════════════════════════════════════════════════════════════════
# 发布回流闭环（T5.4 · §06.8）
# ══════════════════════════════════════════════════════════════════════

AUTO_TEXT = "这个跑酷太帅了，求教程"


class _RefPlanner(FakePlanner):
    """方向里带一条 ``type='feedback'`` 的依据（引文可指定）。"""

    def __init__(self, quote: str) -> None:
        super().__init__()
        self._quote = quote

    async def run(self, ctx: AgentContext, payload: PlannerInput) -> AgentResult[PlannerOutput]:
        self.inputs.append(payload)
        directions = [
            DirectionSpec(
                title=title,
                rationale="依据",
                grounded_on=[
                    GroundingRef(type="persona"),
                    GroundingRef(type="feedback", kind="want", quote=self._quote),
                ],
                priority=100,
                fit_score=8,
            )
            for title in self.titles
        ]
        return AgentResult[PlannerOutput](ok=True, data=PlannerOutput(directions=directions))


def _auto_row(ctx: Ctx) -> None:
    """一条**发布回流**写入的反馈（``is_auto=1`` + 来源发布记录）。"""
    FeedbackItemRepo(ctx.connection).insert_many(
        [
            FeedbackItemRow(
                id=new_ulid(),
                source_file="auto_202609.md",
                platform="douyin",
                occurred_on="2026-09-18",
                content=AUTO_TEXT,
                is_auto=True,
                source_publication_id="01PUB00000000000000000000",
            )
        ]
    )


def _service_with(ctx: Ctx, *, planner: Any, include_auto: bool) -> TopicService:
    return TopicService(
        ctx.connection,
        planner=planner,
        ideator=FakeIdeator(),
        paths=ctx.paths,
        input_service=InputService(ctx.connection, paths=ctx.paths),
        include_auto_feedback=include_auto,
    )


async def test_grounded_on_gets_source_auto(ctx: Ctx) -> None:
    """§6.8 闭环验收：引用了自动回流反馈的方向 ⇒ ``grounded_on`` 里出现 ``source='auto'``。"""
    _auto_row(ctx)
    planner = _RefPlanner(AUTO_TEXT)
    report = await _service_with(ctx, planner=planner, include_auto=True).run_planner(persona=persona())
    assert report.ok is True
    assert report.batch_id is not None

    rows = DirectionRepo(ctx.connection).list_batch(report.batch_id)
    refs = [ref for row in rows for ref in row.grounded_on if ref["type"] == "feedback"]
    assert refs and all(ref["source"] == "auto" for ref in refs)


async def test_manual_feedback_is_not_marked_auto(ctx: Ctx) -> None:
    """引文对不上任何自动回流数据 ⇒ 不标（``None`` 而不是 ``'manual'``：我们只知道"不是自动的"）。"""
    _auto_row(ctx)
    planner = _RefPlanner("这个标题太吵了")  # 来自 0913.md 的人工反馈
    report = await _service_with(ctx, planner=planner, include_auto=True).run_planner(persona=persona())
    assert report.batch_id is not None

    rows = DirectionRepo(ctx.connection).list_batch(report.batch_id)
    refs = [ref for row in rows for ref in row.grounded_on if ref["type"] == "feedback"]
    assert refs and all(ref["source"] is None for ref in refs)


async def test_include_auto_feedback_off_keeps_them_out_of_the_prompt(ctx: Ctx) -> None:
    """§06.8 安全阀 ①：关掉开关 ⇒ 自动回流**不进摘要**，但数据仍在库里。"""
    _auto_row(ctx)
    planner = _RefPlanner(AUTO_TEXT)
    report = await _service_with(ctx, planner=planner, include_auto=False).run_planner(persona=persona())

    digest = planner.inputs[0].feedback_digest
    assert digest is not None
    assert all(not item.is_auto for item in (*digest.wants, *digest.complaints, *digest.trends))
    assert any("include_auto_feedback" in warning for warning in report.warnings)
    # 0913.md 的 2 条人工 + 1 条自动：**一条都没删**，只是没进摘要。
    assert FeedbackItemRepo(ctx.connection).count() == 3


# ══════════════════════════════════════════════════════════════════════
# 清空 · 一键清除所有选题
# ══════════════════════════════════════════════════════════════════════


def _seed_pool(ctx: Ctx, *, directions: int = 2, per_direction: int = 3) -> list[str]:
    """造一批方向 + 候选 ⇒ 返回全部选题 id（**不带任务**）。"""
    ids = DirectionRepo(ctx.connection).insert_batch(
        batch_id="b-clear",
        directions=[
            {"title": f"方向{index}", "rationale": f"理由{index}", "grounded_on": [], "priority": 100}
            for index in range(1, directions + 1)
        ],
    )
    rows = [
        {
            "direction_id": direction_id,
            "seq": seq,
            "title": f"选题 {direction_id}-{seq}",
            "angle": "角度",
            "exec_feasible": True,
            "status": "candidate",
        }
        for direction_id in ids
        for seq in range(1, per_direction + 1)
    ]
    return TopicRepo(ctx.connection).insert_many(rows)


def _empty_service(ctx: Ctx) -> TopicService:
    return _service(ctx, planner=FakePlanner(), ideator=FakeIdeator())


def test_clear_all_preview_reports_and_writes_nothing(ctx: Ctx) -> None:
    """``dry_run`` ⇒ 只报数：两个表一个字节都不动，也不留痕。

    面板上那颗按钮是**点两下**的（第一下预览、第二下真删），所以预览必须是纯读 ——
    它要是顺手写点什么，"点一下看看"就变成了一次不可撤销的操作。
    """
    topic_ids = _seed_pool(ctx, directions=2, per_direction=3)
    TopicRepo(ctx.connection).set_status(topic_id=topic_ids[0], status="queued", task_id="t-1")

    outcome = _empty_service(ctx).clear_all(dry_run=True)

    assert outcome.dry_run is True
    assert outcome.directions == 2
    assert outcome.topics == 6
    assert outcome.detached_task_count == 1
    assert DirectionRepo(ctx.connection).count() == 2
    assert TopicRepo(ctx.connection).count() == 6
    assert AuditRepo(ctx.connection).list_recent(limit=10) == []


def test_clear_all_removes_everything_and_keeps_the_tasks(ctx: Ctx) -> None:
    """真删：两张表清空，**任务不跟着走**，留痕 + 日志各一条。

    用户原话：「堆积太多内容会难以管理」。删掉的是想法，不是活 —— 派生过任务的那些选题
    照删，而它们上的任务一条都不动（任务自己带着标题 / 角度 / 钩子）。
    """
    topic_ids = _seed_pool(ctx, directions=2, per_direction=3)
    TopicRepo(ctx.connection).set_status(topic_id=topic_ids[0], status="queued", task_id="t-1")
    TopicRepo(ctx.connection).set_status(topic_id=topic_ids[1], status="queued", task_id="t-2")
    logged: list[tuple[str, str]] = []

    def sink(**kwargs: Any) -> None:
        logged.append((str(kwargs["level"]), str(kwargs["message"])))

    outcome = _service(ctx, planner=FakePlanner(), ideator=FakeIdeator(), log=sink).clear_all(dry_run=False)

    assert outcome.dry_run is False
    assert outcome.directions == 2
    assert outcome.topics == 6
    assert outcome.detached_task_count == 2
    assert DirectionRepo(ctx.connection).count() == 0
    assert TopicRepo(ctx.connection).count() == 0

    ops = AuditRepo(ctx.connection).list_recent(limit=5)
    assert [op.action for op in ops] == ["topics.cleared"]
    assert ops[0].target_type == "topic_pool"
    assert ops[0].before == {"directions": 2, "topics": 6, "detached_task_count": 2}
    assert ops[0].after == {"directions": 0, "topics": 0}

    assert len(logged) == 1
    assert "已清空选题面板" in logged[0][1]
    assert "2 条已派生任务" in logged[0][1]


def test_clear_all_leaves_the_inputs_and_outputs_alone(ctx: Ctx) -> None:
    """清空**只动选题面板那两张表**：热点 / 反馈 / 任务都不是"面板上堆着的东西"。"""
    _seed_pool(ctx, directions=1, per_direction=1)
    before = {
        "hot": ctx.connection.execute("SELECT COUNT(*) FROM hot_items").fetchone()[0],
        "feedback": FeedbackItemRepo(ctx.connection).count(),
    }

    _empty_service(ctx).clear_all(dry_run=False)

    assert ctx.connection.execute("SELECT COUNT(*) FROM hot_items").fetchone()[0] == before["hot"]
    assert FeedbackItemRepo(ctx.connection).count() == before["feedback"]


def test_clear_all_on_an_empty_panel_writes_no_audit(ctx: Ctx) -> None:
    """空面板上点"清除"不留痕（与 ``asset.prune`` 同一条）：一条什么都没删的审计
    会把审计页淹掉，而"我点了一下、它说没什么可清的"没有留档价值。"""
    outcome = _empty_service(ctx).clear_all(dry_run=False)

    assert (outcome.directions, outcome.topics, outcome.detached_task_count) == (0, 0, 0)
    assert outcome.dry_run is False
    assert AuditRepo(ctx.connection).list_recent(limit=10) == []


def test_clear_all_preview_says_the_same_numbers_as_the_real_thing(ctx: Ctx) -> None:
    """预览与真删走的是**同一个计数路径** ⇒ 预览说 6 条，真删就不会是别的数。

    两处各数一遍的话，"预览说 13 条、真删删了 12 条"这类不一致没有任何地方会报错。
    """
    _seed_pool(ctx, directions=3, per_direction=2)
    service = _empty_service(ctx)

    preview = service.clear_all(dry_run=True)
    real = service.clear_all(dry_run=False)

    assert (preview.directions, preview.topics) == (real.directions, real.topics)
    assert (real.directions, real.topics) == (3, 6)


def test_mark_auto_refs_ignores_short_quotes() -> None:
    """太短的引文（"好"）会在任何一条反馈里命中 ⇒ 不标，免得把人工依据画成自动的。"""
    direction = DirectionSpec(
        title="方向",
        rationale="理由",
        grounded_on=[GroundingRef(type="feedback", kind="want", quote="好")],
        priority=100,
        fit_score=8,
    )
    assert _mark_auto_refs([direction], {"这个跑酷太帅了，求教程"}) == 0
    assert direction.grounded_on[0].source is None
