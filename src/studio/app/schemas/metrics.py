"""观测面板的响应契约（T4.12 · §04.5.11）。

为什么"备份"与"存储"要出现在**指标**面板上
------------------------------------------
指标不只是 CPU / 内存。"这台机器出事了还有得救吗"同样是一个可以被量化、
被画出来的指标 —— 而它恰好是最容易被忽略的一个：备份目录里一直有文件，
只是**都是旧的**；`DELETE` 一直在删行，只是**文件一直没变小**。
所以这两块给的不是"有没有"，而是 `age_hours`（最新一份距今几小时）与
`db_freelist_bytes`（删掉了但还没还给盘的量）。

复用总览台的三个模型
--------------------
`ResourcesModel` / `TodayOutputModel` / `ServiceStatusModel` 直接借用：
它们是**同一个数**（同一口径、同一时刻），另起一套名字只会让前端维护两份形状。
"""

from __future__ import annotations

from pydantic import BaseModel

from studio.app.schemas.overview import ResourcesModel, ServiceStatusModel, TodayOutputModel

__all__ = [
    "BackupHealthModel",
    "MetricsResponse",
    "PoolMetric",
    "StorageHealthModel",
]


class PoolMetric(BaseModel):
    """一个池的**计数器**（worker 明细在四池控制台那一屏）。"""

    pool: str
    pending: int
    running: int
    backlog: int
    dead: int
    failed: int
    paused: bool
    alive_workers: int
    oldest_pending_age_sec: int | None = None


class BackupHealthModel(BaseModel):
    """备份的**新鲜度**（`stale` = 最新一份距今超过 48 小时，或一份都没有）。"""

    dir: str
    count: int
    newest: str | None = None
    age_hours: float | None = None
    total_bytes: int
    stale: bool


class StorageHealthModel(BaseModel):
    """存储体检（DB 空洞 + 三个会自己长大的目录）。"""

    db_bytes: int
    db_freelist_bytes: int
    tts_cache_bytes: int
    tts_cache_limit_bytes: int
    hot_archive_bytes: int
    tmp_bytes: int


class MetricsResponse(BaseModel):
    """`GET /api/v1/metrics` —— 一次拿全（只读，没有任何写动作）。"""

    generated_at: str
    pools: list[PoolMetric]
    today: TodayOutputModel
    services: list[ServiceStatusModel]
    resources: ResourcesModel | None = None
    resources_age_sec: float | None = None
    disk_low: bool | None = None
    worker_total: int
    worker_alive: int
    backups: BackupHealthModel
    storage: StorageHealthModel
