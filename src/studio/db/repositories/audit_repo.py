"""``audit_ops`` 仓储（T1.11 · §03.3.8 / §04.4.4 不变量 2）。

为什么审计单独一个仓储
----------------------
``system_logs`` 是"给人看的流水"（2Hz 广播、30 天回收），``audit_ops`` 是
"给追责用的凭据"（永久保留、带 ``before`` / ``after``）。两者的保留策略与读取
场景都不一样，混在一张表里，GC 一跑就会把凭据当流水删掉。

写入时机（本项目的硬约定）
--------------------------
**任何"改了别人能看到的东西"的动作都要留痕**：确认闸放行/退回/放弃、策略切换、
模板更新、池启停。留痕与状态变更**同事务**（由调用方保证）—— 分开写就会出现
"状态变了但查不到是谁改的"，那正是审计要防的事。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from studio.db.engine import transaction
from studio.db.models import AuditOpRow

__all__ = ["ACTORS", "FACET_COLUMNS", "RESULTS", "SOURCES", "AuditRepo"]

#: DDL 的 ``actor`` CHECK
ACTORS: Final[frozenset[str]] = frozenset({"user", "system", "auto", "worker"})

#: DDL 的 ``result`` CHECK
RESULTS: Final[frozenset[str]] = frozenset({"ok", "denied", "error"})

#: DDL 的 ``source`` CHECK
SOURCES: Final[frozenset[str]] = frozenset({"webui", "api", "cli", "worker", "auto"})

#: 可作筛选条件 / 可分面的列（**白名单**：列名是拼进 SQL 的，绝不允许来自请求）
FILTER_COLUMNS: Final[tuple[str, ...]] = ("task_id", "actor", "action", "target_type", "result")

#: ``distinct()`` 允许查的列（比 ``FILTER_COLUMNS`` 多 ``source``：它只用于展示筛选下拉）
FACET_COLUMNS: Final[tuple[str, ...]] = ("actor", "action", "target_type", "result", "source")

_COLUMNS: Final[str] = (
    "id, at, actor, actor_ref, action, target_type, target_id, task_id, "
    "before_json, after_json, result, reason, request_id, ip, source"
)

_INSERT: Final[str] = """
INSERT INTO audit_ops(
    actor, actor_ref, action, target_type, target_id, task_id,
    before_json, after_json, result, reason, request_id, ip, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class AuditRepo:
    """``audit_ops`` 的写入与查询入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        actor_ref: str | None = None,
        target_id: str | None = None,
        task_id: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        result: str = "ok",
        reason: str | None = None,
        request_id: str | None = None,
        ip: str | None = None,
        source: str = "webui",
    ) -> AuditOpRow:
        """写一条留痕并返回它（``id`` 是自增主键，故写后回读一次）。

        ``actor`` / ``result`` / ``source`` 在**写之前**校验：让 CHECK 约束在
        运行期炸掉，等于把"参数传错了"变成"审计写不进去"，而审计写不进去的时候
        往往正是最需要它的时候。
        """
        if actor not in ACTORS:
            raise ValueError(f"非法 actor：{actor}（合法：{sorted(ACTORS)}）")
        if result not in RESULTS:
            raise ValueError(f"非法 result：{result}（合法：{sorted(RESULTS)}）")
        if source not in SOURCES:
            raise ValueError(f"非法 source：{source}（合法：{sorted(SOURCES)}）")
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                _INSERT,
                (
                    actor,
                    actor_ref,
                    action,
                    target_type,
                    target_id,
                    task_id,
                    json.dumps(dict(before or {}), ensure_ascii=False),
                    json.dumps(dict(after or {}), ensure_ascii=False),
                    result,
                    reason,
                    request_id,
                    ip,
                    source,
                ),
            )
            audit_id = int(cursor.lastrowid or 0)
        row = self.get(audit_id)
        assert row is not None  # 刚写完，读不到说明事务没提交
        return row

    def get(self, audit_id: int) -> AuditOpRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM audit_ops WHERE id = ?", (audit_id,)
        ).fetchone()
        return None if row is None else AuditOpRow.from_row(row)

    def list_for_task(self, task_id: str, *, limit: int = 200) -> list[AuditOpRow]:
        """一个任务的操作史（``idx_audit_task`` 的口径：按时间倒序）。"""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM audit_ops WHERE task_id = ? ORDER BY at DESC, id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [AuditOpRow.from_row(row) for row in rows]

    def list_recent(self, *, limit: int = 100) -> list[AuditOpRow]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM audit_ops ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [AuditOpRow.from_row(row) for row in rows]

    def list_filtered(
        self,
        *,
        task_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        result: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditOpRow]:
        """按任务 / 操作人 / 动作 / 对象 / 结果筛选（审计页的主查询 · §04.5.11）。

        排序固定 ``at DESC, id DESC``：``at`` 只到毫秒，同一毫秒内的两条要靠 ``id``
        才能定序 —— 少了它，翻页会出现"同一条出现两次、另一条永远看不到"。
        """
        where, params = self._where(
            task_id=task_id,
            actor=actor,
            action=action,
            target_type=target_type,
            result=result,
            since=since,
        )
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM audit_ops{where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [AuditOpRow.from_row(row) for row in rows]

    def count_filtered(
        self,
        *,
        task_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        result: str | None = None,
        since: str | None = None,
    ) -> int:
        """同一组筛选条件下的总数（翻页要知道"还有多少"）。"""
        where, params = self._where(
            task_id=task_id,
            actor=actor,
            action=action,
            target_type=target_type,
            result=result,
            since=since,
        )
        row = self._connection.execute(f"SELECT count(*) FROM audit_ops{where}", params).fetchone()
        return 0 if row is None else int(row[0])

    def distinct(self, column: str) -> tuple[str, ...]:
        """某一列的取值清单（筛选下拉用）。列名必须来自 :data:`FACET_COLUMNS`。"""
        if column not in FACET_COLUMNS:
            raise ValueError(f"非法分面列：{column}（合法：{sorted(FACET_COLUMNS)}）")
        rows = self._connection.execute(
            f"SELECT DISTINCT {column} FROM audit_ops WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _where(
        *,
        task_id: str | None,
        actor: str | None,
        action: str | None,
        target_type: str | None,
        result: str | None,
        since: str | None,
    ) -> tuple[str, list[str]]:
        """把非空筛选拼成 ``WHERE`` 子句（**列名全部来自常量**，值一律占位符）。"""
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("task_id", task_id),
            ("actor", actor),
            ("action", action),
            ("target_type", target_type),
            ("result", result),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("at >= ?")
            params.append(since)
        return ("" if not clauses else " WHERE " + " AND ".join(clauses)), params
