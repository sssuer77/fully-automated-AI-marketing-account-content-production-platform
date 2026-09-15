"""持久化层（T1.3 起）。

分层（``core -> db -> domain -> ...``）：本包**只**依赖 ``core``，
不导入 ``domain`` / ``services`` / ``app``。

模块归属
--------
- ``engine.py``  连接工厂 + 连接级 PRAGMA + 语句切分（唯一入口）
- ``migrate.py`` 迁移器与库自检（``studio db migrate|check``）
- ``lease.py``   租约 / 退避的时间算术（纯函数）
- ``queue.py``   四池队列内核 ``JobStore``（**唯一允许写 ``jobs``**，§03.4）
- ``models.py``  ✅ T1.9 行映射（dataclass，**不认识领域模型**）
- ``repositories/`` ✅ T1.9 仓储（**唯一允许出现 SQL 的地方**，§02.1）
- ``migrations/`` 纯 SQL、**只增不改**（§03.7.1）

后续任务落点（尚未实现）
------------------------
- ``write_queue.py``  T4.x 单写者串行化（R10）
"""

from __future__ import annotations

from studio.db.backup import (
    BackupFile,
    BackupResult,
    RestoreReport,
    backup_database,
    list_backups,
    restore_backup,
)
from studio.db.engine import (
    MIGRATIONS_DIR,
    PRAGMAS_FILE,
    apply_pragmas,
    connect,
    footprint,
    iter_statements,
    load_pragmas,
    transaction,
)
from studio.db.lease import (
    backoff_delay_ms,
    heartbeat_interval_sec,
    is_expired,
    lease_expires_at,
    not_before_at,
    poll_delay_ms,
)
from studio.db.migrate import (
    SCHEMA_MIGRATIONS_DDL,
    AppliedMigration,
    DbCheckItem,
    DbCheckReport,
    MigrationFile,
    MigrationPlan,
    MigrationReport,
    check,
    declared_objects,
    discover,
    doctor_check,
    migrate,
    plan,
    read_applied,
)
from studio.db.models import (
    BgmTrackRow,
    BrollClipRow,
    DirectionRow,
    FeedbackItemRow,
    HotItemRow,
    TopicRow,
    VoiceProfileRow,
)
from studio.db.queue import (
    Job,
    JobOutcome,
    JobStore,
    PoolRuntime,
    PoolStats,
    RateLimitState,
    ReclaimResult,
    WorkerHeartbeat,
)
from studio.db.repositories import (
    AssetStats,
    BgmTrackRepo,
    BrollClipRepo,
    DirectionRepo,
    FeedbackItemRepo,
    HotItemRepo,
    IngestAction,
    TopicRepo,
    UpsertResult,
    VoiceProfileRepo,
)
from studio.db.session import ThreadLocalConnections

__all__ = [
    "MIGRATIONS_DIR",
    "PRAGMAS_FILE",
    "SCHEMA_MIGRATIONS_DDL",
    "AppliedMigration",
    "AssetStats",
    "BackupFile",
    "BackupResult",
    "BgmTrackRepo",
    "BgmTrackRow",
    "BrollClipRepo",
    "BrollClipRow",
    "DbCheckItem",
    "DbCheckReport",
    "DirectionRepo",
    "DirectionRow",
    "FeedbackItemRepo",
    "FeedbackItemRow",
    "HotItemRepo",
    "HotItemRow",
    "IngestAction",
    "Job",
    "JobOutcome",
    "JobStore",
    "MigrationFile",
    "MigrationPlan",
    "MigrationReport",
    "PoolRuntime",
    "PoolStats",
    "RateLimitState",
    "ReclaimResult",
    "RestoreReport",
    "ThreadLocalConnections",
    "TopicRepo",
    "TopicRow",
    "UpsertResult",
    "VoiceProfileRepo",
    "VoiceProfileRow",
    "WorkerHeartbeat",
    "apply_pragmas",
    "backoff_delay_ms",
    "backup_database",
    "check",
    "connect",
    "declared_objects",
    "discover",
    "doctor_check",
    "footprint",
    "heartbeat_interval_sec",
    "is_expired",
    "iter_statements",
    "lease_expires_at",
    "list_backups",
    "load_pragmas",
    "migrate",
    "not_before_at",
    "plan",
    "poll_delay_ms",
    "read_applied",
    "restore_backup",
    "transaction",
]
