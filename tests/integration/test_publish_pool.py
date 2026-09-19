"""发布池集成测试（T5.3 验收 · §06.5.4 / §06.10 / §06.12）。

这一条验的是 T5.3 的四条验收口径，一条不落
-------------------------------------------
① **第 4 条当天被限频 ⇒ 自动顺延**（不是失败）：作业回 ``pending``、``attempts`` 不涨、
   盘上不多一条记录。面板上读起来是「顺延到几点」，不是「失败了」；
② **连续失败 3 次 ⇒ 转人工**：``publications.status='manual_required'``，而**任务仍是
   ``completed``**（发布失败不该让成片变得不可用，§06.5.4 不回退）；
③ **幂等键** ``sha256(task_id|platform|account_id)``：同一条内容投两次、跑两次只发一条；
④ ``GET /api/v1/publish/queue`` 能查到待人工那几条（带 ``error_code`` 与取证路径）。

四条纪律（与 ``test_voice_api.py`` 同一套）
-------------------------------------------
1. **临时家目录**：真配置抄一份进 tmp，只动两处 —— ``publish.enabled`` 改 ``true``
   （出厂 ``false`` 是 R14 的不可逆防护，不是「配置写错了」）、``min_gap_min`` 改 ``0``
   （出厂 30 分钟：这几条用例要连着发三条才撞得到**日额度**那一行）。拿仓库根当 home
   跑的就是生产那份配置，改一次档位这里的断言就跟着飘。
2. **假发布器**：真发布器要起浏览器打真平台，那一层由 T5.2 的 ``test_publish_dryrun.py``
   覆盖。这里换掉 ``get_publisher``，验的是「池 + 队列 + 库」这条链路的形状。
3. **真库真盘**：作业状态、记录状态、留痕一律回库查；成片真写到
   ``data/output/videos/``。「REST 返回 200」只说明没抛。
4. **真跑池**：走的是 ``PoolWorker.run()``（认领 → 执行 → 收尾），不是直接调 handler
   —— 顺延、退避、转人工这三件事都发生在**池**那一层，绕开它就等于没验。

为什么「失败 3 次」要压退避参数
-------------------------------
``pools.yaml`` 的 ``backoff_base_ms`` 是 60s：真等三次退避要两分钟。这里把 base/max
压到 1ms，而**退避的算术**（指数 + 抖动）仍走 ``JobStore`` 那一份 —— 验的是「第三次才
转人工」，不是「退避曲线长什么样」（那是 ``tests/unit/db`` 的活）。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import parse_iso
from studio.core.config import PoolConfig, load_pools_config, load_publish_config
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.db.repositories import AuditRepo
from studio.db.repositories.publication_repo import MANUAL_REQUIRED, PUBLISHED
from studio.domain.enums import TaskStatus
from studio.domain.publish import parse_unit_ref
from studio.domain.task_service import TaskService
from studio.pools import publish_worker
from studio.pools.publish_worker import build_publish_handler
from studio.pools.worker_base import PoolWorker
from studio.publish.base import (
    PublishEvidence,
    PublishHealth,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from studio.services.metrics_service import ResourceSnapshot
from studio.services.publish_service import enqueue_publications
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

PUBLICATIONS_URL = "/api/v1/publish/publications"
QUEUE_URL = "/api/v1/publish/queue"
PLATFORMS_URL = "/api/v1/publish/platforms"

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

#: 假发布器记下每一次调用（「发布器只该被叫一次」这类断言靠它）
CALLS: list[PublishRequest] = []


class FakePublisher:
    """可编排的假发布器（与 T5.2 契约测试、T5.3 单测的假件同一形态）。"""

    platform = "douyin"
    selectors_version = "test-2026-09-17"

    #: 类级编排（每个用例自己设；``_reset_fake`` 夹具负责清干净）
    health_result: ClassVar[PublishHealth] = PublishHealth(
        ready=True, logged_in=True, last_check_at="2026-09-17T06:00:00.000Z"
    )
    results: ClassVar[list[PublishResult]] = []

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def health(self) -> PublishHealth:
        return type(self).health_result

    async def publish(self, req: PublishRequest) -> PublishResult:
        CALLS.append(req)
        if type(self).results:
            return type(self).results.pop(0)
        return PublishResult.published(url="https://example.com/v/1", platform_post_id="post-1")

    async def fetch_metrics(self, platform_post_id: str) -> Any:  # pragma: no cover - T5.4
        raise NotImplementedError


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）—— 不碰真 `nvidia-smi` 与真磁盘。"""

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-17T06:00:00.000Z",
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


@dataclass(frozen=True, slots=True)
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库（连接由 ``AppState`` 持有）。"""

    paths: StudioPaths
    connection: sqlite3.Connection


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真配置抄一份，只动 ``enabled`` 与 ``min_gap_min`` 两处（见模块头）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        shutil.copyfile(source, value.config_dir / source.name)

    publish = value.config_dir / "publish.yaml"
    text = publish.read_text(encoding="utf-8")
    # 只换**顶格**那一行：``handoff`` 与二线平台的 ``enabled`` 都是缩进的，不该跟着翻。
    text = text.replace("\nenabled: false", "\nenabled: true")
    text = text.replace("min_gap_min: 30", "min_gap_min: 0")
    publish.write_text(text, encoding="utf-8")
    return value


@pytest.fixture(autouse=True)
def _fake_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """把平台查表换成假件（真实现要起浏览器）。"""
    monkeypatch.setattr(publish_worker, "get_publisher", lambda code: FakePublisher)


@pytest.fixture(autouse=True)
def _reset_fake() -> Iterator[None]:
    """每个用例从干净的假发布器开始（类级编排是**故意**的，但要清）。"""
    CALLS.clear()
    FakePublisher.health_result = PublishHealth(
        ready=True, logged_in=True, last_check_at="2026-09-17T06:00:00.000Z"
    )
    FakePublisher.results = []
    yield
    CALLS.clear()


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


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def fast_pool(rig: Rig) -> PoolConfig:
    """``pools.yaml`` 的 publish 池，把「等」的那几个参数压到毫秒级。

    压**两处**，因为「等多久」有两个真相源：

    * ``pools.yaml`` ⇒ 传进处理器的 ``PoolConfig``。``min_gap_min`` 必须归零 ——
      ``_min_gap`` 以池级为准（``daily_limit_per_account`` 同理），出厂 30 分钟会让
      第 2 条就被「距上次发布不足最小间隔」挡下，日额度那一行根本验不到；
    * ``pool_settings`` 表 ⇒ 队列 ``fail`` 之后算退避用的就是它（``JobStore._runtime``）。
      不压这一处，第二次尝试要等 60 秒 —— 验的是「第三次才转人工」，不是「退避曲线」。
      ``poll_ms`` 的下限是 50ms（``PoolConfig`` 的校验），所以这里到不了「瞬时」。
    """
    rig.connection.execute(
        "UPDATE pool_settings SET backoff_base_ms = ?, backoff_max_ms = ? WHERE pool = ?",
        (50, 100, "publish"),
    )
    rig.connection.commit()

    params: dict[str, Any] = load_pools_config(rig.paths).pools["publish"].model_dump()
    params.update({"poll_ms": 50, "backoff_base_ms": 50, "backoff_max_ms": 100, "min_gap_min": 0})
    return PoolConfig(**params)


def _set_switch(paths: StudioPaths, *, enabled: bool) -> None:
    """把临时配置里的**顶格** ``enabled`` 翻成想要的值（其余一个字节不动）。

    ``paths`` 夹具出厂把开关翻成 ``true``（那几条用例要连着发三条才撞得到日额度）；
    验"开关关着会怎样"的那两条要把它翻回去。读的是**文件**而不是某个内存对象，
    与 ``publish_config_for`` / ``build_publish_handler`` 的真实读法一致
    （两处都是"用的时候现读"）。
    """
    publish = paths.config_dir / "publish.yaml"
    text = publish.read_text(encoding="utf-8")
    wanted = "\nenabled: true" if enabled else "\nenabled: false"
    current = "\nenabled: false" if enabled else "\nenabled: true"
    assert current in text, "顶格那一行不是预期的值 —— 夹具改了？"
    publish.write_text(text.replace(current, wanted), encoding="utf-8")


@contextmanager
def _worker(paths: StudioPaths, *, pool_config: PoolConfig) -> Iterator[PoolWorker]:
    """一个**真的** publish 池 worker（连接用完就关，Windows 上临时目录才删得掉）。

    处理器拿一条自己的连接（模拟独立进程），而 ``PoolWorker`` **不传** connection ——
    它的心跳线程要用自己开的连接，跨线程复用一条会让心跳与主循环互相等（陷阱 #138）。
    """
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
    """建一条「已出片」的任务：``completed`` + 盘上真有一支成片。

    成片按 ``{时间戳}_{task_id}_final.mp4`` 命名 —— ``resolve_final_video`` 的兜底
    就是按这个名字 glob（与真机渲染产物同一形状）。
    """
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    for target in TO_COMPLETED:
        tasks.transition(task_id, target, actor="test", reason="setup")
    video = paths.videos_dir / f"20260917-120000_{task_id}_final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    connection.commit()
    return task_id


def _upload_failed() -> PublishResult:
    """一次「上传断流」—— 可重试的失败（§06.10）。"""
    return PublishResult.failure(
        ErrorCode.PUBLISH_UPLOAD_FAILED,
        "上传断流",
        status=PublishStatus.FAILED,
        evidence=PublishEvidence(stage="upload"),
    )


def _job(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row: sqlite3.Row = connection.execute(
        "SELECT * FROM jobs WHERE task_id = ? AND pool = 'publish'", (task_id,)
    ).fetchone()
    assert row is not None, "这条任务没有 publish 作业"
    return row


def _publication(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    rows: list[sqlite3.Row] = connection.execute(
        "SELECT * FROM publications WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    assert len(rows) == 1, f"这条任务该只有一条发布记录，实际 {len(rows)} 条"
    return rows[0]


# ══════════════════════════════════════════════════════════════════════
# ① 第 4 条当天被限频 ⇒ 自动顺延
# ══════════════════════════════════════════════════════════════════════


def test_the_fourth_publish_of_the_day_is_deferred_not_failed(rig: Rig, fast_pool: PoolConfig) -> None:
    """①当天第 4 条 ⇒ 顺延：作业回 ``pending``、``attempts`` 不涨、记录不落 failed。"""
    task_ids = [_seed_task(rig.connection, rig.paths, title=f"跑酷合集 {index}") for index in range(4)]
    config = load_publish_config(rig.paths)
    for task_id in task_ids:
        report = enqueue_publications(connection=rig.connection, task_id=task_id, config=config)
        assert report.queued == 1

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=4, max_empty_rounds=5)

    assert (run.units_done, run.units_deferred, run.units_failed) == (4, 1, 0), "顺延不是失败"

    published = {
        str(row["task_id"])
        for row in rig.connection.execute("SELECT task_id FROM publications WHERE status = 'published'")
    }
    assert len(published) == 3, "一天三条：第 4 条不该留下一条记录（它还没开始发）"
    (deferred_task,) = set(task_ids) - published

    job = _job(rig.connection, deferred_task)
    assert job["status"] == "pending"
    assert job["attempts"] == 0, "顺延不该消耗一次尝试（否则三天后自己进死信）"
    assert job["error_code"] == ErrorCode.PUBLISH_RATELIMIT.value
    assert job["not_before"] is not None
    assert parse_iso(job["not_before"]) > datetime.now(UTC), "顺延到的是**未来**，不是立刻重试"

    assert TaskService(rig.connection).get(deferred_task).status is TaskStatus.COMPLETED


# ══════════════════════════════════════════════════════════════════════
# ② 连续失败 3 次 ⇒ 转人工（任务不回退）
# ══════════════════════════════════════════════════════════════════════


def test_three_failures_turn_into_manual_required_without_reverting_the_task(
    rig: Rig, fast_pool: PoolConfig
) -> None:
    """②连续失败 3 次 ⇒ ``manual_required``，而任务**仍是** ``completed``（§06.5.4）。"""
    task_id = _seed_task(rig.connection, rig.paths, title="跑酷合集")
    report = enqueue_publications(
        connection=rig.connection, task_id=task_id, config=load_publish_config(rig.paths)
    )
    assert report.queued == 1
    FakePublisher.results = [_upload_failed() for _ in range(3)]

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        run = worker.run(max_units=3, max_empty_rounds=5)

    assert len(CALLS) == 3, "三次尝试都要真的进发布器（不是前两次就被拦下）"
    assert (run.units_done, run.units_failed) == (3, 2), "前两次交回队列，第三次转人工"

    row = _publication(rig.connection, task_id)
    assert row["status"] == MANUAL_REQUIRED
    assert row["attempt_count"] == 3
    assert row["error_code"] == ErrorCode.PUBLISH_UPLOAD_FAILED.value
    assert row["error_message"] == "上传断流"

    assert TaskService(rig.connection).get(task_id).status is TaskStatus.COMPLETED, "发布失败不回退任务"

    audits = [item.action for item in AuditRepo(rig.connection).list_for_task(task_id)]
    assert audits == ["publish.manual_required"], "转人工必须留痕（§06.10 不变量 3）"


# ══════════════════════════════════════════════════════════════════════
# ③ 幂等键防重复发布
# ══════════════════════════════════════════════════════════════════════


def test_the_idempotency_key_stops_a_second_publish(rig: Rig, fast_pool: PoolConfig) -> None:
    """③投两次、跑两次，只发一条；``idempotency_key`` 就是那三个字段的 sha256。"""
    task_id = _seed_task(rig.connection, rig.paths, title="跑酷合集")
    config = load_publish_config(rig.paths)

    first = enqueue_publications(connection=rig.connection, task_id=task_id, config=config)
    second = enqueue_publications(connection=rig.connection, task_id=task_id, config=config)
    assert (first.queued, second.queued) == (1, 0)
    assert second.skipped, "第二次投递要被幂等挡下，而且**说得出**为什么"

    with _worker(rig.paths, pool_config=fast_pool) as worker:
        assert worker.run(max_units=1, max_empty_rounds=5).units_done == 1
    with _worker(rig.paths, pool_config=fast_pool) as worker:
        assert worker.run(max_units=1, max_empty_rounds=1).units_done == 0, "没有第二条作业可认领"

    assert len(CALLS) == 1, "发布器只该被叫一次"
    row = _publication(rig.connection, task_id)
    assert row["status"] == PUBLISHED
    assert row["idempotency_key"] == hashlib.sha256(f"{task_id}|douyin|acc_main".encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════
# ④ 待人工队列可查
# ══════════════════════════════════════════════════════════════════════


def test_the_manual_queue_endpoint_lists_what_needs_a_human(
    rig: Rig, fast_pool: PoolConfig, client: TestClient
) -> None:
    """④``GET /api/v1/publish/queue`` 查得到待人工那几条（§06.10 / §06.12）。"""
    task_id = _seed_task(rig.connection, rig.paths, title="跑酷合集")

    enqueued = client.post(f"/api/v1/publish/tasks/{task_id}/enqueue", json={})
    assert enqueued.status_code == 200, enqueued.text
    assert enqueued.json()["queued"] == 1

    FakePublisher.health_result = PublishHealth.unknown("需人工扫码登录")
    with _worker(rig.paths, pool_config=fast_pool) as worker:
        assert worker.run(max_units=1, max_empty_rounds=1).units_done == 1

    body = client.get(QUEUE_URL).json()
    assert body["counts"]["manual_required"] == 1
    (item,) = body["manual_required"]
    assert item["task_id"] == task_id
    assert item["platform"] == "douyin"
    assert item["account_id"] == "acc_main"
    assert item["error_code"] == ErrorCode.PUBLISH_LOGIN_EXPIRED.value
    assert item["can_retry"] is True
    assert item["can_cancel"] is True
    assert item["can_mark_done"] is True

    board = client.get(PUBLICATIONS_URL).json()
    assert board["counts"]["manual_required"] == 1
    assert board["manual_required"][0]["id"] == item["id"]


# ══════════════════════════════════════════════════════════════════════
# ⑤ 投递面板的选项清单（T5.10）
# ══════════════════════════════════════════════════════════════════════


class TestPlatformOptions:
    """``GET /api/v1/publish/platforms`` —— 面板的选项清单。

    清单**来自配置**（不是面板自己列的），连"点了会怎样"也由服务端算：判据与投递期
    跳过它的那两条（平台未启用 / 这个平台没有启用账号）是同一套。三条最容易做错的：
    **演练台必须在**（没有真账号时的唯一通路）、**演练台不在默认目标里**（T5.9）、
    **点不动的要说清为什么**。
    """

    def test_lists_real_platforms_and_the_rehearsal_stage(self, client: TestClient) -> None:
        body = client.get(PLATFORMS_URL).json()
        codes = [item["code"] for item in body["items"]]
        assert "douyin" in codes
        assert "other" in codes, "演练台不列出来，没有真账号的操作员就只能去命令行"
        # 不选任何平台时后端会投的那几个 —— 面板要把这句话显示出来，
        # 否则"不选"看起来像"都不发"。
        assert body["default_platforms"] == ["douyin"]
        assert "other" not in body["default_platforms"]

    def test_the_rehearsal_option_says_it_is_not_a_real_platform(self, client: TestClient) -> None:
        items = {item["code"]: item for item in client.get(PLATFORMS_URL).json()["items"]}
        rehearsal = items["other"]
        assert rehearsal["rehearsal"] is True
        assert rehearsal["selectable"] is True
        assert rehearsal["accounts"] == ["_rehearsal"]
        assert "靶页" in rehearsal["note"]

    def test_a_disabled_platform_is_listed_but_not_selectable(self, client: TestClient) -> None:
        """二线平台（出厂 ``enabled=false`` · Q9）列出来，但点不动，且说清为什么。"""
        items = {item["code"]: item for item in client.get(PLATFORMS_URL).json()["items"]}
        second = items["xiaohongshu"]
        assert second["selectable"] is False
        assert "未启用" in second["note"]

    def test_the_real_platform_says_dead_letter_while_the_switch_is_off(
        self, rig: Rig, client: TestClient
    ) -> None:
        """**出厂配置**（``enabled=false``）下真平台那一条说"直接死信"。

        这一条是操作员第一眼会撞上的：勾 douyin → 投递 → 作业被守卫挡下、``publications``
        那一行根本不建 ⇒ 发布面板上什么都没发生。面板必须**事先**说清这一档去哪看
        （「四池调度」的死信），否则"什么都没发生"只会被读成"按钮坏了"。
        """
        _set_switch(rig.paths, enabled=False)
        body = client.get(PLATFORMS_URL).json()
        assert body["publish_enabled"] is False
        items = {item["code"]: item for item in body["items"]}
        assert "死信" in items["douyin"]["note"]
        assert "四池调度" in items["douyin"]["note"]
        assert "死信" not in items["other"]["note"], "演练台发的是本地靶页，与开关无关"

    def test_enqueue_to_a_real_platform_while_the_switch_is_off_leaves_no_record(
        self, rig: Rig, fast_pool: PoolConfig, client: TestClient
    ) -> None:
        """投真平台 ⇒ 作业死信、**一条发布记录都不落**（R14 守卫的真实落点）。

        这条用例把"面板上不会出现记录"这句话钉住：面板的提示语就是按它写的。
        守卫抛在 ``create`` **之前**，所以死信而不是待人工 —— 文案说错一个词，
        操作员就会去「待人工」区块里找一个永远不存在的记录。
        """
        _set_switch(rig.paths, enabled=False)
        task_id = _seed_task(rig.connection, rig.paths, title="跑酷合集")
        enqueued = client.post(f"/api/v1/publish/tasks/{task_id}/enqueue", json={"platforms": ["douyin"]})
        assert enqueued.status_code == 200, enqueued.text
        assert enqueued.json()["queued"] == 1

        with _worker(rig.paths, pool_config=fast_pool) as worker:
            result = worker.run(max_units=1, max_empty_rounds=1)
        # `units_done` 是"经手过几条"（含失败那一条），判据要看 `units_failed`
        assert result.units_failed == 1, "守卫抛的是不可重试的 PUBLISH_DISABLED"

        jobs = [
            job
            for job in JobStore(rig.connection).list_jobs(pool="publish", task_id=task_id, limit=10)
            if parse_unit_ref(job.unit_ref)[0] == "douyin"
        ]
        assert len(jobs) == 1
        assert jobs[0].status == "dead"

        # 发布面板与待人工队列**都是空的** —— 面板那句"不会出现记录"就是这一行。
        board = client.get(PUBLICATIONS_URL).json()
        assert board["counts"]["manual_required"] == 0
        assert board["counts"]["queued"] == 0
        assert client.get(QUEUE_URL).json()["counts"]["manual_required"] == 0
