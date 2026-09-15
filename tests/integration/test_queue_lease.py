"""队列内核集成测试（T1.5 验收 · §03.4）—— 真库上的四池并发语义。

六条验收（todolist T1.5）：
① 优先级认领顺序 ② 租约未过期不回收 / 过期回收 + 退避 ③ 依赖 2 级解锁
④ ``attempts >= max`` ⇒ ``dead`` ⑤ 幂等入队不重复 ⑥ ``CHECK`` 拒绝非法状态
外加 ``publish`` 池限频守卫与池控制（暂停 / 并发）。

时间一律由 ``now=`` 参数注入（T1.5 施工裁定 26），因此**不需要** monkeypatch 全局时钟。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from studio.core.clock import format_iso, parse_iso
from studio.core.errors import ErrorCode, QueueError, StudioError
from studio.db.engine import connect
from studio.db.migrate import migrate
from studio.db.queue import (
    LEASE_EXPIRED_CODE,
    JobStore,
)

T0 = datetime(2026, 9, 13, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "studio.db"
    migrate(path)
    conn = connect(path)
    try:
        conn.execute("INSERT INTO tasks(id, title) VALUES ('t1', '测试任务')")
        conn.execute("INSERT INTO tasks(id, title) VALUES ('t2', '另一个任务')")
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(connection: sqlite3.Connection) -> JobStore:
    return JobStore(connection)


def _set_pool(conn: sqlite3.Connection, pool: str, **columns: object) -> None:
    """调池旋钮（``pool_settings`` 不是受限表；``jobs`` / ``tasks`` 才是）。"""
    if not columns:
        return
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE pool_settings SET {assignments} WHERE pool = ?", (*columns.values(), pool))


def _enqueue(store: JobStore, unit_ref: str, **kwargs: object) -> str | None:
    params: dict[str, object] = {
        "task_id": "t1",
        "pool": "draft",
        "unit_type": "task",
        "unit_ref": unit_ref,
    }
    params.update(kwargs)
    return store.enqueue(**params)  # type: ignore[arg-type]


def _insert_publication(
    conn: sqlite3.Connection,
    pub_id: str,
    *,
    account_id: str = "acc_main",
    status: str = "published",
    published_at: str | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO publications(id, task_id, platform, account_id, video_path, video_sha256, "
        "title, status, published_at, created_at, idempotency_key) "
        "VALUES (?, 't1', 'douyin', ?, 'v.mp4', ?, '标题', ?, ?, COALESCE(?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now')), ?)",
        (pub_id, account_id, "sha-" + pub_id, status, published_at, created_at, "key-" + pub_id),
    )


# ══════════════════════════════════════════════════════════════════════
# ⑤ 幂等入队
# ══════════════════════════════════════════════════════════════════════


def test_enqueue_returns_job_id(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    job = store.get(job_id)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.pool == "draft"
    assert job.max_attempts == 3  # 取自 pool_settings.draft


def test_enqueue_is_idempotent(store: JobStore, connection: sqlite3.Connection) -> None:
    """同一 ``(task_id, pool, unit_type, unit_ref)`` 重复入队 ⇒ ``None`` 且不产生第二条。"""
    first = _enqueue(store, "u1")
    second = _enqueue(store, "u1")
    third = _enqueue(store, "u1")
    assert first is not None
    assert second is None
    assert third is None
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_enqueue_same_ref_different_task_or_pool(store: JobStore, connection: sqlite3.Connection) -> None:
    """幂等键含 ``task_id`` ⇒ 不同任务可有各自的 ``final``（§3.3.9 注释）。"""
    assert _enqueue(store, "final", unit_type="final") is not None
    assert _enqueue(store, "final", task_id="t2", unit_type="final") is not None
    assert _enqueue(store, "final", pool="render", unit_type="final") is not None
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3


def test_enqueue_respects_explicit_max_attempts(store: JobStore) -> None:
    job_id = _enqueue(store, "u1", max_attempts=1)
    assert job_id is not None
    assert store.get(job_id).max_attempts == 1


def test_enqueue_keeps_payload(store: JobStore) -> None:
    job_id = _enqueue(store, "u1", payload={"account_id": "acc_main", "platform": "douyin"})
    assert job_id is not None
    assert store.get(job_id).payload == {"account_id": "acc_main", "platform": "douyin"}


# ══════════════════════════════════════════════════════════════════════
# ① 优先级认领顺序
# ══════════════════════════════════════════════════════════════════════


def test_claim_follows_priority_then_created_at(store: JobStore, connection: sqlite3.Connection) -> None:
    """``priority`` 数值越小越优先；同优先级按 ``created_at`` 先来先服务。"""
    _set_pool(connection, "draft", concurrency=4)
    low = _enqueue(store, "low", priority=300)
    high = _enqueue(store, "high", priority=100)
    mid_first = _enqueue(store, "mid-a", priority=200)
    mid_second = _enqueue(store, "mid-b", priority=200)

    claimed = [store.claim(pool="draft", worker_id="w1", now=T0) for _ in range(4)]
    assert [job.id if job else None for job in claimed] == [high, mid_first, mid_second, low]


def test_claim_returns_none_when_empty(store: JobStore) -> None:
    """空池返回 ``None`` —— 是"没活干"，不是错误。"""
    assert store.claim(pool="draft", worker_id="w1", now=T0) is None


def test_claim_skips_not_before_in_future(store: JobStore, connection: sqlite3.Connection) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    connection.execute(
        "UPDATE jobs SET not_before = ? WHERE id = ?",
        ((T0 + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), job_id),
    )
    assert store.claim(pool="draft", worker_id="w1", now=T0) is None
    assert store.claim(pool="draft", worker_id="w1", now=T0 + timedelta(seconds=61)) is not None


def test_claim_increments_attempts_and_sets_lease(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    claimed = store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == "claimed"
    assert claimed.attempts == 1
    assert claimed.lease_owner == "w1"
    assert claimed.lease_expires_at == "2026-09-13T06:03:00.000Z"
    assert claimed.heartbeat_at == "2026-09-13T06:00:00.000Z"


def test_claim_uses_pool_lease_by_default(store: JobStore) -> None:
    _enqueue(store, "u1", pool="voice", unit_type="sentence")
    claimed = store.claim(pool="voice", worker_id="w1", now=T0)
    assert claimed is not None
    assert claimed.lease_expires_at == "2026-09-13T06:01:30.000Z"  # voice lease = 90s


def test_claim_rejects_blank_worker_id(store: JobStore) -> None:
    with pytest.raises(ValueError):
        store.claim(pool="draft", worker_id="   ", now=T0)


# ══════════════════════════════════════════════════════════════════════
# ② 租约：未过期不回收 / 过期回收 + 退避
# ══════════════════════════════════════════════════════════════════════


def test_reclaim_skips_live_lease(store: JobStore) -> None:
    _enqueue(store, "u1")
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)

    result = store.reclaim_expired(pool="draft", now=T0 + timedelta(seconds=179))
    assert result.requeued == ()
    assert result.dead == ()
    assert store.running_count("draft") == 1


def test_reclaim_requeues_expired_lease_with_backoff(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)

    moment = T0 + timedelta(seconds=181)
    result = store.reclaim_expired(pool="draft", now=moment)

    assert result.requeued == (job_id,)
    assert result.dead == ()
    job = store.get(job_id)
    assert job.status == "pending"
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.attempts == 1  # 认领时已 +1，回收不重复计数
    assert job.error_code == LEASE_EXPIRED_CODE

    # draft: base=5000ms, cap=30000ms ⇒ 首次重试 5s + jitter(0..20%)
    assert job.not_before is not None
    delay = (datetime.fromisoformat(job.not_before.replace("Z", "+00:00")) - moment).total_seconds()
    assert 5.0 <= delay <= 6.0


def test_reclaim_is_idempotent(store: JobStore) -> None:
    _enqueue(store, "u1")
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)
    moment = T0 + timedelta(seconds=181)

    assert len(store.reclaim_expired(pool="draft", now=moment).requeued) == 1
    assert store.reclaim_expired(pool="draft", now=moment).requeued == ()


def test_reclaimed_job_is_not_claimable_before_backoff(store: JobStore) -> None:
    _enqueue(store, "u1")
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)
    moment = T0 + timedelta(seconds=181)
    store.reclaim_expired(pool="draft", now=moment)

    assert store.claim(pool="draft", worker_id="w2", now=moment) is None
    again = store.claim(pool="draft", worker_id="w2", now=moment + timedelta(seconds=10))
    assert again is not None
    assert again.attempts == 2


def test_reclaim_backoff_grows_with_attempts(store: JobStore) -> None:
    """第 2 次失败退避 10s（``5000 × 2^1``），明显大于第 1 次的 5s。"""
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    delays: list[float] = []
    cursor = T0
    for attempt in (1, 2):
        # 上一轮的退避还没到点就认领 ⇒ 必然空手而归（这正是 not_before 的作用）
        assert store.claim(pool="draft", worker_id=f"w{attempt}", lease_sec=180, now=cursor) is not None
        moment = cursor + timedelta(seconds=181)
        store.reclaim_expired(pool="draft", now=moment)
        not_before = store.get(job_id).not_before
        assert not_before is not None
        delays.append((parse_iso(not_before) - moment).total_seconds())
        cursor = parse_iso(not_before)
    assert 5.0 <= delays[0] <= 6.0
    assert 10.0 <= delays[1] <= 12.0


# ══════════════════════════════════════════════════════════════════════
# ③ 依赖 2 级解锁
# ══════════════════════════════════════════════════════════════════════


def test_dependency_two_level_unlock(store: JobStore) -> None:
    """A → B → C 两级依赖：上游 succeeded 才解锁，且**逐级**收敛。"""
    a = _enqueue(store, "a", pool="render", unit_type="scene")
    b = _enqueue(store, "b", pool="render", unit_type="scene", depends_on=[a])
    c = _enqueue(store, "c", pool="render", unit_type="scene", depends_on=[b])
    assert a is not None and b is not None and c is not None

    assert store.get(a).status == "pending"
    assert store.get(b).status == "blocked"
    assert store.get(c).status == "blocked"

    # 上游还没成功 ⇒ 一个都不解锁
    assert store.unlock_dependents(pool="render") == ()
    assert store.get(b).status == "blocked"

    # 第 1 级
    store.claim(pool="render", worker_id="w1", now=T0)
    assert store.succeed(job_id=a, worker_id="w1", now=T0)
    assert store.unlock_dependents(pool="render") == (b,)
    assert store.get(b).status == "pending"
    assert store.get(c).status == "blocked"

    # 第 2 级
    store.claim(pool="render", worker_id="w1", now=T0)
    assert store.succeed(job_id=b, worker_id="w1", now=T0)
    assert store.unlock_dependents(pool="render") == (c,)
    assert store.get(c).status == "pending"


def test_dependency_needs_all_upstreams(store: JobStore) -> None:
    """多个上游：**全部** succeeded 才解锁（少一个都不行）。"""
    a = _enqueue(store, "a", pool="render", unit_type="scene")
    b = _enqueue(store, "b", pool="render", unit_type="scene")
    final = _enqueue(store, "final", pool="render", unit_type="final", depends_on=[a, b])
    assert a is not None and b is not None and final is not None

    store.claim(pool="render", worker_id="w1", now=T0)
    assert store.succeed(job_id=a, worker_id="w1", now=T0)
    assert store.unlock_dependents(pool="render") == ()
    assert store.get(final).status == "blocked"

    store.claim(pool="render", worker_id="w1", now=T0)
    assert store.succeed(job_id=b, worker_id="w1", now=T0)
    assert store.unlock_dependents(pool="render") == (final,)


def test_dependency_not_unlocked_by_dead_upstream(store: JobStore) -> None:
    """上游进死信 ⇒ 下游**永不**解锁（不能"看起来像完成了"就放行）。"""
    a = _enqueue(store, "a", pool="render", unit_type="scene", max_attempts=1)
    b = _enqueue(store, "b", pool="render", unit_type="scene", depends_on=[a])
    assert a is not None and b is not None

    store.claim(pool="render", worker_id="w1", now=T0)
    outcome = store.fail(job_id=a, worker_id="w1", error_code="RENDER_FAILED", error_message="炸了", now=T0)
    assert outcome.is_dead
    assert store.unlock_dependents(pool="render") == ()
    assert store.get(b).status == "blocked"


def test_dependency_already_succeeded_enqueues_pending(store: JobStore) -> None:
    """上游**已经**成功（如 final 依赖已完成的场景）⇒ 直接 ``pending``，不等下一轮 sweeper。"""
    a = _enqueue(store, "a", pool="render", unit_type="scene")
    assert a is not None
    store.claim(pool="render", worker_id="w1", now=T0)
    store.succeed(job_id=a, worker_id="w1", now=T0)

    final = _enqueue(store, "final", pool="render", unit_type="final", depends_on=[a])
    assert final is not None
    assert store.get(final).status == "pending"


def test_unlock_is_scoped_by_pool(store: JobStore) -> None:
    a = _enqueue(store, "a", pool="render", unit_type="scene")
    other = _enqueue(store, "x", pool="voice", unit_type="sentence", depends_on=[a])
    assert a is not None and other is not None
    store.claim(pool="render", worker_id="w1", now=T0)
    store.succeed(job_id=a, worker_id="w1", now=T0)

    assert store.unlock_dependents(pool="draft") == ()
    assert store.get(other).status == "blocked"
    assert store.unlock_dependents(pool="voice") == (other,)


# ══════════════════════════════════════════════════════════════════════
# ④ 死信 + 强制告警
# ══════════════════════════════════════════════════════════════════════


def test_fail_retries_until_max_then_dead(store: JobStore, connection: sqlite3.Connection) -> None:
    """``attempts >= max_attempts`` ⇒ ``dead``（max=2 ⇒ 第 2 次失败即死信）。"""
    job_id = _enqueue(store, "u1", max_attempts=2)
    assert job_id is not None

    store.claim(pool="draft", worker_id="w1", now=T0)
    first = store.fail(job_id=job_id, worker_id="w1", error_code="INTERNAL", error_message="第一次", now=T0)
    assert first.status == "pending"
    assert first.attempts == 1
    assert first.not_before is not None

    resume = datetime.fromisoformat(first.not_before.replace("Z", "+00:00"))
    store.claim(pool="draft", worker_id="w1", now=resume + timedelta(seconds=1))
    second = store.fail(job_id=job_id, worker_id="w1", error_code="INTERNAL", error_message="第二次", now=T0)
    assert second.is_dead
    assert second.attempts == 2

    job = store.get(job_id)
    assert job.status == "dead"
    assert job.finished_at is not None
    assert job.lease_owner is None
    assert job.error_message == "第二次"
    assert store.dead_letters(pool="draft")[0].id == job_id


def test_dead_letter_forces_alert(store: JobStore, connection: sqlite3.Connection) -> None:
    """死信**必须**告警（§03.4.3）—— 静默丢弃是 DoD 5 的直接违反。"""
    job_id = _enqueue(store, "u1", max_attempts=1)
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", now=T0)
    store.fail(job_id=job_id, worker_id="w1", error_code="INTERNAL", error_message="炸了", now=T0)

    row = connection.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "error"
    assert row["source"] == "pool.draft"
    assert row["job_id"] == job_id
    payload = json.loads(row["payload_json"])
    assert payload["code"] == "JOB_DEAD"
    assert payload["severity"] == "error"
    assert "hint" in payload


def test_fail_non_retryable_goes_dead_immediately(store: JobStore) -> None:
    job_id = _enqueue(store, "u1", max_attempts=5)
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", now=T0)
    outcome = store.fail(
        job_id=job_id,
        worker_id="w1",
        error_code="INTERNAL",
        error_message="不可重试",
        retryable=False,
        now=T0,
    )
    assert outcome.is_dead
    assert store.get(job_id).status == "dead"


def test_reclaim_expired_dead_letter_when_attempts_exhausted(
    store: JobStore, connection: sqlite3.Connection
) -> None:
    """租约过期且 ``attempts >= max`` ⇒ 死信 + 告警（不是无限重排）。"""
    job_id = _enqueue(store, "u1", max_attempts=1)
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)

    result = store.reclaim_expired(pool="draft", now=T0 + timedelta(seconds=181))
    assert result.requeued == ()
    assert result.dead == (job_id,)

    job = store.get(job_id)
    assert job.status == "dead"
    assert job.error_code == LEASE_EXPIRED_CODE
    payload = json.loads(
        connection.execute("SELECT payload_json FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
    )
    assert payload["code"] == "JOB_DEAD"


def test_reclaim_without_pool_filter_covers_all_pools(store: JobStore) -> None:
    _enqueue(store, "d", pool="draft")
    _enqueue(store, "v", pool="voice", unit_type="sentence")
    store.claim(pool="draft", worker_id="w1", lease_sec=10, now=T0)
    store.claim(pool="voice", worker_id="w2", lease_sec=10, now=T0)

    result = store.reclaim_expired(now=T0 + timedelta(seconds=11))
    assert len(result.requeued) == 2
    assert result.dead == ()


# ══════════════════════════════════════════════════════════════════════
# ⑥ DDL CHECK：非法状态 / 池 / 单元类型进不去库
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("column", "illegal"),
    [
        ("status", "running"),
        ("status", "done"),
        ("pool", "encode"),
        ("unit_type", "segment"),
    ],
)
def test_jobs_check_rejects_illegal_values(connection: sqlite3.Connection, column: str, illegal: str) -> None:
    """``jobs`` 的 CHECK 是最后一道闸：写错枚举必须当场炸，不能静默落库。"""
    values: dict[str, object] = {
        "id": "j_bad",
        "task_id": "t1",
        "pool": "draft",
        "unit_type": "task",
        "unit_ref": "u1",
    }
    values[column] = illegal
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"INSERT INTO jobs({columns}) VALUES ({placeholders})", tuple(values.values()))
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_jobs_check_rejects_broken_json(connection: sqlite3.Connection) -> None:
    """``depends_on_json`` / ``payload_json`` 必须 ``json_valid``。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO jobs(id, task_id, pool, unit_type, unit_ref, depends_on_json) "
            "VALUES ('j_bad', 't1', 'draft', 'task', 'u1', '{oops')"
        )


def test_enqueue_rejects_unknown_pool(store: JobStore) -> None:
    """未知池名 ⇒ ``DB_SCHEMA_DRIFT``（缺 ``pool_settings`` 行），而不是"悄悄丢进 draft"。"""
    with pytest.raises(StudioError) as excinfo:
        _enqueue(store, "u1", pool="encode")
    assert excinfo.value.code is ErrorCode.DB_SCHEMA_DRIFT


# ══════════════════════════════════════════════════════════════════════
# 池控制：暂停 / 并发上限（守卫在 ``claim`` 内，worker 侧缓存只是省一次读）
# ══════════════════════════════════════════════════════════════════════


def test_paused_pool_does_not_claim(store: JobStore, connection: sqlite3.Connection) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    _set_pool(connection, "draft", paused=1)

    assert store.claim(pool="draft", worker_id="w1", now=T0) is None
    job = store.get(job_id)
    assert job.status == "pending"
    assert job.attempts == 0  # 认领失败不能白扣一次尝试


def test_resuming_pool_claims_again(store: JobStore, connection: sqlite3.Connection) -> None:
    _enqueue(store, "u1")
    _set_pool(connection, "draft", paused=1)
    assert store.claim(pool="draft", worker_id="w1", now=T0) is None
    _set_pool(connection, "draft", paused=0)
    assert store.claim(pool="draft", worker_id="w1", now=T0) is not None


def test_concurrency_limit_blocks_extra_claim(store: JobStore, connection: sqlite3.Connection) -> None:
    """``render`` 池默认 concurrency=1：第二个 worker 拿不到活，而不是两个一起跑爆显存。"""
    _set_pool(connection, "draft", concurrency=1)
    _enqueue(store, "u1")
    _enqueue(store, "u2")

    assert store.claim(pool="draft", worker_id="w1", now=T0) is not None
    assert store.claim(pool="draft", worker_id="w2", now=T0) is None
    assert store.running_count("draft") == 1
    assert len(store.list_jobs(pool="draft", status="pending")) == 1


def test_concurrency_slot_freed_after_succeed(store: JobStore, connection: sqlite3.Connection) -> None:
    _set_pool(connection, "draft", concurrency=1)
    _enqueue(store, "u1")
    _enqueue(store, "u2")

    first = store.claim(pool="draft", worker_id="w1", now=T0)
    assert first is not None
    assert store.succeed(job_id=first.id, worker_id="w1", now=T0)
    assert store.running_count("draft") == 0
    assert store.claim(pool="draft", worker_id="w1", now=T0) is not None


def test_paused_pool_still_reclaims_expired_lease(store: JobStore, connection: sqlite3.Connection) -> None:
    """暂停 = 不再开新工，**不是**把在途单元扔了：租约照常回收。"""
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)

    _set_pool(connection, "draft", paused=1)
    result = store.reclaim_expired(pool="draft", now=T0 + timedelta(seconds=181))
    assert result.requeued == (job_id,)
    assert store.get(job_id).status == "pending"


# ══════════════════════════════════════════════════════════════════════
# publish 池限频守卫（§03.4.4 ⑥ · §3.3.15）
# ══════════════════════════════════════════════════════════════════════


def test_publish_pool_rate_limit_defaults(store: JobStore) -> None:
    """四池参数与 ``config/pools.yaml`` 逐字段一致（T1.3 已由迁移测试锁死）。"""
    assert store.pool_runtime("publish").rate_limit == {
        "daily_limit": 3,
        "min_gap_min": 30,
        "window": "local_day",
    }


def test_rate_limit_allows_when_under_cap(store: JobStore, connection: sqlite3.Connection) -> None:
    _insert_publication(connection, "pub1", published_at="2026-09-13T05:00:00.000Z")
    state = store.rate_limit_state(account_id="acc_main", daily_limit=3, min_gap_min=30, now=T0)
    assert state.allowed
    assert state.used_today == 1
    assert state.reason is None
    assert state.next_allowed_at is None


def test_rate_limit_blocks_at_daily_cap(store: JobStore, connection: sqlite3.Connection) -> None:
    for index in range(3):
        _insert_publication(connection, f"pub{index}", published_at="2026-09-13T04:00:00.000Z")

    state = store.rate_limit_state(account_id="acc_main", daily_limit=3, min_gap_min=30, now=T0)
    assert not state.allowed
    assert state.reason == "daily_limit"
    assert state.used_today == 3
    assert state.next_allowed_at == "2026-09-13T16:00:00.000Z"  # 本地次日 00:00（UTC+8）


def test_rate_limit_blocks_min_gap(store: JobStore, connection: sqlite3.Connection) -> None:
    _insert_publication(connection, "pub1", published_at="2026-09-13T05:50:00.000Z")

    state = store.rate_limit_state(account_id="acc_main", daily_limit=3, min_gap_min=30, now=T0)
    assert not state.allowed
    assert state.reason == "min_gap"
    assert state.used_today == 1
    assert state.next_allowed_at == "2026-09-13T06:20:00.000Z"


def test_rate_limit_uses_local_day_not_utc_day(store: JobStore, connection: sqlite3.Connection) -> None:
    """北京时间 09-13 01:00 发布的（UTC 09-12 17:00）必须算进 09-13 的额度。

    直接用 ``substr(utc, 1, 10)`` 会数出 0 —— 北京时间 08:00 前发的全被算进前一天，
    限频就形同虚设（§03.4.4 ⑥ 的 ``window='local_day'`` 就是为这个设的）。
    """
    _insert_publication(connection, "pub_local_today", published_at="2026-09-12T17:00:00.000Z")
    _insert_publication(connection, "pub_local_yesterday", published_at="2026-09-12T15:00:00.000Z")

    state = store.rate_limit_state(
        account_id="acc_main",
        daily_limit=3,
        min_gap_min=0,
        now=datetime(2026, 9, 13, 2, 0, 0, tzinfo=UTC),  # 本地 09-13 10:00
    )
    assert state.used_today == 1


def test_rate_limit_is_per_account(store: JobStore, connection: sqlite3.Connection) -> None:
    """多账号支持是既定能力（D5）：额度按账号各算各的。"""
    _insert_publication(
        connection, "pub_other", account_id="acc_alt", published_at="2026-09-13T04:00:00.000Z"
    )
    state = store.rate_limit_state(account_id="acc_main", daily_limit=3, min_gap_min=30, now=T0)
    assert state.allowed
    assert state.used_today == 0


def test_rate_limit_ignores_queued_and_failed(store: JobStore, connection: sqlite3.Connection) -> None:
    """``queued`` / ``failed`` 不占额度：额度是"发出去了几条"，不是"排了几条"。"""
    _insert_publication(connection, "pub_q", status="queued", published_at="2026-09-13T04:00:00.000Z")
    _insert_publication(connection, "pub_f", status="failed", published_at="2026-09-13T04:00:00.000Z")
    state = store.rate_limit_state(account_id="acc_main", daily_limit=3, min_gap_min=30, now=T0)
    assert state.allowed
    assert state.used_today == 0


def test_rate_limit_counts_uploading(store: JobStore, connection: sqlite3.Connection) -> None:
    """``uploading`` 也算：正在传的那条已经消耗了平台配额。"""
    _insert_publication(connection, "pub_u", status="uploading", published_at="2026-09-13T04:00:00.000Z")
    state = store.rate_limit_state(account_id="acc_main", daily_limit=1, min_gap_min=0, now=T0)
    assert not state.allowed
    assert state.reason == "daily_limit"


# ══════════════════════════════════════════════════════════════════════
# 死信重投（一键重投 + 审计留痕）
# ══════════════════════════════════════════════════════════════════════


def _kill(store: JobStore, unit_ref: str = "u1") -> str:
    """把一个作业推到死信（``max_attempts=1`` ⇒ 首次失败即死）。"""
    job_id = _enqueue(store, unit_ref, max_attempts=1)
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", now=T0)
    store.fail(job_id=job_id, worker_id="w1", error_code="INTERNAL", error_message="炸了", now=T0)
    assert store.get(job_id).status == "dead"
    return job_id


def test_requeue_dead_resets_and_audits(store: JobStore, connection: sqlite3.Connection) -> None:
    job_id = _kill(store)
    store.requeue_dead(job_id=job_id, actor="user", actor_ref="local", reason="已手动修好素材")

    job = store.get(job_id)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error_code is None
    assert job.finished_at is None
    assert store.claim(pool="draft", worker_id="w1", now=T0) is not None

    row = connection.execute("SELECT * FROM audit_ops ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "job.requeue"
    assert row["target_type"] == "job"
    assert row["target_id"] == job_id
    assert row["actor"] == "user"
    assert row["result"] == "ok"
    assert row["reason"] == "已手动修好素材"
    assert json.loads(row["before_json"])["status"] == "dead"
    assert json.loads(row["after_json"]) == {"status": "pending", "attempts": 0}


def test_requeue_dead_rejects_live_job(store: JobStore) -> None:
    _enqueue(store, "u1")
    job_id = store.list_jobs(pool="draft")[0].id
    with pytest.raises(QueueError) as excinfo:
        store.requeue_dead(job_id=job_id)
    assert excinfo.value.code is ErrorCode.JOB_DEAD


def test_requeue_dead_rejects_missing_job(store: JobStore) -> None:
    with pytest.raises(QueueError) as excinfo:
        store.requeue_dead(job_id="j_missing")
    assert excinfo.value.code is ErrorCode.JOB_NOT_FOUND


def test_dead_letter_survives_requeue_cycle(store: JobStore) -> None:
    """重投后再次失败 ⇒ 仍是死信（"重投过"不构成免除告警的理由）。"""
    job_id = _kill(store)
    store.requeue_dead(job_id=job_id)
    store.claim(pool="draft", worker_id="w2", now=T0)

    outcome = store.fail(job_id=job_id, worker_id="w2", error_code="INTERNAL", error_message="又炸", now=T0)
    assert outcome.is_dead
    assert store.dead_letters(pool="draft")[0].id == job_id


# ══════════════════════════════════════════════════════════════════════
# 租约守卫：丢了租约的 worker 不许写结果
# ══════════════════════════════════════════════════════════════════════


def test_renew_extends_lease_only_for_owner(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", lease_sec=180, now=T0)

    assert store.renew(job_id=job_id, worker_id="w1", lease_sec=180, now=T0 + timedelta(seconds=60))
    job = store.get(job_id)
    assert job.lease_expires_at == "2026-09-13T06:04:00.000Z"
    assert job.heartbeat_at == "2026-09-13T06:01:00.000Z"

    assert not store.renew(job_id=job_id, worker_id="w2", lease_sec=180, now=T0)
    assert not store.renew(job_id="j_missing", worker_id="w1", lease_sec=180, now=T0)


def test_succeed_requires_lease(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", now=T0)

    assert not store.succeed(job_id=job_id, worker_id="w2", now=T0)
    assert store.get(job_id).status == "claimed"

    assert store.succeed(job_id=job_id, worker_id="w1", result={"video": "out.mp4"}, now=T0)
    job = store.get(job_id)
    assert job.status == "succeeded"
    assert job.result == {"video": "out.mp4"}
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.finished_at == "2026-09-13T06:00:00.000Z"
    assert not store.succeed(job_id=job_id, worker_id="w1", now=T0)  # 不能重复收尾


def test_fail_requires_lease(store: JobStore) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    store.claim(pool="draft", worker_id="w1", now=T0)

    with pytest.raises(QueueError) as excinfo:
        store.fail(job_id=job_id, worker_id="w2", error_code="INTERNAL", error_message="越权")
    assert excinfo.value.code is ErrorCode.JOB_LEASE_LOST
    assert store.get(job_id).status == "claimed"


def test_fail_missing_job_raises(store: JobStore) -> None:
    with pytest.raises(QueueError) as excinfo:
        store.fail(job_id="j_missing", worker_id="w1", error_code="INTERNAL", error_message="x")
    assert excinfo.value.code is ErrorCode.JOB_NOT_FOUND


# ══════════════════════════════════════════════════════════════════════
# 统计与读接口
# ══════════════════════════════════════════════════════════════════════


def test_stats_counts_by_status(store: JobStore, connection: sqlite3.Connection) -> None:
    _set_pool(connection, "draft", concurrency=4)
    for index in range(4):
        _enqueue(store, f"u{index}")

    done = store.claim(pool="draft", worker_id="w1", now=T0)
    assert done is not None
    assert store.succeed(job_id=done.id, worker_id="w1", now=T0)

    running = store.claim(pool="draft", worker_id="w1", now=T0)
    assert running is not None  # 留在 claimed

    doomed = store.claim(pool="draft", worker_id="w1", now=T0)
    assert doomed is not None
    store.fail(
        job_id=doomed.id,
        worker_id="w1",
        error_code="INTERNAL",
        error_message="炸",
        retryable=False,
        now=T0,
    )

    stats = store.stats(pool="draft", now=T0)
    assert stats.succeeded == 1
    assert stats.claimed == 1
    assert stats.running == 1
    assert stats.dead == 1
    assert stats.pending == 1
    assert stats.blocked == 0
    assert stats.canceled == 0
    assert stats.concurrency == 4
    assert stats.paused is False


def test_stats_oldest_pending_age(store: JobStore, connection: sqlite3.Connection) -> None:
    job_id = _enqueue(store, "u1")
    assert job_id is not None
    connection.execute("UPDATE jobs SET created_at = ? WHERE id = ?", ("2026-09-13T05:58:00.000Z", job_id))
    assert store.stats(pool="draft", now=T0).oldest_pending_age_sec == 120


def test_stats_oldest_pending_age_is_none_when_empty(store: JobStore) -> None:
    assert store.stats(pool="draft", now=T0).oldest_pending_age_sec is None


def test_stats_includes_worker_heartbeats(store: JobStore, connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO worker_heartbeats(worker_id, pool, status, last_seen_at, gpu_mem_mb) "
        "VALUES ('voice#1@123', 'voice', 'busy', ?, 4096)",
        (format_iso(T0),),
    )
    stats = store.stats(pool="voice", now=T0)
    assert len(stats.workers) == 1
    assert stats.to_dict()["workers"] == [
        {
            "worker_id": "voice#1@123",
            "pool": "voice",
            "status": "busy",
            "current_job_id": None,
            "gpu_mem_mb": 4096,
        }
    ]


def test_get_missing_job_raises(store: JobStore) -> None:
    with pytest.raises(QueueError) as excinfo:
        store.get("j_missing")
    assert excinfo.value.code is ErrorCode.JOB_NOT_FOUND


def test_pool_runtime_unknown_pool_raises(store: JobStore) -> None:
    with pytest.raises(StudioError) as excinfo:
        store.pool_runtime("encode")
    assert excinfo.value.code is ErrorCode.DB_SCHEMA_DRIFT


def test_list_jobs_filters(store: JobStore) -> None:
    _enqueue(store, "a")
    _enqueue(store, "b", pool="voice", unit_type="sentence")
    _enqueue(store, "c", task_id="t2")

    assert len(store.list_jobs()) == 3
    assert len(store.list_jobs(pool="voice")) == 1
    assert len(store.list_jobs(task_id="t2")) == 1
    assert len(store.list_jobs(pool="draft", status="pending")) == 2  # a + c
    assert len(store.list_jobs(task_id="t1", status="pending")) == 2
    assert store.list_jobs(limit=2) == store.list_jobs()[:2]


def test_list_jobs_rejects_nonpositive_limit(store: JobStore) -> None:
    """§03.4.6 规则 4：**永远带 LIMIT**，所以 limit 必须正数。"""
    with pytest.raises(ValueError):
        store.list_jobs(limit=0)
