"""总览台的周期采样泵（T4.2 · §04.4.3）。

一轮（每 5s）做三件事
--------------------
① 采资源 ⇒ `metrics.tick`（0.2 Hz，与 §04.4.3 的频率上限一致）；
② 采四池 ⇒ 每池一条 `pool.stats`（§04.4.3 上限 1 Hz，5s 一拍远在限内）；
③ 心跳**有变化**才发 `pool.worker_status`（4 池 × N worker 每拍都推一遍，
   前端画出来的像素一模一样 —— 纯浪费带宽，还把环形缓冲挤掉别的帧）。

为什么泵在 `app/` 而不是 `services/`
------------------------------------
它要往 `Hub` 投递帧，而分层方向是 `app → ws → services → db`：
`services/` import `ws/` 会成环（契约测试会拦）。所以"采什么"留在 `services`，
"什么时候推、推给谁"在这里。

为什么三个事件挤在同一拍里
--------------------------
它们的**真相是同一份**（同一时刻的池 + 心跳 + 资源）。拆成三条独立定时器，
面板上迟早出现"池说 3 个在跑、心跳说 2 个活着"这种互相打架的一帧 ——
而那一帧恰好是用户盯着看的那一帧。

为什么要丢进线程池
------------------
`nvidia-smi` 一次 80–150 ms，`psutil` 的磁盘查询也是真系统调用。在事件循环里
直接跑，每 5s 就有一次 100ms 级的停顿（Hub 的 tail 循环、WS 推送全都卡住）。
`asyncio.to_thread` 把整拍挪走，代价只是"多几条线程亲和的 sqlite 连接"
（`ThreadLocalConnections` 本来就是为这个设计的）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import Any

from studio.core.clock import now_iso
from studio.core.errors import StudioError
from studio.core.logging import get_logger
from studio.core.proto import EventKind
from studio.db import JobStore, ThreadLocalConnections
from studio.pools.runner import POOL_NAMES
from studio.services.metrics_service import METRICS_SAMPLE_INTERVAL_SEC, MetricsService, ResourceSnapshot
from studio.ws.hub import Hub
from studio.ws.protocol import Channel, Envelope, FrameType

__all__ = ["MetricsPump"]

logger = get_logger("studio.app.metrics")


class MetricsPump:
    """把"采样"变成"推送"的那一层（`lifespan` 起停，测试直接调 `tick()`）。

    :param hub: 推送中枢（`app` 层持有；`services/` 碰不到它 —— 那会成环）
    :param connections: 按线程取的连接池（一拍里要读四池统计与心跳）
    """

    def __init__(
        self,
        *,
        hub: Hub,
        connections: ThreadLocalConnections,
        metrics: MetricsService,
        interval_sec: float = METRICS_SAMPLE_INTERVAL_SEC,
    ) -> None:
        self._hub = hub
        self._connections = connections
        self._metrics = metrics
        self._interval = float(interval_sec)
        self._fingerprints: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ── 一拍 ────────────────────────────────────────────────────────────

    def tick(self) -> ResourceSnapshot:
        """同步跑一拍（**阻塞**：`run()` 会把它丢进线程池；测试直接调它）。"""
        snapshot = self._metrics.sample()
        self.publish_resources(snapshot)
        self.publish_pools()
        return snapshot

    def publish_resources(self, snapshot: ResourceSnapshot) -> None:
        """`metrics.tick`（§04.4.3 的载荷字段由 `ResourceSnapshot.to_dict` 保证）。"""
        self._publish(Channel.METRICS, {**snapshot.to_dict(), "kind": EventKind.METRICS_TICK.value})

    def publish_pools(self) -> int:
        """四池各一条 `pool.stats`（+ 心跳变化时的 `pool.worker_status`）。

        :return: 成功发出的 `pool.stats` 条数（测试断言用；池参数缺失的池跳过）
        """
        store = JobStore(self._connections.get())
        published = 0
        for pool in POOL_NAMES:
            try:
                stats = store.stats(pool=pool)
            except StudioError as exc:
                # 与握手快照同取舍：一个池坏了不该让另外三个也推不出去
                logger.warning("metrics.pool_stats_failed", pool=pool, error=str(exc))
                continue
            payload = stats.to_dict()
            workers = payload.pop("workers")
            self._publish(Channel.POOLS, {**payload, "kind": EventKind.POOL_STATS.value})
            published += 1
            self._publish_worker_changes(pool, workers)
        return published

    # ── 内部 ────────────────────────────────────────────────────────────

    def _publish_worker_changes(self, pool: str, workers: list[dict[str, Any]]) -> int:
        """心跳**变了**才发（判据是"面板会画出不同像素"的那几个字段）。"""
        fingerprint = json.dumps([_beat_key(beat) for beat in workers], ensure_ascii=False)
        if self._fingerprints.get(pool) == fingerprint:
            return 0
        self._fingerprints[pool] = fingerprint
        for beat in workers:
            self._publish(Channel.POOLS, {**beat, "kind": EventKind.POOL_WORKER_STATUS.value})
        return len(workers)

    def _publish(self, channel: Channel, data: Mapping[str, Any]) -> None:
        """投递一条事件（`seq` 由每条连接自己分配，见 `Hub.ClientConnection.enqueue`）。"""
        self._hub.publish(Envelope(type=FrameType.EVENT, channel=channel, ts=now_iso(), data=dict(data)))

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """周期循环（必须在事件循环里跑；`stop()` 用 cancel 收尾）。"""
        while not self._stopping:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 采样失败绝不能把整个泵掀翻（下一拍照跑）
                logger.warning("metrics.tick_failed", error=str(exc))
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """起跳（幂等：重复调用只有一条循环）。"""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="metrics-pump")
        logger.info("metrics.pump_started", interval_sec=self._interval)

    async def stop(self) -> None:
        """收尾（幂等）。"""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("metrics.pump_stopped")


def _beat_key(beat: Mapping[str, Any]) -> list[Any]:
    """心跳的"会不会画出不同像素"那几个字段（其余字段变了不发）。"""
    return [
        beat.get("worker_id"),
        beat.get("status"),
        beat.get("current_job_id"),
        beat.get("gpu_mem_mb"),
    ]
