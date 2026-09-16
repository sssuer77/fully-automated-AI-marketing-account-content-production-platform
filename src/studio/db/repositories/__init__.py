"""仓储层（T1.9 起 · §02.1）—— **唯一允许出现 SQL 的地方**。

为什么把 SQL 关进这一个目录
--------------------------
"SQL 散落各处"的代价不是难看，而是**不变式没人守**：``tasks`` 的乐观锁、
``jobs`` 的租约、``llm_calls`` 的记账，全都靠"只有一个地方写"才成立。
契约测试（``tests/contract/test_no_direct_*_write.py``）扫的就是这条边界。

例外（已注明条款）：队列认领 SQL 在 ``db/queue.py``（§3.4，需单语句原子性）。

本包只依赖 ``core`` 与 ``db``，**不 import ``domain``** ——
行结构在 ``db/models.py``，领域模型的翻译在 ``services/``。
"""

from __future__ import annotations

from studio.db.repositories.approval_repo import ApprovalRepo
from studio.db.repositories.artifact_repo import ARTIFACT_COLUMNS, ArtifactRepo, ArtifactRow
from studio.db.repositories.asset_repo import (
    AssetStats,
    BgmTrackRepo,
    BrollClipRepo,
    IngestAction,
    UpsertResult,
    VoiceProfileRepo,
)
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.direction_repo import DirectionRepo
from studio.db.repositories.feedback_repo import FeedbackItemRepo
from studio.db.repositories.hot_repo import HotItemRepo
from studio.db.repositories.review_repo import ReviewRepo
from studio.db.repositories.script_repo import SENTENCE_COLUMNS, SavedScript, ScriptRepo
from studio.db.repositories.sentence_repo import SentenceProgress, SentenceRepo, TimelineSpan
from studio.db.repositories.stats_repo import StatsRepo, TodayOutput, local_day_window
from studio.db.repositories.topic_repo import TopicRepo

__all__ = [
    "ARTIFACT_COLUMNS",
    "SENTENCE_COLUMNS",
    "ApprovalRepo",
    "ArtifactRepo",
    "ArtifactRow",
    "AssetStats",
    "AuditRepo",
    "BgmTrackRepo",
    "BrollClipRepo",
    "DirectionRepo",
    "FeedbackItemRepo",
    "HotItemRepo",
    "IngestAction",
    "ReviewRepo",
    "SavedScript",
    "ScriptRepo",
    "SentenceProgress",
    "SentenceRepo",
    "StatsRepo",
    "TimelineSpan",
    "TodayOutput",
    "TopicRepo",
    "UpsertResult",
    "VoiceProfileRepo",
    "local_day_window",
]
