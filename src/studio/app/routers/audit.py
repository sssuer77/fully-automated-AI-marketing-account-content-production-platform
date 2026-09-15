"""审计页 REST 面（T4.12 · §04.5.11）。

这一层只做三件事：把查询参数翻成仓储调用、组装分页、让 ``StudioError`` 自己
冒到应用级 handler。**筛选的合法性一条都不在这里** —— 列名走
``AuditRepo`` 的白名单（``FACET_COLUMNS``），值一律占位符，纵深防御在仓储那侧。

为什么是"读"而不是"写"
----------------------
审计是**别人写、这里读**：写入口散在确认闸 / 池启停 / 策略切换 / 人物库各处
（§04.4.4 不变量 2：改动了别人能看到的东西就要留痕）。审计页开一个写入口，
等于给"伪造留痕"开了条路。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState
from studio.app.schemas.audit import AuditFacets, AuditOpModel, AuditPage
from studio.db.repositories.audit_repo import AuditRepo

__all__ = ["router"]

router = APIRouter(tags=["audit"])

#: 单页上限（审计行带 ``before`` / ``after``，比日志行重得多）
MAX_LIMIT = 500


def _repo(request: Request) -> AuditRepo:
    state: AppState = request.app.state.studio
    return AuditRepo(state.connections.get())


@router.get("/api/v1/audit", response_model=AuditPage)
def list_audit(
    request: Request,
    task_id: Annotated[str | None, Query(description="只看某个任务的操作史")] = None,
    actor: Annotated[str | None, Query(description="user / system / auto / worker")] = None,
    action: Annotated[str | None, Query(description="如 task.approve / pool.pause")] = None,
    target_type: Annotated[str | None, Query(description="task / script / template / pool …")] = None,
    result: Annotated[str | None, Query(description="ok / denied / error")] = None,
    since: Annotated[str | None, Query(description="ISO 时间下界（含）")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditPage:
    """按任务 / 操作人 / 动作 / 对象 / 结果筛选留痕（新→旧）。"""
    repo = _repo(request)
    filters = {
        "task_id": task_id,
        "actor": actor,
        "action": action,
        "target_type": target_type,
        "result": result,
        "since": since,
    }
    items = repo.list_filtered(**filters, limit=limit, offset=offset)
    total = repo.count_filtered(**filters)
    return AuditPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[AuditOpModel.model_validate(item, from_attributes=True) for item in items],
    )


@router.get("/api/v1/audit/facets", response_model=AuditFacets)
def audit_facets(request: Request) -> AuditFacets:
    """筛选下拉的取值（动作 / 对象 / 操作人 / 结果）。"""
    repo = _repo(request)
    return AuditFacets(
        actors=list(repo.distinct("actor")),
        actions=list(repo.distinct("action")),
        target_types=list(repo.distinct("target_type")),
        results=list(repo.distinct("result")),
    )
