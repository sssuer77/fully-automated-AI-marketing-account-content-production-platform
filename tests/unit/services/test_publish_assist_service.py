"""面板上「人工过验证」（T6.4 · 真机 2026-09-23）。

这个服务只做一件事：**把"要人输验证码"那一步接回发布链路**。所以这里验的是四组
判据，每一组错了都会静默走偏：

① **它真的开了个可见窗口、并且真的在等人** —— ``headless=False`` +
   ``await_manual_verify=True`` 这两个开关**一起**才对（只开一个的症状分别是
   "人看不见窗口"和"人还没动手它就已经放弃了"）；
② **成功落 ``published``** —— 与自动发布走同一个落点（``next_metric_at`` 也照写，
   否则面板上"人工发的那几条永远不回收数据"）；
③ **失败落 ``manual_required`` 且不交回队列** —— 这条路是人点的、他就在机器前面，
   再排一次队只会让他等一个自己刚看完的过程；
④ **四种"这条路不该走"的状态各拦一句** —— 已发布（R14：再走一遍就是第二条）、
   已取消、演练登记、发布池正在发同一个账号（两个浏览器抢同一个登录态目录，
   最坏的结果不是报错，而是**同一条内容发出去两遍**）。

真机那一条（真起浏览器、真上传 182MB、人真输码）不在这一层：它是**演练**，
不是单测能代替的东西。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from studio.core.config import AccountConfig, PlatformCode, PlatformConfig, PublishConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import CLAIMED_STATUS, JobStore
from studio.db.repositories import AuditRepo
from studio.db.repositories.publication_repo import (
    MANUAL_REQUIRED,
    PUBLISHED,
    PublicationRepo,
)
from studio.domain import TaskService
from studio.domain.publish import unit_ref
from studio.publish.base import (
    Publisher,
    PublishEvidence,
    PublishRequest,
    PublishResult,
)
from studio.services import publish_assist_service as assist_module
from studio.services.publish_assist_service import (
    AUDIT_ASSIST,
    LOG_SOURCE,
    PublishAssistService,
)

ACCOUNT_ID = "acc_douyin"
#: 标注成 ``PlatformCode`` 而不是让 mypy 推成 ``str``：``config.platforms`` 是
#: ``dict[PlatformCode, …]``，用 ``str`` 建字典会在两处各留一个 ``type: ignore``
#: （一个在账号、一个在平台表），而它们本来就不该需要豁免。
PLATFORM: PlatformCode = "douyin"


# ── 假件 ──────────────────────────────────────────────────────────────


class FakePublisher:
    """可编排的假发布器（只答 ``publish``；这一层不碰探测与回收）。"""

    def __init__(self, rig: Rig) -> None:
        self._rig = rig

    async def publish(self, req: PublishRequest) -> PublishResult:
        self._rig.requests.append(req)
        row = self._rig.repo.get(self._rig.publication_id)
        assert row is not None
        self._rig.status_during_publish = row.status
        return self._rig.result


class FakeBuilder:
    """假装配器 —— 顺带记下**这一次要的是不是"可见窗口 + 有人在场"**。"""

    def __init__(self, rig: Rig) -> None:
        self._rig = rig

    def __call__(
        self,
        account: AccountConfig,
        *,
        headless: bool,
        await_manual_verify: bool = False,
    ) -> Publisher:
        self._rig.builder_calls.append((account.account_id, headless, await_manual_verify))
        # 假件只答 ``publish``（这一层不碰探测与回收）⇒ 不是 ``Publisher`` 子类。
        # 照 ABC 把另外几个抽象方法补全是给测试加噪音，cast 把这件事写在明面上。
        return cast(Publisher, FakePublisher(self._rig))


class FakeLog:
    """假日志出口（``state.logs.append`` 的形状）。"""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.lines.append(kwargs)


# ── 夹具 ──────────────────────────────────────────────────────────────


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一条**待人工**的发布记录。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    repo: PublicationRepo
    store: JobStore
    audit: AuditRepo
    log: FakeLog
    config: PublishConfig
    task_id: str
    video: Path
    publication_id: str
    #: 假发布器要答的那一条结论（用例自己设）。
    result: PublishResult = field(
        default_factory=lambda: PublishResult.published(
            url="https://example.invalid/v/1", platform_post_id="post-1"
        )
    )
    builder_calls: list[tuple[str, bool, bool]] = field(default_factory=list)
    requests: list[PublishRequest] = field(default_factory=list)
    status_during_publish: str | None = None


def _config() -> PublishConfig:
    """一份够用的发布配置（开关**打开**：这条路的意义就是"真发"）。"""
    return PublishConfig(
        enabled=True,
        metrics_schedule_hours=[6],
        accounts=[
            AccountConfig(
                account_id=ACCOUNT_ID,
                platform=PLATFORM,
                profile_dir=Path("data/browser_profile") / ACCOUNT_ID,
                daily_limit=3,
                min_gap_min=30,
            )
        ],
        platforms={
            PLATFORM: PlatformConfig(
                publisher="douyin",
                profile="douyin_1080x1920_30fps_v1",
                enabled=True,
                title_max=55,
                caption_max=1000,
            )
        },
    )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    config = _config()
    # 配置是**盘上那份**（服务自己读）：这里把它换成一份确定的，免得用例的结论
    # 跟着操作员在面板上改过的账号清单跑。
    monkeypatch.setattr(assist_module, "load_publish_config", lambda _paths: config)

    task = TaskService(connection).create(title="跑酷合集")
    video = paths.videos_dir / f"20260923-120000_{task.id}_final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=task.id,
        platform=PLATFORM,
        account_id=ACCOUNT_ID,
        video_path=video.as_posix(),
        video_sha256="0" * 64,
        title="离谱跑酷地图",
        caption="点个关注",
    )
    repo.mark_uploading(row.id)
    repo.mark_manual_required(
        row.id,
        error_code=ErrorCode.PUBLISH_FAILED.value,
        error_message="平台要求短信验证：这一步要人来做，自动流程到此为止",
    )
    try:
        yield Rig(
            paths=paths,
            connection=connection,
            repo=repo,
            store=JobStore(connection),
            audit=AuditRepo(connection),
            log=FakeLog(),
            config=config,
            task_id=task.id,
            video=video,
            publication_id=row.id,
        )
    finally:
        connection.close()


def _service(rig: Rig) -> PublishAssistService:
    return PublishAssistService(
        rig.paths,
        connection=rig.connection,
        audit=rig.audit,
        log=rig.log,
        publisher_builder=FakeBuilder(rig),
    )


def _actions(rig: Rig) -> list[str]:
    return [row.action for row in rig.audit.list_recent(limit=50)]


# ── ① 可见窗口 + 有人在场 ──────────────────────────────────────────────


async def test_it_publishes_in_a_visible_window_with_a_human_waiting(rig: Rig) -> None:
    """两个开关**一起**：``headless=False``（人看得见窗口）与
    ``await_manual_verify=True``（人还没输码时它不许走）。

    只开前者 ⇒ 弹框那一下它照样收场（人刚坐下就被告知"到此为止"）；
    只开后者 ⇒ 窗口是无头的，人没有地方输码。
    """
    await _service(rig).assist(rig.publication_id, reason="验证码已输")

    assert rig.builder_calls == [(ACCOUNT_ID, False, True)]
    request = rig.requests[0]
    # 这条路**没有** dry-run 形态：它的全部意义就是把这一条发出去。
    assert request.dry_run is False
    assert request.title == "离谱跑酷地图"
    assert request.video_path == rig.video
    # 长动作期间那条记录**保持"待人工"**：人此刻正看着面板上这一行，而这一行必须
    # 一直是他能再点一次的那一行（窗口可能被他自己关掉、短信可能没来）。把它翻成
    # ``uploading`` 会让它看起来像"另一个进程在发它" —— 真机上那两条卡住的记录
    # 正是这个状态，两者混在一起之后，"我能再点一次吗"就没有答案了。
    assert rig.status_during_publish == MANUAL_REQUIRED


async def test_a_successful_run_lands_in_published_with_the_next_metric(rig: Rig) -> None:
    outcome = await _service(rig).assist(rig.publication_id)

    fresh = rig.repo.get(rig.publication_id)
    assert fresh is not None
    assert fresh.status == PUBLISHED
    assert fresh.url == "https://example.invalid/v/1"
    assert fresh.platform_post_id == "post-1"
    # 与自动发布同一个落点：不写这个数，"人工发的那几条永远不回收数据"。
    assert fresh.next_metric_at is not None
    assert outcome["publication"].id == rig.publication_id
    assert outcome["action"] == "assist"
    assert "已发出去了" in outcome["message"]
    assert outcome["waited_sec"] >= 0.0


async def test_it_leaves_a_trail_in_the_audit_and_the_log(rig: Rig) -> None:
    await _service(rig).assist(rig.publication_id, reason="验证码已输")

    assert AUDIT_ASSIST in _actions(rig)
    recorded = rig.audit.list_recent(limit=1)[0]
    assert recorded.actor == "user"
    assert recorded.target_id == rig.publication_id
    assert recorded.reason == "验证码已输"
    assert recorded.after is not None and recorded.after["status"] == PUBLISHED
    assert [line["source"] for line in rig.log.lines] == [LOG_SOURCE, LOG_SOURCE]
    assert any("人工过验证成功" in line["message"] for line in rig.log.lines)


# ── ② 失败：转人工，不交回队列 ────────────────────────────────────────


async def test_a_failed_run_goes_to_manual_required_and_not_back_to_the_queue(rig: Rig) -> None:
    rig.result = PublishResult.failure(ErrorCode.PUBLISH_TIMEOUT, "点下发布后等不到结果页")

    outcome = await _service(rig).assist(rig.publication_id)

    fresh = rig.repo.get(rig.publication_id)
    assert fresh is not None
    assert fresh.status == MANUAL_REQUIRED
    assert fresh.error_code == ErrorCode.PUBLISH_TIMEOUT.value
    assert fresh.error_message == "点下发布后等不到结果页"
    assert fresh.next_metric_at is None
    # **不交回队列**：三个按钮留在原地，他可以就地再来一次。
    assert rig.store.list_jobs(pool="publish", limit=10) == ()
    assert "仍在待人工" in outcome["message"]
    assert AUDIT_ASSIST in _actions(rig)
    assert any(line["level"] == "warn" for line in rig.log.lines)


async def test_the_evidence_of_the_run_is_written_back(rig: Rig) -> None:
    """取证要跟着结论一起落库 —— 没有它，"这一趟到底停在哪儿"就没人答得出来。"""
    rig.result = PublishResult.failure(
        ErrorCode.PUBLISH_TIMEOUT,
        "点下发布后等不到结果页",
        evidence=PublishEvidence(
            screenshot_path=rig.paths.data_dir / "shot.png",
            selector_version="2026-09-23.4",
            stage="07-result",
        ),
    )
    await _service(rig).assist(rig.publication_id)

    fresh = rig.repo.get(rig.publication_id)
    assert fresh is not None
    assert fresh.evidence["selector_version"] == "2026-09-23.4"
    assert fresh.evidence["stage"] == "07-result"
    assert fresh.evidence["screenshot_path"].endswith("shot.png")


# ── ③ 四种"这条路不该走" ──────────────────────────────────────────────


async def test_an_already_published_row_is_refused(rig: Rig) -> None:
    """R14：发布不可逆。再走一遍就是**第二条**作品。"""
    rig.repo.mark_published(rig.publication_id, url="https://example.invalid/v/1")

    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist(rig.publication_id)

    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert "不能再走一遍" in excinfo.value.message
    assert rig.builder_calls == []


async def test_a_canceled_row_is_refused(rig: Rig) -> None:
    rig.repo.cancel(rig.publication_id)

    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist(rig.publication_id)

    assert "已经被取消" in excinfo.value.message
    assert rig.builder_calls == []


async def test_a_dry_run_row_is_refused(rig: Rig) -> None:
    """演练登记的记录是"停在第 ⑥ 步之前"的事实：拿它走真发布，等于让一次演练的
    残留变成一条真作品 —— 而用户从没说过要发它。"""
    repo = PublicationRepo(rig.connection)
    # 换一个任务：``publications`` 的幂等键是 ``(task_id, platform, account_id)``，
    # 同一个任务上再登记一次拿到的是**原来那一行**（它不是演练登记）。
    other_task = TaskService(rig.connection).create(title="另一条")
    row, _ = repo.create(
        task_id=other_task.id,
        platform=PLATFORM,
        account_id=ACCOUNT_ID,
        video_path=rig.video.as_posix(),
        video_sha256="1" * 64,
        title="演练",
        dry_run=True,
    )

    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist(row.id)

    assert "演练登记" in excinfo.value.message
    assert rig.builder_calls == []


async def test_a_missing_video_is_refused(rig: Rig) -> None:
    rig.video.unlink()

    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist(rig.publication_id)

    assert excinfo.value.code == ErrorCode.RENDER_FAILED
    assert "找不到这条记录的成片" in excinfo.value.message
    assert rig.builder_calls == []


async def test_a_claimed_publish_job_for_the_same_account_is_refused(rig: Rig) -> None:
    """发布池正在发同一个号 ⇒ 拒绝。两个浏览器抢同一个 ``user_data_dir`` 是**发不出去**
    的，而更坏的那一半是：两条路径同时点发布 ⇒ 同一条内容出去两遍（R14 不可逆）。"""
    job_id = rig.store.enqueue(
        task_id=rig.task_id,
        pool="publish",
        unit_type="publish",
        unit_ref=unit_ref(PLATFORM, ACCOUNT_ID),
    )
    assert job_id is not None
    assert rig.store.claim(pool="publish", worker_id="publish#1@4242") is not None

    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist(rig.publication_id)

    assert "发布池正在发这个账号" in excinfo.value.message
    assert rig.builder_calls == []


async def test_a_claimed_job_for_another_account_does_not_block_it(rig: Rig) -> None:
    """拦的是"同一个账号"，不是"发布池在忙"：另一个号在跑与本条无关（各自一份登录态）。"""
    rig.store.enqueue(
        task_id=rig.task_id,
        pool="publish",
        unit_type="publish",
        unit_ref=unit_ref(PLATFORM, "acc_other"),
    )
    assert rig.store.claim(pool="publish", worker_id="publish#1@4242") is not None
    assert (
        rig.connection.execute(
            "SELECT count(*) FROM jobs WHERE pool = 'publish' AND status = ?", (CLAIMED_STATUS,)
        ).fetchone()[0]
        == 1
    )

    await _service(rig).assist(rig.publication_id)

    assert rig.builder_calls == [(ACCOUNT_ID, False, True)]


async def test_a_claim_whose_lease_expired_does_not_block_the_human(rig: Rig) -> None:
    """★ 真机坑（2026-09-23）：**死掉的 worker 留下的孤儿认领**不许拦人。

    `publish#1@65928` 崩了之后，那条作业在库里挂了一个多小时还写着 `claimed`
    （租约早过期了），而发布池里一个在跑的进程都没有。拿它拦人，等于让"上一次崩了"
    变成"这一次不许你修" —— 而这条路本来就是人来收拾残局的那一条。
    """
    rig.store.enqueue(
        task_id=rig.task_id,
        pool="publish",
        unit_type="publish",
        unit_ref=unit_ref(PLATFORM, ACCOUNT_ID),
    )
    assert rig.store.claim(pool="publish", worker_id="publish#1@65928") is not None
    # 把租约推到过去（真机上不需要人来推：worker 一崩，租约自己就到点了）。
    rig.connection.execute(
        "UPDATE jobs SET lease_expires_at = ? WHERE status = ?",
        ("2000-01-01T00:00:00.000Z", CLAIMED_STATUS),
    )

    await _service(rig).assist(rig.publication_id)

    assert rig.builder_calls == [(ACCOUNT_ID, False, True)]


async def test_an_unknown_publication_is_refused(rig: Rig) -> None:
    with pytest.raises(StudioError) as excinfo:
        await _service(rig).assist("01NOPE00000000000000000000")

    assert "发布记录不存在" in excinfo.value.message
    assert rig.builder_calls == []
