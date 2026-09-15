"""观测面板一次读全（T4.12 · §04.5.11）。

面板要回答的五个问题
--------------------
① "机器还撑得住吗？" ⇒ 资源（CPU / 内存 / 显存 / 磁盘水位）；
② "队列堵在哪？" ⇒ 四池的 `pending / running / backlog / dead`；
③ "今天出了多少？" ⇒ 今日产量（**本地日**口径）；
④ "进程还齐吗？" ⇒ 五进程就绪；
⑤ **"出事了还有得救吗？"** ⇒ 备份新鲜度 + 存储体检。

为什么复刻总览台的一部分
------------------------
前四块**直接复用** :meth:`OverviewService.read` 的同一份数字（同一口径、同一时刻），
这里只补它没有的第五块。抄一遍那四块的 SQL，就是在给"命令行说 12 GB、网页说 15 GB"
这类查不完的悬案交学费（与 `metrics_service` 放在 services 层同一条理由）。

为什么第五块值得单独存在
------------------------
"备份任务悄悄坏了一周"这件事，磁盘上**看不出来** —— 目录里一直有文件，只是都是旧的。
所以这里报的不是"有没有备份"，而是 :attr:`BackupHealth.age_hours`（最新一份距今几小时）
与 :attr:`BackupHealth.stale`。同理，`db_freelist_bytes` 回答"删掉的旧行到底还了没有"。

代价如实说
----------
存储体检是**递归统计文件**（TTS 缓存 / 热点归档 / tmp），不是一次 `stat`。
所以它按**请求**跑，不进 5s 轮询 —— 面板上是"刷新一下"的按钮，不是自动跳动的数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from studio.core.clock import utc_now
from studio.core.paths import StudioPaths
from studio.db.backup import list_backups, snapshot_age_hours
from studio.db.engine import footprint
from studio.gc.media import tree_size
from studio.gc.policy import resolve_policy
from studio.services.overview_service import OverviewService, PoolCard

__all__ = [
    "BACKUP_STALE_HOURS",
    "BackupHealth",
    "MetricsSnapshot",
    "ObservabilityService",
    "StorageHealth",
]

#: 备份"多久算旧"：日备是每天 03:00 产的 ⇒ 超过两天没有新的，就不只是"今天还没跑"
BACKUP_STALE_HOURS: Final[float] = 48.0


@dataclass(frozen=True, slots=True)
class BackupHealth:
    """备份的**新鲜度**（不是"有没有" —— 见模块 docstring）。"""

    dir: str
    count: int
    newest: str | None
    age_hours: float | None
    total_bytes: int
    stale: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir": self.dir,
            "count": self.count,
            "newest": self.newest,
            "age_hours": self.age_hours,
            "total_bytes": self.total_bytes,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class StorageHealth:
    """存储体检（DB 空洞 + 三个会自己长大的目录）。"""

    db_bytes: int
    db_freelist_bytes: int
    tts_cache_bytes: int
    tts_cache_limit_bytes: int
    hot_archive_bytes: int
    tmp_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_bytes": self.db_bytes,
            "db_freelist_bytes": self.db_freelist_bytes,
            "tts_cache_bytes": self.tts_cache_bytes,
            "tts_cache_limit_bytes": self.tts_cache_limit_bytes,
            "hot_archive_bytes": self.hot_archive_bytes,
            "tmp_bytes": self.tmp_bytes,
        }


@dataclass(slots=True)
class MetricsSnapshot:
    """观测面板一次读全（`GET /api/v1/metrics` 的响应体）。"""

    generated_at: str
    pools: tuple[dict[str, Any], ...]
    today: dict[str, Any]
    services: tuple[dict[str, Any], ...]
    resources: dict[str, Any] | None
    resources_age_sec: float | None
    disk_low: bool | None
    worker_total: int
    worker_alive: int
    backups: BackupHealth
    storage: StorageHealth

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "pools": [dict(card) for card in self.pools],
            "today": dict(self.today),
            "services": [dict(item) for item in self.services],
            "resources": None if self.resources is None else dict(self.resources),
            "resources_age_sec": self.resources_age_sec,
            "disk_low": self.disk_low,
            "worker_total": self.worker_total,
            "worker_alive": self.worker_alive,
            "backups": self.backups.to_dict(),
            "storage": self.storage.to_dict(),
        }


class ObservabilityService:
    """观测面板的读取入口（**只读**：这一屏一个写动作都没有）。"""

    def __init__(
        self,
        connection: Any,
        *,
        paths: StudioPaths,
        overview: OverviewService,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._overview = overview

    def read(self, *, now: datetime | None = None) -> MetricsSnapshot:
        moment = now or utc_now()
        base = self._overview.read(now=moment)
        return MetricsSnapshot(
            generated_at=base.generated_at,
            pools=tuple(_pool_metric(card) for card in base.pools),
            today=base.today.to_dict(),
            services=tuple(item.to_dict() for item in base.services),
            resources=None if base.resources is None else base.resources.to_dict(),
            resources_age_sec=base.resources_age_sec,
            disk_low=base.disk_low,
            worker_total=base.worker_total,
            worker_alive=base.worker_alive,
            backups=self._backups(moment),
            storage=self._storage(),
        )

    def _backups(self, moment: datetime) -> BackupHealth:
        entries = list_backups(self._paths.backups_dir)
        age = snapshot_age_hours(entries, now=moment)
        return BackupHealth(
            dir=str(self._paths.backups_dir),
            count=len(entries),
            newest=None if not entries else entries[0].label,
            age_hours=age,
            total_bytes=sum(item.size_bytes for item in entries),
            stale=age is None or age > BACKUP_STALE_HOURS,
        )

    def _storage(self) -> StorageHealth:
        policy, _ = resolve_policy(self._paths)
        db_bytes, freelist = footprint(self._connection)
        return StorageHealth(
            db_bytes=db_bytes,
            db_freelist_bytes=freelist,
            tts_cache_bytes=tree_size(self._paths.tts_cache_dir),
            tts_cache_limit_bytes=policy.tts_cache_limit_bytes,
            hot_archive_bytes=tree_size(self._paths.hot_archive_dir),
            tmp_bytes=tree_size(self._paths.tmp_dir),
        )


def _pool_metric(card: PoolCard) -> dict[str, Any]:
    """池卡片压成观测面板要的那几列（**不**含 worker 明细：那一屏在四池控制台）。"""
    return {
        "pool": card.pool,
        "pending": card.pending,
        "running": card.running,
        "backlog": card.backlog,
        "dead": card.dead,
        "failed": card.failed,
        "paused": card.paused,
        "alive_workers": card.alive_workers,
        "oldest_pending_age_sec": card.oldest_pending_age_sec,
    }
