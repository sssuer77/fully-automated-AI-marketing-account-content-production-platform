"""``audit_ops`` 仓储（T1.11 · §03.3.16）—— 操作留痕。

两个要点：

1. ``actor`` / ``result`` / ``source`` 在**写之前**校验（让 CHECK 在运行期炸掉，
   等于把"参数传错了"变成"审计写不进去"）；
2. ``task_id`` **没有外键** —— 任务被删掉之后，"谁在什么时候放行了它"仍要查得到。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import AuditRepo
from studio.db.repositories.audit_repo import ACTORS, RESULTS, SOURCES
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
def repo(connection: sqlite3.Connection) -> AuditRepo:
    return AuditRepo(connection)


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="跑酷合集").id


class TestVocabulary:
    def test_the_three_vocabularies_are_the_ddl_ones(self) -> None:
        assert {"user", "system", "auto", "worker"} == ACTORS
        assert {"ok", "denied", "error"} == RESULTS
        assert {"webui", "api", "cli", "worker", "auto"} == SOURCES


class TestRecord:
    def test_it_returns_the_row_with_an_autoincrement_id(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(
            actor="user",
            action="task.approve",
            target_type="task",
            target_id=task_id,
            task_id=task_id,
            source="webui",
        )
        assert row.id >= 1
        assert row.at is not None
        assert (row.actor, row.action, row.target_type) == ("user", "task.approve", "task")

    def test_ids_are_monotonic(self, repo: AuditRepo, task_id: str) -> None:
        first = repo.record(
            actor="auto", action="task.discard", target_type="task", task_id=task_id, source="auto"
        )
        second = repo.record(
            actor="auto", action="task.discard", target_type="task", task_id=task_id, source="auto"
        )
        assert second.id > first.id

    def test_defaults_are_ok_and_webui(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(actor="user", action="x", target_type="task", task_id=task_id)
        assert (row.result, row.source) == ("ok", "webui")

    def test_before_and_after_round_trip(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(
            actor="auto",
            actor_ref="auto_approve_A",
            action="task.approve",
            target_type="task",
            task_id=task_id,
            before={"status": "awaiting_approval"},
            after={"status": "queued_voice", "grade": "A"},
            source="auto",
        )
        assert row.before == {"status": "awaiting_approval"}
        assert row.after == {"status": "queued_voice", "grade": "A"}
        assert row.actor_ref == "auto_approve_A"

    def test_the_human_comment_lands_in_reason(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(
            actor="user",
            action="task.reject",
            target_type="task",
            task_id=task_id,
            reason="开场太平，重写",
            result="denied",
        )
        assert row.reason == "开场太平，重写"
        assert row.result == "denied"

    def test_request_id_and_ip_are_kept(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(
            actor="user",
            action="x",
            target_type="task",
            task_id=task_id,
            request_id="req-1",
            ip="127.0.0.1",
        )
        assert (row.request_id, row.ip) == ("req-1", "127.0.0.1")

    @pytest.mark.parametrize("actor", ["root", "AUTO", ""])
    def test_an_illegal_actor_raises_before_writing(self, repo: AuditRepo, task_id: str, actor: str) -> None:
        with pytest.raises(ValueError):
            repo.record(actor=actor, action="x", target_type="task", task_id=task_id)
        assert repo.list_for_task(task_id) == []

    @pytest.mark.parametrize("result", ["success", "OK", ""])
    def test_an_illegal_result_raises_before_writing(
        self, repo: AuditRepo, task_id: str, result: str
    ) -> None:
        with pytest.raises(ValueError):
            repo.record(actor="user", action="x", target_type="task", task_id=task_id, result=result)

    @pytest.mark.parametrize("source", ["ui", "WEBUI", ""])
    def test_an_illegal_source_raises_before_writing(
        self, repo: AuditRepo, task_id: str, source: str
    ) -> None:
        with pytest.raises(ValueError):
            repo.record(actor="user", action="x", target_type="task", task_id=task_id, source=source)

    def test_a_row_can_be_written_without_a_task_id(self, repo: AuditRepo) -> None:
        """系统级操作（改配置 / 暂停池）不属于任何任务。"""
        row = repo.record(
            actor="system", action="pool.pause", target_type="pool", target_id="draft", source="cli"
        )
        assert row.task_id is None

    def test_a_row_can_reference_a_task_that_does_not_exist(self, repo: AuditRepo) -> None:
        """``task_id`` 没有外键 —— 留痕不该被外键挡住。"""
        row = repo.record(
            actor="auto", action="task.discard", target_type="task", task_id="GONE", source="auto"
        )
        assert row.task_id == "GONE"


class TestReads:
    def test_get_returns_none_for_an_unknown_id(self, repo: AuditRepo) -> None:
        assert repo.get(999_999) is None

    def test_get_returns_the_row(self, repo: AuditRepo, task_id: str) -> None:
        row = repo.record(actor="user", action="x", target_type="task", task_id=task_id)
        assert repo.get(row.id) == row

    def test_list_for_task_is_newest_first(self, repo: AuditRepo, task_id: str) -> None:
        for index in range(3):
            repo.record(actor="user", action=f"step{index}", target_type="task", task_id=task_id)
        ids = [row.id for row in repo.list_for_task(task_id)]
        assert ids == sorted(ids, reverse=True)

    def test_list_for_task_only_returns_that_task(
        self, repo: AuditRepo, connection: sqlite3.Connection, task_id: str
    ) -> None:
        other = TaskService(connection).create(title="别的任务").id
        repo.record(actor="user", action="x", target_type="task", task_id=task_id)
        repo.record(actor="user", action="y", target_type="task", task_id=other)
        assert [row.action for row in repo.list_for_task(task_id)] == ["x"]

    def test_list_for_task_respects_the_limit(self, repo: AuditRepo, task_id: str) -> None:
        for index in range(3):
            repo.record(actor="user", action=f"step{index}", target_type="task", task_id=task_id)
        assert len(repo.list_for_task(task_id, limit=2)) == 2

    def test_list_recent_is_newest_first(self, repo: AuditRepo, task_id: str) -> None:
        for index in range(3):
            repo.record(actor="user", action=f"step{index}", target_type="task", task_id=task_id)
        ids = [row.id for row in repo.list_recent()]
        assert ids == sorted(ids, reverse=True)

    def test_list_recent_respects_the_limit(self, repo: AuditRepo, task_id: str) -> None:
        for index in range(3):
            repo.record(actor="user", action=f"step{index}", target_type="task", task_id=task_id)
        assert len(repo.list_recent(limit=2)) == 2


class TestSurvivesCascade:
    def test_audit_rows_outlive_the_task_they_talk_about(
        self, repo: AuditRepo, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """★ DDL 注释："留痕不得被级联删除" —— 删掉任务后操作史必须还在。"""
        repo.record(actor="auto", action="task.discard", target_type="task", task_id=task_id, source="auto")
        connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        rows = repo.list_for_task(task_id)
        assert len(rows) == 1 and rows[0].action == "task.discard"
