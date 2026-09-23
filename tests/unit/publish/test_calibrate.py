"""逐平台校准探针（T5.14 · §06.2.2「实测校准」· R13 可热修）。

这条探针的全部价值在于**它给出的结论要能被照着做**。所以这里的用例大半是
"两个在读数上长得一模一样、而操作员的下一步动作完全相反"的情形必须被报成不同的东西：

- ``login_ok`` 在**从没登录过**的空 profile 上命中 ⇒ 没登录的号会被判成已登录
  （陷阱 #212 的另一半：只测已登录那一侧**测不出**它）；
- "选择器命中 0 个" 与 "选择器根本问不出来（语法错 / 引擎不支持）"—— 前者改选择器，
  后者改写法，合成一个 0 就把排障的第一步抹掉了；
- "创作页那几条选择器全错" 与 "这个号压根没登录" —— 后者会让人去改几条其实好好的
  选择器，所以这一轮只报 ``login_ok``，其余全部标成"这轮问不了"；
- 必选键 0 命中（**要修**）与可选键 0 命中（只是信息，比如封面弹层要点了才出现）。

外加一条 R13 / R14 的守卫：**探针不许点发布、不许填输入框**。它是只读的，唯一的
写操作是一张排障截图。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from studio.core.config import AccountConfig, PlatformConfig
from studio.core.paths import StudioPaths
from studio.publish.base import PublisherContext
from studio.publish.browser import BrowserSession, SessionFactory
from studio.publish.calibrate import (
    _WHERE_CREATE,
    _WHERE_LOGGED_OUT,
    _WHERE_MANAGE,
    CALIBRATE_PROFILE,
    EXPECT_ANY,
    EXPECT_HIT,
    EXPECT_MISS,
    CalibrateReport,
    calibrate_platform,
)
from studio.publish.selectors import POST_ID_PLACEHOLDER, SelectorPack, load_selector_pack

# ── 假页面 ────────────────────────────────────────────────────────────

LOGIN_OK = "#ok"
LOGIN_REQUIRED = "#login"
UPLOAD = "#video"
TITLE = "#title"
PUBLISH = "#publish"
SUCCESS = "#success"
VERIFY = "#verify"
METRIC_ROW = '[data-post-id="{post_id}"]'
METRIC_VIEWS = ".m-views"
METRIC_COMPLETION = ".m-completion"

UPLOAD_URL = "https://example.invalid/upload"
MANAGE_URL = "https://example.invalid/manage"


class FakePage:
    """按 ``query_selector_all`` 的语义演一个页面。

    与 ``test_playwright_publisher.FakePage`` 只差一处，而那一处正是这一层的核心：
    探针问的是**个数**（``query_selector_all``），不是"在不在"。所以这里直接给两张表
    —— ``选择器 → 个数`` 与 ``选择器 → 异常``。"问不出来"是**单独**一种读数
    （见 ``Probe.error``），不能让它悄悄变成"命中 0 个"。
    """

    def __init__(
        self,
        *,
        hits: dict[str, int] | None = None,
        errors: dict[str, Exception] | None = None,
        body: str = "",
        default: int = 0,
    ) -> None:
        self.hits = dict(hits or {})
        self.errors = dict(errors or {})
        self.body = body
        self.default = default
        self.visited: list[str] = []
        #: 探针**不许**点任何东西、**不许**填任何框、**不许**选文件（R13 / R14）。
        #: 三张表留给用例断言它们是空的 —— 见 ``TestReadOnly``。
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str]] = []
        self.screenshots: list[str] = []
        self.url = ""
        self.timeout = 0.0

    async def goto(self, url: str, **_: Any) -> Any:
        self.visited.append(url)
        self.url = url
        return None

    async def query_selector_all(self, selector: str) -> Any:
        if selector in self.errors:
            raise self.errors[selector]
        return [object()] * self.hits.get(selector, self.default)

    async def inner_text(self, selector: str) -> str:
        return self.body if selector == "body" else ""

    async def screenshot(self, *, path: Any, full_page: bool = False) -> bytes:
        target = Path(path)
        await asyncio.to_thread(_write_png, target)
        self.screenshots.append(str(target))
        return b"\x89PNG fake"

    # ── 下面这些是 ``PageLike`` 的其余成员 ───────────────────────────────
    #
    # 探针一条都不用它们，但**必须**有：``PageLike`` 是**结构化**协议，少一条方法 ⇒
    # ``FakeSession`` 不再是 ``BrowserSession`` ⇒ 整个文件里每一处 ``session_factory=``
    # 都会报一条与本层判据无关的 mypy 错。既然要给，就给**记动作**的实现（而不是
    # ``NotImplementedError``）：这样"探针有没有偷偷点一下发布"就变成一条能断言的
    # 读数（见 ``TestReadOnly``），而不是一句"代码里没写 click"。

    async def query_selector(self, selector: str) -> Any | None:
        return object() if await self.query_selector_all(selector) else None

    async def wait_for_selector(self, selector: str, **_: Any) -> Any:
        handle = await self.query_selector(selector)
        if handle is None:
            raise TimeoutError(selector)
        return handle

    async def set_input_files(self, selector: str, files: Any) -> None:
        self.uploads.append((selector, str(files)))

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def click(self, selector: str, **_: Any) -> None:
        self.clicks.append(selector)

    async def text_content(self, selector: str) -> str | None:
        return await self.inner_text(selector)

    async def input_value(self, selector: str) -> str:
        return ""

    async def get_attribute(self, selector: str, name: str) -> str | None:
        return None

    async def content(self) -> str:
        return "<html><body>fake</body></html>"

    async def evaluate(self, expression: str) -> Any:
        return False

    def set_default_timeout(self, timeout: float) -> None:
        self.timeout = timeout


def _write_png(target: Path) -> None:
    """``to_thread`` 里跑的那一步（真 Playwright 的 ``screenshot`` 也不阻塞事件循环）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG fake")


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.entered = 0

    async def __aenter__(self) -> FakePage:
        self.entered += 1
        return self.page

    async def __aexit__(self, *_: Any) -> None:
        return None


def _logged_out_page(**overrides: Any) -> FakePage:
    """一份**从没登录过**的空 profile 该有的读数：登录页上的标志元素在。"""
    hits = {LOGIN_REQUIRED: 1}
    hits.update(overrides.pop("hits", {}))
    return FakePage(hits=hits, **overrides)


def _logged_in_page(**overrides: Any) -> FakePage:
    """一个已登录账号的创作页：八步要用的那几条都命中。"""
    hits = {
        LOGIN_OK: 1,
        UPLOAD: 1,
        TITLE: 1,
        PUBLISH: 1,
        SUCCESS: 1,
        VERIFY: 1,
        METRIC_VIEWS: 1,
        METRIC_COMPLETION: 1,
    }
    hits.update(overrides.pop("hits", {}))
    return FakePage(hits=hits, **overrides)


def _factory(logged_out: FakePage, logged_in: FakePage) -> SessionFactory:
    """按**账号 id** 分派假页面 —— 探针的未登录那一侧用的是一份 ``_calibrate`` 空档。"""

    def make(ctx: PublisherContext) -> BrowserSession:
        return FakeSession(logged_out if ctx.account_id == CALIBRATE_PROFILE else logged_in)

    return make


# ── 假 pack ───────────────────────────────────────────────────────────

BASE_SELECTORS: dict[str, str] = {
    "login_ok": LOGIN_OK,
    "login_required": LOGIN_REQUIRED,
    "upload_input": UPLOAD,
    "title_input": TITLE,
    "publish_button": PUBLISH,
    "success_marker": SUCCESS,
    "verify_marker": VERIFY,
    "metric_row": METRIC_ROW,
    "metric_views": METRIC_VIEWS,
    "metric_completion_rate": METRIC_COMPLETION,
}


def _pack(tmp_path: Path, *, platform: str = "douyin", **overrides: Any) -> SelectorPack:
    """一份**结构上合法**的 pack（装配期那几条校验都过得去）。

    走 ``load_selector_pack`` 而不是直接 ``SelectorPack(...)``：探针拿到的 pack 在真机
    上就是这么来的，绕过装配期等于让"pack 本身不合法"这种情形在单测里不存在。

    ``overrides`` 覆盖顶层键（``selectors`` / ``markers`` / ``urls`` / ``known_gaps``），
    用来造"某一条留空"这类情形 —— 改的是那一层，不是在 yaml 文本上做字符串替换。
    """
    payload: dict[str, Any] = {
        "platform": platform,
        "version": "1.0",
        "urls": {"upload": UPLOAD_URL, "manage": MANAGE_URL},
        "selectors": dict(BASE_SELECTORS),
        "markers": {
            "login_expired_text": ["登录已过期"],
            "review_rejected_text": ["审核不通过"],
            "success_url_contains": ["/manage"],
        },
    }
    payload.update(overrides)
    path = tmp_path / f"{platform}.yaml"
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n"
    )
    return load_selector_pack(platform, root=tmp_path)


@pytest.fixture
def account(tmp_paths: StudioPaths) -> AccountConfig:
    return AccountConfig(
        account_id="acc_main",
        platform="douyin",
        profile_dir=tmp_paths.browser_profile_dir / "acc_main",
    )


@pytest.fixture
def platform_cfg() -> PlatformConfig:
    return PlatformConfig(publisher="douyin", profile="p1", enabled=True, title_max=55, caption_max=1000)


async def _run(
    pack: SelectorPack,
    *,
    account: AccountConfig,
    platform_cfg: PlatformConfig,
    paths: StudioPaths,
    logged_out: FakePage,
    logged_in: FakePage,
    check_logged_out: bool = True,
) -> CalibrateReport:
    """跑一轮探针（``marker_wait_sec=0``：假页面不需要等渲染）。"""
    return await calibrate_platform(
        pack=pack,
        account=account,
        platform_cfg=platform_cfg,
        paths=paths,
        session_factory=_factory(logged_out, logged_in),
        marker_wait_sec=0.0,
        check_logged_out=check_logged_out,
    )


def _at(report: CalibrateReport, where: str, key: str) -> Any:
    """取某一条读数（没有就是一条**说得出话**的失败，而不是 IndexError）。"""
    found = [probe for probe in report.probes if probe.where == where and probe.key == key]
    assert found, f"{where} 上没有 {key}：{[p.key for p in report.probes if p.where == where]}"
    return found[0]


# ── ① 未登录那一侧（陷阱 #212 的另一半）──────────────────────────────


class TestLoggedOutSide:
    async def test_a_clean_profile_reads_as_logged_out(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        side = [probe for probe in report.probes if probe.where == _WHERE_LOGGED_OUT]
        assert {probe.key for probe in side} == {"login_required", "login_ok", "login_expired_text"}
        assert all(probe.ok for probe in side)
        assert report.ok

    async def test_login_required_must_hit_on_the_login_page(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """认不出"在登录页"就只能报"不知道"，而人需要知道的是"去扫一次码"。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(hits={LOGIN_REQUIRED: 0}),
            logged_in=_logged_in_page(),
        )
        probe = _at(report, _WHERE_LOGGED_OUT, "login_required")
        assert probe.expect == EXPECT_HIT
        assert probe.verdict == "fix"
        assert not report.ok

    async def test_a_wide_login_ok_is_caught_on_the_empty_profile(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ 空 profile 上命中 ``login_ok`` ⇒ 没登录的号会被判成已登录。

        这正是"只测已登录那一侧"漏掉的一半：在**已登录**的页面上，一条写宽了的
        ``login_ok`` 照样命中，校准看着一切正常。
        """
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(hits={LOGIN_OK: 1}),
            logged_in=_logged_in_page(),
        )
        probe = _at(report, _WHERE_LOGGED_OUT, "login_ok")
        assert probe.expect == EXPECT_MISS
        assert probe.hits == 1
        assert probe.verdict == "fix"
        assert not report.ok

    async def test_login_expired_text_on_a_clean_profile_is_a_blocker(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ "登录已过期" 分不出"从没登录"与"被踢下线"，而两者的动作正好相反（#212）。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(body="登录已过期，请重新扫码"),
            logged_in=_logged_in_page(),
        )
        probe = _at(report, _WHERE_LOGGED_OUT, "login_expired_text")
        assert probe.expect == EXPECT_MISS
        assert probe.kind == "text"
        assert probe.hits == 1
        assert probe.verdict == "fix"

    async def test_the_logged_out_side_uses_an_isolated_profile(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """未登录那一侧必须跑在一份**空档** profile 上 —— 拿真账号去问等于自问自答。"""
        seen: list[str] = []

        def factory(ctx: PublisherContext) -> BrowserSession:
            seen.append(ctx.account_id)
            page = _logged_out_page() if ctx.account_id == CALIBRATE_PROFILE else _logged_in_page()
            return FakeSession(page)

        await calibrate_platform(
            pack=_pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            session_factory=factory,
            marker_wait_sec=0.0,
        )
        assert seen[0] == CALIBRATE_PROFILE
        assert seen[1] == "acc_main"


# ── ② 已登录的创作页 ──────────────────────────────────────────────────


class TestCreationPage:
    async def test_a_logged_in_account_reads_as_logged_in(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert report.logged_in
        assert report.ok
        assert _at(report, _WHERE_CREATE, "login_ok").expect == EXPECT_HIT

    async def test_login_required_also_matching_is_a_blocker(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ 已登录的页面上命中它 ⇒ ``_login_markers`` **先问它**，好好的号会被报成未登录。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(hits={LOGIN_REQUIRED: 1}),
        )
        probe = _at(report, _WHERE_CREATE, "login_required")
        assert probe.expect == EXPECT_MISS
        assert probe.verdict == "fix"
        assert not report.ok

    async def test_login_expired_text_on_a_logged_in_page_is_a_blocker(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(body="登录已过期"),
        )
        assert _at(report, _WHERE_CREATE, "login_expired_text").verdict == "fix"

    async def test_a_required_key_with_zero_hits_is_a_blocker(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """必选键 0 命中 ⇒ 八步走不下去，必须修（它是装配期就声明"必须有"的那几条）。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(hits={TITLE: 0}),
        )
        probe = _at(report, _WHERE_CREATE, "title_input")
        assert probe.expect == EXPECT_HIT
        assert probe.verdict == "fix"
        assert not report.ok

    async def test_an_optional_key_with_zero_hits_is_only_information(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """可选键 0 命中只是信息：封面弹层要点了才出现，验证框不是每个号都撞得上。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(hits={VERIFY: 0}),
        )
        probe = _at(report, _WHERE_CREATE, "verify_marker")
        assert probe.expect == EXPECT_ANY
        assert probe.hits == 0
        assert probe.verdict == "info"
        assert probe.ok
        assert report.ok

    async def test_a_selector_that_cannot_be_asked_is_not_a_zero(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ "问不出来"（语法错 / 引擎不支持）与"命中 0 个"要分开报：一个改写法，一个改选择器。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(errors={TITLE: RuntimeError("Unexpected token while parsing")}),
        )
        probe = _at(report, _WHERE_CREATE, "title_input")
        assert probe.error.startswith("RuntimeError")
        assert probe.hits == 0
        assert probe.verdict == "fix"

    async def test_an_empty_selector_is_skipped_instead_of_reported_as_zero(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ pack 里**故意留空**的那几条（``verify_marker`` 是典型）要进"跳过"，不是"命中 0"。

        留空是**声明**："这个平台上还没有这一条"。把它读成"选择器写错了"会让人去补一条
        他根本没见过的东西。
        """
        pack = _pack(tmp_path, selectors={**BASE_SELECTORS, "verify_marker": ""})
        report = await _run(
            pack,
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert "verify_marker" in {item.key for item in report.skipped}
        assert not any(probe.key == "verify_marker" for probe in report.probes)
        assert report.ok


# ── ③ 没登录 ⇒ 创作页那一轮立刻收手 ───────────────────────────────────


async def _not_logged_in_report(
    tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
) -> CalibrateReport:
    """这个号**没登录**（创作页上没有 ``login_ok``）时的一轮读数。"""
    return await _run(
        _pack(tmp_path),
        account=account,
        platform_cfg=platform_cfg,
        paths=tmp_paths,
        logged_out=_logged_out_page(),
        logged_in=_logged_in_page(hits={LOGIN_OK: 0}),
    )


class TestNotLoggedInStopsTheCreationRound:
    """★ 最要紧的一条：没登录时**不许**把创作页的一堆 0 读成"选择器全错"。

    创作页上的表单没渲染出来时，``upload_input`` / ``title_input`` / ``publish_button``
    全是"命中 0 个" —— 而那与"这三条选择器写错了"在读数上**一模一样**。照着改，改的是
    三条其实好好的选择器（改完下次还得改回来）。真相是"这个号还没扫码登录"，而它是一句
    能照着做的话。
    """

    async def test_every_other_creation_key_is_skipped(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _not_logged_in_report(tmp_paths, tmp_path, account, platform_cfg)
        assert not report.logged_in
        creation = [probe for probe in report.probes if probe.where == _WHERE_CREATE]
        assert {probe.key for probe in creation} == {"login_ok"}
        skipped = {item.key: item.reason for item in report.skipped}
        for key in ("upload_input", "title_input", "publish_button"):
            assert "没登录" in skipped[key], key

    async def test_the_manage_page_is_not_visited(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """没登录时管理页只会给出同样一堆 0 ⇒ 干脆不去。"""
        logged_in = _logged_in_page(hits={LOGIN_OK: 0})
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=logged_in,
        )
        assert logged_in.visited == [UPLOAD_URL]
        assert "metric_row" in {item.key for item in report.skipped}

    async def test_a_warning_points_at_the_scan_login_step(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _not_logged_in_report(tmp_paths, tmp_path, account, platform_cfg)
        assert any("扫码登录" in warning for warning in report.warnings)

    async def test_it_is_not_reported_as_all_green(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ 没登录时**绝不能**是一片绿。

        没有 blocker 时 CLI 会说"把 ``calibrated`` 改成 true" —— 而这一轮**什么都没验**
        （创作页那几条全被跳过了）。所以这里必须留下一条"要修"，把那个结论挡住。
        """
        report = await _not_logged_in_report(tmp_paths, tmp_path, account, platform_cfg)
        assert not report.ok
        assert [probe.key for probe in report.blockers] == ["login_ok"]
        assert report.blockers[0].where == _WHERE_CREATE


# ── ④ 管理页（数据回收那一组）────────────────────────────────────────


class TestManagePage:
    async def test_the_manage_url_is_visited_when_logged_in(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        logged_in = _logged_in_page()
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=logged_in,
        )
        assert logged_in.visited == [UPLOAD_URL, MANAGE_URL]
        assert _at(report, _WHERE_MANAGE, "login_ok").verdict == "ok"

    async def test_metric_row_is_skipped_with_the_post_id_reason(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """``metric_row`` 要一条**真作品号**才填得进去 ⇒ 只能等这个平台真发过一条（T5.4）。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        row = [item for item in report.skipped if item.key == "metric_row"]
        assert row and POST_ID_PLACEHOLDER in row[0].reason
        assert not any(probe.key == "metric_row" for probe in report.probes)

    async def test_success_url_contains_can_only_be_checked_by_a_real_publish(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """它判的是"点下发布之后地址跳走了" ⇒ 探针**不下结论**，如实说清楚。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert any(item.key.startswith("success_url_contains") for item in report.skipped)

    async def test_an_empty_metric_selector_is_skipped(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """抖音那份 pack 的 ``metric_completion_rate`` 就是故意留空的（真机上没验过）。"""
        pack = _pack(tmp_path, selectors={**BASE_SELECTORS, "metric_completion_rate": ""})
        report = await _run(
            pack,
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert "metric_completion_rate" in {item.key for item in report.skipped}
        assert not any(probe.key == "metric_completion_rate" for probe in report.probes)


# ── ⑤ 报告本身 ────────────────────────────────────────────────────────


class TestReport:
    async def test_switching_off_the_logged_out_side_says_so(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """``--no-logged-out`` 省下的正是最容易错的那一半 ⇒ 报告里要留一句。"""
        logged_out = _logged_out_page()
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=logged_out,
            logged_in=_logged_in_page(),
            check_logged_out=False,
        )
        assert logged_out.visited == []
        assert not any(probe.where == _WHERE_LOGGED_OUT for probe in report.probes)
        assert any("--no-logged-out" in warning for warning in report.warnings)

    async def test_the_screenshot_lands_on_disk(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """截图是排障时唯一能回看的东西（人是照着一张图去改 CSS 的）。"""
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert report.screenshot_path is not None
        assert report.screenshot_path.is_file()
        assert report.screenshot_path.parent == tmp_paths.work_dir / "calibrate" / "douyin"

    async def test_to_dict_shape(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        data = report.to_dict()
        assert set(data) == {
            "platform",
            "selectors_version",
            "calibrated",
            "calibrated_at",
            "known_gaps",
            "upload_url",
            "manage_url",
            "account_id",
            "logged_in",
            "ok",
            "blockers",
            "probes",
            "skipped",
            "warnings",
            "screenshot_path",
            "elapsed_ms",
        }
        assert data["ok"] is True
        assert data["blockers"] == []
        assert data["logged_in"] is True
        assert data["account_id"] == "acc_main"
        assert isinstance(data["elapsed_ms"], int)
        assert data["probes"] and data["probes"][0]["verdict"] in {"ok", "fix", "info"}

    async def test_blockers_are_listed_flat_and_inside_probes(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(hits={TITLE: 0}),
        )
        data = report.to_dict()
        assert data["ok"] is False
        assert [probe["key"] for probe in data["blockers"]] == ["title_input"]
        assert [probe.key for probe in report.blockers] == ["title_input"]

    async def test_known_gaps_travel_with_the_report(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """校准状态回答不了"这个平台还有没有一段流程压根没写"（B 站的必选分区就是）。"""
        pack = _pack(tmp_path, known_gaps=["B 站发布要选分区，这一段还没写"])
        report = await _run(
            pack,
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert report.known_gaps == ("B 站发布要选分区，这一段还没写",)
        assert report.to_dict()["known_gaps"] == ["B 站发布要选分区，这一段还没写"]

    async def test_an_uncalibrated_pack_says_so_in_the_warnings(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        report = await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=_logged_out_page(),
            logged_in=_logged_in_page(),
        )
        assert report.calibrated is False
        assert any("calibrated: false" in warning for warning in report.warnings)


# ── ⑥ 只读（R13 / R14）───────────────────────────────────────────────


class TestReadOnly:
    async def test_nothing_is_clicked_or_filled(
        self, tmp_paths: StudioPaths, tmp_path: Path, account: AccountConfig, platform_cfg: PlatformConfig
    ) -> None:
        """★ 探针**不许**点发布、不许填输入框。

        这条要是漏了，"校准"就成了"演练"，而演练再往前一步就是 R14 那条不可逆的发布
        （抖音真机上一次点下去就是一条真作品）。所以断言放在**假页面记录到的动作**上，
        而不是"代码里没有 click"这种读一遍就知道的话。
        """
        logged_out = _logged_out_page()
        logged_in = _logged_in_page()
        await _run(
            _pack(tmp_path),
            account=account,
            platform_cfg=platform_cfg,
            paths=tmp_paths,
            logged_out=logged_out,
            logged_in=logged_in,
        )
        for page in (logged_out, logged_in):
            assert page.clicks == []
            assert page.fills == []
            assert page.uploads == []
