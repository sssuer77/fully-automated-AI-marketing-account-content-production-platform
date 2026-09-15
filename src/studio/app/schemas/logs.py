"""`GET /api/v1/logs` 的响应契约（T4.1 起有类型）。

`LogRow` 与 DB 层的 :class:`studio.services.log_service.SystemLog` **字段必须逐字一致**
（``tests/contract/test_web_contracts.py`` 会比对）：两边分别是"API 契约"与"库行"，
各有各的用途，但让它们漂移只会得到"前端少一个字段、排查半天"这种事故。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from studio.services.log_service import SystemLog

__all__ = ["LogPage", "LogRow"]


class LogRow(BaseModel):
    """`system_logs` 一行的 API 视图（WS 快照与 REST 分页共用同一形状）。"""

    id: int
    ts: str
    level: str
    source: str
    message: str
    task_id: str | None = None
    job_id: str | None = None
    stage: str | None = None
    unit_ref: str | None = None
    worker_id: str | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    seq_in_task: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: SystemLog) -> LogRow:
        """库行 → API 视图（**唯一**转换点，避免各处手抄字段）。"""
        return cls(**row.to_dict())


class LogPage(BaseModel):
    """一页日志（按 id 升序）。

    :param next_since_id: 下一页的游标（本页为空时回传请求里的 `since_id`，
        于是"没有新日志"不会把前端的游标清成 `None`）。
    """

    logs: list[LogRow]
    next_since_id: int | None
    limit: int
