"""成片库 REST 面（T5.11 · §06.11 / §06.12）。

两个端点 = 面板上现在能做的两件事
---------------------------------
看（``GET /api/v1/library``）与**批量投递**（``POST /api/v1/library/publish``）。

**这里没有"点发布"**：真发布是 publish 池 worker 干的，本层只投作业 ——
与 :mod:`studio.app.routers.publish` 同一条边界。

为什么批量投递放在这一层，而不是让面板循环调单条那个端点
--------------------------------------------------------
面板循环调 N 次 ``POST /publish/tasks/{id}/enqueue`` 的代价有三条，都不是理论上的：
① 勾 20 条就是 20 个请求，中间断一次（刷新、切屏）会留下一半投了一半没投的状态，
而面板上没有任何地方记得"刚才投到第几条"；② 每一条的失败要在前端各拼一次文案，
于是"douyin 平台未启用"这句话会出现在前端；③ 上限（:data:`~studio.services.library_service.BATCH_MAX`）
只能在前端判，而前端判据与后端判据分叉时，用户看到的是"按钮能点，点了报错"。

所以：**一次请求，一次判据，逐条回结论**。

为什么投递**不看** ``publish.enabled``
--------------------------------------
与单条投递同一条（见 :mod:`studio.app.routers.publish` 的模块注释）：开关关着时投递
照样成功，作业会在 worker 那一侧带 ``PUBLISH_DISABLED`` 进死信。投递期直接拒绝的话，
面板上连作业都没有 —— 而"点了没反应"比"有一条能查的作业"难查得多。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState, publish_config_for
from studio.app.schemas.library import (
    NO_FINAL_HINT,
    LibraryBatchItemView,
    LibraryItemView,
    LibraryPublishRequest,
    LibraryPublishResponse,
    LibraryResponse,
)
from studio.app.schemas.publish import publication_view
from studio.services.library_service import (
    LIBRARY_LIMIT_DEFAULT,
    LIBRARY_LIMIT_MAX,
    publish_many,
    read_library,
)

__all__ = ["router"]

router = APIRouter(tags=["library"])


@router.get("/api/v1/library", response_model=LibraryResponse)
def get_library(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=LIBRARY_LIMIT_MAX, description="最多几行")] = (
        LIBRARY_LIMIT_DEFAULT
    ),
) -> LibraryResponse:
    """成片库：盘上已出的片子，一条任务一行（新的在前）。"""
    state: AppState = request.app.state.studio
    items = read_library(connection=state.connections.get(), paths=state.paths, limit=limit)
    return LibraryResponse(
        items=[
            LibraryItemView.model_validate(
                {
                    **item.to_dict(),
                    # `can_retry` / `can_cancel` 那几个布尔在这里补上 —— 判据只有
                    # `publication_view` 那一份（发布面板用的是同一个）。
                    "publications": [publication_view(row) for row in item.publications],
                }
            )
            for item in items
        ],
        total=len(items),
        limit=limit,
        hint=None if items else NO_FINAL_HINT,
    )


@router.post("/api/v1/library/publish", response_model=LibraryPublishResponse)
def publish_selected(request: Request, body: LibraryPublishRequest) -> LibraryPublishResponse:
    """批量投递：把勾中的这几条一次排进发布池（**立刻返回**，真正发的是 publish 池）。

    请求体本身不合法（空清单 / 超过上限）⇒ 422 ``VALIDATION_FAILED``：那是调用方的
    错，不该混进 ``items`` 里冒充"这一条没投出去"。
    """
    state: AppState = request.app.state.studio
    report = publish_many(
        connection=state.connections.get(),
        paths=state.paths,
        config=publish_config_for(state),
        task_ids=body.task_ids,
        platforms=body.platforms,
        dry_run=body.dry_run,
    )
    payload = report.to_dict()
    return LibraryPublishResponse(
        items=[LibraryBatchItemView.model_validate(item) for item in payload["items"]],
        platforms=payload["platforms"],
        queued_total=payload["queued_total"],
        task_total=payload["task_total"],
    )
