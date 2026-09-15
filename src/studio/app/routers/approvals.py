"""确认闸 REST 面（T4.4 · §04.4.4）—— 全流程**唯一**的人工节点。

控制器只做三件事
----------------
① 把 HTTP 请求翻成服务层调用；② 把服务层的逐条结果打包成响应；③ 让
:class:`~studio.core.errors.StudioError` 自己冒到应用级 handler 去（
`app/errors.py`）。

**业务规则一条都不在这里**：退回必填意见、只有 ``awaiting_approval`` 才能决断、
放行要写 ``audit_ops`` —— 这些住在 ``ReviewService.decide_approval``。抄进控制器
等于让每个新入口（WebUI / CLI / 批量 / 未来的 API 版本）各背一遍，抄漏一处就是
一条静默的合规漏洞（T1.11 施工修正）。

批量为什么"部分失败不回滚"
--------------------------
批量通过是**尽力而为**的人工操作：10 条里有一条被别人抢先处理了，剩下 9 条没理由
跟着失败。所以逐条决断、逐条留痕，把成功与失败**分别**如实报出来 —— 报成"整体失败"
会让人重按一次，而重按只会撞上 ``APPROVAL_NOT_PENDING`` 的噪音。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState, review_service_for
from studio.app.schemas.approvals import (
    ApprovalItem,
    ApprovalList,
    ApprovalResult,
    ApproveBatchBody,
    ApproveBody,
    BatchFailure,
    BatchResult,
    DiscardBody,
    RejectBody,
    RescueBody,
    RescueResult,
)
from studio.core.errors import StudioError
from studio.db.repositories import ApprovalRepo
from studio.domain.enums import ApprovalDecision

__all__ = ["DEFAULT_LIMIT", "MAX_PAGE", "router"]

router = APIRouter(tags=["approvals"])

#: 单页上限（待审列表是"人要一条条看"的东西，给太多反而看不完）
MAX_PAGE: int = 500

#: 默认页大小
DEFAULT_LIMIT: int = 200

#: ``approvals.status`` 的合法取值（与 DDL 的 CHECK 同源）
_STATUS_PATTERN = "^(pending|approved|rejected|discarded|expired)$"


@router.get("/api/v1/approvals", response_model=ApprovalList)
def list_approvals(
    request: Request,
    status: Annotated[str, Query(pattern=_STATUS_PATTERN, description="默认只看待审")] = "pending",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_LIMIT,
) -> ApprovalList:
    """待审 / 已决列表（``counts`` 是面板顶部的"待审 N 条"）。"""
    state: AppState = request.app.state.studio
    repo = ApprovalRepo(state.connections.get())
    rows = repo.list_by_status(status, limit=limit)
    return ApprovalList(
        approvals=[ApprovalItem.from_row(row) for row in rows],
        counts=repo.count_by_status(),
        limit=limit,
    )


@router.post("/api/v1/tasks/{task_id}/approve", response_model=ApprovalResult)
def approve_task(request: Request, task_id: str, body: ApproveBody | None = None) -> ApprovalResult:
    """确认放行：``awaiting_approval → queued_voice``。"""
    return _decide(request, task_id, ApprovalDecision.APPROVE, _comment(body))


@router.post("/api/v1/tasks/{task_id}/reject", response_model=ApprovalResult)
def reject_task(request: Request, task_id: str, body: RejectBody) -> ApprovalResult:
    """退回改稿：``awaiting_approval → editing``（``revision_round + 1``，意见必填）。"""
    return _decide(request, task_id, ApprovalDecision.REJECT, body.comment)


@router.post("/api/v1/tasks/{task_id}/discard", response_model=ApprovalResult)
def discard_task(request: Request, task_id: str, body: DiscardBody | None = None) -> ApprovalResult:
    """放弃：``awaiting_approval → discarded``（可经 ``/rescue`` 捞回）。"""
    return _decide(request, task_id, ApprovalDecision.DISCARD, _reason(body))


@router.post("/api/v1/approvals/approve_batch", response_model=BatchResult)
def approve_batch(request: Request, body: ApproveBatchBody) -> BatchResult:
    """批量通过：**逐条**决断 + 逐条写 ``audit_ops``，一条失败不影响其余。"""
    state: AppState = request.app.state.studio
    service = review_service_for(state)
    approved: list[ApprovalResult] = []
    failed: list[BatchFailure] = []
    for task_id in body.task_ids:
        try:
            outcome = service.decide_approval(
                task_id=task_id,
                decision=ApprovalDecision.APPROVE,
                comment=body.comment,
            )
        except StudioError as exc:
            failed.append(
                BatchFailure(
                    task_id=task_id,
                    code=str(exc.code),
                    message=exc.message,
                    remediation=exc.remediation,
                )
            )
            continue
        approved.append(ApprovalResult.from_outcome(outcome))
    return BatchResult(requested=len(body.task_ids), approved=approved, failed=failed)


@router.post("/api/v1/tasks/{task_id}/rescue", response_model=RescueResult)
def rescue_task(request: Request, task_id: str, body: RescueBody | None = None) -> RescueResult:
    """捞回：``discarded`` / ``canceled`` → ``pending``（误点放弃的唯一补救）。"""
    state: AppState = request.app.state.studio
    outcome = review_service_for(state).rescue_task(task_id=task_id, reason=_reason(body))
    return RescueResult.from_outcome(outcome)


def _decide(
    request: Request,
    task_id: str,
    decision: ApprovalDecision,
    comment: str | None,
) -> ApprovalResult:
    state: AppState = request.app.state.studio
    outcome = review_service_for(state).decide_approval(task_id=task_id, decision=decision, comment=comment)
    return ApprovalResult.from_outcome(outcome)


def _comment(body: ApproveBody | None) -> str | None:
    return None if body is None else body.comment


def _reason(body: DiscardBody | RescueBody | None) -> str | None:
    return None if body is None else body.reason
