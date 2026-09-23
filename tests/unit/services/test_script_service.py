"""``ScriptService`` 服务层（T1.10）—— 编排纪律与失败路径。

集成测试跑的是**真实链路**（真网关 + 真提示词 + 真 schema）；这里用假 Agent 把
"出岔子时怎么办"单独钉死：

- 选题不存在 ⇒ ``TOPIC_NOT_FOUND``（不猜、不新建任务）；
- Director / Writer 失败 ⇒ 任务置 ``failed``，**库里不留半成品**；
- 同一个选题跑两次 ⇒ **只建一个任务**（幂等键 ``topic:<id>``）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentContext, AgentResult
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import DirectionRepo, OutlineRepo, ScriptRepo, TopicRepo
from studio.domain.enums import TaskStatus
from studio.domain.script import (
    DirectorInput,
    DirectorOutput,
    OutlineInput,
    OutlineOutput,
    ScriptSegment,
    SentenceSpec,
    WriterInput,
    WriterOutput,
)
from studio.domain.task_service import TaskService
from studio.services import ScriptService, read_active_script
from tests.unit.agents.fakes import persona


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
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


def seed_topic(
    connection: sqlite3.Connection,
    title: str = "MC跑酷最难的一跳",
    *,
    evidence: str = "",
) -> str:
    """造一条方向 + 一条选题；``evidence`` 非空 ⇒ 这条方向带着**今日新闻的事件总结**。"""
    directions = DirectionRepo(connection)
    batch = directions.new_batch_id()
    (direction_id,) = directions.insert_batch(
        batch_id=batch,
        directions=[
            {
                "title": "跑酷技术流",
                "rationale": "账号定位就是跑酷",
                "grounded_on": [
                    {"type": "persona"},
                    *([{"type": "hot", "kind": "news", "quote": evidence}] if evidence else []),
                ],
                "priority": 100,
                "risk_flags": [],
            }
        ],
    )
    topics = TopicRepo(connection)
    (topic_id,) = topics.insert_many(
        [
            {
                "direction_id": direction_id,
                "seq": 1,
                "title": title,
                "hook_type": "suspense",
                "angle": "只讲那一跳",
                "exec_feasible": True,
                "score": 8.5,
                "reason": "钩子够硬",
            }
        ]
    )
    return topic_id


def outline() -> DirectorOutput:
    return DirectorOutput(
        hook_3s="熊大又整活了",
        segments=[
            ScriptSegment(seq=index, point=f"要点{index}", visual=f"画面{index}", mood="兴奋", est_chars=200)
            for index in range(1, 4)
        ],
        cta="点个关注",
        est_duration_ms=140_000,
    )


def writer_output() -> WriterOutput:
    body = "这不科学，俺寻思也是。" + "熊" * 691
    return WriterOutput(
        title="标题",
        hook="开场",
        body_md=body,
        cta="关注",
        sentences=[
            SentenceSpec(
                seq=index,
                text="这不科学，熊大又跑起来了。",
                speaker="bigbear" if index % 2 else "littlebear",
            )
            for index in range(1, 21)
        ],
        est_duration_ms=140_000,
        catchphrases_used=["这不科学", "俺寻思"],
    )


def outline_output() -> OutlineOutput:
    return OutlineOutput(title="原标题够好就别改", core_argument="表面是价格，内核是信任")


class FakeDirector:
    """记录收到的 ``DirectorInput``，返回脚本化的结果。"""

    def __init__(self, *, data: DirectorOutput | None = None, ok: bool = True, **extra: Any) -> None:
        self.data = data
        self.ok = ok
        self.extra = extra
        self.inputs: list[DirectorInput] = []

    async def run(self, ctx: AgentContext, payload: DirectorInput) -> AgentResult[DirectorOutput]:
        self.inputs.append(payload)
        return AgentResult[DirectorOutput](ok=self.ok, data=self.data, **self.extra)


class FakeWriter:
    def __init__(self, *, data: WriterOutput | None = None, ok: bool = True, **extra: Any) -> None:
        self.data = data
        self.ok = ok
        self.extra = extra
        self.inputs: list[WriterInput] = []

    async def run(self, ctx: AgentContext, payload: WriterInput) -> AgentResult[WriterOutput]:
        self.inputs.append(payload)
        return AgentResult[WriterOutput](ok=self.ok, data=self.data, **self.extra)


class FakeOutliner:
    """记录收到的 ``OutlineInput``，返回脚本化的二级产物。"""

    def __init__(self, *, data: OutlineOutput | None = None, ok: bool = True, **extra: Any) -> None:
        self.data = data
        self.ok = ok
        self.extra = extra
        self.inputs: list[OutlineInput] = []

    async def run(self, ctx: AgentContext, payload: OutlineInput) -> AgentResult[OutlineOutput]:
        self.inputs.append(payload)
        return AgentResult[OutlineOutput](ok=self.ok, data=self.data, **self.extra)


def build(
    connection: sqlite3.Connection,
    *,
    director: FakeDirector | None = None,
    writer: FakeWriter | None = None,
    outliner: FakeOutliner | None = None,
) -> ScriptService:
    return ScriptService(
        connection,
        director=director or FakeDirector(data=outline()),
        writer=writer or FakeWriter(data=writer_output()),
        outliner=outliner,
    )


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestDraft:
    async def test_persists_script_and_sentences(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        report = await build(connection).draft(topic_id=topic_id, persona=persona())
        assert report.ok and report.created_task
        repo = ScriptRepo(connection)
        script = repo.get_active(report.task_id)
        assert script is not None and script.id == report.script_id
        assert repo.count_sentences(script.id) == report.sentence_count

    async def test_task_goes_to_drafting(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        report = await build(connection).draft(topic_id=topic_id, persona=persona())
        assert TaskService(connection).get(report.task_id).status.value == "drafting"

    async def test_topic_is_marked_queued_and_bound_to_the_task(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        report = await build(connection).draft(topic_id=topic_id, persona=persona())
        row = TopicRepo(connection).get(topic_id)
        assert row is not None and row.status == "queued" and row.task_id == report.task_id

    async def test_director_receives_the_topic(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection, title="独特的选题标题")
        director = FakeDirector(data=outline())
        await build(connection, director=director).draft(topic_id=topic_id, persona=persona())
        assert director.inputs[0].topic.title == "独特的选题标题"

    async def test_writer_receives_the_outline(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        writer = FakeWriter(data=writer_output())
        await build(connection, writer=writer).draft(topic_id=topic_id, persona=persona())
        assert writer.inputs[0].outline.hook_3s == "熊大又整活了"

    async def test_second_run_reuses_the_task(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        service = build(connection)
        first = await service.draft(topic_id=topic_id, persona=persona())
        second = await service.draft(topic_id=topic_id, persona=persona())
        assert first.task_id == second.task_id
        assert not second.created_task
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    async def test_second_run_creates_a_new_script_version(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        service = build(connection)
        first = await service.draft(topic_id=topic_id, persona=persona())
        second = await service.draft(topic_id=topic_id, persona=persona())
        assert (first.version, second.version) == (1, 2)
        assert second.script_id != first.script_id

    async def test_a_deleted_topic_does_not_strand_the_task_it_derived(
        self, connection: sqlite3.Connection
    ) -> None:
        """选题行被删掉之后，**已经排队的活照跑** —— 它自己带着要说什么。

        「删选题 / 删方向」是清理选题池的动作，不该让一条已经派生的任务写不出稿
        （用户原话：「堆积太多内容会难以管理」）。任务自带 ``tasks.title`` 与
        ``payload_json`` 里的 angle / hook_type ⇒ 退路是把这三样捡起来接着写。
        """
        topic_id = seed_topic(connection)
        first = await build(connection).draft(topic_id=topic_id, persona=persona())
        assert first.ok
        assert TopicRepo(connection).delete(topic_id) is True

        director = FakeDirector(data=outline())
        report = await build(connection, director=director).draft(
            topic_id=topic_id, persona=persona(), task_id=first.task_id
        )

        assert report.ok
        assert report.task_id == first.task_id
        assert report.topic_id == topic_id
        assert not report.created_task  # 任务早就在那儿了，不该新建第二个
        spec = director.inputs[0].topic
        assert spec.title == "MC跑酷最难的一跳"
        assert spec.angle == "只讲那一跳"
        assert spec.hook_type == "suspense"
        # 理由那一栏只能给一句实话（`TaskPayload` 里没有 reason，那一份是冻结的契约）
        assert "已从选题池删除" in spec.reason


# ══════════════════════════════════════════════════════════════════════
# 已知事实（今日新闻那条链路 · T5.12 增补）
# ══════════════════════════════════════════════════════════════════════


class TestFacts:
    """事件总结要**真的**走到写稿那两级 —— 否则它只是躺在库里的一句话。

    为什么在服务层按 ``topic.direction_id`` 回读，而不是让上游哪一级的模型抄下来：
    抄写会漂（模型改写一句、漏一句都看不出来），而事实漂了就是幻觉 —— 用户要的正是
    "别编"。这一层是确定性的：方向里有什么，提示词里就有什么。
    """

    async def test_news_evidence_reaches_director_and_writer(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection, evidence="一家三口一氧化碳中毒身亡。")
        director = FakeDirector(data=outline())
        writer = FakeWriter(data=writer_output())
        await build(connection, director=director, writer=writer).draft(topic_id=topic_id, persona=persona())
        assert director.inputs[0].facts == "一家三口一氧化碳中毒身亡。"
        assert writer.inputs[0].facts == "一家三口一氧化碳中毒身亡。"

    async def test_a_topic_without_news_evidence_gets_no_facts(self, connection: sqlite3.Connection) -> None:
        """不是新闻来的选题 ⇒ 空串（渲染成「（无 …）」，而不是让模型以为有事实可依）。"""
        topic_id = seed_topic(connection)
        director = FakeDirector(data=outline())
        await build(connection, director=director).draft(topic_id=topic_id, persona=persona())
        assert director.inputs[0].facts == ""


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_unknown_topic_raises(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(StudioError) as info:
            await build(connection).draft(topic_id="nope", persona=persona())
        assert info.value.code == ErrorCode.TOPIC_NOT_FOUND
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    async def test_director_failure_marks_the_task_failed(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        director = FakeDirector(data=None, ok=False, error_code="LLM_TIMEOUT", error_message="超时")
        report = await build(connection, director=director).draft(topic_id=topic_id, persona=persona())
        assert not report.ok
        assert report.error_code == "LLM_TIMEOUT"
        assert TaskService(connection).get(report.task_id).status.value == "failed"

    async def test_director_failure_leaves_no_script(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        director = FakeDirector(data=None, ok=False, error_code="LLM_TIMEOUT")
        report = await build(connection, director=director).draft(topic_id=topic_id, persona=persona())
        assert read_active_script(connection, report.task_id) is None
        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0

    async def test_forbidden_script_blocks_and_persists_nothing(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        writer = FakeWriter(
            data=None,
            ok=False,
            error_code=str(ErrorCode.SCRIPT_FORBIDDEN),
            error_message="禁区命中：脏话",
            warnings=["forbidden:脏话"],
        )
        report = await build(connection, writer=writer).draft(topic_id=topic_id, persona=persona())
        assert not report.ok
        assert report.error_code == str(ErrorCode.SCRIPT_FORBIDDEN)
        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0
        assert TaskService(connection).get(report.task_id).status.value == "failed"

    async def test_a_late_failure_does_not_pull_the_task_back(self, connection: sqlite3.Connection) -> None:
        """迟到的写稿失败**没有话语权**（T1.9 裁定 316 的真机现场）。

        CLI 的 `script draft` 就地跑完、把任务一路推进配音，而池里那条 `draft/task`
        单元还在跑**同一个**任务；它稍后失败时若照常置 ``failed``，就会把一次好端端的
        生产打断（真机现场：`voicing \u2192 failed`，随后流水线撞上非法迁移
        `failed \u2192 queued_render`）。稿子还在，任务没错，错的是这条迟到的失败。
        """
        topic_id = seed_topic(connection)
        first = await build(connection).draft(topic_id=topic_id, persona=persona())
        assert first.ok
        tasks = TaskService(connection)
        for target in (TaskStatus.REVIEWING, TaskStatus.QUEUED_VOICE, TaskStatus.VOICING):
            tasks.transition(first.task_id, target, actor="pipeline")

        director = FakeDirector(data=None, ok=False, error_code="LLM_TIMEOUT", error_message="超时")
        report = await build(connection, director=director).draft(topic_id=topic_id, persona=persona())

        assert not report.ok
        row = tasks.get(first.task_id)
        assert row.status is TaskStatus.VOICING, "任务已越过写稿段 \u21d2 不许拽回 failed"
        assert row.error_code is None, "也不许把错误盖上去"
        assert read_active_script(connection, first.task_id) is not None

    async def test_out_of_range_script_is_still_persisted_with_warnings(
        self, connection: sqlite3.Connection
    ) -> None:
        """规格书要求"取最接近版本 + warn"，**不是**整批失败（DoD 6）。"""
        topic_id = seed_topic(connection)
        short = writer_output().model_copy(update={"body_md": "这不科学，俺寻思也是。" + "熊" * 291})
        report = await build(connection, writer=FakeWriter(data=short)).draft(
            topic_id=topic_id, persona=persona()
        )
        assert report.ok
        assert any(item.startswith("script:字数") for item in report.warnings)
        assert read_active_script(connection, report.task_id) is not None


# ══════════════════════════════════════════════════════════════════════
# 读路径
# ══════════════════════════════════════════════════════════════════════


class TestReadActiveScript:
    async def test_returns_none_without_a_script(self, connection: sqlite3.Connection) -> None:
        task_id = TaskService(connection).create(title="空任务").id
        assert read_active_script(connection, task_id) is None

    async def test_returns_the_active_version(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        report = await build(connection).draft(topic_id=topic_id, persona=persona())
        payload = read_active_script(connection, report.task_id)
        assert payload is not None
        script, sentences = payload
        assert script.id == report.script_id
        assert [row.seq for row in sentences] == list(range(1, len(sentences) + 1))

    async def test_sentences_are_within_the_tts_limit(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        long_one = writer_output().model_copy(
            update={
                "sentences": [
                    SentenceSpec(seq=1, text="熊大说这不科学，" * 5, speaker="bigbear"),
                    *[SentenceSpec(seq=index, text="短句。", speaker="bigbear") for index in range(2, 21)],
                ]
            }
        )
        report = await build(connection, writer=FakeWriter(data=long_one)).draft(
            topic_id=topic_id, persona=persona()
        )
        _, sentences = read_active_script(connection, report.task_id)  # type: ignore[misc]
        assert all(len(row.text) <= 28 for row in sentences)
        assert [row.seq for row in sentences] == list(range(1, len(sentences) + 1))


# ══════════════════════════════════════════════════════════════════════
# 二级产物：缺位时**写稿前自动补一次**
# ══════════════════════════════════════════════════════════════════════


class TestAutoOutline:
    """二级（视频标题 + 核心论点）以前只有面板上那颗手动按钮能触发 —— 实际链路里几乎
    从不发生，于是三级拿到 ``OUTLINE_UNSET``，「围绕核心论点深挖」这句指令**没有论点
    可围绕**，只能写成表面叙事；改了 outliner 提示词也看不到效果（那一级压根没跑）。
    这里把「自动补」与两条兜底钉死。
    """

    async def test_draft_fills_the_second_level_when_it_is_missing(
        self, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        outliner = FakeOutliner(data=outline_output())
        writer = FakeWriter(data=writer_output())
        report = await build(connection, outliner=outliner, writer=writer).draft(
            topic_id=topic_id, persona=persona()
        )
        assert report.ok
        assert len(outliner.inputs) == 1
        # ★ 论点真的喂给了三级 —— 否则「深挖内核」没有任何东西可挖
        assert writer.inputs[0].core_argument == "表面是价格，内核是信任"
        assert writer.inputs[0].outline_title == "原标题够好就别改"

    async def test_the_generated_outline_is_persisted(self, connection: sqlite3.Connection) -> None:
        topic_id = seed_topic(connection)
        await build(connection, outliner=FakeOutliner(data=outline_output())).draft(
            topic_id=topic_id, persona=persona()
        )
        row = OutlineRepo(connection).get(topic_id)
        assert row is not None and row.title == "原标题够好就别改"

    async def test_an_existing_outline_is_not_regenerated(self, connection: sqlite3.Connection) -> None:
        """已经有了就**不再烧一次调用**（自动补是"缺位时"的兜底，不是每篇都跑）。"""
        topic_id = seed_topic(connection)
        service = build(connection, outliner=FakeOutliner(data=outline_output()))
        await service.draft(topic_id=topic_id, persona=persona())
        second = FakeWriter(data=writer_output())
        outliner = FakeOutliner(data=outline_output())
        await build(connection, outliner=outliner, writer=second).draft(topic_id=topic_id, persona=persona())
        assert outliner.inputs == []
        assert second.inputs[0].outline_title == "原标题够好就别改"

    async def test_without_an_outliner_the_title_falls_back_to_the_topic(
        self, connection: sqlite3.Connection
    ) -> None:
        """没接 Outliner ⇒ 标题用**一级选题标题**，而不是 ``OUTLINE_UNSET``。

        用户口径：原标题已经够好了。落到 ``OUTLINE_UNSET`` 上等于让模型「按选题自行
        发挥」一个标题 —— 而标题是最不该自由发挥的东西（它是观众看到的第一行字）。
        """
        topic_id = seed_topic(connection, title="汉堡包做成月饼")
        writer = FakeWriter(data=writer_output())
        report = await build(connection, writer=writer).draft(topic_id=topic_id, persona=persona())
        assert report.ok
        assert writer.inputs[0].outline_title == "汉堡包做成月饼"
        assert writer.inputs[0].core_argument is None

    async def test_a_failed_outliner_does_not_block_the_draft(self, connection: sqlite3.Connection) -> None:
        """best-effort：二级失败也**必须**写得出稿（退回一级标题）。"""
        topic_id = seed_topic(connection, title="汉堡包做成月饼")
        writer = FakeWriter(data=writer_output())
        report = await build(connection, outliner=FakeOutliner(data=None, ok=False), writer=writer).draft(
            topic_id=topic_id, persona=persona()
        )
        assert report.ok
        assert writer.inputs[0].outline_title == "汉堡包做成月饼"
