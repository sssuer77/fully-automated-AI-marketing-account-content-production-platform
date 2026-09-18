"""数据回收的轮询（T5.4 · §06.6）。

一轮做四件事
------------
① 发布池**正忙 ⇒ 整拍让路**（见下）；② 找出到点的记录；③ 逐条采数（成功即落库）；
④ 失败按 §06.6 顺延，次数用尽就停止采集这一条。

为什么"发布池忙"要整拍让路
--------------------------
发布器用的是**按账号持久化的浏览器 profile**（``data/browser_profile/<账号>``），
而 Chromium **不允许两个进程同时打开同一个 user-data-dir**。发布池 worker 正在
发这条账号的作品时，API 进程这边再起一个浏览器去管理页读数，两边会互相把对方的
登录态锁住 —— 表现出来是"发布失败"（一个比"这次没采到数"贵得多的代价）。
让路只是把这次采数推迟到下一拍（默认 60s 之后），而 §06.6 的时点是小时级的，
晚一分钟什么也不影响。

为什么轮询在 ``app/`` 之外、日志却在 ``services/`` 里发
-------------------------------------------------------
"采哪几条、失败怎么办"是业务口径；"多久拍一次、什么时候起停"在 ``app/recycle.py``。
与 §04.4.3 的资源采样同一条分法（见 ``app/metrics.py`` 的模块注释）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from studio.core.config import PublishConfig
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import JobStore
from studio.db.repositories.publication_repo import PublicationRepo, PublicationRow
from studio.publish.base import Publisher
from studio.publish.metrics import METRICS_MAX_ATTEMPTS, collect_metrics, defer_after_failure
from studio.services.log_service import LogSink

__all__ = [
    "METRICS_POLL_INTERVAL_SEC",
    "PUBLISH_POOL",
    "MetricsTickReport",
    "PublishMetricsService",
]

logger = get_logger("studio.services.publish_metrics")

#: 轮询周期。时点是小时级的（T+1h/6h/24h/72h），1 分钟一拍已经比需求细两个量级；
#: 再密只是空转（每拍一条索引查询 + 一次池统计）。
METRICS_POLL_INTERVAL_SEC: Final[float] = 60.0

#: 发布池名（与 ``publish_service.PUBLISH_POOL`` 同值；那边是投递口径，这里是让路口径）。
PUBLISH_POOL: Final[str] = "publish"

#: 一拍最多采几条（防止积压时一次把浏览器起个没完）。
METRICS_BATCH_LIMIT: Final[int] = 20

#: 顺延时长的人话（与 ``publish/metrics.py`` 的 ``METRICS_RETRY_DELAY_HOURS`` 同一口径）。
METRICS_POLL_DELAY_TEXT: Final[str] = "1 小时"


@dataclass(frozen=True, slots=True)
class MetricsTickReport:
    """一拍的结论（面板/CLI 直接展示）。"""

    collected: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    #: 发布池正忙 ⇒ 这一拍**什么都没做**（不是失败，下一拍照跑）。
    yielded: bool = False

    @property
    def touched(self) -> int:
        return len(self.collected) + len(self.deferred) + len(self.stopped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected": list(self.collected),
            "deferred": list(self.deferred),
            "stopped": list(self.stopped),
            "yielded": self.yielded,
        }


class PublishMetricsService:
    """数据回收的一拍（**无状态**：所有状态都在 ``publications`` 那一行上）。

    无状态是有意的：``next_metric_at`` / ``metric_attempts`` 都在库里，所以
    "重启一次 API 会不会把重试次数忘掉"这个问题根本不存在（与 §04.4.3 的磁盘
    水位状态机相反 —— 那个状态本来就只活在进程里）。

    :param connection: 本线程的库连接（调用方按线程取，见 ``ThreadLocalConnections``）
    :param publisher_factory: 测试注入用；不传就按记录里的平台+账号现造（见
        ``publish/metrics.py`` 的 ``_build_publisher``）
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        config: PublishConfig,
        log: LogSink | None = None,
        publisher_factory: Callable[[PublicationRow], Publisher] | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._config = config
        self._log = log
        self._repo = PublicationRepo(connection)
        self._factory = publisher_factory

    # ── 一拍 ────────────────────────────────────────────────────────

    def due(self, *, now: str | None = None, limit: int = METRICS_BATCH_LIMIT) -> tuple[PublicationRow, ...]:
        """到点该采的记录（走 ``idx_pub_metrics`` 那条部分索引）。"""
        return self._repo.list_due_metrics(now=now, limit=limit)

    def publish_busy(self) -> bool:
        """发布池里有没有**正在跑**的作业（有 ⇒ 这一拍让路，见模块注释）。

        池统计读不出来（库刚重建、表还没建）时**当成忙**：让路的代价是晚一拍，
        而误判成"不忙"的代价是两个浏览器抢同一个 profile。
        """
        try:
            return JobStore(self._connection).stats(pool=PUBLISH_POOL).running > 0
        except Exception as exc:  # pragma: no cover - 只有坏库会走到
            logger.warning("metrics.busy_check_failed", error=str(exc))
            return True

    async def tick(self, *, now: str | None = None, limit: int = METRICS_BATCH_LIMIT) -> MetricsTickReport:
        """采一轮（**逐条隔离**：一条失败不影响其余 · §06.6「不阻塞其他发布」）。"""
        if self.publish_busy():
            return MetricsTickReport(yielded=True)

        collected: list[str] = []
        deferred: list[str] = []
        stopped: list[str] = []
        for row in self.due(now=now, limit=limit):
            outcome = await self.collect_one(row, now=now)
            {"collected": collected, "deferred": deferred, "stopped": stopped}[outcome].append(row.id)
        return MetricsTickReport(collected=tuple(collected), deferred=tuple(deferred), stopped=tuple(stopped))

    async def collect_one(self, row: PublicationRow, *, now: str | None = None) -> str:
        """采一条 ⇒ ``'collected'`` / ``'deferred'`` / ``'stopped'``（**不抛**）。

        失败**不抛**：调用方是一个循环，一条采不到不该让后面几条也停在原地
        （§06.6 明写"不阻塞其他发布"）。异常在这里变成"顺延"或"停止采集"两种结论。
        """
        try:
            metrics = await collect_metrics(
                row.id,
                connection=self._connection,
                paths=self._paths,
                config=self._config,
                publisher=None if self._factory is None else self._factory(row),
                now=now,
            )
        except Exception as exc:
            return self._on_failure(row, exc, now=now)

        self._emit(
            "info",
            f"数据回收：{row.platform} {row.platform_post_id} 播放 {metrics.views}",
            payload={
                "publication_id": row.id,
                "platform": row.platform,
                "views": metrics.views,
                "likes": metrics.likes,
                "comments": metrics.comments,
                "shares": metrics.shares,
            },
        )
        return "collected"

    def _on_failure(self, row: PublicationRow, exc: Exception, *, now: str | None) -> str:
        """失败处置（§06.6）：顺延 1 小时重试；次数用尽 ⇒ 停止采集这一条。"""
        fresh = self._repo.defer_metrics(row.id, next_metric_at=defer_after_failure(row, now=now))
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        if fresh.next_metric_at is None:
            # **error 而不是 warn**：这一条到此为止了，人不看一眼就永远不会有它的数据。
            # 其余记录照常回收（这里只影响这一行）。
            self._emit(
                "error",
                f"数据回收放弃：{row.platform} {row.platform_post_id} 连续 "
                f"{METRICS_MAX_ATTEMPTS} 次采不到（{reason}）",
                payload={
                    "publication_id": row.id,
                    "platform": row.platform,
                    "attempts": fresh.metric_attempts,
                    "error": reason,
                },
            )
            return "stopped"
        self._emit(
            "warn",
            f"数据回收失败，{METRICS_POLL_DELAY_TEXT}后重试：{row.platform} {reason}",
            payload={
                "publication_id": row.id,
                "platform": row.platform,
                "attempts": fresh.metric_attempts,
                "next_metric_at": fresh.next_metric_at,
                "error": reason,
            },
        )
        return "deferred"

    def _emit(self, level: str, message: str, *, payload: Mapping[str, Any]) -> None:
        if self._log is not None:
            self._log(level=level, source="publish.metrics", message=message, payload=payload)  # type: ignore[arg-type]
            return
        if level == "info":
            logger.info(message, **payload)
        elif level == "warn":
            logger.warning(message, **payload)
        else:
            logger.error(message, **payload)
