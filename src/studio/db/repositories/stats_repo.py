"""总览台的统计查询（T4.2 · §04.4.5 第 1 行"队列长度、今日产量"）。

"今日"的口径必须先钉死
----------------------
取**本地日**（`Asia/Shanghai`），不是 UTC 日 —— 用户看的是"我这一天干了多少活"。
`tasks.created_at` 存的是 UTC（DDL 默认 `strftime('%Y-%m-%dT%H:%M:%fZ','now')`），
所以查询要把本地日边界**换算成 UTC ISO** 再比较。**不能**用
`substr(created_at,1,10)`：那样在 UTC+8 的 08:00 之前，"今天新建的"会被算进昨天，
而"今天明明建了 5 条，面板说 0"是最容易被当成"后端坏了"的一类偏差（陷阱 #75）。

为什么不用 `idx_tasks_created_day`
---------------------------------
那个索引建在 `substr(created_at,1,10)`（UTC 日）上，与本地日口径对不上。
本项目的 `tasks` 是"一天几条到几十条"的量级，全表扫描可忽略。真要上量时，
正确做法是把索引改成裸 `(created_at)` 让范围查询走索引，
**而不是**把口径改回 UTC（口径是给人看的，索引是给机器用的，两者冲突时改索引）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from studio.core.clock import format_iso, local_tz, utc_now

__all__ = ["StatsRepo", "TodayOutput", "local_day_window"]

#: 算作"出片"的状态（`published` 也含在内：发布过的当然已经出过片）
COMPLETED_STATUSES: Final[tuple[str, ...]] = ("completed", "published")


@dataclass(frozen=True, slots=True)
class TodayOutput:
    """今日产量（面板顶部那几个数字）。"""

    day: str
    window_start: str
    window_end: str
    created: int
    completed: int
    published: int
    failed: int
    by_status: dict[str, int]
    by_grade: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "created": self.created,
            "completed": self.completed,
            "published": self.published,
            "failed": self.failed,
            "by_status": dict(self.by_status),
            "by_grade": dict(self.by_grade),
        }


def local_day_window(now: datetime | None = None) -> tuple[str, str, str]:
    """本地日窗口 ⇒ `(day, utc_start, utc_end)`（左闭右开）。

    纯函数（不碰库、不碰时钟以外的外部状态）⇒ 单测可以直接钉一个时刻验边界。
    """
    moment = (now or utc_now()).astimezone(local_tz())
    start_local = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.strftime("%Y-%m-%d"),
        format_iso(start_local.astimezone(UTC)),
        format_iso(end_local.astimezone(UTC)),
    )


class StatsRepo:
    """`tasks` 的聚合读数（**只读**；不改任何状态）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def today_output(self, *, now: datetime | None = None) -> TodayOutput:
        """今日新建 / 出片 / 已发布 / 失败 + 按状态、按等级的分布。"""
        day, start, end = local_day_window(now)
        created = self._scalar(
            "SELECT count(*) FROM tasks WHERE created_at >= ? AND created_at < ?", (start, end)
        )
        by_status = self._group(
            "SELECT status, count(*) AS n FROM tasks WHERE finished_at >= ? AND finished_at < ?"
            " GROUP BY status",
            (start, end),
        )
        by_grade = self._group(
            "SELECT COALESCE(grade, '?') AS grade, count(*) AS n FROM tasks"
            " WHERE finished_at >= ? AND finished_at < ? AND status IN (?, ?) GROUP BY grade",
            (start, end, *COMPLETED_STATUSES),
        )
        completed = sum(count for status, count in by_status.items() if status in COMPLETED_STATUSES)
        return TodayOutput(
            day=day,
            window_start=start,
            window_end=end,
            created=created,
            completed=completed,
            published=by_status.get("published", 0),
            failed=by_status.get("failed", 0),
            by_status=by_status,
            by_grade=by_grade,
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    def _scalar(self, sql: str, params: tuple[Any, ...]) -> int:
        row = self._connection.execute(sql, params).fetchone()
        return 0 if row is None else int(row[0])

    def _group(self, sql: str, params: tuple[Any, ...]) -> dict[str, int]:
        return {str(row[0]): int(row[1]) for row in self._connection.execute(sql, params)}
