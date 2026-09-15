"""迁移集成测试（T1.3 验收）：schema 语义 + seed 防漂移 + doctor 门禁。

与 ``tests/unit/db/`` 的分工：这里**真的把库建起来**，验证"29 表能不能用"
（约束、触发器、级联、部分索引），而不只是"建出来了"。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from studio.core.paths import StudioPaths
from studio.db.engine import MIGRATIONS_DIR, connect, iter_statements, transaction
from studio.db.migrate import check, discover, doctor_check, migrate

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = 30
EXPECTED_INDEXES = 61
EXPECTED_TRIGGERS = 6

#: 迁移文件的**期望清单**（§03.7.1）。新增一个迁移 ⇒ 只改这一行（版本号、文件名、
#: 条数三处断言都由它推导）—— 散着写就会出现"加了文件却漏改某一条计数"。
EXPECTED_MIGRATIONS = (
    "0001_init",
    "0002_topics",
    "0003_templates",
    "0004_publish",
    "0005_schedule_report",
    "0006_seed",
    "0007_pool_autodegrade",
    "0008_observability",
    "0009_assets",
)
EXPECTED_VERSIONS = tuple(label.split("_", 1)[0] for label in EXPECTED_MIGRATIONS)
EXPECTED_MIGRATION_NAMES = tuple(label.split("_", 1)[1] for label in EXPECTED_MIGRATIONS)

#: 16 态状态机（§3.3.6 · C 类裁决）
TASK_STATUSES = (
    "pending",
    "drafting",
    "reviewing",
    "editing",
    "awaiting_approval",
    "queued_voice",
    "voicing",
    "queued_render",
    "rendering",
    "completed",
    "publishing",
    "published",
    "failed",
    "manual_pool",
    "discarded",
    "canceled",
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "studio.db"
    migrate(path)
    return path


@pytest.fixture
def connection(db: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _count(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _insert_task(conn: sqlite3.Connection, task_id: str = "t1", **overrides: object) -> None:
    columns: dict[str, object] = {"id": task_id, "title": "测试任务"}
    columns.update(overrides)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO tasks({','.join(columns)}) VALUES ({placeholders})",
        tuple(columns.values()),
    )


# ══════════════════════════════════════════════════════════════════════
# 结构验收
# ══════════════════════════════════════════════════════════════════════


def test_table_index_trigger_counts(connection: sqlite3.Connection) -> None:
    assert (
        _count(
            connection, "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        == EXPECTED_TABLES
    )
    assert (
        _count(
            connection, "SELECT count(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        == EXPECTED_INDEXES
    )
    assert _count(connection, "SELECT count(*) FROM sqlite_master WHERE type='trigger'") == (
        EXPECTED_TRIGGERS
    )


def test_journal_mode_is_wal_and_persisted(tmp_path: Path) -> None:
    """WAL 是库级持久设置：重开连接仍是 wal（§03.7.2 规则 6）。"""
    path = tmp_path / "studio.db"
    migrate(path)
    connection = connect(path, apply=False)
    try:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        connection.close()


def test_foreign_keys_enforced_per_connection(db: Path) -> None:
    connection = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, task_id, pool, unit_type, unit_ref) "
                "VALUES ('j1','ghost','render','final','final')"
            )
    finally:
        connection.close()


def test_migration_file_names_are_frozen() -> None:
    """迁移文件名与版本号一旦发布即冻结（改 = 拒绝启动）。"""
    assert [f.label for f in discover()] == list(EXPECTED_MIGRATIONS)


# ══════════════════════════════════════════════════════════════════════
# seed 防漂移（T1.3 施工裁定：以 config/pools.yaml 为准）
# ══════════════════════════════════════════════════════════════════════


def _pools_yaml() -> dict[str, dict[str, object]]:
    raw: Any = yaml.safe_load((REPO_ROOT / "config" / "pools.yaml").read_text(encoding="utf-8"))
    return cast("dict[str, dict[str, object]]", raw["pools"])


def test_pool_settings_matches_pools_yaml(connection: sqlite3.Connection) -> None:
    """DB 初值必须与 ``config/pools.yaml`` 逐字段相等 —— 改了 YAML 忘了改 seed 立刻红。"""
    expected = _pools_yaml()
    rows = connection.execute("SELECT * FROM pool_settings").fetchall()
    assert len(rows) == len(expected) == 4

    for row in rows:
        pool = row["pool"]
        want = expected[pool]
        assert row["concurrency"] == want["concurrency"], pool
        assert row["lease_sec"] == want["lease_sec"], pool
        assert row["max_attempts"] == want["max_attempts"], pool
        assert row["backoff_base_ms"] == want["backoff_base_ms"], pool
        assert row["backoff_max_ms"] == want["backoff_max_ms"], pool
        assert row["paused"] == 0, pool


def test_pool_settings_render_lease_is_final_not_scene(connection: sqlite3.Connection) -> None:
    """§1.4.4 的 render 租约是「300s(scene) / 600s(final)」；一期只产 final ⇒ 600。"""
    row = connection.execute("SELECT lease_sec FROM pool_settings WHERE pool='render'").fetchone()
    assert row["lease_sec"] == 600


def test_publish_rate_limit_seeded(connection: sqlite3.Connection) -> None:
    row = connection.execute("SELECT rate_limit_json FROM pool_settings WHERE pool='publish'").fetchone()
    payload = json.loads(row["rate_limit_json"])
    assert payload == {"daily_limit": 3, "min_gap_min": 30, "window": "local_day"}

    publish_yaml = yaml.safe_load((REPO_ROOT / "config" / "publish.yaml").read_text(encoding="utf-8"))
    account = next(a for a in publish_yaml["accounts"] if a["account_id"] == "acc_main")
    assert payload["daily_limit"] == account["daily_limit"]
    assert payload["min_gap_min"] == account["min_gap_min"]


def test_report_schedules_defaults(connection: sqlite3.Connection) -> None:
    """Q15：周期可编辑 —— 出厂 weekly/monthly 启用、daily 停用（§3.3.20）。"""
    rows = {r["period"]: r for r in connection.execute("SELECT * FROM report_schedules")}
    assert set(rows) == {"daily", "weekly", "monthly"}

    weekly = rows["weekly"]
    assert (weekly["weekday"], weekly["at_time"], weekly["enabled"], weekly["is_builtin"]) == (
        0,
        "09:00",
        1,
        1,
    )
    monthly = rows["monthly"]
    assert (monthly["day_of_month"], monthly["at_time"], monthly["enabled"]) == (1, "09:00", 1)
    assert rows["daily"]["enabled"] == 0
    # T5.7 必须把 NULL 当"未排期 ⇒ 立刻计算"，不能用 `next_run_at <= now`
    assert all(r["next_run_at"] is None for r in rows.values())


def test_report_schedule_requires_weekday_or_day(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO report_schedules(id, period, at_time) VALUES ('bad','weekly','09:00')"
        )


def test_report_schedule_single_enabled_per_period(connection: sqlite3.Connection) -> None:
    """每个周期最多 1 个启用中的计划（部分唯一索引）。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO report_schedules(id, period, weekday, at_time, enabled) "
            "VALUES ('dup','weekly',2,'10:00',1)"
        )


def test_seed_is_idempotent_on_rerun(tmp_path: Path) -> None:
    """约束 3：seed 用 ON CONFLICT DO NOTHING ⇒ 重跑不翻倍。

    这里**直接重放 seed 的 SQL**，而不是"删掉 0006 的登记再 ``migrate()``"：
    后者在 0007 落地之后会被乱序守卫拦下（"0006 的版本号小于已应用的最大版本
    0007"）—— 而那条守卫是对的，不该为测试让路。重放 SQL 反而更贴题：
    要验的是 seed 文件自身幂等，与迁移账本无关。
    """
    path = tmp_path / "studio.db"
    migrate(path)
    seed = (MIGRATIONS_DIR / "0006_seed.sql").read_text(encoding="utf-8")
    connection = connect(path, apply=False)
    try:
        with transaction(connection, immediate=True):
            for statement in iter_statements(seed):
                connection.execute(statement)
        assert _count(connection, "SELECT count(*) FROM pool_settings") == 4
        assert _count(connection, "SELECT count(*) FROM report_schedules") == 3
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════
# 约束与触发器（schema 真的能用，而不只是建出来了）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", TASK_STATUSES)
def test_all_16_task_statuses_accepted(connection: sqlite3.Connection, status: str) -> None:
    _insert_task(connection, f"t_{status}", status=status)
    assert _count(connection, "SELECT count(*) FROM tasks WHERE id=?", f"t_{status}") == 1


def test_unknown_task_status_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(connection, "bad", status="hallucinated")


def test_task_progress_range_enforced(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(connection, "bad_progress", progress=1.5)


def test_json_columns_validate(connection: sqlite3.Connection) -> None:
    """``json_valid`` CHECK：脏 JSON 不能落库（否则前端解析必炸）。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(connection, "bad_json", payload_json="{not json")


def test_trigger_refreshes_updated_at(connection: sqlite3.Connection) -> None:
    """6 个 ``trg_*_touch`` 触发器：UPDATE 时自动刷 updated_at（应用层不用管）。"""
    _insert_task(connection, "t_touch")
    before = connection.execute("SELECT updated_at FROM tasks WHERE id='t_touch'").fetchone()[0]
    connection.execute(
        "UPDATE tasks SET stage_detail='voice: 1/27 句', "
        "updated_at=(SELECT updated_at FROM tasks WHERE id='t_touch') WHERE id='t_touch'"
    )
    after = connection.execute("SELECT updated_at FROM tasks WHERE id='t_touch'").fetchone()[0]
    assert after != before


def test_persona_active_unique_index(connection: sqlite3.Connection) -> None:
    """``ux_persona_active``：同一时刻只能有 1 个激活人物（部分唯一索引）。"""
    base = (
        "INSERT INTO personas(id,name,is_active,role_desc,tone,audience) VALUES (?,?,?,'人设','口吻','受众')"
    )
    connection.execute(base, ("p1", "甲", 1))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(base, ("p2", "乙", 1))
    connection.execute(base, ("p3", "丙", 0))  # 未激活可多个
    assert _count(connection, "SELECT count(*) FROM personas") == 2


def test_jobs_idempotency_key_is_scoped_by_task(connection: sqlite3.Connection) -> None:
    """★ 幂等键含 task_id：不同任务可有各自的 scene/1、final。"""
    _insert_task(connection, "t1")
    _insert_task(connection, "t2")
    sql = "INSERT INTO jobs(id, task_id, pool, unit_type, unit_ref) VALUES (?,?,?,?,?)"
    connection.execute(sql, ("j1", "t1", "render", "final", "final"))
    connection.execute(sql, ("j2", "t2", "render", "final", "final"))  # 允许
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("j3", "t1", "render", "final", "final"))  # 同任务重复 ⇒ 拒绝


def test_cascade_delete_task_removes_children(connection: sqlite3.Connection) -> None:
    """``ON DELETE CASCADE``：删任务必须带走 jobs / script_sentences / artifacts。"""
    _insert_task(connection, "t1")
    connection.execute("INSERT INTO scripts(id,task_id,body_md) VALUES ('s1','t1','正文')")
    connection.execute(
        "INSERT INTO script_sentences(id,task_id,script_id,seq,text_raw,text,speaker) "
        "VALUES ('sen1','t1','s1',1,'原文','文本','bigbear')"
    )
    connection.execute(
        "INSERT INTO jobs(id,task_id,pool,unit_type,unit_ref) VALUES ('j1','t1','voice','sentence','sen1')"
    )
    connection.execute("DELETE FROM tasks WHERE id='t1'")
    assert _count(connection, "SELECT count(*) FROM jobs") == 0
    assert _count(connection, "SELECT count(*) FROM script_sentences") == 0
    assert _count(connection, "SELECT count(*) FROM scripts") == 0


def test_sentence_unique_per_script(connection: sqlite3.Connection) -> None:
    """``UNIQUE(script_id, seq)`` 是断点续传的基石：同句不会插两条。"""
    _insert_task(connection, "t1")
    connection.execute("INSERT INTO scripts(id,task_id,body_md) VALUES ('s1','t1','正文')")
    sql = (
        "INSERT INTO script_sentences(id,task_id,script_id,seq,text_raw,text,speaker) VALUES (?,?,?,?,?,?,?)"
    )
    connection.execute(sql, ("sen1", "t1", "s1", 1, "a", "a", "bigbear"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("sen2", "t1", "s1", 1, "b", "b", "littlebear"))


def test_script_sentence_tts_status_check(connection: sqlite3.Connection) -> None:
    """逐句合成的 5 态（pending/synthesizing/done/failed/skipped）。"""
    _insert_task(connection, "t1")
    connection.execute("INSERT INTO scripts(id,task_id,body_md) VALUES ('s1','t1','正文')")
    sql = (
        "INSERT INTO script_sentences(id,task_id,script_id,seq,text_raw,text,speaker,tts_status) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )
    for status in ("pending", "synthesizing", "done", "failed", "skipped"):
        connection.execute(
            sql, (f"sen_{status}", "t1", "s1", hash(status) % 1000, "a", "a", "bigbear", status)
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("bad", "t1", "s1", 999, "a", "a", "bigbear", "donee"))


def test_publish_schedule_due_partial_index_used(connection: sqlite3.Connection) -> None:
    """到期查询必须走 (enabled, next_run_at) 索引而不是全表扫描（T5.6 真机已验证的形态）。

    库里有两条候选索引（``idx_sched_due`` 是 ``WHERE enabled=1`` 的部分索引、
    ``idx_sched_enabled`` 是普通复合索引），规划器选哪条都可能，这里只锁死
    "走索引 + 不全表扫描"这个真正要保证的性质。
    """
    connection.execute(
        "INSERT INTO publish_schedules(id, platforms_json, account_ids_json, mode, window_start, "
        "window_end, next_run_at, enabled) VALUES (?,?,?,?,?,?,?,?)",
        (
            "sch1",
            '["douyin"]',
            '["acc_main"]',
            "daily_window",
            "18:00",
            "21:30",
            "2026-09-13T10:00:00.000Z",
            1,
        ),
    )
    plan_rows = connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM publish_schedules "
        "WHERE enabled=1 AND next_run_at <= '2026-09-14T00:00:00.000Z'"
    ).fetchall()
    detail = " ".join(str(r["detail"]) for r in plan_rows)
    assert "USING INDEX" in detail
    assert "idx_sched" in detail
    assert "SCAN publish_schedules" not in detail


def test_publications_idempotency_key_unique(connection: sqlite3.Connection) -> None:
    _insert_task(connection, "t1")
    sql = (
        "INSERT INTO publications(id,task_id,platform,account_id,video_path,video_sha256,"
        "title,idempotency_key) VALUES (?,?,?,?,?,?,?,?)"
    )
    connection.execute(sql, ("p1", "t1", "douyin", "acc", "v.mp4", "abc", "标题", "k1"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("p2", "t1", "douyin", "acc", "v.mp4", "abc", "标题", "k1"))


def test_publications_rejects_unknown_platform(connection: sqlite3.Connection) -> None:
    _insert_task(connection, "t1")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO publications(id,task_id,platform,account_id,video_path,video_sha256,"
            "title,idempotency_key) VALUES ('p1','t1','myspace','acc','v.mp4','abc','标题','k1')"
        )


def test_broll_license_required(connection: sqlite3.Connection) -> None:
    """``license`` 是 NOT NULL 且限定 4 值：授权不清的素材进不来（§3.3.14）。"""
    sql = "INSERT INTO broll_clips(id,path,sha256,duration_ms,license) VALUES (?,?,?,?,?)"
    connection.execute(sql, ("c1", "a.mp4", "h1", 1000, "self_recorded"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, ("c2", "b.mp4", "h2", 1000, "unknown_license"))


def test_system_logs_autoincrement_is_cursor(connection: sqlite3.Connection) -> None:
    """``system_logs.id`` 单调递增 ⇒ WS 的 ``since_id`` 增量游标（§03.3.12）。"""
    for i in range(3):
        connection.execute(
            "INSERT INTO system_logs(level, source, message) VALUES ('info','pipeline',?)", (f"m{i}",)
        )
    ids = [r["id"] for r in connection.execute("SELECT id FROM system_logs ORDER BY id")]
    assert ids == sorted(ids) and len(set(ids)) == 3


def test_audit_ops_actor_and_source_enums(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO audit_ops(actor, action, target_type, source) "
            "VALUES ('robot','task.approve','task','webui')"
        )


# ══════════════════════════════════════════════════════════════════════
# doctor 门禁（§03.7.2 规则 1：改历史文件 = 全站拒绝启动）
# ══════════════════════════════════════════════════════════════════════


def test_doctor_check_ok_when_db_missing(tmp_path: Path) -> None:
    """库还不存在 ⇒ ok（首次 migrate 会创建），不是故障。"""
    paths = StudioPaths(home=tmp_path, data_dir=tmp_path / "data")
    result = doctor_check(paths)
    assert result.status == "ok"
    assert result.blocking is False


def test_doctor_check_ok_after_migrate(tmp_path: Path) -> None:
    paths = StudioPaths(home=tmp_path, data_dir=tmp_path / "data")
    migrate(paths.db_file)
    result = doctor_check(paths)
    assert result.status == "ok"
    assert result.blocking is True
    assert len(result.data["applied"]) == len(EXPECTED_MIGRATIONS)


def test_doctor_check_warns_on_pending(tmp_path: Path) -> None:
    """待应用迁移 ⇒ warn（非阻塞）：跑 `studio db migrate` 补上即可。

    删的必须是**最新**那条（``EXPECTED_VERSIONS[-1]``）：删旧的那条会变成"乱序"
    （另一条用例的场景）—— 乱序是 fail 而不是 warn，两者要能区分开。
    """
    paths = StudioPaths(home=tmp_path, data_dir=tmp_path / "data")
    migrate(paths.db_file)
    connection = connect(paths.db_file, apply=False)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (EXPECTED_VERSIONS[-1],))
    finally:
        connection.close()

    result = doctor_check(paths)
    assert result.status == "warn"
    assert result.blocking is False
    assert result.data["pending"] == [EXPECTED_VERSIONS[-1]]


def test_doctor_check_blocks_on_out_of_order(tmp_path: Path) -> None:
    """乱序（库已到 0006，却冒出 0005 待应用）⇒ fail + 阻塞（§03.7.2 规则 1）。"""
    paths = StudioPaths(home=tmp_path, data_dir=tmp_path / "data")
    migrate(paths.db_file)
    connection = connect(paths.db_file, apply=False)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version='0005'")
    finally:
        connection.close()

    result = doctor_check(paths)
    assert result.status == "fail"
    assert result.blocking is True
    assert result.data["pending"] == ["0005"]


def test_check_on_fresh_db_is_ok(tmp_path: Path) -> None:
    path = tmp_path / "studio.db"
    migrate(path)
    assert check(path).ok
