"""记忆沉淀（T5.4 · §06.8）：高互动评论回流 / 低互动降权 / 汇总文件可被直接消费。

最要紧的一条是**闭环**：``sink_memory`` 写出来的 ``auto_*.md`` 要能被
``InputService.import_feedback()`` 直接吃掉，而且**不会把同一批评论记第二遍**
（幂等键 ``(source_file, content)`` 正好命中 —— 这正是它设计成这样的理由）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from studio.core.clock import format_iso, utc_now
from studio.core.config import PublishConfig, load_config
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.direction_repo import DirectionRepo
from studio.db.repositories.feedback_repo import FeedbackItemRepo
from studio.db.repositories.publication_repo import PublicationRepo
from studio.db.repositories.topic_repo import TopicRepo
from studio.domain import TaskService
from studio.publish.base import PublishMetrics
from studio.publish.memory import (
    DEMOTION_MAX_RATIO,
    MIN_COMMENT_LIKES,
    CommentSample,
    digest_path,
    is_high_engagement,
    sink_memory,
)
from studio.services.input_service import InputService


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


def _config() -> PublishConfig:
    return load_config(StudioPaths.from_env()).bundle.publish


class _NoComments:
    """没有 ``fetch_comments`` 的发布器（模块注释里那条缝的"没有"一侧）。"""

    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        raise AssertionError("本用例不采数")


def _published(
    connection: sqlite3.Connection,
    *,
    views: int | None = None,
    platform: str = "douyin",
    account_id: str = "acc_main",
) -> tuple[str, str]:
    """一条已发布记录 ⇒ ``(publication_id, task_id)``。"""
    task_id = TaskService(connection).create(title="跑酷合集").id
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=task_id,
        platform=platform,
        account_id=account_id,
        video_path="data/output/x.mp4",
        video_sha256="a" * 64,
        title="跑酷",
    )
    repo.mark_published(
        row.id,
        platform_post_id="741",
        next_metric_at=format_iso(utc_now() + timedelta(hours=1)),
    )
    if views is not None:
        repo.record_metrics(
            row.id,
            {
                "views": views,
                "likes": 1,
                "comments": 0,
                "shares": 0,
                "collected_at": format_iso(utc_now()),
                "source": "publisher",
            },
            next_metric_at=None,
        )
    return row.id, task_id


def _direction_with_topics(connection: sqlite3.Connection, *, task_id: str, scores: list[float]) -> list[str]:
    """建一个方向 + 若干候选选题（第一条挂在 ``task_id`` 上 ⇒ "这条作品的同类"）。"""
    direction_id = DirectionRepo(connection).insert_batch(
        batch_id="01BATCH0000000000000000000",
        directions=[{"title": "跑酷极限", "rationale": "定位契合"}],
    )[0]
    topic_ids = TopicRepo(connection).insert_many(
        [
            {
                "direction_id": direction_id,
                "seq": index,
                "title": f"选题 {index}",
                "angle": "角度",
                "score": score,
                "status": "candidate",
            }
            for index, score in enumerate(scores, start=1)
        ]
    )
    TopicRepo(connection).set_status(topic_id=topic_ids[0], status="selected", task_id=task_id)
    return topic_ids


# ── 判据 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        (CommentSample(text="好", likes=MIN_COMMENT_LIKES), True),
        (CommentSample(text="好", likes=MIN_COMMENT_LIKES - 1), False),
        (CommentSample(text="好", replies=5), True),
        (CommentSample(text="好"), False),
        (CommentSample(text="好", likes=None, replies=None), False),
    ],
)
def test_is_high_engagement(comment: CommentSample, expected: bool) -> None:
    """§6.8 ①：点赞 ≥ 阈值 **或** 回复数 ≥ 阈值；``None`` **不算命中**。"""
    assert is_high_engagement(comment) is expected


def test_digest_path_is_monthly_and_local(paths: StudioPaths) -> None:
    path = digest_path(paths, now="2026-09-18T16:30:00.000Z")
    assert path == paths.feedback_dir / "auto_202609.md"


# ── 闭环 ──────────────────────────────────────────────────────────────


async def test_sink_memory_closes_the_loop(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """高互动评论 ⇒ ``feedback_items(is_auto=1)`` + 汇总文件**能被解析器直接吃**。"""
    pub_id, _task_id = _published(connection, views=9000)
    result = await sink_memory(
        pub_id,
        connection=connection,
        paths=paths,
        config=_config(),
        comments=[
            CommentSample(text="这个跑酷太帅了，求教程", likes=120),
            CommentSample(text="沙发", likes=1),
        ],
        now="2026-09-18T02:00:00.000Z",
    )
    assert result.feedback_items_created == 1
    assert result.comments_seen == 2
    assert result.planner_consumable is True

    rows = FeedbackItemRepo(connection).list_recent()
    assert len(rows) == 1
    assert rows[0].is_auto is True
    assert rows[0].source_publication_id == pub_id
    assert rows[0].content == "这个跑酷太帅了，求教程"

    text = result.digest_path.read_text(encoding="utf-8")
    assert "douyin|2026-09-18|unknown|这个跑酷太帅了，求教程" in text
    assert "沙发" not in text


async def test_import_feedback_does_not_double_count_the_digest(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """汇总文件被 ``import_feedback`` 扫到时，``(source_file, content)`` 幂等键命中 ⇒ 不重复入库。"""
    pub_id, _ = _published(connection, views=100)
    await sink_memory(
        pub_id,
        connection=connection,
        paths=paths,
        config=_config(),
        comments=[CommentSample(text="求教程", likes=99)],
    )
    report = InputService(connection, paths=paths).import_feedback()
    assert report.files and report.inserted == 0
    assert FeedbackItemRepo(connection).count() == 1


async def test_sink_memory_is_idempotent(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """同一批评论沉淀两次：库里不重复，文件里也不重复。"""
    pub_id, _ = _published(connection, views=100)
    comments = [CommentSample(text="求教程", likes=99)]
    first = await sink_memory(pub_id, connection=connection, paths=paths, config=_config(), comments=comments)
    second = await sink_memory(
        pub_id, connection=connection, paths=paths, config=_config(), comments=comments
    )
    assert first.feedback_items_created == 1
    assert second.feedback_items_created == 0
    assert FeedbackItemRepo(connection).count() == 1
    assert first.digest_path.read_text(encoding="utf-8").count("求教程") == 1


async def test_pipe_in_a_comment_does_not_break_the_digest(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """评论里的 ``|`` / 换行会被压平（否则一行会被切成两行或多列 ⇒ 解析告警）。"""
    pub_id, _ = _published(connection, views=100)
    result = await sink_memory(
        pub_id,
        connection=connection,
        paths=paths,
        config=_config(),
        comments=[CommentSample(text="1. 这样 | 2. 那样\n第三行", likes=99)],
    )
    assert result.planner_consumable is True


# ── 低互动降权 ────────────────────────────────────────────────────────


async def test_low_engagement_demotes_by_at_most_twenty_percent(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """§6.8 ②：播放低于同账号近期中位数 50% ⇒ 同方向候选降权（**幅度上限 20%**）。"""
    for _ in range(3):
        _published(connection, views=10000)
    pub_id, task_id = _published(connection, views=1000)
    topic_ids = _direction_with_topics(connection, task_id=task_id, scores=[10.0, 8.0])

    result = await sink_memory(pub_id, connection=connection, paths=paths, config=_config(), comments=[])
    assert result.low_engagement is True
    assert result.topics_demoted == 1  # 只有那条 **candidate**；selected 的那条不动

    repo = TopicRepo(connection)
    selected = repo.get(topic_ids[0])
    candidate = repo.get(topic_ids[1])
    assert selected is not None and selected.score == 10.0  # 已被人选中的不改分
    assert candidate is not None
    assert candidate.score == pytest.approx(8.0 * (1.0 - DEMOTION_MAX_RATIO))


async def test_high_engagement_does_not_demote(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    for _ in range(3):
        _published(connection, views=10000)
    pub_id, task_id = _published(connection, views=9000)
    topic_ids = _direction_with_topics(connection, task_id=task_id, scores=[10.0, 8.0])
    result = await sink_memory(pub_id, connection=connection, paths=paths, config=_config(), comments=[])
    assert result.low_engagement is False
    assert result.topics_demoted == 0
    assert TopicRepo(connection).get(topic_ids[1]).score == 8.0  # type: ignore[union-attr]


async def test_unknown_views_never_count_as_low(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """采不到播放量 ⇒ **不判**（§06.6：禁止用 0 冒充"无数据"）。"""
    for _ in range(3):
        _published(connection, views=10000)
    pub_id, task_id = _published(connection)  # 没有 metrics
    _direction_with_topics(connection, task_id=task_id, scores=[10.0, 8.0])
    result = await sink_memory(pub_id, connection=connection, paths=paths, config=_config(), comments=[])
    assert result.low_engagement is False and result.topics_demoted == 0


# ── 边角 ──────────────────────────────────────────────────────────────


async def test_no_comment_source_is_not_a_crash(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """平台适配层还没提供评论抓取 ⇒ 只做 ②③，并在结论里说清楚。"""
    pub_id, _ = _published(connection, views=100)
    result = await sink_memory(
        pub_id,
        connection=connection,
        paths=paths,
        config=_config(),
        publisher=_NoComments(),  # type: ignore[arg-type]
    )
    assert result.comments_seen == 0
    assert result.feedback_items_created == 0
    assert result.planner_consumable is True


async def test_sink_memory_ignores_records_that_were_never_published(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    repo = PublicationRepo(connection)
    task_id = TaskService(connection).create(title="演练").id
    row, _ = repo.create(
        task_id=task_id,
        platform="douyin",
        account_id="acc_main",
        video_path="data/output/x.mp4",
        video_sha256="b" * 64,
        title="演练",
        dry_run=True,
    )
    result = await sink_memory(row.id, connection=connection, paths=paths, config=_config())
    assert result.feedback_items_created == 0
    assert result.planner_consumable is False
