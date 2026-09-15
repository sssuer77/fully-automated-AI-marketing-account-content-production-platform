"""``gc/rows.py`` 单测：按保留期删 DB 旧行（T4.12 · §03.7.5）。

三条"必须做到"
--------------
① **只删过期的**：边界内的行一行不许少（"跑一次 GC 丢一周日志"是最难查的一类事故）；
② **分批**：单条 ``DELETE`` 影响 ≤ ``BATCH_ROWS`` 行（长事务会挡住四池的认领）；
③ **``audit_ops`` 永久**：留痕表没有对应规则，任何一轮 GC 都不该碰它。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from studio.core.clock import format_iso
from studio.db.engine import connect
from studio.db.migrate import migrate
from studio.gc.policy import RetentionPolicy
from studio.gc.rows import BATCH_ROWS, RowRule, default_row_rules, gc_rows

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "studio.db"
    migrate(path)
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _stamp(days: float) -> str:
    return format_iso(NOW - timedelta(days=days))


def _log(connection: sqlite3.Connection, *, level: str = "info", days: float = 0.0) -> None:
    moment = _stamp(days)
    connection.execute(
        "INSERT INTO system_logs(level, source, message, ts, created_at) VALUES (?, 'test', 'x', ?, ?)",
        (level, moment, moment),
    )


def _llm(connection: sqlite3.Connection, *, days: float) -> None:
    connection.execute(
        "INSERT INTO llm_calls(id, agent, engine, model, status, created_at) "
        "VALUES (?, 'writer', 'oi_compatible', 'm', 'ok', ?)",
        (f"call-{days}", _stamp(days)),
    )


def _task(connection: sqlite3.Connection, task_id: str) -> None:
    connection.execute("INSERT INTO tasks(id, title) VALUES (?, 'x')", (task_id,))


def _event(connection: sqlite3.Connection, task_id: str, *, days: float) -> None:
    connection.execute(
        "INSERT INTO task_events(task_id, to_status, actor, created_at) VALUES (?, 'pending', 'system', ?)",
        (task_id, _stamp(days)),
    )


def _audit(connection: sqlite3.Connection, *, days: float) -> None:
    connection.execute(
        "INSERT INTO audit_ops(at, actor, action, target_type) VALUES (?, 'user', 'task.approve', 'task')",
        (_stamp(days),),
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


# ══════════════════════════════════════════════════════════════════════
# 规则表
# ══════════════════════════════════════════════════════════════════════


def test_rules_cover_the_three_rolling_tables() -> None:
    rules = default_row_rules(RetentionPolicy())

    assert [item.table for item in rules] == ["system_logs", "system_logs", "llm_calls", "task_events"]
    assert rules[0].days == 7  # debug 先清
    assert rules[1].days == 30
    assert (rules[2].days, rules[3].days) == (90, 90)


def test_no_rule_touches_audit_ops() -> None:
    """★ 合规留痕**永久**：没有规则 = 不可能被"忘了加一条"带进来。"""
    assert all(item.table != "audit_ops" for item in default_row_rules(RetentionPolicy()))


def test_cutoff_is_iso_milliseconds() -> None:
    rule = RowRule(label="x", table="system_logs", time_column="ts", days=30)

    assert rule.cutoff(NOW) == "2026-08-15T04:00:00.000Z"
    assert rule.predicate() == "ts < ?"


def test_predicate_appends_the_extra_filter() -> None:
    rule = RowRule(label="x", table="system_logs", time_column="ts", days=7, where="level = 'debug'")

    assert rule.predicate() == "ts < ? AND level = 'debug'"


# ══════════════════════════════════════════════════════════════════════
# 回收
# ══════════════════════════════════════════════════════════════════════


def test_only_expired_rows_go(connection: sqlite3.Connection) -> None:
    """★ 边界内的行一行不许少。"""
    _log(connection, days=40)
    _log(connection, days=29)
    _log(connection, days=0)

    results = gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW)

    assert {item.label: item.deleted for item in results}["系统日志"] == 1
    assert _count(connection, "system_logs") == 2


def test_debug_rows_use_the_shorter_window(connection: sqlite3.Connection) -> None:
    _log(connection, level="debug", days=10)
    _log(connection, level="info", days=10)

    results = gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW)
    by_label = {item.label: item.deleted for item in results}

    assert by_label["debug 日志"] == 1
    assert by_label["系统日志"] == 0
    assert _count(connection, "system_logs") == 1


def test_llm_and_events_follow_their_own_windows(connection: sqlite3.Connection) -> None:
    _llm(connection, days=100)
    _llm(connection, days=10)
    _task(connection, "t1")
    _event(connection, "t1", days=100)
    _event(connection, "t1", days=10)

    results = gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW)
    by_label = {item.label: item.deleted for item in results}

    assert by_label["LLM 调用流水"] == 1
    assert by_label["任务事件"] == 1
    assert _count(connection, "llm_calls") == 1
    assert _count(connection, "task_events") == 1


def test_audit_ops_survives_every_rule(connection: sqlite3.Connection) -> None:
    """★ 留痕表跑多少轮都还在（§03.7.5「永久」）。"""
    _audit(connection, days=3650)

    gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW)

    assert _count(connection, "audit_ops") == 1


def test_batches_cap_the_transaction(connection: sqlite3.Connection) -> None:
    """★ 单条 ``DELETE`` 影响 ≤ batch_size 行。"""
    for _ in range(12):
        _log(connection, days=40)

    results = gc_rows(
        connection,
        (RowRule(label="系统日志", table="system_logs", time_column="ts", days=30),),
        now=NOW,
        batch_size=5,
    )

    assert results[0].deleted == 12
    assert results[0].batches == 3
    assert _count(connection, "system_logs") == 0


def test_default_batch_size_is_the_spec_number() -> None:
    assert BATCH_ROWS == 5000


def test_dry_run_counts_without_deleting(connection: sqlite3.Connection) -> None:
    for _ in range(3):
        _log(connection, days=40)

    results = gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW, dry_run=True)

    assert {item.label: item.deleted for item in results}["系统日志"] == 3
    assert all(item.dry_run for item in results)
    assert _count(connection, "system_logs") == 3


def test_result_serializes(connection: sqlite3.Connection) -> None:
    _log(connection, days=40)

    results = gc_rows(connection, default_row_rules(RetentionPolicy()), now=NOW)
    payload = results[1].to_dict()

    assert payload["table"] == "system_logs"
    assert payload["deleted"] == 1
    assert payload["days"] == 30
