"""总览台的请求 / 响应契约（T4.2 · §04.4.5 第 1 行）。

面板要回答的五个问题，一个模型对一块
------------------------------------
⓪ "这台机器还在自己照顾自己吗？" ⇒ :class:`~studio.app.schemas.watchdog.WatchdogStatusModel`
   （T4.11 补上：自愈次数 / 停手进程 / 磁盘门禁 / **人工池**）；
① "四个池现在什么情况？" ⇒ :class:`PoolStatus`（含 worker 心跳，一次给全）；
② "今天出了多少片？" ⇒ :class:`TodayOutputModel`（**本地日**口径，见 `stats_repo`）；
③ "机器还撑得住吗？" ⇒ :class:`ResourcesModel`（CPU / 内存 / 显存 / 磁盘水位）；
④ "我能按哪些按钮？" ⇒ :class:`SettingsModel`（当前放行策略）+ :class:`ServiceStatusModel`。

请求体为什么全部 ``extra="forbid"``
-----------------------------------
与确认闸、选题面板同一理由：把 ``paused`` 写成 ``pause`` 被静默忽略，
会让人以为"我已经暂停了"，而池还在照常认领 —— 这类"以为生效了"的错，
在暂停/全自动这两个动作上代价最大（一个白烧 GPU，一个绕过唯一的人工节点）。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from studio.app.schemas.watchdog import WatchdogStatusModel

__all__ = [
    "PolicyName",
    "PolicyRequest",
    "PolicyResponse",
    "PoolName",
    "PoolPauseRequest",
    "PoolPauseResponse",
    "PoolStatus",
    "ResourcesModel",
    "ServiceName",
    "ServiceStatusModel",
    "SettingsModel",
    "StartResponse",
    "TodayOutputModel",
    "WorkerStatus",
]

#: 池名（与 `pool_settings.pool` 的 CHECK 同源）
PoolName = Literal["draft", "voice", "render", "publish"]

#: 六进程名（与 `service_manager.SERVICE_NAMES` 同源）
ServiceName = Literal["api", "tts", "draft", "voice", "render"]

#: 放行策略（与 `config/app.yaml → approval.auto_approve_policy` 同源）
PolicyName = Literal["off", "grade_a", "grade_ab"]


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读 · 总览
# ══════════════════════════════════════════════════════════════════════


class WorkerStatus(BaseModel):
    """一个 worker 的心跳（`worker_heartbeats` 的展示面 · §04.5.1）。"""

    worker_id: str
    pool: str
    status: str
    current_job_id: str | None = None
    gpu_mem_mb: int | None = None
    rss_mb: int | None = None
    last_seen_at: str
    silent_sec: int
    stale: bool


class PoolStatus(BaseModel):
    """一个池的卡片（`pool.stats` 的展示面 · §04.4.3）。"""

    pool: str
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    canceled: int
    concurrency: int
    running: int
    paused: bool
    paused_at: str | None = None
    paused_by: str | None = None
    oldest_pending_age_sec: int | None = None
    backlog: int
    alive_workers: int
    workers: list[WorkerStatus]
    error: str | None = None


class TodayOutputModel(BaseModel):
    """今日产量（**本地日**口径 · `stats_repo.local_day_window`）。"""

    day: str
    window_start: str
    window_end: str
    created: int
    completed: int
    published: int
    failed: int
    by_status: dict[str, int]
    by_grade: dict[str, int]


class ResourcesModel(BaseModel):
    """资源占用（`metrics.tick` 的载荷 · §04.4.3）。"""

    sampled_at: str
    cpu_pct: float
    ram_mb: int
    ram_total_mb: int
    ram_pct: float
    gpu_util: float | None = None
    gpu_mem_mb: int | None = None
    gpu_mem_total_mb: int | None = None
    gpu_name: str | None = None
    disk_free_gb: float
    disk_free_c_gb: float
    disk_free_d_min_gb: float
    disk_drive: str
    disk_low: bool
    process_rss_mb: int
    tts_rtf_avg: float | None = None
    render_fps: float | None = None


class SettingsModel(BaseModel):
    """运行期配置里**与面板有关**的那几个（`RuntimeSettings.to_dict`）。"""

    auto_approve_policy: str
    free_c_min_gb: float
    free_d_min_gb: float
    pause_pools_on_low: bool


class ServiceStatusModel(BaseModel):
    """一个进程的就绪结论（`ServiceReadiness.to_dict` · §04.8.1）。"""

    name: str
    readiness: str
    ready: bool
    detail: str
    remediation: str | None = None


class OverviewResponse(BaseModel):
    """`GET /api/v1/overview` —— 一次拿全（与 T4.4「一次请求拿全」同一取舍）。"""

    generated_at: str
    pools: list[PoolStatus]
    today: TodayOutputModel
    resources: ResourcesModel | None = None
    resources_age_sec: float | None = None
    settings: SettingsModel
    services: list[ServiceStatusModel]
    worker_total: int
    worker_alive: int
    disk_low: bool | None = None
    #: 无人值守守护（T4.11）。与 `GET /api/v1/watchdog` **同一个来源**：这里给的是
    #: "一眼扫过"的那几个数（自愈次数 / 停手进程 / 门禁水位 / 人工池），
    #: 逐进程核对请去守护面板。`None` ⇒ 这套依赖没接守护。
    watchdog: WatchdogStatusModel | None = None


# ══════════════════════════════════════════════════════════════════════
# 写 · 暂停 / 恢复 / 一键全自动 / 启动
# ══════════════════════════════════════════════════════════════════════


class PoolPauseRequest(_Body):
    """暂停 / 恢复一个池（`paused` 是**目标状态**，不是"切换"）。"""

    pool: PoolName
    paused: bool
    reason: str | None = None


class PoolPauseResponse(BaseModel):
    """暂停 / 恢复的结论（`note` 直接给人看："在途 N 个跑完"）。"""

    pool: str
    paused: bool
    changed: bool
    running: int
    pending: int
    paused_at: str | None = None
    paused_by: str | None = None
    note: str


class PolicyRequest(_Body):
    """切自动放行策略（`grade_ab` = 一键全自动；`grade_a` = 一键回退）。"""

    policy: PolicyName
    reason: str | None = None


class PolicyResponse(BaseModel):
    """策略切换的结论（`config_path` 让"到底写到哪了"可查）。"""

    policy: str
    previous: str
    changed: bool
    config_path: str


class StartResponse(BaseModel):
    """`POST /api/v1/overview/start` 的结论（`StartReport.to_dict` · §04.8.2）。

    ``browser_opened`` 恒为 ``false``：从 WebUI 点"启动"时浏览器**已经开着**，
    再 `webbrowser.open` 一次只会多弹一个标签页（裁定 137）。
    """

    ok: bool
    started: list[str]
    ready: list[str]
    degraded: list[str]
    failed: list[str]
    already_running: list[str]
    port_busy: list[str]
    readiness: list[ServiceStatusModel]
    url: str
    browser_opened: bool
    elapsed_ms: int
