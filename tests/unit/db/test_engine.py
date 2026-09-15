"""``db/engine.py`` 单测：PRAGMA 唯一真相、语句切分、连接语义。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.db.engine import (
    DEFAULT_BUSY_TIMEOUT_MS,
    PRAGMAS_FILE,
    apply_pragmas,
    connect,
    iter_statements,
    load_pragmas,
    transaction,
)

# ══════════════════════════════════════════════════════════════════════
# PRAGMA 唯一真相
# ══════════════════════════════════════════════════════════════════════


def test_pragmas_file_is_the_single_source() -> None:
    """§3.2 的 8 条连接级 PRAGMA 必须全部落在磁盘文件里（代码里不另抄一份）。"""
    statements = load_pragmas()
    names = [s.split()[1].lower() for s in statements]
    assert names == [
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "foreign_keys",
        "temp_store",
        "cache_size",
        "mmap_size",
        "wal_autocheckpoint",
    ]


def test_pragmas_include_r10_defences() -> None:
    """R10 锁竞争的两道防线：WAL + busy_timeout=5000。"""
    text = PRAGMAS_FILE.read_text(encoding="utf-8")
    assert "journal_mode = WAL" in text
    assert f"busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}" in text


def test_load_pragmas_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        load_pragmas(tmp_path / "nope.sql")
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_MISSING_FILE


def test_load_pragmas_empty_file_raises(tmp_path: Path) -> None:
    empty = tmp_path / "0000_pragmas.sql"
    empty.write_text("-- 只有注释\n", encoding="utf-8")
    with pytest.raises(StudioError) as excinfo:
        load_pragmas(empty)
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_MISSING_FILE


# ══════════════════════════════════════════════════════════════════════
# 语句切分（自研正则几乎必错的地方）
# ══════════════════════════════════════════════════════════════════════


def test_iter_statements_splits_simple_statements() -> None:
    sql = "CREATE TABLE a(x);\nCREATE INDEX i ON a(x);\n"
    assert list(iter_statements(sql)) == ["CREATE TABLE a(x);", "CREATE INDEX i ON a(x);"]


def test_iter_statements_keeps_trigger_body_whole() -> None:
    """触发器体里的分号**不得**被当成语句边界（本项目 6 个触发器全依赖这点）。"""
    sql = (
        "CREATE TRIGGER g AFTER UPDATE ON t FOR EACH ROW\n"
        "WHEN NEW.updated_at = OLD.updated_at\n"
        "BEGIN\n"
        "  UPDATE t SET updated_at = 'x' WHERE id = NEW.id;\n"
        "END;\n"
    )
    statements = list(iter_statements(sql))
    assert len(statements) == 1
    assert statements[0].startswith("CREATE TRIGGER")
    assert statements[0].rstrip().endswith("END;")


def test_iter_statements_strips_leading_comments() -> None:
    """注释横幅会被 ``complete_statement`` 粘进下一条语句 ⇒ 必须剥掉再执行。"""
    sql = "-- ===== banner =====\n-- 说明文字\n\nPRAGMA journal_mode = WAL;\n"
    assert list(iter_statements(sql)) == ["PRAGMA journal_mode = WAL;"]


def test_iter_statements_keeps_inline_trailing_comment() -> None:
    """行尾注释属于语句本体，不能连 SQL 一起删。"""
    sql = "CREATE TABLE a(x); -- 主键待补\n"
    assert list(iter_statements(sql)) == ["CREATE TABLE a(x); -- 主键待补"]


def test_iter_statements_ignores_semicolon_in_string_literal() -> None:
    sql = "INSERT INTO t VALUES ('a;b');\n"
    assert list(iter_statements(sql)) == ["INSERT INTO t VALUES ('a;b');"]


def test_iter_statements_tolerates_trailing_comments() -> None:
    """文件末尾的注释与空行不该被当成“未闭合语句”。"""
    sql = "CREATE TABLE a(x);\n\n-- 收尾说明\n-- 再来一行\n"
    assert list(iter_statements(sql)) == ["CREATE TABLE a(x);"]


def test_iter_statements_raises_on_unterminated() -> None:
    with pytest.raises(StudioError) as excinfo:
        list(iter_statements("CREATE TABLE a(x)\n"))
    assert excinfo.value.code is ErrorCode.DB_MIGRATION_FAILED


def test_iter_statements_raises_on_unterminated_trigger() -> None:
    """触发器漏了 ``END;`` ⇒ 必须报错而不是静默截断。"""
    sql = "CREATE TRIGGER g AFTER UPDATE ON t FOR EACH ROW\nBEGIN\n  UPDATE t SET a=1;\n"
    with pytest.raises(StudioError):
        list(iter_statements(sql))


def test_iter_statements_on_real_migrations() -> None:
    """全部真实迁移文件都能被完整切分（含 6 个触发器）。"""
    total = 0
    for path in sorted(PRAGMAS_FILE.parent.glob("0*.sql")):
        statements = list(iter_statements(path.read_text(encoding="utf-8")))
        assert statements, f"{path.name} 未切出任何语句"
        total += len(statements)
    assert total > 60


# ══════════════════════════════════════════════════════════════════════
# 连接
# ══════════════════════════════════════════════════════════════════════


def test_connect_applies_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    connection = connect(db)
    try:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == DEFAULT_BUSY_TIMEOUT_MS
        assert str(connection.execute("PRAGMA temp_store").fetchone()[0]) == "2"  # MEMORY
    finally:
        connection.close()


def test_connect_is_autocommit(tmp_path: Path) -> None:
    """``isolation_level=None``：事务必须显式 BEGIN（迁移器依赖此语义）。"""
    connection = connect(tmp_path / "x.db")
    try:
        assert connection.isolation_level is None
        connection.execute("CREATE TABLE a(x)")
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "deep" / "nested" / "x.db"
    connection = connect(db)
    connection.close()
    assert db.is_file()


def test_connect_read_only_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        connect(tmp_path / "nope.db", read_only=True)
    assert excinfo.value.code is ErrorCode.DB_NOT_WRITABLE


def test_connect_read_only_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    connect(db).close()
    connection = connect(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE a(x)")
    finally:
        connection.close()


def test_apply_pragmas_skips_journal_mode_when_read_only(tmp_path: Path) -> None:
    """只读连接改不了 WAL ⇒ 必须跳过而不是抛错。"""
    db = tmp_path / "x.db"
    connect(db).close()
    connection = sqlite3.connect(db, isolation_level=None)
    try:
        apply_pragmas(connection, read_only=True)
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    finally:
        connection.close()


def test_row_factory_is_row(tmp_path: Path) -> None:
    connection = connect(tmp_path / "x.db")
    try:
        connection.execute("CREATE TABLE a(x INTEGER)")
        connection.execute("INSERT INTO a VALUES (7)")
        row = connection.execute("SELECT x FROM a").fetchone()
        assert row["x"] == 7
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════
# 事务
# ══════════════════════════════════════════════════════════════════════


def test_transaction_commits(tmp_path: Path) -> None:
    connection = connect(tmp_path / "x.db")
    try:
        with transaction(connection):
            connection.execute("CREATE TABLE a(x)")
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    connection = connect(tmp_path / "x.db")
    try:
        with pytest.raises(RuntimeError), transaction(connection):
            connection.execute("CREATE TABLE a(x)")
            raise RuntimeError("boom")
        assert connection.in_transaction is False
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='a'").fetchone()
        assert exists is None
    finally:
        connection.close()


def test_transaction_immediate_takes_write_lock(tmp_path: Path) -> None:
    """``BEGIN IMMEDIATE`` 立刻取写锁 ⇒ 并发写者靠 busy_timeout 排队而非读到旧值。"""
    db = tmp_path / "x.db"
    first = connect(db)
    second = connect(db)
    try:
        first.execute("CREATE TABLE a(x INTEGER)")
        first.execute("BEGIN IMMEDIATE")
        assert first.in_transaction is True
        assert second.in_transaction is False
    finally:
        first.execute("ROLLBACK")
        first.close()
        second.close()
