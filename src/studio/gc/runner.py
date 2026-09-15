"""GC 编排（T4.12 · §03.7.5）。

一次 ``studio gc run`` 做四件事：

1. 按保留期删 DB 旧行（``system_logs`` / ``llm_calls`` / ``task_events``）；
2. 删"任务 completed 满 N 小时"的句子音频与人声母带（成片不在则**跳过**）；
3. 删过期的场景中间产物，失败任务的 ``.partial`` 不等 TTL；
4. TTS 缓存按 LRU 压到上限、已消费热点**移动**进 ``data/hot/archive/``。

``audit_ops`` 永不清理（合规留痕，§03.7.5）。

为什么先问库、再动文件
----------------------
"这个任务完成了没有、什么时候完成的、成片在哪"全是**库里的事实**。文件那一半
不自己猜（不拿 mtime 当完成时间）：库里查不到的任务，音频一个字节都不动。
库文件不存在（还没 ``db migrate``）⇒ 跳过 DB 相关部分并如实记一条 note，
而不是让 ``sqlite3.connect`` 顺手建出一个 0 字节的 ``studio.db``。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from studio.core.clock import format_iso, parse_iso, utc_now
from studio.core.paths import StudioPaths
from studio.db.engine import connect, footprint
from studio.gc.media import (
    FAILED_STATUSES,
    CompletedTask,
    MediaGcResult,
    gc_media,
)
from studio.gc.policy import RetentionPolicy
from studio.gc.rows import RowGcResult, default_row_rules, gc_rows

__all__ = ["GcReport", "run_gc"]


@dataclass(slots=True)
class GcReport:
    """一次回收的完整结论（CLI ``--json`` 直接吐它）。"""

    started_at: datetime
    duration_ms: int
    policy: RetentionPolicy
    dry_run: bool
    rows: tuple[RowGcResult, ...] = ()
    media: MediaGcResult | None = None
    db_bytes: int = 0
    db_freelist_bytes: int = 0
    notes: tuple[str, ...] = ()

    @property
    def refused(self) -> tuple[Any, ...]:
        """守卫拒绝项 —— 非空说明有候选想删白名单里的东西（是 bug，要报警）。"""
        return self.media.refused if self.media is not None else ()

    @property
    def ok(self) -> bool:
        return not self.refused

    @property
    def rows_deleted(self) -> int:
        return sum(item.deleted for item in self.rows)

    @property
    def freed_bytes(self) -> int:
        return self.media.freed_bytes if self.media is not None else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": format_iso(self.started_at),
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "policy": self.policy.to_dict(),
            "rows": [item.to_dict() for item in self.rows],
            "rows_deleted": self.rows_deleted,
            "media": self.media.to_dict() if self.media is not None else None,
            "freed_bytes": self.freed_bytes,
            "db_bytes": self.db_bytes,
            "db_freelist_bytes": self.db_freelist_bytes,
            "notes": list(self.notes),
        }


def run_gc(
    paths: StudioPaths,
    *,
    connection: sqlite3.Connection | None = None,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    rows: bool = True,
    media: bool = True,
) -> GcReport:
    """跑一轮回收。``connection`` 由调用方给（测试/服务端复用），否则自己开一个。"""
    began = time.monotonic()
    moment = now or utc_now()
    active = policy or RetentionPolicy()
    notes: list[str] = []

    owned = connection is None
    conn = connection
    if conn is None and paths.db_file.is_file():
        conn = connect(paths.db_file)
    elif conn is None:
        notes.append(f"数据库不存在（{paths.db_file}）：本轮只做文件回收，DB 行一条没动")
    try:
        row_results = (
            gc_rows(conn, default_row_rules(active), now=moment, dry_run=dry_run)
            if rows and conn is not None
            else ()
        )
        completed = _completed_tasks(conn, paths, moment, active) if media and conn is not None else ()
        failed = _failed_task_ids(conn) if media and conn is not None else ()
        db_bytes, freelist = footprint(conn) if conn is not None else (0, 0)
    finally:
        if owned and conn is not None:
            conn.close()

    media_result = (
        gc_media(
            paths,
            policy=active,
            now=moment,
            completed=completed,
            failed_task_ids=failed,
            dry_run=dry_run,
        )
        if media
        else None
    )
    return GcReport(
        started_at=moment,
        duration_ms=int((time.monotonic() - began) * 1000),
        policy=active,
        dry_run=dry_run,
        rows=row_results,
        media=media_result,
        db_bytes=db_bytes,
        db_freelist_bytes=freelist,
        notes=tuple(notes),
    )


def _completed_tasks(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    now: datetime,
    policy: RetentionPolicy,
) -> tuple[CompletedTask, ...]:
    """已完成且**最早那条 TTL 已到**的任务（两条 TTL 各自在 media 层再判一次）。

    ``COALESCE(finished_at, updated_at)``：``finished_at`` 理论上必填，
    但真出现 NULL 时"永远不回收"是个没人会发现的泄漏 —— 退到 ``updated_at``
    至少让它有被清掉的一天。
    """
    horizon = min(policy.sentence_audio_hours, policy.voice_master_hours)
    cutoff = format_iso(now - timedelta(hours=horizon))
    cursor = connection.execute(
        "SELECT id, COALESCE(finished_at, updated_at) AS done, context_json FROM tasks "
        "WHERE status = 'completed' AND COALESCE(finished_at, updated_at) < ?",
        (cutoff,),
    )
    tasks: list[CompletedTask] = []
    for row in cursor.fetchall():
        tasks.append(
            CompletedTask(
                task_id=str(row["id"]),
                finished_at=parse_iso(str(row["done"])),
                deliverable=_deliverable(row["context_json"], paths),
            )
        )
    return tuple(tasks)


def _failed_task_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    marks = ", ".join("?" for _ in FAILED_STATUSES)
    cursor = connection.execute(f"SELECT id FROM tasks WHERE status IN ({marks})", FAILED_STATUSES)
    return tuple(str(row["id"]) for row in cursor.fetchall())


def _deliverable(raw: object, paths: StudioPaths) -> Path | None:
    """从 ``context_json.final_path`` 取成片路径；没有 / 解析不了 ⇒ ``None``。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("final_path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (paths.home / path)
