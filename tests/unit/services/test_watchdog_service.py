"""无人值守守护单元测试（T4.11 · §01.2.1 / §03.7.5）。

验收四条（todolist T4.11）
-------------------------
① 杀任一 worker ⇒ 15s 内被重启并恢复认领（判死靠 PID 台账，**不是** 15s 心跳）；
② 崩溃重启走指数退避 + **次数上限**（超限停手 + 告警，防重启风暴）；
③ `free_D < 门禁` ⇒ 暂停 `render` / `publish` 认领；水位恢复 ⇒ 只放开**自己暂停的**；
④ `attempt_count ≥ 3` ⇒ `manual_pool` 且总览台可见。

两条"必须做到"的边界（比上面的正常路径更容易写错）
--------------------------------------------------
- **守护者守不了自己**：`api` 死了没有任何进程内机制能救它。报告里必须如实标
  `guarded=False`，而不是画一个"守护中"的绿灯；
- **不猜**：没采过磁盘、没有显存读数 ⇒ 对应分支整体跳过。基于猜测去暂停别人的池，
  比不暂停坏得多。

测试纪律
--------
1. **临时家目录**：`config/pools.yaml` 与 `config/app.yaml` 抄进 tmp —— 守护会**真的
   写** `pool_settings` 与 `audit_ops`，拿仓库根当 home 会改开发机的库；
2. **假编排器**：`ServiceManager` 换成只回答"进程还在不在 / 拉起成不成"的假件，
   用例不碰真进程（否则会真的把 worker 拉起来）；
3. **假探针**：资源采样注入，用例不碰 `nvidia-smi` 与真磁盘。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from studio.core.clock import now_iso, utc_now
from studio.core.config import (
    PoolName,
    PoolsConfig,
    RuntimeSettings,
    load_pools_config,
    load_runtime_settings,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import JobStore
from studio.db.engine import connect
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.services.log_service import LogService
from studio.services.metrics_service import MetricsService, ResourceSnapshot
from studio.services.overview_service import OverviewService
from studio.services.service_manager import (
    Readiness,
    ServiceManager,
    ServiceReadiness,
    ServiceStatus,
    StartReport,
)
from studio.services.watchdog_service import (
    AUTO_PAUSE_ACTOR,
    AUTORECOVER_CODE,
    LOG_SOURCE,
    MANUAL_TICK_CODE,
    OVERRIDDEN_CODE,
    RESTART_HALTED_CODE,
    RESTARTED_CODE,
    WatchdogService,
    WatchdogTick,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_NAMES = ("api", "tts", "draft", "voice", "render")


# ══════════════════════════════════════════════════════════════════════
# 假件
# ══════════════════════════════════════════════════════════════════════


class FakeManager:
    """假编排器：只回答"进程还在不在"与"拉起它成不成"。

    ``start`` 会记下每次被要求拉起的进程名 —— "重启了几次"这条验收就是靠它数出来的。
    """

    def __init__(
        self,
        *,
        alive: tuple[str, ...] = SERVICE_NAMES,
        reported: tuple[str, ...] = SERVICE_NAMES,
        start_error: StudioError | None = None,
        landed: bool = True,
        ready: bool = True,
    ) -> None:
        self.alive = set(alive)
        self.reported = reported
        self.start_error = start_error
        self.landed = landed
        self.ready = ready
        self.starts: list[tuple[str, ...]] = []
        self.status_error: StudioError | None = None

    def status(self) -> tuple[ServiceStatus, ...]:
        if self.status_error is not None:
            raise self.status_error
        return tuple(
            ServiceStatus(
                name=name,
                pid=1000 + index,
                alive=name in self.alive,
                port=None,
                port_open=False,
                log_file=Path("logs") / f"{name}.log",
            )
            for index, name in enumerate(self.reported)
        )

    def spec(self, name: str) -> str:
        return name

    def readiness(self, spec: str) -> ServiceReadiness:
        if self.ready:
            return ServiceReadiness(name=spec, readiness=Readiness.READY, detail="就绪")
        return ServiceReadiness(
            name=spec,
            readiness=Readiness.HANDLER_MISSING,
            detail="单元处理器尚未落地",
            remediation="等 T2.6 / T3.x",
        )

    def start(
        self,
        *,
        only: list[str] | None = None,
        open_browser: bool = False,
        doctor_gate: bool = True,
        ready_timeout_sec: float | None = None,
    ) -> StartReport:
        del open_browser, doctor_gate, ready_timeout_sec
        names = tuple(only or ())
        self.starts.append(names)
        if self.start_error is not None:
            raise self.start_error
        for name in names:
            if self.landed:
                self.alive.add(name)
        return StartReport(
            started=names if self.landed else (),
            ready=names if self.landed else (),
            failed=() if self.landed else names,
        )


class FakeProbe:
    """可编排的资源探针（默认磁盘充裕、显存 1/8）。"""

    def __init__(self, *, free_d_gb: float = 100.0, gpu_used_mb: int = 1024) -> None:
        self.free_d_gb = free_d_gb
        self.gpu_used_mb = gpu_used_mb

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at=now_iso(),
            cpu_pct=12.5,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=50.0,
            disk_free_d_gb=self.free_d_gb,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=self.free_d_gb < 15.0,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=self.gpu_used_mb,
            gpu_mem_total_mb=8192,
        )


# ══════════════════════════════════════════════════════════════════════
# 夹具与小工具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """临时家目录：抄真 `config/pools.yaml` 与 `config/app.yaml`（守护会**真的写盘**）。"""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    for name in ("pools", "app"):
        shutil.copyfile(REPO_ROOT / "config" / f"{name}.yaml", root / "config" / f"{name}.yaml")
    return root


@pytest.fixture
def paths(home: Path, tmp_path: Path) -> StudioPaths:
    resolved = StudioPaths(home=home, data_dir=tmp_path / "data")
    resolved.ensure_runtime_dirs()
    migrate(resolved.db_file)
    return resolved


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    opened = connect(paths.db_file)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def logs(connection: sqlite3.Connection) -> LogService:
    return LogService(lambda: connection)


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


@pytest.fixture
def settings(paths: StudioPaths) -> RuntimeSettings:
    return load_runtime_settings(paths)


@pytest.fixture
def pools(paths: StudioPaths) -> PoolsConfig:
    return load_pools_config(paths)


@pytest.fixture
def metrics(paths: StudioPaths, logs: LogService, probe: FakeProbe) -> MetricsService:
    """真采样器 + 假探针（**采一拍**，否则门禁会因为"没数据"整体跳过）。"""
    service = MetricsService(
        paths=paths,
        log=logs,
        free_c_min_gb=1.0,
        free_d_min_gb=15.0,
        probe=probe,
    )
    service.sample()
    return service


#: `pools=` 不传 ⇒ 读真 `config/pools.yaml`；显式传 `None` ⇒ "读不到"那条分支
_UNSET: Any = object()


def make_watchdog(
    paths: StudioPaths,
    connection: sqlite3.Connection,
    logs: LogService,
    *,
    manager: FakeManager | None = None,
    metrics: MetricsService | None = None,
    settings: RuntimeSettings | None = None,
    pools: Any = _UNSET,
    self_name: str | None = "api",
    now: Any = None,
) -> WatchdogService:
    """造一个守护（管理器是假件 —— 用例绝不真的拉起进程）。

    默认读真 `config/pools.yaml`（夹具已把它抄进 tmp）；``pools=None`` 表示
    "读不到这份配置"，那是**另一条分支**（守护整体停用）。
    """
    resolved_pools: PoolsConfig | None = load_pools_config(paths) if pools is _UNSET else pools
    return WatchdogService(
        paths=paths,
        connection_factory=lambda: connection,
        manager=cast(ServiceManager, manager if manager is not None else FakeManager()),
        log=logs,
        metrics=metrics,
        settings=settings if settings is not None else load_runtime_settings(paths),
        pools_config=resolved_pools,
        self_name=self_name,
        now=now if now is not None else utc_now,
    )


def with_watchdog(pools: PoolsConfig, **updates: Any) -> PoolsConfig:
    """在真 `pools.yaml` 的基础上改几个守护阈值（**只改内存**，不动盘上的那一份）。"""
    return pools.model_copy(update={"watchdog": pools.watchdog.model_copy(update=updates)})


def with_pool_concurrency(pools: PoolsConfig, pool: PoolName, value: int) -> PoolsConfig:
    """改一个池的**配置**并发（`auto_concurrency` 的上限取自它）。"""
    current = dict(pools.pools)
    current[pool] = current[pool].model_copy(update={"concurrency": value})
    return pools.model_copy(update={"pools": current})


def guards(tick: WatchdogTick) -> dict[str, Any]:
    return {guard.name: guard for guard in tick.services}


def rows_of(logs: LogService, *, code: str | None = None) -> list[Any]:
    """`source='watchdog'` 的日志行（`code` 非空时按 payload 里的 code 过滤）。"""
    found = [row for row in logs.recent(limit=200) if row.source == LOG_SOURCE]
    if code is None:
        return found
    return [row for row in found if row.payload.get("code") == code]


def add_task(connection: sqlite3.Connection, task_id: str, **columns: Any) -> None:
    """插一条任务（**只用于"任务已经在某个状态"的前置条件**）。"""
    columns.setdefault("title", f"任务 {task_id}")
    columns.setdefault("status", "rendering")
    names = ["id", *columns]
    placeholders = ", ".join("?" for _ in names)
    connection.execute(
        f"INSERT INTO tasks({', '.join(names)}) VALUES ({placeholders})",
        (task_id, *columns.values()),
    )


def add_dead_job(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    pool: str = "render",
    unit_type: str = "final",
    unit_ref: str = "final",
) -> str:
    """造一条**真死信**（走 `enqueue → claim → fail(retryable=False)`，不 UPDATE 状态）。"""
    store = JobStore(connection)
    job_id = store.enqueue(task_id=task_id, pool=pool, unit_type=unit_type, unit_ref=unit_ref)
    assert job_id is not None
    claimed = store.claim(pool=pool, worker_id=f"{pool}#1@test")
    assert claimed is not None and claimed.id == job_id
    store.fail(
        job_id=job_id,
        worker_id=f"{pool}#1@test",
        error_code="RENDER_FAILED",
        error_message="滤镜图不合法",
        retryable=False,
    )
    return job_id


def pool_row(connection: sqlite3.Connection, pool: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM pool_settings WHERE pool = ?", (pool,)).fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


def task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


# ══════════════════════════════════════════════════════════════════════
# ⓪ 开关与就绪：不猜、不假装
# ══════════════════════════════════════════════════════════════════════


class TestGating:
    def test_without_the_pools_config_the_guard_is_off(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """`pools.yaml` 读不到 ⇒ **整体停用**，并在 `detail` 里说清原因（不猜阈值）。"""
        watchdog = make_watchdog(paths, connection, logs, pools=None)
        assert watchdog.enabled is False
        assert watchdog.runnable is False
        assert watchdog.snapshot()["detail"] == "config/pools.yaml 读不到 ⇒ 守护停用"

        tick = watchdog.tick()
        assert tick.enabled is False
        assert tick.services == ()

    def test_runnable_needs_the_workers_dir(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """`workers/` 不在 ⇒ 一个池 worker 都拉不起来，守护**不起跳**（集成测试的家目录）。"""
        watchdog = make_watchdog(paths, connection, logs)
        assert watchdog.enabled is True
        assert watchdog.runnable is False

        paths.workers_dir.mkdir(parents=True, exist_ok=True)
        assert watchdog.runnable is True

    def test_the_snapshot_reports_the_thresholds(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """阈值必须和状态出现在同一个响应里：否则"3 是从哪来的"要去翻 YAML。"""
        snapshot = make_watchdog(paths, connection, logs).snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["self_name"] == "api"
        assert snapshot["tick_sec"] == 5.0
        assert snapshot["manual_pool_after"] == 3
        assert snapshot["last_tick_at"] is None
        assert snapshot["restarted_total"] == 0
        assert snapshot["manual_pool"] == []

    def test_a_broken_process_table_does_not_kill_the_tick(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """读进程表失败 ⇒ 如实记在 `errors` 里，**其余三项照跑**。"""
        manager = FakeManager()
        manager.status_error = StudioError(
            "台账文件坏了", code=ErrorCode.CONFIG_INVALID, remediation="删掉 logs/*.pid"
        )
        watchdog = make_watchdog(paths, connection, logs, manager=manager)
        tick = watchdog.tick()
        # 名单还在（每个进程如实报"不是已知进程"），失败只体现在 errors 里
        assert len(tick.services) == len(SERVICE_NAMES)
        assert all(guard.guarded is False for guard in tick.services)
        assert any("进程表读取失败" in item for item in tick.errors)


# ══════════════════════════════════════════════════════════════════════
# ① 进程守护：判死 / 退避 / 停手 / 不守自己
# ══════════════════════════════════════════════════════════════════════


class TestProcessGuard:
    def test_a_dead_process_is_restarted(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """杀任一 worker ⇒ 这一拍就发现并拉起（判死靠 PID 台账，**不是** 15s 心跳）。"""
        manager = FakeManager(alive=("api", "tts", "draft", "render"))
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        tick = watchdog.tick()

        assert tick.restarted == ("voice",)
        assert manager.starts == [("voice",)]
        assert guards(tick)["voice"].running is True
        assert guards(tick)["voice"].restarts == 1
        assert [row.payload["service"] for row in rows_of(logs, code=RESTARTED_CODE)] == ["voice"]

    def test_the_guard_cannot_guard_itself(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """`api` 死了没有任何进程内机制能救它 ⇒ **如实标不守**，不画绿灯。"""
        manager = FakeManager(alive=("tts", "draft", "voice", "render"))
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        tick = watchdog.tick()

        api = guards(tick)["api"]
        assert api.guarded is False
        assert api.running is False
        assert api.detail is not None and "start_all.ps1" in api.detail
        assert tick.restarted == ()
        assert manager.starts == []

    def test_a_process_missing_from_the_table_is_reported_not_restarted(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """守护名单里有、进程表里却没有 ⇒ 报出来（**不硬拉**：那多半是配置/台账不一致）。"""
        manager = FakeManager(reported=("api", "tts", "draft", "render"))
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        tick = watchdog.tick()

        voice = guards(tick)["voice"]
        assert voice.guarded is False
        assert voice.detail is not None and "不是已知进程" in voice.detail
        assert manager.starts == []

    def test_a_failed_restart_backs_off(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """拉起失败 ⇒ 记一次 + 写下退避落点；**下一拍不重复拉**（退避中）。"""
        manager = FakeManager(
            alive=("api", "tts", "draft", "render"),
            start_error=StudioError("端口被占", code=ErrorCode.SERVICE_START_BUSY),
        )
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        first = watchdog.tick()
        assert first.restarted == ()
        assert guards(first)["voice"].restarts == 1
        assert guards(first)["voice"].next_restart_at is not None

        manager.start_error = None
        second = watchdog.tick()
        assert manager.starts == [("voice",)]  # 第二拍还在退避窗口里
        assert guards(second)["voice"].detail is not None
        assert "退避" in guards(second)["voice"].detail

    def test_the_restart_limit_halts_and_alerts(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """超限 ⇒ **停手 + 告警**（防重启风暴把机器拖垮），之后一拍都不再拉。"""
        pools = with_watchdog(
            load_pools_config(paths), restart_limit=2, restart_base_ms=100, restart_max_ms=200
        )
        manager = FakeManager(alive=("api", "tts", "draft", "render"), landed=False)
        watchdog = make_watchdog(paths, connection, logs, manager=manager, pools=pools)

        base = datetime(2026, 9, 14, 0, 0, 0, tzinfo=UTC)
        verdicts = [watchdog.tick(now=base + timedelta(minutes=index)) for index in range(4)]

        assert len(manager.starts) == 2  # 只拉了两次
        assert verdicts[-1].halted == ("voice",)
        assert guards(verdicts[-1])["voice"].halted is True
        assert rows_of(logs, code=RESTART_HALTED_CODE) != []

    def test_the_window_rolls_over(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """ "十分钟前崩过"不该压死现在：窗口外的重启次数**不再算数**。"""
        pools = with_watchdog(load_pools_config(paths), restart_base_ms=100, restart_max_ms=200)
        manager = FakeManager(
            alive=("api", "tts", "draft", "render"),
            start_error=StudioError("起不来", code=ErrorCode.CONFIG_INVALID),
        )
        watchdog = make_watchdog(paths, connection, logs, manager=manager, pools=pools)

        base = datetime(2026, 9, 14, 0, 0, 0, tzinfo=UTC)
        watchdog.tick(now=base)
        watchdog.tick(now=base + timedelta(minutes=1))
        last = watchdog.last_tick
        assert last is not None
        assert guards(last)["voice"].restarts == 2

        later = watchdog.tick(now=base + timedelta(seconds=pools.watchdog.restart_window_sec + 60))
        assert guards(later)["voice"].restarts == 1

    def test_the_window_is_seeded_from_the_log(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """守护自己重启过 ⇒ 计数**从留痕里读回来**，否则"超限停手"会被一次重启抹掉。"""
        for _ in range(2):
            logs.append(
                level="warn",
                source=LOG_SOURCE,
                message="voice 进程已死，第 N 次重启",
                payload={"code": RESTARTED_CODE, "service": "voice"},
            )
        pools = with_watchdog(load_pools_config(paths), restart_limit=2)
        manager = FakeManager(alive=("api", "tts", "draft", "render"))
        watchdog = make_watchdog(paths, connection, logs, manager=manager, pools=pools)

        tick = watchdog.tick()

        assert tick.halted == ("voice",)
        assert manager.starts == []  # 窗口内已经用掉 2 次 ⇒ 直接停手

    def test_a_not_ready_process_is_not_restarted(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """**不静默起一个空转 worker**（裁定 103）：未就绪 ⇒ 记在报告里，不拉。"""
        manager = FakeManager(alive=("api", "tts", "draft", "render"), ready=False)
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        tick = watchdog.tick()

        assert manager.starts == []
        assert tick.restarted == ()
        assert "未就绪" in (guards(tick)["voice"].detail or "")


# ══════════════════════════════════════════════════════════════════════
# ② 磁盘水位门禁
# ══════════════════════════════════════════════════════════════════════


class TestDiskGate:
    def test_low_water_pauses_render_and_publish(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
        probe: FakeProbe,
        settings: RuntimeSettings,
    ) -> None:
        """`free_D < 门禁` ⇒ 暂停 `render` / `publish` 认领（§01.7）。"""
        probe.free_d_gb = 5.0
        metrics.sample()
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, settings=settings)

        tick = watchdog.tick()

        assert tick.disk_gate.low is True
        assert tick.disk_gate.applied == ("render", "publish")
        for pool in ("render", "publish"):
            row = pool_row(connection, pool)
            assert row["paused"] == 1
            assert row["paused_by"] == AUTO_PAUSE_ACTOR  # 归属标记：谁因为什么暂停的
        assert pool_row(connection, "voice")["paused"] == 0  # 只动门禁名单里的池

    def test_recovery_releases_what_it_paused(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
        probe: FakeProbe,
        settings: RuntimeSettings,
    ) -> None:
        """水位恢复 ⇒ 放开**自己暂停的那几个**，并留一条"已放开"的日志。"""
        probe.free_d_gb = 5.0
        metrics.sample()
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, settings=settings)
        watchdog.tick()

        probe.free_d_gb = 100.0
        metrics.sample()
        tick = watchdog.tick()

        assert tick.disk_gate.low is False
        assert tick.disk_gate.released == ("render", "publish")
        for pool in ("render", "publish"):
            row = pool_row(connection, pool)
            assert row["paused"] == 0
            assert row["paused_by"] is None

    def test_a_manual_pause_is_never_touched(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
        probe: FakeProbe,
        settings: RuntimeSettings,
    ) -> None:
        """人按下的暂停只有人能取消：水位恢复后**一个字都不写**。"""
        OverviewService(connection, paths=paths, settings=settings).set_pool_paused(
            pool="render", paused=True, reason="我先手工停一下"
        )
        probe.free_d_gb = 5.0
        metrics.sample()
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, settings=settings)

        low = watchdog.tick()
        assert low.disk_gate.applied == ("publish",)
        assert low.disk_gate.skipped == ("render",)

        probe.free_d_gb = 100.0
        metrics.sample()
        recovered = watchdog.tick()

        assert recovered.disk_gate.released == ("publish",)
        render = pool_row(connection, "render")
        assert render["paused"] == 1
        assert render["paused_by"] == "user"

    def test_no_sample_means_no_action(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """还没采过资源 ⇒ **不猜**（拿"没数据"当"水位正常"会把暂停过的池提前放开）。"""
        fresh = MetricsService(paths=paths, log=logs, probe=FakeProbe())
        watchdog = make_watchdog(paths, connection, logs, metrics=fresh)

        tick = watchdog.tick()

        assert tick.disk_gate.low is None
        assert tick.disk_gate.applied == ()
        assert tick.disk_gate.detail is not None and "还没有资源采样" in tick.disk_gate.detail

    def test_the_global_switch_turns_the_gate_off(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
        probe: FakeProbe,
        settings: RuntimeSettings,
    ) -> None:
        """`pause_pools_on_low = false` ⇒ 只告警不暂停（阈值可配的兜底开关）。"""
        probe.free_d_gb = 5.0
        metrics.sample()
        watchdog = make_watchdog(
            paths,
            connection,
            logs,
            metrics=metrics,
            settings=replace(settings, pause_pools_on_low=False),
        )

        tick = watchdog.tick()

        assert tick.disk_gate.enabled is False
        assert tick.disk_gate.applied == ()
        assert pool_row(connection, "render")["paused"] == 0

    def test_a_manual_override_is_respected_and_self_clears(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
        probe: FakeProbe,
        settings: RuntimeSettings,
    ) -> None:
        """水位误判时人能覆盖；水位恢复后覆盖**自清**（不能变成永久豁免）。"""
        probe.free_d_gb = 5.0
        metrics.sample()
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, settings=settings)
        watchdog.tick()
        assert pool_row(connection, "render")["paused_by"] == AUTO_PAUSE_ACTOR

        # 人把 render 放开（"这一条今天必须出"）—— 留痕就是覆盖的凭据
        OverviewService(connection, paths=paths, settings=settings).set_pool_paused(
            pool="render", paused=False, reason="磁盘我知道，这条先跑"
        )

        overridden = watchdog.tick()
        assert overridden.disk_gate.overridden == ("render",)
        assert overridden.disk_gate.applied == ()
        assert pool_row(connection, "render")["paused"] == 0
        assert rows_of(logs, code=OVERRIDDEN_CODE) != []

        # 水位恢复 ⇒ 写一条"解除覆盖"，覆盖状态从推导里消失
        probe.free_d_gb = 100.0
        metrics.sample()
        watchdog.tick()
        cleared = [
            row
            for row in AuditRepo(connection).list_recent(limit=50)
            if row.action == "pool.override_cleared"
        ]
        assert [row.target_id for row in cleared] == ["render"]

        # 再低 ⇒ 门禁重新生效（覆盖只覆盖"这一轮水位"，不是永久豁免）
        probe.free_d_gb = 5.0
        metrics.sample()
        again = watchdog.tick()
        assert again.disk_gate.applied == ("render", "publish")


# ══════════════════════════════════════════════════════════════════════
# ③ 显存自动回升（T4.10 只做了"降"，这里把"升"补上）
# ══════════════════════════════════════════════════════════════════════


class TestAutoRecover:
    def _degrade(
        self,
        connection: sqlite3.Connection,
        *,
        pool: str,
        concurrency: int,
        action: str,
        moment: datetime,
    ) -> None:
        """把 `pool` 的运行时并发**先抬一格**，再落到 `concurrency`。

        为什么不能直接设目标值：`set_concurrency` 是幂等的（值没变 ⇒ 不留痕），而
        `config/pools.yaml` 里 voice 池的并发恰好就是 1 —— 直接"降到 1"什么都没发生，
        锚点为空、回升分支根本没被走到，用例会**假绿**。先抬一格，这次变更才是真的。
        """
        store = JobStore(connection)
        store.set_concurrency(pool=pool, concurrency=concurrency + 1, now=moment - timedelta(minutes=1))
        store.set_concurrency(
            pool=pool,
            concurrency=concurrency,
            actor="auto" if action == "pool.autodegrade" else "user",
            actor_ref="auto:watchdog" if action == "pool.autodegrade" else None,
            source="auto" if action == "pool.autodegrade" else "webui",
            action=action,
            now=moment,
        )

    def test_concurrency_creeps_back_up(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
    ) -> None:
        """`recover_after_min` 无 OOM + 显存低于阈值 + 上次变更是自动降级 ⇒ +1。"""
        pools = with_pool_concurrency(load_pools_config(paths), "voice", 3)
        base = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
        self._degrade(
            connection,
            pool="voice",
            concurrency=1,
            action="pool.autodegrade",
            moment=base - timedelta(minutes=6),
        )
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, pools=pools)

        tick = watchdog.tick(now=base)

        assert tick.recovered == ("voice",)
        assert JobStore(connection).pool_runtime("voice").concurrency == 2
        assert rows_of(logs, code=AUTORECOVER_CODE) != []

    def test_a_manual_change_is_not_touched(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        metrics: MetricsService,
    ) -> None:
        """最近一次并发是**人**定的 ⇒ 一个数都不动（那是人的决定，不是抖动的产物）。"""
        pools = with_pool_concurrency(load_pools_config(paths), "voice", 3)
        base = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
        self._degrade(
            connection,
            pool="voice",
            concurrency=1,
            action="pool.set_concurrency",
            moment=base - timedelta(minutes=30),
        )
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, pools=pools)

        tick = watchdog.tick(now=base)

        assert tick.recovered == ()
        assert JobStore(connection).pool_runtime("voice").concurrency == 1

    def test_high_gpu_memory_blocks_recovery(
        self,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        logs: LogService,
        probe: FakeProbe,
    ) -> None:
        """显存还在高位 ⇒ 不回升（回升的前提是"真的空出来了"）。"""
        pools = with_pool_concurrency(load_pools_config(paths), "voice", 3)
        base = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
        self._degrade(
            connection,
            pool="voice",
            concurrency=1,
            action="pool.autodegrade",
            moment=base - timedelta(minutes=30),
        )
        probe.gpu_used_mb = 7000  # 85% > gpu_mem_high_ratio(60%)
        metrics = MetricsService(paths=paths, log=logs, probe=probe)
        metrics.sample()
        watchdog = make_watchdog(paths, connection, logs, metrics=metrics, pools=pools)

        tick = watchdog.tick(now=base)

        assert tick.recovered == ()
        assert JobStore(connection).pool_runtime("voice").concurrency == 1


# ══════════════════════════════════════════════════════════════════════
# ④ 任务收尾清扫（死信 ⇒ failed；反复失败 ⇒ manual_pool）
# ══════════════════════════════════════════════════════════════════════


class TestSweep:
    def test_a_dead_unit_fails_the_task(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """单元进死信 ⇒ 任务 `failed`（否则任务永远停在"看起来在跑"）。"""
        add_task(connection, "T-DEAD", status="rendering")
        add_dead_job(connection, task_id="T-DEAD")
        watchdog = make_watchdog(paths, connection, logs)

        tick = watchdog.tick()

        assert tick.sweep.failed == ("T-DEAD",)
        row = task_row(connection, "T-DEAD")
        assert row["status"] == "failed"
        assert row["error_code"] == "RENDER_FAILED"

    def test_a_finished_task_is_never_touched(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """已经 `completed` 的任务再"失败"一次只会多写一条无意义的事件。"""
        add_task(connection, "T-DONE", status="completed")
        add_dead_job(connection, task_id="T-DONE")
        watchdog = make_watchdog(paths, connection, logs)

        tick = watchdog.tick()

        assert tick.sweep.failed == ()
        assert task_row(connection, "T-DONE")["status"] == "completed"

    def test_repeated_failures_go_to_the_manual_pool(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """`attempt_count ≥ 3` ⇒ `manual_pool`，且**在快照里看得见**（P4）。"""
        add_task(
            connection,
            "T-POOL",
            status="failed",
            attempt_count=3,
            retry_from="rendering",
            error_code="RENDER_FAILED",
            stage_detail="render: 2/5 场景",
        )
        watchdog = make_watchdog(paths, connection, logs)

        tick = watchdog.tick()

        assert tick.sweep.manual_pool == ("T-POOL",)
        assert task_row(connection, "T-POOL")["status"] == "manual_pool"

        rows = watchdog.snapshot()["manual_pool"]
        assert [row["task_id"] for row in rows] == ["T-POOL"]
        assert rows[0]["attempt_count"] == 3
        assert rows[0]["retry_from"] == "rendering"  # 断点留着 ⇒ 人知道该从哪继续
        assert rows[0]["error_code"] == "RENDER_FAILED"
        assert rows[0]["stage_detail"] == "render: 2/5 场景"

    def test_a_task_below_the_threshold_stays_failed(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """还没到阈值 ⇒ 留在 `failed`（队列还会重投，别提前塞进人工池）。"""
        add_task(connection, "T-SHY", status="failed", attempt_count=2)
        watchdog = make_watchdog(paths, connection, logs)

        tick = watchdog.tick()

        assert tick.sweep.manual_pool == ()
        assert task_row(connection, "T-SHY")["status"] == "failed"


# ══════════════════════════════════════════════════════════════════════
# ⑤ 人工触发一轮（WebUI 的"立即自检"）
# ══════════════════════════════════════════════════════════════════════


class TestManualTick:
    def test_it_runs_a_round_and_writes_an_audit_row(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """跑的是**同一个** `tick()`，且留痕（`actor=user` / `source=webui`）。"""
        manager = FakeManager(alive=("api", "tts", "draft", "render"))
        watchdog = make_watchdog(paths, connection, logs, manager=manager)

        outcome = watchdog.manual_tick(reason="手工验证守护")

        assert outcome.tick.restarted == ("voice",)
        assert outcome.audit_id is not None
        row = AuditRepo(connection).list_recent(limit=5)[0]
        assert row.action == "watchdog.tick"
        assert row.actor == "user"
        assert row.source == "webui"
        assert row.reason == "手工验证守护"
        assert "restarted" in row.after
        assert rows_of(logs, code=MANUAL_TICK_CODE) != []
        assert outcome.to_dict()["audit_id"] == outcome.audit_id
        assert "重启 1 个进程" in outcome.note

    def test_a_disabled_guard_still_answers_and_still_leaves_a_trace(
        self, paths: StudioPaths, connection: sqlite3.Connection, logs: LogService
    ) -> None:
        """守护停用时**照跑照留痕**：回一句"本轮什么都没做"比一个 400 有用得多。"""
        watchdog = make_watchdog(paths, connection, logs, pools=None)

        outcome = watchdog.manual_tick()

        assert outcome.tick.enabled is False
        assert "停用" in outcome.note
        assert outcome.audit_id is not None
        assert AuditRepo(connection).list_recent(limit=5)[0].action == "watchdog.tick"
