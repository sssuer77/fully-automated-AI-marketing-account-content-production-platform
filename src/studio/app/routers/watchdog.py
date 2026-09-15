"""无人值守守护 REST 面（T4.11 · §04.5.10）。

这一层只做两件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.watchdog_service.WatchdogService` 调用；
② 把服务层的形状交给 Pydantic 复核（``model_validate(to_dict())``）。

**一个动作都没有**：判死、退避、重启、磁盘门禁、任务清扫全在服务层，而且与周期泵
跑的是**同一份** ``tick()``。控制器里再写一遍"什么情况该重启"，等于让按钮和后台
两套逻辑各走各的 —— 而这两条路最终都要动进程表。

为什么触发一轮是 ``POST`` 而不是 ``GET /tick``
---------------------------------------------
它会**改状态**（可能拉起进程、暂停池、把任务推进人工池）。用 GET 的话，浏览器预取、
链接预览、甚至一次误粘贴的地址栏都会真的重启一个 worker —— 把"能改状态的动作"
放进 GET，是这类事故最常见的入口。

为什么这个路由是同步 ``def``
----------------------------
``tick()`` 是阻塞的（要等进程就绪、要读进程表）。``def`` 路由跑在 Starlette 的
线程池里，不占事件循环 —— 否则一次重启探测会把整个 WebSocket 推送卡住。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState
from studio.app.schemas.common import clean_reason
from studio.app.schemas.watchdog import (
    WatchdogStatusModel,
    WatchdogTickRequest,
    WatchdogTickResponse,
)

__all__ = ["router"]

router = APIRouter(tags=["watchdog"])


@router.get("/api/v1/watchdog", response_model=WatchdogStatusModel)
def get_watchdog(request: Request) -> WatchdogStatusModel:
    """守护当前状态：守谁 / 自愈几次 / 谁停手了 / 门禁什么水位 / 人工池有几条。

    与总览台那份（``/api/v1/overview`` 的 ``watchdog`` 段）**同一个来源**，这里是
    独立面板的完整视图：总览台要的是"一眼扫过"，这块要的是"逐进程核对"。
    """
    state: AppState = request.app.state.studio
    return WatchdogStatusModel.model_validate(state.watchdog.snapshot())


@router.post("/api/v1/watchdog/tick", response_model=WatchdogTickResponse)
def run_watchdog_tick(request: Request, body: WatchdogTickRequest) -> WatchdogTickResponse:
    """人工触发一轮（**留痕**：写 ``audit_ops`` · ``actor=user`` / ``source=webui``）。

    守护停用时也照跑照留痕：返回值里 ``enabled=false`` + ``note`` 说清"本轮什么都没做"，
    而不是给一个 400 —— 用户按这个按钮的动机是"我想确认它还在"，回一句
    "守护没开"比一个错误码有用得多。
    """
    state: AppState = request.app.state.studio
    outcome = state.watchdog.manual_tick(reason=clean_reason(body.reason))
    return WatchdogTickResponse.model_validate(outcome.to_dict())
