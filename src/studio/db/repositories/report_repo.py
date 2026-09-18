"""``reports`` / ``report_schedules`` 的读写（T5.7 · §03.3.19 / §03.3.20）。

``reports`` 行**只增不改**（除 ``applied_count``）
--------------------------------------------------
报告是"当时的结论"：同一周期的数字过一周再看已经不对了，但它记的是
"那天我们据此做了什么决定"。所以重算不覆盖，而是生成新行
（``UNIQUE (period, start_date, end_date)`` 挡住同周期重复插入 —— 幂等靠约束，
不靠调用方记得先查一次）。

``report_schedules.next_run_at`` 是**列**
----------------------------------------
与 ``publish_schedules`` 同一条（陷阱 #30）：重启丢计划是同一个病。
``NULL`` 表示"停用/未排期"，到期查询用 ``next_run_at <= now`` 永远取不到它 ——
这正是 T5.7 陷阱 178 点名的那一条（"把 NULL 当成该立刻跑"）。

编辑走**一条 UPDATE**
----------------------
``update()`` 把新的周期参数与重算出来的 ``next_run_at`` 写在同一条语句里
（陷阱 #33）：分成两条时，中间那一瞬库里是"新参数 + 旧时刻"。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.db.engine import transaction

__all__ = ["ReportRepo", "ReportRow", "ReportScheduleRepo", "ReportScheduleRow"]

_REPORT_COLUMNS: Final[str] = (
    "id, period, start_date, end_date, generated_at, trigger, task_count, publish_count, "
    "total_views, total_likes, total_comments, total_shares, avg_views, median_views, "
    "llm_cost_usd, tts_skip_ratio, summary_md, data_json, insights_json, artifacts_json, "
    "applied_count, coverage_json"
)

_RSCHED_COLUMNS: Final[str] = (
    "id, period, weekday, day_of_month, at_time, tz, lookback_days, include_json, enabled, "
    "is_builtin, next_run_at, last_run_at, last_report_id, last_result, fail_streak, "
    "created_by, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class ReportRow:
    """一行 ``reports``。"""

    id: str
    period: str
    start_date: str
    end_date: str
    generated_at: str | None = None
    trigger: str = "scheduled"
    task_count: int = 0
    publish_count: int = 0
    total_views: int | None = None
    total_likes: int | None = None
    total_comments: int | None = None
    total_shares: int | None = None
    avg_views: float | None = None
    median_views: float | None = None
    llm_cost_usd: float | None = None
    tts_skip_ratio: float | None = None
    summary_md: str = ""
    data_json: str = "{}"
    insights_json: str = "[]"
    artifacts_json: str = "[]"
    applied_count: int = 0
    coverage_json: str = "{}"

    @property
    def insights(self) -> list[dict[str, Any]]:
        """``insights_json`` 解析后的列表（坏 JSON ⇒ 空列表，**不抛**）。"""
        loaded = _json_any(self.insights_json)
        return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []

    @property
    def data(self) -> dict[str, Any]:
        loaded = _json_any(self.data_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def coverage(self) -> dict[str, Any]:
        loaded = _json_any(self.coverage_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def artifacts(self) -> list[str]:
        loaded = _json_any(self.artifacts_json)
        return [str(item) for item in loaded] if isinstance(loaded, list) else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "period": self.period,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "generated_at": self.generated_at,
            "trigger": self.trigger,
            "task_count": self.task_count,
            "publish_count": self.publish_count,
            "total_views": self.total_views,
            "total_likes": self.total_likes,
            "total_comments": self.total_comments,
            "total_shares": self.total_shares,
            "avg_views": self.avg_views,
            "median_views": self.median_views,
            "llm_cost_usd": self.llm_cost_usd,
            "tts_skip_ratio": self.tts_skip_ratio,
            "summary_md": self.summary_md,
            "data": self.data,
            "insights": self.insights,
            "artifacts": self.artifacts,
            "applied_count": self.applied_count,
            "coverage": self.coverage,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ReportRow:
        return cls(
            id=str(row["id"]),
            period=str(row["period"]),
            start_date=str(row["start_date"]),
            end_date=str(row["end_date"]),
            generated_at=_opt(row, "generated_at"),
            trigger=str(row["trigger"]),
            task_count=int(row["task_count"]),
            publish_count=int(row["publish_count"]),
            total_views=_int_or_none(row, "total_views"),
            total_likes=_int_or_none(row, "total_likes"),
            total_comments=_int_or_none(row, "total_comments"),
            total_shares=_int_or_none(row, "total_shares"),
            avg_views=_float_or_none(row, "avg_views"),
            median_views=_float_or_none(row, "median_views"),
            llm_cost_usd=_float_or_none(row, "llm_cost_usd"),
            tts_skip_ratio=_float_or_none(row, "tts_skip_ratio"),
            summary_md=str(row["summary_md"]),
            data_json=str(row["data_json"]),
            insights_json=str(row["insights_json"]),
            artifacts_json=str(row["artifacts_json"]),
            applied_count=int(row["applied_count"]),
            coverage_json=str(row["coverage_json"]),
        )


class ReportRepo:
    """``reports`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, report_id: str) -> ReportRow | None:
        row = self._connection.execute(
            f"SELECT {_REPORT_COLUMNS} FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        return None if row is None else ReportRow.from_row(row)

    def require(self, report_id: str) -> ReportRow:
        row = self.get(report_id)
        if row is None:
            raise StudioError(
                f"报告不存在：{report_id}",
                code=ErrorCode.REPORT_NOT_FOUND,
                context={"report_id": report_id},
                remediation="刷新报告列表；它可能被删了，或者这个 id 是别处抄来的",
            )
        return row

    def find_period(self, *, period: str, start_date: str, end_date: str) -> ReportRow | None:
        """同周期那一行（幂等判据，与 ``UNIQUE (period, start_date, end_date)`` 一致）。"""
        row = self._connection.execute(
            f"SELECT {_REPORT_COLUMNS} FROM reports WHERE period = ? AND start_date = ? AND end_date = ?",
            (period, start_date, end_date),
        ).fetchone()
        return None if row is None else ReportRow.from_row(row)

    def list_recent(self, *, period: str | None = None, limit: int = 30) -> list[ReportRow]:
        """报告列表（**新到旧**；``rowid`` 兜同毫秒的排序 —— 陷阱 #55 的同一手法）。"""
        if period is None:
            rows = self._connection.execute(
                f"SELECT {_REPORT_COLUMNS} FROM reports ORDER BY generated_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_REPORT_COLUMNS} FROM reports WHERE period = ?"
                " ORDER BY generated_at DESC, rowid DESC LIMIT ?",
                (period, int(limit)),
            ).fetchall()
        return [ReportRow.from_row(row) for row in rows]

    def create(
        self,
        *,
        report_id: str | None = None,
        period: str,
        start_date: str,
        end_date: str,
        trigger: str,
        task_count: int,
        publish_count: int,
        total_views: int | None,
        total_likes: int | None,
        total_comments: int | None,
        total_shares: int | None,
        avg_views: float | None,
        median_views: float | None,
        llm_cost_usd: float | None,
        tts_skip_ratio: float | None,
        summary_md: str,
        data: Mapping[str, Any],
        insights: Sequence[Mapping[str, Any]],
        artifacts: Sequence[str],
        coverage: Mapping[str, Any],
    ) -> ReportRow:
        """插一行报告（**调用方先查 ``find_period``**：撞唯一索引会抛 sqlite3.IntegrityError）。"""
        identifier = report_id or new_ulid()
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                INSERT INTO reports(
                    id, period, start_date, end_date, trigger, task_count, publish_count,
                    total_views, total_likes, total_comments, total_shares, avg_views,
                    median_views, llm_cost_usd, tts_skip_ratio, summary_md, data_json,
                    insights_json, artifacts_json, coverage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    period,
                    start_date,
                    end_date,
                    trigger,
                    int(task_count),
                    int(publish_count),
                    total_views,
                    total_likes,
                    total_comments,
                    total_shares,
                    avg_views,
                    median_views,
                    llm_cost_usd,
                    tts_skip_ratio,
                    summary_md,
                    json.dumps(dict(data), ensure_ascii=False),
                    json.dumps([dict(item) for item in insights], ensure_ascii=False),
                    json.dumps(list(artifacts), ensure_ascii=False),
                    json.dumps(dict(coverage), ensure_ascii=False),
                ),
            )
        return self.require(identifier)

    def mark_applied(self, report_id: str, *, insights: Sequence[Mapping[str, Any]]) -> ReportRow:
        """记一次采纳（``insights_json`` 整列改写 + ``applied_count`` 加一）。

        ``applied_count`` 用 ``applied_count + 1`` 而不是"重算列表长度"：
        它是**累计次数**（同一条建议被采纳两次就记两次），而列表里那个
        ``applied`` 标记只是给面板看的当前状态。
        """
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                "UPDATE reports SET insights_json = ?, applied_count = applied_count + 1 WHERE id = ?",
                (json.dumps([dict(item) for item in insights], ensure_ascii=False), report_id),
            )
        return self.require(report_id)


@dataclass(frozen=True, slots=True)
class ReportScheduleRow:
    """一行 ``report_schedules``。"""

    id: str
    period: str
    at_time: str = "09:00"
    tz: str = "Asia/Shanghai"
    lookback_days: int = 7
    include: tuple[str, ...] = ()
    enabled: bool = True
    is_builtin: bool = False
    weekday: int | None = None
    day_of_month: int | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_report_id: str | None = None
    last_result: str | None = None
    fail_streak: int = 0
    created_by: str = "user"
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def is_failing(self) -> bool:
        """上一条结论是不是一次失败（``error:…``）。"""
        result = self.last_result
        return result is not None and result.startswith("error:")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "period": self.period,
            "weekday": self.weekday,
            "day_of_month": self.day_of_month,
            "at_time": self.at_time,
            "tz": self.tz,
            "lookback_days": self.lookback_days,
            "include": list(self.include),
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_report_id": self.last_report_id,
            "last_result": self.last_result,
            "fail_streak": self.fail_streak,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ReportScheduleRow:
        return cls(
            id=str(row["id"]),
            period=str(row["period"]),
            at_time=str(row["at_time"]),
            tz=str(row["tz"]),
            lookback_days=int(row["lookback_days"]),
            include=tuple(_json_list(row["include_json"])),
            enabled=bool(row["enabled"]),
            is_builtin=bool(row["is_builtin"]),
            weekday=None if row["weekday"] is None else int(row["weekday"]),
            day_of_month=None if row["day_of_month"] is None else int(row["day_of_month"]),
            next_run_at=_opt(row, "next_run_at"),
            last_run_at=_opt(row, "last_run_at"),
            last_report_id=_opt(row, "last_report_id"),
            last_result=_opt(row, "last_result"),
            fail_streak=int(row["fail_streak"]),
            created_by=str(row["created_by"]),
            created_at=_opt(row, "created_at"),
            updated_at=_opt(row, "updated_at"),
        )


class ReportScheduleRepo:
    """``report_schedules`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, schedule_id: str) -> ReportScheduleRow | None:
        row = self._connection.execute(
            f"SELECT {_RSCHED_COLUMNS} FROM report_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return None if row is None else ReportScheduleRow.from_row(row)

    def require(self, schedule_id: str) -> ReportScheduleRow:
        row = self.get(schedule_id)
        if row is None:
            raise StudioError(
                f"报告周期不存在：{schedule_id}",
                code=ErrorCode.SCHEDULE_NOT_FOUND,
                context={"schedule_id": schedule_id},
                remediation="刷新报告周期列表；它可能被别人删了",
            )
        return row

    def list_all(self) -> list[ReportScheduleRow]:
        """全部周期（**启用的排前面**，同组按下次触发升序；``NULL`` 排最后）。"""
        rows = self._connection.execute(
            f"SELECT {_RSCHED_COLUMNS} FROM report_schedules"
            " ORDER BY enabled DESC, next_run_at IS NULL, next_run_at, period"
        ).fetchall()
        return [ReportScheduleRow.from_row(row) for row in rows]

    def list_due(self, *, now: str, limit: int = 20) -> tuple[ReportScheduleRow, ...]:
        """到点该生成的周期（走 ``idx_rsched_due``）。

        **``next_run_at IS NULL`` 不在这里面**：``NULL <= '…'`` 在 SQL 里是 NULL（假），
        所以停用/未排期的行天然取不到 —— 这正是陷阱 178 要的语义
        （把 NULL 当"立刻跑"会让停用的计划每天都生成一份报告）。
        """
        rows = self._connection.execute(
            f"SELECT {_RSCHED_COLUMNS} FROM report_schedules"
            " WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?"
            " ORDER BY next_run_at LIMIT ?",
            (now, int(limit)),
        ).fetchall()
        return tuple(ReportScheduleRow.from_row(row) for row in rows)

    def list_unscheduled(self, *, limit: int = 20) -> tuple[ReportScheduleRow, ...]:
        """启用但**还没排期**的周期（``next_run_at IS NULL``）。

        这是 ``0006_seed.sql`` 留下的初值：三条内置计划建的时候还没有调度器，
        ``next_run_at`` 是 NULL。把它当成「永远不触发」是错的（陷阱 178）——
        正确做法是「未排期 ⇒ 现在算一次并落库」。**只取 ``enabled=1``**：
        停用的行 ``next_run_at`` 也是 NULL，那一条的意思正好相反（别排）。
        """
        rows = self._connection.execute(
            f"SELECT {_RSCHED_COLUMNS} FROM report_schedules"
            " WHERE enabled = 1 AND next_run_at IS NULL ORDER BY period LIMIT ?",
            (int(limit),),
        ).fetchall()
        return tuple(ReportScheduleRow.from_row(row) for row in rows)

    def create(
        self,
        *,
        schedule_id: str | None = None,
        period: str,
        weekday: int | None,
        day_of_month: int | None,
        at_time: str,
        tz: str,
        lookback_days: int,
        include: Sequence[str],
        enabled: bool,
        is_builtin: bool,
        next_run_at: str | None,
        created_by: str = "user",
    ) -> ReportScheduleRow:
        """建一条周期（``next_run_at`` 由调用方算好传进来 —— 仓储不认识时钟）。"""
        identifier = schedule_id or new_ulid()
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                INSERT INTO report_schedules(
                    id, period, weekday, day_of_month, at_time, tz, lookback_days, include_json,
                    enabled, is_builtin, next_run_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    period,
                    weekday,
                    day_of_month,
                    at_time,
                    tz,
                    int(lookback_days),
                    json.dumps(list(include), ensure_ascii=False),
                    1 if enabled else 0,
                    1 if is_builtin else 0,
                    next_run_at,
                    created_by,
                ),
            )
        return self.require(identifier)

    def update(
        self,
        schedule_id: str,
        *,
        period: str,
        weekday: int | None,
        day_of_month: int | None,
        at_time: str,
        tz: str,
        lookback_days: int,
        include: Sequence[str],
        enabled: bool,
        next_run_at: str | None,
    ) -> ReportScheduleRow:
        """整行改写（**含重算出来的 ``next_run_at``**，见模块注释）。"""
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                UPDATE report_schedules
                   SET period = ?, weekday = ?, day_of_month = ?, at_time = ?, tz = ?,
                       lookback_days = ?, include_json = ?, enabled = ?, next_run_at = ?
                 WHERE id = ?
                """,
                (
                    period,
                    weekday,
                    day_of_month,
                    at_time,
                    tz,
                    int(lookback_days),
                    json.dumps(list(include), ensure_ascii=False),
                    1 if enabled else 0,
                    next_run_at,
                    schedule_id,
                ),
            )
        return self.require(schedule_id)

    def set_enabled(self, schedule_id: str, *, enabled: bool, next_run_at: str | None) -> ReportScheduleRow:
        """只动启停（启用时重排、停用时清空 ``next_run_at``）。"""
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                "UPDATE report_schedules SET enabled = ?, next_run_at = ? WHERE id = ?",
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
        report_id: str | None = None,
        failed: bool = False,
    ) -> ReportScheduleRow:
        """记一次生成结论（``last_result`` / ``next_run_at`` / 连续失败数）。

        ``fail_streak`` 在**成功或跳过**时归零：没有数据不是故障，
        而"停用时空转"更不是 —— 它们都说明这条链路是活的。
        """
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                UPDATE report_schedules
                   SET last_run_at = ?, last_result = ?, next_run_at = ?,
                       last_report_id = COALESCE(?, last_report_id),
                       fail_streak = CASE WHEN ? THEN fail_streak + 1 ELSE 0 END
                 WHERE id = ?
                """,
                (at, result, next_run_at, report_id, 1 if failed else 0, schedule_id),
            )
        return self.require(schedule_id)

    def delete(self, schedule_id: str) -> bool:
        """删一条周期（返回"真的删掉了吗"）。

        ``AND is_builtin = 0`` 是**写死在 SQL 里**的：内建计划只能停用。
        把这条判断放在 service 层的话，任何一条新加的删除路径都得记得再判一次，
        而漏判的代价是"默认的周报被谁删了"变成一个答不出来的问题。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "DELETE FROM report_schedules WHERE id = ? AND is_builtin = 0", (schedule_id,)
            )
        return cursor.rowcount > 0


def _opt(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _int_or_none(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    return None if value is None else int(value)


def _float_or_none(row: sqlite3.Row, key: str) -> float | None:
    value = row[key]
    return None if value is None else float(value)


def _json_any(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _json_list(raw: Any) -> list[str]:
    loaded = _json_any(raw)
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]
