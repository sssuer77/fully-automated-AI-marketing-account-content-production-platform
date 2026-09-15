"""``scripts`` / ``script_sentences`` 仓储（T1.10 · §03.3.7）。

这里测的是**事务边界**与**版本语义**：两表必须同生共死、``is_active`` 只能有一版。
用真实库（临时目录 + 迁移）而不是 mock 连接 —— 这些断言的价值全在 SQL 与索引上。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import SavedScript, ScriptRepo
from studio.domain import TaskService


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


def _sentences(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "seq": index,
            "text_raw": f"第{index}句",
            "text": f"第{index}句",
            "speaker": "bigbear",
            "emotion": "neutral",
            "pause_after_ms": 200,
        }
        for index in range(1, count + 1)
    ]


def _save(
    repo: ScriptRepo,
    task_id: str,
    *,
    sentences: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> SavedScript:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "title": "标题",
        "hook": "钩子",
        "body_md": "正文",
        "cta": "关注",
        "word_count": 700,
        "est_duration_ms": 140_000,
        "speaker_ratio": {"bigbear": 1.0},
        "outline": {"hook_3s": "钩子", "segments": []},
        "sentences": _sentences() if sentences is None else sentences,
    }
    payload.update(extra)
    return repo.save_draft(**payload)


class TestSaveDraft:
    def test_writes_both_tables(self, connection: sqlite3.Connection, task_id: str) -> None:
        repo = ScriptRepo(connection)
        saved = _save(repo, task_id)
        assert saved.version == 1
        assert saved.sentence_count == 3
        assert repo.count_sentences(saved.script_id) == 3

    def test_json_columns_round_trip(self, connection: sqlite3.Connection, task_id: str) -> None:
        repo = ScriptRepo(connection)
        saved = _save(repo, task_id)
        row = repo.get(saved.script_id)
        assert row is not None
        assert row.speaker_ratio == {"bigbear": 1.0}
        assert row.outline["hook_3s"] == "钩子"

    def test_sentences_come_back_in_seq_order(self, connection: sqlite3.Connection, task_id: str) -> None:
        repo = ScriptRepo(connection)
        saved = _save(repo, task_id, sentences=list(reversed(_sentences(5))))
        assert [row.seq for row in repo.list_sentences(saved.script_id)] == [1, 2, 3, 4, 5]

    def test_empty_sentences_are_refused(self, connection: sqlite3.Connection, task_id: str) -> None:
        repo = ScriptRepo(connection)
        with pytest.raises(ValueError):
            _save(repo, task_id, sentences=[])
        assert repo.get_active(task_id) is None

    def test_duplicate_seq_rolls_back_the_script_row(
        self, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """句子写失败 ⇒ **连稿件行都不能留下**（"有稿无句"的半成品禁止存在）。"""
        repo = ScriptRepo(connection)
        broken = [*_sentences(2), _sentences(1)[0]]
        with pytest.raises(sqlite3.IntegrityError):
            _save(repo, task_id, sentences=broken)
        assert repo.get_active(task_id) is None
        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0


class TestVersions:
    def test_second_save_bumps_version_and_flips_active(
        self, connection: sqlite3.Connection, task_id: str
    ) -> None:
        repo = ScriptRepo(connection)
        first = _save(repo, task_id)
        second = _save(repo, task_id, title="标题2")
        assert second.version == 2
        active = repo.get_active(task_id)
        assert active is not None and active.id == second.script_id
        assert [row.version for row in repo.list_versions(task_id)] == [2, 1]
        assert [row.is_active for row in repo.list_versions(task_id)] == [True, False]
        assert repo.get(first.script_id) is not None

    def test_active_is_scoped_per_task(self, connection: sqlite3.Connection) -> None:
        tasks = TaskService(connection)
        repo = ScriptRepo(connection)
        first_task = tasks.create(title="甲").id
        second_task = tasks.create(title="乙").id
        _save(repo, first_task)
        _save(repo, second_task)
        assert repo.get_active(first_task).id != repo.get_active(second_task).id  # type: ignore[union-attr]

    def test_no_active_script_returns_none(self, connection: sqlite3.Connection, task_id: str) -> None:
        assert ScriptRepo(connection).get_active(task_id) is None

    def test_unknown_script_returns_none(self, connection: sqlite3.Connection) -> None:
        assert ScriptRepo(connection).get("nope") is None
