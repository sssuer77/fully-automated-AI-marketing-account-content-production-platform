"""领域 Pydantic 契约（§03.5.3）——DDL 的强类型视图。

规则（§3.5）：**DDL 是唯一真相，Pydantic 只做映射与校验**；
所有写路径必须经过 Pydantic（``extra="forbid"``），禁止裸 dict 落库。
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from studio.domain.enums import Grade, TaskKind, TaskPool, TaskStatus
from studio.domain.topics import HookType

__all__ = ["QualityReport", "TaskEventRead", "TaskPayload", "TaskRead"]


class RowLike(Protocol):
    """sqlite3.Row 的结构类型。

    typeshed 把 sqlite3.Row 标成 Sequence[Any] 而不是 Mapping，
    所以这里用协议 —— 只要求能按列名取值这一件事。
    """

    def __getitem__(self, key: str) -> Any: ...


def _opt(row: RowLike, key: str) -> Any:
    """取列值；列不存在 ⇒ ``None``（``sqlite3.Row`` 缺键抛 ``IndexError``）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _json_or(row: RowLike, key: str, default: Any) -> Any:
    """取 JSON 列并解析；``NULL`` / 空串 ⇒ ``default``。"""
    raw = _opt(row, key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(str(raw))


class TaskPayload(BaseModel):
    """``tasks.payload_json``：建任务时的输入契约（§03.5.3）。"""

    model_config = ConfigDict(extra="forbid")

    audience: str | None = None
    angle: str | None = None
    hook_type: HookType | None = None
    persona_id: str | None = None
    target_duration_ms: int = Field(default=180_000, ge=60_000, le=180_000)
    style: Literal["koubo", "duihua"] = "koubo"
    template_id: str = "douyin_9x16_default"
    voice_map: dict[str, str] = Field(
        default_factory=lambda: {"bigbear": "bigbear", "littlebear": "littlebear"}
    )  # 逻辑角色 → 音色 ID（可换）
    seed: int | None = None


class QualityReport(BaseModel):
    """``tasks.quality_json``：QC 结论 + 降级留痕（§03.5.3）。

    一期不做音画同步（C12），``av_sync_offset_ms`` 仍保留：二期/QC 用得上。
    """

    model_config = ConfigDict(extra="forbid")

    av_sync_offset_ms: int | None = None
    lufs: float | None = None
    true_peak: float | None = None
    phash_distance_avg: float | None = None
    dup_audit_pass: bool | None = None
    #: 成片是否叠加了水印（T5.1 · §06.4 门禁 1）。
    #:
    #: ``None`` 与 ``False`` 是两件事：前者是"没留判据"（旧任务的 quality_json
    #: 里就没这个字段），后者是"确定没贴"。发布门禁对两者同样拒绝
    #: —— 发出去就收不回来了。
    watermark_applied: bool | None = None
    degraded: bool = False
    degrade_reason: str | None = None


class TaskRead(BaseModel):
    """``tasks`` 行的强类型视图（§03.5.3）。

    ⚠️ 相对 §03.5.3 多了 ``last_healthy_status``（DDL 里有，断点重试要用）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: TaskKind
    title: str
    topic: str | None = None
    status: TaskStatus
    last_healthy_status: TaskStatus | None = None
    pool: TaskPool
    priority: int = 100
    version: int = 1
    grade: Grade | None = None
    score_total: float | None = None
    score_rule: float | None = None
    score_llm: float | None = None
    revision_round: int = 0
    progress: float = 0.0
    stage_detail: str | None = None
    # pydantic v2 对 BaseModel 默认值会逐实例深拷贝（已实测），故直接给实例；
    # 写 `Field(default_factory=TaskPayload)` 会触发 mypy 的 Field 重载误判。
    payload: TaskPayload = TaskPayload()
    context: dict[str, Any] = Field(default_factory=dict)
    quality: QualityReport = QualityReport()
    attempt_count: int = 0
    max_attempts: int = 3
    retry_from: TaskStatus | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> TaskRead:
        """把 ``tasks`` 的一行映射成模型（``*_json`` 列在此解析）。"""
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            topic=_opt(row, "topic"),
            status=row["status"],
            last_healthy_status=_opt(row, "last_healthy_status"),
            pool=row["pool"],
            priority=int(row["priority"]),
            version=int(row["version"]),
            grade=_opt(row, "grade"),
            score_total=_opt(row, "score_total"),
            score_rule=_opt(row, "score_rule"),
            score_llm=_opt(row, "score_llm"),
            revision_round=int(row["revision_round"]),
            progress=float(row["progress"]),
            stage_detail=_opt(row, "stage_detail"),
            payload=TaskPayload.model_validate(_json_or(row, "payload_json", {})),
            context=_json_or(row, "context_json", {}),
            quality=QualityReport.model_validate(_json_or(row, "quality_json", {})),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            retry_from=_opt(row, "retry_from"),
            error_code=_opt(row, "error_code"),
            error_message=_opt(row, "error_message"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=_opt(row, "started_at"),
            finished_at=_opt(row, "finished_at"),
            approved_at=_opt(row, "approved_at"),
            approved_by=_opt(row, "approved_by"),
        )


class TaskEventRead(BaseModel):
    """``task_events`` 的一行：状态机审计的**唯一**回放来源。"""

    id: int
    task_id: str
    from_status: TaskStatus | None = None
    to_status: TaskStatus
    actor: str
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: RowLike) -> TaskEventRead:
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            from_status=_opt(row, "from_status"),
            to_status=row["to_status"],
            actor=row["actor"],
            reason=_opt(row, "reason"),
            detail=_json_or(row, "detail_json", {}),
            created_at=row["created_at"],
        )
