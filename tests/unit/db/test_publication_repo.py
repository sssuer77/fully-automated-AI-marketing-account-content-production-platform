"""``publications`` 仓储（T5.3 · §03.3.15 / §06.5.4）。

这里测的是**状态机与幂等**，不是 SQL 语法：用真库（临时目录 + 迁移），因为要验的
正是 ``idempotency_key`` 的 UNIQUE、``strftime`` 落的 ``published_at``（限频守卫
数的是它）、以及触发器维护的 ``updated_at``。四件事各自都会**静默走偏**：

① 幂等 ``create`` 命中已有行 ⇒ 返回**原来那一行**、不报错、不插第二行
   （报错会把一个正常的重复调用变成一条无人处理的异常）；
② ``published_at`` 由**数据库**落（两个时钟会让"今天发了几条"在边界上飘）；
③ 失败 ⇒ ``attempt_count + 1``，转人工 ⇒ 不动 ``tasks``；
④ 演练落 ``queued``（写 ``published`` 等于在库里记一条**从没发生过的发布**）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.publication_repo import (
    CANCELED,
    FAILED,
    MANUAL_REQUIRED,
    PUBLISHED,
    QUEUED,
    UPLOADING,
    PublicationRepo,
    PublicationRow,
)
from studio.domain import TaskService
from studio.domain.publish import idempotency_key


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """迁移好的临时库（**用完关掉**，否则 Windows 上临时目录删不干净）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="跑酷合集").id


@pytest.fixture
def repo(connection: sqlite3.Connection) -> PublicationRepo:
    return PublicationRepo(connection)


def _create(repo: PublicationRepo, task_id: str, **overrides: object) -> tuple[PublicationRow, bool]:
    params: dict[str, object] = {
        "task_id": task_id,
        "platform": "douyin",
        "account_id": "acc_main",
        "video_path": "output/videos/x_final.mp4",
        "video_sha256": "a" * 64,
        "title": "标题",
    }
    params.update(overrides)
    return repo.create(**params)  # type: ignore[arg-type]


# ── 幂等 ────────────────────────────────────────────────────────────────


def test_create_is_idempotent_on_the_same_triple(repo: PublicationRepo, task_id: str) -> None:
    """同一份内容 + 同一个账号 ⇒ 一辈子只有一行（重投 / 定时到点都走这里）。"""
    first, created_first = _create(repo, task_id)
    second, created_second = _create(repo, task_id, title="标题改了")

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    # 第二次的标题**不覆盖**已有那一行：快照是"发布那一刻发的是什么"，不能事后改
    assert second.title == "标题"
    assert repo.counts()[QUEUED] == 1


def test_create_key_contains_the_account(repo: PublicationRepo, task_id: str) -> None:
    """换账号 ⇒ 另一行（§06.2.4：同任务分发到多账号是两件事）。"""
    first, _ = _create(repo, task_id)
    second, created = _create(repo, task_id, account_id="acc_second")

    assert created is True
    assert second.id != first.id
    assert repo.counts()[QUEUED] == 2


def test_find_uses_the_idempotency_key(repo: PublicationRepo, task_id: str) -> None:
    row, _ = _create(repo, task_id)

    assert repo.find(task_id=task_id, platform="douyin", account_id="acc_main") is not None
    assert repo.find(task_id=task_id, platform="kuaishou", account_id="acc_main") is None
    assert repo.get_by_key(idempotency_key(task_id, "douyin", "acc_main")) is not None
    assert repo.get(row.id) is not None


# ── 状态机 ──────────────────────────────────────────────────────────────


def test_uploading_then_published(repo: PublicationRepo, task_id: str) -> None:
    """第 ② 步 ⇒ ``uploading``；第 ⑧ 步 ⇒ ``published`` 且 ``published_at`` 由库落。"""
    row, _ = _create(repo, task_id)

    uploading = repo.mark_uploading(row.id)
    assert uploading.status == UPLOADING

    published = repo.mark_published(row.id, url="https://example.com/v/1", platform_post_id="post-1")
    assert published.status == PUBLISHED
    assert published.published_at is not None
    assert published.finished_at is not None
    assert published.url == "https://example.com/v/1"
    assert published.is_terminal is True
    assert published.needs_human is False


def test_failure_counts_and_terminal_states(repo: PublicationRepo, task_id: str) -> None:
    """失败记账 +1；转人工**不是**终态（面板上它是"等人"）。"""
    row, _ = _create(repo, task_id)

    failed = repo.mark_failed(row.id, error_code="PUBLISH_UPLOAD_FAILED", error_message="断流")
    assert failed.status == FAILED
    assert failed.attempt_count == 1
    assert failed.is_terminal is False

    manual = repo.mark_manual_required(row.id, error_code="PUBLISH_LOGIN_EXPIRED", error_message="登录态过期")
    assert manual.status == MANUAL_REQUIRED
    assert manual.needs_human is True
    assert manual.is_terminal is False
    assert manual.finished_at is None, "转人工还没完结：人接手之后才算"
    assert manual.attempt_count == 2, "转人工是 mark_failed 的**替代**，那一次失败的账也要记"


def test_cancel_refuses_a_published_row(repo: PublicationRepo, task_id: str) -> None:
    """已经发出去的取消不了（平台上那条作品还在）。"""
    row, _ = _create(repo, task_id)
    repo.mark_published(row.id, url="https://example.com/v/1", platform_post_id="post-1")

    with pytest.raises(StudioError) as excinfo:
        repo.cancel(row.id, reason="手滑")

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.get(row.id).status == PUBLISHED  # type: ignore[union-attr]


def test_cancel_and_retry_round_trip(repo: PublicationRepo, task_id: str) -> None:
    """取消 ⇒ 终态；人工重试 ⇒ 回 ``queued`` 且计数归零（给它一次完整的机会）。"""
    row, _ = _create(repo, task_id)
    repo.mark_failed(row.id, error_code="PUBLISH_TIMEOUT", error_message="超时")

    canceled = repo.cancel(row.id, reason="不发这个平台了")
    assert canceled.status == CANCELED
    assert canceled.is_terminal is True

    retried = repo.reset_for_retry(row.id)
    assert retried.status == QUEUED
    assert retried.attempt_count == 0
    assert retried.error_code is None
    assert retried.finished_at is None


def test_dry_run_lands_on_queued_not_published(repo: PublicationRepo, task_id: str) -> None:
    """演练**不能**占额度：落 ``queued``，``published_at`` 保持空。"""
    row, _ = _create(repo, task_id, dry_run=True)
    assert row.dry_run is True

    after = repo.mark_dry_run(row.id, evidence={"stage": "before_publish"})

    assert after.status == QUEUED
    assert after.published_at is None
    assert after.evidence["stage"] == "before_publish"


def test_dry_run_does_not_erase_a_real_publication(repo: PublicationRepo, task_id: str) -> None:
    """真发出去过的那一行不会被后一次演练抹掉。"""
    row, _ = _create(repo, task_id)
    repo.mark_published(row.id, url="https://example.com/v/1", platform_post_id="post-1")

    after = repo.mark_dry_run(row.id)

    assert after.status == PUBLISHED
    assert after.url == "https://example.com/v/1"


# ── 查询面 ──────────────────────────────────────────────────────────────


def test_counts_and_manual_queue(repo: PublicationRepo, task_id: str) -> None:
    """六个状态**都在**计数里（面板的区块标题要的是全量数字）。"""
    queued, _ = _create(repo, task_id)
    manual, _ = _create(repo, task_id, account_id="acc_second")
    repo.mark_manual_required(manual.id, error_code="PUBLISH_SELECTOR_MISS", error_message="选择器没命中")

    counts = repo.counts()
    assert counts[QUEUED] == 1
    assert counts[MANUAL_REQUIRED] == 1
    assert counts[PUBLISHED] == 0

    queue = repo.manual_queue(limit=10)
    assert [row.id for row in queue] == [manual.id]
    assert repo.get(queued.id).status == QUEUED  # type: ignore[union-attr]


def test_list_by_status_rejects_unknown_statuses(repo: PublicationRepo, task_id: str) -> None:
    """未知状态**抛**而不是回空列表：回空会让"打错字"看起来像"没有记录"。"""
    _create(repo, task_id)

    with pytest.raises(StudioError) as excinfo:
        repo.list_by_status("publised")

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert repo.list_by_status((QUEUED, MANUAL_REQUIRED))[0].status == QUEUED


def test_list_for_task_is_ordered_by_platform(repo: PublicationRepo, task_id: str) -> None:
    """一条任务的记录按平台排（``idx_pub_task`` 的口径）—— 面板按平台分组显示。"""
    _create(repo, task_id, platform="douyin")
    _create(repo, task_id, platform="kuaishou")

    rows = repo.list_for_task(task_id)
    assert [row.platform for row in rows] == ["douyin", "kuaishou"]
