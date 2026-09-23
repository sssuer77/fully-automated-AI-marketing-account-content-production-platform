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
import inspect
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import AccountConfig, PlatformConfig
from studio.core.errors import ErrorCode, PublishError
from studio.core.paths import StudioPaths
from studio.publish import playwright_publisher
from studio.publish.base import (
    LOGIN_TIMEOUT_SEC,
    PublisherContext,
    PublishRequest,
    PublishStatus,
)
from studio.publish.platforms.fixture import FixturePublisher
from studio.publish.playwright_publisher import (
    READBACK_ATTEMPTS,
    TOLERATED_READBACK_REASONS,
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
VERIFY = "#verify"
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
        progress_delay_ticks: int = 0,
        progress_missing: bool = False,
        progress_text: str = "",
        redirect_to: str = "",
        result: str = "success",
        verify_clears_after: int | None = None,
        verify_leads_to_success: bool = True,
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
        #: 每一次 ``fill`` 的 ``(选择器, 填进去的值)``。回读失败会重填 ≤2 次，
        #: 所以"填了几次"是"有没有进重填循环"的直接证据。
        self.fills: list[tuple[str, str]] = []
        #: 关键动作的**先后**（"上传 → 看见进度 → 填字 → 点发布"）。
        #: 第 ③ 步跑在上传还没完的时候，是"结果对不对"看不出来的那类错 ——
        #: 只能靠顺序钉住（见 ``progress_delay_ticks``）。
        self.events: list[str] = []
        self.screenshots: list[str] = []
        self.evaluate_calls: list[str] = []
        self._logged_in = logged_in
        self._login_page_text = login_page_text
        self._eat = eat
        self._progress_ticks = progress_ticks
        #: 文件选上之后，进度条**隔几次轮询才出现**。真机上它不是立刻就有的：
        #: 抖音先给一屏"加载中，请稍候…"，约 1~2s 后表单与进度条才渲染出来
        #: （2026-09-23 真机实测）—— "看不到进度条"被读成"已经传完"正是那一坑。
        self._progress_delay_ticks = progress_delay_ticks
        #: 这个页面**根本没有**进度元素（有些平台不上报上传进度）。
        self._progress_missing = progress_missing
        #: 进度元素 ``text_content`` 的前缀。真机上进度条常常被裹在一个外层里，
        #: 于是读到的是一大段（"已上传：100.0MB/182.1MB … 25%"）而不是干净的 `25%`。
        self._progress_text = progress_text
        self._progress_rounds = 0
        self._uploading = False
        self._result = result
        #: 「人过掉验证」的模拟：验证框前 ``verify_clears_after`` 次轮询还在，之后消失
        #: ⇒ 成功页出现（真机上人输对码之后，平台自己把这次发布走完）。
        #: ``None`` ⇒ 框一直在（人没动手 / 输错了），用来验"等不到"那条。
        self._verify_clears_after = verify_clears_after
        self._verify_rounds = 0
        #: 框消失之后**平台是不是真的把这次发布走完了**。真机上大多数情况是
        #: （人输对码 ⇒ 出现成功页），但"人点了取消 / 输错三次退出"是同一现象、
        #: 不同结局 —— 而这两种结局正是要分开验的，所以它能单独关掉。
        self._verify_leads_to_success = verify_leads_to_success
        #: 人过掉验证之后置真 —— 成功判据看它（见 ``query_selector``）。
        self._human_passed_verify = False
        #: 点下发布之后页面会跳到哪儿（真机：抖音落到内容管理列表）。
        #: 空 ⇒ 停在原地（与"平台只是原地换个元素"那种改版一致）。
        self._redirect_to = redirect_to
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
        #: 当前地址（真 Playwright 的 ``page.url``）。第 ⑦ 步的"平台把页面跳走了"
        #: 那条判据读它（见 ``markers.success_url_contains``）。
        self.url = ""

    # ── 动作 ────────────────────────────────────────────────────────

    async def goto(self, url: str, **_: Any) -> Any:
        self.visited.append(url)
        self.url = url
        return None

    async def query_selector(self, selector: str) -> Any | None:
        if selector in self._missing:
            return None
        if selector == PROGRESS:
            # 上传面板的进度条：**选完文件之后**才可能出现在页面上，而且可能还要等
            # 一两拍（见 ``progress_delay_ticks``）。轮次在这里推进：``_wait_upload``
            # 每一轮先问在不在、再读文本。
            self._progress_rounds += 1
            if (
                self._progress_missing
                or not self._uploading
                or self._progress_rounds <= self._progress_delay_ticks
            ):
                return None
            self.events.append("progress:seen")
            return object()
        if selector == VERIFY and self._result == "verify":
            # 验证框**不**走下面那张查表：它有一条时间轴（人什么时候把码输对）。
            self._verify_rounds += 1
            if self._verify_clears_after is not None and self._verify_rounds > self._verify_clears_after:
                self._human_passed_verify = self._verify_leads_to_success
                return None
            return object()
        # 查表而不是 if 链：多一个"元素在不在"的判据时只加一行，不改控制流。
        present = {
            LOGIN_OK: self._logged_in,
            LOGIN_REQUIRED: not self._logged_in,
            SUCCESS: self._result == "success" or self._human_passed_verify,
            REJECT: self._result == "reject",
            VERIFY: self._result == "verify",
        }
        return object() if present.get(selector, True) else None

    async def wait_for_selector(self, selector: str, **_: Any) -> Any:
        handle = await self.query_selector(selector)
        if handle is None:
            raise TimeoutError(selector)
        return handle

    async def query_selector_all(self, selector: str) -> Any:
        """这个选择器命中几个。

        八步里**一处都不用**它 —— 它只服务于 ``publish calibrate`` 那条探针
        （校准要回答的第一个问题就是"这条猜出来的 CSS 在真页面上命中了几个"）。
        这里仍然给一条实现，是因为 ``PageLike`` 是**结构化**协议：少一条方法 ⇒
        ``FakeSession`` 不再是 ``BrowserSession`` ⇒ 本文件每一处 ``session_factory=``
        都过不了 mypy（而那是 10 条与本层判据无关的报错）。
        """
        return [] if selector in self._missing else [object()]

    async def set_input_files(self, selector: str, files: Any) -> None:
        if selector in self._missing:
            raise RuntimeError(f"no element: {selector}")
        self.uploaded.append((selector, files))
        self._uploading = True
        self.events.append("upload")

    async def fill(self, selector: str, value: str) -> None:
        if selector in self._missing:
            raise RuntimeError(f"no element: {selector}")
        self.fills.append((selector, value))
        self.events.append(f"fill:{selector}")
        if selector == TITLE:
            self.title = value
        elif selector == CAPTION:
            self.caption = self._swallow(value)

    async def click(self, selector: str, **_: Any) -> None:
        # 元素不在 ⇒ 真 Playwright 会一直等（等到超时抛错），这里照做：
        # "点一个不存在的元素"静默成功，会让"弹层关掉了"这类判据在单测里变成假的。
        if selector in self._missing:
            raise TimeoutError(f"click: {selector}")
        self.clicks.append(selector)
        self.events.append(f"click:{selector}")
        if selector == PUBLISH and self._redirect_to:
            self.url = self._redirect_to

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
            VERIFY: "接收短信验证码",
        }
        for suffix, text in self._metric_text.items():
            if selector.endswith(suffix):
                return text
        return values.get(selector, "")

    async def text_content(self, selector: str) -> str | None:
        if selector == PROGRESS:
            # 已经读到 100% 就不再重复报 —— 真页面上那个数字也会停住。
            if self._progress_ticks <= 0:
                return f"{self._progress_text}100%"
            self._progress_ticks -= 1
            percent = "100%" if self._progress_ticks == 0 else "42%"
            return f"{self._progress_text}{percent}"
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


class ScanningPage(FakePage):
    """扫完码之后才答"登上了"的页面（第 ``after`` 次被问才翻转）。

    真机上的顺序是：窗口里是登录页 ⇒ 人掏手机扫 ⇒ 平台把页面换掉。所以"翻转"
    发生在**问**的那一刻，而不是某个定时器上 —— 与真页面一致。
    """

    def __init__(self, *, after: int = 1, **kwargs: Any) -> None:
        super().__init__(logged_in=False, **kwargs)
        self._after = after
        self._asked = 0

    async def query_selector(self, selector: str) -> Any | None:
        if selector == LOGIN_REQUIRED:
            self._asked += 1
            if self._asked >= self._after:
                self._logged_in = True
        return await super().query_selector(selector)


class LateRenderPage(FakePage):
    """首屏**还没渲染完**的页面：前几次问"登录标志在不在"都答"不在"。

    真机实测（2026-09-23）：已登录的抖音创作中心在 ``domcontentloaded`` 之后 +0.1s
    两个标志都不在、+1.0s 才出现头像。只问一次就把这个页面读成了"没登录"。
    """

    def __init__(self, *, after: int = 1, **kwargs: Any) -> None:
        super().__init__(logged_in=False, **kwargs)
        self._after = after
        self._asked = 0
        self._rendered = False

    async def query_selector(self, selector: str) -> Any | None:
        if selector in (LOGIN_OK, LOGIN_REQUIRED) and not self._rendered:
            self._asked += 1
            if self._asked < self._after:
                return None
            self._rendered = True
            self._logged_in = True
        return await super().query_selector(selector)


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


def _ctx(tmp_paths: StudioPaths, *, await_manual_verify: bool = False) -> PublisherContext:
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
        await_manual_verify=await_manual_verify,
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
    progress_grace_sec: float = 0.0,
    marker_wait_sec: float = 0.0,
    await_manual_verify: bool = False,
    manual_verify_wait_sec: float = 0.0,
) -> tuple[PlaywrightPublisher, FakeSession]:
    session = FakeSession(page)

    class _Publisher(PlaywrightPublisher):
        platform = "douyin"

    publisher = _Publisher(
        _ctx(tmp_paths, await_manual_verify=await_manual_verify),
        pack=pack or _pack(),
        session_factory=lambda _ctx: session,
        settle_sec=settle_sec,
        progress_grace_sec=progress_grace_sec,
        marker_wait_sec=marker_wait_sec,
        manual_verify_wait_sec=manual_verify_wait_sec,
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

    async def test_upload_waits_for_a_progress_bar_that_appears_late(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """★ 真机坑（2026-09-23）：进度条**不是**选完文件就有的，而"看不到它"曾被读成"传完了"。

        症状：190MB 的成片还在 0%，第 ③ 步已经把标题文案填完、发布按钮也点下去了，
        平台回一句"你还有上次未发布的视频，是否继续编辑？"，整条发布卡在 600s 单元超时上。
        判据必须是"**先看见、再消失**（或走到 100%）"。
        """
        page = FakePage(progress_ticks=1, progress_delay_ticks=2)
        publisher, _ = _publisher(tmp_paths, page, progress_grace_sec=5.0)
        assert (await publisher.publish(_request(tmp_path))).ok

        order = page.events
        assert order.index("upload") < order.index("progress:seen")
        # 上传还没被看见之前，一个框都不许填（填了就等于在 0% 的页面上往下走）。
        first_fill = next(index for index, item in enumerate(order) if item.startswith("fill:"))
        assert order.index("progress:seen") < first_fill

    async def test_a_page_without_a_progress_bar_falls_back_to_a_settle(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """有些页面根本没有进度元素 ⇒ 宽限期一过就退回"等一拍"，**不是**死等到超时。"""
        page = FakePage(progress_missing=True)
        publisher, _ = _publisher(tmp_paths, page, progress_grace_sec=0.0)
        assert (await publisher.publish(_request(tmp_path))).ok
        assert "progress:seen" not in page.events

    async def test_a_byte_count_containing_100_is_not_read_as_done(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """进度元素外面裹一层时，文字里会出现"已上传 100.0MB/182.1MB"这种读数。

        在那一大段里找子串 ``100`` 会把 **100MB 的成片**在 25% 时判成"传完了" ——
        而"提前往下走"正是这块判据要防的事。要的是 ``100%``。
        """
        page = FakePage(progress_ticks=2, progress_text="已上传：100.0MB/182.1MB  25%")
        publisher, _ = _publisher(tmp_paths, page, progress_grace_sec=5.0)
        assert (await publisher.publish(_request(tmp_path))).ok
        # 读了两轮（25% → 100%）才放行，而不是第一轮就往下走。
        assert page.events.count("progress:seen") >= 2


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

    async def test_invisible_only_readback_is_tolerated(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """真机 2026-09-23：编辑器在文案末尾塞了一个零宽空格（U+200B）⇒ **照样发**。

        这不是"放宽判据"：文案一个字都没少，多出来的是编辑器自己的哨兵。判据本身
        仍然说"两边不逐字相同"（``matched`` 为假），是**调用方**决定放行 —— 两者的
        分工就是为这种情况留的。
        """
        page = FakePage(caption_readback="点个关注\u200b")
        result = await _run(tmp_paths, tmp_path, page)
        assert result.ok
        # 一次就够：放行的这一类**不进重填循环**（进了就是三次 ``fill``）。
        assert [selector for selector, _ in page.fills].count(CAPTION) == 1

    async def test_invisible_only_is_logged_at_info(
        self, tmp_paths: StudioPaths, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """放行要**留一句话**：这是全链路唯一一处"两边对不上、我们照样往下发"。

        info 而不是 debug：平台每次都会塞那个哨兵 ⇒ 这条日志每次都会命中，写成 debug
        等于默认级别下什么都没记（真机排查时正需要它）。
        """
        page = FakePage(caption_readback="点个关注\u200b")
        with caplog.at_level(logging.INFO, logger=playwright_publisher.__name__):
            assert (await _run(tmp_paths, tmp_path, page)).ok
        released = [record.getMessage() for record in caplog.records]
        assert any(
            "回读放行（只差不可见字符）" in line and "文案" in line and "U+200B" in line for line in released
        ), released

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

    async def test_a_blocking_overlay_is_dismissed_first(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """★ 真机坑（2026-09-23）：平台提示弹层的按钮压在发布按钮的命中区上。

        抖音第一次上传成片时会弹"视频预览功能 / 我知道了"，而 Playwright 的 ``click``
        会**等**元素能收到指针事件（默认 60s）—— 那一步会安静地耗掉一整分钟。
        弹层是装饰品，点掉它就好；点不掉也不该把这条片子卡死（同 ``cover_trigger``）。
        """
        page = FakePage()
        pack = _pack(selectors={"dismiss_overlay": "#dismiss"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        assert (await publisher.publish(_request(tmp_path, dry_run=False))).ok
        assert page.clicks.index("#dismiss") < page.clicks.index(PUBLISH)

    async def test_a_dismiss_that_fails_is_not_a_failure(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """没有弹层（大多数时候）⇒ ``click`` 超时，**照常往下走**。"""
        page = FakePage(missing=frozenset({"#dismiss"}))
        pack = _pack(selectors={"dismiss_overlay": "#dismiss"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        assert (await publisher.publish(_request(tmp_path, dry_run=False))).ok
        assert "#dismiss" not in page.clicks

    async def test_no_dismiss_selector_means_no_extra_click(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """没声明就一次多余的点击都不发（默认 pack 与靶页都没有这个键）。"""
        page = FakePage()
        publisher, _ = _publisher(tmp_paths, page)
        await publisher.publish(_request(tmp_path, dry_run=False))
        assert page.clicks == [PUBLISH]


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

    async def test_a_redirect_away_is_also_a_success(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23）：抖音**不发**"发布成功"，而是把页面跳到内容管理列表。

        只认元素的话，一条**已经发出去**的内容会在 600s 之后被记成失败 —— 而那时人
        已经在平台上看到它了。平台把页面跳走，是同一个事实的另一种说法（陷阱 #228）。
        """
        page = FakePage(result="nothing", redirect_to="https://example.invalid/manage")
        pack = _pack(markers={"success_url_contains": ["/manage"]})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.ok and result.status == PublishStatus.PUBLISHED
        assert result.evidence is not None and result.evidence.stage == "07-result"

    async def test_a_rejection_still_wins_over_the_redirect(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """审核不通过的判据**排在前面**：跳走的同时页面写着"审核不通过" ⇒ 那是驳回。"""
        page = FakePage(result="reject", redirect_to="https://example.invalid/manage")
        pack = _pack(markers={"success_url_contains": ["/manage"]})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_REVIEW_REJECTED

    async def test_no_url_marker_means_the_redirect_is_not_enough(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """没声明就不认（默认 pack 与靶页都没这个键）：判据是**平台自己选的**，不是默认。"""
        page = FakePage(result="nothing", redirect_to="https://example.invalid/manage")
        publisher, _ = _publisher(tmp_paths, page)
        publisher._publish_timeout_sec = 0.05
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_TIMEOUT

    async def test_a_verification_prompt_goes_to_a_human(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """★ 真机坑（2026-09-23）：点下发布之后平台弹「接收短信验证码」。

        这是**人的活**（R13：不替人过验证），但它是"自动这条路走完了、接下来等人"，
        不是失败 —— 所以转 ``manual_required`` 并说清要做什么。不认这一条的话，
        现场看到的是"卡在那里直到 600s 超时"，而屏幕上明明写着要做什么。
        """
        page = FakePage(result="verify")
        pack = _pack(selectors={"verify_marker": "#verify"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.status == PublishStatus.MANUAL_REQUIRED
        assert "短信验证" in (result.error_message or "")
        assert result.evidence is not None and result.evidence.screenshot_path is not None

    async def test_verification_loses_to_a_rejection(self, tmp_paths: StudioPaths, tmp_path: Path) -> None:
        """驳回的判据排在前面：页面同时写着"审核不通过" ⇒ 那是驳回，不是等人验证。"""
        page = FakePage(result="reject")
        pack = _pack(selectors={"verify_marker": "#verify"})
        publisher, _ = _publisher(tmp_paths, page, pack=pack)
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_REVIEW_REJECTED

    async def test_no_verify_selector_means_no_extra_probe(
        self, tmp_paths: StudioPaths, tmp_path: Path
    ) -> None:
        """没声明就不问（默认 pack 与靶页都没有这个键）—— 判据是**平台自己选的**。"""
        page = FakePage(result="success")
        publisher, _ = _publisher(tmp_paths, page)
        assert (await publisher.publish(_request(tmp_path, dry_run=False))).ok


class TestManualVerify:
    """窗口前面**有人**时的第 ⑦ 步（T6.4 · 真机 2026-09-23「人工过验证」）。

    与 :class:`TestResult` 里那条"弹验证框就转 manual_required"的差别**只有一个人**：
    ``ctx.await_manual_verify``。判据一条都没变 —— 变的只是"弹框"这件事的读法：
    没人时它是"自动流程到此为止"，有人时它是"轮到你了，我接着等"。

    这里要钉住三件事：
    - 人过掉验证 ⇒ 照旧由成功判据收场（**不是**因为"框没了"就算成功）；
    - 人没动手 ⇒ 超时文案得说"验证框一直在"，而不是含糊的"等不到结果页"；
    - 框没了但没看到结果页 ⇒ 文案要把人**先赶去平台上确认**，**不**引导他点重试
      （万一已经发出去了，重试就是第二条，R14 不可逆）。
    """

    async def test_a_human_passing_the_verification_lets_the_publish_finish(
        self, tmp_paths: StudioPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(playwright_publisher, "POLL_SEC", 0.0)
        page = FakePage(result="verify", verify_clears_after=2, post_url="https://example.invalid/post/1")
        pack = _pack(selectors={"verify_marker": VERIFY})
        publisher, _ = _publisher(
            tmp_paths,
            page,
            pack=pack,
            await_manual_verify=True,
            manual_verify_wait_sec=5.0,
        )
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.ok
        assert result.status == PublishStatus.PUBLISHED
        assert result.url == "https://example.invalid/post/1"
        # 人在窗口里动手这件事**看得见**：轮询真的问过那个框。
        assert page.clicks.count(PUBLISH) == 1

    async def test_a_verification_box_that_never_goes_away_times_out_saying_so(
        self, tmp_paths: StudioPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(playwright_publisher, "POLL_SEC", 0.0)
        page = FakePage(result="verify")
        pack = _pack(selectors={"verify_marker": VERIFY})
        publisher, _ = _publisher(
            tmp_paths,
            page,
            pack=pack,
            await_manual_verify=True,
            manual_verify_wait_sec=0.0,
        )
        # 两条上界都压到 0：这里验的是**文案**，不是真等 900 秒。
        publisher._publish_timeout_sec = 0.05
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_TIMEOUT
        assert result.evidence is not None
        tail = result.evidence.stderr_tail or ""
        assert "验证框一直在" in tail
        # 这一步之后窗口已经关了，所以下一句必须是"要接着发就再点一次"，
        # 而不是"点重试"（重试只会再弹一次同一个框）。
        assert "再点一次「人工过验证」" in tail

    async def test_a_vanished_box_but_no_result_page_sends_the_human_to_check_first(
        self, tmp_paths: StudioPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """框没了**不等于**发成功：人可能只是点了取消。这时最要紧的是"先去平台上看看"。"""
        monkeypatch.setattr(playwright_publisher, "POLL_SEC", 0.0)
        page = FakePage(result="verify", verify_clears_after=1, verify_leads_to_success=False)
        pack = _pack(selectors={"verify_marker": VERIFY})
        publisher, _ = _publisher(
            tmp_paths,
            page,
            pack=pack,
            await_manual_verify=True,
            manual_verify_wait_sec=0.0,
        )
        publisher._publish_timeout_sec = 0.05
        result = await publisher.publish(_request(tmp_path, dry_run=False))
        assert result.error_code == ErrorCode.PUBLISH_TIMEOUT
        assert result.evidence is not None
        tail = result.evidence.stderr_tail or ""
        assert "验证框已经没了" in tail
        # 万一它其实已经发出去了，引导他点「重试」就是引导他发第二条（R14 不可逆）。
        assert "确认这条到底发出去没有" in tail

    async def test_the_human_wait_only_raises_the_ceiling_never_lowers_it(
        self, tmp_paths: StudioPaths
    ) -> None:
        """``max`` 而不是"直接换掉"：调用方显式调长的超时不该被这一条又缩回去。"""
        publisher, _ = _publisher(tmp_paths, FakePage(), await_manual_verify=True)
        publisher._publish_timeout_sec = 5_000.0
        assert publisher._result_budget_sec() == 5_000.0

        plain, _ = _publisher(tmp_paths, FakePage())
        plain._publish_timeout_sec = 5_000.0
        assert plain._result_budget_sec() == 5_000.0


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

    async def test_waits_for_the_first_screen_to_render(
        self, tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 真机坑（2026-09-23）：``domcontentloaded`` 那一刻 SPA 还没渲染完。

        实测：已登录的创作中心在 +0.1s 两个标志都不在、+1.0s 才出现头像。只问一次
        就把一个**已经登录**的号报成"尚未登录" —— 人于是去重扫一个其实好好的号。
        """
        monkeypatch.setattr(playwright_publisher, "POLL_SEC", 0.0)
        page = LateRenderPage(after=2)
        publisher, _ = _publisher(tmp_paths, page, marker_wait_sec=5.0)

        health = await publisher.health()

        assert health.ready and health.logged_in
        assert page._asked >= 2

    async def test_markers_missing_is_unknown_not_logged_out(self, tmp_paths: StudioPaths) -> None:
        """两个标志都没出现 ⇒ **不知道**，不是"你没登录"。

        说成"没登录"会把人赶去扫码，而这句话唯一有资格说的是页面：它可能只是还没
        渲染完，也可能是平台改版了 —— 两种的下一步动作都不是"再扫一次"。
        """
        page = FakePage(missing=frozenset({LOGIN_OK, LOGIN_REQUIRED}))
        publisher, _ = _publisher(tmp_paths, page)

        health = await publisher.health()

        assert not health.ready and not health.logged_in
        assert "都没出现" in (health.hint or "")
        assert "尚未登录" not in (health.hint or "")

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


class TestLogin:
    """扫码登录（T6.4）：开**可见**窗口等人扫，扫完关掉、报结论。

    这一组验的是"等待期间**不做**什么"——它比"做了什么"更容易出错：
    重新导航会把码换掉、不关窗口会把 profile 占住。
    """

    async def test_waits_for_the_scan_then_reports_success(
        self, tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(playwright_publisher, "LOGIN_POLL_SEC", 0.0)
        page = ScanningPage(after=1)
        publisher, session = _publisher(tmp_paths, page)

        health = await publisher.login(timeout_sec=5)

        assert health.ready and health.logged_in
        assert health.account_name == "测试账号"
        # 窗口必须关掉：同一个账号的 profile **同时只能开一个浏览器**
        assert session.exited == 1

    async def test_polls_until_the_scan_lands(
        self, tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """人掏手机要时间 —— 第 3 次问才答"登上了"，照样要等到。"""
        monkeypatch.setattr(playwright_publisher, "LOGIN_POLL_SEC", 0.0)
        page = ScanningPage(after=3)
        publisher, _ = _publisher(tmp_paths, page)

        assert (await publisher.login(timeout_sec=5)).ready
        assert page._asked >= 3

    async def test_does_not_renavigate_while_waiting(
        self, tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """等扫码期间**只轮询、不重新导航**：登录页的码是一次性的。

        每重新导航一次就换一张码 ⇒ 用户扫的那张在他按下确认的那一刻已经作废，
        而症状是"我明明扫了，它说没扫到"—— 一句两边都自洽、没有任何地方会报错的谎话。
        """
        monkeypatch.setattr(playwright_publisher, "LOGIN_POLL_SEC", 0.0)
        page = ScanningPage(after=4)
        publisher, _ = _publisher(tmp_paths, page)

        await publisher.login(timeout_sec=5)

        assert page.visited == ["https://example.invalid/manage"]

    async def test_timeout_is_a_hint_not_an_error(
        self, tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """等不到人扫码 ⇒ **不抛**：返回一句照做的话（"再点一次"），窗口照样关掉。"""
        monkeypatch.setattr(playwright_publisher, "LOGIN_POLL_SEC", 0.0)
        publisher, session = _publisher(tmp_paths, FakePage(logged_in=False))

        health = await publisher.login(timeout_sec=0)

        assert not health.ready and not health.logged_in
        assert "没等到" in (health.hint or "")
        assert session.exited == 1

    def test_the_wait_has_a_shared_default(self) -> None:
        """默认等待上限来自 ``LOGIN_TIMEOUT_SEC``（CLI / REST / 面板说的是同一个数）。"""
        default = inspect.signature(PlaywrightPublisher.login).parameters["timeout_sec"].default
        assert default == LOGIN_TIMEOUT_SEC


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

    async def test_only_invisible_differences_are_tolerated(self) -> None:
        """放行名单**只有一项**，且必须只有一项。

        ``whitespace_only`` / ``emoji_stripped`` 代表"文案真的变了"（换行被吃、emoji 被
        吞），放行它们等于把第 ⑤ 步存在的理由取消掉。这条用例是那道闸：谁把这两个塞进
        名单，这里就红。
        """
        assert frozenset({"invisible_only"}) == TOLERATED_READBACK_REASONS

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
