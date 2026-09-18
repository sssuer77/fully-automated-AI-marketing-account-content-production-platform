"""报告与报告周期 REST 面（T5.7 · §04.6.5.2）。

九个端点 = 面板上现在能做的九件事
--------------------------------
报告：列表 / 详情 / 手动生成 / 采纳建议 / 导出（md|csv）；
周期：列表 / 新建 / 修改（含启停）/ 删除 / 立刻生成一次。

为什么"采纳建议"要单独一个端点、还要带序号
-----------------------------------------
``insights`` 是**报告那一刻**算出来的建议。采纳的语义是"我看了第 N 条，同意它"，
所以写进留痕的是 ``report_id + index`` 而不是那句话本身 —— 后者在报告被重算之后
就对不上了，而留痕的全部意义就是事后能对上。

为什么采纳走 ``PersonaStore`` 而不是直接写文件
---------------------------------------------
规格书 §04.6.5.2 点名的三条安全阀之一（留痕）。``PersonaStore`` 负责校验、备份、
留痕三件事；绕过它直接改 ``config/persona.yaml`` 的话，"谁把风格改了"就没有答案，
而且写坏的那一份连备份都没有（§04.5.8 裁定 158 的同一条理由）。

为什么导出返回文件而不是 JSON 里的一个字段
------------------------------------------
导出的用途是"拿出去"（发给别人、丢进表格）。塞进 JSON 再由前端另存，
会把换行与编码的责任放到浏览器那一侧 —— 而 CSV 的中文编码一旦错了，
打开的人看到的是乱码，且没有任何地方会报错。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi import Path as PathParam

from studio.app.deps import AppState
from studio.app.schemas.report import (
    ReportApplyResponse,
    ReportDeleteResponse,
    ReportDetail,
    ReportGenerateRequest,
    ReportList,
    ReportScheduleList,
    ReportSchedulePatch,
    ReportScheduleRunRequest,
    ReportScheduleRunResponse,
    ReportScheduleSpec,
    ReportScheduleView,
    report_detail,
    report_schedule_view,
    report_summary,
)
from studio.core.errors import StudioError
from studio.db.repositories.report_repo import ReportRepo, ReportScheduleRepo
from studio.services.report_service import (
    apply_insight,
    create_schedule,
    delete_schedule,
    export_report,
    generate_report,
    run_now,
    update_schedule,
)
from studio.services.scheduler_service import SCHEDULER_TICK_INTERVAL_SEC as SCHEDULER_TICK_SEC

__all__ = ["router"]

router = APIRouter(tags=["reports"])

#: 报告 / 周期的 id 形状。**含下划线**：``0006_seed.sql`` 的内置计划 id 是
#: ``rsched_weekly`` 这种可读写法（ULID 也能过，两者都收）。
_ID = PathParam(pattern=r"^[0-9A-Za-z_-]{1,64}$")


@router.get("/api/v1/reports", response_model=ReportList)
def list_reports(
    request: Request,
    period: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> ReportList:
    """报告列表（新到旧；``period`` 可选过滤）。"""
    state: AppState = request.app.state.studio
    repo = ReportRepo(state.connections.get())
    rows = repo.list_recent(period=period, limit=limit)
    items = [report_summary(row) for row in rows]
    return ReportList(
        items=items,
        counts={
            "total": len(items),
            "with_insights": sum(1 for item in items if item.insights_count > 0),
            "applied": sum(item.applied_count for item in items),
        },
        periods=["daily", "weekly", "monthly"],
        tick_sec=SCHEDULER_TICK_SEC,
    )


@router.get("/api/v1/reports/{report_id}", response_model=ReportDetail)
def get_report(request: Request, report_id: Annotated[str, _ID]) -> ReportDetail:
    """报告详情（正文 + 聚合数据 + 建议）。"""
    state: AppState = request.app.state.studio
    return report_detail(ReportRepo(state.connections.get()).require(report_id))


@router.post("/api/v1/reports/generate", response_model=ReportDetail)
def generate(request: Request, body: ReportGenerateRequest) -> ReportDetail:
    """手动生成一份（同周期已存在 ⇒ 返回那一份，**不重复插入**）。"""
    state: AppState = request.app.state.studio
    row = generate_report(
        connection=state.connections.get(),
        paths=state.paths,
        period=body.period,
        start_date=body.start,
        end_date=body.end,
        include=body.include,
        trigger="manual",
        actor="user",
        source="webui",
        log=state.logs.append,
    )
    return report_detail(row)


@router.post("/api/v1/reports/{report_id}/insights/{index}/apply", response_model=ReportApplyResponse)
def apply(
    request: Request,
    report_id: Annotated[str, _ID],
    index: Annotated[int, PathParam(ge=0)],
) -> ReportApplyResponse:
    """采纳第 ``index`` 条建议 ⇒ 写进人物偏好（``style_hint``）+ 留痕。

    已经采纳过的再点一次**什么都不做**（幂等）：往提示词里重复追加同一句话
    会让它越滚越长，而且从内容上完全看不出重复。
    """
    state: AppState = request.app.state.studio
    row = apply_insight(
        connection=state.connections.get(),
        persona=state.persona,
        report_id=report_id,
        index=index,
        actor="user",
        source="webui",
        log=state.logs.append,
    )
    applied = bool(row.insights[index].get("applied")) if index < len(row.insights) else False
    try:
        style_hint: str | None = state.persona.current().config.style_hint
    except StudioError:
        # 人物文件坏了也照样回结论：写偏好那一步已经成功或明确失败过，
        # 这里只是为了把当前偏好回显给面板 —— 读不出来不该把整个响应变成 500。
        style_hint = None
    return ReportApplyResponse(
        report_id=row.id,
        index=index,
        applied=applied,
        message="已采纳 ⇒ 写进人物偏好，下一轮 Planner 就会读到"
        if applied
        else "这条建议本来就采纳过，没有重复写",
        style_hint=style_hint,
        report=report_detail(row),
    )


@router.get("/api/v1/reports/{report_id}/export")
def export(
    request: Request,
    report_id: Annotated[str, _ID],
    format: Annotated[str, Query()] = "md",
) -> Response:
    """导出（``?format=md|csv``）—— 直接回文件，不塞进 JSON。"""
    state: AppState = request.app.state.studio
    filename, text, media_type = export_report(
        connection=state.connections.get(), report_id=report_id, fmt=format
    )
    return Response(
        content=text,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/v1/report-schedules", response_model=ReportScheduleList)
def list_schedules(request: Request) -> ReportScheduleList:
    """全部周期（启用的排前面，同组按下次触发升序）。"""
    state: AppState = request.app.state.studio
    rows = ReportScheduleRepo(state.connections.get()).list_all()
    items = [report_schedule_view(row) for row in rows]
    return ReportScheduleList(
        items=items,
        counts={
            "total": len(items),
            "enabled": sum(1 for item in items if item.enabled),
            "disabled": sum(1 for item in items if not item.enabled),
            "builtin": sum(1 for item in items if item.is_builtin),
            "failing": sum(1 for item in items if item.fail_streak > 0),
        },
        tick_sec=SCHEDULER_TICK_SEC,
    )


@router.post("/api/v1/report-schedules", response_model=ReportScheduleView)
def create(request: Request, body: ReportScheduleSpec) -> ReportScheduleView:
    """新建一条周期（``next_run_at`` 当场算好落库 · 陷阱 #30）。"""
    state: AppState = request.app.state.studio
    row = create_schedule(
        connection=state.connections.get(),
        spec=body.model_dump(),
        actor="user",
        source="webui",
        log=state.logs.append,
    )
    return report_schedule_view(row)


@router.patch("/api/v1/report-schedules/{schedule_id}", response_model=ReportScheduleView)
def patch(
    request: Request, schedule_id: Annotated[str, _ID], body: ReportSchedulePatch
) -> ReportScheduleView:
    """改一条周期（部分字段；``exclude_unset=True`` 是这一层的关键）。"""
    state: AppState = request.app.state.studio
    payload = body.model_dump(exclude_unset=True)
    reason = payload.pop("reason", None)
    row = update_schedule(
        connection=state.connections.get(),
        schedule_id=schedule_id,
        patch=payload,
        actor="user",
        reason=reason,
        source="webui",
        log=state.logs.append,
    )
    return report_schedule_view(row)


@router.delete("/api/v1/report-schedules/{schedule_id}", response_model=ReportDeleteResponse)
def remove(request: Request, schedule_id: Annotated[str, _ID]) -> ReportDeleteResponse:
    """删一条周期（**内置的不能删**，只能停用 ⇒ 400 说清原因）。"""
    state: AppState = request.app.state.studio
    removed = delete_schedule(
        connection=state.connections.get(), schedule_id=schedule_id, actor="user", source="webui"
    )
    return ReportDeleteResponse(
        schedule_id=schedule_id,
        deleted=removed,
        message="已删除（留痕里记着这一下）" if removed else "这条周期本来就不在（可能是别人已经删了）",
    )


@router.post("/api/v1/report-schedules/{schedule_id}/run_now", response_model=ReportScheduleRunResponse)
def run(
    request: Request, schedule_id: Annotated[str, _ID], body: ReportScheduleRunRequest | None = None
) -> ReportScheduleRunResponse:
    """立刻生成一次（``trigger='manual'``）。"""
    state: AppState = request.app.state.studio
    reason = None if body is None else body.reason
    outcome = run_now(
        connection=state.connections.get(),
        paths=state.paths,
        schedule_id=schedule_id,
        log=state.logs.append,
        now=None,
    )
    if reason:
        state.logs.append(
            level="info",
            source="report.scheduler",
            message=f"人工生成报告 {schedule_id}：{reason}",
            payload={"schedule_id": schedule_id, "result": outcome.result},
        )
    return ReportScheduleRunResponse.model_validate(outcome.to_dict())
