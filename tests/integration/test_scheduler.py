"""定时发布调度集成测试（T5.6 验收 · §04.6.5.1 / §06.5.5）。

十条验收口径，一条一个用例
--------------------------
① 窗口模式在 ``[18:00,21:30]`` 内取时刻且叠加 <=15min 抖动（不越界）
② **同 schedule + 同一天多次计算得到同一时刻**（重启不漂移）
③ 未到点 / 已停用的计划**不**被取到
④ 到点 ⇒ 创建 ``publish`` 作业，且**不占 worker**（作业停在 ``pending``）
⑤ 被限频 ⇒ ``skipped_ratelimit`` + 顺延（**不算失败**，``fail_streak`` 归零）
⑥ ``publish.enabled=false`` ⇒ ``skipped_disabled``（空转，不建作业）
⑦ 连续失败 >=5 次 ⇒ ``system.alert(SCHEDULE_FAILING)``
⑧ 增 / 删 / 改 / 启停均写 ``audit_ops``
⑥′ 这条任务**早就投过**（幂等命中）⇒ ``skipped_duplicate``（**不是失败**，真机踩到）
⑨ 非法参数被拒且**不落库**
⑩ 编辑 ⇒ ``next_run_at`` **与参数在同一条 UPDATE 里**重算（陷阱 #33）

四条纪律（与 ``test_publish_pool.py`` 同一套）
----------------------------------------------
1. **临时家目录**：真配置抄一份进 tmp，只把顶格的 ``enabled`` 翻成 ``true``。
   拿仓库根当 home 跑的就是生产那份配置，改一次档位这里的断言就跟着飘。
2. **真库真盘**：计划状态、作业、留痕一律回库查。「REST 返回 200」只说明没抛。
3. **不碰真平台**：这些用例全都投到 ``other``（本地演练台）—— 它是唯一
   「发得出去但不会真发到网上」的目标，而 T5.6 验的是**调度**不是发布器。
4. **时刻一律显式传入**：``now=`` 到处带着走。调度器的正确性全在"相对某一刻"上，
   让用例自己去问系统时钟，边界用例就会在 18:00 前后随机红一次。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import format_iso, local_tz, parse_iso, utc_now
from studio.core.config import PlatformCode, PublishConfig, load_publish_config
from studio.core.paths import StudioPaths
from studio.core.proto import AlertCode, EventKind
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import PublicationRepo
from studio.db.repositories.schedule_repo import ScheduleRepo
from studio.domain.enums import TaskKind, TaskStatus
from studio.domain.publish import unit_ref
from studio.domain.schedule import next_run_at, window_moment
from studio.domain.task_service import TaskService
from studio.services.metrics_service import ResourceSnapshot
from studio.services.scheduler_service import (
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_DISABLE,
    AUDIT_UPDATE,
    RESULT_OK,
    RESULT_SKIPPED_DISABLED,
    RESULT_SKIPPED_DUPLICATE,
    RESULT_SKIPPED_RATELIMIT,
    SCHEDULER_TICK_INTERVAL_SEC,
    SchedulerService,
    create_schedule,
    delete_schedule,
    run_now,
    update_schedule,
)
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEDULES_URL = "/api/v1/schedules"

#: 任务状态机的「一路出片」路径（建任务 → 成片完成）
TO_COMPLETED: tuple[TaskStatus, ...] = (
    TaskStatus.DRAFTING,
    TaskStatus.REVIEWING,
    TaskStatus.QUEUED_VOICE,
    TaskStatus.VOICING,
    TaskStatus.QUEUED_RENDER,
    TaskStatus.RENDERING,
    TaskStatus.COMPLETED,
)

#: 演练台（T5.9）：唯一一个「发得出去但不会真发到网上」的目标。
REHEARSAL = "other"
REHEARSAL_ACCOUNT = "_rehearsal"

WINDOW: tuple[str, str] = ("18:00", "21:30")


class _Probe:
    """假资源探针（不碰真 `nvidia-smi` 与真磁盘）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-18T06:00:00.000Z",
            cpu_pct=10.0,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=50.0,
            disk_free_d_gb=100.0,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=False,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=8192,
        )


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真配置抄一份，只把**顶格**那一行 ``enabled`` 翻成 ``true``。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        shutil.copyfile(source, value.config_dir / source.name)
    publish = value.config_dir / "publish.yaml"
    text = publish.read_text(encoding="utf-8")
    # 只换顶格那一行：``handoff`` 与二线平台的 ``enabled`` 都是缩进的，不该跟着翻。
    publish.write_text(text.replace("\nenabled: false", "\nenabled: true"), encoding="utf-8")
    return value


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
    )
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def connection(state: AppState) -> sqlite3.Connection:
    return state.connections.get()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


@pytest.fixture
def config(paths: StudioPaths) -> PublishConfig:
    return load_publish_config(paths)


def make_completed_task(connection: sqlite3.Connection, *, title: str = "定时发布用") -> str:
    """建一条走完出片的任务（``completed``）—— 待发布池里的那一条。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title, kind=TaskKind.VIDEO, actor="test").id
    for status in TO_COMPLETED:
        tasks.transition(task_id, status, actor="test")
    return task_id


def service_for(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    config: PublishConfig,
    *,
    enabled: bool | None = None,
    log: object | None = None,
) -> SchedulerService:
    """造一个调度器；``enabled`` 非空 ⇒ 换掉总开关（验验收 ⑥）。

    ``log`` 只在要看 ``system_logs`` 的用例里给（告警与 WS 事件）——
    不给时事件退回进程日志，而"日志里没有"不等于"没发生"。
    """
    if enabled is not None:
        config = config.model_copy(update={"enabled": enabled})
    return SchedulerService(
        connection=connection,
        paths=paths,
        config=config,
        log=log,  # type: ignore[arg-type]
    )


def local_hm(row_next_run_at: str) -> str:
    """``next_run_at`` 的本地 ``HH:MM``（窗口断言要按本地时区看）。"""
    return parse_iso(row_next_run_at).astimezone(local_tz()).strftime("%H:%M")


def spec_for(
    *,
    mode: str = "daily_window",
    at_time: str | None = None,
    window: tuple[str, str] | None = WINDOW,
    interval_hours: int | None = None,
    jitter_min: int = 15,
    task_id: str | None = None,
    platforms: tuple[str, ...] = (REHEARSAL,),
    account_ids: tuple[str, ...] = (REHEARSAL_ACCOUNT,),
    enabled: bool = True,
) -> dict[str, object]:
    """一份计划参数（默认：演练台 + 18:00-21:30 窗口）。"""
    return {
        "mode": mode,
        "at_time": at_time,
        "window": None if window is None else list(window),
        "interval_hours": interval_hours,
        "jitter_min": jitter_min,
        "task_id": task_id,
        "platforms": list(platforms),
        "account_ids": list(account_ids),
        "enabled": enabled,
    }


def make_due_schedule(
    connection: sqlite3.Connection,
    config: PublishConfig,
    *,
    task_id: str | None,
    at_time: str | None = None,
) -> str:
    """建一条**已经到点**的计划（``at_time`` 模式，时刻刻意落在过去）。

    为什么不用 ``daily_window`` 造"到点"：窗口是每日的，今天那一格还没到的时候
    它是**未来** —— 用例会在每天 18:00 之前随机红一次。``at_time`` 是唯一一个
    能把"到点"这件事钉死的模式。
    """
    now = utc_now()
    moment = at_time or format_iso(now - timedelta(hours=1))
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(mode="at_time", at_time=moment, window=None, task_id=task_id),
        now=format_iso(now - timedelta(hours=2)),
    )
    return row.id


# ══════════════════════════════════════════════════════════════════════
# ① 窗口 + 抖动（不越界）
# ══════════════════════════════════════════════════════════════════════


def test_window_moment_stays_inside_window() -> None:
    """① 窗口模式在 ``[18:00,21:30]`` 内取时刻，抖动再大也不越界。

    扫 400 天而不是抽一天：越界只发生在窗口两端（抖动把它推出边界的那一格），
    抽一天有 1/400 的概率漏掉 —— 而这类"偶尔红一次"的用例最后都会被人加 xfail。
    """
    day = parse_iso(format_iso(utc_now())).astimezone(local_tz()).date()
    for offset in range(400):
        moment = window_moment(
            schedule_id="sched-fixed",
            day=day + timedelta(days=offset),
            window=WINDOW,
            jitter_min=120,
        )
        text = moment.strftime("%H:%M")
        assert "18:00" <= text <= "21:30", f"{moment} 跑出了窗口"


def test_window_moment_is_not_a_fixed_clock() -> None:
    """① 反面：窗口内取的时刻**不是**每天都一样（否则就是陷阱 #29 的机器特征）。"""
    day = parse_iso(format_iso(utc_now())).astimezone(local_tz()).date()
    moments = {
        window_moment(
            schedule_id="sched-fixed", day=day + timedelta(days=offset), window=WINDOW, jitter_min=15
        ).strftime("%H:%M")
        for offset in range(30)
    }
    assert len(moments) > 20, f"30 天里只取到 {len(moments)} 个不同时刻，随机性不够"


def test_next_run_at_daily_window_lands_inside_window() -> None:
    """① ``next_run_at`` 与 ``window_moment`` 是同一份算术（不是各写一遍）。"""
    now = parse_iso(format_iso(utc_now()))
    text = next_run_at(schedule_id="sched-a", mode="daily_window", now=now, window=WINDOW, jitter_min=15)
    assert text is not None
    assert "18:00" <= local_hm(text) <= "21:30"


# ══════════════════════════════════════════════════════════════════════
# ② 同一天同一时刻（重启不漂移）
# ══════════════════════════════════════════════════════════════════════


def test_same_schedule_same_day_yields_same_moment() -> None:
    """② 同 schedule + 同一天多次计算得到**同一时刻**（陷阱 #30 的另一半）。"""
    now = parse_iso(format_iso(utc_now()))
    day = now.astimezone(local_tz()).date()
    first = window_moment(schedule_id="sched-b", day=day, window=WINDOW, jitter_min=15)
    for _ in range(5):
        assert window_moment(schedule_id="sched-b", day=day, window=WINDOW, jitter_min=15) == first
    calls = [
        next_run_at(schedule_id="sched-b", mode="daily_window", now=now, window=WINDOW, jitter_min=15)
        for _ in range(3)
    ]
    assert len(set(calls)) == 1


def test_different_schedule_ids_get_different_moments() -> None:
    """② 反面：换一个 schedule_id 就该换一个时刻（否则所有计划挤在同一分钟发）。"""
    now = parse_iso(format_iso(utc_now()))
    day = now.astimezone(local_tz()).date()
    moments = {
        window_moment(schedule_id=f"sched-{index}", day=day, window=WINDOW, jitter_min=15)
        for index in range(30)
    }
    assert len(moments) > 20


# ══════════════════════════════════════════════════════════════════════
# ③ 未到点 / 已停用 不被取到
# ══════════════════════════════════════════════════════════════════════


def test_not_due_schedule_is_not_picked(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """③ 未到点 ⇒ ``list_due`` 取不到，``tick`` 一拍什么都不做。"""
    task_id = make_completed_task(connection)
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(mode="daily_window", task_id=task_id),
        now=format_iso(utc_now()),
    )
    assert row.next_run_at is not None and parse_iso(row.next_run_at) > utc_now()
    scheduler = service_for(connection, paths, config)
    assert scheduler.due(now=format_iso(utc_now())) == ()
    report = scheduler.tick(now=format_iso(utc_now()))
    assert report.due == 0
    assert ScheduleRepo(connection).require(row.id).last_result is None


def test_disabled_schedule_is_not_picked(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """③ 已停用 ⇒ 即便 ``next_run_at`` 是过去也不被取到（``enabled=1`` 是索引条件）。"""
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    repo = ScheduleRepo(connection)
    repo.set_enabled(schedule_id, enabled=False, next_run_at=None)
    scheduler = service_for(connection, paths, config)
    assert scheduler.due(now=format_iso(utc_now())) == ()
    assert repo.require(schedule_id).last_result is None


# ══════════════════════════════════════════════════════════════════════
# ④ 到点建作业，不占 worker
# ══════════════════════════════════════════════════════════════════════


def test_due_creates_job_without_occupying_worker(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """④ 到点 ⇒ 建 ``publish`` 作业，且作业**停在 pending**（调度器不认领）。

    "不占 worker 空转"的可观测形态就是这个：作业在那儿、``lease_owner`` 是空、
    池的 ``running`` 是 0。认领是 publish 池 worker 的事（§03.4），
    调度器多走一步就会把"两个进程抢同一份登录态"这类问题引进来。
    """
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config)
    report = scheduler.tick(now=format_iso(utc_now()))

    assert report.fired == (schedule_id,)
    assert len(report.job_ids) == 1
    job = JobStore(connection).get(report.job_ids[0])
    assert job.pool == "publish"
    assert job.unit_type == "publish"
    assert job.unit_ref == unit_ref(REHEARSAL, REHEARSAL_ACCOUNT)
    assert job.task_id == task_id
    assert job.status == "pending"
    assert job.lease_owner is None and job.lease_expires_at is None
    stats = JobStore(connection).stats(pool="publish")
    assert stats.pending == 1 and stats.running == 0

    fresh = ScheduleRepo(connection).require(schedule_id)
    assert fresh.last_result == RESULT_OK
    assert fresh.run_count == 1
    # 一次性计划跑完即停：留着 enabled=1 而 next_run_at=NULL 会显示"启用中"却永不触发。
    assert fresh.next_run_at is None
    assert fresh.enabled is False


def test_tick_is_idempotent_for_the_same_moment(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """④ 同一拍跑两次不会建出第二个作业（``next_run_at`` 已被推到未来）。"""
    task_id = make_completed_task(connection)
    make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config)
    moment = format_iso(utc_now())
    first = scheduler.tick(now=moment)
    second = scheduler.tick(now=moment)
    assert len(first.job_ids) == 1
    assert second.due == 0
    assert JobStore(connection).stats(pool="publish").pending == 1


# ══════════════════════════════════════════════════════════════════════
# ⑤ 被限频 ⇒ 顺延（不是失败）
# ══════════════════════════════════════════════════════════════════════


def _fill_today_quota(connection: sqlite3.Connection, count: int) -> None:
    """把今天的发布额度打满（``pool_settings.rate_limit_json`` 的 ``daily_limit=3``）。

    用**池级**那一份而不是账号级：守卫读的是池级（§03.4.4 ⑥），
    演练台账号自己的 ``daily_limit=100`` 在这里是不生效的 ——
    拿账号级去造场景会让用例"看着像限频、其实没限"。
    """
    repo = PublicationRepo(connection)
    for index in range(count):
        # 占位记录也要挂在真任务上：`publications.task_id` 有外键。
        task_id = make_completed_task(connection, title=f"额度占位 {index}")
        row, _ = repo.create(
            task_id=task_id,
            platform=REHEARSAL,
            account_id=REHEARSAL_ACCOUNT,
            video_path="data/output/videos/quota.mp4",
            video_sha256="a" * 64,
            title="额度占位",
        )
        repo.mark_published(row.id, url="https://example.com/quota", platform_post_id=f"q-{index}")


def test_ratelimit_defers_without_failing(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """⑤ 被限频 ⇒ ``skipped_ratelimit`` + 顺延；``fail_streak`` **不涨**。"""
    _fill_today_quota(connection, 3)
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config)
    report = scheduler.tick(now=format_iso(utc_now()))

    assert report.skipped_ratelimit == (schedule_id,)
    assert report.errors == () and report.job_ids == ()
    assert JobStore(connection).stats(pool="publish").pending == 0

    fresh = ScheduleRepo(connection).require(schedule_id)
    assert fresh.last_result == RESULT_SKIPPED_RATELIMIT
    assert fresh.fail_streak == 0
    assert fresh.next_run_at is not None
    assert parse_iso(fresh.next_run_at) > utc_now()


# ══════════════════════════════════════════════════════════════════════
# ⑥ publish.enabled=false ⇒ 空转
# ══════════════════════════════════════════════════════════════════════


def test_publish_disabled_records_skipped_disabled(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """⑥ 总开关关着 ⇒ 记 ``skipped_disabled``、**不建作业**，但仍然"拍了一下"。"""
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config, enabled=False)
    report = scheduler.tick(now=format_iso(utc_now()))

    assert report.skipped_disabled == (schedule_id,)
    assert report.job_ids == () and report.errors == ()
    assert JobStore(connection).stats(pool="publish").pending == 0

    fresh = ScheduleRepo(connection).require(schedule_id)
    assert fresh.last_result == RESULT_SKIPPED_DISABLED
    assert fresh.run_count == 1
    assert fresh.fail_streak == 0


# ══════════════════════════════════════════════════════════════════════
# ⑥′ 幂等命中（这条任务早投过）⇒ skipped_duplicate，**不是失败**
# ══════════════════════════════════════════════════════════════════════


def test_already_published_records_skipped_duplicate(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """到点了、作业没建出来，但原因是「早就投过」⇒ 记 ``skipped_duplicate``。

    真机踩到（陷阱 182）：钉着一条已发任务的计划，每一拍都撞幂等，被记成
    ``error:PUBLISH_FAILED`` ⇒ ``fail_streak`` 每天 +1 ⇒ 第 5 天拉一条
    ``SCHEDULE_FAILING`` 告警。而那条告警淹掉的正是真故障。
    """
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config)

    first = scheduler.tick(now=format_iso(utc_now()))
    assert first.fired == (schedule_id,)

    repo = ScheduleRepo(connection)
    repo.set_enabled(schedule_id, enabled=True, next_run_at=format_iso(utc_now() - timedelta(minutes=1)))
    second = scheduler.tick(now=format_iso(utc_now()))

    assert second.skipped_duplicate == (schedule_id,)
    assert second.errors == () and second.job_ids == ()
    assert JobStore(connection).stats(pool="publish").pending == 1

    fresh = repo.require(schedule_id)
    assert fresh.last_result == RESULT_SKIPPED_DUPLICATE
    assert fresh.fail_streak == 0
    assert fresh.run_count == 2


def test_platform_skip_is_still_an_error(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """边界：**平台没启用**与「早就投过」不是一回事 —— 前者仍然是 ``error:…``。

    两者都会让 ``queued=0``，但处置动作相反：一个要人去改配置，一个什么都不用做。
    把后者也算成成功会让"配置写错了"永远静默；把前者算成"跳过"则相反。
    """
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    key = cast("PlatformCode", REHEARSAL)
    platforms = dict(config.platforms)
    platforms[key] = platforms[key].model_copy(update={"enabled": False})
    patched = config.model_copy(update={"platforms": platforms})

    scheduler = service_for(connection, paths, patched)
    outcome = scheduler.fire(ScheduleRepo(connection).require(schedule_id), now=format_iso(utc_now()))

    assert outcome.result.startswith("error:")
    assert "平台未启用" in outcome.result
    fresh = ScheduleRepo(connection).require(schedule_id)
    assert fresh.fail_streak == 1


# ══════════════════════════════════════════════════════════════════════
# ⑦ 连续失败 >=5 ⇒ system.alert
# ══════════════════════════════════════════════════════════════════════


def _alert_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT level, source, message, payload_json FROM system_logs "
            "WHERE payload_json LIKE ? ORDER BY id ASC",
            (f"%{AlertCode.SCHEDULE_FAILING}%",),
        ).fetchall()
    )


def test_consecutive_failures_raise_alert(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig, state: AppState
) -> None:
    """⑦ 连续失败 5 次 ⇒ ``system.alert(SCHEDULE_FAILING)``（前 4 次不告警）。"""
    # 不钉 task_id，且库里一条 completed 都没有 ⇒ 每次触发都失败（"待发布池是空的"）。
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(mode="interval", window=None, interval_hours=1, task_id=None),
        now=format_iso(utc_now()),
    )
    scheduler = service_for(connection, paths, config, log=state.logs.append)
    moment = format_iso(utc_now())
    for attempt in range(1, 5):
        outcome = scheduler.fire(row, now=moment)
        assert outcome.failed
        assert ScheduleRepo(connection).require(row.id).fail_streak == attempt
    assert _alert_rows(connection) == [], "还没到 5 次就告警了"

    scheduler.fire(row, now=moment)
    fresh = ScheduleRepo(connection).require(row.id)
    assert fresh.fail_streak == 5
    alerts = _alert_rows(connection)
    assert len(alerts) == 1
    assert alerts[0]["level"] == "error"
    assert alerts[0]["source"] == "publish.scheduler"
    assert AlertCode.SCHEDULE_FAILING in str(alerts[0]["payload_json"])


def test_success_resets_fail_streak(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig, state: AppState
) -> None:
    """⑦ 反面：中间成功一次就把 ``fail_streak`` 归零（否则"连续"这个词是假的）。"""
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(mode="interval", window=None, interval_hours=1, task_id=None),
        now=format_iso(utc_now()),
    )
    scheduler = service_for(connection, paths, config, log=state.logs.append)
    moment = format_iso(utc_now())
    scheduler.fire(row, now=moment)
    assert ScheduleRepo(connection).require(row.id).fail_streak == 1

    # 池里出现一条 completed 的任务 ⇒ 这一次能建出作业。
    make_completed_task(connection)
    outcome = scheduler.fire(row, now=moment)
    assert outcome.ok
    fresh = ScheduleRepo(connection).require(row.id)
    assert fresh.fail_streak == 0
    assert fresh.last_result == RESULT_OK


# ══════════════════════════════════════════════════════════════════════
# ⑧ 增删改启停均写 audit_ops
# ══════════════════════════════════════════════════════════════════════


def test_crud_writes_audit(connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig) -> None:
    """⑧ 建 / 改 / 停 / 删四个动作各留一条痕，且带 before/after。"""
    task_id = make_completed_task(connection)
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(task_id=task_id),
        now=format_iso(utc_now()),
    )
    update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"jitter_min": 30},
        now=format_iso(utc_now()),
    )
    update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"enabled": False},
        now=format_iso(utc_now()),
    )
    delete_schedule(connection=connection, schedule_id=row.id)

    ops = AuditRepo(connection).list_filtered(target_type="schedule", limit=50)
    actions = [op.action for op in ops]
    assert actions.count(AUDIT_CREATE) == 1
    assert actions.count(AUDIT_UPDATE) == 1
    assert actions.count(AUDIT_DISABLE) == 1
    assert actions.count(AUDIT_DELETE) == 1
    for op in ops:
        assert op.target_id == row.id
    disable = next(op for op in ops if op.action == AUDIT_DISABLE)
    assert disable.before.get("enabled") is True
    assert disable.after.get("enabled") is False


# ══════════════════════════════════════════════════════════════════════
# ⑨ 非法参数被拒且不落库
# ══════════════════════════════════════════════════════════════════════


def _count_schedules(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT count(*) FROM publish_schedules").fetchone()[0])


def test_invalid_params_are_rejected_without_persisting(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """⑨ 窗口倒置 / 抖动越界 / at_time 不带时区 / 平台不存在 ⇒ **400 且不落库**。

    形状错误（缺字段、``platforms`` 空数组）由 Pydantic 拦成 422 —— 那一条也"不落库"，
    但语义错误必须是 400：规格书 §04.6.5.1 明写「非法参数 ⇒ 400」。
    """
    before = _count_schedules(connection)
    cases: list[tuple[dict[str, object], int]] = [
        (spec_for(window=("21:30", "18:00")), 400),
        (spec_for(jitter_min=200), 400),
        (spec_for(mode="at_time", at_time="2026-09-20T19:30:00", window=None), 400),
        (spec_for(platforms=("meipai",)), 400),
        (spec_for(account_ids=("nobody",)), 400),
        (spec_for(mode="interval", window=None, interval_hours=0), 400),
        # 模式名不在字面量里 ⇒ Pydantic 的形状校验先拦下来（422），也不落库。
        (spec_for(mode="weekly"), 422),
        (spec_for(platforms=()), 422),
    ]
    for payload, expected in cases:
        response = client.post(SCHEDULES_URL, json=payload)
        assert response.status_code == expected, (payload, response.text)
        assert response.json()["code"] in {"SCHEDULE_INVALID", "VALIDATION_FAILED"} or expected == 422
    assert _count_schedules(connection) == before


def test_at_time_in_the_past_is_rejected(client: TestClient, connection: sqlite3.Connection) -> None:
    """⑨ ``at_time`` 已经过去 ⇒ 400（建一条永不触发的计划比报错更糟）。"""
    payload = spec_for(
        mode="at_time",
        at_time=format_iso(utc_now() - timedelta(days=1)),
        window=None,
    )
    response = client.post(SCHEDULES_URL, json=payload)
    assert response.status_code == 400
    assert response.json()["code"] == "SCHEDULE_INVALID"
    assert _count_schedules(connection) == 0


# ══════════════════════════════════════════════════════════════════════
# ⑩ 编辑 ⇒ 同一条 UPDATE 里重算 next_run_at
# ══════════════════════════════════════════════════════════════════════


def test_patch_recomputes_next_run_at(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """⑩ 改窗口 ⇒ ``next_run_at`` **立刻**按新窗口算（陷阱 #33）。

    判据是"改完之后那一刻必须落在**新**窗口里"。只改参数不重算的话，
    读到的会是旧窗口里的时刻 —— 而它在面板上看起来完全正常。
    """
    task_id = make_completed_task(connection)
    row = create_schedule(
        connection=connection, config=config, spec=spec_for(task_id=task_id), now=format_iso(utc_now())
    )
    assert row.next_run_at is not None and "18:00" <= local_hm(row.next_run_at) <= "21:30"

    moved = update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"window": ["06:00", "07:00"]},
        now=format_iso(utc_now()),
    )
    assert moved.next_run_at is not None
    assert "06:00" <= local_hm(moved.next_run_at) <= "07:00"

    off = update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"enabled": False},
        now=format_iso(utc_now()),
    )
    assert off.next_run_at is None and off.enabled is False

    on = update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"enabled": True},
        now=format_iso(utc_now()),
    )
    assert on.next_run_at is not None
    assert "06:00" <= local_hm(on.next_run_at) <= "07:00"


def test_patch_switching_mode_clears_the_old_mode_fields(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig
) -> None:
    """⑩ 换模式 ⇒ 旧模式的字段被清掉（留着会让面板显示一个不会生效的时刻）。"""
    row = create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(mode="at_time", at_time=format_iso(utc_now() + timedelta(hours=3)), window=None),
        now=format_iso(utc_now()),
    )
    assert row.at_time is not None
    switched = update_schedule(
        connection=connection,
        config=config,
        schedule_id=row.id,
        patch={"mode": "daily_window", "window": ["18:00", "21:30"]},
        now=format_iso(utc_now()),
    )
    assert switched.mode == "daily_window"
    assert switched.at_time is None
    assert switched.window == ("18:00", "21:30")


# ══════════════════════════════════════════════════════════════════════
# REST 面（五个端点）
# ══════════════════════════════════════════════════════════════════════


def test_rest_create_list_patch_delete(client: TestClient, connection: sqlite3.Connection) -> None:
    """五个端点的正常路径（建 -> 列 -> 改 -> 立刻跑 -> 删）。"""
    task_id = make_completed_task(connection)
    created = client.post(SCHEDULES_URL, json=spec_for(task_id=task_id))
    assert created.status_code == 200, created.text
    body = created.json()
    schedule_id = body["id"]
    assert body["platforms"] == [REHEARSAL]
    assert body["enabled"] is True
    assert body["next_run_at"] is not None

    listed = client.get(SCHEDULES_URL)
    assert listed.status_code == 200
    payload = listed.json()
    assert [item["id"] for item in payload["items"]] == [schedule_id]
    assert payload["counts"] == {"total": 1, "enabled": 1, "disabled": 0, "failing": 0}
    assert REHEARSAL in payload["enabled_platforms"]
    assert payload["tick_sec"] == SCHEDULER_TICK_INTERVAL_SEC

    patched = client.patch(f"{SCHEDULES_URL}/{schedule_id}", json={"jitter_min": 45})
    assert patched.status_code == 200, patched.text
    assert patched.json()["jitter_min"] == 45

    # run_now 走的是**同一条** fire（仍看开关、仍过限频）
    ran = client.post(f"{SCHEDULES_URL}/{schedule_id}/run_now", json={"reason": "手动验证"})
    assert ran.status_code == 200, ran.text
    assert ran.json()["result"] == RESULT_OK
    assert len(ran.json()["job_ids"]) == 1

    removed = client.delete(f"{SCHEDULES_URL}/{schedule_id}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert client.delete(f"{SCHEDULES_URL}/{schedule_id}").json()["deleted"] is False
    assert client.get(SCHEDULES_URL).json()["items"] == []


def test_rest_patch_is_partial(client: TestClient, connection: sqlite3.Connection) -> None:
    """``exclude_unset`` 是这一层的护栏：只给 ``enabled`` 就只动 ``enabled``。"""
    created = client.post(SCHEDULES_URL, json=spec_for(jitter_min=20)).json()
    stopped = client.patch(f"{SCHEDULES_URL}/{created['id']}", json={"enabled": False}).json()
    assert stopped["enabled"] is False
    assert stopped["next_run_at"] is None
    assert stopped["jitter_min"] == 20
    assert stopped["window"] == list(WINDOW)
    assert stopped["platforms"] == [REHEARSAL]


def test_run_now_respects_the_switch(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig, state: AppState
) -> None:
    """``run_now`` 不绕过 R14 的开关：关着的时候它空转，不建作业。"""
    task_id = make_completed_task(connection)
    schedule_id = make_due_schedule(connection, config, task_id=task_id)
    outcome = run_now(
        connection=connection,
        paths=paths,
        config=config.model_copy(update={"enabled": False}),
        schedule_id=schedule_id,
        log=state.logs.append,
    )
    assert outcome.result == RESULT_SKIPPED_DISABLED
    assert JobStore(connection).stats(pool="publish").pending == 0


# ══════════════════════════════════════════════════════════════════════
# WS 事件（§04.6.5.1）
# ══════════════════════════════════════════════════════════════════════


def _event_kinds(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT payload_json FROM system_logs WHERE source = ? ORDER BY id ASC",
        ("publish.scheduler",),
    ).fetchall()
    out: list[str] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        kind = payload.get("event_kind")
        if isinstance(kind, str):
            out.append(kind)
    return out


def test_firing_emits_both_ws_events(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig, state: AppState
) -> None:
    """``publish.schedule_fired``（建了作业）与 ``publish.scheduled``（排下一期）都发。"""
    task_id = make_completed_task(connection)
    make_due_schedule(connection, config, task_id=task_id)
    scheduler = service_for(connection, paths, config, log=state.logs.append)
    scheduler.tick(now=format_iso(utc_now()))
    kinds = _event_kinds(connection)
    assert EventKind.PUBLISH_SCHEDULE_FIRED.value in kinds
    assert EventKind.PUBLISH_SCHEDULED.value in kinds


def test_creating_a_schedule_emits_scheduled_event(
    connection: sqlite3.Connection, paths: StudioPaths, config: PublishConfig, state: AppState
) -> None:
    """建计划也发 ``publish.scheduled``：面板据此立刻把新时刻画上去。"""
    create_schedule(
        connection=connection,
        config=config,
        spec=spec_for(),
        log=state.logs.append,
        now=format_iso(utc_now()),
    )
    assert EventKind.PUBLISH_SCHEDULED.value in _event_kinds(connection)
