"""产物清单仓储（T2.7 起 · §03.3.11）。

钉三件事，每件都有过"看起来对"的错法：

1. **路径相对 ``data/``**：写绝对路径不会报错，只会在换机器 / 搬目录之后
   指着一堆不存在的文件 —— 而清单指错地方比没有清单更坏；
2. **重复产出刷新事实**（``bytes`` / ``sha256`` / ``meta``），不是插第二行、也不是报错：
   ``UNIQUE(task_id, kind, path)`` 决定了同一份产物只有一行；
3. **文件不在了不抛**：``bytes`` / ``sha256`` 只是留痕字段（与 ``core/files.file_sha256``
   同一条口径）—— 为一次留痕把收尾阶段打挂不值当。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import ArtifactRepo
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
def repo(connection: sqlite3.Connection, tmp_path: Path) -> ArtifactRepo:
    return ArtifactRepo(connection, data_dir=tmp_path / "studio" / "data")


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="产物清单").id


def test_record_stores_a_path_relative_to_data(repo: ArtifactRepo, task_id: str, tmp_path: Path) -> None:
    data_dir = tmp_path / "studio" / "data"
    timeline = data_dir / "work" / task_id / "timeline.json"
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_text("{}", encoding="utf-8")

    repo.record(task_id=task_id, kind="timeline", path=timeline, ttl_hint="forever")

    row = repo.list_for_task(task_id)[0]
    assert row.path == f"work/{task_id}/timeline.json"
    assert row.size_bytes == 2
    assert row.sha256 is not None and len(row.sha256) == 64
    assert row.ttl_hint == "forever"


def test_record_is_idempotent_and_refreshes_the_facts(
    repo: ArtifactRepo, task_id: str, tmp_path: Path
) -> None:
    """同一份产物再记一次 ⇒ 还是**一行**，而且事实是新的那一份。"""
    master = tmp_path / "studio" / "data" / "work" / task_id / "tts" / "voice_master.wav"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"a" * 10)
    repo.record(task_id=task_id, kind="voice_master", path=master, meta={"duration_ms": 100})

    master.write_bytes(b"b" * 30)
    repo.record(task_id=task_id, kind="voice_master", path=master, meta={"duration_ms": 300})

    rows = repo.list_for_task(task_id)
    assert len(rows) == 1
    assert rows[0].size_bytes == 30
    assert rows[0].meta == {"duration_ms": 300}


def test_record_keeps_the_kinds_apart(repo: ArtifactRepo, task_id: str, tmp_path: Path) -> None:
    """``kind`` 是清单的一部分：同一个路径换了 ``kind`` 就是另一条产物。"""
    shared = tmp_path / "studio" / "data" / "work" / task_id / "timeline.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("{}", encoding="utf-8")

    repo.record(task_id=task_id, kind="timeline", path=shared)
    repo.record(task_id=task_id, kind="manifest", path=shared)

    assert repo.kinds_for_task(task_id) == ["manifest", "timeline"]
    assert len(repo.list_for_task(task_id, kind="manifest")) == 1


def test_record_does_not_raise_when_the_file_is_gone(
    repo: ArtifactRepo, task_id: str, tmp_path: Path
) -> None:
    """文件刚被 GC 删掉的那一瞬间，"指纹为空"是如实回答，不是错误。"""
    gone = tmp_path / "studio" / "data" / "work" / task_id / "gone.wav"

    repo.record(task_id=task_id, kind="voice_master", path=gone)

    row = repo.list_for_task(task_id)[0]
    assert (row.size_bytes, row.sha256) == (None, None)
    assert row.path == f"work/{task_id}/gone.wav"


def test_list_for_task_is_empty_for_an_unknown_task(repo: ArtifactRepo) -> None:
    assert repo.list_for_task("nope") == []
    assert repo.kinds_for_task("nope") == []
