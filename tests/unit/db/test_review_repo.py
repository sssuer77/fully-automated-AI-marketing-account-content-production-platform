"""``review_scores`` 仓储（T1.11 · §03.3.8）—— 一轮一行，不可覆盖。

这里测的是**唯一键语义**与 **JSON 往返**：分数必须能被复盘，而复盘的前提是
"同一版稿子的同一轮只有一份分数"。用真实库（临时目录 + 迁移）而不是 mock 连接 ——
这些断言的价值全在 SQL、CHECK 与外键上。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.models import ReviewRow
from studio.db.repositories import ReviewRepo, ScriptRepo
from studio.domain import TaskService
from studio.domain.scoring import ChannelItem, LlmChannelDetail, RuleChannelDetail


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """迁移好的临时库（**用完关掉**，否则 Windows 上临时目录删不干净）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="跑酷合集").id


def make_script(connection: sqlite3.Connection, task_id: str, *, hook: str = "开场") -> str:
    """落一版真稿件（``review_scores.script_id`` 有外键，凑不出假 id）。"""
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook=hook,
        body_md="熊" * 700,
        cta="关注",
        word_count=700,
        est_duration_ms=140_000,
        speaker_ratio={"bigbear": 1.0},
        outline={"hook_3s": "开场", "segments": [], "cta": "关注", "est_duration_ms": 140_000},
        sentences=[
            {
                "seq": index,
                "text_raw": f"第{index}句",
                "text": f"第{index}句",
                "speaker": "bigbear",
                "emotion": "neutral",
                "pause_after_ms": 200,
            }
            for index in range(1, 4)
        ],
    )
    return saved.script_id


@pytest.fixture
def script_id(connection: sqlite3.Connection, task_id: str) -> str:
    return make_script(connection, task_id)


def rule_detail() -> dict[str, Any]:
    return RuleChannelDetail(
        length=ChannelItem(score=9.0, comment="700 字（要求 600–800）"),
        banned_hits=[],
        opening_ok=True,
        ending_ok=True,
        paragraph_dup_ratio=0.0,
        chars=700,
        est_duration_ms=140_000,
        catchphrases_hit=2,
    ).model_dump(mode="json")


def llm_detail() -> dict[str, Any]:
    item = ChannelItem(score=8.0, comment="还行")
    return LlmChannelDetail(
        hook_opening=item,
        positioning_fit=item,
        oral_style=item,
        emotion_rhythm=item,
        ending_cta=item,
        forbidden=item,
    ).model_dump(mode="json")


def insert(
    repo: ReviewRepo,
    *,
    task_id: str,
    script_id: str,
    round_no: int = 1,
    grade: str = "A",
    decision: str = "pass_auto",
    issues: list[dict[str, Any]] | None = None,
) -> ReviewRow:
    return repo.insert(
        task_id=task_id,
        script_id=script_id,
        round_no=round_no,
        rule_total=9.0,
        rule_detail=rule_detail(),
        llm_total=8.0,
        llm_detail=llm_detail(),
        total=8.3,
        grade=grade,
        decision=decision,
        issues=issues if issues is not None else [],
        llm_model="cloud-model",
        prompt_version="1+deadbeef",
    )


class TestInsert:
    def test_returns_the_row_it_wrote(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        row = insert(ReviewRepo(connection), task_id=task_id, script_id=script_id)
        assert row.task_id == task_id and row.script_id == script_id
        assert (row.total, row.grade, row.decision) == (8.3, "A", "pass_auto")
        assert (row.llm_model, row.prompt_version) == ("cloud-model", "1+deadbeef")
        assert row.created_at is not None

    def test_detail_json_round_trips(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        row = insert(ReviewRepo(connection), task_id=task_id, script_id=script_id)
        assert row.rule_detail["length"]["score"] == 9.0
        assert row.rule_detail["chars"] == 700
        assert row.llm_detail["hook_opening"] == {"score": 8.0, "comment": "还行"}

    def test_issues_round_trip(self, connection: sqlite3.Connection, task_id: str, script_id: str) -> None:
        issues = [
            {
                "code": "weak_hook",
                "severity": "major",
                "target": "hook",
                "detail": "开场太平",
                "suggestion": "把结果前置",
            }
        ]
        row = insert(ReviewRepo(connection), task_id=task_id, script_id=script_id, issues=issues)
        assert row.issues == issues

    def test_an_unknown_task_is_rejected_by_the_foreign_key(self, connection: sqlite3.Connection) -> None:
        script_id = make_script(connection, TaskService(connection).create(title="占位").id)
        with pytest.raises(sqlite3.IntegrityError):
            insert(ReviewRepo(connection), task_id="NOPE", script_id=script_id)

    @pytest.mark.parametrize("grade", ["D", "a", ""])
    def test_only_abc_grades_are_accepted(
        self, connection: sqlite3.Connection, task_id: str, script_id: str, grade: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            insert(ReviewRepo(connection), task_id=task_id, script_id=script_id, grade=grade)

    @pytest.mark.parametrize("decision", ["passed", "auto_pass", ""])
    def test_only_the_five_ddl_decisions_are_accepted(
        self, connection: sqlite3.Connection, task_id: str, script_id: str, decision: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            insert(ReviewRepo(connection), task_id=task_id, script_id=script_id, decision=decision)


class TestUniqueness:
    def test_the_same_script_and_round_cannot_be_scored_twice(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        """两份互相矛盾的分数比"这次没记上"危险得多 —— 撞唯一键是**故意的**。"""
        repo = ReviewRepo(connection)
        insert(repo, task_id=task_id, script_id=script_id, round_no=1)
        with pytest.raises(sqlite3.IntegrityError):
            insert(repo, task_id=task_id, script_id=script_id, round_no=1, grade="C")

    def test_a_later_round_of_the_same_script_is_allowed(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        repo = ReviewRepo(connection)
        insert(repo, task_id=task_id, script_id=script_id, round_no=1)
        insert(repo, task_id=task_id, script_id=script_id, round_no=2, grade="B")
        assert len(repo.list_for_task(task_id)) == 2

    def test_the_same_round_of_a_different_script_is_allowed(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        repo = ReviewRepo(connection)
        second = make_script(connection, task_id, hook="第二版开场")
        insert(repo, task_id=task_id, script_id=script_id, round_no=1)
        insert(repo, task_id=task_id, script_id=second, round_no=1, grade="B")
        assert len(repo.list_for_task(task_id)) == 2


class TestReads:
    def test_get_returns_none_for_an_unknown_id(self, connection: sqlite3.Connection) -> None:
        assert ReviewRepo(connection).get("NOPE") is None

    def test_get_returns_the_row(self, connection: sqlite3.Connection, task_id: str, script_id: str) -> None:
        repo = ReviewRepo(connection)
        row = insert(repo, task_id=task_id, script_id=script_id)
        assert repo.get(row.id) == row

    def test_latest_for_task_returns_none_without_reviews(self, connection: sqlite3.Connection) -> None:
        assert ReviewRepo(connection).latest_for_task("NOPE") is None

    def test_latest_for_task_is_the_highest_round(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        repo = ReviewRepo(connection)
        insert(repo, task_id=task_id, script_id=script_id, round_no=1)
        insert(repo, task_id=task_id, script_id=script_id, round_no=2, grade="B")
        insert(repo, task_id=task_id, script_id=script_id, round_no=3, grade="C", decision="discard")
        latest = repo.latest_for_task(task_id)
        assert latest is not None and latest.round_no == 3

    def test_list_for_task_is_ordered_by_round(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        repo = ReviewRepo(connection)
        insert(repo, task_id=task_id, script_id=script_id, round_no=3, grade="C", decision="discard")
        insert(repo, task_id=task_id, script_id=script_id, round_no=1)
        insert(repo, task_id=task_id, script_id=script_id, round_no=2, grade="B")
        assert [row.round_no for row in repo.list_for_task(task_id)] == [1, 2, 3]

    def test_list_for_task_only_returns_that_task(
        self, connection: sqlite3.Connection, task_id: str, script_id: str
    ) -> None:
        repo = ReviewRepo(connection)
        other_task = TaskService(connection).create(title="别的任务").id
        insert(repo, task_id=task_id, script_id=script_id)
        insert(repo, task_id=other_task, script_id=make_script(connection, other_task))
        assert [row.task_id for row in repo.list_for_task(task_id)] == [task_id]
