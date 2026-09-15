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
from studio.db.repositories import DirectionRepo, ScriptRepo, TopicRepo
from studio.domain.script import (
    DirectorInput,
    DirectorOutput,
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


def seed_topic(connection: sqlite3.Connection, title: str = "MC跑酷最难的一跳") -> str:
    directions = DirectionRepo(connection)
    batch = directions.new_batch_id()
    (direction_id,) = directions.insert_batch(
        batch_id=batch,
        directions=[
            {
                "title": "跑酷技术流",
                "rationale": "账号定位就是跑酷",
                "grounded_on": [{"type": "persona"}],
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


def build(
    connection: sqlite3.Connection,
    *,
    director: FakeDirector | None = None,
    writer: FakeWriter | None = None,
) -> ScriptService:
    return ScriptService(
        connection,
        director=director or FakeDirector(data=outline()),
        writer=writer or FakeWriter(data=writer_output()),
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
