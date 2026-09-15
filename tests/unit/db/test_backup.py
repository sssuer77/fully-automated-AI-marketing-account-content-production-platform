"""``db/backup.py`` 单测：热备、轮转、恢复演练（T4.12 · §03.7.4）。

四条"必须做到"
--------------
① **一致快照**：备份出来的库能独立打开、`db check` 全绿、行数与源库一致
   （WAL 下 `copyfile` 做不到这一点，这也是本模块存在的理由）；
② **轮转不越界**：7 日 + 4 周，且**永不删刚产出的那一份**；
③ **恢复演练不碰活库**：目标等于活库 ⇒ 拒绝；目标已存在 ⇒ 拒绝（除非显式覆盖）；
④ **如实报告**：抽查表缺失记 `skipped` 而不是记 0 行 —— 0 行与"表不在"是两件事。
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.db.backup import (
    BACKUP_STEM,
    BACKUP_SUFFIX,
    BackupFile,
    backup_database,
    backup_name,
    list_backups,
    plan_prune,
    restore_backup,
    snapshot_age_hours,
)
from studio.db.engine import MIGRATIONS_DIR, connect
from studio.db.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[3]


def _trim_migrations(dest: Path, *, keep: int) -> None:
    """只留 <= ``keep`` 的迁移，造一份"落后 N 个迁移"的库。

    别写死文件名：每加一个迁移就断一次，断的还是这条"备份只是旧了"的判据。
    """
    for path in sorted(dest.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if int(path.name[:4]) > keep:
            path.unlink()


# ══════════════════════════════════════════════════════════════════════
# 夹具与小工具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "studio.db"
    migrate(path)
    return path


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def _seed(path: Path, *, tasks: int = 3, logs: int = 5, start: int = 0) -> None:
    """写点真数据（备份里得真有东西，否则"行数一致"验的是两个 0）。

    ``start`` 让两次调用能接着写：`tasks.id` 是主键，复用 `t0` 会撞键 ——
    而"覆盖备份后行数变多"这条断言要的正是**第二次写进去的行**。
    """
    connection = connect(path)
    try:
        for index in range(start, start + tasks):
            connection.execute("INSERT INTO tasks(id, title) VALUES (?, ?)", (f"t{index}", f"任务 {index}"))
        for index in range(logs):
            connection.execute(
                "INSERT INTO system_logs(level, source, message) VALUES ('info','test',?)",
                (f"第 {index} 条",),
            )
    finally:
        connection.close()


def _rows(path: Path, table: str) -> int:
    connection = connect(path, read_only=True)
    try:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _make_backup(dest: Path, day: date, *, size: int = 10, at_hour: int = 3) -> BackupFile:
    """造一份"看起来像日备产物"的文件（**mtime 也钉死**）。

    新鲜度是按**文件写入时刻**算的（:func:`snapshot_age_hours`），日备又跑在 03:00
    （§03.7.4）⇒ 不钉 mtime 的话，这几个用例的结果会跟着"跑测试的那一分钟"漂移。
    """
    path = dest / f"{BACKUP_STEM}{day.strftime('%Y%m%d')}{BACKUP_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    moment = datetime.combine(day, time(at_hour, 0), tzinfo=UTC)
    os.utime(path, (moment.timestamp(), moment.timestamp()))
    return BackupFile(path=path, day=day, size_bytes=size, written_at=moment)


# ══════════════════════════════════════════════════════════════════════
# 命名与发现
# ══════════════════════════════════════════════════════════════════════


def test_the_name_is_the_local_day() -> None:
    """日备的名字是**本地日**：备份是给人看的，不是 UTC 账本。"""
    moment = datetime(2026, 9, 14, 23, 30, tzinfo=UTC)
    assert backup_name(moment).startswith(BACKUP_STEM)
    assert backup_name(moment).endswith(BACKUP_SUFFIX)
    assert len(backup_name(moment)) == len(BACKUP_STEM) + 8 + len(BACKUP_SUFFIX)


def test_listing_ignores_foreign_files(tmp_path: Path) -> None:
    """认不出来的文件**跳过而不报错**：备份目录是人也能往里放东西的地方。"""
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "README.txt").write_text("说明", encoding="utf-8")
    (dest / "studio_notadate.db").write_bytes(b"x")
    (dest / "studio_20260914.db").write_bytes(b"x")
    (dest / "subdir").mkdir()

    entries = list_backups(dest)

    assert [item.label for item in entries] == ["20260914"]


def test_listing_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "nope") == ()


def test_listing_is_newest_first(tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    _make_backup(dest, date(2026, 9, 1))
    _make_backup(dest, date(2026, 9, 14))
    _make_backup(dest, date(2026, 9, 7))

    assert [item.label for item in list_backups(dest)] == ["20260914", "20260907", "20260901"]


def test_sunday_backups_are_weekly() -> None:
    sunday = datetime(2026, 9, 13, 3, tzinfo=UTC)
    assert BackupFile(path=Path("x"), day=date(2026, 9, 13), size_bytes=0, written_at=sunday).weekly
    assert not BackupFile(path=Path("x"), day=date(2026, 9, 14), size_bytes=0, written_at=sunday).weekly


# ══════════════════════════════════════════════════════════════════════
# 轮转（纯函数）
# ══════════════════════════════════════════════════════════════════════


def test_prune_keeps_seven_daily_and_four_weekly(tmp_path: Path) -> None:
    """70 天连续备份 ⇒ 留最新 7 日 ∪ 最新 4 个周日，其余删。

    周日**不重复计数**：保留集是并集而不是"7 份日备再加 4 份周备"
    （后者会让实际保留数在周日之后悄悄变成 11 份）。
    """
    dest = tmp_path / "backups"
    dest.mkdir()
    entries = tuple(_make_backup(dest, date(2026, 7, 1) + timedelta(days=i)) for i in range(70))

    doomed = plan_prune(entries)

    keep = {item.day for item in entries} - {item.day for item in doomed}
    assert len(keep) >= 7
    # 最新 7 天必留
    newest = sorted((item.day for item in entries), reverse=True)[:7]
    assert set(newest) <= keep
    # 周日里最新的 4 个必留
    sundays = sorted((item.day for item in entries if item.weekly), reverse=True)
    assert set(sundays[:4]) <= keep
    # 第 5 个周日（更旧的那个）不因"周备"身份留下
    assert sundays[4] not in keep


def test_prune_never_touches_the_fresh_one(tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    entries = tuple(_make_backup(dest, date(2026, 8, 18) + timedelta(days=i)) for i in range(40))
    newest = max(entries, key=lambda item: item.day)

    doomed = plan_prune(entries, daily_keep=1, weekly_keep=1)

    assert newest.path not in {item.path for item in doomed}


def test_prune_with_zero_keeps_nothing_but_is_allowed(tmp_path: Path) -> None:
    """0 份是合法的（"一个都不留"），但不能是负数。"""
    dest = tmp_path / "backups"
    dest.mkdir()
    entries = (_make_backup(dest, date(2026, 9, 14)),)

    assert len(plan_prune(entries, daily_keep=0, weekly_keep=0)) == 1
    with pytest.raises(ValueError):
        plan_prune(entries, daily_keep=-1)


def test_prune_of_nothing_is_nothing() -> None:
    assert plan_prune(()) == ()


# ══════════════════════════════════════════════════════════════════════
# 备份
# ══════════════════════════════════════════════════════════════════════


def test_backup_produces_a_consistent_snapshot(db: Path, dest: Path) -> None:
    """★ 备份要**能独立打开且数据对得上** —— 这是 `VACUUM INTO` 的全部意义。"""
    _seed(db, tasks=3, logs=5)

    result = backup_database(db, dest_dir=dest)

    assert result.path.is_file()
    assert result.path.parent == dest
    assert result.size_bytes > 0
    assert _rows(result.path, "tasks") == 3
    assert _rows(result.path, "system_logs") == 5


def test_the_snapshot_has_no_wal_sidecar(db: Path, dest: Path) -> None:
    """快照是**单文件**：旁边不该有 `-wal`（有的话还原就又要拼盘了）。"""
    _seed(db)

    result = backup_database(db, dest_dir=dest)

    assert not Path(str(result.path) + "-wal").exists()


def test_backup_creates_the_directory(db: Path, tmp_path: Path) -> None:
    """备份目录不在 ⇒ 现造（无人值守跑的时候没人先 `mkdir`）。"""
    result = backup_database(db, dest_dir=tmp_path / "deep" / "nested")

    assert result.path.is_file()


def test_backup_of_a_missing_db_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        backup_database(tmp_path / "nope.db", dest_dir=tmp_path / "backups")

    assert excinfo.value.code is ErrorCode.BACKUP_FAILED
    assert "migrate" in (excinfo.value.remediation or "")


def test_a_second_backup_on_the_same_day_is_refused(db: Path, dest: Path) -> None:
    """同一天第二次 ⇒ **拒绝**而不是静默覆盖：那份可能是唯一可用的一份。"""
    # 两个时刻都落在**同一个本地日**（文件名用本地日，UTC 会跨到第二天）
    backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))

    with pytest.raises(StudioError) as excinfo:
        backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, 9, 0, tzinfo=UTC))

    assert excinfo.value.code is ErrorCode.BACKUP_FAILED
    assert "--overwrite" in (excinfo.value.remediation or "")


def test_overwrite_replaces_todays_backup(db: Path, dest: Path) -> None:
    _seed(db, tasks=1)
    first = backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))
    assert _rows(first.path, "tasks") == 1

    _seed(db, tasks=2, start=1)
    again = backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, 9, 0, tzinfo=UTC), overwrite=True)

    assert again.path == first.path
    assert _rows(again.path, "tasks") == 3  # 1 + 2，读到的是最新状态


def test_backup_never_targets_the_source_itself(tmp_path: Path) -> None:
    """纵深防御：库文件**恰好**叫今天的备份名（备份目录 = 库所在目录）⇒ 拒绝。

    这条守卫平时够不着（文件名不同），但"把备份目录配成 data/"这种手滑是会发生的，
    而那时的失败方式是 `VACUUM INTO` 把自己的源文件覆盖掉 —— 库当场报废。
    """
    dest = tmp_path / "data"
    dest.mkdir()
    db = dest / f"{BACKUP_STEM}20260914{BACKUP_SUFFIX}"
    migrate(db)

    with pytest.raises(StudioError) as excinfo:
        backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))

    assert excinfo.value.code is ErrorCode.BACKUP_FAILED
    assert _rows(db, "tasks") == 0  # 源库还在，且没被自己覆盖


def test_backup_prunes_on_the_way(db: Path, dest: Path) -> None:
    """轮转在**备份的同一次调用里**发生：无人值守没人再跑一条 prune。"""
    for offset in range(12):
        day = date(2026, 8, 1) + timedelta(days=offset)
        _make_backup(dest, day)

    result = backup_database(db, dest_dir=dest, now=datetime(2026, 9, 14, tzinfo=UTC))

    assert len(result.kept) == 8  # 7 份日备 + 今天那份
    assert result.pruned
    assert all(path.exists() for path in result.pruned) is False
    assert result.path in {item.path for item in result.kept}
    assert result.kept_bytes == sum(item.size_bytes for item in result.kept)


def test_the_fresh_backup_survives_pruning(db: Path, dest: Path) -> None:
    """`daily_keep=0` 也不能把刚产出的那份删掉（纵深防御）。"""
    result = backup_database(db, dest_dir=dest, daily_keep=0, weekly_keep=0)

    assert result.path.is_file()
    assert result.path in {item.path for item in result.kept}


# ══════════════════════════════════════════════════════════════════════
# 恢复演练
# ══════════════════════════════════════════════════════════════════════


def test_restore_reports_counts_and_a_green_check(db: Path, dest: Path, tmp_path: Path) -> None:
    """★ 恢复演练 = 还原 + `db check` + 抽查 3 张表（§03.7.4 明文三步）。"""
    _seed(db, tasks=4, logs=7)
    made = backup_database(db, dest_dir=dest)

    report = restore_backup(made.path, target=tmp_path / "drill" / "restored.db")

    assert report.ok
    counts = {item.name: item.rows for item in report.table_counts}
    assert counts["tasks"] == 4
    assert counts["jobs"] == 0
    assert counts["system_logs"] == 7
    assert report.check_report is not None and report.check_report.ok
    assert report.skipped_tables == ()
    assert report.to_dict()["ok"] is True


def test_restore_refuses_the_live_database(db: Path, dest: Path) -> None:
    """★ 拒绝还原到活库：这条路径是"验证备份可用"，不是"就地回滚"。"""
    made = backup_database(db, dest_dir=dest)

    with pytest.raises(StudioError) as excinfo:
        restore_backup(made.path, target=db, live_db=db, overwrite=True)

    assert excinfo.value.code is ErrorCode.RESTORE_REFUSED_LIVE_DB
    assert "restore_drill" in (excinfo.value.remediation or "")


def test_restore_refuses_an_existing_target(db: Path, dest: Path, tmp_path: Path) -> None:
    made = backup_database(db, dest_dir=dest)
    target = tmp_path / "restored.db"
    target.write_bytes(b"old")

    with pytest.raises(StudioError) as excinfo:
        restore_backup(made.path, target=target)

    assert excinfo.value.code is ErrorCode.RESTORE_TARGET_EXISTS
    assert target.read_bytes() == b"old"  # 一个字节都没动


def test_restore_clears_stale_wal(db: Path, dest: Path, tmp_path: Path) -> None:
    """覆盖还原前必须清 `-wal` / `-shm`：旧 WAL + 新主文件 = "能打开但对不上"。"""
    made = backup_database(db, dest_dir=dest)
    target = tmp_path / "restored.db"
    Path(str(target) + "-wal").write_bytes(b"stale")

    report = restore_backup(made.path, target=target, overwrite=True)

    assert report.ok
    assert not Path(str(target) + "-wal").exists()


def test_restore_of_a_missing_backup_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        restore_backup(tmp_path / "nope.db", target=tmp_path / "out.db")

    assert excinfo.value.code is ErrorCode.BACKUP_NOT_FOUND


def test_restore_marks_missing_tables_instead_of_reporting_zero(db: Path, dest: Path, tmp_path: Path) -> None:
    """表不在 ⇒ `skipped`，**不**记 0 行：0 行与"表不在"是两件事。"""
    made = backup_database(db, dest_dir=dest)

    report = restore_backup(made.path, target=tmp_path / "out.db", tables=("tasks", "no_such_table"))

    assert report.skipped_tables == ("no_such_table",)
    assert [item.name for item in report.table_counts] == ["tasks"]
    assert report.ok is False  # 抽查没做全 ⇒ 不算过


# ══════════════════════════════════════════════════════════════════════
# 新鲜度（总览台/运维脚本用）
# ══════════════════════════════════════════════════════════════════════


def test_snapshot_age_of_nothing_is_none() -> None:
    assert snapshot_age_hours(()) is None


def test_snapshot_age_counts_from_when_the_newest_was_written(tmp_path: Path) -> None:
    """按**文件写入时刻**算：09-14 03:00 那份到 09-15 00:00 是 21 小时。

    差一天的两份里必须挑最新那份 —— 挑错了会把"昨天刚备过"报成两天前。
    """
    dest = tmp_path / "backups"
    dest.mkdir()
    _make_backup(dest, date(2026, 9, 12))
    _make_backup(dest, date(2026, 9, 14))

    age = snapshot_age_hours(list_backups(dest), now=datetime(2026, 9, 15, 0, 0, tzinfo=UTC))

    assert age is not None
    assert 20.9 < age < 21.1


def test_snapshot_age_does_not_count_from_midnight(tmp_path: Path) -> None:
    """★ 03:00 产出的那份，到 04:00 只该是 1 小时 —— 不是"从零点起 4 小时"。

    按日期零点算会让上午打开面板的人看到"11 小时前"，进而去查一个根本没坏的任务。
    """
    dest = tmp_path / "backups"
    dest.mkdir()
    _make_backup(dest, date(2026, 9, 14))

    age = snapshot_age_hours(list_backups(dest), now=datetime(2026, 9, 14, 4, 0, tzinfo=UTC))

    assert age is not None
    assert 0.9 < age < 1.1


def test_restore_against_a_stale_live_db_is_still_faithful(tmp_path: Path) -> None:
    """★ 活库自己欠迁移 ⇒ 演练**不算失败**（那是源库的欠账，不是备份坏了）。

    这条是本模块最容易写错的一处判据：把"还原库全绿"当成通过，会在每次"活库
    落后一个迁移"的月份里报一次假红 —— 而告警疲劳正是从假红开始的。
    """
    migrations = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations)
    (migrations / "0008_observability.sql").unlink()  # 活库停在 0007
    stale = tmp_path / "stale.db"
    migrate(stale, migrations_dir=migrations)
    _seed(stale, tasks=2)
    made = backup_database(stale, dest_dir=tmp_path / "backups")

    # 自检仍用**当前**的迁移清单（仓库里那份）：这正是"活库欠着 0008"的真实场景
    report = restore_backup(made.path, target=tmp_path / "drill.db", live_db=stale)

    assert report.baseline_checked is True
    assert "db.indexes" in report.baseline_failures  # 源库自己就缺索引
    assert report.extra_failures == ()
    assert report.ok is True


def test_restore_catches_a_corrupt_backup(tmp_path: Path) -> None:
    """★ 反向用例：备份**真的坏了**时必须红（否则上面那条判据就是"永远绿"）。"""
    good = tmp_path / "good.db"
    migrate(good)
    _seed(good, tasks=3)
    made = backup_database(good, dest_dir=tmp_path / "backups")

    connection = connect(made.path)
    try:
        connection.execute("DROP INDEX idx_logs_source")  # 备份被损坏
    finally:
        connection.close()

    report = restore_backup(made.path, target=tmp_path / "drill.db", live_db=good)

    assert report.baseline_checked is True
    assert report.baseline_failures == ()
    assert report.extra_failures == ("db.indexes",)
    assert report.ok is False


# ══════════════════════════════════════════════════════════════════════
# 「备份只是比活库旧」≠「备份坏了」
# ══════════════════════════════════════════════════════════════════════


def test_restore_explains_a_pre_migration_backup(tmp_path: Path) -> None:
    """★ 刚上过迁移 ⇒ 盘上最新那份备份必然"落后"，这不是演练失败。

    真实场景：迁移上线 → 当天 03:00 的日备是迁移前的 → 月度演练若因此报红，
    人就会学会"演练红了不用管"。所以判据是"还原库的迁移集是活库的真前缀"。
    """
    partial = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, partial)
    _trim_migrations(partial, keep=6)
    old_db = tmp_path / "old.db"
    migrate(old_db, migrations_dir=partial)
    backup = backup_database(old_db, dest_dir=tmp_path / "bak").path
    live = tmp_path / "live.db"
    migrate(live)

    report = restore_backup(backup, target=tmp_path / "restored.db", live_db=live)

    assert report.ok is True
    # 具体是哪几项取决于"被砍掉的那几个迁移干了什么"（建表 / 建索引），
    # 所以只钉住判据本身：全落在滞后白名单里，且没有"多出来"的失败项。
    assert "db.indexes" in report.schema_lag
    assert set(report.schema_lag) <= {"db.tables", "db.indexes", "db.triggers", "db.migrations"}
    assert report.extra_failures == ()
    assert report.to_dict()["schema_lag"] == list(report.schema_lag)


def test_the_lag_rule_cannot_excuse_a_real_problem(tmp_path: Path) -> None:
    """★ 反向用例：真被删了索引的库，报的**也**是 `db.indexes`。

    光看失败项名字就放过它，等于把"备份坏了"洗成"备份旧了"。
    迁移集不是活库的真前缀 ⇒ 一律照旧报红。
    """
    live = tmp_path / "live.db"
    migrate(live)
    broken = tmp_path / "broken.db"
    shutil.copyfile(live, broken)
    connection = connect(broken)
    try:
        connection.execute("DROP INDEX idx_logs_source")
    finally:
        connection.close()

    report = restore_backup(broken, target=tmp_path / "restored.db", live_db=live)

    assert report.ok is False
    assert report.schema_lag == ()
    assert report.extra_failures == ("db.indexes",)


def test_restore_without_a_live_database_never_claims_a_lag(tmp_path: Path) -> None:
    """没给活库 ⇒ 没有基线可比 ⇒ 认不出就报红（宁可假红，不可假绿）。"""
    db = tmp_path / "studio.db"
    migrate(db)
    backup = backup_database(db, dest_dir=tmp_path / "bak").path

    report = restore_backup(backup, target=tmp_path / "restored.db")

    assert report.baseline_checked is False
    assert report.schema_lag == ()
