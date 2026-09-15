"""无人值守守护的周期循环（T4.11 · §04.8）。

与 `app/metrics.py` 的 `MetricsPump` 同一形状（`lifespan` 起停，测试直接调 `tick()`），
但两处刻意的不同：

1. **只有 `runnable` 才起跳**。临时家目录（集成测试）里 `workers/` 不存在，起跳只会
   每 5s 白读一次进程表 —— 而且守护会**在后台改数据**（任务清扫、池暂停），
   那正是"测试用例偶尔红一次、重跑又绿"的来源。生产家目录里 `workers/` 一定在。
2. **为什么泵在 `app/` 而不是 `services/`**：与采样泵同一条理由 —— 它要跨线程调度、
   要读 `AppState`，而 `services/` 不认识应用层。
"""

from __future__ import annotations

import asyncio
import contextlib

from studio.core.logging import get_logger
from studio.services.watchdog_service import WatchdogService, WatchdogTick

__all__ = ["WatchdogPump"]

logger = get_logger("studio.app.watchdog")


class WatchdogPump:
    """把"跑一轮守护"变成"每 5s 跑一轮"。

    :param watchdog: 守护实现（`AppState` 持有；REST 面读的是同一份引用）
    """

    def __init__(self, *, watchdog: WatchdogService, interval_sec: float | None = None) -> None:
        self._watchdog = watchdog
        self._interval = float(interval_sec if interval_sec is not None else watchdog.tick_sec)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def watchdog(self) -> WatchdogService:
        return self._watchdog

    @property
    def runnable(self) -> bool:
        return self._watchdog.runnable

    @property
    def interval_sec(self) -> float:
        return self._interval

    def tick(self) -> WatchdogTick:
        """同步跑一轮（**阻塞**：`run()` 把它丢进线程池；测试直接调它）。"""
        return self._watchdog.tick()

    async def run(self) -> None:
        """周期循环（必须在事件循环里跑；`stop()` 用 cancel 收尾）。"""
        while not self._stopping:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮守护失败绝不能把泵掀翻（下一拍照跑）
                logger.warning("watchdog.tick_failed", error=str(exc))
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """起跳（幂等：重复调用只有一条循环）。"""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="watchdog-pump")
        logger.info("watchdog.pump_started", interval_sec=self._interval)

    async def stop(self) -> None:
        """收尾（幂等；没起跳过也安全）。"""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("watchdog.pump_stopped")
