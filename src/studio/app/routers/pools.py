"""四池调度控制台 REST 面（T4.10 · §04.4.5 / §03.4.4）。

这一层只做三件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.pool_service.PoolService` 调用；
② 把服务层的数据形状交给 Pydantic 复核（``model_validate(to_dict())``）；
③ 让 :class:`~studio.core.errors.StudioError` 自己冒到应用级 handler
   （``app/errors.py``）—— 池名合法性由 ``Literal``（请求体）与
   :func:`~studio.core.config.concurrency_bounds`（服务层）两层守。

为什么暂停 / 恢复**不在**这里
-----------------------------
T4.2 已经落地 ``POST /api/v1/overview/pools``。同一件事两条写路径，审计 / 事件 /
幂等判据迟早分叉，所以四池控制台是它的**第二个视图**，不是第二个入口（裁定 144）。

为什么配置读失败不返回 500
--------------------------
面板的作用是「告诉我现在什么情况」。``pools.yaml`` 被改坏时，用户最需要看到的
恰恰是「配置读不到，去修这一份」—— 而不是一个什么都不说的 500。
所以 ``pool_service_for`` 把读失败翻成 ``config=None``，卡片带 ``error`` 上桌。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState, pool_service_for
from studio.app.schemas.common import clean_reason
from studio.app.schemas.pools import (
    ConcurrencyRequest,
    ConcurrencyResponse,
    PoolsResponse,
    RequeueRequest,
    RequeueResponse,
)

__all__ = ["router"]

router = APIRouter(tags=["pools"])


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/pools", response_model=PoolsResponse)
def get_pools(request: Request) -> PoolsResponse:
    """四池一次拿全：配置值 / 运行值 / 状态 / 死信（面板首屏就这一个请求）。"""
    state: AppState = request.app.state.studio
    return PoolsResponse.model_validate(pool_service_for(state).read().to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写 · 并发旋钮
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/pools/concurrency", response_model=ConcurrencyResponse)
def set_concurrency(request: Request, body: ConcurrencyRequest) -> ConcurrencyResponse:
    """调并发（写 ``pool_settings`` + ``audit_ops``，**生效无需重启** · §03.4.4）。

    ``le=MAX_CONCURRENCY_ANY``（=8）只挡住物理越界；按池的工程上限（voice ≤ 3）
    在服务层兜底，返回的是带 ``remediation`` 的业务错误（``POOL_CONCURRENCY_LIMIT``）。
    """
    state: AppState = request.app.state.studio
    outcome = pool_service_for(state).set_concurrency(
        pool=body.pool,
        concurrency=body.concurrency,
        reason=clean_reason(body.reason),
    )
    return ConcurrencyResponse.model_validate(outcome.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写 · 死信重投
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/pools/requeue", response_model=RequeueResponse)
def requeue_dead(request: Request, body: RequeueRequest) -> RequeueResponse:
    """批量重投死信（``dead → pending`` + ``attempts`` 归零 + ``audit_ops``）。

    **逐条独立**：一条不是死信（或已被别人重投过）不影响其余。失败逐条如实返回，
    不折叠成一句「部分失败」—— 用户要的是「哪几条没进去、为什么」。
    """
    state: AppState = request.app.state.studio
    outcome = pool_service_for(state).requeue_dead(
        job_ids=tuple(body.job_ids),
        reason=clean_reason(body.reason),
    )
    return RequeueResponse.model_validate(outcome.to_dict())
