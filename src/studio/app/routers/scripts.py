"""稿件面板 REST 面（T4.4 · §04.4.5 第 3 行）。

为什么"一次请求拿全"
--------------------
面板要同时显示：稿件全文、逐句表、**每一轮**的双通道评分、当前待审记录。拆成四个
端点会让面板自己"拼"视图 —— 而在改稿过程中（`editing → reviewing`）各端点之间
本来就可能读到不同版本，拼出来的页面会自相矛盾：正文是 v2、分数是 v1 的。
一次请求 + 一个连接读到底，矛盾就没有藏身之处。

读路径不碰 Agent
----------------
`read_active_script` / `ReviewRepo` / `ApprovalRepo` 都是纯读，不需要 LLM 网关。
这也是"面板能打开"与"模型能不能用"解耦的地方（T1.10 的同一手法）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState
from studio.app.schemas.scripts import (
    DiffSummaryModel,
    ScriptDetail,
    ScriptDiff,
    ScriptVersion,
    ScriptVersionList,
    SentenceDiffRow,
    approval_ref,
    review_view,
    script_view,
    sentence_view,
)
from studio.core.errors import ErrorCode, StudioError
from studio.db.models import ScriptRow
from studio.db.repositories import ApprovalRepo, ReviewRepo, ScriptRepo
from studio.domain.diff import diff_sentences, summarize_changes
from studio.domain.task_service import TaskService
from studio.services.script_service import read_active_script

__all__ = ["router"]

router = APIRouter(tags=["scripts"])


@router.get("/api/v1/scripts/{task_id}", response_model=ScriptDetail)
def get_script(request: Request, task_id: str) -> ScriptDetail:
    """稿件全文 + 逐句表 + 全部轮次评分 + 当前待审记录。

    任务不存在 ⇒ 404（``TASK_NOT_FOUND``）；任务在但还没有稿件 ⇒ 404
    （``REVIEW_SCRIPT_MISSING``）—— 两种"没有"分开报，面板才能给出不同的提示。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    task = TaskService(connection).get(task_id)
    payload = read_active_script(connection, task_id)
    if payload is None:
        raise StudioError(
            f"任务 {task_id} 还没有生效稿件",
            code=ErrorCode.REVIEW_SCRIPT_MISSING,
            context={"task_id": task_id, "status": task.status.value},
            remediation="先跑写稿（`studio script draft <topic_id>`）产出稿件",
        )
    script_row, sentence_rows = payload
    pending = ApprovalRepo(connection).pending_for_task(task_id)
    return ScriptDetail(
        task_id=task_id,
        task_status=task.status.value,
        revision_round=task.revision_round,
        script=script_view(script_row),
        sentences=[sentence_view(row) for row in sentence_rows],
        reviews=[review_view(row) for row in ReviewRepo(connection).list_for_task(task_id)],
        approval=None if pending is None else approval_ref(pending),
    )


@router.get("/api/v1/scripts/{task_id}/versions", response_model=ScriptVersionList)
def list_script_versions(request: Request, task_id: str) -> ScriptVersionList:
    """全部版本（"v1 → v2"下拉用；``is_active`` 标出当前生效的那一版）。"""
    state: AppState = request.app.state.studio
    repo = ScriptRepo(state.connections.get())
    return ScriptVersionList(
        task_id=task_id,
        versions=[
            ScriptVersion(
                version=row.version,
                is_active=row.is_active,
                word_count=row.word_count,
                grade=row.grade,
                score_total=row.score_total,
                revision_round=row.revision_round,
                sentence_count=repo.count_sentences(row.id),
                created_at=row.created_at,
            )
            for row in repo.list_versions(task_id)
        ],
    )


@router.get("/api/v1/scripts/{task_id}/diff", response_model=ScriptDiff)
def diff_script_versions(
    request: Request,
    task_id: str,
    from_version: Annotated[int, Query(ge=1, description="旧版本号")],
    to_version: Annotated[int, Query(ge=1, description="新版本号")],
) -> ScriptDiff:
    """两版逐句对照（改稿到底改了什么）。

    两个版本号都必须**显式**给：默认成"上一版 vs 当前版"看起来很贴心，但改稿
    可能一次跳两版（人工退回 + 自动重跑），默认值会让人对着错误的对照下结论。
    """
    state: AppState = request.app.state.studio
    repo = ScriptRepo(state.connections.get())
    old_row = _version_or_404(repo, task_id, from_version)
    new_row = _version_or_404(repo, task_id, to_version)
    changes = diff_sentences(repo.list_sentences(old_row.id), repo.list_sentences(new_row.id))
    summary = summarize_changes(changes)
    return ScriptDiff(
        task_id=task_id,
        from_version=from_version,
        to_version=to_version,
        changes=[
            SentenceDiffRow(
                op=change.op,
                old_seq=change.old_seq,
                new_seq=change.new_seq,
                old_text=change.old_text,
                new_text=change.new_text,
            )
            for change in changes
        ],
        summary=DiffSummaryModel(**summary.to_dict()),
    )


def _version_or_404(repo: ScriptRepo, task_id: str, version: int) -> ScriptRow:
    row = repo.get_by_version(task_id, version)
    if row is None:
        raise StudioError(
            f"任务 {task_id} 没有第 {version} 版稿件",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id, "version": version},
            remediation="先看 `GET /api/v1/scripts/{task_id}/versions` 里有哪些版本",
        )
    return row
