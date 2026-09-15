"""``approvals`` 仓储（T1.11 · §04.4.4）—— 确认闸的唯一数据入口。

两个不变量在这里被钉死：

1. **不删行**：``pending`` 是待办、``approved`` / ``rejected`` 是历史，同表两态；
2. **只对 pending 生效**：重复点击不会把第二个人的意见盖在第一个人的上面
   （``decide`` 返回 ``None`` 就是这件事的可观测信号）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import ApprovalRepo, ScriptRepo
from studio.domain import TaskService


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


@pytest.fixture
def repo(connection: sqlite3.Connection) -> ApprovalRepo:
    return ApprovalRepo(connection)


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="跑酷合集").id


def make_script(connection: sqlite3.Connection, task_id: str) -> str:
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook="开场",
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


class TestRequest:
    def test_a_new_request_is_pending_and_undecided(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=2)
        assert row.pending
        assert (row.decided_at, row.decided_by, row.comment) == (None, None, None)
        assert (row.grade, row.score_total, row.revision_round) == ("B", 6.3, 2)
        assert row.requested_at is not None

    def test_the_three_facts_the_human_needs_are_stored(self, repo: ApprovalRepo, task_id: str) -> None:
        """§2.2⑦：等级 / 评分 / 修改次数 —— 少了它们，确认闸就只是"盲点通过"。"""
        row = repo.request(task_id=task_id, script_id=None, grade="C", score_total=4.35, revision_round=1)
        stored = repo.get(row.id)
        assert stored is not None
        assert (stored.grade, stored.score_total, stored.revision_round) == ("C", 4.35, 1)

    def test_an_unknown_task_is_rejected_by_the_foreign_key(self, repo: ApprovalRepo) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            repo.request(task_id="NOPE", script_id=None, grade="B", score_total=6.3, revision_round=0)

    def test_auto_expire_is_off_by_default(self, repo: ApprovalRepo, task_id: str) -> None:
        assert (
            repo.request(
                task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0
            ).auto_expire_at
            is None
        )

    def test_auto_expire_can_be_set(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(
            task_id=task_id,
            script_id=None,
            grade="B",
            score_total=6.3,
            revision_round=0,
            auto_expire_at="2026-09-14T00:00:00.000Z",
        )
        assert row.auto_expire_at == "2026-09-14T00:00:00.000Z"


class TestRecordAutoApproval:
    def test_it_writes_one_already_decided_row(self, repo: ApprovalRepo, task_id: str) -> None:
        """裁定 94：自动放行**一次事务**写一条 ``approved``，不留永远 pending 的幽灵。"""
        row = repo.record_auto_approval(
            task_id=task_id,
            script_id=None,
            grade="A",
            score_total=8.7,
            revision_round=0,
            decided_by="auto_approve_A",
        )
        assert row.status == "approved"
        assert row.decided_by == "auto_approve_A"
        assert row.decided_at is not None
        assert not row.pending

    def test_it_leaves_nothing_pending(self, repo: ApprovalRepo, task_id: str) -> None:
        repo.record_auto_approval(
            task_id=task_id,
            script_id=None,
            grade="A",
            score_total=8.7,
            revision_round=0,
            decided_by="auto_approve_A",
        )
        assert repo.pending_for_task(task_id) is None
        assert repo.list_pending() == []


class TestDecide:
    def test_deciding_a_pending_row_records_who_and_why(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        decided = repo.decide(row.id, status="rejected", decided_by="user", comment="开场太平")
        assert decided is not None
        assert (decided.status, decided.decided_by, decided.comment) == ("rejected", "user", "开场太平")
        assert decided.decided_at is not None

    def test_deciding_twice_returns_none_and_keeps_the_first_verdict(
        self, repo: ApprovalRepo, task_id: str
    ) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        repo.decide(row.id, status="approved", decided_by="user")
        assert repo.decide(row.id, status="rejected", decided_by="user", comment="反悔") is None
        stored = repo.get(row.id)
        assert stored is not None and stored.status == "approved" and stored.comment is None

    def test_deciding_an_unknown_id_returns_none(self, repo: ApprovalRepo) -> None:
        assert repo.decide("NOPE", status="approved", decided_by="user") is None

    def test_comment_is_optional_on_approval(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        decided = repo.decide(row.id, status="approved", decided_by="user")
        assert decided is not None and decided.comment is None

    def test_comment_survives_a_second_write_without_one(self, repo: ApprovalRepo, task_id: str) -> None:
        """``COALESCE`` 的语义：不传 comment 不会把已有的意见抹掉。"""
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        repo.decide(row.id, status="rejected", decided_by="user", comment="开场太平")
        stored = repo.get(row.id)
        assert stored is not None and stored.comment == "开场太平"

    @pytest.mark.parametrize("status", ["pending", "expired_soon", ""])
    def test_an_illegal_target_status_raises_before_touching_the_db(
        self, repo: ApprovalRepo, task_id: str, status: str
    ) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        with pytest.raises(ValueError):
            repo.decide(row.id, status=status, decided_by="user")
        assert repo.get(row.id) is not None

    @pytest.mark.parametrize("status", ["approved", "rejected", "discarded", "expired"])
    def test_every_decided_status_is_accepted(self, repo: ApprovalRepo, task_id: str, status: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=1)
        decided = repo.decide(row.id, status=status, decided_by="user")
        assert decided is not None and decided.status == status


class TestReads:
    def test_get_returns_none_for_an_unknown_id(self, repo: ApprovalRepo) -> None:
        assert repo.get("NOPE") is None

    def test_pending_for_task_returns_the_open_one(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        pending = repo.pending_for_task(task_id)
        assert pending is not None and pending.id == row.id

    def test_pending_for_task_is_none_once_decided(self, repo: ApprovalRepo, task_id: str) -> None:
        row = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        repo.decide(row.id, status="approved", decided_by="user")
        assert repo.pending_for_task(task_id) is None

    def test_pending_for_task_ignores_other_tasks(
        self, repo: ApprovalRepo, connection: sqlite3.Connection, task_id: str
    ) -> None:
        other = TaskService(connection).create(title="别的任务").id
        repo.request(task_id=other, script_id=None, grade="B", score_total=6.3, revision_round=0)
        assert repo.pending_for_task(task_id) is None

    def test_list_pending_only_returns_open_ones(self, repo: ApprovalRepo, task_id: str) -> None:
        kept = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        closed = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        repo.decide(closed.id, status="approved", decided_by="user")
        assert [row.id for row in repo.list_pending()] == [kept.id]

    def test_list_pending_respects_the_limit(self, repo: ApprovalRepo, task_id: str) -> None:
        for _ in range(3):
            repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        assert len(repo.list_pending(limit=2)) == 2

    def test_list_for_task_is_chronological(self, repo: ApprovalRepo, task_id: str) -> None:
        first = repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        repo.decide(first.id, status="rejected", decided_by="user", comment="再改")
        second = repo.request(task_id=task_id, script_id=None, grade="B", score_total=7.1, revision_round=1)
        assert [row.id for row in repo.list_for_task(task_id)] == [first.id, second.id]

    def test_count_by_status(self, repo: ApprovalRepo, task_id: str) -> None:
        repo.request(task_id=task_id, script_id=None, grade="B", score_total=6.3, revision_round=0)
        repo.record_auto_approval(
            task_id=task_id,
            script_id=None,
            grade="A",
            score_total=8.7,
            revision_round=0,
            decided_by="auto_approve_A",
        )
        assert repo.count_by_status() == {"pending": 1, "approved": 1}

    def test_the_script_id_is_kept_when_it_exists(
        self, repo: ApprovalRepo, connection: sqlite3.Connection, task_id: str
    ) -> None:
        script_id = make_script(connection, task_id)
        row = repo.request(task_id=task_id, script_id=script_id, grade="B", score_total=6.3, revision_round=0)
        assert row.script_id == script_id
