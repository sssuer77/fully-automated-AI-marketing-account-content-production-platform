"""``domain/enums.py`` 单测：枚举取值必须与迁移 SQL 的 ``CHECK`` 逐字一致。

这是"改了 DDL 忘了改枚举"的**唯一**防线：值不一致时 SQLite 会在写入时抛
``IntegrityError``，那已经是运行时事故了。
"""

from __future__ import annotations

import re
from enum import StrEnum

import pytest

from studio.db.engine import MIGRATIONS_DIR
from studio.domain.enums import (
    ApprovalDecision,
    AutoApprovePolicy,
    Grade,
    JobStatus,
    PoolName,
    PublishStatus,
    TaskKind,
    TaskPool,
    TaskStatus,
    UnitType,
)

_QUOTED = re.compile(r"'([^']*)'")


def _migration_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("0*.sql")))


def _table_block(table: str) -> str:
    """从迁移 SQL 里抠出 ``CREATE TABLE <table> ( ... );`` 的列定义段。"""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS\s+{table}\s*\((.*?)\n\);",
        _migration_text(),
        re.DOTALL,
    )
    assert match is not None, f"迁移里找不到 {table} 的建表语句"
    return match.group(1)


def _check_in_values(table: str, column: str) -> set[str]:
    """解析 ``CHECK (<column> IN ('a','b'))`` 的字面量集合。"""
    block = _table_block(table)
    match = re.search(
        rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)\s*\)",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, f"{table}.{column} 没有 CHECK ... IN (...) 约束"
    return set(_QUOTED.findall(match.group(1)))


# ══════════════════════════════════════════════════════════════════════
# DDL 防漂移
# ══════════════════════════════════════════════════════════════════════


def test_task_status_matches_ddl_check() -> None:
    """16 态必须与 ``tasks.status`` 的 CHECK 逐字一致。"""
    assert {s.value for s in TaskStatus} == _check_in_values("tasks", "status")
    assert len(TaskStatus) == 16


def test_task_kind_matches_ddl_check() -> None:
    assert {k.value for k in TaskKind} == _check_in_values("tasks", "kind")


def test_task_pool_matches_ddl_check() -> None:
    """``tasks.pool`` 是 4 池 + ``none``（T1.4 施工裁定：与 ``PoolName`` 分开）。"""
    assert {p.value for p in TaskPool} == _check_in_values("tasks", "pool")
    assert {p.value for p in PoolName} | {"none"} == {p.value for p in TaskPool}


def test_grade_matches_ddl_check() -> None:
    assert {g.value for g in Grade} == _check_in_values("tasks", "grade")


def test_job_status_and_unit_type_match_ddl() -> None:
    assert {s.value for s in JobStatus} == _check_in_values("jobs", "status")
    assert {u.value for u in UnitType} == _check_in_values("jobs", "unit_type")
    assert {p.value for p in PoolName} == _check_in_values("jobs", "pool")


def test_approval_decision_covers_only_the_decidable_values() -> None:
    decided = {item.value for item in ApprovalDecision}
    assert decided <= _check_in_values("approvals", "status")
    assert decided == {"approved", "rejected", "discarded"}


def test_publish_status_matches_ddl_check() -> None:
    assert {s.value for s in PublishStatus} == _check_in_values("publications", "status")


# ══════════════════════════════════════════════════════════════════════
# 枚举自身的形状
# ══════════════════════════════════════════════════════════════════════


def test_pool_name_is_four_pools() -> None:
    """§01.4.4 的四池 —— 多一个少一个都会让队列参数表对不上。"""
    assert [p.value for p in PoolName] == ["draft", "voice", "render", "publish"]


def test_str_enum_behaves_like_str() -> None:
    """``StrEnum`` 必须能直接当字符串用（SQL 参数 / JSON 序列化都靠它）。"""
    as_str: str = TaskStatus.PENDING
    assert as_str == "pending"
    assert f"{TaskStatus.PENDING}" == "pending"
    assert PoolName.VOICE in {"voice"}


def test_auto_approve_policy_values() -> None:
    assert [p.value for p in AutoApprovePolicy] == ["off", "grade_a", "grade_ab"]


@pytest.mark.parametrize("enum_cls", [TaskStatus, TaskKind, TaskPool, Grade, PoolName, UnitType])
def test_no_duplicate_values(enum_cls: type[StrEnum]) -> None:
    values = [member.value for member in enum_cls]
    assert len(values) == len(set(values))
