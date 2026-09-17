"""发布池单元处理器（``publish/publish``）的单元测试（T5.3）。

测什么、不测什么
----------------
"真打平台"由 T5.2 的 ``test_publish_dryrun.py``（本地靶页 + 真浏览器）覆盖。
这里用假发布器把**判据**逐条钉住 —— 每一件出错都会**静默走偏**：

① 限频触顶 ⇒ **顺延**（回 ``pending``、不耗 ``attempts``、面板上不是"失败"）；
② ``publish.enabled=false`` ⇒ 真发布一律拒绝（R14 不可逆），但**演练放行**
   （演练的全部意义就是"在开关还关着的时候验证链路"）；
③ 登录态不过 ⇒ 转人工 + 写 ``audit_ops``，而且**这一轮算成功**
   （转人工是"自动这条路走完了"，不是失败）；
④ 可重试的失败 ⇒ 抛给队列（退避重排），到 ``max_attempts`` 才转人工；
⑤ 幂等：已经发过的内容不再发第二遍（一条记录一个账号只有一行）；
⑥ 发布失败**不回退任务状态**（成片仍然有效）。
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from studio.core.clock import parse_iso
from studio.core.config import (
    CONFIG_FILE_NAMES,
    AccountConfig,
    PlatformConfig,
    PoolConfig,
    PublishConfig,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import JobStore
from studio.db.repositories import AuditRepo
from studio.db.repositories.publication_repo import (
    MANUAL_REQUIRED,
    PUBLISHED,
    QUEUED,
    PublicationRepo,
)
from studio.domain import TaskService
from studio.domain.enums import TaskStatus, UnitType
from studio.pools import publish_worker
from studio.pools.heartbeat import WorkerIdentity
from studio.pools.publish_worker import (
    PUBLISH_UNIT_TYPES,
    PublishPlatformHandler,
    build_publish_handler,
)
from studio.pools.runner import HANDLER_MODULES
from studio.pools.worker_base import PoolWorker, UnitContext, UnitDeferred, _Pulse
from studio.publish.base import (
    PublishEvidence,
    PublishHealth,
    PublishRequest,
    PublishResult,
    PublishStatus,
)

WORKER_ID = "publish#1@4242"

#: 假发布器记下每一次调用（"演练也调了发布器"这类断言靠它）
CALLS: list[PublishRequest] = []


class FakePublisher:
    """可编排的假发布器（与 T5.2 契约测试的假件同一形态）。"""

    platform = "douyin"
    selectors_version = "test-2026-09-16"

    #: 类级编排（每个用例自己设；``_reset`` 夹具负责清干净）
    health_result: PublishHealth = PublishHealth(
        ready=True, logged_in=True, last_check_at="2026-09-16T06:00:00.000Z"
    )
    results: ClassVar[list[PublishResult]] = []
    error: StudioError | None = None

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def health(self) -> PublishHealth:
        return type(self).health_result

    async def publish(self, req: PublishRequest) -> PublishResult:
        CALLS.append(req)
        error = type(self).error
        if error is not None:
            raise error
        if type(self).results:
            return type(self).results.pop(0)
        return PublishResult.published(url="https://example.com/v/1", platform_post_id="post-1")

    async def fetch_metrics(self, platform_post_id: str) -> Any:  # pragma: no cover - T5.4
        raise NotImplementedError


DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (("acc_main", "douyin"),)


def _config(
    *, enabled: bool = True, accounts: tuple[tuple[str, str], ...] = DEFAULT_ACCOUNTS
) -> PublishConfig:
    """一份够用的发布配置（默认**打开**开关：单测要验的是链路，不是出厂状态）。"""
    return PublishConfig(
        enabled=enabled,
        accounts=[
            AccountConfig(
                account_id=account_id,
                platform=platform,  # type: ignore[arg-type]
                profile_dir=Path("data/browser_profile") / account_id,
                daily_limit=3,
                min_gap_min=30,
            )
            for account_id, platform in accounts
        ],
        platforms={
            "douyin": PlatformConfig(
                publisher="douyin",
                profile="douyin_1080x1920_30fps_v1",
                enabled=True,
                title_max=55,
                caption_max=1000,
                cover_required=False,
            ),
            "xiaohongshu": PlatformConfig(
                publisher="xiaohongshu",
                profile="xhs_1080x1440_30fps_v1",
                enabled=False,
                title_max=20,
                caption_max=1000,
            ),
        },
    )


def _pool_config(**overrides: object) -> PoolConfig:
    params: dict[str, object] = {
        "unit_type": UnitType.PUBLISH,
        "concurrency": 1,
        "poll_ms": 50,
        "lease_sec": 600,
        "backoff_base_ms": 60_000,
        "backoff_max_ms": 600_000,
        "unit_timeout_sec": 600,
        "max_attempts": 3,
        "priority": 400,
        "daily_limit_per_account": 3,
        "min_gap_min": 30,
    }
    params.update(overrides)
    return PoolConfig(**params)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_fake() -> Iterator[None]:
    """每个用例从干净的假发布器开始（类级编排是**故意**的，但要清）。"""
    CALLS.clear()
    FakePublisher.health_result = PublishHealth(
        ready=True, logged_in=True, last_check_at="2026-09-16T06:00:00.000Z"
    )
    FakePublisher.results = []
    FakePublisher.error = None
    yield
    CALLS.clear()


@pytest.fixture(autouse=True)
def _fake_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """把平台查表换成假件（真实现要起浏览器）。"""
    monkeypatch.setattr(publish_worker, "get_publisher", lambda code: FakePublisher)


@pytest.fixture
def rig(tmp_path: Path, _fake_publisher: None) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    task = TaskService(connection).create(title="跑酷合集")
    video = paths.videos_dir / f"20260916-120000_{task.id}_final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    try:
        yield Rig(
            paths=paths,
            connection=connection,
            store=JobStore(connection),
            repo=PublicationRepo(connection),
            task_id=task.id,
            video=video,
        )
    finally:
        connection.close()


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一条出好片的任务。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    store: JobStore
    repo: PublicationRepo
    task_id: str
    video: Path
    config: PublishConfig = field(default_factory=_config)


def _handler(rig: Rig, *, config: PublishConfig | None = None, **kwargs: Any) -> PublishPlatformHandler:
    return PublishPlatformHandler(
        paths=rig.paths,
        connection=rig.connection,
        publish=config if config is not None else rig.config,
        pool=kwargs.pop("pool", _pool_config()),
        log=None,
        **kwargs,
    )


def _claimed(
    rig: Rig,
    *,
    platform: str = "douyin",
    payload: Mapping[str, Any] | None = None,
    max_attempts: int = 3,
) -> tuple[UnitContext, str]:
    """入队一条 ``publish/publish`` 并认领它（真走一遍 ``enqueue`` + ``claim``）。"""
    job_id = rig.store.enqueue(
        task_id=rig.task_id,
        pool="publish",
        unit_type="publish",
        unit_ref=platform,
        payload=dict(payload or {}),
        max_attempts=max_attempts,
    )
    assert job_id is not None
    job = rig.store.claim(pool="publish", worker_id=WORKER_ID)
    assert job is not None and job.id == job_id
    pulse = _Pulse(
        identity=WorkerIdentity(pool="publish", slot=1, pid=4242),
        connection_factory=lambda: rig.connection,
        version=None,
    )
    return (
        UnitContext(
            job=job,
            pool="publish",
            worker_id=WORKER_ID,
            paths=rig.paths,
            timeout_sec=600,
            pulse=pulse,
            renew=_always_alive,
        ),
        job_id,
    )


def _always_alive() -> bool:
    """租约续期永远成功（单测不验续租，那是 T1.5 的活）。"""
    return True


def _publish_rows(rig: Rig) -> list[sqlite3.Row]:
    return list(rig.connection.execute("SELECT * FROM publications ORDER BY id").fetchall())


# ── ① 顺延 ──────────────────────────────────────────────────────────────


def test_rate_limit_defers_instead_of_failing(rig: Rig) -> None:
    """额度用完 ⇒ ``UnitDeferred``（**不是** StudioError），且没碰发布器。"""
    for index in range(3):
        rig.connection.execute(
            "INSERT INTO publications(id, task_id, platform, account_id, video_path, "
            "video_sha256, title, status, published_at, idempotency_key) "
            "VALUES (?, ?, 'douyin', 'acc_main', 'x.mp4', 'h', 't', 'published', ?, ?)",
            (
                f"pub{index}",
                rig.task_id,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                f"key{index}",
            ),
        )

    ctx, _ = _claimed(rig)

    with pytest.raises(UnitDeferred) as excinfo:
        _handler(rig).run(ctx)

    assert excinfo.value.not_before
    assert excinfo.value.reason == "daily_limit"
    assert CALLS == []
    assert len(_publish_rows(rig)) == 3, "顺延的那一条不该建记录（它还没开始发）"


def test_the_worker_counts_deferral_separately_from_failure(rig: Rig) -> None:
    """池这一层的口径：顺延**不算失败**，作业回 ``pending`` 且 ``attempts`` 退回。"""
    for index in range(3):
        rig.connection.execute(
            "INSERT INTO publications(id, task_id, platform, account_id, video_path, "
            "video_sha256, title, status, published_at, idempotency_key) "
            "VALUES (?, ?, 'douyin', 'acc_main', 'x.mp4', 'h', 't', 'published', ?, ?)",
            (
                f"pub{index}",
                rig.task_id,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                f"key{index}",
            ),
        )
    # **不预认领**：这条用例验的正是"池自己认领 ⇒ 顺延"这一整条。
    # 预认领过的话作业不在 pending，worker 只会空转（`max_empty_rounds` 是那条保险）。
    job_id = rig.store.enqueue(
        task_id=rig.task_id,
        pool="publish",
        unit_type="publish",
        unit_ref="douyin",
        payload={"account_id": "acc_main"},
    )
    assert job_id is not None

    worker = PoolWorker(
        pool="publish",
        handler=_handler(rig),
        pool_config=_pool_config(),
        paths=rig.paths,
        slot=1,
        version="test",
    )
    report = worker.run(max_units=1, max_empty_rounds=1)

    assert report.units_deferred == 1
    assert report.units_failed == 0
    row = rig.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0, "顺延不该消耗一次尝试（否则三天后自己进死信）"
    assert row["not_before"] is not None
    assert row["error_code"] == ErrorCode.PUBLISH_RATELIMIT.value


# ── ② 开关 ──────────────────────────────────────────────────────────────


def test_disabled_switch_blocks_a_real_publish(rig: Rig) -> None:
    """``enabled=false`` ⇒ 真发布拒绝（R14），且**一条记录都不建**。"""
    ctx, _ = _claimed(rig)

    with pytest.raises(StudioError) as excinfo:
        _handler(rig, config=_config(enabled=False)).run(ctx)

    assert excinfo.value.code is ErrorCode.PUBLISH_DISABLED
    assert _publish_rows(rig) == []
    assert CALLS == []


def test_dry_run_passes_while_the_switch_is_off(rig: Rig) -> None:
    """演练**不看**开关（它的意义就是"在开关还关着的时候验证链路"）。"""
    FakePublisher.results = [
        PublishResult.stopped_before_publish(
            evidence=PublishEvidence(stage="before_publish"), elapsed_ms=1200
        )
    ]
    ctx, _ = _claimed(rig, payload={"dry_run": True})

    result = _handler(rig, config=_config(enabled=False)).run(ctx)

    assert result["stage"] == "dry_run"
    assert result["status"] == QUEUED
    assert CALLS and CALLS[0].dry_run is True
    row = _publish_rows(rig)[0]
    assert row["status"] == QUEUED
    assert row["published_at"] is None, "演练不能占额度（写 published 等于伪造一次发布）"
    assert row["dry_run"] == 1


# ── ③ 登录态 ────────────────────────────────────────────────────────────


def test_login_expired_goes_manual_and_counts_as_done(rig: Rig) -> None:
    """登录态不过 ⇒ 转人工 + 留痕，**不抛**（自动这条路走完了，接下来等人）。"""
    FakePublisher.health_result = PublishHealth.unknown("需人工扫码登录")
    ctx = _claimed(rig)[0]

    result = _handler(rig).run(ctx)

    assert result["stage"] == "manual_required"
    row = _publish_rows(rig)[0]
    assert row["status"] == MANUAL_REQUIRED
    assert row["error_code"] == ErrorCode.PUBLISH_LOGIN_EXPIRED.value
    assert CALLS == [], "登录态不过就不该进创作页"
    audits = list(AuditRepo(rig.connection).list_for_task(rig.task_id))
    assert [item.action for item in audits] == ["publish.manual_required"]
    assert audits[0].actor == "worker"


# ── ④ 失败与重试 ────────────────────────────────────────────────────────


def test_retryable_failure_is_raised_for_the_queue(rig: Rig) -> None:
    """可重试的失败 ⇒ 抛给队列（退避重排），记录落 ``failed``。"""
    FakePublisher.results = [
        PublishResult.failure(
            ErrorCode.PUBLISH_UPLOAD_FAILED,
            "上传断流",
            status=PublishStatus.FAILED,
            evidence=PublishEvidence(stage="upload"),
        )
    ]
    ctx, _ = _claimed(rig)

    with pytest.raises(StudioError) as excinfo:
        _handler(rig).run(ctx)

    assert excinfo.value.code is ErrorCode.PUBLISH_UPLOAD_FAILED
    row = _publish_rows(rig)[0]
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1
    assert row["error_message"] == "上传断流"


def test_exhausted_attempts_turn_into_manual_required(rig: Rig) -> None:
    """最后一次机会也没成 ⇒ 转人工（§06.10「重试 ≥3 仍失败」一行）。"""
    FakePublisher.results = [
        PublishResult.failure(ErrorCode.PUBLISH_UPLOAD_FAILED, "还是断流", status=PublishStatus.FAILED)
    ]
    ctx, _ = _claimed(rig, max_attempts=1)

    result = _handler(rig).run(ctx)

    assert result["stage"] == "manual_required"
    row = _publish_rows(rig)[0]
    assert row["status"] == MANUAL_REQUIRED
    assert "重试 1 次仍失败" in result["note"]


def test_selector_miss_goes_straight_to_manual(rig: Rig) -> None:
    """选择器失效**重试多少次都是同一个答案** ⇒ 立刻转人工（不耗完三次）。"""
    FakePublisher.results = [PublishResult.failure(ErrorCode.PUBLISH_SELECTOR_MISS, "找不到发布按钮")]
    ctx, _ = _claimed(rig)

    result = _handler(rig).run(ctx)

    assert result["stage"] == "manual_required"
    assert _publish_rows(rig)[0]["error_code"] == ErrorCode.PUBLISH_SELECTOR_MISS.value
    audits = AuditRepo(rig.connection).list_for_task(rig.task_id)
    assert audits[0].reason is not None and "找不到发布按钮" in audits[0].reason


def test_publication_failure_does_not_touch_the_task(rig: Rig) -> None:
    """§06.5.4 的关键约定：发布失败**不回退任务状态**（成片仍然有效）。"""
    FakePublisher.results = [PublishResult.failure(ErrorCode.PUBLISH_SELECTOR_MISS, "找不到发布按钮")]
    before = TaskService(rig.connection).get(rig.task_id)
    ctx, _ = _claimed(rig)

    _handler(rig).run(ctx)

    after = TaskService(rig.connection).get(rig.task_id)
    assert after.status == before.status
    assert after.status is not TaskStatus.FAILED
    assert after.version == before.version


# ── ⑤ 幂等 ──────────────────────────────────────────────────────────────


def test_already_published_is_not_published_again(rig: Rig) -> None:
    """幂等键命中且已 ``published`` ⇒ 收工，发布器一次都不调。"""
    ctx, _ = _claimed(rig)
    rig.repo.create(
        task_id=rig.task_id,
        platform="douyin",
        account_id="acc_main",
        video_path=rig.video.as_posix(),
        video_sha256="a" * 64,
        title="标题",
    )
    existing = rig.repo.find(task_id=rig.task_id, platform="douyin", account_id="acc_main")
    assert existing is not None
    rig.repo.mark_published(existing.id, url="https://example.com/v/9", platform_post_id="post-9")

    result = _handler(rig).run(ctx)

    assert result["stage"] == "settled"
    assert result["status"] == PUBLISHED
    assert CALLS == []
    assert len(_publish_rows(rig)) == 1


def test_canceled_is_not_published_either(rig: Rig) -> None:
    """人工取消过的那条不再发（重投一次也不会把它复活）。"""
    ctx, _ = _claimed(rig)
    rig.repo.create(
        task_id=rig.task_id,
        platform="douyin",
        account_id="acc_main",
        video_path=rig.video.as_posix(),
        video_sha256="a" * 64,
        title="标题",
    )
    existing = rig.repo.find(task_id=rig.task_id, platform="douyin", account_id="acc_main")
    assert existing is not None
    rig.repo.cancel(existing.id, reason="不发这个平台了")

    result = _handler(rig).run(ctx)

    assert result["stage"] == "settled"
    assert CALLS == []


# ── ⑥ 守卫 ──────────────────────────────────────────────────────────────


def test_disabled_platform_is_a_non_retryable_error(rig: Rig) -> None:
    """二线平台 ``enabled=false`` ⇒ ``PUBLISH_NOT_IMPLEMENTED``（重试多少次都一样）。"""
    ctx, _ = _claimed(rig, platform="xiaohongshu")

    with pytest.raises(StudioError) as excinfo:
        _handler(rig).run(ctx)

    assert excinfo.value.code is ErrorCode.PUBLISH_NOT_IMPLEMENTED


def test_two_accounts_on_one_platform_is_rejected(rig: Rig) -> None:
    """一个平台两个启用账号 ⇒ 报错（发布单元是"一任务一平台一次"，挑第一个会安静地少发一半）。"""
    config = _config(accounts=(("acc_main", "douyin"), ("acc_backup", "douyin")))
    ctx, _ = _claimed(rig)

    with pytest.raises(StudioError) as excinfo:
        _handler(rig, config=config).run(ctx)

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert CALLS == []


def test_payload_account_wins(rig: Rig) -> None:
    """payload 指定账号 ⇒ 用它（多账号分发 T5.8 的接口面已经在这儿了）。"""
    config = _config(accounts=(("acc_main", "douyin"), ("acc_backup", "douyin")))
    ctx, _ = _claimed(rig, payload={"account_id": "acc_backup"})

    result = _handler(rig, config=config).run(ctx)

    assert result["account_id"] == "acc_backup"
    assert _publish_rows(rig)[0]["account_id"] == "acc_backup"


def test_missing_video_is_a_clear_error(rig: Rig) -> None:
    """盘上找不到成片 ⇒ ``RENDER_FAILED`` + 能照做的 remediation（不是 AttributeError）。"""
    rig.video.unlink()
    ctx, _ = _claimed(rig)

    with pytest.raises(StudioError) as excinfo:
        _handler(rig).run(ctx)

    assert excinfo.value.code is ErrorCode.RENDER_FAILED
    assert excinfo.value.remediation is not None


def test_wrong_unit_type_is_rejected(rig: Rig) -> None:
    """认领错池的单元 ⇒ 明确报错（静默跑偏最贵）。"""
    ctx = _claimed(rig)[0]
    handler = _handler(rig)
    assert handler.unit_types == PUBLISH_UNIT_TYPES

    other = UnitContext(
        job=replace(ctx.job, unit_type=UnitType.SENTENCE.value),
        pool="publish",
        worker_id=WORKER_ID,
        paths=rig.paths,
        timeout_sec=600,
        pulse=ctx._pulse,
        renew=_always_alive,
    )

    with pytest.raises(StudioError) as excinfo:
        handler.run(other)

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


# ── ⑦ 装配 ──────────────────────────────────────────────────────────────


def test_handler_is_registered_under_the_declared_module() -> None:
    """``HANDLER_MODULES`` 里声明的模块必须真的能 import（T1.12 的先问再拉）。"""
    assert HANDLER_MODULES["publish"] == "studio.pools.publish_worker"
    assert importlib.util.find_spec(HANDLER_MODULES["publish"]) is not None


def test_build_handler_reads_the_config_once(rig: Rig) -> None:
    """装配期读配置（缺平台表 / 缺账号 ⇒ 起不来，而不是发到一半才炸）。"""
    repo_root = Path(__file__).resolve().parents[3]
    rig.paths.config_dir.mkdir(parents=True, exist_ok=True)
    # 跟着 CONFIG_FILE_NAMES 走，不抄一份手写清单：清单漏一份的症状是
    # `load_config` 报 CONFIG_MISSING（看着像"夹具坏了"，其实是新增了第 9 份配置）。
    for stem in CONFIG_FILE_NAMES:
        name = f"{stem}.yaml"
        source = repo_root / "config" / name
        if source.is_file():
            shutil.copyfile(source, rig.paths.config_dir / name)

    handler = build_publish_handler(paths=rig.paths, log=None)

    assert handler.unit_types == PUBLISH_UNIT_TYPES


def test_next_metric_at_uses_the_first_schedule_point(rig: Rig) -> None:
    """``next_metric_at`` = T+1h（``metrics_schedule_hours`` 的第一项）。"""
    handler = _handler(rig)
    stamp = handler._next_metric_at()

    assert stamp is not None
    delta = parse_iso(stamp) - datetime.now(UTC)
    assert timedelta(minutes=55) < delta < timedelta(hours=1, minutes=5)
