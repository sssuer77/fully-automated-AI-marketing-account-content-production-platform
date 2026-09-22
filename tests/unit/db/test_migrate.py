"""``db/migrate.py`` 单测：发现、计划、六条硬约束、自检。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.db.engine import MIGRATIONS_DIR, connect
from studio.db.migrate import (
    SCHEMA_MIGRATIONS_DDL,
    check,
    declared_objects,
    discover,
    migrate,
    plan,
    read_applied,
)

EXPECTED_TABLES = 31
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
    "0010_metrics",
    "0011_topic_outlines",
)
EXPECTED_VERSIONS = tuple(label.split("_", 1)[0] for label in EXPECTED_MIGRATIONS)
EXPECTED_MIGRATION_NAMES = tuple(label.split("_", 1)[1] for label in EXPECTED_MIGRATIONS)


@pytest.fixture
def migrations_copy(tmp_path: Path) -> Path:
    """真实迁移目录的可写副本（用于制造"历史被改 / 文件缺失 / 乱序"）。"""
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "studio.db"


# ══════════════════════════════════════════════════════════════════════
# 发现
# ══════════════════════════════════════════════════════════════════════


def test_discover_finds_all_versioned_files() -> None:
    files = discover()
    assert tuple(f.version for f in files) == EXPECTED_VERSIONS
    assert tuple(f.name for f in files) == EXPECTED_MIGRATION_NAMES


def test_discover_skips_pragmas_file() -> None:
    """``0000_pragmas.sql`` 是连接级、**不登记版本**（§03.7.1）。"""
    assert all(f.version != "0000" for f in discover())


def test_discover_checksum_is_sha256_of_file(tmp_path: Path, migrations_copy: Path) -> None:
    first = discover(migrations_copy)[0]
    assert first.checksum == hashlib.sha256(first.path.read_bytes()).hexdigest()
    assert len(first.checksum) == 64


def test_discover_rejects_bad_filename(migrations_copy: Path) -> None:
    (migrations_copy / "7_bad.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(StudioError) as excinfo:
        discover(migrations_copy)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_FAILED


def test_discover_rejects_duplicate_version(migrations_copy: Path) -> None:
    (migrations_copy / "0007_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (migrations_copy / "0007_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(StudioError) as excinfo:
        discover(migrations_copy)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_FAILED


def test_discover_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        discover(tmp_path / "nope")
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_MISSING_FILE


# ══════════════════════════════════════════════════════════════════════
# 声明对象（自检基准）
# ══════════════════════════════════════════════════════════════════════


def test_declared_objects_counts_match_spec() -> None:
    """§03.7.1 真机验证过的 29 表 / 60 索引 / 6 触发器。"""
    objects = declared_objects()
    assert len(objects["table"]) == EXPECTED_TABLES
    assert len(objects["index"]) == EXPECTED_INDEXES
    assert len(objects["trigger"]) == EXPECTED_TRIGGERS


def test_declared_objects_are_unique_names() -> None:
    """跨文件重名 ⇒ 后一个会被 IF NOT EXISTS 静默吞掉，必须防住。"""
    objects = declared_objects()
    for kind, names in objects.items():
        assert len(names) == len(set(names)), kind


def test_bootstrap_ddl_matches_init_migration() -> None:
    """迁移器自建的 ``schema_migrations`` 必须与 0001 里的定义逐字一致（防漂移）。"""
    init = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")

    def normalize(text: str) -> str:
        return " ".join(text.replace(";", " ").split())

    assert normalize(SCHEMA_MIGRATIONS_DDL) in normalize(init)


# ══════════════════════════════════════════════════════════════════════
# 迁移：六条硬约束
# ══════════════════════════════════════════════════════════════════════


def test_migrate_creates_all_objects(db: Path) -> None:
    report = migrate(db)
    assert report.executed == list(EXPECTED_MIGRATIONS)
    assert report.plan.up_to_date
    assert report.table_count == EXPECTED_TABLES


def test_migrate_is_idempotent(db: Path) -> None:
    """约束 3：连续执行两次结果一致（第二次为空操作）。"""
    first = migrate(db)
    second = migrate(db)
    assert len(first.executed) == len(EXPECTED_MIGRATIONS)
    assert second.executed == []
    assert second.plan.up_to_date

    connection = connect(db, read_only=True)
    try:
        count = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        pools = connection.execute("SELECT count(*) FROM pool_settings").fetchone()[0]
        scheds = connection.execute("SELECT count(*) FROM report_schedules").fetchone()[0]
    finally:
        connection.close()
    assert count == len(EXPECTED_MIGRATIONS)
    assert pools == 4
    assert scheds == 3


def test_migrate_dry_run_writes_nothing(db: Path) -> None:
    report = migrate(db, dry_run=True)
    assert report.executed == []
    assert len(report.plan.pending) == len(EXPECTED_MIGRATIONS)
    connection = connect(db, apply=False)
    try:
        assert read_applied(connection) == ()
    finally:
        connection.close()


def test_migrate_records_checksums(db: Path) -> None:
    migrate(db)
    connection = connect(db, read_only=True)
    try:
        applied = read_applied(connection)
    finally:
        connection.close()
    assert tuple(a.version for a in applied) == EXPECTED_VERSIONS
    assert all(len(a.checksum) == 64 for a in applied)
    assert all(a.duration_ms is not None for a in applied)
    assert all(a.applied_at.endswith("Z") for a in applied)


def test_migrate_rejects_modified_history(db: Path, migrations_copy: Path) -> None:
    """约束 1：已应用的迁移**永不修改** ⇒ 变更即拒绝（§03.7.2）。"""
    migrate(db, migrations_dir=migrations_copy)
    victim = migrations_copy / "0002_topics.sql"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n-- 偷改\n", encoding="utf-8")

    with pytest.raises(StudioError) as excinfo:
        migrate(db, migrations_dir=migrations_copy)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_CHECKSUM_MISMATCH
    assert "0002" in excinfo.value.context["version"]


def test_migrate_rejects_missing_file(db: Path, migrations_copy: Path) -> None:
    migrate(db, migrations_dir=migrations_copy)
    (migrations_copy / "0003_templates.sql").unlink()

    with pytest.raises(StudioError) as excinfo:
        migrate(db, migrations_dir=migrations_copy)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_MISSING_FILE


def _forget_applied(db: Path, version: str) -> None:
    """模拟"库已滚到更高版本，却又冒出一个更低版本的待应用迁移"。"""
    connection = connect(db, apply=False)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
    finally:
        connection.close()


def test_migrate_rejects_bad_filename(db: Path, migrations_copy: Path) -> None:
    """文件名不合规（4 位数字 + 下划线 + 小写名）⇒ 拒绝执行，不猜版本号。"""
    (migrations_copy / "0003b_late.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(StudioError) as excinfo:
        migrate(db, migrations_dir=migrations_copy)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_FAILED


def test_migrate_rejects_out_of_order(db: Path) -> None:
    """约束 5：新迁移必须比已应用的最大版本更大。"""
    migrate(db)
    _forget_applied(db, "0005")

    with pytest.raises(StudioError) as excinfo:
        migrate(db)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_OUT_OF_ORDER
    assert excinfo.value.context["version"] == "0005"
    assert excinfo.value.context["highest_applied"] == EXPECTED_VERSIONS[-1]


def test_migrate_out_of_order_detected_via_plan(db: Path) -> None:
    """乱序要在 ``plan()`` 层就能看出来（不只靠 ``migrate()`` 抛错）。"""
    migrate(db)
    _forget_applied(db, "0005")

    connection = connect(db, apply=False)
    try:
        current = plan(connection, discover())
    finally:
        connection.close()
    assert [f.version for f in current.out_of_order] == ["0005"]
    assert not current.is_clean
    with pytest.raises(StudioError) as excinfo:
        current.raise_if_dirty()
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_OUT_OF_ORDER


def test_migrate_rolls_back_failed_file(db: Path, migrations_copy: Path) -> None:
    """约束 2：单文件单事务 ⇒ 失败整体回滚，不留半成品。"""
    bad = migrations_copy / "0008_broken.sql"
    bad.write_text(
        "CREATE TABLE IF NOT EXISTS ok_table(x);\n"
        "CREATE TABLE IF NOT EXISTS broken(y INTEGER NOT NULL DEFAULT);\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):  # noqa: B017 — sqlite3.OperationalError
        migrate(db, migrations_dir=migrations_copy)

    connection = connect(db, apply=False)
    try:
        applied = {a.version for a in read_applied(connection)}
        leftover = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ok_table'"
        ).fetchone()
    finally:
        connection.close()
    assert "0008" not in applied
    assert leftover is None  # 同文件内已执行的语句也被回滚


def test_migrate_new_file_applies_on_top(db: Path, migrations_copy: Path) -> None:
    """约束 4：可前滚 —— 在已有库上追加一个**更大版本号**的迁移即可继续。

    版本号由 ``EXPECTED_VERSIONS[-1] + 1`` 现算：写死一个号就会在"这个号被真迁移
    用掉"的那天变成"版本号重复"（本用例 2026-09-14 就踩过一次）。
    """
    migrate(db, migrations_dir=migrations_copy)
    next_version = f"{int(EXPECTED_VERSIONS[-1]) + 1:04d}"
    (migrations_copy / f"{next_version}_extra.sql").write_text(
        "CREATE TABLE IF NOT EXISTS extra(x INTEGER);\n", encoding="utf-8"
    )
    report = migrate(db, migrations_dir=migrations_copy)
    assert report.executed == [f"{next_version}_extra"]
    assert report.plan.up_to_date


# ══════════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════════


def test_check_all_green_after_migrate(db: Path) -> None:
    migrate(db)
    report = check(db)
    assert report.ok, report.to_dict()
    names = [item.name for item in report.items]
    assert names == [
        "db.journal_mode",
        "db.foreign_keys",
        "db.integrity_check",
        "db.foreign_key_check",
        "db.tables",
        "db.indexes",
        "db.triggers",
        "db.migrations",
    ]
    assert all(item.status == "ok" for item in report.items)


def test_check_reports_object_counts(db: Path) -> None:
    migrate(db)
    report = check(db)
    by_name = {item.name: item for item in report.items}
    assert by_name["db.tables"].data == {
        "actual": EXPECTED_TABLES,
        "declared": EXPECTED_TABLES,
        "missing": [],
        "extra": [],
    }
    assert by_name["db.indexes"].data["actual"] == EXPECTED_INDEXES
    assert by_name["db.triggers"].data["actual"] == EXPECTED_TRIGGERS


def test_check_detects_dropped_index(db: Path) -> None:
    """漏建 / 被删索引必须被发现（而不是靠硬编码数字碰运气）。"""
    migrate(db)
    connection = connect(db, apply=False)
    try:
        connection.execute("DROP INDEX idx_jobs_claim")
    finally:
        connection.close()

    report = check(db)
    assert not report.ok
    item = next(i for i in report.items if i.name == "db.indexes")
    assert item.status == "fail"
    assert "idx_jobs_claim" in item.data["missing"]


def test_check_detects_extra_object(db: Path) -> None:
    migrate(db)
    connection = connect(db, apply=False)
    try:
        connection.execute("CREATE TABLE sneaky(x INTEGER)")
    finally:
        connection.close()

    report = check(db)
    assert not report.ok
    item = next(i for i in report.items if i.name == "db.tables")
    assert "sneaky" in item.data["extra"]


def test_check_warns_on_pending_migration(db: Path) -> None:
    """未应用的迁移是 warn（非 fail）：库能用，只是没滚到最新。"""
    migrate(db, dry_run=False)
    connection = connect(db, apply=False)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (EXPECTED_VERSIONS[-1],))
    finally:
        connection.close()

    report = check(db)
    item = next(i for i in report.items if i.name == "db.migrations")
    assert item.status == "warn"
    assert EXPECTED_VERSIONS[-1] in item.data["pending"]


def test_check_fails_on_checksum_mismatch(db: Path, migrations_copy: Path) -> None:
    migrate(db, migrations_dir=migrations_copy)
    victim = migrations_copy / "0004_publish.sql"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n-- 偷改\n", encoding="utf-8")

    report = check(db, migrations_dir=migrations_copy)
    assert not report.ok
    item = next(i for i in report.items if i.name == "db.migrations")
    assert item.status == "fail"
    with pytest.raises(StudioError) as excinfo:
        report.raise_if_failed()
    assert excinfo.value.code is ErrorCode.DB_INTEGRITY_FAILED


def test_check_detects_foreign_key_violation(db: Path) -> None:
    """``foreign_keys=ON`` 只在写入时校验；``foreign_key_check`` 兜底扫描。"""
    migrate(db)
    connection = connect(db, apply=False)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO jobs(id, task_id, pool, unit_type, unit_ref) "
            "VALUES ('j1','ghost-task','render','final','final')"
        )
    finally:
        connection.close()

    report = check(db)
    item = next(i for i in report.items if i.name == "db.foreign_key_check")
    assert item.status == "fail"
    assert item.data["violations"] >= 1


def test_check_report_to_dict(db: Path) -> None:
    migrate(db)
    payload = check(db).to_dict()
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert len(payload["items"]) == 8
