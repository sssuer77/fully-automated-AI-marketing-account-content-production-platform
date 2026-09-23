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
from collections.abc import Mapping, Sequence
from typing import Any, Final

from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import DirectionRow

__all__ = ["EDITABLE_COLUMNS", "DirectionRepo"]

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

#: 允许按列改写的字段（与 ``TopicRepo.EDITABLE_COLUMNS`` 同一手法）。
#:
#: ``batch_id`` / ``seq`` 不在里面：它们是**这一批方向怎么排的**，改一个方向
#: 的归属或序号会让「批次」这个组织单位失去意义。``status`` 也不在这里 ——
#: 它有自己的入口（:meth:`set_status`），混进来只会让「改标题」顺手把方向标成 dropped。
EDITABLE_COLUMNS: Final[frozenset[str]] = frozenset({"title", "rationale", "priority", "risk_flags"})


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

    def next_seq(self, batch_id: str) -> int:
        """批次内的下一个序号（空批次 ⇒ 1）。**人工加的方向排在模型产出的后面**。"""
        row = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM content_directions WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return int(row[0]) if row else 1

    def insert_manual(
        self,
        *,
        batch_id: str,
        title: str,
        rationale: str,
        priority: int = 100,
        risk_flags: Sequence[str] = (),
        grounded_on: Sequence[Mapping[str, Any]] = (),
        status: str = "open",
    ) -> DirectionRow:
        """单条写一个方向（人工写的 / 今日新闻挑出来的都走这里）⇒ 返回**写完之后**那一行。

        ``seq`` 由这里自己算（``next_seq``）：调用方不该为了插一行先去查一次最大值，
        那样"算序号"与"写行"之间就有了一个别人可以插进来的窗口。

        ``llm_model`` / ``prompt_version`` 留空 —— 人写的方向**没有**模型与提示词可溯源，
        填上任何一个都是假账（``audit_ops`` 里有这次人工新增）。

        ``grounded_on`` 给的是**纯数据**（``{type, ref_id, kind, quote}``，同
        :meth:`insert_batch`）：人工写的方向照旧空着（人没说依据，就不替他说），
        今日新闻挑出来的方向带着那条新闻的**事件总结**（见 ``topic_service._news_evidence``）。
        """
        if status not in STATUSES:
            raise ValueError(f"非法方向状态：{status}（合法：{sorted(STATUSES)}）")
        with transaction(self._connection, immediate=True):
            row = self._connection.execute(
                f"""
                INSERT INTO content_directions(
                    id, batch_id, seq, title, rationale, grounded_on_json, priority,
                    risk_flags_json, status
                )
                VALUES (?, ?, (
                    SELECT COALESCE(MAX(seq), 0) + 1 FROM content_directions WHERE batch_id = ?
                ), ?, ?, ?, ?, ?, ?)
                RETURNING {_SELECT_COLUMNS}
                """,
                (
                    new_ulid(),
                    batch_id,
                    batch_id,
                    title,
                    rationale,
                    json.dumps(list(grounded_on), ensure_ascii=False),
                    int(priority),
                    json.dumps(list(risk_flags), ensure_ascii=False),
                    status,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - RETURNING 一定会给一行
            raise RuntimeError(f"content_directions 插入没有回读行：{title}")
        return DirectionRow.from_row(row)

    def patch(self, *, direction_id: str, changes: Mapping[str, Any]) -> DirectionRow | None:
        """按列改写（只认 :data:`EDITABLE_COLUMNS`），返回**改完之后**那一行。

        ``changes`` 为空 ⇒ 一个字节都不写（空 PATCH 不该假装改了一次），照常把当前行
        读回来 —— 调用方拿到的形状因此永远一致。与 ``TopicRepo.patch`` 逐字同一取舍。
        """
        unknown = sorted(set(changes) - EDITABLE_COLUMNS)
        if unknown:
            raise ValueError(f"不可改写的列：{unknown}（可改：{sorted(EDITABLE_COLUMNS)}）")
        normalized = {
            ("risk_flags_json" if column == "risk_flags" else column): (
                json.dumps(list(value), ensure_ascii=False) if column == "risk_flags" else value
            )
            for column, value in changes.items()
        }
        if normalized:
            assignments = ", ".join(f"{column} = ?" for column in normalized)
            with transaction(self._connection, immediate=True):
                self._connection.execute(
                    f"UPDATE content_directions SET {assignments} WHERE id = ?",
                    (*normalized.values(), direction_id),
                )
        return self.get(direction_id)

    def delete(self, direction_id: str) -> int:
        """硬删一个方向 ⇒ 返回**被它带走的选题条数**。

        候选选题是 ``ON DELETE CASCADE`` 跟着走的（DDL 里那一条），所以这里要先把
        条数数出来再删 —— 删完就再也数不到了，而"这一下删掉了 7 条候选"正是面板
        必须如实告诉人的事。
        """
        with transaction(self._connection, immediate=True):
            counted = self._connection.execute(
                "SELECT COUNT(*) FROM topic_candidates WHERE direction_id = ?", (direction_id,)
            ).fetchone()
            cascaded = int(counted[0]) if counted else 0
            self._connection.execute("DELETE FROM content_directions WHERE id = ?", (direction_id,))
        return cascaded

    def clear(self) -> int:
        """清空所有方向 ⇒ 返回**被一起带走的选题条数**。

        与 :meth:`delete` 同一条：候选是 ``ON DELETE CASCADE`` 跟着走的，所以条数必须
        在删之前数出来 —— 删完就再也数不到了，而"这一下带走了 13 条候选"正是面板必须
        如实告诉人的事。
        """
        with transaction(self._connection, immediate=True):
            counted = self._connection.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()
            cascaded = int(counted[0]) if counted else 0
            self._connection.execute("DELETE FROM content_directions")
        return cascaded

    def count_topics(self, direction_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM topic_candidates WHERE direction_id = ?", (direction_id,)
        ).fetchone()
        return int(row[0]) if row else 0
