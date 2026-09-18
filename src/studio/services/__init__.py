"""业务服务层（T1.7 起）—— 把 I/O、Agent、仓储缝在一起的那一层。

已落地：

- ``log_service``：`system_logs` 的唯一应用层写入口 + 只读查询；
- ``input_service``：``data/hot`` / ``data/feedback`` 的解析与归档（T1.9）；
- ``topic_service``：选题流水线（Planner → 方向 → Ideator → 选题池，T1.9）；
- ``script_service``：写稿流水线（Director → Writer → 逐句落库，T1.10）；
- ``review_service``：审稿/改稿/分级放行（Reviewer → 评分 → 四路去向，T1.11）；
- ``service_manager``：六进程编排（启动 / 优雅关停 / 查看，T1.12 + T5.5）。

其余服务按任务落点逐步补齐：``voice_service``(T2.x) / ``render_service``(T3.x) /
``publish_service``(T5.x) ……

依赖方向（§02.1）：``services`` 可以 import ``agents`` / ``db`` / ``domain`` / ``core``，
反过来一律不行 —— 所以 Agent 拿到的日志出口是注入的 :class:`LogSink` 回调，
而不是 ``LogService`` 本身。
"""

from __future__ import annotations

from studio.services.input_service import (
    FeedbackParseReport,
    HotParseReport,
    ImportReport,
    InputService,
    ParseIssue,
    parse_feedback_text,
    parse_hot_text,
)
from studio.services.log_service import DB_MIN_LEVEL, LogService, SystemLog, is_alert_code, ndjson_lines
from studio.services.review_service import (
    ApprovalOutcome,
    ReviewReport,
    ReviewService,
    read_latest_review,
)
from studio.services.script_service import DraftReport, ScriptService, read_active_script
from studio.services.service_manager import (
    DEFAULT_READY_TIMEOUT_SEC,
    DEFAULT_STOP_TIMEOUT_SEC,
    SERVICE_NAMES,
    PortProbe,
    Readiness,
    ServiceKind,
    ServiceManager,
    ServiceReadiness,
    ServiceSpec,
    ServiceStatus,
    StartReport,
    StopReport,
    default_manager,
    run_entry,
)
from studio.services.topic_service import (
    AnalyzeReport,
    DirectionOutcome,
    IdeateReport,
    TopicService,
    classify_by_keywords,
    db_sentiment,
    direction_spec_from_row,
    topic_spec_from_row,
)

__all__ = [
    "DB_MIN_LEVEL",
    "DEFAULT_READY_TIMEOUT_SEC",
    "DEFAULT_STOP_TIMEOUT_SEC",
    "SERVICE_NAMES",
    "AnalyzeReport",
    "ApprovalOutcome",
    "DirectionOutcome",
    "DraftReport",
    "FeedbackParseReport",
    "HotParseReport",
    "IdeateReport",
    "ImportReport",
    "InputService",
    "LogService",
    "ParseIssue",
    "PortProbe",
    "Readiness",
    "ReviewReport",
    "ReviewService",
    "ScriptService",
    "ServiceKind",
    "ServiceManager",
    "ServiceReadiness",
    "ServiceSpec",
    "ServiceStatus",
    "StartReport",
    "StopReport",
    "SystemLog",
    "TopicService",
    "classify_by_keywords",
    "db_sentiment",
    "default_manager",
    "direction_spec_from_row",
    "is_alert_code",
    "ndjson_lines",
    "parse_feedback_text",
    "parse_hot_text",
    "read_active_script",
    "read_latest_review",
    "run_entry",
    "topic_spec_from_row",
]
