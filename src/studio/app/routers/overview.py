"""总览台 REST 面（T4.2 · §04.4.5 第 1 行）。

这一层的三个职责
----------------
① 把 HTTP 请求翻成服务层调用（``OverviewService`` / ``ServiceManager``）；
② 把服务层的结果打包成响应（含**逐池**成败，不折叠成一句"部分失败"）；
③ 让 ``StudioError`` 自己冒到应用级 handler（``app/errors.py``）去 ——
   池名 / 策略名的合法性一条都不在这里（``Literal`` + 服务层的纵深防御）。

为什么"启动"只暴露**启动**，不暴露"停止"
----------------------------------------
在 API 进程里停服务 = 让 API 进程**杀掉自己**（``ServiceManager.stop()`` 的第一个
目标就是 ``api``）。HTTP 响应还没写完，连接就断了；而且"我点了停止，然后呢"——
没人能再把它拉起来，因为拉起来的那个东西刚被自己停了。
**停止的正门是 ``停止.bat``**（它跑在另一个进程里，与目标同控制台，裁定 107）。
所以这里只留启动：它是幂等的（已在跑的进 ``already_running``，端口被占的进
``port_busy``），从 WebUI 点一百次也不会多起一份。

为什么"启动"必须 ``open_browser=False``
--------------------------------------
点这个按钮的人**已经在浏览器里了**。再 ``webbrowser.open`` 一次只会多弹一个
标签页，而且新标签页落在同一个地址上，看起来像"点了没反应"（裁定 137）。

为什么"启动"要一把非阻塞锁
--------------------------
``ServiceManager.start()`` 的"先问再拉"是**读-改-写**：两次点击会各自读到
"没在跑"，然后各拉一份进程。端口冲突只是最轻的后果（池 worker 会真的起两份，
同一批 job 被两个进程认领）。锁只守入口，拿不到立刻 409 ``SERVICE_START_BUSY``，
**不排队** —— 与选题面板的长任务单飞同一手法（裁定 129）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Request

from studio.app.deps import AppState, overview_service_for
from studio.app.schemas.common import clean_reason
from studio.app.schemas.overview import (
    OverviewResponse,
    PolicyRequest,
    PolicyResponse,
    PoolPauseRequest,
    PoolPauseResponse,
    StartResponse,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.services.service_manager import ServiceManager

__all__ = ["router"]

router = APIRouter(tags=["overview"])


class _StartGuard:
    """进程级"同一时刻只跑一次启动"的守卫（见模块 docstring）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise StudioError(
                "已经有一次启动在进行中，请等它结束",
                code=ErrorCode.SERVICE_START_BUSY,
                remediation="进度见「实时日志」面板；启动最多等 60s 就绪，不要重复点击",
            )
        try:
            yield
        finally:
            self._lock.release()


_START_GUARD = _StartGuard()


def _service_manager(paths: StudioPaths) -> ServiceManager:
    """五进程编排器的构造点（单测 monkeypatch 它就能不碰真进程）。"""
    return ServiceManager(paths)


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/overview", response_model=OverviewResponse)
def get_overview(request: Request) -> OverviewResponse:
    """总览台一次拿全：四池 + 今日产量 + 资源 + 服务就绪 + 当前策略。"""
    state: AppState = request.app.state.studio
    return OverviewResponse.model_validate(overview_service_for(state).read().to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写 · 暂停 / 恢复
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/overview/pools", response_model=PoolPauseResponse)
def set_pool_paused(request: Request, body: PoolPauseRequest) -> PoolPauseResponse:
    """暂停 / 恢复一个池（**在途跑完**，不重启 worker · 裁定 138）。"""
    state: AppState = request.app.state.studio
    outcome = overview_service_for(state).set_pool_paused(
        pool=body.pool,
        paused=body.paused,
        reason=clean_reason(body.reason),
    )
    return PoolPauseResponse.model_validate(outcome.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写 · 一键全自动 / 回退
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/overview/auto", response_model=PolicyResponse)
def set_auto_approve_policy(request: Request, body: PolicyRequest) -> PolicyResponse:
    """切自动放行策略（写 `config/app.yaml` + 同步内存 + `audit_ops` · 裁定 139）。"""
    state: AppState = request.app.state.studio
    outcome = overview_service_for(state).set_auto_approve_policy(
        policy=body.policy,
        reason=clean_reason(body.reason),
    )
    return PolicyResponse.model_validate(outcome.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写 · 启动
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/overview/start", response_model=StartResponse)
def start_services(request: Request) -> StartResponse:
    """拉起五进程（**不**开浏览器、**要**过 doctor 门禁 · 裁定 137）。

    这是**同步阻塞**调用：`ServiceManager.start()` 要等各进程就绪（上限 60s）。
    路由是 ``def`` ⇒ 跑在 Starlette 线程池里，不占事件循环；前端把超时放宽到 90s。
    """
    state: AppState = request.app.state.studio
    with _START_GUARD.hold():
        report = _service_manager(state.paths).start(open_browser=False, doctor_gate=True)
    return StartResponse.model_validate(report.to_dict())
