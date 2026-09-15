"""健康检查（T1.7 · §02.2；T4.1 补响应模型）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState
from studio.app.schemas.health import HealthResponse

__all__ = ["router"]

router = APIRouter(tags=["health"])


@router.get("/api/v1/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """进程存活 + Hub 运行态（池监控面板的兜底探针）。"""
    state: AppState = request.app.state.studio
    return HealthResponse.model_validate(
        {
            "ok": True,
            "spec_version": request.app.state.spec_version,
            "db": str(state.paths.db_file),
            "latest_log_id": state.logs.latest_id(),
            "ws": state.hub.stats(),
        }
    )
