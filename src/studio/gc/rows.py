"""DB 行级保留期回收（T4.12 · §03.7.5）。

为什么分批
----------
``DELETE FROM system_logs WHERE ts < ?`` 一次删掉十万行会**持写锁几十秒** ——
四池 worker 的每一次认领（``UPDATE ... RETURNING``）都被挡在门外，前端 WS 也开始堆。
所以按 ≤ :data:`BATCH_ROWS` 行一批、每批一个隐式事务地删：总时长可能更长，
但**最长持锁时间是个常数**（连接是 autocommit，见 ``db/engine.py``）。

为什么每个表按自己那一列删
--------------------------
``system_logs`` 的 ``ts`` 与 ``created_at`` 是两个独立默认值、内容一样，但只有
``ts`` 有索引（``idx_logs_ts``）；``task_events`` / ``llm_calls`` 只有 ``created_at``
（``idx_events_created`` / ``idx_llm_cost``）。按**被索引的那一列**删，
否则每日 GC 就是三次全表扫 —— 在最该省的地方花钱（GC 无人值守地跑，没人看着它慢）。

``audit_ops`` 不在这里
----------------------
合规留痕，**永久**保留（§03.7.5）。它没有对应的 :class:`RowRule`，于是
"忘了加一条规则"这种失误不可能把它带进来。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from studio.core.clock import format_iso
from studio.gc.policy import RetentionPolicy

__all__ = [
    "BATCH_ROWS",
    "RowGcResult",
    "RowRule",
    "default_row_rules",
    "gc_rows",
]

#: 单条 ``DELETE`` 最多影响的行数（§03.7.5「分批 ≤5000 行/次」）
BATCH_ROWS: Final[int] = 5000


@dataclass(frozen=True, slots=True)
class RowRule:
    """一条"按时间删旧行"的规则。"""

    label: str
    table: str
    time_column: str
    days: int
    where: str = ""

    def cutoff(self, now: datetime) -> str:
        """保留期边界（UTC ISO 毫秒，与 DDL 的 ``strftime('%f')`` 同形）。"""
        return format_iso(now - timedelta(days=self.days))

    def predicate(self) -> str:
        extra = f" AND {self.where}" if self.where else ""
        return f"{self.time_column} < ?{extra}"


@dataclass(slots=True)
class RowGcResult:
    """一条规则的回收结果（``dry_run`` 时 ``deleted`` 是"**将要**删多少"）。"""

    label: str
    table: str
    days: int
    deleted: int
    batches: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "table": self.table,
            "days": self.days,
            "deleted": self.deleted,
            "batches": self.batches,
            "dry_run": self.dry_run,
        }


def default_row_rules(policy: RetentionPolicy) -> tuple[RowRule, ...]:
    """§03.7.5 里"按时间删行"的四条规则。

    ``debug`` 排在"系统日志"前面是**故意的**：先按 7 天把调试行清掉，第二条
    30 天的计数就只剩"真日志"，报告读起来是"debug 删了 X / 其余删了 Y"，
    而不是一个把两者混在一起的大数。
    """
    return (
        RowRule(
            label="debug 日志",
            table="system_logs",
            time_column="ts",
            days=policy.debug_logs_days,
            where="level = 'debug'",
        ),
        RowRule(
            label="系统日志",
            table="system_logs",
            time_column="ts",
            days=policy.system_logs_days,
        ),
        RowRule(
            label="LLM 调用流水",
            table="llm_calls",
            time_column="created_at",
            days=policy.llm_calls_days,
        ),
        RowRule(
            label="任务事件",
            table="task_events",
            time_column="created_at",
            days=policy.task_events_days,
        ),
    )


def gc_rows(
    connection: sqlite3.Connection,
    rules: tuple[RowRule, ...],
    *,
    now: datetime,
    dry_run: bool = False,
    batch_size: int = BATCH_ROWS,
) -> tuple[RowGcResult, ...]:
    """按规则逐条回收，返回每条的计数。

    :param dry_run: 只数不删（``SELECT count(*)``）—— 上生产前先看一眼"要删多少"
    """
    results: list[RowGcResult] = []
    for rule in rules:
        cutoff = rule.cutoff(now)
        predicate = rule.predicate()
        if dry_run:
            row = connection.execute(
                f"SELECT count(*) FROM {rule.table} WHERE {predicate}",
                (cutoff,),
            ).fetchone()
            results.append(
                RowGcResult(
                    label=rule.label,
                    table=rule.table,
                    days=rule.days,
                    deleted=int(row[0]) if row is not None else 0,
                    batches=0,
                    dry_run=True,
                )
            )
            continue

        deleted = 0
        batches = 0
        while True:
            # 表名 / 列名全部来自本模块的常量（不是外部输入）；值一律走占位符。
            cursor = connection.execute(
                f"DELETE FROM {rule.table} WHERE id IN ("
                f"SELECT id FROM {rule.table} WHERE {predicate} LIMIT ?)",
                (cutoff, batch_size),
            )
            affected = cursor.rowcount if cursor.rowcount > 0 else 0
            batches += 1
            deleted += affected
            if affected < batch_size:
                break
        results.append(
            RowGcResult(
                label=rule.label,
                table=rule.table,
                days=rule.days,
                deleted=deleted,
                batches=batches,
                dry_run=False,
            )
        )
    return tuple(results)
