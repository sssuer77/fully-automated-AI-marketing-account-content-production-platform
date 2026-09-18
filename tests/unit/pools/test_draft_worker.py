"""写稿池的单元处理器（T4.11）：**迟到的那次写稿失败没有话语权**。

真机现场（T1.9 裁定 316）：CLI 的 `script draft` 就地跑完、把任务一路推进配音，
而池里那条 `draft/task` 单元还在跑**同一个**任务 —— 它稍后失败时会照常把任务
置 `failed`，把一次好端端的生产打断（随后流水线撞上非法迁移 `failed \u2192 queued_render`）。

两条边都要钉住：越过写稿段 \u21d2 空操作成功；仍在写稿段 \u21d2 照常抛错（否则
真失败会被吞掉，那比打断生产更糟）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.agents.fakes import persona

from studio.core.errors import StudioError
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.domain.enums import TaskStatus, UnitType
from studio.domain.task_service import TaskService
from studio.pools.draft_worker import DraftServices, DraftTaskHandler
from studio.pools.heartbeat import WorkerIdentity
from studio.pools.worker_base import UnitContext, _Pulse
from studio.services.script_service import DraftReport

WORKER_ID = "draft#1@4242"
TOPIC_ID = "01TOPIC00000000000000000000"
TASK_ID = "t1"


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 队列门面（**主线程专用**）。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    store: JobStore


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    connection.execute(
        "INSERT INTO tasks(id, title, topic, source_topic_id) VALUES (?, ?, ?, ?)",
        (TASK_ID, "测试任务", "测试任务", TOPIC_ID),
    )
    connection.commit()
    TaskService(connection).transition(TASK_ID, TaskStatus.DRAFTING, actor="worker:draft#1")
    try:
        yield Rig(paths=paths, connection=connection, store=JobStore(connection))
    finally:
        connection.close()


class _LateScripts:
    """一次"迟到"的写稿：跑完的那一刻，任务已经被别的路径推过了。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._tasks = TaskService(connection)
        self.calls = 0

    async def draft(self, **_: Any) -> DraftReport:
        self.calls += 1
        for target in (TaskStatus.REVIEWING, TaskStatus.QUEUED_VOICE, TaskStatus.VOICING):
            self._tasks.transition(TASK_ID, target, actor="pipeline")
        return DraftReport(
            ok=False,
            task_id=TASK_ID,
            topic_id=TOPIC_ID,
            error_code="LLM_SCHEMA_INVALID",
            error_message="模型没吐够",
        )


class _OnTimeScripts:
    """一次"准时"的失败：任务还停在写稿段。"""

    async def draft(self, **_: Any) -> DraftReport:
        return DraftReport(
            ok=False,
            task_id=TASK_ID,
            topic_id=TOPIC_ID,
            error_code="LLM_TIMEOUT",
            error_message="超时",
        )


class _NeverDrafts:
    """断点不在写稿段时，连 LLM 都不该碰（碰了就是白烧一次）。"""

    async def draft(self, **_: Any) -> Any:
        raise AssertionError("断点不在写稿段，不该重跑写稿")


class _NeverReviews:
    """越过写稿段就不该走到审稿 —— 走到了说明这条单元还在硬推。"""

    def review(self, **_: Any) -> Any:
        raise AssertionError("任务已越过写稿段，不该走到审稿")


def _run(rig: Rig, scripts: Any) -> tuple[Mapping[str, Any], UnitContext]:
    job_id = rig.store.enqueue(task_id=TASK_ID, pool="draft", unit_type=UnitType.TASK.value, unit_ref=TASK_ID)
    assert job_id is not None
    job = rig.store.claim(pool="draft", worker_id=WORKER_ID)
    assert job is not None and job.id == job_id
    ctx = UnitContext(
        job=job,
        pool="draft",
        worker_id=WORKER_ID,
        paths=rig.paths,
        timeout_sec=900,
        pulse=_Pulse(
            identity=WorkerIdentity(pool="draft", slot=1, pid=4242),
            connection_factory=lambda: rig.connection,
            version=None,
        ),
        renew=lambda: True,
    )
    handler = DraftTaskHandler(
        paths=rig.paths,
        services_factory=lambda _connection: DraftServices(
            scripts=scripts, reviews=cast(Any, _NeverReviews())
        ),
        persona_provider=persona,
    )
    return handler.run(ctx), ctx


def test_a_breakpoint_outside_the_draft_segment_is_a_noop_success(rig: Rig) -> None:
    """断点不在写稿段 ⇒ 空操作成功（重投死信不该变成"再死一次"）。

    真机现场：任务在配音段挂掉（`retry_from=voicing`），写稿池那条死信被重投后
    会一路走到 `review()`，拿 `failed` 去撞 `failed ⇒ reviewing` 这条不存在的边，
    于是"重投"只是把死信又生产了一遍。
    """
    tasks = TaskService(rig.connection)
    tasks.transition(TASK_ID, TaskStatus.REVIEWING, actor="pipeline")
    tasks.transition(TASK_ID, TaskStatus.QUEUED_VOICE, actor="pipeline")
    tasks.transition(TASK_ID, TaskStatus.VOICING, actor="pipeline")
    tasks.transition(TASK_ID, TaskStatus.FAILED, actor="worker:voice#1", error_code="TTS_FAILED")

    outcome, _ = _run(rig, _NeverDrafts())

    assert outcome["drafted"] is False
    assert outcome["status"] == TaskStatus.FAILED.value
    assert "越过写稿段" in str(outcome["note"])
    assert TaskService(rig.connection).get(TASK_ID).status is TaskStatus.FAILED


def test_a_late_draft_failure_is_a_noop_success(rig: Rig) -> None:
    """任务已被推过写稿段 \u21d2 空操作成功（不记死信，也不天天报假警）。"""
    outcome, _ = _run(rig, _LateScripts(rig.connection))

    assert outcome["drafted"] is False
    assert outcome["status"] == TaskStatus.VOICING.value
    assert "越过写稿段" in str(outcome["note"])
    assert TaskService(rig.connection).get(TASK_ID).status is TaskStatus.VOICING


def test_a_failure_while_still_drafting_still_raises(rig: Rig) -> None:
    """边界另一侧：任务还在写稿段 \u21d2 照常抛错，让队列的重试 / 死信接手。"""
    with pytest.raises(StudioError) as info:
        _run(rig, _OnTimeScripts())

    assert info.value.code.value == "LLM_TIMEOUT"
    assert TaskService(rig.connection).get(TASK_ID).status is TaskStatus.DRAFTING
