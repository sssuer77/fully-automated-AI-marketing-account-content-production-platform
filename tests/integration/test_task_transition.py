"""``TaskService.transition`` 集成测试（T1.4 验收）：真库上的乐观锁与审计链。

覆盖"单测覆盖不到、只有真库才暴露"的部分：``version`` 乐观锁、
``task_events`` / ``system_logs`` 的同事务落库、``pool`` 冗余列跟随状态。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode
from studio.db.engine import connect
from studio.db.migrate import migrate
from studio.domain.enums import TaskPool
from studio.domain.enums import TaskStatus as S
from studio.domain.errors import ConcurrentModification, IllegalTransition, TaskNotFound
from studio.domain.task_service import TaskService


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "studio.db"
    migrate(path)
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _insert_task(conn: sqlite3.Connection, task_id: str = "t1", **overrides: object) -> None:
    columns: dict[str, object] = {"id": task_id, "title": "测试任务"}
    columns.update(overrides)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO tasks({','.join(columns)}) VALUES ({placeholders})",
        tuple(columns.values()),
    )


def _count(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


@pytest.fixture
def service(connection: sqlite3.Connection) -> TaskService:
    _insert_task(connection)
    return TaskService(connection)


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


def test_get_missing_task_raises(connection: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFound) as excinfo:
        TaskService(connection).get("nope")
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_new_task_defaults(service: TaskService) -> None:
    task = service.get("t1")
    assert task.status is S.PENDING
    assert task.pool is TaskPool.DRAFT
    assert task.version == 1
    assert task.attempt_count == 0
    assert task.payload.template_id == "douyin_9x16_default"


# ══════════════════════════════════════════════════════════════════════
# 合法迁移：状态 / 版本 / 审计
# ══════════════════════════════════════════════════════════════════════


def test_legal_transition_bumps_version_and_status(service: TaskService) -> None:
    result = service.transition("t1", S.DRAFTING, actor="worker:draft#1", reason="开始写稿")
    assert result.task.status is S.DRAFTING
    assert result.task.version == 2
    assert result.event.from_status is S.PENDING
    assert result.event.to_status is S.DRAFTING
    assert result.event.actor == "worker:draft#1"
    assert result.event.reason == "开始写稿"


def test_transition_writes_task_event(connection: sqlite3.Connection, service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.REVIEWING, actor="system")
    rows = connection.execute("SELECT from_status, to_status FROM task_events ORDER BY id").fetchall()
    assert [(r["from_status"], r["to_status"]) for r in rows] == [
        ("pending", "drafting"),
        ("drafting", "reviewing"),
    ]


def test_transition_writes_system_log(connection: sqlite3.Connection, service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="worker:draft#1", reason="开始写稿")
    row = connection.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "info"
    assert row["source"] == "domain.task"
    assert row["task_id"] == "t1"
    assert row["stage"] == "drafting"
    assert row["message"] == "pending → drafting（开始写稿）"
    assert row["worker_id"] == "worker:draft#1"


def test_failed_logs_error_level(connection: sqlite3.Connection, service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.FAILED, actor="worker:draft#1")
    row = connection.execute("SELECT level FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "error"


def test_manual_pool_logs_warn_level(connection: sqlite3.Connection, service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.FAILED, actor="system")
    service.transition("t1", S.MANUAL_POOL, actor="system")
    row = connection.execute("SELECT level FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "warn"


def test_pool_column_follows_status(service: TaskService) -> None:
    """``tasks.pool`` 是 WebUI 的过滤维度，必须跟着状态走。"""
    chain = [
        (S.DRAFTING, TaskPool.DRAFT),
        (S.REVIEWING, TaskPool.DRAFT),
        (S.QUEUED_VOICE, TaskPool.VOICE),
        (S.VOICING, TaskPool.VOICE),
        (S.QUEUED_RENDER, TaskPool.RENDER),
        (S.RENDERING, TaskPool.RENDER),
        (S.COMPLETED, TaskPool.NONE),
        (S.PUBLISHING, TaskPool.PUBLISH),
        (S.PUBLISHED, TaskPool.PUBLISH),
    ]
    for status, pool in chain:
        assert service.transition("t1", status, actor="system").task.pool is pool


# ══════════════════════════════════════════════════════════════════════
# 非法迁移：拒绝 + 不留痕
# ══════════════════════════════════════════════════════════════════════


def test_illegal_transition_writes_nothing(connection: sqlite3.Connection, service: TaskService) -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        service.transition("t1", S.RENDERING, actor="system")
    assert excinfo.value.context["from"] == "pending"
    assert service.get("t1").status is S.PENDING
    assert service.get("t1").version == 1
    assert _count(connection, "SELECT count(*) FROM task_events") == 0
    assert _count(connection, "SELECT count(*) FROM system_logs") == 0


def test_actor_must_not_be_blank(service: TaskService) -> None:
    with pytest.raises(ValueError):
        service.transition("t1", S.DRAFTING, actor="   ")


# ══════════════════════════════════════════════════════════════════════
# 乐观锁
# ══════════════════════════════════════════════════════════════════════


def test_stale_expected_version_is_rejected(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")  # version 1 → 2
    with pytest.raises(ConcurrentModification) as excinfo:
        service.transition("t1", S.REVIEWING, actor="system", expected_version=1)
    assert excinfo.value.code is ErrorCode.STATE_VERSION_CONFLICT
    assert excinfo.value.context["actual_version"] == 2
    assert service.get("t1").status is S.DRAFTING


def test_matching_expected_version_passes(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    result = service.transition("t1", S.REVIEWING, actor="system", expected_version=2)
    assert result.task.version == 3


def test_concurrent_writer_loses_without_overwrite(connection: sqlite3.Connection) -> None:
    """两个写者各自持旧 version：先到者成功，后到者**重试而非覆盖**。"""
    _insert_task(connection, "t2")
    first = TaskService(connection)
    second = TaskService(connection)
    stale = first.get("t2").version

    first.transition("t2", S.DRAFTING, actor="worker:draft#1", expected_version=stale)
    with pytest.raises(ConcurrentModification):
        second.transition("t2", S.CANCELED, actor="user", expected_version=stale)
    assert first.get("t2").status is S.DRAFTING


def test_expected_version_must_be_positive(service: TaskService) -> None:
    with pytest.raises(ValueError):
        service.transition("t1", S.DRAFTING, actor="system", expected_version=0)


# ══════════════════════════════════════════════════════════════════════
# 断点：failed / manual_pool / 重试
# ══════════════════════════════════════════════════════════════════════


def test_failed_records_breakpoint(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.REVIEWING, actor="system")
    task = service.transition(
        "t1",
        S.FAILED,
        actor="worker:draft#1",
        reason="LLM 超时",
        error_code=str(ErrorCode.INTERNAL),
        error_message="三次重试均超时",
    ).task
    assert task.status is S.FAILED
    assert task.last_healthy_status is S.REVIEWING
    assert task.retry_from is S.REVIEWING
    assert task.attempt_count == 1
    assert task.error_code == str(ErrorCode.INTERNAL)
    assert task.error_message == "三次重试均超时"


def test_pending_cannot_fail_directly(connection: sqlite3.Connection) -> None:
    """``pending`` 没有到 ``failed`` 的边 ⇒ 不可能凭空造出一个断点。"""
    _insert_task(connection, "t3")
    service = TaskService(connection)
    with pytest.raises(IllegalTransition):
        service.transition("t3", S.FAILED, actor="system")
    assert service.get("t3").retry_from is None
    assert service.get("t3").last_healthy_status is None


def test_retry_from_whitelist_guard(connection: sqlite3.Connection) -> None:
    """库里若存在非白名单的 ``retry_from``（历史脏数据），动态边必须拒绝。"""
    _insert_task(connection, "t4", status="failed", retry_from="pending")
    service = TaskService(connection)
    assert service.get("t4").retry_from is S.PENDING

    with pytest.raises(IllegalTransition):
        service.transition("t4", S.PENDING, actor="user")
    # 静态边不受影响：failed → canceled 仍然放行
    assert service.transition("t4", S.CANCELED, actor="user").task.status is S.CANCELED


def test_retry_uses_retry_from_and_clears_error(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.REVIEWING, actor="system")
    service.transition("t1", S.QUEUED_VOICE, actor="system")
    service.transition("t1", S.VOICING, actor="system")
    service.transition(
        "t1",
        S.FAILED,
        actor="worker:voice#1",
        error_code=str(ErrorCode.TTS_OOM),
        error_message="显存不足",
    )

    task = service.transition("t1", S.VOICING, actor="system", reason="自动重试").task
    assert task.status is S.VOICING
    assert task.retry_from is S.VOICING  # 断点保留，再次失败还能回到这里
    assert task.error_code is None
    assert task.error_message is None
    assert task.attempt_count == 1


def test_retry_rejects_target_other_than_retry_from(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.REVIEWING, actor="system")
    service.transition("t1", S.QUEUED_VOICE, actor="system")
    service.transition("t1", S.VOICING, actor="system")
    service.transition("t1", S.FAILED, actor="system")

    with pytest.raises(IllegalTransition):
        service.transition("t1", S.QUEUED_VOICE, actor="system")  # retry_from 是 voicing


def test_failed_to_manual_pool(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")
    service.transition("t1", S.FAILED, actor="system")
    task = service.transition("t1", S.MANUAL_POOL, actor="system", reason="反复失败").task
    assert task.status is S.MANUAL_POOL
    assert task.retry_from is S.DRAFTING


# ══════════════════════════════════════════════════════════════════════
# 生命周期时间戳
# ══════════════════════════════════════════════════════════════════════


def test_started_at_set_once(service: TaskService) -> None:
    assert service.get("t1").started_at is None
    started = service.transition("t1", S.DRAFTING, actor="system").task.started_at
    assert started is not None
    again = service.transition("t1", S.REVIEWING, actor="system").task.started_at
    assert again == started


def test_full_chain_sets_finished_at(service: TaskService) -> None:
    chain = [
        S.DRAFTING,
        S.REVIEWING,
        S.QUEUED_VOICE,
        S.VOICING,
        S.QUEUED_RENDER,
        S.RENDERING,
        S.COMPLETED,
        S.PUBLISHING,
        S.PUBLISHED,
    ]
    for status in chain:
        task = service.transition("t1", status, actor="system").task
    assert task.status is S.PUBLISHED
    assert task.finished_at is not None
    assert task.version == 1 + len(chain)


def test_rescue_from_canceled_clears_finished_at(service: TaskService) -> None:
    canceled = service.transition("t1", S.CANCELED, actor="user").task
    assert canceled.finished_at is not None
    rescued = service.transition("t1", S.PENDING, actor="user", reason="误操作恢复").task
    assert rescued.status is S.PENDING
    assert rescued.finished_at is None


# ══════════════════════════════════════════════════════════════════════
# 进度（非状态字段）
# ══════════════════════════════════════════════════════════════════════


def test_update_progress_does_not_bump_version(service: TaskService) -> None:
    service.transition("t1", S.DRAFTING, actor="system")  # version 1 → 2
    task = service.update_progress("t1", progress=0.42, stage_detail="voice: 12/27 句")
    assert task.progress == pytest.approx(0.42)
    assert task.stage_detail == "voice: 12/27 句"
    assert task.version == 2


def test_update_progress_rejects_out_of_range(service: TaskService) -> None:
    with pytest.raises(ValueError):
        service.update_progress("t1", progress=1.5)
    with pytest.raises(ValueError):
        service.update_progress("t1", progress=-0.1)


def test_update_progress_missing_task_raises(connection: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFound):
        TaskService(connection).update_progress("nope", progress=0.1)
