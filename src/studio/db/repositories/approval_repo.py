"""``approvals`` 仓储（T1.11 · §04.4.4）—— 确认闸的唯一数据入口。

"待审"与"已决"是同一张表的两种行
--------------------------------
``status='pending'`` 是待办，``approved`` / ``rejected`` / ``discarded`` 是历史。
**不删行**：确认闸是全流程唯一的人工节点，"谁在什么时候把哪一稿放行了"是
复盘时最该查得到的东西，删掉就等于把审计线索丢了。

``decided_by`` 必须区分人与策略
-------------------------------
``user`` 与 ``auto_approve_A`` / ``auto_approve_AB`` 混在一起，会让"这个月人工
放行了几条"变成一个答不上来的问题 —— 而这正是评估"全自动模式敢不敢开"的依据。
"""

from __future__ import annotations

import sqlite3
from typing import Final

from studio.core.clock import now_iso
from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import ApprovalRow

__all__ = ["ApprovalRepo"]

#: 允许被"决断"的目标状态（``pending`` 之外的都是终局）
DECIDED_STATUSES: Final[frozenset[str]] = frozenset({"approved", "rejected", "discarded", "expired"})

_COLUMNS: Final[str] = (
    "id, task_id, script_id, status, grade, score_total, revision_round, "
    "requested_at, decided_at, decided_by, comment, auto_expire_at"
)

_INSERT: Final[str] = """
INSERT INTO approvals(
    id, task_id, script_id, status, grade, score_total, revision_round, auto_expire_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


class ApprovalRepo:
    """``approvals`` 的读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 写 ──────────────────────────────────────────────────────────────

    def request(
        self,
        *,
        task_id: str,
        script_id: str | None,
        grade: str | None,
        score_total: float | None,
        revision_round: int,
        auto_expire_at: str | None = None,
    ) -> ApprovalRow:
        """开一条待审（``grade`` / ``score_total`` / ``revision_round`` 是原文 §2.2⑦
        要求呈现给人工的三样 —— 少了它们，确认闸就只是"盲点通过"）。"""
        return self._insert(
            task_id=task_id,
            script_id=script_id,
            status="pending",
            grade=grade,
            score_total=score_total,
            revision_round=revision_round,
            auto_expire_at=auto_expire_at,
        )

    def record_auto_approval(
        self,
        *,
        task_id: str,
        script_id: str | None,
        grade: str | None,
        score_total: float | None,
        revision_round: int,
        decided_by: str,
    ) -> ApprovalRow:
        """**一次事务**写入一条"已自动放行"的记录（§04.4.4 不变量 2）。

        为什么不做成"先 ``request()`` 再 ``decide()``"：那是两个事务，中间挂掉会留下
        一条永远 ``pending`` 的记录，而任务其实已经进了 ``queued_voice`` ——
        人工面板上就会出现一个"待审"的幽灵，点进去还发现任务早跑了。
        """
        return self._insert(
            task_id=task_id,
            script_id=script_id,
            status="approved",
            grade=grade,
            score_total=score_total,
            revision_round=revision_round,
            auto_expire_at=None,
            decided_by=decided_by,
        )

    def _insert(
        self,
        *,
        task_id: str,
        script_id: str | None,
        status: str,
        grade: str | None,
        score_total: float | None,
        revision_round: int,
        auto_expire_at: str | None,
        decided_by: str | None = None,
    ) -> ApprovalRow:
        approval_id = new_ulid()
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                _INSERT,
                (
                    approval_id,
                    task_id,
                    script_id,
                    status,
                    grade,
                    score_total,
                    revision_round,
                    auto_expire_at,
                ),
            )
            if decided_by is not None:
                self._connection.execute(
                    "UPDATE approvals SET decided_at = ?, decided_by = ? WHERE id = ?",
                    (now_iso(), decided_by, approval_id),
                )
        row = self.get(approval_id)
        assert row is not None  # 刚写完，读不到说明事务没提交
        return row

    def decide(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: str,
        comment: str | None = None,
    ) -> ApprovalRow | None:
        """决断一条待审；**只对 ``pending`` 生效**（重复点击不会覆盖既有结论）。

        返回 ``None`` ⇒ 这条已经被人抢先决断过（或 id 不存在）。调用方据此报
        "已经处理过了"，而不是把第二个人的意见盖在第一个人的上面。
        """
        if status not in DECIDED_STATUSES:
            raise ValueError(f"非法决断状态：{status}（合法：{sorted(DECIDED_STATUSES)}）")
        with transaction(self._connection, immediate=True):
            before = self._connection.total_changes
            self._connection.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
                "comment = COALESCE(?, comment) WHERE id = ? AND status = 'pending'",
                (status, now_iso(), decided_by, comment, approval_id),
            )
            if self._connection.total_changes <= before:
                return None
        return self.get(approval_id)

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, approval_id: str) -> ApprovalRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return None if row is None else ApprovalRow.from_row(row)

    def pending_for_task(self, task_id: str) -> ApprovalRow | None:
        """该任务当前待审的那一条（``pending`` 最多一条 —— 状态机保证同一时刻只能进闸一次）。"""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE task_id = ? AND status = 'pending' "
            "ORDER BY requested_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else ApprovalRow.from_row(row)

    def list_pending(self, *, limit: int = 200) -> list[ApprovalRow]:
        """待审列表（``idx_appr_pending`` 就是为它建的）。"""
        return self.list_by_status("pending", limit=limit)

    def list_by_status(self, status: str, *, limit: int = 200) -> list[ApprovalRow]:
        """按状态列（确认闸面板既要看「待审」，也要能回看「已决」）。

        ``status`` 在这里**不**做白名单校验：它最终由 REST 层的
        ``Query(pattern=...)`` 卡住，而这里多一道校验只会把「库里存了个未来
        才有的状态」变成 500 —— 那本该是「查不到，返回空列表」。
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE status = ? ORDER BY requested_at, rowid LIMIT ?",
            (status, limit),
        ).fetchall()
        return [ApprovalRow.from_row(row) for row in rows]

    def list_for_task(self, task_id: str) -> list[ApprovalRow]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE task_id = ? ORDER BY requested_at, rowid",
            (task_id,),
        ).fetchall()
        return [ApprovalRow.from_row(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._connection.execute("SELECT status, COUNT(*) FROM approvals GROUP BY status").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
