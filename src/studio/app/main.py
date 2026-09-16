"""FastAPI 应用工厂（T1.7 · §02.2）。

`create_app()` 是**纯函数式**的：依赖从外面传进来（`AppState`），不在模块级造单例。
理由很实际 —— 测试要能"每个用例一套临时库 + 一套 Hub"，模块级单例做不到这件事。

T1.7 落 **WS 骨架 + health + logs 分页**；T4.9 补日志过滤 / 搜索 / 导出；
T4.4 补确认闸与稿件面（`approvals` / `scripts`）；T4.3 补选题面（`topics` / `hot`）；
T4.2 补总览台（`overview`）；T4.10 补四池控制台（`pools`）；T4.13 补人物库（`persona`）；
T4.7 补合成配置（`outputs`）；T4.11 补无人值守守护（`watchdog`）；T4.8 补素材库（`assets`）；
T4.6 补渲染面板（`render`）；T2.9 补配音操作面（`voice`）。
其余面板的 REST 由 T4.x 逐步补上。
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from studio import __spec_version__, __version__
from studio.app.deps import AppState, build_state
from studio.app.errors import install_error_handlers
from studio.app.lifespan import lifespan
from studio.app.routers.approvals import router as approvals_router
from studio.app.routers.assets import router as assets_router
from studio.app.routers.audit import router as audit_router
from studio.app.routers.health import router as health_router
from studio.app.routers.logs import router as logs_router
from studio.app.routers.metrics import router as metrics_router
from studio.app.routers.outputs import router as outputs_router
from studio.app.routers.overview import router as overview_router
from studio.app.routers.persona import router as persona_router
from studio.app.routers.pools import router as pools_router
from studio.app.routers.render import router as render_router
from studio.app.routers.scripts import router as scripts_router
from studio.app.routers.topics import router as topics_router
from studio.app.routers.voice import router as voice_router
from studio.app.routers.watchdog import router as watchdog_router
from studio.app.routers.ws import router as ws_router
from studio.core.config import load_config
from studio.core.errors import ConfigError, ErrorCode
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.ws.hub import HubSettings

__all__ = ["create_app", "run_server"]

logger = get_logger("studio.app.main")


def create_app(
    *,
    state: AppState | None = None,
    paths: StudioPaths | None = None,
    hub_settings: HubSettings | None = None,
    openapi_enabled: bool = True,
) -> FastAPI:
    """构造应用；`state=None` ⇒ 按 `paths`（或环境变量）现造一套依赖。"""
    resolved = (
        state
        if state is not None
        else build_state(
            paths=paths or StudioPaths.from_env(),
            hub_settings=hub_settings,
        )
    )
    application = FastAPI(
        title="AI 全自动营销号制片台",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if openapi_enabled else None,
        openapi_url="/api/openapi.json" if openapi_enabled else None,
    )
    application.state.studio = resolved
    application.state.spec_version = __spec_version__
    install_error_handlers(application)
    application.include_router(health_router)
    application.include_router(logs_router)
    application.include_router(audit_router)
    application.include_router(metrics_router)
    application.include_router(approvals_router)
    application.include_router(scripts_router)
    application.include_router(topics_router)
    application.include_router(overview_router)
    application.include_router(pools_router)
    application.include_router(persona_router)
    application.include_router(outputs_router)
    application.include_router(assets_router)
    application.include_router(watchdog_router)
    application.include_router(render_router)
    application.include_router(voice_router)
    application.include_router(ws_router)
    mount_web_ui(application, resolved.paths)
    return application


def mount_web_ui(application: FastAPI, paths: StudioPaths) -> None:
    """把 `web/dist` 挂到 ``/`` —— 没有这一步，图形界面在浏览器里根本打不开。

    三个必须写下来的点
    ------------------
    1. **必须挂在所有 API 路由之后**。Starlette 按注册顺序匹配，``Mount("/")`` 兜住的是
       "前面谁都没接"的路径；挂在前面会把 ``/api/v1/...`` 与 ``/ws/ui`` 一起吃下去，
       症状是"接口全 404、首页却打得开"，看上去像后端坏了。
    2. **产物不在就干脆不挂**（不是挂一个空目录）。挂上去的代价不是"首页 404"，而是
       **所有没匹配上路由的请求都会掉进 StaticFiles** —— 而它在目录不存在时抛
       ``RuntimeError``，于是 ``POST /api/v1/metrics``（本该 405）、打错的路径（本该 404）
       全变成 500。API 自己的 404 / 405 语义比"首页能打开"重要得多。
       所以：没有 ``web/dist`` ⇒ 只留一条日志指路，路由表与从前**一模一样**。
    3. ``html=True``：``/`` 直接给 ``index.html``。这个 SPA 的"换面板"是内存里的状态，
       不走前端路由，所以不需要"任意路径都回 index.html"的兜底。
    """
    dist = paths.web_dist_dir
    if not dist.is_dir():
        logger.warning("app.web_dist_missing", dir=dist.as_posix(), hint="cd web && npm run build")
        return
    application.mount("/", StaticFiles(directory=dist, html=True), name="web")


def run_server(
    *,
    paths: StudioPaths | None = None,
    host: str | None = None,
    port: int | None = None,
    tail_interval_sec: float = 0.25,
) -> None:
    """按配置起 uvicorn（``studio serve`` 与 ``workers/run_api.py`` **共用一份**）。

    抽出来是因为 T1.12 给 API 加了一个新的拉起路径（``workers/run_api.py``，
    由 ``studio service start`` 调用）：两份实现各写一遍绑定地址与库校验，
    迟早出现"CLI 起得来、一键启动起不来"。
    """
    resolved = paths or StudioPaths.from_env()
    loaded = load_config(resolved)
    if not resolved.db_file.is_file():
        raise ConfigError(
            f"数据库尚未初始化：{resolved.db_file}",
            code=ErrorCode.PATH_MISSING,
            context={"db": str(resolved.db_file)},
            remediation="先跑 `studio db migrate`",
        )
    web = loaded.bundle.app.web
    uvicorn.run(
        create_app(
            paths=resolved,
            hub_settings=HubSettings(tail_interval_sec=tail_interval_sec),
            openapi_enabled=web.openapi_enabled,
        ),
        host=host or web.host,
        port=port or web.port,
    )
