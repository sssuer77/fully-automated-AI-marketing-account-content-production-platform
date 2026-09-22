"""``topic_outlines`` 仓储（文案三级 · 二级：视频标题 + 核心论点）。

为什么是 upsert 而不是 insert + update
--------------------------------------
二级产物对**一个选题**只存在一份（``topic_id`` UNIQUE）：模型重跑一次、人手改一次，
语义都是"这条选题现在要说什么"，而不是"又攒了一版"。所以写入只有一条路径 ——
``INSERT ... ON CONFLICT(topic_id) DO UPDATE``，调用方不需要先查再判。

``llm_model`` / ``prompt_version`` 在人工改写时**不覆盖**：它们记的是"这一行最初是
哪个模型、哪版提示词产出的"，是溯源信息；人改过之后把模型名抹掉，等于让"这条是不是
模型写的"从此查不出来（``audit_ops`` 里有这次人工改动）。
"""

from __future__ import annotations

import sqlite3

from studio.core.clock import now_iso
from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import TopicOutlineRow

__all__ = ["OutlineRepo"]

_SELECT_COLUMNS: str = "id, topic_id, title, core_argument, llm_model, prompt_version, created_at, updated_at"

_UPSERT_SQL: str = f"""
INSERT INTO topic_outlines(
    id, topic_id, title, core_argument, llm_model, prompt_version, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(topic_id) DO UPDATE SET
    title = excluded.title,
    core_argument = excluded.core_argument,
    llm_model = COALESCE(excluded.llm_model, topic_outlines.llm_model),
    prompt_version = COALESCE(excluded.prompt_version, topic_outlines.prompt_version),
    updated_at = excluded.updated_at
RETURNING {_SELECT_COLUMNS}
"""


class OutlineRepo:
    """``topic_outlines`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, topic_id: str) -> TopicOutlineRow | None:
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM topic_outlines WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return None if row is None else TopicOutlineRow.from_row(row)

    def upsert(
        self,
        *,
        topic_id: str,
        title: str,
        core_argument: str,
        llm_model: str | None = None,
        prompt_version: str | None = None,
    ) -> TopicOutlineRow:
        """写一行（已存在就改写它）⇒ 返回**写完之后**那一行。"""
        with transaction(self._connection, immediate=True):
            row = self._connection.execute(
                _UPSERT_SQL,
                (
                    new_ulid(),
                    topic_id,
                    title,
                    core_argument,
                    llm_model,
                    prompt_version,
                    # ``updated_at`` 显式给：``ON CONFLICT`` 分支里的 ``excluded`` 指的是
                    # 「本来要插进去的那一行」，靠 DEFAULT 会让「更新」与「新建」共用同一个
                    # 时间戳来源 —— 写出来比推导出来好查。
                    now_iso(),
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - RETURNING 一定会给一行
            raise RuntimeError(f"topic_outlines upsert 没有回读行：{topic_id}")
        return TopicOutlineRow.from_row(row)

    def delete(self, topic_id: str) -> bool:
        """删掉一个选题的二级产物（``True`` = 真的少了一行）。"""
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute("DELETE FROM topic_outlines WHERE topic_id = ?", (topic_id,))
            return self._connection.total_changes > before
