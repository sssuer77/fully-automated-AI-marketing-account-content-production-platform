"""``review_scores`` 仓储（T1.11 · §03.3.8）。

为什么分数必须落库而不是只放内存
--------------------------------
"这条稿子为什么是 B 级"要能被复盘：规则通道算出来是几分、LLM 六维度各几分、
指出了哪些问题 —— 这三样只要缺一样，复盘就只能靠猜。所以一次审稿 = 一行，
``issues_json`` 与两个 ``*_detail_json`` 一起写。

``UNIQUE (script_id, round_no)`` 的含义
---------------------------------------
同一版稿子的**同一轮**只能有一个结论。重跑审稿会撞唯一键 ⇒
:class:`sqlite3.IntegrityError`，这是**故意的**：两份互相矛盾的分数比"这次没记上"
危险得多（面板会随机显示其中一份）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Final

from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import ReviewRow

__all__ = ["ReviewRepo"]

_COLUMNS: Final[str] = (
    "id, task_id, script_id, round_no, rule_total, rule_detail_json, llm_total, "
    "llm_detail_json, total, grade, decision, issues_json, llm_model, prompt_version, created_at"
)

_INSERT: Final[str] = """
INSERT INTO review_scores(
    id, task_id, script_id, round_no, rule_total, rule_detail_json, llm_total,
    llm_detail_json, total, grade, decision, issues_json, llm_model, prompt_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class ReviewRepo:
    """``review_scores`` 的读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 写 ──────────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        task_id: str,
        script_id: str,
        round_no: int,
        rule_total: float,
        rule_detail: Mapping[str, Any],
        llm_total: float,
        llm_detail: Mapping[str, Any],
        total: float,
        grade: str,
        decision: str,
        issues: Sequence[Mapping[str, Any]],
        llm_model: str | None = None,
        prompt_version: str | None = None,
    ) -> ReviewRow:
        """写一次审稿结论；``(script_id, round_no)`` 重复 ⇒ :class:`sqlite3.IntegrityError`。"""
        review_id = new_ulid()
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                _INSERT,
                (
                    review_id,
                    task_id,
                    script_id,
                    round_no,
                    rule_total,
                    json.dumps(dict(rule_detail), ensure_ascii=False),
                    llm_total,
                    json.dumps(dict(llm_detail), ensure_ascii=False),
                    total,
                    grade,
                    decision,
                    json.dumps([dict(item) for item in issues], ensure_ascii=False),
                    llm_model,
                    prompt_version,
                ),
            )
        row = self.get(review_id)
        assert row is not None  # 刚写完，读不到说明事务没提交
        return row

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, review_id: str) -> ReviewRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM review_scores WHERE id = ?", (review_id,)
        ).fetchone()
        return None if row is None else ReviewRow.from_row(row)

    def latest_for_task(self, task_id: str) -> ReviewRow | None:
        """最近一次审稿（按 ``round_no`` 倒序 —— 同毫秒写入也不会乱序）。"""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM review_scores WHERE task_id = ? "
            "ORDER BY round_no DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else ReviewRow.from_row(row)

    def list_for_task(self, task_id: str) -> list[ReviewRow]:
        """一个任务的全部审稿结论（按轮次正序 —— UI 按时间线读）。"""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM review_scores WHERE task_id = ? ORDER BY round_no, rowid",
            (task_id,),
        ).fetchall()
        return [ReviewRow.from_row(row) for row in rows]
