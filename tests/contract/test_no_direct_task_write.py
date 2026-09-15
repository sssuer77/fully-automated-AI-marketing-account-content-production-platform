"""契约：``tasks`` 表**只允许** ``domain/task_service.py`` 写（§03.5.1 · T1.4）。

为什么值得一条静态检查
----------------------
状态机守卫、``task_events`` 审计链、``version`` 乐观锁，全部挂在
``TaskService.transition`` 这一个入口上。只要有一个模块图省事直接
``UPDATE tasks SET status=...``，上面三件事就同时失效，而且**不会报错**
—— 只是状态悄悄错、审计悄悄断。这类回退必须靠静态扫描拦住。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "studio"

#: 唯一允许写 ``tasks`` 的文件（相对 ``src/studio/``）
ALLOWED_WRITERS = frozenset({"domain/task_service.py"})

#: 命中即为越权写入
_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"UPDATE\s+OR\s+\w+\s+tasks\s+SET", re.IGNORECASE),
    re.compile(r"UPDATE\s+tasks\s+SET", re.IGNORECASE),
    re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+tasks\b", re.IGNORECASE),
    re.compile(r"REPLACE\s+INTO\s+tasks\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+tasks\b", re.IGNORECASE),
)


def _python_files() -> list[Path]:
    return sorted(
        path for path in SRC_ROOT.rglob("*.py") if "__pycache__" not in path.parts and path.is_file()
    )


def test_only_task_service_writes_tasks_table() -> None:
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

    assert not offenders, "tasks 表被越权写入（必须走 domain.task_service.TaskService）：\n" + "\n".join(
        offenders
    )


def test_allowlist_is_not_vacuous() -> None:
    """反向断言：白名单文件被删/改名后，上面那条用例会变成永远通过。"""
    writer = SRC_ROOT / "domain" / "task_service.py"
    text = writer.read_text(encoding="utf-8")
    assert "UPDATE tasks SET" in text
    assert "INSERT INTO task_events" in text


def test_no_module_writes_tasks_status_via_orm_style() -> None:
    """拦住"换个写法绕过"：``tasks.status = ...`` / ``tasks["status"] = ...``。"""
    pattern = re.compile(r"tasks\s*\[\s*['\"]status['\"]\s*\]\s*=", re.IGNORECASE)
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in _python_files()
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"疑似直接改写 tasks.status：{offenders}"


def test_state_machine_has_no_io_dependencies() -> None:
    """状态机必须是纯函数：导入 sqlite3 / httpx 之类就该被拦下。"""
    text = (SRC_ROOT / "domain" / "state_machine.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "import httpx", "import subprocess", "from studio.db"):
        assert forbidden not in text, f"state_machine.py 不该依赖 {forbidden}"
