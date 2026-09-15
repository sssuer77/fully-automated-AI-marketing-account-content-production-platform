"""媒资回收（T4.12 · §03.7.5）。

分层（``core -> db -> gc -> cli``）：本包依赖 ``core`` 与 ``db``，
**不**导入 ``services`` / ``app``（GC 是运维动作，不是业务服务）。

模块归属
--------
- ``policy.py`` 保留期快照 + **唯一**的删除闸口 ``guard_path``
- ``rows.py``   DB 行级回收（分批、按各表被索引的时间列）
- ``media.py``  文件级回收（句子音频 / 场景产物 / TTS 缓存 / 热点归档）
- ``runner.py`` 编排 + :class:`GcReport`（CLI ``--json`` 直接吐它）
"""

from __future__ import annotations

from studio.gc.media import (
    FAILED_STATUSES,
    REUSE_THRESHOLD,
    CompletedTask,
    FileAction,
    FileNote,
    MediaGcResult,
    gc_media,
    tree_size,
)
from studio.gc.policy import (
    PROTECTED_WORK_NAMES,
    RetentionPolicy,
    guard_path,
    protected_roots,
    resolve_policy,
)
from studio.gc.rows import BATCH_ROWS, RowGcResult, RowRule, default_row_rules, gc_rows
from studio.gc.runner import GcReport, run_gc

__all__ = [
    "BATCH_ROWS",
    "FAILED_STATUSES",
    "PROTECTED_WORK_NAMES",
    "REUSE_THRESHOLD",
    "CompletedTask",
    "FileAction",
    "FileNote",
    "GcReport",
    "MediaGcResult",
    "RetentionPolicy",
    "RowGcResult",
    "RowRule",
    "default_row_rules",
    "gc_media",
    "gc_rows",
    "guard_path",
    "protected_roots",
    "resolve_policy",
    "run_gc",
    "tree_size",
]
