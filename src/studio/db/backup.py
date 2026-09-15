"""数据库备份与恢复演练（T4.12 · §03.7.4）。

为什么是 ``VACUUM INTO`` 而不是复制文件
--------------------------------------
WAL 模式下，库的**真身**分散在 `studio.db` + `studio.db-wal` 两个文件里，而且
`-wal` 随时在被改写。`shutil.copyfile(db, bak)` 拿到的是一份"主文件在某个瞬间 +
WAL 在另一个瞬间"的拼盘 —— 还原它要么丢最后几秒的写，要么直接报 malformed。
`VACUUM INTO` 由 SQLite 自己产出一个**一致快照**：它读事务视图、写全新文件、
顺带把碎片压实（备份通常比源库小），且**不阻塞写**（读事务不挡 WAL 的写者）。

三份保留期（§03.7.4）
--------------------
7 份日备 + 4 份周备（周日）。判据落在**文件名里的日期**而不是 mtime：文件被
同步工具摸过一次 mtime 就会漂，而"这是哪一天的备份"必须钉死。

恢复演练（§03.7.4「每月一次」）
------------------------------
还原到**临时库** → `db check` → 抽查 3 张表行数。两处刻意的保守：

1. **绝不覆盖活库**（``RESTORE_REFUSED_LIVE_DB``）：这条路径的存在意义是"验证备份
   可用"，不是"就地回滚"。真要回滚，先停进程、再手工换文件 —— 一个能被误触的
   "还原"按钮，迟早会在某个手滑的下午把当天的工作抹掉；
2. **覆盖已有文件要显式 ``overwrite=True``**，且覆盖前**清掉目标旁边的 `-wal` / `-shm`**
   —— 旧 WAL 配上新主文件，是"还原后库能打开但数据对不上"的经典来源。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from studio.core.clock import format_iso, utc_now
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.db.engine import connect
from studio.db.migrate import DbCheckReport, check, read_applied

__all__ = [
    "BACKUP_STEM",
    "BACKUP_SUFFIX",
    "DAILY_KEEP",
    "DRILL_TABLES",
    "LAG_FAILURES",
    "WEEKLY_KEEP",
    "BackupFile",
    "BackupResult",
    "RestoreReport",
    "TableCount",
    "backup_database",
    "backup_name",
    "list_backups",
    "plan_prune",
    "restore_backup",
]

logger = get_logger("studio.db.backup")

#: 备份文件名：``studio_YYYYMMDD.db``（§03.7.4）
BACKUP_STEM = "studio_"
BACKUP_SUFFIX = ".db"

#: 保留：7 份日备 + 4 份周备（周日）
DAILY_KEEP = 7
WEEKLY_KEEP = 4

#: 恢复演练抽查的表（§03.7.4 明文「抽查 3 张表行数」）。
#: 挑这三张的理由：`tasks` 是主业务表、`jobs` 是队列（GC 动得最勤）、
#: `system_logs` 是最大的流水表 —— 三张分别代表"核心状态 / 高频翻转 / 体量最大"，
#: 只查一张 `tasks` 会漏掉"队列表其实没还原成功"这类事故。
DRILL_TABLES: tuple[str, ...] = ("tasks", "jobs", "system_logs")

#: 「备份比活库**旧**」时**允许**出现的失败项（§03.7.4 恢复演练）。
#: 判据是"还原库的迁移集是活库迁移集的**真前缀**"，见 :func:`_is_schema_lag` ——
#: 光看名字不够：一个被删掉索引的坏库也会在 `db.indexes` 上报 fail。
LAG_FAILURES: frozenset[str] = frozenset({"db.indexes", "db.tables", "db.triggers", "db.migrations"})

#: 周日（`date.weekday()`：周一 = 0）
_WEEKLY_WEEKDAY = 6


@dataclass(frozen=True, slots=True)
class BackupFile:
    """一份备份（**只描述，不打开**）。

    ``day`` 是文件名里那一天（回答"这是哪天的"），``written_at`` 是文件真正的写入时刻
    （回答"多久没动了"）。两个都要：前者决定轮转（7 日 + 4 周），后者决定新鲜度 ——
    只留日期的话，早上刚跑完的日备会被算成"11 小时前"（见 :func:`snapshot_age_hours`）。
    """

    path: Path
    day: date
    size_bytes: int
    written_at: datetime

    @property
    def weekly(self) -> bool:
        """是不是周备（周日那天产的）。"""
        return self.day.weekday() == _WEEKLY_WEEKDAY

    @property
    def label(self) -> str:
        return self.day.strftime("%Y%m%d")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "day": self.label,
            "weekly": self.weekly,
            "size_bytes": self.size_bytes,
            "written_at": format_iso(self.written_at),
        }


@dataclass(slots=True)
class BackupResult:
    """一次备份的结果（含**轮转掉了什么**）。"""

    path: Path
    size_bytes: int
    duration_ms: int
    kept: tuple[BackupFile, ...] = ()
    pruned: tuple[Path, ...] = ()

    @property
    def kept_bytes(self) -> int:
        """备份目录当前总占用 —— 要计入磁盘水位预算（§03.7.4）。"""
        return sum(item.size_bytes for item in self.kept)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "kept": [item.to_dict() for item in self.kept],
            "pruned": [str(path) for path in self.pruned],
            "kept_bytes": self.kept_bytes,
        }


@dataclass(frozen=True, slots=True)
class TableCount:
    """恢复演练里的一次行数抽查。"""

    name: str
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rows": self.rows}


@dataclass(slots=True)
class RestoreReport:
    """恢复演练的结论（§03.7.4「每月一次」的证据）。

    ``ok`` 的判据是**"备份忠实还原了源库"**，不是"还原出来的库全绿"
    -----------------------------------------------------------------
    两者在一种很常见的情况下会分叉：**活库自己欠着迁移**。这时还原出来的库会
    在 `db.indexes` 上报 fail（缺了某个迁移建的索引）—— 但那是源库的欠账，
    不是备份坏了。把它算成演练失败，人就学会"演练红了不用管"，而这正是告警
    疲劳的起点。所以判据取**与活库基线比对**：还原库的 fail 集合 ⊆ 活库的 fail
    集合 ⇒ 备份是忠实的；多出来的 fail 才是"备份不可用"。

    ``baseline_failures`` 为空有两种含义，靠 ``baseline_checked`` 区分：
    "活库全绿"与"根本没给活库"（只还原到临时库的纯离线演练）。

    反过来的那种分叉同样常见：**备份比活库旧**（刚上过一个迁移，而盘上最新的
    那份备份是迁移前产的）。这时还原库会在 `db.indexes` 上报 fail —— 备份没坏，
    它只是老了，回滚时补跑一次 `studio db migrate` 即可。这一类比
    ``baseline_failures`` 更严格地判：必须**同时**满足"失败项只落在
    :data:`LAG_FAILURES` 里"与"还原库的迁移集是活库迁移集的真前缀"，
    才归到 :attr:`schema_lag`；否则一律算 :attr:`extra_failures`。
    只看失败项名字是不够的：一个真被删了索引的坏库，报的也是 `db.indexes`。
    """

    backup_path: Path
    target: Path
    table_counts: tuple[TableCount, ...] = ()
    check_report: DbCheckReport | None = None
    baseline_failures: tuple[str, ...] = ()
    baseline_checked: bool = False
    duration_ms: int = 0
    skipped_tables: tuple[str, ...] = field(default_factory=tuple)
    schema_lag: tuple[str, ...] = ()

    @property
    def extra_failures(self) -> tuple[str, ...]:
        """还原库**多出来**的失败项 —— 这些才是备份自己的问题。"""
        if self.check_report is None:
            return ()
        return tuple(
            item.name
            for item in self.check_report.failures
            if item.name not in self.baseline_failures and item.name not in self.schema_lag
        )

    @property
    def ok(self) -> bool:
        """抽查做全了 **且** 没有"活库没有、还原库却有"的失败项。"""
        return bool(self.check_report is not None) and not self.extra_failures and not self.skipped_tables

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backup_path": str(self.backup_path),
            "target": str(self.target),
            "table_counts": [item.to_dict() for item in self.table_counts],
            "skipped_tables": list(self.skipped_tables),
            "baseline_checked": self.baseline_checked,
            "baseline_failures": list(self.baseline_failures),
            "extra_failures": list(self.extra_failures),
            "schema_lag": list(self.schema_lag),
            "checks": None if self.check_report is None else self.check_report.to_dict(),
            "duration_ms": self.duration_ms,
        }


def backup_name(now: datetime) -> str:
    """``studio_YYYYMMDD.db``。用**本地日**：备份是给人看的日备，不是 UTC 账本。"""
    return f"{BACKUP_STEM}{now.astimezone().strftime('%Y%m%d')}{BACKUP_SUFFIX}"


def _parse_day(path: Path) -> date | None:
    """从文件名里读日期；读不出来 ⇒ ``None``（不是备份，别去删它）。"""
    if path.suffix != BACKUP_SUFFIX or not path.name.startswith(BACKUP_STEM):
        return None
    stamp = path.name[len(BACKUP_STEM) : -len(BACKUP_SUFFIX)]
    if len(stamp) != 8 or not stamp.isdigit():
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError:
        return None


def list_backups(dest_dir: Path) -> tuple[BackupFile, ...]:
    """目录里的备份，**按日期倒序**（最新在前）。

    认不出来的文件直接跳过（不报错）：备份目录是给人也能往里放东西的地方，
    一个 `README.txt` 不该让日备任务整体失败。
    """
    if not dest_dir.is_dir():
        return ()
    found: list[BackupFile] = []
    for path in dest_dir.iterdir():
        if not path.is_file():
            continue
        day = _parse_day(path)
        if day is None:
            continue
        info = path.stat()
        found.append(
            BackupFile(
                path=path,
                day=day,
                size_bytes=info.st_size,
                written_at=datetime.fromtimestamp(info.st_mtime, tz=UTC),
            )
        )
    found.sort(key=lambda item: (item.day, item.path.name), reverse=True)
    return tuple(found)


def plan_prune(
    entries: tuple[BackupFile, ...], *, daily_keep: int = DAILY_KEEP, weekly_keep: int = WEEKLY_KEEP
) -> tuple[BackupFile, ...]:
    """算出该删哪些（**纯函数**：好测，也好在 CLI 的 ``--dry-run`` 里复用）。

    保留集 = 「最新 ``daily_keep`` 份」∪「最新 ``weekly_keep`` 份周备」。
    不是"7 份日备 + 另外 4 份周备"：周日的备份本来就该在日备里，把它算两遍会让
    实际保留数在周日之后悄悄变成 11 份 —— 一个每年多占 4 份备份的偏差。
    """
    if daily_keep < 0 or weekly_keep < 0:
        raise ValueError("保留份数不能为负")
    ordered = sorted(entries, key=lambda item: (item.day, item.path.name), reverse=True)
    keep = {item.path for item in ordered[:daily_keep]}
    weekly = [item for item in ordered if item.weekly]
    keep.update(item.path for item in weekly[:weekly_keep])
    return tuple(item for item in ordered if item.path not in keep)


def backup_database(
    db_path: Path | str,
    *,
    dest_dir: Path,
    now: datetime | None = None,
    name: str | None = None,
    daily_keep: int = DAILY_KEEP,
    weekly_keep: int = WEEKLY_KEEP,
    overwrite: bool = False,
) -> BackupResult:
    """热备一份库并轮转旧备份（§03.7.4）。

    :param name: 文件名（缺省 ``studio_YYYYMMDD.db``）。**关键节点检查点**用它
        换成 ``premigrate_...`` 并放进单独目录 —— 那种备份不该被日备轮转碰到。
    :raises StudioError: 库不存在 / 目标已存在（``overwrite=False``）/ SQLite 报错
    """
    source = Path(db_path)
    if not source.is_file():
        raise StudioError(
            f"数据库不存在，无法备份：{source}",
            code=ErrorCode.BACKUP_FAILED,
            context={"db_path": str(source)},
            remediation="先跑 `studio db migrate` 初始化",
        )

    moment = now or utc_now()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (name or backup_name(moment))
    if target.exists() and not overwrite:
        # 同一天跑第二次：**不静默覆盖**（"今天的备份"可能正是唯一可用的那份）
        raise StudioError(
            f"今天的备份已存在：{target}",
            code=ErrorCode.BACKUP_FAILED,
            context={"path": str(target)},
            remediation="要重做请先手工移走旧文件，或加 --overwrite 显式覆盖",
        )
    if target.resolve() == source.resolve():
        raise StudioError(
            "备份目标不能是源库本身",
            code=ErrorCode.BACKUP_FAILED,
            context={"path": str(target)},
        )

    began = time.monotonic()
    _vacuum_into(source, target, overwrite=overwrite)
    duration_ms = int((time.monotonic() - began) * 1000)

    entries = list_backups(dest_dir)
    pruned = plan_prune(entries, daily_keep=daily_keep, weekly_keep=weekly_keep)
    removed: list[Path] = []
    for item in pruned:
        if item.path == target:
            continue  # 纵深防御：刚产出的那份永不删
        try:
            item.path.unlink()
        except OSError as exc:
            # 删不掉不算失败：备份已经产出了，轮转失败不该让"今天没备份"发生
            logger.warning("db.backup_prune_failed", path=str(item.path), error=str(exc))
            continue
        removed.append(item.path)

    kept = tuple(item for item in list_backups(dest_dir))
    logger.info(
        "db.backup_done",
        path=str(target),
        size_bytes=target.stat().st_size,
        duration_ms=duration_ms,
        pruned=len(removed),
        kept=len(kept),
    )
    return BackupResult(
        path=target,
        size_bytes=target.stat().st_size,
        duration_ms=duration_ms,
        kept=kept,
        pruned=tuple(removed),
    )


def _vacuum_into(source: Path, target: Path, *, overwrite: bool) -> None:
    """跑 ``VACUUM INTO``。**不能**在事务里（SQLite 会直接报错）。"""
    if overwrite:
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(target) + suffix)
            if stale.exists():
                stale.unlink()
    connection = connect(source)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        raise StudioError(
            f"备份失败：{exc}",
            code=ErrorCode.BACKUP_FAILED,
            context={"db_path": str(source), "target": str(target), "error": str(exc)},
            remediation="确认目标目录可写、磁盘空间足够；库本身可跑 `studio db check` 先自查",
        ) from exc
    finally:
        connection.close()


def restore_backup(
    backup_path: Path | str,
    *,
    target: Path,
    live_db: Path | str | None = None,
    overwrite: bool = False,
    tables: tuple[str, ...] = DRILL_TABLES,
    migrations_dir: Path | None = None,
) -> RestoreReport:
    """把一份备份还原到 ``target`` 并**当场验它可用**（§03.7.4 恢复演练）。

    :param live_db: 活库路径。与 ``target`` 相同 ⇒ 拒绝（见模块 docstring）；
        存在 ⇒ 一起算基线（见 :class:`RestoreReport`）
    :param overwrite: 覆盖已有 ``target``（默认拒绝，且覆盖前清 ``-wal`` / ``-shm``）
    :param migrations_dir: 自检用的迁移目录（缺省用仓库里那一份；测试注入副本用）
    """
    source = Path(backup_path)
    if not source.is_file():
        raise StudioError(
            f"备份文件不存在：{source}",
            code=ErrorCode.BACKUP_NOT_FOUND,
            context={"path": str(source)},
            remediation="跑 `studio db backup` 产出一份，或检查 --from 路径",
        )
    if live_db is not None and Path(live_db).resolve() == Path(target).resolve():
        raise StudioError(
            "拒绝把备份还原到活库上（恢复演练只还原到临时库）",
            code=ErrorCode.RESTORE_REFUSED_LIVE_DB,
            context={"live_db": str(live_db), "target": str(target)},
            remediation="真要回滚：先停全部进程，再手工替换 studio.db（见 docs/runbook/restore_drill.md）",
        )
    if target.exists() and not overwrite:
        raise StudioError(
            f"还原目标已存在：{target}",
            code=ErrorCode.RESTORE_TARGET_EXISTS,
            context={"path": str(target)},
            remediation="换一个空路径，或加 --force 显式覆盖",
        )

    began = time.monotonic()
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    shutil.copyfile(source, target)

    counts: list[TableCount] = []
    skipped: list[str] = []
    for name in tables:
        rows = _count_rows(target, name)
        if rows is None:
            skipped.append(name)
        else:
            counts.append(TableCount(name=name, rows=rows))

    report = check(target, migrations_dir=migrations_dir)
    baseline_failures: tuple[str, ...] = ()
    baseline_checked = False
    if live_db is not None and Path(live_db).is_file():
        # 基线：活库自己有哪些失败项。还原库多出来的才算备份的问题（见 RestoreReport）。
        baseline_checked = True
        baseline_failures = tuple(
            item.name for item in check(live_db, migrations_dir=migrations_dir).failures
        )

    lag = _schema_lag(
        report,
        baseline_failures=baseline_failures,
        baseline_checked=baseline_checked,
        restored=target,
        live_db=Path(live_db) if live_db is not None else None,
    )
    duration_ms = int((time.monotonic() - began) * 1000)
    outcome = RestoreReport(
        backup_path=source,
        target=target,
        table_counts=tuple(counts),
        check_report=report,
        baseline_failures=baseline_failures,
        baseline_checked=baseline_checked,
        duration_ms=duration_ms,
        skipped_tables=tuple(skipped),
        schema_lag=lag,
    )
    logger.info(
        "db.restore_drill_done",
        backup=str(source),
        target=str(target),
        ok=outcome.ok,
        extra_failures=list(outcome.extra_failures),
        schema_lag=list(outcome.schema_lag),
        duration_ms=duration_ms,
    )
    return outcome


def _schema_lag(
    report: DbCheckReport,
    *,
    baseline_failures: tuple[str, ...],
    baseline_checked: bool,
    restored: Path,
    live_db: Path | None,
) -> tuple[str, ...]:
    """把"备份只是比活库旧"的那几个失败项认出来（见 :class:`RestoreReport`）。

    两个条件**同时**成立才算：

    1. 多出来的失败项全部落在 :data:`LAG_FAILURES` 里（`integrity_check` 之类
       一旦失败就不在此列 ⇒ 真损坏不会被放过）；
    2. 还原库的迁移集是活库迁移集的**真前缀**（老，但不是另一条分支）。

    没给活库 / 读不出迁移表 ⇒ 返回空：认不出就照旧报红，宁可假红不可假绿。
    """
    if not baseline_checked or live_db is None or not live_db.is_file():
        return ()
    extra = tuple(item.name for item in report.failures if item.name not in baseline_failures)
    if not extra or not set(extra) <= LAG_FAILURES:
        return ()
    older = _applied_versions(restored)
    newer = _applied_versions(live_db)
    if not older or len(older) >= len(newer) or newer[: len(older)] != older:
        return ()
    return extra


def _applied_versions(db_path: Path) -> tuple[str, ...]:
    """已应用的迁移版本（按序）；库打不开 / 表不在 ⇒ 空元组。"""
    try:
        connection = connect(db_path, read_only=True)
    except (StudioError, sqlite3.Error):
        return ()
    try:
        return tuple(item.version for item in read_applied(connection))
    except sqlite3.Error:
        return ()
    finally:
        connection.close()


def _count_rows(db_path: Path, table: str) -> int | None:
    """数一张表的行数；表不存在 ⇒ ``None``（如实记成 skipped，不当 0）。"""
    connection = connect(db_path, read_only=True)
    try:
        try:
            row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        except sqlite3.Error:
            return None
        return None if row is None else int(row[0])
    finally:
        connection.close()


def snapshot_age_hours(entries: tuple[BackupFile, ...], *, now: datetime | None = None) -> float | None:
    """最新一份备份距现在几小时（``None`` = 一份都没有）。

    给总览台/运维脚本回答"日备还新鲜吗"：备份任务悄悄坏掉一周，磁盘上仍然
    "有备份"，只是都是旧的 —— 这个函数就是用来戳破那种"看起来有"的。

    口径是**文件的写入时刻**，不是文件名里那个日期的零点。两者最多能差 24 小时：
    日备 03:00 产出，按零点算的话上午打开面板会看到"11 小时前" —— 一个刚跑完的任务
    被报成快半天没动，这面板上就没人再信第二行了。文件名只回答"哪一天"，"多久没动"
    必须实测。
    """
    if not entries:
        return None
    moment = now or utc_now()
    delta = moment.astimezone(UTC) - entries[0].written_at.astimezone(UTC)
    return round(delta.total_seconds() / 3600.0, 2)
