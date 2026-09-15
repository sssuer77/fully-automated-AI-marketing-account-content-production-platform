"""契约：``jobs`` 表**只允许** ``db/queue.py`` 写（§03.4.1 · T1.5）。

为什么值得一条静态检查
----------------------
队列的全部并发正确性押在三件事上：单语句原子认领、租约、幂等键。
只要有一个模块图省事直接 ``UPDATE jobs SET status='succeeded'``，
租约就形同虚设（谁都能替别人收尾），``attempts`` 也不再可信
—— 而且**不会报错**，只是并发下偶尔丢单元、偶尔重复跑。这类回退必须静态拦住。

顺带锁住分层：``db`` 不许 import ``domain``（§02.4 依赖方向 core → db → domain）。
池名与单元类型在队列里一律以 ``str`` 透传，合法性由 DDL 的 ``CHECK`` 兜底；
一旦 ``db`` 反向依赖 ``domain``，DDL 就不再是唯一真相。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "studio"

#: 唯一允许写 ``jobs`` 的文件（相对 ``src/studio/``）
ALLOWED_WRITERS = frozenset({"db/queue.py"})

#: 命中即为越权写入
_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"UPDATE\s+OR\s+\w+\s+jobs\s+SET", re.IGNORECASE),
    re.compile(r"UPDATE\s+jobs\s+SET", re.IGNORECASE),
    re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+jobs\b", re.IGNORECASE),
    re.compile(r"REPLACE\s+INTO\s+jobs\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+jobs\b", re.IGNORECASE),
)


def _python_files() -> list[Path]:
    return sorted(
        path for path in SRC_ROOT.rglob("*.py") if "__pycache__" not in path.parts and path.is_file()
    )


def test_only_job_store_writes_jobs_table() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative in ALLOWED_WRITERS:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _WRITE_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{relative}:{line}: {match.group(0)}")

    assert not offenders, "jobs 表被越权写入（必须走 db.queue.JobStore）：\n" + "\n".join(offenders)


def test_allowlist_is_not_vacuous() -> None:
    """反向断言：白名单文件被删/改名后，上面那条用例会变成永远通过。"""
    writer = SRC_ROOT / "db" / "queue.py"
    text = writer.read_text(encoding="utf-8")
    assert "INSERT INTO jobs" in text
    assert "UPDATE jobs" in text
    assert "class JobStore" in text


def test_job_store_is_the_only_writer_exported() -> None:
    """``db/__init__.py`` 只能导出 ``JobStore``，不能导出裸连接或 SQL 片段。"""
    text = (SRC_ROOT / "db" / "__init__.py").read_text(encoding="utf-8")
    assert "JobStore" in text
    for forbidden in ("def execute", "def raw_sql"):
        assert forbidden not in text, f"db/__init__.py 不该暴露 {forbidden}"


def test_db_layer_does_not_import_domain() -> None:
    """依赖方向：``core → db → domain``；``db`` 反向依赖 ``domain`` 即分层破损。"""
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in (SRC_ROOT / "db").rglob("*.py")
        if "studio.domain" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"db 层反向依赖 domain：{offenders}"
