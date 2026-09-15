"""LLM 调用记账（T1.8 · §01.2.4「每次调用写 ``llm_calls``」）。

职责边界
--------
- **写** ``llm_calls``：本模块是**唯一**写入方（契约测试锁死，与
  ``domain/task_service.py`` 写 ``tasks``、``pools/heartbeat.py`` 写
  ``worker_heartbeats`` 同一模式）。
- **读**：给预算闸门（R16）与成本面板提供聚合查询。
- **不判断**该不该调用 —— 那是 ``budget.py`` / ``gateway.py`` 的事。

为什么失败也要记账
------------------
``status='schema_invalid'`` / ``'http_error'`` 的行**同样计入 token 与成本**：
失败重试一样烧钱，若不记账，"反复重试"就能绕开预算闸门。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from studio.core.clock import now_iso
from studio.core.ids import new_ulid
from studio.core.logging import get_logger

__all__ = [
    "CallStatus",
    "LlmCallRecord",
    "LlmCallStore",
    "estimate_cost",
]

logger = get_logger("studio.agents.cost")

_INSERT_SQL: Final[str] = """
INSERT INTO llm_calls(
    id, task_id, job_id, agent, engine, model, is_local, prompt_version,
    input_tokens, output_tokens, cost_usd, latency_ms, status, error_message, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_COLUMNS: Final[str] = (
    "id, task_id, job_id, agent, engine, model, is_local, prompt_version, "
    "input_tokens, output_tokens, cost_usd, latency_ms, status, error_message, created_at"
)


class CallStatus(StrEnum):
    """``llm_calls.status`` 合法取值 —— 必须与 DDL 的 CHECK **逐字一致**。"""

    OK = "ok"
    SCHEMA_INVALID = "schema_invalid"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"


class LlmCallRecord(BaseModel):
    """一行 ``llm_calls``（DDL 的 Python 镜像，字段名逐字对应）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    task_id: str | None = None
    job_id: str | None = None
    agent: str
    engine: str
    model: str
    is_local: bool = False
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    status: CallStatus = CallStatus.OK
    error_message: str | None = None
    created_at: str = Field(default_factory=now_iso)

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def estimate_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_per_1k_in: float,
    cost_per_1k_out: float,
) -> float:
    """按 profile 的单价估算成本（USD，保留 6 位小数）。

    ``llm.yaml`` 默认单价为 0 ⇒ 本地通道与"未配置价格"的云端都是 0 成本，
    但 token 仍照记（成本面板展示"今日花费"时不会凭空为 0 而误导）。
    """
    cost = (input_tokens or 0) / 1000 * cost_per_1k_in + (output_tokens or 0) / 1000 * cost_per_1k_out
    return round(cost, 6)


class LlmCallStore:
    """``llm_calls`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 写 ──────────────────────────────────────────────────────────
    def record(self, record: LlmCallRecord) -> None:
        """落一行调用记录（**单语句、无显式事务**：sqlite3 autocommit + 隐式事务）。

        R10 纪律：这里不包 ``BEGIN`` —— 记账是"尽力而为"的旁路，
        绝不因为它去持有写锁、拖住主链路。
        """
        try:
            self._connection.execute(
                _INSERT_SQL,
                (
                    record.id,
                    record.task_id,
                    record.job_id,
                    record.agent,
                    record.engine,
                    record.model,
                    int(record.is_local),
                    record.prompt_version,
                    record.input_tokens,
                    record.output_tokens,
                    record.cost_usd,
                    record.latency_ms,
                    str(record.status),
                    record.error_message,
                    record.created_at,
                ),
            )
        except sqlite3.Error:
            # 记账失败**不得**中断主链路（宁可少一行账，不可丢一次产出）
            logger.exception(
                "llm.call_record_failed",
                agent=record.agent,
                status=str(record.status),
            )

    # ── 读（预算闸门 / 成本面板）────────────────────────────────────
    def tokens_for_task(self, task_id: str) -> int:
        """该任务**已烧掉**的 token 总量（含失败尝试）。"""
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
            FROM llm_calls WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def cost_usd_since(self, since_iso: str) -> float:
        """``created_at >= since_iso`` 的累计成本（默认用于"今日花费"）。"""
        row = self._connection.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) FROM llm_calls WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def count_for_task(self, task_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def recent(self, *, limit: int = 50, task_id: str | None = None) -> list[LlmCallRecord]:
        """最近若干行（成本面板 / 排障；倒序 = 最新的在前）。

        定序为什么是 ``created_at DESC, rowid DESC`` 而不是 ``..., id DESC``
        ---------------------------------------------------------------
        ``created_at`` 只到**毫秒**（:func:`studio.core.clock.format_iso`），
        而 ``new_ulid()`` 同一毫秒内是**随机**后缀 ⇒ 一次重试的三行往往落在
        同一毫秒里，用 ``id`` 定序会**随机**打乱"第几次尝试"。
        ``rowid`` 严格随插入递增 ⇒ 用它兜底，任何查询结果都唯一确定。
        """
        suffix = "WHERE task_id = ? " if task_id is not None else ""
        params: tuple[object, ...] = (task_id, limit) if task_id is not None else (limit,)
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM llm_calls {suffix}ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_record(row) for row in rows]


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> LlmCallRecord:
    mapping: Mapping[str, Any] = (
        row if isinstance(row, Mapping) else dict(zip(_SELECT_COLUMNS.split(", "), row, strict=True))
    )
    return LlmCallRecord(
        id=str(mapping["id"]),
        task_id=_opt_str(mapping["task_id"]),
        job_id=_opt_str(mapping["job_id"]),
        agent=str(mapping["agent"]),
        engine=str(mapping["engine"]),
        model=str(mapping["model"]),
        is_local=bool(mapping["is_local"]),
        prompt_version=_opt_str(mapping["prompt_version"]),
        input_tokens=_opt_int(mapping["input_tokens"]),
        output_tokens=_opt_int(mapping["output_tokens"]),
        cost_usd=None if mapping["cost_usd"] is None else float(mapping["cost_usd"]),
        latency_ms=_opt_int(mapping["latency_ms"]),
        status=CallStatus(str(mapping["status"])),
        error_message=_opt_str(mapping["error_message"]),
        created_at=str(mapping["created_at"]),
    )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)
