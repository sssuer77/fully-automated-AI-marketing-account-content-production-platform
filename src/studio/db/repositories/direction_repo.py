"""``content_directions`` 仓储（T1.9 · §03.3.4）。

批次（``batch_id``）是这一层的组织单位
--------------------------------------
一次 Planner 运行 = 一个 ``batch_id``。``seq`` 是批次内序号（1 起），
配合 ``idx_dir_batch(batch_id, seq)`` ⇒ "按批次顺序读回来"是索引扫描。
**不覆盖历史批次**：方向是"当时为什么这么做"的证据，重跑就是新批次。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any, Final

from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import DirectionRow

__all__ = ["DirectionRepo"]

_INSERT_SQL: Final[str] = """
INSERT INTO content_directions(
    id, batch_id, seq, title, rationale, grounded_on_json, priority,
    risk_flags_json, status, llm_model, prompt_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_COLUMNS: Final[str] = (
    "id, batch_id, seq, title, rationale, grounded_on_json, priority, "
    "risk_flags_json, status, llm_model, prompt_version, created_at"
)

#: ``status`` 合法取值（与 DDL CHECK 逐字一致）
STATUSES: Final[frozenset[str]] = frozenset({"open", "selected", "dropped"})


class DirectionRepo:
    """``content_directions`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @staticmethod
    def new_batch_id() -> str:
        return new_ulid()

    def insert_batch(
        self,
        *,
        batch_id: str,
        directions: Sequence[dict[str, Any]],
        llm_model: str | None = None,
        prompt_version: str | None = None,
    ) -> list[str]:
        """写入一批方向 ⇒ 返回按 ``seq`` 顺序的 id 列表。

        ``directions`` 是**已经翻译好的纯数据**（``{title, rationale, grounded_on,
        priority, risk_flags}``）：``db`` 不认识领域模型（§02.1）。
        """
        rows = [
            (
                new_ulid(),
                batch_id,
                index,
                str(item["title"]),
                str(item["rationale"]),
                json.dumps(list(item.get("grounded_on") or []), ensure_ascii=False),
                int(item.get("priority", 100)),
                json.dumps(list(item.get("risk_flags") or []), ensure_ascii=False),
                str(item.get("status", "open")),
                llm_model,
                prompt_version,
            )
            for index, item in enumerate(directions, start=1)
        ]
        if not rows:
            return []
        with transaction(self._connection, immediate=True):
            self._connection.executemany(_INSERT_SQL, rows)
        return [str(row[0]) for row in rows]

    def list_batch(self, batch_id: str) -> list[DirectionRow]:
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM content_directions WHERE batch_id = ? ORDER BY seq",
            (batch_id,),
        ).fetchall()
        return [DirectionRow.from_row(row) for row in rows]

    def list_batches(self, *, limit: int = 20) -> list[str]:
        """全部批次 id（**新到旧**）—— 面板的批次下拉。

        ``ORDER BY created_at DESC, rowid DESC``：同一批的方向共享 ``created_at``
        （毫秒级），只按时间排会在同毫秒内乱序（陷阱 #55 的同一手法）。
        """
        rows = self._connection.execute(
            "SELECT batch_id FROM content_directions "
            "GROUP BY batch_id ORDER BY MAX(created_at) DESC, MAX(rowid) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def latest_batch_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT batch_id FROM content_directions ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def get(self, direction_id: str) -> DirectionRow | None:
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM content_directions WHERE id = ?", (direction_id,)
        ).fetchone()
        return None if row is None else DirectionRow.from_row(row)

    def set_status(self, *, direction_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"非法方向状态：{status}（合法：{sorted(STATUSES)}）")
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute(
                "UPDATE content_directions SET status = ? WHERE id = ?", (status, direction_id)
            )
            return self._connection.total_changes > before

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            row = self._connection.execute("SELECT COUNT(*) FROM content_directions").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM content_directions WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0]) if row else 0
