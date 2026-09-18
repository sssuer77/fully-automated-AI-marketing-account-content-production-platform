"""``publish_schedules`` 的读写（T5.6 · §03.3.18）。

为什么单独一个仓储
------------------
与 ``publications`` / ``artifacts`` 同一条：一张表一个写入者。定时计划是
「这个账号打算在什么时间、把哪些内容、发到哪些平台」的**唯一凭据**，
散着写会让"它到底排在哪一刻"变成一个要去几个地方对的问题。

``next_run_at`` 是**列**，不是内存状态
-------------------------------------
陷阱 #30（重启丢计划）的根治办法就是它：``enabled / next_run_at`` 都在库里，
调度器崩了、API 重启了、机器断电了，起来之后照旧按同一张表往下跑。

编辑走**一条 UPDATE**
----------------------
``update()`` 把「新的模式/窗口/抖动」与「重算出来的 ``next_run_at``」写在**同一条
语句**里（陷阱 #33：只改参数不重算 ⇒ 不生效或立刻触发）。这不是风格问题：
分成两条语句时，中间那一瞬的 ``next_run_at`` 是按**旧**窗口算的，
而"窗口改了、时刻没改"正是那个陷阱要防的事。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.db.engine import transaction

__all__ = ["SCHEDULE_MODES", "ScheduleRepo", "ScheduleRow"]

#: 三种模式（与 DDL 的 ``CHECK (mode IN (...))`` 逐字一致）
SCHEDULE_MODES: Final[tuple[str, ...]] = ("at_time", "daily_window", "interval")

_COLUMNS: Final[str] = (
    "id, task_id, platforms_json, account_ids_json, mode, at_time, window_start, window_end, "
    "interval_hours, jitter_min, enabled, next_run_at, last_run_at, run_count, last_result, "
    "fail_streak, created_by, created_at, updated_at"
)

_INSERT: Final[str] = """
INSERT INTO publish_schedules(
    id, task_id, platforms_json, account_ids_json, mode, at_time, window_start, window_end,
    interval_hours, jitter_min, enabled, next_run_at, created_by
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """一行 ``publish_schedules``。"""

    id: str
    mode: str
    platforms: tuple[str, ...]
    account_ids: tuple[str, ...]
    jitter_min: int = 15
    enabled: bool = True
    task_id: str | None = None
    at_time: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    interval_hours: int | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    run_count: int = 0
    last_result: str | None = None
    fail_streak: int = 0
    created_by: str = "user"
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def window(self) -> tuple[str, str] | None:
        """``('18:00','21:30')``；非窗口模式 ⇒ ``None``。"""
        if self.window_start is None or self.window_end is None:
            return None
        return (self.window_start, self.window_end)

    @property
    def is_failing(self) -> bool:
        """上一条结论是不是一次失败（``error:…``）。"""
        result = self.last_result
        return result is not None and result.startswith("error:")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "platforms": list(self.platforms),
            "account_ids": list(self.account_ids),
            "mode": self.mode,
            "at_time": self.at_time,
            "window": None if self.window is None else list(self.window),
            "interval_hours": self.interval_hours,
            "jitter_min": self.jitter_min,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "fail_streak": self.fail_streak,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ScheduleRow:
        return cls(
            id=str(row["id"]),
            mode=str(row["mode"]),
            platforms=tuple(_json_list(row["platforms_json"])),
            account_ids=tuple(_json_list(row["account_ids_json"])),
            jitter_min=int(row["jitter_min"]),
            enabled=bool(row["enabled"]),
            task_id=_opt(row, "task_id"),
            at_time=_opt(row, "at_time"),
            window_start=_opt(row, "window_start"),
            window_end=_opt(row, "window_end"),
            interval_hours=None if row["interval_hours"] is None else int(row["interval_hours"]),
            next_run_at=_opt(row, "next_run_at"),
            last_run_at=_opt(row, "last_run_at"),
            run_count=int(row["run_count"]),
            last_result=_opt(row, "last_result"),
            fail_streak=int(row["fail_streak"]),
            created_by=str(row["created_by"]),
            created_at=_opt(row, "created_at"),
            updated_at=_opt(row, "updated_at"),
        )


class ScheduleRepo:
    """``publish_schedules`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 读 ──────────────────────────────────────────────────────────

    def get(self, schedule_id: str) -> ScheduleRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publish_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return None if row is None else ScheduleRow.from_row(row)

    def require(self, schedule_id: str) -> ScheduleRow:
        row = self.get(schedule_id)
        if row is None:
            raise StudioError(
                f"定时计划不存在：{schedule_id}",
                code=ErrorCode.SCHEDULE_NOT_FOUND,
                context={"schedule_id": schedule_id},
                remediation="刷新发布面板；计划被删之后这个 id 就不再有效",
            )
        return row

    def list_all(self) -> tuple[ScheduleRow, ...]:
        """全部计划（**启用的排前面**，同组内按下次触发时刻升序）。

        面板要的是"接下来会发生什么"，所以排序按 ``next_run_at`` 而不是创建时间；
        停用的排最后 —— 它们不会发生，混在中间只会让第一屏看不出重点。
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publish_schedules "
            "ORDER BY enabled DESC, next_run_at IS NULL, next_run_at ASC, created_at ASC"
        ).fetchall()
        return tuple(ScheduleRow.from_row(row) for row in rows)

    def list_due(self, *, now: str, limit: int = 20) -> tuple[ScheduleRow, ...]:
        """到点该跑的计划（走 ``idx_sched_due`` 那条部分索引）。

        ``next_run_at IS NULL`` **不**算到点：那是"没有下一期"（一次性计划已经跑完），
        把它当到点会让调度器每一拍都对同一行做一次无用功。
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publish_schedules "
            "WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "ORDER BY next_run_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        return tuple(ScheduleRow.from_row(row) for row in rows)

    # ── 写 ──────────────────────────────────────────────────────────

    def create(
        self,
        *,
        mode: str,
        platforms: Sequence[str],
        account_ids: Sequence[str],
        jitter_min: int = 15,
        task_id: str | None = None,
        at_time: str | None = None,
        window: tuple[str, str] | None = None,
        interval_hours: int | None = None,
        enabled: bool = True,
        next_run_at: str | None = None,
        created_by: str = "user",
        schedule_id: str | None = None,
    ) -> ScheduleRow:
        """建一条计划（``next_run_at`` **必须**由调用方算好传进来）。

        这里不自己算时刻：算时刻需要 ``now``，而"什么时候算的"决定了结果 ——
        让仓储去问时钟，测试就没法把那一刻摆到想要的相对位置上。
        """
        identifier = schedule_id or new_ulid()
        start, end = window if window is not None else (None, None)
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                _INSERT,
                (
                    identifier,
                    task_id,
                    json.dumps(list(platforms), ensure_ascii=False),
                    json.dumps(list(account_ids), ensure_ascii=False),
                    mode,
                    at_time,
                    start,
                    end,
                    interval_hours,
                    jitter_min,
                    1 if enabled else 0,
                    next_run_at,
                    created_by,
                ),
            )
        return self.require(identifier)

    def update(
        self,
        schedule_id: str,
        *,
        mode: str,
        platforms: Sequence[str],
        account_ids: Sequence[str],
        jitter_min: int,
        task_id: str | None,
        at_time: str | None,
        window: tuple[str, str] | None,
        interval_hours: int | None,
        enabled: bool,
        next_run_at: str | None,
    ) -> ScheduleRow:
        """整行改写（**含重算出来的 ``next_run_at``**，见模块注释）。"""
        start, end = window if window is not None else (None, None)
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                UPDATE publish_schedules
                   SET task_id = ?, platforms_json = ?, account_ids_json = ?, mode = ?,
                       at_time = ?, window_start = ?, window_end = ?, interval_hours = ?,
                       jitter_min = ?, enabled = ?, next_run_at = ?
                 WHERE id = ?
                """,
                (
                    task_id,
                    json.dumps(list(platforms), ensure_ascii=False),
                    json.dumps(list(account_ids), ensure_ascii=False),
                    mode,
                    at_time,
                    start,
                    end,
                    interval_hours,
                    jitter_min,
                    1 if enabled else 0,
                    next_run_at,
                    schedule_id,
                ),
            )
        return self.require(schedule_id)

    def set_enabled(self, schedule_id: str, *, enabled: bool, next_run_at: str | None) -> ScheduleRow:
        """只动启停（``next_run_at`` 一并写：启用时要重新排期，停用时要清空）。"""
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                "UPDATE publish_schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
                (1 if enabled else 0, next_run_at, schedule_id),
            )
        return self.require(schedule_id)

    def record_result(
        self,
        schedule_id: str,
        *,
        at: str,
        result: str,
        next_run_at: str | None,
        failed: bool = False,
    ) -> ScheduleRow:
        """记一次触发结论（``last_run_at`` / ``run_count`` / ``last_result`` / 连续失败数）。

        ``fail_streak`` 在**成功或跳过**时归零：被限频不是失败（§04.6.5.1），
        而"停用时空转"更不是 —— 它们都说明"这条链路是活的"。
        """
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                UPDATE publish_schedules
                   SET last_run_at = ?, run_count = run_count + 1, last_result = ?,
                       next_run_at = ?, fail_streak = CASE WHEN ? THEN fail_streak + 1 ELSE 0 END
                 WHERE id = ?
                """,
                (at, result, next_run_at, 1 if failed else 0, schedule_id),
            )
        return self.require(schedule_id)

    def delete(self, schedule_id: str) -> bool:
        """删一条计划（返回"真的删掉了吗"—— 不存在的 id 不算错）。"""
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute("DELETE FROM publish_schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0


def _opt(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]
