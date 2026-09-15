"""应用生命周期（T1.7 · §02.2）。

启动：Hub 的 tail 循环起跳（起点 = 当前最大 `system_logs.id`，只推"启动之后"的新行），
随后 T4.2 的采样泵起跳（每 5s 一拍：资源 / 四池 / 心跳）。

关闭的**顺序**是有讲究的：**先停泵，再停 Hub，最后关 DB**。
- 泵还在跑而 Hub 已经停了 ⇒ 每拍都往一个没人 drain 的合并器里塞事件（白占内存）；
- Hub 还在跑而连接已经关了 ⇒ 在途的 tail 查询撞上已关闭的连接，日志里一串
  看不出所以然的 `ProgrammingError`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from studio.app.deps import AppState
from studio.core.logging import get_logger

__all__ = ["lifespan"]

logger = get_logger("studio.app.lifespan")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    state: AppState = application.state.studio
    state.hub.start()
    state.persona_events.start()
    state.pump.start()
    # 无人值守守护（T4.11）：`workers/` 不在 ⇒ 一个池都拉不起来 ⇒ 不起跳。
    # 这条判据让集成测试的临时家目录天然"不被打扰"，同时生产家目录照常守护。
    if state.watchdog_pump.runnable:
        state.watchdog_pump.start()
    else:
        logger.info("app.watchdog_idle", reason="workers/ 目录不在或 watchdog.enabled=false")
    logger.info("app.started", db=str(state.paths.db_file))
    try:
        yield
    finally:
        await state.pump.stop()
        await state.watchdog_pump.stop()
        state.persona_events.stop()
        await state.hub.stop()
        state.close()
        logger.info("app.stopped")
