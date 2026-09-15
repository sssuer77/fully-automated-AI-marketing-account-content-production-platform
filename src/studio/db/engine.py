"""SQLite 连接工厂与语句工具（T1.3 · §03.2 / §03.7）。

约定
----
- **PRAGMA 唯一真相**是 ``migrations/0000_pragmas.sql``（磁盘文件），
  本模块只负责"读出来、每条连接执行一遍"，不在代码里另抄一份。
- ``isolation_level=None``（autocommit）：事务一律显式 ``BEGIN IMMEDIATE``，
  这样才能精确控制"单文件单事务"（§03.7.2 规则 2）与 R10 的短事务纪律。
- 连接**不做连接池**：SQLite + WAL 下每进程少量长连接即可（四池各 1–2 个），
  连接池只会掩盖"事务里做 I/O"的错误用法。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger

__all__ = [
    "MIGRATIONS_DIR",
    "PRAGMAS_FILE",
    "apply_pragmas",
    "connect",
    "footprint",
    "iter_statements",
    "load_pragmas",
    "transaction",
]

logger = get_logger("studio.db")

#: 迁移目录（``src/studio/db/migrations/``）
MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "migrations"

#: 连接级 PRAGMA 文件（**不登记版本**，每次 connect 都执行）
PRAGMAS_FILE: Final[Path] = MIGRATIONS_DIR / "0000_pragmas.sql"

#: 默认 busy_timeout（与 0000_pragmas.sql 保持一致，R10 锁竞争第一道防线）
DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5000

#: 只读连接下**不能**执行的 PRAGMA（WAL 是库级持久设置，需写权限）
_READ_ONLY_SKIP: Final[frozenset[str]] = frozenset({"journal_mode"})


def load_pragmas(path: Path | None = None) -> tuple[str, ...]:
    """解析 PRAGMA 文件，返回语句元组（已去注释与空行）。

    :raises StudioError: 文件缺失或没有解析出任何 PRAGMA
    """
    pragma_path = path or PRAGMAS_FILE
    if not pragma_path.is_file():
        raise StudioError(
            f"PRAGMA 文件缺失：{pragma_path}",
            code=ErrorCode.DB_MIGRATION_MISSING_FILE,
            context={"path": str(pragma_path)},
            remediation="恢复 src/studio/db/migrations/0000_pragmas.sql（禁止删除）",
        )
    statements = tuple(iter_statements(pragma_path.read_text(encoding="utf-8")))
    if not statements:
        raise StudioError(
            f"PRAGMA 文件为空：{pragma_path}",
            code=ErrorCode.DB_MIGRATION_MISSING_FILE,
            context={"path": str(pragma_path)},
            remediation="恢复 0000_pragmas.sql 的 8 条 PRAGMA",
        )
    return statements


def _pragma_name(statement: str) -> str:
    """从 ``PRAGMA journal_mode = WAL;`` 提取 ``journal_mode``（小写）。"""
    match = re.search(r"^\s*PRAGMA\s+([A-Za-z_][A-Za-z0-9_]*)", statement, re.IGNORECASE | re.MULTILINE)
    return match.group(1).lower() if match else ""


def apply_pragmas(connection: sqlite3.Connection, *, read_only: bool = False) -> None:
    """逐条执行连接级 PRAGMA（幂等；只读连接跳过 ``journal_mode``）。"""
    for statement in load_pragmas():
        if read_only and _pragma_name(statement) in _READ_ONLY_SKIP:
            continue
        connection.execute(statement).fetchall()


def connect(
    db_path: Path | str,
    *,
    read_only: bool = False,
    apply: bool = True,
) -> sqlite3.Connection:
    """打开数据库并应用连接级 PRAGMA。

    :param read_only: 只读打开（``mode=ro``）；库文件不存在 ⇒ 直接报错
    :param apply: 是否执行 PRAGMA（仅测试需要关掉）
    """
    path = Path(db_path)
    if read_only:
        if not path.is_file():
            raise StudioError(
                f"数据库不存在（只读打开失败）：{path}",
                code=ErrorCode.DB_NOT_WRITABLE,
                context={"path": str(path)},
                remediation="先跑 `studio db migrate` 初始化数据库",
            )
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )

    connection.row_factory = sqlite3.Row
    if apply:
        try:
            apply_pragmas(connection, read_only=read_only)
        except sqlite3.Error:
            connection.close()
            raise
    return connection


def footprint(connection: sqlite3.Connection) -> tuple[int, int]:
    """``(库文件字节, 可回收空洞字节)``。

    空洞 = ``freelist_count × page_size``：``DELETE`` 删完旧行之后**文件并不会变小**，
    这个数回答的是"想立刻把盘还回去，得 VACUUM（要 2 倍空间 + 独占写）"。
    写在这里而不是各处各写一遍：GC 报告、``db vacuum``、观测面板三处要的是
    **同一个数**，三份实现迟早会让"面板说 0、命令说 120 MB"。
    """
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    return page_size * page_count, page_size * freelist


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """显式事务（默认 ``BEGIN IMMEDIATE``）。

    R10 纪律：**事务内禁止任何 I/O**（文件读写、HTTP、ffmpeg），
    事务只包住 SQL，目标 < 20ms。
    """
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _strip_leading_comments(text: str) -> str:
    """剥掉开头的空行与 ``--`` 注释行。

    ``sqlite3.complete_statement`` 只在**看见分号**时返回 True，因此注释横幅会
    被粘进后面第一条语句里（``-- banner\nPRAGMA journal_mode = WAL;`` 是一个
    完整块）。执行前必须把注释剥掉，否则 ``PRAGMA`` 名字解析会失手。
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("--"):
            break
        index += 1
    return "\n".join(lines[index:]).strip()


def iter_statements(sql: str) -> Iterator[str]:
    """把一段 SQL 脚本切分成可逐条 ``execute`` 的语句。

    用 :func:`sqlite3.complete_statement` 判定边界 —— 它认识字符串字面量、
    注释与 ``CREATE TRIGGER ... BEGIN ... END;`` 的块体，因此触发器里的分号
    不会被误切（自研正则切分器在这里几乎必错）。

    :raises StudioError: 文件末尾存在**未闭合**语句（多半是漏了分号）
    """
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = _strip_leading_comments(buffer)
            buffer = ""
            if statement:
                yield statement
    if _strip_leading_comments(buffer):
        raise StudioError(
            "SQL 文件末尾存在未闭合语句（多半是漏了分号）",
            code=ErrorCode.DB_MIGRATION_FAILED,
            context={"tail": buffer.strip()[-200:]},
            remediation="补上分号；注意 CREATE TRIGGER 必须以 `END;` 收尾",
        )
