"""数据回收（T5.4 · §06.6）：读数解析 / 时点推进 / 落库 / 失败顺延。

用**真库**（临时目录 + 迁移）：要验的正是 ``metrics_json`` 与
``metrics_history_json`` 两列的关系、``next_metric_at`` 的推进、以及
``metric_attempts`` 这个计数器 —— 三条都只在真库上成立。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from studio.core.clock import format_iso, now_iso, parse_iso, utc_now
from studio.core.config import PublishConfig, load_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.publication_repo import PUBLISHED, PublicationRepo, PublicationRow
from studio.domain import TaskService
from studio.publish.base import Publisher, PublishMetrics
from studio.publish.metrics import (
    METRICS_MAX_ATTEMPTS,
    collect_metrics,
    defer_after_failure,
    metrics_payload,
    next_metric_at,
    parse_metric_count,
    parse_metric_ratio,
)
from studio.services.publish_metrics_service import PublishMetricsService

SCHEDULE = [1, 6, 24, 72]


# ── 夹具 ──────────────────────────────────────────────────────────────


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    home = tmp_path / "studio"
    result = StudioPaths(home=home, data_dir=home / "data")
    result.ensure_runtime_dirs()
    return result


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


class StubPublisher:
    """只实现 ``fetch_metrics`` 的假发布器（这个用例不关心其余两个方法）。"""

    def __init__(self, *, metrics: PublishMetrics | None = None, boom: Exception | None = None) -> None:
        self._metrics = metrics
        self._boom = boom
        self.calls: list[str] = []

    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        self.calls.append(platform_post_id)
        if self._boom is not None:
            raise self._boom
        assert self._metrics is not None
        return self._metrics


def _task(connection: sqlite3.Connection, title: str = "跑酷合集") -> str:
    """``publications.task_id`` 有外键 ⇒ 先造一条真任务（与 T5.3 的仓储用例同一手法）。"""
    return TaskService(connection).create(title=title).id


def _published(connection: sqlite3.Connection, *, due_in_hours: float = 1.0) -> PublicationRow:
    """一条已发布的记录；``due_in_hours <= 0`` ⇒ **已经到点**（轮询那几条用例要它）。"""
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=_task(connection),
        platform="douyin",
        account_id="acc_main",
        video_path="data/output/x.mp4",
        video_sha256="a" * 64,
        title="跑酷",
    )
    return repo.mark_published(
        row.id,
        url="https://example.invalid/v/1",
        platform_post_id="741",
        next_metric_at=format_iso(utc_now() + timedelta(hours=due_in_hours)),
    )


def _hours_after(row: PublicationRow, hours: float) -> str:
    """相对**发布时刻**的某个瞬间（时点都是相对它算的）。"""
    assert row.published_at is not None
    return format_iso(parse_iso(row.published_at) + timedelta(hours=hours))


def _config() -> PublishConfig:
    return load_config(StudioPaths.from_env()).bundle.publish


# ── 读数解析 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2万", 12000),
        ("1,234", 1234),
        ("12.5k", 12500),
        ("3.4亿", 340000000),
        ("0", 0),
        ("340", 340),
        ("—", None),
        ("暂无", None),
        ("", None),
        (None, None),
        ("播放 1.2万", 12000),
    ],
)
def test_parse_metric_count(text: str | None, expected: int | None) -> None:
    assert parse_metric_count(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42.3%", 0.423),
        ("42.3", 0.423),
        ("0.423", 0.423),
        ("100%", 1.0),
        ("1", 1.0),
        ("0%", 0.0),
        ("0", 0.0),
        ("68%", 0.68),
        ("—", None),
        ("暂无", None),
        ("", None),
        (None, None),
        ("完播率 42.3%", 0.423),
        # 101% / 负数：要么平台算错了，要么我们抓到了别的数字 —— 都不该进库。
        ("101%", None),
        ("-3%", None),
    ],
)
def test_parse_metric_ratio(text: str | None, expected: float | None) -> None:
    assert parse_metric_ratio(text) == expected


def test_parse_metric_ratio_never_confuses_ratio_and_percent() -> None:
    """``42.3`` 与 ``0.423`` 必须落到**同一个**值上。

    这一条是完播率唯一真正危险的错法：42 与 0.42 都是"看着正常"的数，
    混起来不会报错、不会崩，只会在选题那边悄悄把"完播率低"当成"完播率高"。
    """
    assert parse_metric_ratio("42.3") == parse_metric_ratio("42.3%") == parse_metric_ratio("0.423")


def test_parse_metric_count_never_fakes_a_zero() -> None:
    """读不出来 ⇒ ``None``。**0 与"不知道"在选题决策里含义相反**（§06.6 平台限制）。"""
    assert parse_metric_count("—") is None
    assert parse_metric_count("0") == 0


# ── 时点推进 ──────────────────────────────────────────────────────────


def test_next_metric_at_picks_the_first_point() -> None:
    published = "2026-09-18T00:00:00.000Z"
    assert next_metric_at(published, SCHEDULE, now="2026-09-18T00:30:00.000Z") == ("2026-09-18T01:00:00.000Z")


def test_next_metric_at_advances_past_the_collected_point() -> None:
    """T+1h 采过之后 ⇒ 下一个是 T+6h（**相对发布时刻**，不是"上次 + 1h"）。"""
    published = "2026-09-18T00:00:00.000Z"
    assert next_metric_at(published, SCHEDULE, now="2026-09-18T01:00:00.000Z") == ("2026-09-18T06:00:00.000Z")


def test_next_metric_at_is_none_after_the_last_point() -> None:
    """四个时点都过了 ⇒ ``None`` ⇒ 停止采集这一条（§06.6）。"""
    published = "2026-09-18T00:00:00.000Z"
    assert next_metric_at(published, SCHEDULE, now="2026-09-22T00:00:00.000Z") is None


def test_next_metric_at_without_schedule_is_none() -> None:
    assert next_metric_at("2026-09-18T00:00:00.000Z", [], now="2026-09-18T00:30:00.000Z") is None


# ── 失败顺延 ──────────────────────────────────────────────────────────


def test_defer_after_failure_moves_one_hour(connection: sqlite3.Connection) -> None:
    row = _published(connection)
    assert defer_after_failure(row, now="2026-09-18T01:00:00.000Z") == "2026-09-18T02:00:00.000Z"


def test_defer_after_failure_gives_up_on_the_last_attempt(connection: sqlite3.Connection) -> None:
    """第 3 次失败 ⇒ ``None``（停止采集这一条 · §06.6「仍失败 ⇒ 停止」）。"""
    repo = PublicationRepo(connection)
    row = _published(connection)
    for _ in range(METRICS_MAX_ATTEMPTS - 2):
        row = repo.defer_metrics(row.id, next_metric_at=now_iso())
        assert defer_after_failure(row) is not None
    row = repo.defer_metrics(row.id, next_metric_at=now_iso())
    assert row.metric_attempts == METRICS_MAX_ATTEMPTS - 1
    assert defer_after_failure(row) is None


def test_metrics_payload_carries_the_source() -> None:
    payload = metrics_payload(PublishMetrics(collected_at="2026-09-18T01:00:00.000Z", views=1))
    assert payload == {
        "views": 1,
        "likes": None,
        "comments": None,
        "shares": None,
        "completion_rate": None,
        "collected_at": "2026-09-18T01:00:00.000Z",
        "source": "publisher",
    }


# ── 采集 ──────────────────────────────────────────────────────────────


async def test_collect_metrics_writes_both_columns(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    row = _published(connection)
    stub = StubPublisher(
        metrics=PublishMetrics(collected_at=now_iso(), views=12000, likes=340, comments=12, shares=3)
    )
    await collect_metrics(
        row.id,
        connection=connection,
        paths=paths,
        config=_config(),
        publisher=stub,  # type: ignore[arg-type]
        now=_hours_after(row, 1),
    )
    fresh = PublicationRepo(connection).get(row.id)
    assert fresh is not None
    assert fresh.metrics["views"] == 12000
    assert fresh.metrics["source"] == "publisher"
    assert len(fresh.metrics_history) == 1
    assert fresh.metrics_history[0]["at"] == fresh.metrics["collected_at"]
    assert stub.calls == ["741"]


async def test_history_appends_and_attempts_reset(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    row = _published(connection)
    PublicationRepo(connection).defer_metrics(row.id, next_metric_at=now_iso())
    stub = StubPublisher(metrics=PublishMetrics(collected_at=now_iso(), views=100))
    for moment in (_hours_after(row, 1), _hours_after(row, 6)):
        await collect_metrics(
            row.id,
            connection=connection,
            paths=paths,
            config=_config(),
            publisher=stub,  # type: ignore[arg-type]
            now=moment,
        )
    fresh = PublicationRepo(connection).get(row.id)
    assert fresh is not None
    assert len(fresh.metrics_history) == 2
    # 一次成功读数 ⇒ 计数器归零（0010 迁移的语义）。
    assert fresh.metric_attempts == 0
    # T+1h 与 T+6h 都采过 ⇒ 下一个是 T+24h（相对**发布时刻**）。
    assert fresh.next_metric_at == _hours_after(row, 24)


async def test_collect_metrics_refuses_a_dry_run_row(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """演练记录（``queued``）没有平台作品号，采不到任何东西 ⇒ 明确报错。"""
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=_task(connection, "演练"),
        platform="douyin",
        account_id="acc_main",
        video_path="data/output/x.mp4",
        video_sha256="b" * 64,
        title="演练",
        dry_run=True,
    )
    with pytest.raises(StudioError) as info:
        await collect_metrics(row.id, connection=connection, paths=paths, config=_config())
    assert info.value.code == ErrorCode.VALIDATION_FAILED


async def test_collect_metrics_rejects_a_missing_publication(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    with pytest.raises(StudioError) as info:
        await collect_metrics("01NOPE", connection=connection, paths=paths, config=_config())
    assert info.value.code == ErrorCode.VALIDATION_FAILED


# ── 轮询 ──────────────────────────────────────────────────────────────


def _service(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    *,
    publisher_factory: Callable[[PublicationRow], Publisher] | None = None,
) -> PublishMetricsService:
    return PublishMetricsService(
        connection=connection, paths=paths, config=_config(), publisher_factory=publisher_factory
    )


async def test_tick_collects_due_rows(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    row = _published(connection, due_in_hours=0)
    stub = StubPublisher(metrics=PublishMetrics(collected_at=now_iso(), views=777))
    service = _service(connection, paths, publisher_factory=lambda _row: stub)  # type: ignore[arg-type, return-value]
    report = await service.tick()
    assert report.collected == (row.id,)
    assert report.deferred == () and report.stopped == ()


async def test_tick_ignores_rows_that_are_not_due(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    _published(connection)  # next_metric_at = 真实时间 +1h ⇒ 还没到点
    service = _service(connection, paths)
    assert service.due() == ()


async def test_tick_yields_while_the_publish_pool_is_busy(
    connection: sqlite3.Connection, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布池正忙 ⇒ 整拍让路（两个浏览器抢同一个 profile 会让**发布**失败）。"""
    row = _published(connection, due_in_hours=0)
    stub = StubPublisher(metrics=PublishMetrics(collected_at=now_iso(), views=1))
    service = _service(connection, paths, publisher_factory=lambda _row: stub)  # type: ignore[arg-type, return-value]
    monkeypatch.setattr(service, "publish_busy", lambda: True)
    report = await service.tick()
    assert report.yielded and report.touched == 0
    assert PublicationRepo(connection).get(row.id).metrics == {}  # type: ignore[union-attr]


async def test_failure_defers_then_stops(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """§06.6：失败 ⇒ 顺延 1 小时重试（≤3 次）；仍失败 ⇒ 停止采集**这一条**。"""
    row = _published(connection, due_in_hours=0)
    stub = StubPublisher(boom=RuntimeError("页面打不开"))
    service = _service(connection, paths, publisher_factory=lambda _row: stub)  # type: ignore[arg-type, return-value]

    # 时钟每轮往前推 2 小时：顺延把下一个时点推到 now+1h，不推就永远"还没到点"。
    assert row.published_at is not None
    moment = parse_iso(row.published_at) + timedelta(hours=1)
    first = await service.tick(now=format_iso(moment))
    assert first.deferred == (row.id,)
    fresh = PublicationRepo(connection).get(row.id)
    assert fresh is not None and fresh.metric_attempts == 1 and fresh.next_metric_at is not None

    for _ in range(METRICS_MAX_ATTEMPTS - 2):
        moment += timedelta(hours=2)
        await service.tick(now=format_iso(moment))
    moment += timedelta(hours=2)
    final = await service.tick(now=format_iso(moment))
    assert final.stopped == (row.id,)
    fresh = PublicationRepo(connection).get(row.id)
    assert fresh is not None and fresh.next_metric_at is None
    assert fresh.metric_attempts == METRICS_MAX_ATTEMPTS


async def test_one_failure_does_not_block_the_others(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """§06.6「不阻塞其他发布」：一条采不到，其余照采。"""
    good = _published(connection, due_in_hours=0)
    other = PublicationRepo(connection).create(
        task_id=_task(connection, "第二条"),
        platform="douyin",
        account_id="acc_main",
        video_path="data/output/y.mp4",
        video_sha256="c" * 64,
        title="第二条",
    )[0]
    PublicationRepo(connection).mark_published(
        other.id, platform_post_id="999", next_metric_at=format_iso(utc_now() - timedelta(minutes=1))
    )

    class _Mixed(StubPublisher):
        async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
            if platform_post_id == "741":
                raise RuntimeError("这一条坏了")
            return PublishMetrics(collected_at=now_iso(), views=5)

    service = _service(connection, paths, publisher_factory=lambda _row: _Mixed())  # type: ignore[arg-type, return-value]
    report = await service.tick()
    assert report.deferred == (good.id,)
    assert report.collected == (other.id,)


def test_parse_iso_round_trip() -> None:
    """顺延与推进都建立在 ``parse_iso`` 上，顺手钉一下它的口径。"""
    assert parse_iso("2026-09-18T01:00:00.000Z") == parse_iso("2026-09-18T09:00:00.000+08:00")


def test_published_status_constant_is_the_repo_one() -> None:
    assert PUBLISHED == "published"
