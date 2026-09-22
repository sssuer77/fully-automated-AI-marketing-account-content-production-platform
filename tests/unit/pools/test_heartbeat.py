"""心跳层单元测试（T1.6 · §04.5.1）。

覆盖：worker_id 格式与解析、进程画像、RSS 泄漏判定、``worker_heartbeats`` 读写、
判死与留痕（幂等）、优雅退出摘行、死行 GC。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from studio.core.clock import format_iso, utc_now
from studio.db.engine import connect
from studio.db.migrate import migrate
from studio.pools.heartbeat import (
    HEARTBEAT_TIMEOUT_SEC,
    WORKER_DEAD_CODE,
    HeartbeatStore,
    ProcessStats,
    RssLeakWatch,
    WorkerIdentity,
)

T0 = datetime(2026, 9, 13, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "studio.db"
    migrate(path)
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(connection: sqlite3.Connection) -> HeartbeatStore:
    return HeartbeatStore(connection)


def _beat(
    store: HeartbeatStore,
    worker_id: str = "draft#1@4242",
    *,
    pool: str = "draft",
    status: str = "idle",
    at: datetime = T0,
    **extra: object,
) -> None:
    store.upsert(worker_id=worker_id, pool=pool, status=status, now=at, **extra)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
# worker_id 格式（§04.5.1：<pool>#<slot>@<pid>）
# ══════════════════════════════════════════════════════════════════════


def test_identity_worker_id_format() -> None:
    assert WorkerIdentity(pool="voice", slot=2, pid=20344).worker_id == "voice#2@20344"


def test_identity_round_trip() -> None:
    identity = WorkerIdentity(pool="render", slot=1, pid=777)
    assert WorkerIdentity.parse(identity.worker_id) == identity


def test_identity_current_uses_this_process() -> None:
    identity = WorkerIdentity.current("publish", 3)
    assert identity.pid == os.getpid()
    assert identity.worker_id.startswith("publish#3@")


@pytest.mark.parametrize("bad", ["draft", "draft#1", "draft#1@", "#1@2", "draft#x@2", "draft#1@y"])
def test_identity_parse_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        WorkerIdentity.parse(bad)


# ══════════════════════════════════════════════════════════════════════
# 进程画像与 RSS 泄漏判定
# ══════════════════════════════════════════════════════════════════════


def test_process_stats_samples_current_process() -> None:
    stats = ProcessStats.sample()
    assert stats.rss_mb > 0
    assert stats.cpu_percent >= 0.0


def test_process_stats_tolerates_unknown_pid() -> None:
    """采样失败必须给 0 而不是抛 —— 心跳线程不能因为读不到资源就死。"""
    assert ProcessStats.sample(pid=-1) == ProcessStats()


def test_rss_watch_sets_baseline_on_first_sample() -> None:
    watch = RssLeakWatch()
    assert watch.observe(300) is False
    assert watch.baseline_mb == 300


def test_rss_watch_ignores_zero_sample() -> None:
    watch = RssLeakWatch()
    watch.observe(300)
    assert watch.observe(0) is False
    assert watch.strikes == 0


def test_rss_watch_needs_sustained_growth() -> None:
    """单拍尖峰不算泄漏；连续 3 拍才告警。"""
    watch = RssLeakWatch(growth_ratio=0.5, min_growth_mb=256, strikes_to_warn=3)
    watch.observe(300)
    assert watch.observe(900) is False
    assert watch.observe(900) is False
    assert watch.observe(900) is True


def test_rss_watch_resets_on_drop() -> None:
    watch = RssLeakWatch(growth_ratio=0.5, min_growth_mb=256, strikes_to_warn=3)
    watch.observe(300)
    watch.observe(900)
    watch.observe(900)
    assert watch.observe(320) is False
    assert watch.strikes == 0
    assert watch.baseline_mb == 300


def test_rss_watch_raises_baseline_after_warning() -> None:
    """告警后基线抬到当前值 ⇒ 不会每 5s 刷一条同样的告警。"""
    watch = RssLeakWatch(growth_ratio=0.5, min_growth_mb=256, strikes_to_warn=1)
    watch.observe(300)
    assert watch.observe(900) is True
    assert watch.baseline_mb == 900
    assert watch.observe(900) is False


# ══════════════════════════════════════════════════════════════════════
# worker_heartbeats 读写
# ══════════════════════════════════════════════════════════════════════


def test_upsert_inserts_row(store: HeartbeatStore) -> None:
    _beat(store, status="busy", current_job_id="j1", pid=4242, rss_mb=512, gpu_mem_mb=1024)

    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.pool == "draft"
    assert beat.status == "busy"
    assert beat.pid == 4242
    assert beat.current_job_id == "j1"
    assert beat.rss_mb == 512
    assert beat.gpu_mem_mb == 1024
    assert beat.last_seen_at == "2026-09-13T06:00:00.000Z"


def test_upsert_updates_existing_row(store: HeartbeatStore, connection: sqlite3.Connection) -> None:
    _beat(store, status="idle")
    _beat(store, status="busy", current_job_id="j1", at=T0 + timedelta(seconds=5))

    assert connection.execute("SELECT count(*) FROM worker_heartbeats").fetchone()[0] == 1
    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.status == "busy"
    assert beat.current_job_id == "j1"
    assert beat.last_seen_at == "2026-09-13T06:00:05.000Z"


def test_upsert_keeps_earliest_started_at(store: HeartbeatStore) -> None:
    """``started_at`` 是判断重启频率的依据 ⇒ 后续心跳不得把它改晚。"""
    _beat(store, started_at="2026-09-13T05:00:00.000Z")
    _beat(store, started_at="2026-09-13T06:00:00.000Z", at=T0 + timedelta(seconds=5))

    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.started_at == "2026-09-13T05:00:00.000Z"


def test_upsert_fills_started_at_when_missing(store: HeartbeatStore) -> None:
    _beat(store, started_at=None)
    _beat(store, started_at="2026-09-13T06:00:00.000Z", at=T0 + timedelta(seconds=5))

    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.started_at == "2026-09-13T06:00:00.000Z"


def test_upsert_rejects_illegal_status(store: HeartbeatStore) -> None:
    """状态必须与 DDL CHECK 一致 —— 在 Python 侧就拦住，别等 SQLite 报错。"""
    with pytest.raises(ValueError, match="非法 worker 状态"):
        _beat(store, status="running")


def test_upsert_fills_hostname(store: HeartbeatStore, connection: sqlite3.Connection) -> None:
    _beat(store)
    hostname = connection.execute("SELECT hostname FROM worker_heartbeats").fetchone()[0]
    assert hostname


def test_list_workers_filters_and_sorts(store: HeartbeatStore) -> None:
    _beat(store, "voice#1@1", pool="voice")
    _beat(store, "draft#2@2", pool="draft")
    _beat(store, "draft#1@3", pool="draft")

    assert [beat.worker_id for beat in store.list_workers(pool="draft")] == [
        "draft#1@3",
        "draft#2@2",
    ]
    assert len(store.list_workers()) == 3


def test_read_missing_worker_returns_none(store: HeartbeatStore) -> None:
    assert store.read("nobody#1@0") is None


# ══════════════════════════════════════════════════════════════════════
# 判活：stale / mark_dead / reap_stale / forget / purge
# ══════════════════════════════════════════════════════════════════════


def test_stale_excludes_fresh_heartbeat(store: HeartbeatStore) -> None:
    _beat(store)
    assert store.stale(now=T0 + timedelta(seconds=14)) == ()


def test_stale_includes_timeout_heartbeat(store: HeartbeatStore) -> None:
    _beat(store)
    stale = store.stale(now=T0 + timedelta(seconds=16))
    assert [beat.worker_id for beat in stale] == ["draft#1@4242"]


def test_stale_threshold_is_exactly_15s_by_default() -> None:
    assert HEARTBEAT_TIMEOUT_SEC == 15.0


def test_stale_excludes_already_dead(store: HeartbeatStore) -> None:
    """已经是 dead 的不重复算 —— 否则 supervisor 每轮都会"重新发现"它。"""
    _beat(store)
    store.mark_dead(worker_id="draft#1@4242")
    assert store.stale(now=T0 + timedelta(seconds=60)) == ()


def test_mark_dead_preserves_last_seen_and_job(store: HeartbeatStore) -> None:
    _beat(store, status="busy", current_job_id="j1")
    assert store.mark_dead(worker_id="draft#1@4242") is True

    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.status == "dead"
    assert beat.last_seen_at == "2026-09-13T06:00:00.000Z"  # 最后一次真实接触
    assert beat.current_job_id == "j1"  # 排查"死在哪一步"靠它


def test_mark_dead_is_idempotent(store: HeartbeatStore) -> None:
    _beat(store)
    assert store.mark_dead(worker_id="draft#1@4242") is True
    assert store.mark_dead(worker_id="draft#1@4242") is False
    assert store.mark_dead(worker_id="nobody#1@0") is False


def test_reap_stale_marks_and_logs(store: HeartbeatStore, connection: sqlite3.Connection) -> None:
    _beat(store, status="busy", current_job_id="j1")
    reaped = store.reap_stale(now=T0 + timedelta(seconds=20))

    assert reaped == ("draft#1@4242",)
    assert store.read("draft#1@4242").status == "dead"  # type: ignore[union-attr]

    row = connection.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "error"
    assert row["source"] == "pool.draft"
    assert row["job_id"] == "j1"
    assert row["worker_id"] == "draft#1@4242"
    payload = json.loads(row["payload_json"])
    assert payload["code"] == WORKER_DEAD_CODE
    assert payload["severity"] == "error"
    assert payload["silent_sec"] == 20.0
    assert "hint" in payload


def test_reap_stale_is_idempotent(store: HeartbeatStore, connection: sqlite3.Connection) -> None:
    _beat(store)
    assert len(store.reap_stale(now=T0 + timedelta(seconds=20))) == 1
    assert store.reap_stale(now=T0 + timedelta(seconds=21)) == ()
    assert connection.execute("SELECT count(*) FROM system_logs").fetchone()[0] == 1


def test_reap_stale_scoped_by_pool(store: HeartbeatStore) -> None:
    _beat(store, "draft#1@1", pool="draft")
    _beat(store, "voice#1@2", pool="voice")
    assert store.reap_stale(now=T0 + timedelta(seconds=20), pool="voice") == ("voice#1@2",)
    assert store.read("draft#1@1").status == "idle"  # type: ignore[union-attr]


def test_forget_removes_row(store: HeartbeatStore) -> None:
    """优雅退出摘行 ⇒ "行存在 ⇔ 进程应当在跑"的不变式成立（不会误判为猝死）。"""
    _beat(store)
    assert store.forget("draft#1@4242") is True
    assert store.read("draft#1@4242") is None
    assert store.forget("draft#1@4242") is False


def test_forget_orphans_removes_rows_of_dead_pids(store: HeartbeatStore) -> None:
    """强杀留下的尸体行：pid 不在了就是垃圾（"行存在 ⇔ 进程应当在跑"）。"""
    _beat(store, "draft#1@100", pid=100)
    _beat(store, "draft#1@200", pid=200)
    _beat(store, "voice#1@300", pool="voice", pid=300)

    gone = store.forget_orphans(alive=lambda pid: pid == 300)

    assert gone == ("draft#1@100", "draft#1@200")
    assert store.read("draft#1@100") is None
    assert store.read("voice#1@300") is not None


def test_forget_orphans_keeps_rows_without_pid(store: HeartbeatStore) -> None:
    """没有 pid 的行核对不了 ⇒ 不猜、不动它（留给 purge 按保留期处理）。"""
    _beat(store, "draft#1@1")

    assert store.forget_orphans(alive=lambda pid: False) == ()
    assert store.read("draft#1@1") is not None


def test_forget_orphans_treats_broken_probe_as_alive(store: HeartbeatStore) -> None:
    """探针自己炸了 ⇒ 当作活着：宁可多留一行，不可误删活行。"""
    _beat(store, "draft#1@7", pid=7)

    def boom(pid: int) -> bool:
        raise ValueError(pid)

    assert store.forget_orphans(alive=boom) == ()
    assert store.read("draft#1@7") is not None


def test_forget_orphans_is_quiet_when_all_alive(store: HeartbeatStore) -> None:
    """全活 / 空表 ⇒ 空元组（调用方靠它判断"要不要说一句"）。"""
    assert store.forget_orphans(alive=lambda pid: True) == ()
    _beat(store, "draft#1@9", pid=9)
    assert store.forget_orphans(alive=lambda pid: True) == ()


def test_forget_orphans_clears_reused_pid(store: HeartbeatStore) -> None:
    """pid 被复用（别人占了这号）也算尸体 —— 否则这条"疑似猝死"永远清不掉。

    拿本进程自己的 pid 当样本：它一定活着，而"进程创建时间晚于 ``started_at``"
    正是复用的定义。
    """
    pid = os.getpid()
    worker_id = f"draft#1@{pid}"
    _beat(store, worker_id, pid=pid, started_at=format_iso(T0))

    assert store.forget_orphans() == (worker_id,)
    assert store.read(worker_id) is None


def test_forget_orphans_keeps_live_worker_row(store: HeartbeatStore) -> None:
    """真活行必须留下：pid 在 **且** 进程创建时间不晚于 ``started_at``。"""
    pid = os.getpid()
    worker_id = f"draft#1@{pid}"
    _beat(store, worker_id, pid=pid, started_at=format_iso(utc_now()))

    assert store.forget_orphans() == ()
    assert store.read(worker_id) is not None


def test_purge_removes_only_old_dead_rows(store: HeartbeatStore) -> None:
    _beat(store, "old#1@1", pool="voice")
    store.mark_dead(worker_id="old#1@1")
    _beat(store, "fresh#1@2", pool="voice", at=T0 + timedelta(hours=30))
    store.mark_dead(worker_id="fresh#1@2")
    _beat(store, "alive#1@3", pool="voice", at=T0)

    removed = store.purge(now=T0 + timedelta(hours=30), keep_sec=24 * 3600)
    assert removed == 1
    assert store.read("old#1@1") is None
    assert store.read("fresh#1@2") is not None
    assert store.read("alive#1@3") is not None


def test_format_iso_round_trip_for_last_seen(store: HeartbeatStore) -> None:
    """``last_seen_at`` 必须能直接字典序比较（毫秒 UTC）⇒ 与判活 SQL 口径一致。"""
    _beat(store)
    beat = store.read("draft#1@4242")
    assert beat is not None
    assert beat.last_seen_at == format_iso(T0)
    assert len(beat.last_seen_at) == 24
