"""迁移器与库自检（T1.3 · §03.7）。

六条硬约束（§03.7.2）在本模块的落点
------------------------------------
1. **只增不改** ⇒ :func:`plan` 比对文件 sha256，历史迁移被改即 ``DB_MIGRATION_CHECKSUM_MISMATCH``；
2. **单文件单事务** ⇒ 每个文件在 ``BEGIN IMMEDIATE ... COMMIT`` 内执行，失败整体回滚；
3. **幂等** ⇒ DDL 全带 ``IF NOT EXISTS``、seed 带 ``ON CONFLICT DO NOTHING``（文件层保证）；
4. **可前滚不可回滚** ⇒ 本模块**没有** down 迁移入口（回退靠 ``scripts/restore_db.ps1``）；
5. **顺序执行 + 版本登记** ⇒ 按 version 字典序，每条成功即写 ``schema_migrations``；
6. **启动自检** ⇒ :func:`check` 断言 WAL / FK / integrity / 外键 / 对象清单。

``0000_pragmas.sql`` **不是**版本化迁移（连接级，由 ``db/engine.py`` 执行），
因此不出现在 ``schema_migrations`` 里。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from studio.core.doctor import CheckResult
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.engine import MIGRATIONS_DIR, connect, iter_statements, transaction

__all__ = [
    "SCHEMA_MIGRATIONS_DDL",
    "AppliedMigration",
    "DbCheckReport",
    "MigrationFile",
    "MigrationPlan",
    "MigrationReport",
    "check",
    "declared_objects",
    "discover",
    "doctor_check",
    "migrate",
    "plan",
    "read_applied",
]

logger = get_logger("studio.db.migrate")

#: 迁移文件名约定：``0001_init.sql``（§02.4）
MIGRATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

#: 非版本化文件（连接级 PRAGMA，不进 schema_migrations）
NON_VERSIONED: Final[frozenset[str]] = frozenset({"0000"})

#: ``schema_migrations`` 的引导 DDL。
#: 必须与 ``0001_init.sql`` 内的同名语句**逐字一致** ——
#: 迁移器要在应用 0001 之前就能登记版本，因此得先自己建表；
#: ``tests/integration/test_migrations.py`` 会断言两者不漂移。
SCHEMA_MIGRATIONS_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,                      -- '0001'
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL,                         -- 文件 sha256，防偷改历史迁移
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms INTEGER
)"""

_CREATE_TABLE: Final[re.Pattern[str]] = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)", re.IGNORECASE
)
_CREATE_INDEX: Final[re.Pattern[str]] = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)", re.IGNORECASE
)
_CREATE_TRIGGER: Final[re.Pattern[str]] = re.compile(
    r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)", re.IGNORECASE
)

#: 对象类别的（kind, 中文标签, 复数名）—— 自检项命名与双向比对共用
_OBJECT_LABELS: Final[tuple[tuple[str, str, str], ...]] = (
    ("table", "表", "tables"),
    ("index", "索引", "indexes"),
    ("trigger", "触发器", "triggers"),
)


# ── 数据结构 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """磁盘上的一个迁移文件。"""

    version: str
    name: str
    path: Path
    checksum: str
    statements: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.version}_{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "path": str(self.path),
            "checksum": self.checksum,
            "statements": len(self.statements),
        }


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """``schema_migrations`` 里的一行。"""

    version: str
    name: str
    checksum: str
    applied_at: str
    duration_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "checksum": self.checksum,
            "applied_at": self.applied_at,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class MigrationPlan:
    """磁盘文件 × 库内登记的差异。"""

    files: tuple[MigrationFile, ...]
    applied: tuple[AppliedMigration, ...]
    pending: tuple[MigrationFile, ...] = ()
    missing_files: tuple[AppliedMigration, ...] = ()
    checksum_mismatch: tuple[tuple[AppliedMigration, MigrationFile], ...] = ()
    out_of_order: tuple[MigrationFile, ...] = ()

    @property
    def is_clean(self) -> bool:
        """无缺文件、无 checksum 漂移、无乱序。"""
        return not (self.missing_files or self.checksum_mismatch or self.out_of_order)

    @property
    def up_to_date(self) -> bool:
        return self.is_clean and not self.pending

    def raise_if_dirty(self) -> None:
        """历史被改动 / 文件缺失 / 乱序 ⇒ 拒绝启动（§03.7.2 规则 1）。"""
        if self.checksum_mismatch:
            applied, current = self.checksum_mismatch[0]
            raise StudioError(
                f"迁移文件被修改过：{current.label}"
                f"（库内 {applied.checksum[:12]} ≠ 磁盘 {current.checksum[:12]}）",
                code=ErrorCode.DB_MIGRATION_CHECKSUM_MISMATCH,
                context={
                    "version": current.version,
                    "path": str(current.path),
                    "expected": applied.checksum,
                    "actual": current.checksum,
                },
                remediation=(
                    "已应用的迁移**只增不改**：还原该文件到原样，"
                    "或新增一个 000N_*.sql 来改结构（改 CHECK 见 §03.7.3 四步重建）"
                ),
            )
        if self.missing_files:
            gone = self.missing_files[0]
            raise StudioError(
                f"迁移文件缺失：{gone.version}_{gone.name}.sql（库内已登记但磁盘上没有）",
                code=ErrorCode.DB_MIGRATION_MISSING_FILE,
                context={"version": gone.version, "name": gone.name},
                remediation="从版本库恢复该文件；**不要**手工删 schema_migrations 里的行",
            )
        if self.out_of_order:
            late = self.out_of_order[0]
            highest = max(a.version for a in self.applied)
            raise StudioError(
                f"迁移顺序非法：{late.label} 的版本号小于已应用的最大版本 {highest}",
                code=ErrorCode.DB_MIGRATION_OUT_OF_ORDER,
                context={"version": late.version, "highest_applied": highest},
                remediation="新迁移必须用比现有最大版本更大的编号（只增不改）",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "applied": [a.to_dict() for a in self.applied],
            "pending": [f.to_dict() for f in self.pending],
            "missing_files": [a.to_dict() for a in self.missing_files],
            "checksum_mismatch": [
                {"applied": a.to_dict(), "current": f.to_dict()} for a, f in self.checksum_mismatch
            ],
            "out_of_order": [f.to_dict() for f in self.out_of_order],
            "is_clean": self.is_clean,
            "up_to_date": self.up_to_date,
        }


@dataclass(slots=True)
class MigrationReport:
    """:func:`migrate` 的结果。"""

    db_path: Path
    plan: MigrationPlan
    executed: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def table_count(self) -> int:
        return len(declared_objects(self.plan.files)["table"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "executed": list(self.executed),
            "executed_count": len(self.executed),
            "duration_ms": self.duration_ms,
            "table_count": self.table_count,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DbCheckItem:
    """一条自检项。"""

    name: str
    status: str  # ok | warn | fail
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "data": self.data}


@dataclass(slots=True)
class DbCheckReport:
    """``studio db check`` 的结果。"""

    db_path: Path
    items: list[DbCheckItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.status != "fail" for item in self.items)

    @property
    def failures(self) -> tuple[DbCheckItem, ...]:
        return tuple(item for item in self.items if item.status == "fail")

    def raise_if_failed(self) -> None:
        if not self.ok:
            first = self.failures[0]
            raise StudioError(
                f"数据库自检未通过：{first.name} — {first.detail}",
                code=ErrorCode.DB_INTEGRITY_FAILED,
                context={"db_path": str(self.db_path), "failures": [f.name for f in self.failures]},
                remediation="跑 `studio db check --json` 看逐项明细；必要时从每日备份还原",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "db_path": str(self.db_path),
            "items": [item.to_dict() for item in self.items],
            "failures": [item.name for item in self.failures],
        }


# ── 发现与解析 ──────────────────────────────────────────────────────────


def discover(migrations_dir: Path | None = None) -> tuple[MigrationFile, ...]:
    """按 version 字典序发现全部**版本化**迁移文件。"""
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        raise StudioError(
            f"迁移目录不存在：{directory}",
            code=ErrorCode.DB_MIGRATION_MISSING_FILE,
            context={"dir": str(directory)},
            remediation="恢复 src/studio/db/migrations/（禁止删除）",
        )

    found: list[MigrationFile] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if match is None:
            raise StudioError(
                f"迁移文件名不合规：{path.name}",
                code=ErrorCode.DB_MIGRATION_FAILED,
                context={"path": str(path), "pattern": MIGRATION_PATTERN.pattern},
                remediation="命名必须形如 0007_xxx.sql（4 位数字 + 下划线 + 小写名）",
            )
        version = match.group("version")
        if version in NON_VERSIONED:
            continue
        raw = path.read_bytes()
        found.append(
            MigrationFile(
                version=version,
                name=match.group("name"),
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                statements=tuple(iter_statements(raw.decode("utf-8"))),
            )
        )

    versions = [f.version for f in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise StudioError(
            f"迁移版本号重复：{', '.join(sorted(duplicates))}",
            code=ErrorCode.DB_MIGRATION_FAILED,
            context={"versions": sorted(duplicates)},
            remediation="每个版本号只能有一个文件",
        )
    return tuple(sorted(found, key=lambda f: f.version))


def declared_objects(files: tuple[MigrationFile, ...] | None = None) -> dict[str, set[str]]:
    """从迁移 SQL 里解析出**声明**的表 / 索引 / 触发器名字集合。

    用途：``check`` 拿它与库内实际对象比对，从而发现"迁移漏建了索引"
    这类静默缺口 —— 比硬编码 "29 表 / 58 索引" 更耐改。
    """
    source = files if files is not None else discover()
    objects: dict[str, set[str]] = {"table": set(), "index": set(), "trigger": set()}
    for migration in source:
        text = migration.path.read_text(encoding="utf-8")
        objects["table"].update(_CREATE_TABLE.findall(text))
        objects["index"].update(_CREATE_INDEX.findall(text))
        objects["trigger"].update(_CREATE_TRIGGER.findall(text))
    return objects


# ── 计划 ────────────────────────────────────────────────────────────────


def read_applied(connection: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
    """读取 ``schema_migrations``（表不存在 ⇒ 返回空，表示从未迁移）。"""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return ()
    rows = connection.execute(
        "SELECT version, name, checksum, applied_at, duration_ms FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple(
        AppliedMigration(
            version=row["version"],
            name=row["name"],
            checksum=row["checksum"],
            applied_at=row["applied_at"],
            duration_ms=row["duration_ms"],
        )
        for row in rows
    )


def plan(
    connection: sqlite3.Connection,
    files: tuple[MigrationFile, ...] | None = None,
) -> MigrationPlan:
    """比对磁盘与库内登记，产出可执行计划。"""
    source = files if files is not None else discover()
    applied = read_applied(connection)

    by_version = {f.version: f for f in source}
    applied_versions = {a.version for a in applied}

    pending = tuple(f for f in source if f.version not in applied_versions)
    missing = tuple(a for a in applied if a.version not in by_version)
    mismatch = tuple(
        (a, by_version[a.version])
        for a in applied
        if a.version in by_version and by_version[a.version].checksum != a.checksum
    )
    highest = max(applied_versions) if applied_versions else ""
    out_of_order = tuple(f for f in pending if highest and f.version < highest)

    return MigrationPlan(
        files=source,
        applied=applied,
        pending=pending,
        missing_files=missing,
        checksum_mismatch=mismatch,
        out_of_order=out_of_order,
    )


# ── 执行 ────────────────────────────────────────────────────────────────


def _apply_one(connection: sqlite3.Connection, migration: MigrationFile) -> int:
    """在**单事务**内执行一个迁移文件并登记版本，返回耗时毫秒。"""
    started = time.perf_counter()
    with transaction(connection, immediate=True):
        for statement in migration.statements:
            connection.execute(statement)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, duration_ms) VALUES (?,?,?,?)",
            (migration.version, migration.name, migration.checksum, elapsed_ms),
        )
    return elapsed_ms


def migrate(
    db_path: Path | str,
    *,
    migrations_dir: Path | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """把库前滚到最新版本（幂等；连续跑两次第二次为空操作）。

    :raises StudioError: 历史被改 / 文件缺失 / 乱序 / 单文件执行失败
    """
    files = discover(migrations_dir)
    path = Path(db_path)
    started = time.perf_counter()
    # 用带 PRAGMA 的连接建库：WAL 是**库级持久设置**，必须在这里就落盘，
    # 否则首建库会停在 journal_mode=delete（§03.7.2 规则 6 的启动自检会拦下）。
    connection = connect(path)
    try:
        connection.execute(SCHEMA_MIGRATIONS_DDL)
        current_plan = plan(connection, files)
        current_plan.raise_if_dirty()

        executed: list[str] = []
        if not dry_run:
            for migration in current_plan.pending:
                elapsed = _apply_one(connection, migration)
                executed.append(migration.label)
                logger.info(
                    "db.migration_applied",
                    version=migration.version,
                    name=migration.name,
                    duration_ms=elapsed,
                    db=str(path),
                )
            current_plan = plan(connection, files)
    finally:
        connection.close()

    report = MigrationReport(
        db_path=path,
        plan=current_plan,
        executed=executed,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    logger.info(
        "db.migrate_done",
        db=str(path),
        executed=len(executed),
        pending=len(current_plan.pending),
        duration_ms=report.duration_ms,
    )
    return report


# ── 自检 ────────────────────────────────────────────────────────────────


def _objects_in_db(connection: sqlite3.Connection) -> dict[str, set[str]]:
    """库内实际对象（排除 ``sqlite_%`` 内部表与自动索引）。"""
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual: dict[str, set[str]] = {"table": set(), "index": set(), "trigger": set()}
    for row in rows:
        bucket = actual.get(row["type"])
        if bucket is not None:
            bucket.add(row["name"])
    return actual


def check(db_path: Path | str, *, migrations_dir: Path | None = None) -> DbCheckReport:
    """库自检：WAL / 外键 / integrity / 对象清单 / 迁移状态（§03.7.2 规则 6）。"""
    path = Path(db_path)
    report = DbCheckReport(db_path=path)
    files = discover(migrations_dir)
    expected = declared_objects(files)

    connection = connect(path, apply=False)
    try:
        # ① 连接级 PRAGMA（在只读探针上无法改 WAL，故用读写连接）
        connection.execute("PRAGMA journal_mode=WAL").fetchall()
        connection.execute("PRAGMA foreign_keys=ON").fetchall()
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        report.items.append(
            DbCheckItem(
                name="db.journal_mode",
                status="ok" if journal_mode == "wal" else "fail",
                detail=f"journal_mode={journal_mode}（要求 wal）",
                data={"journal_mode": journal_mode},
            )
        )
        report.items.append(
            DbCheckItem(
                name="db.foreign_keys",
                status="ok" if foreign_keys == 1 else "fail",
                detail=f"foreign_keys={foreign_keys}（要求 1）",
                data={"foreign_keys": foreign_keys},
            )
        )

        # ② integrity_check / foreign_key_check
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        report.items.append(
            DbCheckItem(
                name="db.integrity_check",
                status="ok" if integrity == "ok" else "fail",
                detail=f"integrity_check={integrity}",
                data={"integrity_check": integrity},
            )
        )
        fk_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        report.items.append(
            DbCheckItem(
                name="db.foreign_key_check",
                status="ok" if not fk_violations else "fail",
                detail=f"外键违规 {len(fk_violations)} 行（要求 0）",
                data={"violations": len(fk_violations)},
            )
        )

        # ③ 对象清单：迁移声明 vs 库内实际（双向比对，漏建/多建都能发现）
        actual = _objects_in_db(connection)
        for kind, label, plural in _OBJECT_LABELS:
            missing = sorted(expected[kind] - actual[kind])
            extra = sorted(actual[kind] - expected[kind])
            report.items.append(
                DbCheckItem(
                    name=f"db.{plural}",
                    status="ok" if not (missing or extra) else "fail",
                    detail=(
                        f"{label} {len(actual[kind])} 个（声明 {len(expected[kind])} 个）"
                        if not (missing or extra)
                        else f"缺失 {missing}；多余 {extra}"
                    ),
                    data={
                        "actual": len(actual[kind]),
                        "declared": len(expected[kind]),
                        "missing": missing,
                        "extra": extra,
                    },
                )
            )

        # ④ 迁移状态
        current_plan = plan(connection, files)
        if not current_plan.is_clean:
            status, detail = "fail", "迁移历史不一致（改过历史文件 / 文件缺失 / 乱序）"
        elif current_plan.pending:
            first = current_plan.pending[0].label
            status, detail = "warn", f"待应用 {len(current_plan.pending)} 个迁移：{first}…"
        else:
            status, detail = "ok", f"已应用 {len(current_plan.applied)} 个迁移，无待应用"
        report.items.append(
            DbCheckItem(
                name="db.migrations",
                status=status,
                detail=detail,
                data={
                    "applied": [a.version for a in current_plan.applied],
                    "pending": [f.version for f in current_plan.pending],
                    "clean": current_plan.is_clean,
                },
            )
        )
    finally:
        connection.close()

    return report


def doctor_check(paths: StudioPaths) -> CheckResult:
    """把迁移状态包装成 ``doctor`` 检查项（供上层注入，见 ``DoctorReport.with_extra_checks``）。

    阻塞语义（§03.7.2 规则 1）：

    - 库**不存在** ⇒ ok（首次 ``studio db migrate`` 会创建，不是故障）
    - 历史迁移被改 / 文件缺失 / 乱序 ⇒ **fail + 阻塞**（"改历史文件 = 全站拒绝启动"）
    - 有未应用迁移 ⇒ warn（非阻塞；``studio db migrate`` 补上即可）
    """
    name = "db.migrations"
    db_path = paths.db_file
    if not db_path.is_file():
        return CheckResult(
            name=name,
            status="ok",
            blocking=False,
            detail=f"数据库尚未初始化（{db_path.name} 不存在；首次 `studio db migrate` 会创建 29 表）",
        )

    files = discover()
    try:
        connection = connect(db_path, read_only=True)
        try:
            current = plan(connection, files)
        finally:
            connection.close()
    except (StudioError, sqlite3.Error) as exc:
        return CheckResult(
            name=name,
            status="fail",
            blocking=True,
            detail=f"无法读取迁移状态：{exc}",
            remediation="检查库文件权限；禁止放在网络盘 / OneDrive / SMB",
        )

    data = {
        "applied": [a.version for a in current.applied],
        "pending": [f.version for f in current.pending],
        "checksum_mismatch": [f.version for _, f in current.checksum_mismatch],
        "missing_files": [a.version for a in current.missing_files],
    }
    if not current.is_clean:
        try:
            current.raise_if_dirty()
        except StudioError as exc:
            return CheckResult(
                name=name,
                status="fail",
                blocking=True,
                detail=exc.message,
                data={**data, **exc.context},
                remediation=exc.remediation,
            )
    if current.pending:
        return CheckResult(
            name=name,
            status="warn",
            blocking=False,
            detail=f"有 {len(current.pending)} 个待应用迁移（首个：{current.pending[0].label}）",
            data=data,
            remediation="跑 `studio db migrate`",
        )
    return CheckResult(
        name=name,
        status="ok",
        blocking=True,
        detail=f"已应用 {len(current.applied)} 个迁移，schema 与磁盘文件一致",
        data=data,
    )
