"""``feedback_items`` 仓储（T1.9 · §03.3.3 / §04.1.7）。

幂等键为什么是 ``(source_file, content)`` 而不是 ``(source_file, line_no, text)``
------------------------------------------------------------------------------
DDL 里**没有** ``line_no`` 列（§03.3.3 冻结），所以 §04.1.7 那个键在 feedback 上
根本无法表达。改用"文件 + 原文"还**更稳**：编辑文件导致行号漂移时，
未改动的行不会重复入库；而原文完全相同的两条反馈本来也该合并。

``wants_json`` / ``complaints_json`` 的写入时机
-----------------------------------------------
解析阶段**不分类**（§04.1.7：禁止在解析阶段调 LLM），两列留空 ``[]``；
分类由 Planner 阶段批量做一次，再用 :meth:`apply_classification` 回填。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Final

from studio.db.engine import transaction
from studio.db.models import FeedbackItemRow

__all__ = ["FeedbackItemRepo"]

_INSERT_SQL: Final[str] = """
INSERT INTO feedback_items(
    id, source_file, platform, occurred_on, content, sentiment,
    wants_json, complaints_json, is_auto, source_publication_id
)
SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (
    SELECT 1 FROM feedback_items WHERE source_file = ? AND content = ?
)
"""

_SELECT_COLUMNS: Final[str] = (
    "id, source_file, platform, occurred_on, content, sentiment, wants_json, "
    "complaints_json, is_auto, source_publication_id, created_at"
)


class FeedbackItemRepo:
    """``feedback_items`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_many(self, rows: Sequence[FeedbackItemRow]) -> int:
        """批量入库（幂等）⇒ 返回**实际新增**行数。"""
        if not rows:
            return 0
        params = [
            (
                row.id,
                row.source_file,
                row.platform,
                row.occurred_on,
                row.content,
                row.sentiment,
                json.dumps(row.wants, ensure_ascii=False),
                json.dumps(row.complaints, ensure_ascii=False),
                int(row.is_auto),
                row.source_publication_id,
                row.source_file,
                row.content,
            )
            for row in rows
        ]
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.executemany(_INSERT_SQL, params)
            inserted = self._connection.total_changes - before
        return inserted

    def list_recent(self, *, limit: int = 200, unclassified_only: bool = False) -> list[FeedbackItemRow]:
        """最近反馈（``unclassified_only`` ⇒ 只取 ``sentiment IS NULL`` 的）。"""
        clause = "WHERE sentiment IS NULL " if unclassified_only else ""
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM feedback_items {clause}"
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [FeedbackItemRow.from_row(row) for row in rows]

    def apply_classification(
        self,
        *,
        item_id: str,
        sentiment: str,
        wants: Sequence[str],
        complaints: Sequence[str],
    ) -> bool:
        """回填分类结果（Planner 阶段批量做一次之后）。"""
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute(
                "UPDATE feedback_items SET sentiment = ?, wants_json = ?, complaints_json = ? WHERE id = ?",
                (
                    sentiment,
                    json.dumps(list(wants), ensure_ascii=False),
                    json.dumps(list(complaints), ensure_ascii=False),
                    item_id,
                ),
            )
            return self._connection.total_changes > before

    def count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM feedback_items").fetchone()
        return int(row[0]) if row else 0
