"""多账号支持集成测试（T5.8 验收 · §06.2.4 / §06.9）。

五条验收口径，一条一个用例
--------------------------
① 两账号**限频互不影响**：额度按 ``account_id`` 各算各的
② 同一任务分发到两账号 ⇒ **2 条作业 + 2 条 ``publications``**（幂等键含 ``account_id``）
③ 账号 A 登录态失效**不影响**账号 B（同一次 run 里 B 照常发出去）
④ 报告可按账号拆分（``data_json.by_account`` + 正文「账号归因」一节）
⑤ 新增第 2 个账号 ⇒ **零迁移**（只改 ``config/publish.yaml``，``schema_migrations`` 一行不动）

为什么两个账号都挂在演练台（``other``）上
----------------------------------------
真账号（``douyin``）要浏览器 + 真网络，而这几条用例验的是**账号维度**的分发与隔离，
不是发布器。``other`` 是唯一"发得出去但不会真发到网上"的目标（T5.9），两个账号挂在
它上面 ⇒ 整条链路离线可验，且验的仍是真代码路径（队列、限频、幂等、报告）。

为什么第二个账号要**临时加**而不是写进出厂配置
--------------------------------------------
D1 的出厂口径是「结构支持多账号，**默认只启用 1 个**」。测试夹具里那个账号是
"矩阵运营自己加的那一条"，验收 ⑤ 要的正是"加它只需要改这一个文件"。

四条纪律（与 ``test_publish_pool.py`` 同一套）
--------------------------------------------
1. **临时家目录**：真配置抄一份，只动 ``enabled`` / ``min_gap_min``，再加第二个账号。
   拿仓库根当 home 跑的就是生产那份配置。
2. **真库真盘**：作业、发布记录、限频计数一律回库查。「返回 200」只说明没抛。
3. **不碰真平台**：发布器换成假件（真实现要起浏览器）。
4. **日期显式算**：限频按**本地日**统计、报告窗口也按本地日 —— 用例自己算"今天"，
   不让断言去问第二遍系统时钟。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import local_tz, utc_now
from studio.core.config import PoolConfig, PublishConfig, load_pools_config, load_publish_config
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import Job, JobStore
from studio.db.repositories.publication_repo import (
    MANUAL_REQUIRED,
    PUBLISHED,
    PublicationRepo,
)
from studio.domain.enums import TaskStatus
from studio.domain.publish import unit_ref
from studio.domain.task_service import TaskService
from studio.pools import publish_worker
from studio.pools.publish_worker import build_publish_handler
from studio.pools.worker_base import PoolWorker
from studio.publish.base import PublishHealth, PublishRequest, PublishResult
from studio.services.metrics_service import ResourceSnapshot
from studio.services.publish_service import EnqueueReport, enqueue_publications
from studio.services.report_service import build_report, report_markdown
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 演练台（T5.9）：唯一一个「发得出去但不会真发到网上」的目标。
REHEARSAL = "other"
REHEARSAL_ACCOUNT = "_rehearsal"
#: 第二个账号 —— 验收 ⑤ 的"新增第 2 个账号"就是**这一条配置**。
SECOND_ACCOUNT = "_rehearsal_b"

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

#: 假发布器记下每一次调用（「两个账号各发了一次」这类断言靠它）
CALLS: list[PublishRequest] = []


class FakePublisher:
    """可编排的假发布器（与 ``test_publish_pool.py`` 的假件同一形态）。

    ``expired_accounts`` **按账号**编排：验收 ③ 要的是"账号 A 失效不影响 B"，
    而"哪个账号失效"只有账号自己知道 —— 一个全局开关表达不了它。
    """

    platform = "douyin"
    selectors_version = "test-2026-09-19"

    expired_accounts: ClassVar[set[str]] = set()

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def health(self) -> PublishHealth:
        account_id = str(self.ctx.account.account_id)
        if account_id in type(self).expired_accounts:
            return PublishHealth(
                ready=False,
                logged_in=False,
                last_check_at="2026-09-19T06:00:00.000Z",
                hint=f"账号 {account_id} 的登录态已失效",
            )
        return PublishHealth(ready=True, logged_in=True, last_check_at="2026-09-19T06:00:00.000Z")

    async def publish(self, req: PublishRequest) -> PublishResult:
        CALLS.append(req)
        return PublishResult.published(
            url=f"https://example.com/{req.account_id}/1",
            platform_post_id=f"post-{req.account_id}",
        )

    async def fetch_metrics(self, platform_post_id: str) -> Any:  # pragma: no cover - T5.4
        raise NotImplementedError


class _Probe:
    """假资源探针（不碰真 ``nvidia-smi`` 与真磁盘）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-19T06:00:00.000Z",
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


class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库（连接由 ``AppState`` 持有）。"""

    def __init__(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        self.paths = paths
        self.connection = connection


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


def add_second_account(publish_yaml: Path, *, account_id: str = SECOND_ACCOUNT) -> None:
    """往 ``accounts`` 里加第二个账号 —— **只改配置文件**（验收 ⑤ 的"零迁移"）。

    走一次 yaml 往返而不是手写字符串插入：后者要跟着缩进与书写顺序走，配置一改
    测试就红，而"加一个账号"这件事本身与缩进无关（这份是 tmp 里的副本，注释丢了
    无所谓）。
    """
    data: dict[str, Any] = yaml.safe_load(publish_yaml.read_text(encoding="utf-8"))
    data["accounts"].append(
        {
            "account_id": account_id,
            "platform": REHEARSAL,
            "display_name": "演练台二号",
            "profile_dir": f"data/browser_profile/{account_id}",
            "enabled": True,
            "daily_limit": 100,
            "min_gap_min": 0,
        }
    )
    publish_yaml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def drop_second_account(publish_yaml: Path, *, account_id: str = SECOND_ACCOUNT) -> None:
    """把第二个账号摘掉（验收 ⑤ 要先回到"出厂只有一个账号"那一刻）。"""
    data: dict[str, Any] = yaml.safe_load(publish_yaml.read_text(encoding="utf-8"))
    data["accounts"] = [item for item in data["accounts"] if item["account_id"] != account_id]
    publish_yaml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真配置抄一份，动 ``enabled`` / ``min_gap_min``，再加第二个账号。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        shutil.copyfile(source, value.config_dir / source.name)

    publish = value.config_dir / "publish.yaml"
    text = publish.read_text(encoding="utf-8")
    # 只换**顶格**那一行与那两个参数：``handoff`` 与二线平台的 ``enabled`` 都是缩进的。
    text = text.replace("\nenabled: false", "\nenabled: true")
    text = text.replace("min_gap_min: 30", "min_gap_min: 0")
    publish.write_text(text, encoding="utf-8")
    add_second_account(publish)
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
def rig(paths: StudioPaths, connection: sqlite3.Connection) -> Rig:
    return Rig(paths=paths, connection=connection)


@pytest.fixture
def config(paths: StudioPaths) -> PublishConfig:
    return load_publish_config(paths)


@pytest.fixture(autouse=True)
def _fake_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """把平台查表换成假件（真实现要起浏览器）。"""
    monkeypatch.setattr(publish_worker, "get_publisher", lambda code: FakePublisher)


@pytest.fixture(autouse=True)
def _reset_fake() -> Iterator[None]:
    """每个用例从干净的假发布器开始（类级编排是**故意**的，但要清）。"""
    CALLS.clear()
    FakePublisher.expired_accounts = set()
    yield
    CALLS.clear()
    FakePublisher.expired_accounts = set()


@pytest.fixture
def fast_pool(rig: Rig) -> PoolConfig:
    """``pools.yaml`` 的 publish 池，把「等」的那几个参数压到毫秒级。

    ``min_gap_min`` 归零是必须的：``_min_gap`` 以池级为准，出厂 30 分钟会让
    **第二个账号**的第一条就被"距上次发布不足最小间隔"挡下 —— 而这一挡看起来
    和"限频生效"一模一样，验收 ① 就永远验不到它想验的那件事。
    """
    rig.connection.execute(
        "UPDATE pool_settings SET backoff_base_ms = ?, backoff_max_ms = ? WHERE pool = ?",
        (50, 100, "publish"),
    )
    rig.connection.commit()
    params: dict[str, Any] = load_pools_config(rig.paths).pools["publish"].model_dump()
    params.update({"poll_ms": 50, "backoff_base_ms": 50, "backoff_max_ms": 100, "min_gap_min": 0})
    return PoolConfig(**params)


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════


@contextmanager
def _worker(paths: StudioPaths, *, pool_config: PoolConfig) -> Iterator[PoolWorker]:
    """一个**真的** publish 池 worker（连接用完就关，Windows 上临时目录才删得掉）。"""
    connection = connect(paths.db_file)
    try:
        handler = build_publish_handler(paths=paths, connection=connection, pool=pool_config, log=None)
        yield PoolWorker(
            pool="publish",
            handler=handler,
            pool_config=pool_config,
            paths=paths,
            slot=1,
            version="test",
        )
    finally:
        connection.close()


def _seed_task(connection: sqlite3.Connection, paths: StudioPaths, *, title: str) -> str:
    """建一条「已出片」的任务：``completed`` + 盘上真有一支成片。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    for target in TO_COMPLETED:
        tasks.transition(task_id, target, actor="test", reason="setup")
    video = paths.videos_dir / f"20260919-120000_{task_id}_final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    connection.commit()
    return task_id


def _seed_published(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    *,
    account_id: str,
    count: int,
    views: int,
) -> None:
    """直接落 ``count`` 条"今天已经发出去"的记录。

    限频守卫数的就是 ``publications`` 里**今天**的 ``published``/``uploading`` 行，
    所以这是造"某个账号额度用满"最贴近真机的办法 —— 走一遍 worker 也能造出来，
    但那要用例先跑三轮才能开始验它想验的那件事。

    ``published_at`` 由**数据库**落（与真发布同一条路径），不在这里传时间。
    """
    repo = PublicationRepo(connection)
    video = paths.videos_dir / "seed.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    for index in range(count):
        task_id = _seed_task(connection, paths, title=f"已发 {account_id} #{index}")
        row, _created = repo.create(
            task_id=task_id,
            platform=REHEARSAL,
            account_id=account_id,
            video_path=video.as_posix(),
            video_sha256="0" * 64,
            title="已发的一条",
            profile_key="douyin_1080x1920_30fps_v1",
        )
        repo.mark_published(row.id, url="https://example.com/seed", platform_post_id=f"seed-{index}")
        repo.record_metrics(
            row.id,
            {"views": views, "likes": views // 20, "comments": 10, "shares": 5},
            next_metric_at=None,
        )
    connection.commit()


def _jobs(connection: sqlite3.Connection, task_id: str) -> tuple[Job, ...]:
    return JobStore(connection).list_jobs(pool="publish", task_id=task_id, limit=10)


def _refs(connection: sqlite3.Connection, task_id: str) -> set[str]:
    return {job.unit_ref for job in _jobs(connection, task_id)}


def _enqueue(
    connection: sqlite3.Connection,
    config: PublishConfig,
    task_id: str,
    *,
    account_ids: tuple[str, ...] | None = None,
) -> EnqueueReport:
    """投到演练台（显式点名，不走默认目标 —— 演练台不在默认目标里，T5.9）。"""
    return enqueue_publications(
        connection=connection,
        task_id=task_id,
        config=config,
        platforms=(REHEARSAL,),
        account_ids=account_ids,
    )


# ══════════════════════════════════════════════════════════════════════
# ② 同一任务分发到两账号 ⇒ 2 条作业 + 2 条 publications
# ══════════════════════════════════════════════════════════════════════


def test_one_task_fans_out_to_both_accounts(rig: Rig, config: PublishConfig, fast_pool: PoolConfig) -> None:
    """②一个任务 × 两个账号 ⇒ 两条作业、两条发布记录，**互不顶掉**。

    这条是 T5.8 的核心：``jobs`` 的唯一键是 ``(task_id, pool, unit_type, unit_ref)``，
    单元标识只写平台代号时"第二个账号"根本投不出来（第一条作业把它顶掉了）。
    """
    task_id = _seed_task(rig.connection, rig.paths, title="矩阵分发")

    report = _enqueue(rig.connection, config, task_id)
    assert report.queued == 2, report.skipped
    assert _refs(rig.connection, task_id) == {
        unit_ref(REHEARSAL, REHEARSAL_ACCOUNT),
        unit_ref(REHEARSAL, SECOND_ACCOUNT),
    }

    # 再投一次：两条都幂等命中，**不重复建作业**（面板上"投过了"要说得出是哪个账号）
    again = _enqueue(rig.connection, config, task_id)
    assert again.queued == 0
    assert set(again.duplicates) == {
        f"{REHEARSAL}/{REHEARSAL_ACCOUNT}",
        f"{REHEARSAL}/{SECOND_ACCOUNT}",
    }
    assert len(_jobs(rig.connection, task_id)) == 2

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=2, max_empty_rounds=5)
    assert (run.units_done, run.units_failed) == (2, 0)

    rows = PublicationRepo(rig.connection).list_for_task(task_id)
    assert len(rows) == 2, "两个账号是两件不同的事，各留一条记录"
    assert {row.account_id for row in rows} == {REHEARSAL_ACCOUNT, SECOND_ACCOUNT}
    assert {row.status for row in rows} == {PUBLISHED}
    assert len({row.idempotency_key for row in rows}) == 2, "幂等键里含 account_id"
    assert {req.account_id for req in CALLS} == {REHEARSAL_ACCOUNT, SECOND_ACCOUNT}


# ══════════════════════════════════════════════════════════════════════
# ① 两账号限频互不影响
# ══════════════════════════════════════════════════════════════════════


def test_rate_limit_is_counted_per_account(rig: Rig, config: PublishConfig, fast_pool: PoolConfig) -> None:
    """①账号 A 额度用满 ⇒ 只有 A 被顺延，B 照发。

    限频的计数在 ``JobStore.rate_limit_state``，按 ``publications.account_id`` 数。
    "按平台数"会让矩阵运营的两个号共用一个额度 —— 那正是账号风控关联要做掉的事
    （§06.2.4 的 ⚠️：独立限频 + 独立 profile + 错峰发布）。
    """
    store = JobStore(rig.connection)
    _seed_published(rig.connection, rig.paths, account_id=REHEARSAL_ACCOUNT, count=3, views=1_000)

    # 判据先单独看一眼（不等 worker 跑完）：这就是"各算各的"最直白的形态
    blocked = store.rate_limit_state(account_id=REHEARSAL_ACCOUNT, daily_limit=3, min_gap_min=0)
    free = store.rate_limit_state(account_id=SECOND_ACCOUNT, daily_limit=3, min_gap_min=0)
    assert (blocked.allowed, blocked.used_today) == (False, 3)
    assert (free.allowed, free.used_today) == (True, 0)

    full = _seed_task(rig.connection, rig.paths, title="额度已满的那个号")
    fresh = _seed_task(rig.connection, rig.paths, title="另一个号")
    assert _enqueue(rig.connection, config, full, account_ids=(REHEARSAL_ACCOUNT,)).queued == 1
    assert _enqueue(rig.connection, config, fresh, account_ids=(SECOND_ACCOUNT,)).queued == 1

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=2, max_empty_rounds=5)
    assert (run.units_deferred, run.units_failed) == (1, 0), "顺延不是失败"

    # A 那条回 pending 且**不消耗尝试**，也没留下发布记录
    (deferred,) = _jobs(rig.connection, full)
    assert deferred.status == "pending" and deferred.attempts == 0
    assert deferred.not_before is not None
    assert PublicationRepo(rig.connection).list_for_task(full) == ()

    # B 那条照发不误
    (row,) = PublicationRepo(rig.connection).list_for_task(fresh)
    assert row.status == PUBLISHED and row.account_id == SECOND_ACCOUNT
    assert {req.account_id for req in CALLS} == {SECOND_ACCOUNT}


# ══════════════════════════════════════════════════════════════════════
# ③ 账号 A 登录态失效不影响账号 B
# ══════════════════════════════════════════════════════════════════════


def test_one_expired_login_does_not_stop_the_other_account(
    rig: Rig, config: PublishConfig, fast_pool: PoolConfig
) -> None:
    """③A 的登录态失效 ⇒ A 转人工，**同一次 run 里 B 照常发出去**。

    两个账号的登录态目录是物理隔离的（``profile_dir`` 在配置里被校验唯一），
    所以"A 掉了"这件事对 B 没有任何传导路径 —— 这条用例钉的就是"没有传导"。
    """
    FakePublisher.expired_accounts = {REHEARSAL_ACCOUNT}
    task_id = _seed_task(rig.connection, rig.paths, title="一个号掉线")

    assert _enqueue(rig.connection, config, task_id).queued == 2
    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=2, max_empty_rounds=5)
    assert (run.units_done, run.units_failed) == (2, 0), "登录态失效转人工，不是失败"

    by_account = {row.account_id: row for row in PublicationRepo(rig.connection).list_for_task(task_id)}
    assert by_account[REHEARSAL_ACCOUNT].status == MANUAL_REQUIRED
    assert by_account[SECOND_ACCOUNT].status == PUBLISHED
    # 待人工队列里只有 A 那一条 —— B 没有被连带拖进去
    assert {row.account_id for row in PublicationRepo(rig.connection).manual_queue()} == {REHEARSAL_ACCOUNT}


# ══════════════════════════════════════════════════════════════════════
# ④ 报告可按账号拆分
# ══════════════════════════════════════════════════════════════════════


def test_report_splits_by_account(rig: Rig) -> None:
    """④``data_json.by_account`` 与正文「账号归因」都要有，且两组各算各的。

    ``by_account`` **不进建议列表**：建议都指向一个能改的东西，而"换个号发"不是。
    """
    _seed_published(rig.connection, rig.paths, account_id=REHEARSAL_ACCOUNT, count=2, views=1_000)
    _seed_published(rig.connection, rig.paths, account_id=SECOND_ACCOUNT, count=2, views=400)

    today = utc_now().astimezone(local_tz()).date().isoformat()
    bundle = build_report(rig.connection, period="daily", start_date=today, end_date=today)
    groups = {item["key"]: item for item in bundle.data["by_account"]}
    assert set(groups) == {REHEARSAL_ACCOUNT, SECOND_ACCOUNT}
    assert groups[REHEARSAL_ACCOUNT]["views_median"] == 1000.0
    assert groups[SECOND_ACCOUNT]["views_median"] == 400.0
    assert groups[REHEARSAL_ACCOUNT]["n"] == 2

    markdown = report_markdown(bundle, generated_at="2026-09-19T09:00:00.000Z", trigger="manual")
    assert "账号归因" in markdown
    assert REHEARSAL_ACCOUNT in markdown and SECOND_ACCOUNT in markdown
    # 账号维度**不出建议**（出了就是一条操作员执行不了的动作项）
    assert all(insight.evidence.get("dimension") != "by_account" for insight in bundle.insights)


# ══════════════════════════════════════════════════════════════════════
# ⑤ 新增第 2 个账号 ⇒ 零迁移
# ══════════════════════════════════════════════════════════════════════


def test_adding_a_second_account_is_a_config_edit_only(
    rig: Rig, config: PublishConfig, fast_pool: PoolConfig
) -> None:
    """⑤加账号只改 ``config/publish.yaml``：``schema_migrations`` 一行不动、不重跑迁移。

    先回到"出厂只有一个账号"那一刻投一次（1 条），再把账号写回配置投一次（2 条）——
    两次用的是**同一个库、同一条连接**，中间没有任何 DDL。
    """
    publish = rig.paths.config_dir / "publish.yaml"
    drop_second_account(publish)
    single = load_publish_config(rig.paths)
    enabled_single = {account.account_id for account in single.enabled_accounts}
    assert REHEARSAL_ACCOUNT in enabled_single
    assert SECOND_ACCOUNT not in enabled_single, "先回到出厂那一刻：第二个账号还没配上"

    before = rig.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]

    first = _seed_task(rig.connection, rig.paths, title="加账号之前")
    assert _enqueue(rig.connection, single, first).queued == 1
    assert _refs(rig.connection, first) == {unit_ref(REHEARSAL, REHEARSAL_ACCOUNT)}

    add_second_account(publish)
    both = load_publish_config(rig.paths)
    second = _seed_task(rig.connection, rig.paths, title="加账号之后")
    assert _enqueue(rig.connection, both, second).queued == 2

    after = rig.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert after == before, "加一个账号不该动库结构（零迁移）"

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=3, max_empty_rounds=5)
    assert (run.units_done, run.units_failed) == (3, 0)
    assert len(PublicationRepo(rig.connection).list_for_task(second)) == 2
    assert len(PublicationRepo(rig.connection).list_for_task(first)) == 1


# ══════════════════════════════════════════════════════════════════════
# ⑥ account_ids 是**全局**名单：按平台取交集
# ══════════════════════════════════════════════════════════════════════


def test_account_ids_intersect_per_platform(rig: Rig, config: PublishConfig) -> None:
    """⑥一份 ``account_ids`` 落到两个平台 ⇒ 各取各的交集，谁都不被整条跳过。

    面板上的账号勾选框是**分平台**画的，而请求体里只有一份名单（T5.10）。严格版
    （名单里出现别的平台的账号就报错）会让"勾了 douyin 的号 + 也要发演练台"变成
    "演练台整条跳过"，而用户什么都没做错。
    """
    task_id = _seed_task(rig.connection, rig.paths, title="跨平台选号")
    report = enqueue_publications(
        connection=rig.connection,
        task_id=task_id,
        config=config,
        platforms=("douyin", REHEARSAL),
        account_ids=("acc_main", SECOND_ACCOUNT),
    )
    assert report.queued == 2, report.skipped
    assert _refs(rig.connection, task_id) == {
        unit_ref("douyin", "acc_main"),
        unit_ref(REHEARSAL, SECOND_ACCOUNT),
    }

    # 点名了一个**哪个平台都没有**的账号 ⇒ 落 skipped，批量指令不整批失败
    other = _seed_task(rig.connection, rig.paths, title="名字写错了")
    wrong = enqueue_publications(
        connection=rig.connection,
        task_id=other,
        config=config,
        platforms=("douyin", REHEARSAL),
        account_ids=("nobody",),
    )
    assert wrong.queued == 0
    assert len(wrong.skipped) == 2
    assert all("nobody" in item for item in wrong.skipped)
    assert _refs(rig.connection, other) == set()
