"""通用发布器 · §06.5.3 八步（T5.2）。

这里用**假页面**把八步逐条走完：毫秒级、不起浏览器、不联网。真机演练（真 Playwright
打本地靶页）在 ``tests/integration/test_publish_dryrun.py`` —— 两边跑的是**同一份流程
代码**，所以这一层验的是"判据对不对"，那一层验的是"对着真浏览器也成立"。

八步里最要紧的两条，各自有一组用例：
- **第 ⑤ 步回读比对**：它是唯一一条能证明"发出去的是我们写的"的判据（§06.5.3）；
- **第 ⑥ 步 dry-run 不点发布**：R14 不可逆，这条要是漏了，演练就变成了真发布。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import AccountConfig, PlatformConfig
from studio.core.errors import ErrorCode, PublishError
from studio.core.paths import StudioPaths
from studio.publish.base import PublisherContext, PublishRequest, PublishStatus
from studio.publish.platforms.fixture import FixturePublisher
from studio.publish.playwright_publisher import (
    READBACK_ATTEMPTS,
    PlaywrightPublisher,
)
from studio.publish.selectors import SelectorPack

# ── 假页面 ────────────────────────────────────────────────────────────

LOGIN_OK = "#ok"
LOGIN_REQUIRED = "#login"
VIDEO = "#video"
TITLE = "#title"
CAPTION = "#caption"
PROGRESS = "#progress"
PUBLISH = "#publish"
SUCCESS = "#success"
REJECT = "#reject"
POST_LINK = "#post-link"
METRIC_ROW = '[data-post-id="{post_id}"]'
METRIC_VIEWS = ".m-views"
METRIC_LIKES = ".m-likes"
METRIC_COMMENTS = ".m-comments"
METRIC_SHARES = ".m-shares"
METRIC_COMPLETION = ".m-completion"


class FakePage:
    """按 **Playwright 的语义**演一个页面（不是"想当然的页面"）。

    几处刻意的讲究：
    - ``query_selector`` **不等待**（真 Playwright 的 ``query_selector`` 也不等待）；
    - ``inner_text`` 只返回**可见**元素的文本，``text_content`` 不看可见性；
    - ``input_value`` 只对 ``<input>/<textarea>`` 有效，对富文本区抛 —— 真 Playwright
      就是这么抛的，而"用错读法"正是 ``readback`` 声明要防的那件事。
    """

    def __init__(
        self,
        *,
        logged_in: bool = True,
        login_page_text: str = "请扫码登录",
        eat: str = "",
        progress_ticks: int = 0,
        result: str = "success",
        title_readback: str | None = None,
        caption_readback: str | None = None,
        missing: frozenset[str] = frozenset(),
        caption_is_textarea: bool = False,
        post_url: str | None = None,
        metric_text: dict[str, str] | None = None,
    ) -> None:
        self.visited: list[str] = []
        self.clicks: list[str] = []
        self.uploaded: list[Any] = []
        self.screenshots: list[str] = []
        self.evaluate_calls: list[str] = []
        self._logged_in = logged_in
        self._login_page_text = login_page_text
        self._eat = eat
        self._progress_ticks = progress_ticks
        self._result = result
        self._missing = missing
        self._title_readback = title_readback
        self._caption_readback = caption_readback
        self._caption_is_textarea = caption_is_textarea
        self._post_url = post_url
        #: 管理页上"这一行作品的四个数字"（键 = 后代选择器，值 = 页面上的原文）。
        #: 原文原样给（``1.2万`` 这种缩写也照给）：缩写解析正是要验的东西之一。
        self._metric_text = dict(metric_text or {})
        self.title = ""
        self.caption = ""

    # ── 动作 ────────────────────────────────────────────────────────

    async def goto(self, url: str, **_: Any) -> Any:
        self.visited.append(url)
        return None

    async def query_selector(self, selector: str) -> Any | None:
        if selector in self._missing:
            return None
        # 查表而不是 if 链：多一个"元素在不在"的判据时只加一行，不改控制流。
        present = {
            LOGIN_OK: self._logged_in,
            LOGIN_REQUIRED: not self._logged_in,
            PROGRESS: self._progress_ticks > 0,
            SUCCESS: self._result == "success",
            REJECT: self._result == "reject",
        }
        return object() if present.get(selector, True) else None

    async def wait_for_selector(self, selector: str, **_: Any) -> Any:
        handle = await self.query_selector(selector)
        if handle is None:
            raise TimeoutError(selector)
        return handle

    async def set_input_files(self, selector: str, files: Any) -> None:
        if selector in self._missing:
            raise RuntimeError(f"no element: {selector}")
        self.uploaded.append((selector, files))

    async def fill(self, selector: str, value: str) -> None:
        if selector in self._missing:
            raise RuntimeError(f"no element: {selector}")
        if selector == TITLE:
            self.title = value
        elif selector == CAPTION:
            self.caption = self._swallow(value)

    async def click(self, selector: str, **_: Any) -> None:
        self.clicks.append(selector)

    async def screenshot(self, *, path: Any, full_page: bool = False) -> bytes:
        # ``to_thread``：真 Playwright 的 screenshot 不阻塞事件循环，假页面也照做 ——
        # 否则"截图期间事件循环是活的"这件事在单测里被验成了假的。
        await asyncio.to_thread(_write_png, Path(path))
        self.screenshots.append(str(path))
        return b"\x89PNG fake"

    async def content(self) -> str:
        return "<html><body>fake</body></html>"

    async def evaluate(self, expression: str) -> Any:
        self.evaluate_calls.append(expression)
        return False

    def set_default_timeout(self, timeout: float) -> None:
        self._timeout = timeout

    # ── 读取 ────────────────────────────────────────────────────────

    async def inner_text(self, selector: str) -> str:
        if selector in self._missing:
            raise RuntimeError(f"no element: {selector}")
        values = {
            "body": self._login_page_text,
            LOGIN_OK: "测试账号",
            CAPTION: self._caption_value(),
            TITLE: self._title_value(),
            REJECT: "审核不通过",
            SUCCESS: "发布成功",
        }
        for suffix, text in self._metric_text.items():
            if selector.endswith(suffix):
                return text
        return values.get(selector, "")

    async def text_content(self, selector: str) -> str | None:
        if selector == PROGRESS:
            if self._progress_ticks <= 0:
                return None
            self._progress_ticks -= 1
            return "100%" if self._progress_ticks == 0 else "42%"
        return await self.inner_text(selector)

    async def input_value(self, selector: str) -> str:
        if selector == CAPTION and not self._caption_is_textarea:
            raise RuntimeError("contenteditable 上读 input_value")
        if selector == TITLE:
            return self._title_readback if self._title_readback is not None else self.title
        if selector == CAPTION:
            return self._caption_readback if self._caption_readback is not None else self.caption
        return ""

    async def get_attribute(self, selector: str, name: str) -> str | None:
        if selector == POST_LINK and self._post_url:
            return self._post_url if name == "href" else None
        return None

    # ── 内部 ────────────────────────────────────────────────────────

    def _title_value(self) -> str:
        return self._title_readback if self._title_readback is not None else self.title

    def _caption_value(self) -> str:
        return self._caption_readback if self._caption_readback is not None else self.caption

    def _swallow(self, value: str) -> str:
        """复现平台编辑器的两种吞法（**静默**吞，与真平台一致）。"""
        if self._eat == "emoji":
            return "".join(ch for ch in value if not _is_emoji(ch))
        if self._eat == "newlines":
            return value.replace("\n", " ")
        return value


def _write_png(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG fake")


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2B00 <= code <= 0x2BFF
        or ch in "\ufe0f\u200d"
    )


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakePage:
        self.entered += 1
        return self.page

    async def __aexit__(self, *_: Any) -> None:
        self.exited += 1


# ── 夹具 ──────────────────────────────────────────────────────────────


def _pack(**overrides: Any) -> SelectorPack:
    selectors = {
        "login_ok": LOGIN_OK,
        "login_required": LOGIN_REQUIRED,
        "upload_input": VIDEO,
        "title_input": TITLE,
        "caption_input": CAPTION,
        "upload_progress": PROGRESS,
        "cover_input": "#cover",
        "cover_trigger": "#cover-trigger",
        "publish_button": PUBLISH,
        "success_marker": SUCCESS,
        "reject_marker": REJECT,
        "post_link": POST_LINK,
        "metric_row": METRIC_ROW,
        "metric_views": METRIC_VIEWS,
        "metric_likes": METRIC_LIKES,
        "metric_comments": METRIC_COMMENTS,
        "metric_shares": METRIC_SHARES,
        "metric_completion_rate": METRIC_COMPLETION,
    }
    selectors.update(overrides.pop("selectors", {}))
    markers: dict[str, tuple[str, ...]] = {
        "login_expired_text": ("登录已过期",),
        "review_rejected_text": ("审核不通过",),
    }
    markers.update(overrides.pop("markers", {}))
    return SelectorPack(
        platform="douyin",
        version="test-1",
        urls={"upload": "https://example.invalid/upload", "manage": "https://example.invalid/manage"},
        selectors=selectors,
        markers=markers,
        readback=overrides.pop("readback", {"title": "value", "caption": "text"}),
        source=Path("selectors/douyin.yaml"),
    )


def _ctx(tmp_paths: StudioPaths) -> PublisherContext:
    return PublisherContext(
        paths=tmp_paths,
        account=AccountConfig(
            account_id="acc_main",
            platform="douyin",
            profile_dir=tmp_paths.browser_profile_dir / "acc_main",
        ),
        platform=PlatformConfig(
            publisher="douyin",
            profile="douyin_1080x1920_30fps_v1",
            enabled=True,
            title_max=55,
            caption_max=1000,
        ),
    )


def _request(tmp_path: Path, **overrides: Any) -> PublishRequest:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"fake video")
    data: dict[str, Any] = {
        "task_id": "01TASK",
        "platform": "douyin",
        "account_id": "acc_main",
        "video_path": video,
        "title": "离谱跑酷地图",
        "caption": "点个关注",
        "dry_run": True,
    }
    data.update(overrides)
    return PublishRequest(**data)


def _publisher(
    tmp_paths: StudioPaths,
    page: FakePage,
    *,
    pack: SelectorPack | None = None,
    settle_sec: float = 0.0,
) -> tuple[PlaywrightPublisher, FakeSession]:
    session = FakeSession(page)

    class _Publisher(PlaywrightPublisher):
        platform = "douyin"

    publisher = _Publisher(
        _ctx(tmp_paths),
        pack=pack or _pack(),
        session_factory=lambda _ctx: session,
        settle_sec=settle_sec,
    )
    return publisher, session


# ── 第 ①②步：打开 + 上传 ─────────────────────────────────────────────


class TestOpenAndUpload:
    async def test_opens_the_upload_url(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        result = await publisher.publish(_request(tmp_path))
        assert result.ok
        assert page.visited == ["https://example.invalid/upload"]

    async def test_uploads_the_final_video(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        request = _request(tmp_path)
        await publisher.publish(request)
        assert (VIDEO, request.video_path) in page.uploaded

    async def test_not_logged_in_goes_to_manual_required(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """§06.10：登录态失效 ⇒ ``PUBLISH_LOGIN_EXPIRED`` + ``manual_required``，**不自动登录**。"""
        page = FakePage(logged_in=False)
        publisher, _ = _publisher(tmp_paths, page)
        result = await publisher.publish(_request(tmp_path))
        assert not result.ok
        assert result.status == PublishStatus.MANUAL_REQUIRED
        assert result.error_code == ErrorCode.PUBLISH_LOGIN_EXPIRED
        assert "扫码" in (result.error_message or "")
        assert page.uploaded == []

    async def test_missing_video_is_reported_before_touching_the_page(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        result = await publisher.publish(_request(tmp_path, video_path=tmp_path / "gone.mp4"))
        assert not result.ok
        assert result.error_code == ErrorCode.PUBLISH_UPLOAD_FAILED
        assert page.uploaded == []

    async def test_upload_waits_for_progress_to_finish(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(progress_ticks=2)
        publisher, _ = _publisher(tmp_paths, page)
        assert (await publisher.publish(_request(tmp_path))).ok
        assert page.title == "离谱跑酷地图"

    async def test_upload_timeout_is_reported(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(progress_ticks=10_000)
        publisher, _ = _publisher(tmp_paths, page)
        publisher._upload_timeout_sec = 0.05
        result = await publisher.publish(_request(tmp_path))
        assert result.error_code == ErrorCode.PUBLISH_TIMEOUT


# ── 第 ③④步：填字 + 封面 ─────────────────────────────────────────────


class TestFillAndCover:
    async def test_fills_title_and_caption(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        await publisher.publish(_request(tmp_path, caption="今天看一张离谱的跑酷地图 #跑酷"))
        assert page.title == "离谱跑酷地图"
        assert page.caption == "今天看一张离谱的跑酷地图 #跑酷"

    async def test_same_selector_for_title_and_caption_is_filled_once(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """有些平台标题与文案是同一个框；填两次会把标题冲掉。"""
        page = FakePage()
        pack = _pack(selectors={"caption_input": TITLE})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        await publisher.publish(_request(tmp_path, caption="正文"))
        assert page.title == "离谱跑酷地图"

    async def test_cover_is_uploaded(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpg")
        await publisher.publish(_request(tmp_path, cover_path=cover))
        assert ("#cover", cover) in page.uploaded
        assert "#cover-trigger" in page.clicks

    async def test_cover_failure_does_not_block_publishing(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """封面是**可选装饰**（与 T3.2 的水印同一条裁定）：它失败不该卡死一条能发的片子。"""
        page = FakePage(missing=frozenset({"#cover"}))
        publisher, _ = _publisher(tmp_paths, page)
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpg")
        assert (await publisher.publish(_request(tmp_path, cover_path=cover))).ok

    async def test_no_cover_means_no_cover_step(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        await publisher.publish(_request(tmp_path, cover_path=None))
        assert "#cover-trigger" not in page.clicks


# ── 第 ⑤步：回读比对 ─────────────────────────────────────────────────


class TestReadback:
    async def test_identical_readback_passes(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        assert (await _run(tmp_paths, tmp_path, FakePage())).ok

    async def test_mismatch_fails_after_three_attempts(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(title_readback="完全不是这个")
        result = await _run(tmp_paths, tmp_path, page)
        assert result.error_code == ErrorCode.PUBLISH_UPLOAD_FAILED
        assert str(READBACK_ATTEMPTS - 1) in (result.error_message or "")

    async def test_mismatch_reports_the_kind_of_swallowing(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """**消息里要说清是哪一种吞法** —— 操作员的下一步动作靠它定。"""
        page = FakePage(eat="emoji")
        result = await _run(tmp_paths, tmp_path, page, caption="点个关注 😀🔥")
        assert "emoji_stripped" in (result.error_message or "")

    async def test_emoji_swallowing_also_fails(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """吞 emoji **不放过**：平台不报错、接口返回 200，只有回读能发现。"""
        page = FakePage(eat="emoji")
        assert not (await _run(tmp_paths, tmp_path, page, caption="点个关注 😀")).ok

    async def test_newline_normalisation_also_fails(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """只差空白也**不放行**：真遇到时该改的是文案生成，不是判据（见模块注释）。"""
        page = FakePage(eat="newlines")
        result = await _run(tmp_paths, tmp_path, page, caption="第一行\n第二行")
        assert not result.ok
        assert "whitespace_only" in (result.error_message or "")

    async def test_caption_is_read_as_value_when_declared_textarea(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """``readback.caption: value`` ⇒ 走 ``input_value``（``<textarea>`` 的当前值）。"""
        page = FakePage(caption_is_textarea=True)
        pack = _pack(readback={"title": "value", "caption": "value"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        assert (await publisher.publish(_request(tmp_path))).ok

    async def test_wrong_readback_declaration_falls_back_instead_of_exploding(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """声明成 ``value`` 但元素其实是富文本 ⇒ 换 ``text`` 读，**不抛**。

        真机上"某平台把文案框从 ``<textarea>`` 改成了富文本"是会发生的（页面改版），
        而那时我们该做的是照常回读，不是让整条链路崩在 ``input_value`` 上。
        """
        page = FakePage(caption_is_textarea=False)
        pack = _pack(readback={"title": "value", "caption": "value"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        assert (await publisher.publish(_request(tmp_path))).ok

    async def test_empty_caption_is_not_read_back(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """空文案不回读：对着空框比"空 == 空"是白跑一趟。"""
        page = FakePage(caption_readback="编辑器里躺着别的字")
        assert (await _run(tmp_paths, tmp_path, page, caption="")).ok

    async def test_readback_failure_captures_evidence(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(title_readback="不对")
        result = await _run(tmp_paths, tmp_path, page)
        assert result.evidence is not None
        assert result.evidence.screenshot_path is not None
        assert result.evidence.dom_snapshot_path is not None
        assert result.evidence.selector_version == "test-1"


# ── 第 ⑥步：dry-run 绝不点发布 ───────────────────────────────────────


class TestDryRunStopsBeforePublish:
    async def test_publish_button_is_never_clicked(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """★ R14 的核心保护：演练**一次都不能点**那个按钮。"""
        page = FakePage()
        await _run(tmp_paths, tmp_path, page)
        assert PUBLISH not in page.clicks

    async def test_dry_run_reports_ok_with_queued_status(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """§06.5.3 第 ⑥ 步逐字：``ok=true, status='queued'``。"""
        result = await _run(tmp_paths, tmp_path, FakePage())
        assert result.ok and result.status == PublishStatus.QUEUED
        assert result.url is None and result.platform_post_id is None

    async def test_dry_run_takes_a_screenshot(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        result = await _run(tmp_paths, tmp_path, page)
        assert result.evidence is not None
        assert result.evidence.stage == "06-before-publish"
        assert result.evidence.screenshot_path is not None
        assert result.evidence.screenshot_path.is_file()

    async def test_evidence_lands_under_the_task_and_platform(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        result = await _run(tmp_paths, tmp_path, FakePage())
        path = result.evidence.screenshot_path
        assert path.parent == tmp_paths.publish_evidence_dir("01TASK", "douyin")

    async def test_real_publish_does_click(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """反面对照：``dry_run=False`` 时必须真的点下去 —— 否则上面那条测试可能只是"什么都没做"。"""
        page = FakePage()
        result = await _run(tmp_paths, tmp_path, page, dry_run=False)
        assert PUBLISH in page.clicks
        assert result.status == PublishStatus.PUBLISHED


# ── 第 ⑦⑧步：结果页 ─────────────────────────────────────────────────


class TestResult:
    async def test_success_returns_published_with_url(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(post_url="https://example.invalid/video/12345")
        result = await _run(tmp_paths, tmp_path, page, dry_run=False)
        assert result.ok and result.status == PublishStatus.PUBLISHED
        assert result.url == "https://example.invalid/video/12345"
        assert result.platform_post_id == "12345"
        assert result.published_at

    async def test_success_without_link_is_still_published(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """平台没给出作品链接时不该把一次成功的发布判成失败。"""
        result = await _run(tmp_paths, tmp_path, FakePage(), dry_run=False)
        assert result.ok and result.url is None

    async def test_review_rejection_is_its_own_error_code(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """§06.10：平台审核不通过 ⇒ ``PUBLISH_REVIEW_REJECTED`` + ``manual_required``。"""
        page = FakePage(result="reject")
        result = await _run(tmp_paths, tmp_path, page, dry_run=False)
        assert result.error_code == ErrorCode.PUBLISH_REVIEW_REJECTED
        assert result.status == PublishStatus.MANUAL_REQUIRED
        assert result.evidence is not None
        assert result.evidence.platform_text == "审核不通过"

    async def test_waiting_for_result_times_out(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(result="nothing")
        publisher, _ = _publisher(tmp_paths, page)
        publisher._publish_timeout_sec = 0.05
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_TIMEOUT


# ── 登录态探测 ───────────────────────────────────────────────────────


class TestHealth:
    async def test_logged_in(self, tmp_paths: StudioPaths) -> None:
        publisher, _ = _publisher(tmp_paths, FakePage())
        health = await publisher.health()
        assert health.ready and health.logged_in
        assert health.account_name == "测试账号"

    async def test_not_logged_in(self, tmp_paths: StudioPaths) -> None:
        publisher, _ = _publisher(tmp_paths, FakePage(logged_in=False))
        health = await publisher.health()
        assert not health.ready and not health.logged_in
        assert health.hint == "尚未登录，需人工扫码登录"

    async def test_expired_is_distinguished_from_never_logged_in(self, tmp_paths: StudioPaths) -> None:
        """§06.13 逐字要求"能正确报告'未登录'与'登录态已过期'"。

        两者都要人工扫码，但操作员的下一步动作不同（第一次用 vs 昨天还好好的）。
        """
        publisher, _ = _publisher(
            tmp_paths, FakePage(logged_in=False, login_page_text="登录已过期，请重新登录")
        )
        health = await publisher.health()
        assert health.hint == "登录态已过期，需人工重新扫码登录"

    async def test_login_required_wins_over_login_ok(self, tmp_paths: StudioPaths) -> None:
        """登录页上常常**同时**有"请扫码登录"和一个残留的头部占位元素。

        先问 ``login_ok`` 会把未登录判成已登录 —— 而那个错误直到上传完才暴露。
        """
        page = FakePage(logged_in=False)
        page._missing = frozenset()
        publisher, _ = _publisher(tmp_paths, page)
        assert not (await publisher.health()).logged_in

    async def test_probe_failure_is_reported_as_not_ready(self, tmp_paths: StudioPaths) -> None:
        """探不出来 ⇒ ``ready=False`` 而**不是**默认放行（放行会把问题推迟成上传失败）。"""

        class _Boom:
            async def __aenter__(self) -> Any:
                raise RuntimeError("browser exploded")

            async def __aexit__(self, *_: Any) -> None:
                return None

        publisher, _ = _publisher(tmp_paths, FakePage())
        publisher._session_factory = lambda _ctx: _Boom()
        health = await publisher.health()
        assert not health.ready and "探测失败" in (health.hint or "")

    async def test_health_uses_the_manage_url(self, tmp_paths: StudioPaths) -> None:
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        await publisher.health()
        assert page.visited == ["https://example.invalid/manage"]


# ── 其余约定 ─────────────────────────────────────────────────────────


class TestFetchMetrics:
    """数据回收（T5.4 · §06.6）：打管理页、在列表里找这一行、读四个计数。"""

    async def test_opens_the_manage_url_and_reads_the_four_counts(self, tmp_paths: StudioPaths) -> None:
        page = FakePage(
            metric_text={
                METRIC_VIEWS: "1.2万",
                METRIC_LIKES: "340",
                METRIC_COMMENTS: "12",
                METRIC_SHARES: "3",
            }
        )
        publisher, _ = _publisher(tmp_paths, page)
        metrics = await publisher.fetch_metrics("741")
        assert page.visited == ["https://example.invalid/manage"]
        assert (metrics.views, metrics.likes, metrics.comments, metrics.shares) == (
            12000,
            340,
            12,
            3,
        )
        assert metrics.collected_at.endswith("Z")

    async def test_reads_the_completion_rate_with_its_own_parser(self, tmp_paths: StudioPaths) -> None:
        """完播率走 ``parse_metric_ratio``，**不走** ``parse_metric_count``。

        走错解析器的后果不会报错：``42.3%`` 会被读成整数 ``42``，而 42 与 0.423
        都是"看着正常"的数 —— 这条用例就是钉住那个 100 倍。
        """
        page = FakePage(metric_text={METRIC_VIEWS: "1.2万", METRIC_COMPLETION: "42.3%"})
        publisher, _ = _publisher(tmp_paths, page)
        metrics = await publisher.fetch_metrics("741")
        assert metrics.completion_rate == 0.423
        assert metrics.views == 12000

    async def test_manage_url_placeholder_gets_the_post_id(self, tmp_paths: StudioPaths) -> None:
        """``urls.manage`` 里的 ``{post_id}`` 会被换成这一条的作品号（本地演练台靠它）。

        真平台的管理页是"列出全部作品"，不需要占位符；靶页没有后端，只能把"看哪一条"
        写在地址里。占位符要**转义**：作品号将来带 ``&`` 时不转义会多出一个查询参数。
        """
        pack = replace(_pack(), urls={"manage": "https://example.invalid/manage?post={post_id}"})
        page = FakePage(metric_text={METRIC_VIEWS: "7"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        await publisher.fetch_metrics("a&b")
        assert page.visited == ["https://example.invalid/manage?post=a%26b"]

    async def test_login_expired_is_reported_not_silently_empty(self, tmp_paths: StudioPaths) -> None:
        """登录态没了 ⇒ 抛 ``PUBLISH_LOGIN_EXPIRED``（轮询层据此顺延重试）。"""
        publisher, _ = _publisher(tmp_paths, FakePage(logged_in=False))
        with pytest.raises(PublishError) as info:
            await publisher.fetch_metrics("741")
        assert info.value.code == ErrorCode.PUBLISH_LOGIN_EXPIRED

    async def test_missing_row_is_a_selector_miss(self, tmp_paths: StudioPaths) -> None:
        page = FakePage(missing=frozenset({METRIC_ROW.replace("{post_id}", "741")}))
        publisher, _ = _publisher(tmp_paths, page)
        with pytest.raises(PublishError) as info:
            await publisher.fetch_metrics("741")
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS

    async def test_no_data_yet_is_not_a_failure(self, tmp_paths: StudioPaths) -> None:
        """四个数都是 ``—`` ⇒ 一条**有效读数**（全 ``None``），不是失败。

        刚发出去的作品就是这样；把它当失败会让这条记录连试三次之后**永久停止采集**，
        而它恰恰是最该在 T+1h/6h/24h 被回头看的那一条。
        """
        page = FakePage(
            metric_text=dict.fromkeys([METRIC_VIEWS, METRIC_LIKES, METRIC_COMMENTS, METRIC_SHARES], "—")
        )
        publisher, _ = _publisher(tmp_paths, page)
        metrics = await publisher.fetch_metrics("741")
        assert (metrics.views, metrics.likes, metrics.comments, metrics.shares) == (
            None,
            None,
            None,
            None,
        )

    async def test_pack_without_metric_row_says_so(self, tmp_paths: StudioPaths) -> None:
        pack = _pack(selectors={"metric_row": ""})
        publisher, _ = _publisher(tmp_paths, FakePage(), pack=pack)
        with pytest.raises(PublishError) as info:
            await publisher.fetch_metrics("741")
        assert info.value.code == ErrorCode.PUBLISH_NOT_IMPLEMENTED


class TestConventions:
    async def test_selectors_version_comes_from_the_pack_not_the_class(self, tmp_paths: StudioPaths) -> None:
        """版本住在 yaml 里（可热修）。写成 ``ClassVar`` 会得到一个**永远不变**的版本号。"""
        publisher, _ = _publisher(tmp_paths, FakePage(), pack=_pack())
        assert publisher.selectors_version == "test-1"

    async def test_session_is_closed_on_success(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage()
        publisher, session = _publisher(tmp_paths, page)
        await publisher.publish(_request(tmp_path))
        assert session.entered == 1 and session.exited == 1

    async def test_session_is_closed_on_failure(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        page = FakePage(logged_in=False)
        publisher, session = _publisher(tmp_paths, page)
        await publisher.publish(_request(tmp_path))
        assert session.exited == 1

    async def test_unexpected_exception_becomes_a_result_not_a_crash(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        class _BoomPage(FakePage):
            async def goto(self, url: str, **_: Any) -> Any:
                raise RuntimeError("net down")

        result = await _run(tmp_paths, tmp_path, _BoomPage())
        assert not result.ok
        # 没有分类的异常 ⇒ ``PUBLISH_UNKNOWN``（``FAILED`` 留给"知道坏在哪一步"的那些）。
        assert result.error_code == ErrorCode.PUBLISH_UNKNOWN

    async def test_publisher_without_platform_is_rejected(self, tmp_paths: StudioPaths) -> None:
        class _NoCode(PlaywrightPublisher):
            platform = ""

        with pytest.raises(PublishError) as info:
            _NoCode(_ctx(tmp_paths), pack=_pack(), session_factory=lambda _c: FakeSession(FakePage()))
        assert "platform" in info.value.message

    async def test_fixture_target_refuses_real_publish(self, tmp_paths: StudioPaths) -> None:
        """保护 ②：有人把靶页接去发**真平台** ⇒ 直接拒（T5.9 起仍然如此）。

        放行的后果是库里出现一条 ``platform='douyin'`` 而其实什么都没发出去的记录 ——
        那是"看着像真的假数据"，比报错糟得多。
        """
        publisher = FixturePublisher(_ctx(tmp_paths), session_factory=lambda _c: FakeSession(FakePage()))
        result = await publisher.publish(
            PublishRequest(
                task_id="01TASK",
                platform="douyin",
                account_id="acc",
                video_path=Path("x.mp4"),
                dry_run=False,
            )
        )
        assert result.error_code == ErrorCode.PUBLISH_NOT_IMPLEMENTED
        assert "演练台" in (result.error_message or "")

    async def test_rehearsal_platform_may_really_publish_to_the_local_page(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """演练台（``platform='other'``）**可以**真发布 —— 它打的是本地靶页。

        这一条是 T5.9 的全部意义所在：没有真账号时，"点下发布 → 作品号回到库里"
        这半条链路此前没有任何办法验。
        """
        page = FakePage(post_url="file:///rehearsal/rehearsal-1")
        # 用假包而不是真 ``selectors/fixture.yaml``：这一层验的是"流程放不放行"，
        # 真靶页的选择器由 `tests/integration/test_publish_dryrun.py` 对着真浏览器验。
        publisher = FixturePublisher(
            _ctx(tmp_paths), pack=_pack(), session_factory=lambda _c: FakeSession(page)
        )
        result = await publisher.publish(_request(tmp_path, platform="other", dry_run=False))
        assert result.ok is True
        assert result.status is PublishStatus.PUBLISHED
        assert result.platform_post_id == "rehearsal-1"
        assert PUBLISH in page.clicks  # 发布按钮**真的被点了**

    async def test_rehearsal_manage_url_carries_the_post_id_placeholder(self, tmp_paths: StudioPaths) -> None:
        """管理页地址带 ``{post_id}``：靶页没有后端，只能把"看哪一条"写在地址里。"""
        publisher = FixturePublisher(_ctx(tmp_paths), session_factory=lambda _c: FakeSession(FakePage()))
        manage = publisher._pack.urls["manage"]
        assert manage.startswith("file://")
        assert manage.endswith("post={post_id}")

    async def test_elapsed_ms_is_recorded(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        result = await _run(tmp_paths, tmp_path, FakePage())
        assert result.elapsed_ms >= 0


async def _run(
    tmp_paths: StudioPaths,
    tmp_path: Path,
    page: FakePage,
    *,
    dry_run: bool = True,
    caption: str = "点个关注",
    title: str = "离谱跑酷地图",
) -> Any:
    publisher, _ = _publisher(tmp_paths, page)
    return await publisher.publish(_request(tmp_path, dry_run=dry_run, caption=caption, title=title))
