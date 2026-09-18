"""``topic_candidates`` 仓储（T1.9 · §03.3.5）。

选题池是"待人工挑"的池子，读路径比写路径重要
------------------------------------------
``idx_topic_pool(status, score DESC)`` 就是为"按分数倒序的瀑布流"建的索引 ——
所以 :meth:`list_pool` 的排序必须与索引**逐字一致**（``status`` 等值 + ``score DESC``），
否则 SQLite 会退化成全表扫描 + 排序。

一级去重（``DROP``）**不入库**
-----------------------------
``dedup_hash`` 已经命中的选题再插一行只会让瀑布流多一条噪声；而"为什么没进来"
的审计信息本来就在库里（按 ``dedup_hash`` 查得到原行）。二级去重（``DEMOTE``）
则照常入库，把"跟谁像、像多少"写进 ``similar_to_json``。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import TopicRow

__all__ = ["TopicRepo"]

_INSERT_SQL: Final[str] = """
INSERT INTO topic_candidates(
    id, direction_id, seq, title, hook_type, angle, exec_feasible, score, reason,
    dedup_hash, similar_to_json, status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_COLUMNS: Final[str] = (
    "id, direction_id, seq, title, hook_type, angle, exec_feasible, score, reason, "
    "dedup_hash, similar_to_json, status, task_id, selected_at, selected_by, created_at"
)

#: ``status`` 合法取值（与 DDL CHECK 逐字一致）
STATUSES: Final[frozenset[str]] = frozenset({"candidate", "selected", "queued", "rejected", "expired"})


class TopicRepo:
    """``topic_candidates`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_many(self, rows: Sequence[dict[str, Any]]) -> list[str]:
        """批量入库 ⇒ 返回新 id 列表（``rows`` 是已翻译好的纯数据）。"""
        params = [
            (
                new_ulid(),
                str(item["direction_id"]),
                int(item["seq"]),
                str(item["title"]),
                item.get("hook_type"),
                str(item["angle"]),
                int(bool(item.get("exec_feasible", True))),
                item.get("score"),
                item.get("reason"),
                item.get("dedup_hash"),
                json.dumps(list(item.get("similar_to") or []), ensure_ascii=False),
                str(item.get("status", "candidate")),
            )
            for item in rows
        ]
        if not params:
            return []
        with transaction(self._connection, immediate=True):
            self._connection.executemany(_INSERT_SQL, params)
        return [str(row[0]) for row in params]

    def list_pool(self, *, status: str = "candidate", limit: int = 200) -> list[TopicRow]:
        """瀑布流读取：与 ``idx_topic_pool(status, score DESC)`` 对齐。"""
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_candidates WHERE status = ? "
            "ORDER BY score DESC, seq LIMIT ?",
            (status, limit),
        ).fetchall()
        return [TopicRow.from_row(row) for row in rows]

    def list_by_direction(self, direction_id: str) -> list[TopicRow]:
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_candidates WHERE direction_id = ? ORDER BY seq",
            (direction_id,),
        ).fetchall()
        return [TopicRow.from_row(row) for row in rows]

    def list_for_dedup(self, *, limit: int = 500) -> list[TopicRow]:
        """去重比对用：**全部状态**的最近若干条（含 ``selected``/``queued``/``rejected``）。

        只比 ``candidate`` 是不够的 —— R15 要防的是"跟已经发过的选题撞车"，
        而那些早就是 ``queued``/``selected`` 了。
        """
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_candidates ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [TopicRow.from_row(row) for row in rows]

    def get(self, topic_id: str) -> TopicRow | None:
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_candidates WHERE id = ?", (topic_id,)
        ).fetchone()
        return None if row is None else TopicRow.from_row(row)

    def get_by_task(self, task_id: str) -> TopicRow | None:
        """按任务反查选题（一个选题只建一个任务，故最多一条）。

        C 级废弃时要把选题候选置 ``rejected``（"这条选题被机器判定为不值得做"），
        而服务层手上只有 ``task_id`` —— 让它去写 SQL 找 topic 是越界。
        """
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_candidates WHERE task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else TopicRow.from_row(row)

    def set_status(
        self,
        *,
        topic_id: str,
        status: str,
        task_id: str | None = None,
        selected_by: str | None = None,
        selected_at: str | None = None,
    ) -> bool:
        """改状态（勾选入队 / 驳回 / 过期）。

        ``selected_at`` **只在转入 ``selected`` 时写**，且省略时自动盖当前时间：
        与 ``HotItemRepo.mark_consumed`` 同一口径 —— "什么时候被选中的"是这一行的
        事实，不该指望每个调用方都记得传时间戳（漏传就是一条静默的空列）。
        """
        if status not in STATUSES:
            raise ValueError(f"非法选题状态：{status}（合法：{sorted(STATUSES)}）")
        stamp = (selected_at or now_iso()) if status == "selected" else None
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute(
                "UPDATE topic_candidates SET status = ?, "
                "task_id = COALESCE(?, task_id), "
                "selected_by = COALESCE(?, selected_by), "
                "selected_at = COALESCE(?, selected_at) "
                "WHERE id = ?",
                (status, task_id, selected_by, stamp, topic_id),
            )
            return self._connection.total_changes > before

    def demote_candidates(self, *, direction_id: str, factor: float) -> int:
        """把一个方向下**待选**的选题降权（T5.4 · §06.8 ②）。

        只动 ``status='candidate'``：已经被人选中的（``selected``/``queued``）不该被
        事后改分 —— 那会让"我当初为什么选它"变得无法复盘。``score IS NULL`` 的也不动
        （没有分数可降，乘出来还是 NULL，白白产生一次写）。

        ``factor`` 由**调用方**夹好上限（§06.8 的"幅度上限 20%"），这里只做形状校验：
        把业务上限写进仓储，等于让"上限是多少"有两个出处。
        """
        if not 0.0 < factor < 1.0:
            raise ValueError(f"降权系数必须在 (0, 1) 之间：{factor}")
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute(
                "UPDATE topic_candidates SET score = score * ? "
                "WHERE direction_id = ? AND status = 'candidate' AND score IS NOT NULL",
                (factor, direction_id),
            )
            return self._connection.total_changes - before

    def count_by_direction(self) -> dict[str, dict[str, int]]:
        """每个方向下各状态的条数 ⇒ ``{direction_id: {status: n}}``。

        面板要显示"这个方向出了 4 条、选中 1 条"，而逐个方向发一次
        ``list_by_direction`` 是 N+1 次查询 —— 方向最多 8 个，但这属于
        "本来一条 SQL 就能算完的事"。
        """
        rows = self._connection.execute(
            "SELECT direction_id, status, COUNT(*) FROM topic_candidates GROUP BY direction_id, status"
        ).fetchall()
        grouped: dict[str, dict[str, int]] = {}
        for row in rows:
            grouped.setdefault(str(row[0]), {})[str(row[1])] = int(row[2])
        return grouped

    def count_by_status(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) FROM topic_candidates GROUP BY status"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
