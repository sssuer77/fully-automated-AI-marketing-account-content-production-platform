"""逐平台校准探针（T5.14 · §06.2.2「实测校准」· R13 可热修）。

它解决什么问题
--------------
``selectors/<platform>.yaml`` 里的 CSS 基本是**猜的**（七份里只有抖音那份在真机上
逐条验过）。"猜得对不对"只能到真页面上问 —— 而在有这条探针之前，唯一的问法是
**真发一条**：慢（一条 600s 超时）、不可逆（R14），而且失败信息里没有一行指向
"是哪条选择器错了"。

探针把"问一次真页面"与"发一条内容"拆开：

- 开一个**可见**窗口，打开平台的创作页；
- 把 pack 里**每一条**选择器在真页面上逐条跑一遍，报"命中几个 / 0 个 / 问不出来"；
- **未登录那一侧用一份空 profile 单独跑一遍**（``login_required`` 必须命中、
  ``login_ok`` 必须**不**命中 —— 陷阱 #212 就是这么翻的车）；
- 管理页也走一遍（数据回收那一组要在列表页上才问得了）。

它**只读**
----------
不点发布、不填任何输入框、不往平台上送任何内容（R13 / R14）。唯一的写操作是往
``data/work/calibrate/<platform>/`` 落一张截图 —— 那是排障用的。

为什么它住在 ``publish/`` 而不是 ``services/``
---------------------------------------------
它不需要数据库、不认识 ``publications`` 表、也不需要任务：输入是一份 pack + 一个
账号的登录态目录，输出是一张表。服务层那层缝（``publish_service``）在这里没有东西
可缝 —— 而多一层缝的代价是"探针用的选择器"与"发布用的选择器"有可能不是同一份。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import file_stamp
from studio.core.config import AccountConfig, PlatformConfig
from studio.core.paths import StudioPaths
from studio.publish.base import PublisherContext
from studio.publish.browser import (
    NAV_TIMEOUT_MS,
    BrowserSession,
    PageLike,
    SessionFactory,
    open_session,
)
from studio.publish.playwright_publisher import MARKER_WAIT_SEC, POLL_SEC
from studio.publish.selectors import (
    METRIC_SELECTOR_KEYS,
    OPTIONAL_SELECTORS,
    POST_ID_PLACEHOLDER,
    REQUIRED_SELECTORS,
    SelectorPack,
)

__all__ = [
    "CALIBRATE_PROFILE",
    "EXPECT_ANY",
    "EXPECT_HIT",
    "EXPECT_MISS",
    "CalibrateReport",
    "Probe",
    "Skipped",
    "calibrate_platform",
]

#: 一条选择器的**期望**。
#:
#: 为什么"期望"是探针的输入而不是"命中就算好"：同一个数字在不同键上意思相反 ——
#: ``login_required`` 命中 1 个是**对的**，而它在**已登录**的页面上命中 1 个是**错的**
#: （``_login_markers`` 先问它，于是那个号会被报成"未登录"）。没有期望，探针就只能
#: 报一堆数字让人自己去想。
EXPECT_HIT: Final[str] = "hit"
EXPECT_MISS: Final[str] = "miss"
#: 可选选择器：命中与否都正常，只报个数（比如封面弹层要点了才出现）。
EXPECT_ANY: Final[str] = "any"

_KIND_SELECTOR: Final[str] = "selector"
_KIND_TEXT: Final[str] = "text"

#: 探针自己用的**空档** profile 目录名（``data/browser_profile/_calibrate``）。
#:
#: 与真账号的登录态**物理隔离**（与 ``_fixture`` / ``_rehearsal`` 同一条理由）：
#: 未登录那一侧要的正是"一份从没登录过的 profile"，拿真账号去问等于自问自答。
#: ⚠️ 别在这个窗口里扫码 —— 扫了就再也测不了未登录那一侧了（删掉该目录即可复原）。
CALIBRATE_PROFILE: Final[str] = "_calibrate"

_WHERE_LOGGED_OUT: Final[str] = "未登录页"
_WHERE_CREATE: Final[str] = "创作页"
_WHERE_MANAGE: Final[str] = "管理页"


@dataclass(frozen=True, slots=True)
class Probe:
    """一条选择器（或一句文案）在真页面上的**读数**。"""

    key: str
    selector: str
    hits: int
    expect: str
    where: str
    kind: str = _KIND_SELECTOR
    #: 问不出来时的错因（选择器语法错 / 引擎不支持）。**与"命中 0 个"分开报**：
    #: 前者要改写法，后者要改选择器 —— 合成一个 0 就把排障的第一步抹掉了。
    error: str = ""
    note: str = ""

    @property
    def verdict(self) -> str:
        """``ok`` / ``fix`` / ``info``（人读的那一列）。"""
        if self.error:
            return "fix"
        if self.expect == EXPECT_HIT:
            return "ok" if self.hits else "fix"
        if self.expect == EXPECT_MISS:
            return "fix" if self.hits else "ok"
        return "info"

    @property
    def ok(self) -> bool:
        return self.verdict != "fix"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "selector": self.selector,
            "hits": self.hits,
            "expect": self.expect,
            "where": self.where,
            "kind": self.kind,
            "error": self.error,
            "note": self.note,
            "verdict": self.verdict,
        }


@dataclass(frozen=True, slots=True)
class Skipped:
    """这一条**这轮问不了**，以及为什么（而不是"没命中"）。"""

    key: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CalibrateReport:
    """一轮校准的结论（CLI 与 ``--json`` 共用同一份）。"""

    platform: str
    selectors_version: str
    calibrated: bool
    calibrated_at: str
    known_gaps: tuple[str, ...]
    upload_url: str
    manage_url: str
    account_id: str
    logged_in: bool
    probes: tuple[Probe, ...]
    skipped: tuple[Skipped, ...]
    warnings: tuple[str, ...]
    screenshot_path: Path | None
    elapsed_ms: int

    @property
    def blockers(self) -> tuple[Probe, ...]:
        """**必须修**的那几条（其余的都只是信息）。"""
        return tuple(probe for probe in self.probes if not probe.ok)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "selectors_version": self.selectors_version,
            "calibrated": self.calibrated,
            "calibrated_at": self.calibrated_at,
            "known_gaps": list(self.known_gaps),
            "upload_url": self.upload_url,
            "manage_url": self.manage_url,
            "account_id": self.account_id,
            "logged_in": self.logged_in,
            "ok": self.ok,
            "blockers": [probe.to_dict() for probe in self.blockers],
            "probes": [probe.to_dict() for probe in self.probes],
            "skipped": [item.to_dict() for item in self.skipped],
            "warnings": list(self.warnings),
            "screenshot_path": None if self.screenshot_path is None else self.screenshot_path.as_posix(),
            "elapsed_ms": self.elapsed_ms,
        }


# ── 小工具 ────────────────────────────────────────────────────────────


async def _count(page: PageLike, selector: str) -> tuple[int, str]:
    """这个选择器命中几个，取不到就给错因（见 :attr:`Probe.error`）。"""
    if not selector:
        return 0, ""
    try:
        return len(await page.query_selector_all(selector)), ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {str(exc)[:160]}"


async def _body_text(page: PageLike) -> str:
    """整页可见文本（拿不到 ⇒ 空串）。只用于认提示语，不做断言。"""
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _settle(page: PageLike, pack: SelectorPack, budget_sec: float) -> None:
    """等这个 SPA 渲染出**能判断的东西**。

    抖音真机教训（见 ``MARKER_WAIT_SEC``）：``goto(wait_until="domcontentloaded")``
    回来之后 **+0.1s 页面上什么都没有、+1.0s 才出现头像**。问一次就把"还没渲染完"
    读成了"元素不存在" —— 而那是**探针**最容易给出的一类假结论（人会照着它去改一条
    其实好好的选择器）。
    """
    deadline = time.monotonic() + max(budget_sec, 0.0)
    login_required = pack.selector("login_required")
    login_ok = pack.selector("login_ok")
    while True:
        if (await _count(page, login_required))[0] or (await _count(page, login_ok))[0]:
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(POLL_SEC)


class _Prober:
    """一轮探测的收集器（一个页面 + 两份清单）。"""

    def __init__(self, page: PageLike, *, where: str) -> None:
        self._page = page
        self._where = where
        self.probes: list[Probe] = []
        self.skipped: list[Skipped] = []

    async def selector(self, key: str, selector: str, *, expect: str, note: str = "") -> None:
        hits, error = await _count(self._page, selector)
        self.probes.append(
            Probe(
                key=key,
                selector=selector,
                hits=hits,
                expect=expect,
                where=self._where,
                error=error,
                note=note,
            )
        )

    def text(self, body: str, key: str, text: str, *, expect: str, note: str = "") -> None:
        self.probes.append(
            Probe(
                key=key,
                selector=text,
                hits=1 if text in body else 0,
                expect=expect,
                where=self._where,
                kind=_KIND_TEXT,
                note=note,
            )
        )

    def skip(self, key: str, reason: str) -> None:
        self.skipped.append(Skipped(key=key, reason=reason))


def _context(
    paths: StudioPaths,
    account: AccountConfig,
    platform_cfg: PlatformConfig,
    *,
    headless: bool,
) -> PublisherContext:
    return PublisherContext(paths=paths, account=account, platform=platform_cfg, headless=headless)


def _default_session(ctx: PublisherContext) -> BrowserSession:
    """与 ``PlaywrightPublisher`` 的默认会话**同一件事**（``open_session`` 那一行）。

    抄的是**这一行**，不是**那套流程**：探针不走 ``Publisher``（它不发布），而
    "打开一个带该账号登录态的浏览器"只有这一行 —— 为它去 import 发布器里那个
    **私有**的 ``_default_session`` 才是真的抄错地方。
    """
    return open_session(profile_dir=ctx.profile_dir, headless=ctx.headless)


async def _goto(page: PageLike, url: str) -> str:
    """打开一个页面；打不开就如实返回错因（探针不该因为一个地址整轮挂掉）。"""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return ""


async def _shot(page: PageLike, directory: Path, name: str) -> Path | None:
    """落一张截图（失败 ⇒ ``None``）。截图是排障用的，不该让整轮探针挂掉。"""
    try:
        # ASYNC240 在这里是误报：`mkdir` 是一次元数据系统调用（微秒级），而它一轮
        # 校准只发生一次 —— 为它开一个线程的开销比它本身大一个量级。同一个判断
        # 见 `playwright_publisher._capture`（那里做的是同一件事）。
        directory.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        path = directory / f"{file_stamp()}_{name}.png"
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        return None
    return path


# ── 三轮探测 ──────────────────────────────────────────────────────────


async def _probe_logged_out(
    pack: SelectorPack,
    account: AccountConfig,
    platform_cfg: PlatformConfig,
    paths: StudioPaths,
    factory: SessionFactory,
    *,
    headless: bool,
    marker_wait_sec: float,
) -> tuple[list[Probe], list[Skipped], list[str]]:
    """① 未登录那一侧：一份**空的** profile。

    为什么必须单独跑一遍（陷阱 #212 的另一半）：``login_ok`` 写宽了，在**已登录**的
    页面上照样命中 —— 于是校准看着一切正常，而错误只在"这个号还没登录"时才暴露，
    症状是"人扫完码它还说没扫到"（或者反过来：没登录被判成已登录，一路跑到上传完
    才以一句莫名其妙的失败收场）。只测已登录那一侧**测不出**这一半。
    """
    # ``model_copy`` 而不是 ``dataclasses.replace``：``AccountConfig`` 是 pydantic 模型，
    # ``replace`` 会直接抛 ``TypeError: replace() should be called on dataclass instances``
    # —— 而那会发生在**这一轮的第一个动作**上（探针一行都跑不了）。
    empty = _context(
        paths,
        account.model_copy(update={"account_id": CALIBRATE_PROFILE}),
        platform_cfg,
        headless=headless,
    )
    warnings: list[str] = []
    async with factory(empty) as page:
        prober = _Prober(page, where=_WHERE_LOGGED_OUT)
        error = await _goto(page, pack.url("upload"))
        if error:
            warnings.append(f"未登录侧打不开 {pack.url('upload')}：{error}")
        else:
            await _settle(page, pack, marker_wait_sec)
        await prober.selector(
            "login_required",
            pack.selector("login_required"),
            expect=EXPECT_HIT,
            note="未登录时**必须**命中它：认不出「在登录页」就只能报「不知道」，"
            "而人需要知道的是「去扫一次码」",
        )
        await prober.selector(
            "login_ok",
            pack.selector("login_ok"),
            expect=EXPECT_MISS,
            note="在一份**从没登录过**的空 profile 上命中它 ⇒ 没登录的号会被判成已登录",
        )
        body = await _body_text(page)
        for text in pack.texts("login_expired_text"):
            prober.text(
                body,
                "login_expired_text",
                text,
                expect=EXPECT_MISS,
                note="全新 profile 上不该出现这一句：出现了说明它分不出「从没登录」"
                "与「被踢下线」，而两者给的操作员动作正好相反（陷阱 #212）",
            )
    return prober.probes, prober.skipped, warnings


async def _probe_creation_page(page: PageLike, pack: SelectorPack) -> tuple[_Prober, bool]:
    """② 已登录的创作页：八步真正用到的那一批。回 ``(读数, 这个号登录了没有)``。

    ★ 没登录就**立刻收手**，这是这一轮最容易出的假结论
    ----------------------------------------------------
    创作页上的表单没渲染出来时，``upload_input`` / ``title_input`` / ``publish_button``
    全都是"命中 0 个" —— 而那与"这三条选择器写错了"在读数上**一模一样**。人于是会去
    改三条其实好好的选择器（改完下次还得改回来）。真相是"这个号还没扫码登录"，
    而它是一句**能照着做**的话。所以这一轮只报 ``login_ok`` 那一条，其余全部标成
    "这一轮问不了"。
    """
    prober = _Prober(page, where=_WHERE_CREATE)
    await prober.selector(
        "login_ok",
        pack.selector("login_ok"),
        expect=EXPECT_HIT,
        note="已登录时**必须**命中它（否则 health 会把一个好好的号报成「尚未登录」）；"
        "**没**命中时先看下面那句提示 —— 多半是这个号还没扫码登录，而不是这条选择器写错了",
    )
    logged_in = prober.probes[-1].hits > 0
    if not logged_in:
        for key in (*REQUIRED_SELECTORS, *OPTIONAL_SELECTORS):
            if key in ("login_ok", "login_required"):
                continue
            prober.skip(key, "账号没登录 ⇒ 创作页这一轮问不了（先在面板上扫码登录）")
        return prober, False
    await prober.selector(
        "login_required",
        pack.selector("login_required"),
        expect=EXPECT_MISS,
        note="已登录的页面上命中它 ⇒ `_login_markers` **先问它**，于是这个号会被报成未登录",
    )
    body = await _body_text(page)
    for text in pack.texts("login_expired_text"):
        prober.text(
            body,
            "login_expired_text",
            text,
            expect=EXPECT_MISS,
            note="已登录的页面上出现「登录已过期」⇒ 这一句分不出两种状态",
        )
    for key in REQUIRED_SELECTORS:
        if key in ("login_ok", "login_required"):
            continue
        await prober.selector(
            key, pack.selector(key), expect=EXPECT_HIT, note="八步要用到它（装配期的必选键）"
        )
    for key in OPTIONAL_SELECTORS:
        if key in METRIC_SELECTOR_KEYS:
            continue
        selector = pack.selectors.get(key, "")
        if not selector:
            prober.skip(key, "pack 里**故意留空**（见该文件里这一条的注释）")
            continue
        await prober.selector(
            key,
            selector,
            expect=EXPECT_ANY,
            note="可选：这一页上没有它是正常的（比如封面弹层要点了才出现）",
        )
    return prober, True


async def _probe_manage_page(page: PageLike, pack: SelectorPack, *, reachable: bool) -> _Prober:
    """③ 管理页：数据回收那一组。

    这一轮**只报个数，不下结论**：``metric_views`` 这类是**相对 ``metric_row``** 的
    后代选择器，单独在列表页上问它们本来就没有意义；而 ``metric_row`` 里的
    ``{post_id}`` 要一条**真作品号**才填得进去。所以真正验得了它们的时刻是
    "这个平台真发过一条之后" —— 与其在这里给一个看着像结论的数字，不如说清这件事。
    """
    prober = _Prober(page, where=_WHERE_MANAGE)
    if not reachable:
        for key in (*METRIC_SELECTOR_KEYS, "success_url_contains"):
            prober.skip(key, "账号没登录 ⇒ 管理页这一轮没跑（先在面板上扫码登录）")
        return prober
    await prober.selector(
        "login_ok",
        pack.selector("login_ok"),
        expect=EXPECT_HIT,
        note="管理页也要能认出登录态（数据回收走的是这个页面）",
    )
    prober.skip(
        "metric_row",
        f"含 {POST_ID_PLACEHOLDER} 占位符 ⇒ 要一条**真作品号**才填得进去（这个平台真发过一条之后再验）",
    )
    for key in METRIC_SELECTOR_KEYS:
        if key == "metric_row":
            continue
        selector = pack.selectors.get(key, "")
        if not selector:
            prober.skip(key, "pack 里**故意留空**（见该文件里这一条的注释）")
            continue
        await prober.selector(
            key,
            selector,
            expect=EXPECT_ANY,
            note="**相对 metric_row** 的后代选择器：这里的个数不是结论，真要验得等这个平台真发过一条（T5.4）",
        )
    for fragment in pack.texts("success_url_contains"):
        prober.skip(
            f"success_url_contains[{fragment}]",
            "判的是「点下发布之后地址跳走了」—— 只能在**真发一条**时验",
        )
    return prober


# ── 入口 ──────────────────────────────────────────────────────────────


async def calibrate_platform(
    *,
    pack: SelectorPack,
    account: AccountConfig,
    platform_cfg: PlatformConfig,
    paths: StudioPaths,
    headless: bool = False,
    session_factory: SessionFactory | None = None,
    marker_wait_sec: float = MARKER_WAIT_SEC,
    check_logged_out: bool = True,
) -> CalibrateReport:
    """把一份 pack 里的每条选择器在真页面上问一遍（**只读**，见模块注释）。

    ``headless=False``（默认）是刻意的：未登录/掉登录态时人得能看见那个窗口，
    才知道该去扫码还是该去查网络。``session_factory`` 是给单测注入假页面的缝
    （与 ``PlaywrightPublisher`` 同一条缝、同一个理由）。
    """
    started = time.monotonic()
    factory: SessionFactory = session_factory or _default_session
    probes: list[Probe] = []
    skipped: list[Skipped] = []
    warnings: list[str] = []
    screenshot: Path | None = None
    evidence_dir = paths.work_dir / "calibrate" / pack.platform

    if check_logged_out:
        logged_out_probes, logged_out_skipped, logged_out_warnings = await _probe_logged_out(
            pack,
            account,
            platform_cfg,
            paths,
            factory,
            headless=headless,
            marker_wait_sec=marker_wait_sec,
        )
        probes += logged_out_probes
        skipped += logged_out_skipped
        warnings += logged_out_warnings
    else:
        warnings.append("跳过了未登录那一侧（--no-logged-out）：`login_ok` 写宽了测不出来")

    ctx = _context(paths, account, platform_cfg, headless=headless)
    logged_in = False
    async with factory(ctx) as page:
        error = await _goto(page, pack.url("upload"))
        if error:
            warnings.append(f"创作页打不开 {pack.url('upload')}：{error}")
        else:
            await _settle(page, pack, marker_wait_sec)
        creation, logged_in = await _probe_creation_page(page, pack)
        probes += creation.probes
        skipped += creation.skipped
        screenshot = await _shot(page, evidence_dir, "create")

        manage_url = pack.urls.get("manage", "")
        # 没登录就不去管理页了：那一页同样只会给出"全是 0"的读数。
        if manage_url and logged_in:
            manage_error = await _goto(page, manage_url)
            if manage_error:
                warnings.append(f"管理页打不开 {manage_url}：{manage_error}")
            else:
                await _settle(page, pack, marker_wait_sec)
            manage = await _probe_manage_page(page, pack, reachable=True)
            probes += manage.probes
            skipped += manage.skipped
        elif manage_url:
            manage = await _probe_manage_page(page, pack, reachable=False)
            skipped += manage.skipped
        else:
            warnings.append("这份 pack 没有 urls.manage ⇒ 数据回收那一组这轮没验")

    if not logged_in:
        warnings.append(
            f"账号 {account.account_id} 的 profile 里**没有登录态** ⇒ 创作页那一轮问的是"
            "登录页。先在面板上点这个账号的「扫码登录」（T6.4），再跑一次本命令"
        )
    if not pack.calibrated:
        warnings.append(
            f"这份 pack 现在写着 calibrated: false（{pack.platform}.yaml）—— 校完记得"
            f"把 calibrated 改成 true、写上 calibrated_at，并把 version 改掉"
        )

    return CalibrateReport(
        platform=pack.platform,
        selectors_version=pack.version,
        calibrated=pack.calibrated,
        calibrated_at=pack.calibrated_at,
        known_gaps=pack.known_gaps,
        upload_url=pack.urls.get("upload", ""),
        manage_url=pack.urls.get("manage", ""),
        account_id=account.account_id,
        logged_in=logged_in,
        probes=tuple(probes),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
        screenshot_path=screenshot,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
