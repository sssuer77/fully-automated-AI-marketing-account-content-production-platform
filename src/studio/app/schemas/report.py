"""报告与报告周期契约（T5.7 · §04.6.5.2）。

列表项与详情分成两个模型
------------------------
``summary_md`` 是一份完整的 Markdown（几 KB），而列表要一次画十几条 ——
把详情塞进列表等于每次刷新都传十几份正文。列表只给"能不能点进去看"所需要的字段
（周期 / 区间 / 关键数字 / 建议条数），正文走 ``GET /reports/{id}``。

``include`` 与 ``period`` 的校验不在这里
---------------------------------------
规格书 §04.6.5.2 明写非法周期参数 ⇒ **400**（``REPORT_INVALID``），
而写在 Pydantic 里会先被拦成 **422** —— 422 在本项目里的意思是
"某个表单字段写错了，前端把红字标到那个框上"，而这里的判据是跨字段的
（weekly 必须有 weekday、monthly 的 day_of_month ≤ 28）。一个接口只有一条报错路径。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.db.repositories.report_repo import ReportRow, ReportScheduleRow

__all__ = [
    "InsightModel",
    "ReportApplyResponse",
    "ReportDeleteResponse",
    "ReportDetail",
    "ReportGenerateRequest",
    "ReportList",
    "ReportScheduleList",
    "ReportSchedulePatch",
    "ReportScheduleRunRequest",
    "ReportScheduleRunResponse",
    "ReportScheduleSpec",
    "ReportScheduleView",
    "ReportSummary",
    "insight_model",
    "report_detail",
    "report_schedule_view",
    "report_summary",
]


class _Body(BaseModel):
    """请求体基类（禁多余字段 —— 写错的键必须报错，不能静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


PeriodName = Literal["daily", "weekly", "monthly"]
IncludeName = Literal["publish", "metrics", "topics", "quality", "cost", "errors"]


class InsightModel(BaseModel):
    """一条决策建议（``applied`` 是本项目补的：面板据此显示"已采纳"）。"""

    kind: str
    statement: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: str
    suggested_action: str
    applied: bool = False
    applied_at: str | None = None
    applied_by: str | None = None


class ReportSummary(BaseModel):
    """报告列表里的一条（**不含正文**）。"""

    id: str
    period: str
    start_date: str
    end_date: str
    generated_at: str | None = None
    trigger: str
    task_count: int
    publish_count: int
    total_views: int | None = None
    median_views: float | None = None
    llm_cost_usd: float | None = None
    insights_count: int
    applied_count: int


class ReportDetail(ReportSummary):
    """报告详情（正文 + 聚合数据 + 建议 + 产物路径）。"""

    total_likes: int | None = None
    total_comments: int | None = None
    total_shares: int | None = None
    avg_views: float | None = None
    tts_skip_ratio: float | None = None
    summary_md: str
    data: dict[str, Any] = Field(default_factory=dict)
    insights: list[InsightModel] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)


class ReportList(BaseModel):
    """``GET /api/v1/reports`` —— 报告列表 + 计数 + 周期设置。"""

    items: list[ReportSummary]
    counts: dict[str, int]
    periods: list[str]
    tick_sec: float


class ReportGenerateRequest(_Body):
    """手动生成（``trigger='manual'``）。``start``/``end`` 缺省 ⇒ 按周期自动算。"""

    period: PeriodName = "weekly"
    start: str | None = None
    end: str | None = None
    include: list[IncludeName] | None = None


class ReportApplyResponse(BaseModel):
    """采纳一条建议的结论。"""

    report_id: str
    index: int
    applied: bool
    message: str
    style_hint: str | None = None
    report: ReportDetail


class ReportDeleteResponse(BaseModel):
    """删除周期的结论（``deleted=False`` ⇒ 这个 id 本来就不在，**不是错误**）。"""

    schedule_id: str
    deleted: bool
    message: str


class ReportScheduleSpec(_Body):
    """新建一条报告周期（§04.6.5.2 的 ``ReportScheduleSpec``）。"""

    period: PeriodName
    weekday: int | None = None
    day_of_month: int | None = None
    at_time: str = "09:00"
    tz: str = "Asia/Shanghai"
    lookback_days: int | None = None
    include: list[IncludeName] | None = None
    enabled: bool = True


class ReportSchedulePatch(_Body):
    """改一条周期（**部分字段**：只给 ``enabled`` 就是启停）。"""

    period: PeriodName | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    at_time: str | None = None
    tz: str | None = None
    lookback_days: int | None = None
    include: list[IncludeName] | None = None
    enabled: bool | None = None
    reason: str | None = None


class ReportScheduleView(BaseModel):
    """一条周期（``ReportScheduleRow.to_dict()`` 原样）。"""

    id: str
    period: str
    weekday: int | None = None
    day_of_month: int | None = None
    at_time: str
    tz: str
    lookback_days: int
    include: list[str]
    enabled: bool
    is_builtin: bool
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_report_id: str | None = None
    last_result: str | None = None
    fail_streak: int
    created_by: str
    created_at: str | None = None
    updated_at: str | None = None


class ReportScheduleList(BaseModel):
    """``GET /api/v1/report-schedules``。"""

    items: list[ReportScheduleView]
    counts: dict[str, int]
    tick_sec: float


class ReportScheduleRunRequest(_Body):
    """立刻生成一次（``reason`` 进日志）。"""

    reason: str | None = None


class ReportScheduleRunResponse(BaseModel):
    """立即生成的结论（``report_id`` 为空 ⇒ 这次没生成，看 ``result`` 与 ``note``）。"""

    schedule_id: str
    result: str
    report_id: str | None = None
    next_run_at: str | None = None
    note: str | None = None


def report_summary(row: ReportRow) -> ReportSummary:
    return ReportSummary(
        id=row.id,
        period=row.period,
        start_date=row.start_date,
        end_date=row.end_date,
        generated_at=row.generated_at,
        trigger=row.trigger,
        task_count=row.task_count,
        publish_count=row.publish_count,
        total_views=row.total_views,
        median_views=row.median_views,
        llm_cost_usd=row.llm_cost_usd,
        insights_count=len(row.insights),
        applied_count=row.applied_count,
    )


def insight_model(raw: dict[str, Any]) -> InsightModel:
    """``insights_json`` 里的一条 -> 契约模型（多余键**丢掉**，不是报错）。"""
    evidence = raw.get("evidence")
    return InsightModel(
        kind=str(raw.get("kind") or "topic"),
        statement=str(raw.get("statement") or ""),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        confidence=str(raw.get("confidence") or "low"),
        suggested_action=str(raw.get("suggested_action") or ""),
        applied=bool(raw.get("applied")),
        applied_at=None if raw.get("applied_at") is None else str(raw["applied_at"]),
        applied_by=None if raw.get("applied_by") is None else str(raw["applied_by"]),
    )


def report_detail(row: ReportRow) -> ReportDetail:
    return ReportDetail(
        **report_summary(row).model_dump(),
        total_likes=row.total_likes,
        total_comments=row.total_comments,
        total_shares=row.total_shares,
        avg_views=row.avg_views,
        tts_skip_ratio=row.tts_skip_ratio,
        summary_md=row.summary_md,
        data=row.data,
        insights=[insight_model(item) for item in row.insights],
        artifacts=row.artifacts,
        coverage=row.coverage,
    )


def report_schedule_view(row: ReportScheduleRow) -> ReportScheduleView:
    return ReportScheduleView.model_validate(row.to_dict())
