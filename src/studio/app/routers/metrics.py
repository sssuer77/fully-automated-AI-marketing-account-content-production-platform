"""观测面板 REST 面（T4.12 · §04.5.11）。

一个端点，一次拿全
------------------
与总览台同一取舍：首屏那六块（资源 / 队列 / 产量 / 进程 / 备份 / 存储）分六个
端点拿，会出现"这块是 11:00:00 的、那块是 11:00:03 的" —— 排障时最不该有的
就是这种"看起来是同一时刻"的错觉。

**只读**
--------
这一屏一个写动作都没有：GC / 备份 / VACUUM 都是 CLI 或计划任务的事。
把它们做成按钮，就等于给"删数据"这个动作开了一个只隔一次点击的入口。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState, observability_service_for
from studio.app.schemas.metrics import MetricsResponse

__all__ = ["router"]

router = APIRouter(tags=["metrics"])


@router.get("/api/v1/metrics", response_model=MetricsResponse)
def get_metrics(request: Request) -> MetricsResponse:
    """观测面板一次拿全（含备份新鲜度与存储体检）。"""
    state: AppState = request.app.state.studio
    return MetricsResponse.model_validate(observability_service_for(state).read().to_dict())
