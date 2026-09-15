"""``hot_items`` 仓储（T1.9 · §03.3.2 / §04.1.7）。

幂等：``(source_file, line_no, raw_line)``
----------------------------------------
§04.1.7 定的键是 ``(source_file, line_no, sha1(text))``。这里用 ``raw_line``
代替 ``sha1(text)``：两者等价（raw_line 就是那一行的原文），而存原文还能让人
直接看懂"哪一行重复了"。

为什么不用"先 SELECT 再 INSERT"
------------------------------
那是两步，中间可能被另一个进程插进来（导入是 CLI/API 都可能触发的操作）。
这里用**单条** ``INSERT ... SELECT ... WHERE NOT EXISTS``：SQLite 的单条写语句
是原子的，重复导入天然安全，不需要额外索引、也不需要动冻结的迁移链。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Final

from studio.core.clock import now_iso
from studio.db.engine import transaction
from studio.db.models import HotItemRow

__all__ = ["HotItemRepo"]

_INSERT_SQL: Final[str] = """
INSERT INTO hot_items(id, source_file, line_no, title, heat, platform, raw_line, parse_ok)
SELECT ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (
    SELECT 1 FROM hot_items
     WHERE source_file = ? AND line_no = ? AND raw_line = ?
)
"""

_SELECT_COLUMNS: Final[str] = (
    "id, source_file, line_no, title, heat, platform, raw_line, parse_ok, "
    "used_by_direction_id, consumed_at, created_at"
)


class HotItemRepo:
    """``hot_items`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_many(self, rows: Sequence[HotItemRow]) -> int:
        """批量入库（幂等）⇒ 返回**实际新增**行数。"""
        if not rows:
            return 0
        params = [
            (
                row.id,
                row.source_file,
                row.line_no,
                row.title,
                row.heat,
                row.platform,
                row.raw_line,
                int(row.parse_ok),
                row.source_file,
                row.line_no,
                row.raw_line,
            )
            for row in rows
        ]
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.executemany(_INSERT_SQL, params)
            inserted = self._connection.total_changes - before
        return inserted

    def list_unconsumed(self, *, limit: int = 200) -> list[HotItemRow]:
        """未被消费**且解析成功**的热点（坏行不参与选题，裁定 67）。"""
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM hot_items "
            "WHERE consumed_at IS NULL AND parse_ok = 1 "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [HotItemRow.from_row(row) for row in rows]

    def list_unconsumed_sources(self) -> list[str]:
        """还有**可消费**热点的源文件（用于"消费后才归档"）。

        ``parse_ok = 1`` 不能漏：坏行永远不会被消费（``list_unconsumed`` 不选它），
        少了这个条件，只要文件里有一个坏行，这个文件就**永远归档不了**
        —— 每轮都重新解析、重新报同一条坏行（T1.9 施工期发现的真实缺陷）。
        """
        rows = self._connection.execute(
            "SELECT DISTINCT source_file FROM hot_items WHERE consumed_at IS NULL AND parse_ok = 1"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def list_consumed_sources(self) -> list[str]:
        """**已经全部消费**的源文件（可以安全归档）。

        判据是"这个文件在库里**有行**，且没有任何一行还等着消费"——比
        "本轮导入报告里出现过"更可靠：归档决策只该看库里的**事实**，
        不该依赖"这次运行有没有走导入那一步"。
        """
        rows = self._connection.execute(
            "SELECT DISTINCT source_file FROM hot_items source WHERE NOT EXISTS ("
            "  SELECT 1 FROM hot_items pending"
            "   WHERE pending.source_file = source.source_file"
            "     AND pending.consumed_at IS NULL AND pending.parse_ok = 1)"
            " ORDER BY source_file"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def mark_consumed(
        self, *, item_ids: Sequence[str], direction_id: str | None = None, consumed_at: str | None = None
    ) -> int:
        """标记消费（写入"被哪个方向用了"，用于复盘"当时为什么选这个方向"）。

        ``direction_id=None`` ⇒ **只写** ``consumed_at``：这条热点本轮被 Planner
        读过，但没有任何方向引用它。塞一个假的 direction_id 会让"这个热点支撑了
        哪个方向"在复盘时变成错的；``NULL`` 才是诚实的"参考过、没采纳"（T1.9 裁定 72）。
        """
        if not item_ids:
            return 0
        stamp = consumed_at or now_iso()
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.executemany(
                "UPDATE hot_items SET consumed_at = ?, used_by_direction_id = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                [(stamp, direction_id, item_id) for item_id in item_ids],
            )
            return self._connection.total_changes - before

    def count_unconsumed(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM hot_items WHERE consumed_at IS NULL AND parse_ok = 1"
        ).fetchone()
        return int(row[0]) if row else 0
