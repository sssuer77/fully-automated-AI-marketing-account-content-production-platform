"""数据回收的周期循环（T5.4 · §06.6）。

为什么单独一个泵，而不是塞进 ``app/metrics.py``
--------------------------------------------
那个泵是 5s 一拍的**采样**（同步、无 IO、丢线程池），这个泵是 60s 一拍的**业务动作**
（异步、要起浏览器、可能跑几十秒）。混在一起只有坏处：采样会被一次采数拖住，
而采数的周期本来就不该被"面板刷新频率"绑住。

两道安全闸（都会让它在测试与临时家目录里天然不动）
--------------------------------------------------
① ``runnable``：``workers/`` 不在 ⇒ 不起跳（与 ``WatchdogPump`` 同一条判据 ——
   那个目录是"这是生产家目录"的信号）；
② **先睡再拍**：``start()`` 之后第一拍在 ``interval_sec`` 之后才发生。
   少了这一条，每次起 API 都会立刻摸一次浏览器，而"服务刚起来就慢了 3 秒"
   排查起来会绕到别的地方去。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from studio.core.config import PublishConfig
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import ThreadLocalConnections
from studio.services.log_service import LogService
from studio.services.publish_metrics_service import (
    METRICS_POLL_INTERVAL_SEC,
    MetricsTickReport,
    PublishMetricsService,
)

__all__ = ["MetricsRecyclePump"]

logger = get_logger("studio.app.recycle")


class MetricsRecyclePump:
    """按 ``next_metric_at`` 轮询的周期循环（``lifespan`` 起停；测试直接调 ``tick()``）。

    :param config_provider: **每拍现读**发布配置。写成"构造时读一次"的话，
        改 ``metrics_schedule_hours`` 之后要重启 API 才生效 —— 而这一项正是
        §06.6 写着"可配"的那一个。
    """

    def __init__(
        self,
        *,
        connections: ThreadLocalConnections,
        paths: StudioPaths,
        config_provider: Callable[[], PublishConfig],
        log: LogService | None = None,
        interval_sec: float = METRICS_POLL_INTERVAL_SEC,
    ) -> None:
        self._connections = connections
        self._paths = paths
        self._config_provider = config_provider
        self._log = None if log is None else log.append
        self._interval = float(interval_sec)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def runnable(self) -> bool:
        """``workers/`` 在 ⇒ 这是生产家目录（见模块注释）。"""
        return self._paths.workers_dir.is_dir()

    async def tick(self) -> MetricsTickReport:
        """跑一轮（**同步等完**：一轮最多 ``METRICS_BATCH_LIMIT`` 条）。"""
        service = PublishMetricsService(
            connection=self._connections.get(),
            paths=self._paths,
            config=self._config_provider(),
            log=self._log,
        )
        report = await service.tick()
        if report.collected or report.stopped:
            # 只在**有结果**时留一条汇总：每 60s 写一行"什么都没发生"会把日志表刷满，
            # 而这张表是有保留期的（§03.7.5），刷满了就看不见真正的故障。
            logger.info(
                "metrics.recycle_tick",
                collected=len(report.collected),
                deferred=len(report.deferred),
                stopped=len(report.stopped),
            )
        return report

    async def run(self) -> None:
        while not self._stopping:
            # ① 先睡：见模块注释（"服务刚起来就慢了 3 秒"）。
            await asyncio.sleep(self._interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败绝不能把泵掀翻（下一拍照跑）
                logger.warning("metrics.recycle_failed", error=str(exc))

    def start(self) -> None:
        if self._task is not None or not self.runnable:
            return
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="metrics-recycle")
        logger.info("metrics.recycle_started", interval_sec=self._interval)

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("metrics.recycle_stopped")

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"MetricsRecyclePump(runnable={self.runnable}, interval_sec={self._interval})"
