"""应用生命周期（T1.7 · §02.2）。

启动：Hub 的 tail 循环起跳（起点 = 当前最大 `system_logs.id`，只推"启动之后"的新行），
随后 T4.2 的采样泵起跳（每 5s 一拍：资源 / 四池 / 心跳）。

关闭的**顺序**是有讲究的：**先停泵，再停 Hub，最后关 DB**。
- 泵还在跑而 Hub 已经停了 ⇒ 每拍都往一个没人 drain 的合并器里塞事件（白占内存）；
- Hub 还在跑而连接已经关了 ⇒ 在途的 tail 查询撞上已关闭的连接，日志里一串
  看不出所以然的 `ProgrammingError`。

出片工作线程（T4.6）与 Hub 没有关系，它的位置只看一件事：**它碰数据库**。
所以它排在任何"关连接"的动作之前 —— 收工信号发出后，在途的那一次 `read_active_script`
仍然能拿到一条活着的连接。反过来把它放在 `close()` 之后，运气不好就是一条
"数据库连接已关闭"的报错，而它看上去和用户按的那一下取消毫无关系。
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
    # 出片工作线程（T4.6）：起在这里 ⇒ 提交的任务在请求返回后真的会跑起来。
    state.render_jobs.start()
    logger.info("app.started", db=str(state.paths.db_file))
    try:
        yield
    finally:
        await state.pump.stop()
        await state.watchdog_pump.stop()
        state.persona_events.stop()
        # 不等当前这一条跑完：进程都要退了，等它没有意义（成片已在盘上，半成品
        # 是 `.partial`，不会被当成成片列出来）。
        state.render_jobs.stop()
        await state.hub.stop()
        state.close()
        logger.info("app.stopped")
