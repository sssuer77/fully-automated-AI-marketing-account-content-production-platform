"""通用发布器（T5.2 · §06.5.3 八步 · §06.5.2 工程约束）。

八步逐条对应
------------
```text
① 打开平台创作页            _step_open      未登录 ⇒ PUBLISH_LOGIN_EXPIRED
② 上传视频（等上传完成）      _step_upload    超时 ⇒ PUBLISH_TIMEOUT
③ 填标题 / 文案 / 话题        _step_fill
④ 选封面（平台支持时）        _step_cover     失败**不阻塞**（封面是可选装饰）
⑤ 回读比对 ★                _step_readback  失败 ⇒ 重填 ≤2 ⇒ PUBLISH_UPLOAD_FAILED
⑥ 点发布                    _step_publish   dry_run ⇒ **不点**，截图后返回
⑦ 等结果页 / 取链接           _step_result    审核不通过 ⇒ PUBLISH_REVIEW_REJECTED
⑧ 落库                      服务层做（本文件不碰 DB）
```

第 ⑤ 步是这套东西里**唯一一条能证明"发出去的是我们写的"**的判据
--------------------------------------------------------------
平台富文本编辑器会吞 emoji / 换行 / 超长文本，而且是**静默**吞的（页面不报错、
接口返回 200）。没有回读，我们唯一的信号是"发完之后看平台上的成品" ——
那时已经发出去了，而 R14 的自动发布**不可逆**。

所以这里**不给"空白差异放行"开口子**：一旦存在"什么算空白差异"的定义，它就得
跟着每个平台变；真遇到"平台把换行归一了"时，该改的是**文案生成**（别写换行），
不是回读判据。失败时把 ``reason`` 原样报出来（``emoji_stripped`` / ``whitespace_only``…），
让操作员一眼知道是哪种吞法。

数据回收（第 ⑨ 步）不在八步里
------------------------------
:meth:`PlaywrightPublisher.fetch_metrics` 走的是**同一个** Publisher、同一份登录态，
但它不属于发布流程（§06.6 明写"不用队列"）：它是发布**之后**按 ``next_metric_at``
定时回头读一次数。放在这个类里，是因为"这个平台的页面怎么读"与"怎么发"是同一份
平台知识，拆到两个文件只会让选择器包被读两遍。

dry-run 停在哪
--------------
停在第 ⑥ 步**之前**（§06.5.3 第 ⑥ 步的括号里写的就是"在此停止 + 截图 + 返回
ok=true, status='queued'"）。所以：文件选中了、文案填了、回读比过了、截图留了，
**发布按钮一次都没被点**。本地靶页把这件事做成了可断言的事实（``window.__published``）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar, Final

from studio.core.clock import file_stamp, now_iso
from studio.core.errors import ErrorCode, PublishError
from studio.core.logging import get_logger
from studio.domain.publish import compare_readback
from studio.publish.base import (
    Publisher,
    PublisherContext,
    PublishEvidence,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
)
from studio.publish.browser import NAV_TIMEOUT_MS, PageLike, SessionFactory, open_session
from studio.publish.metrics import parse_metric_count
from studio.publish.selectors import METRIC_KEYS, POST_ID_PLACEHOLDER, SelectorPack, load_selector_pack

__all__ = [
    "POLL_SEC",
    "PUBLISH_TIMEOUT_SEC",
    "READBACK_ATTEMPTS",
    "UPLOAD_TIMEOUT_SEC",
    "PlaywrightPublisher",
]

logger = get_logger(__name__)

#: 回读比对的总尝试次数（1 次原样 + 2 次重填 · §06.5.3 第 ⑤ 步）。
READBACK_ATTEMPTS: Final[int] = 3
#: 轮询间隔。0.5s 是"人眼看着像实时"与"不打满 CPU"之间的折中。
POLL_SEC: Final[float] = 0.5
#: 上传超时（§06.5.2：上传 300s）。
UPLOAD_TIMEOUT_SEC: Final[float] = 300.0
#: 整体超时（§06.5.2：600s = job 租约）。超过它，job 会被别的 worker 抢走，
#: 我们继续跑只会让**两个**浏览器同时操作同一个账号。
PUBLISH_TIMEOUT_SEC: Final[float] = 600.0
#: 点开弹层这类"顺手试一下"的动作超时（失败了也无所谓）。
SHORT_TIMEOUT_MS: Final[float] = 5_000
#: 没有进度元素时，选完文件后干等这么久（见 _step_upload 的注释）。
UPLOAD_SETTLE_SEC: Final[float] = 3.0
#: ``evidence.stderr_tail`` 的截断长度（§4.6.1：截断 8KB）。
TAIL_LIMIT: Final[int] = 8 * 1024

#: 取证用的步骤名（直接编进文件名，排障时一眼看出停在哪一步）。
STAGE_OPEN: Final[str] = "01-open"
STAGE_UPLOAD: Final[str] = "02-upload"
STAGE_FILL: Final[str] = "03-fill"
STAGE_COVER: Final[str] = "04-cover"
STAGE_BEFORE_PUBLISH: Final[str] = "06-before-publish"
STAGE_RESULT: Final[str] = "07-result"
STAGE_FAILURE: Final[str] = "99-failure"


class PlaywrightPublisher(Publisher):
    """§06.5.3 八步的通用实现（**平台子类只给选择器与代号**）。"""

    platform: ClassVar[str] = ""

    def __init__(
        self,
        ctx: PublisherContext,
        *,
        pack: SelectorPack | None = None,
        session_factory: SessionFactory | None = None,
        settle_sec: float = UPLOAD_SETTLE_SEC,
        upload_timeout_sec: float = UPLOAD_TIMEOUT_SEC,
        publish_timeout_sec: float = PUBLISH_TIMEOUT_SEC,
    ) -> None:
        if not self.platform:
            raise PublishError(
                f"{type(self).__name__} 没有声明 platform",
                code=ErrorCode.PUBLISH_FAILED,
                remediation="子类加 platform: ClassVar[str] = '<平台代号>'（与 selectors/<x>.yaml 同名）",
            )
        super().__init__(ctx)
        self._pack = pack or load_selector_pack(self.platform)
        self._session_factory: SessionFactory = session_factory or _default_session
        self._settle_sec = settle_sec
        self._upload_timeout_sec = upload_timeout_sec
        self._publish_timeout_sec = publish_timeout_sec

    # ── 对外 ────────────────────────────────────────────────────────

    @property
    def selectors_version(self) -> str:
        """**实例属性**而不是 ``ClassVar``（与 §4.6.1 的写法有意不同）。

        版本住在 yaml 里（可热修）。写成 ``ClassVar`` 之后，"改了 yaml 但忘了改类属性"
        会得到一个**永远不变的版本号** —— 而 ``selectors_version`` 存在的全部意义
        就是事后能回答"这条是哪个版本的选择器发的"。读 yaml 才是那一份的真实读数。
        """
        return self._pack.version

    @property
    def pack(self) -> SelectorPack:
        return self._pack

    async def health(self) -> PublishHealth:
        url = self._pack.urls.get("manage") or self._pack.url("upload")
        try:
            async with self._session_factory(self._ctx) as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if not await self._logged_in(page):
                    return await self._not_logged_in(page)
                name = await self._optional_text(page, self._pack.selector("login_ok"))
                return PublishHealth(
                    ready=True,
                    logged_in=True,
                    last_check_at=now_iso(),
                    account_name=name or None,
                )
        except PublishError as exc:
            return PublishHealth.unknown(f"登录态探测失败：{exc.message}")
        except Exception as exc:
            return PublishHealth.unknown(f"登录态探测失败：{type(exc).__name__}")

    async def publish(self, req: PublishRequest) -> PublishResult:
        started = time.monotonic()
        try:
            async with self._session_factory(self._ctx) as page:
                return await self._run(page, req, started=started)
        except PublishError as exc:
            # 会话级失败（浏览器起不来 / profile 被占用）—— 此时没有页面可截图。
            return PublishResult.failure(exc.code, exc.message, elapsed_ms=_ms(started))
        except Exception as exc:
            # ``UNKNOWN`` 而不是 ``FAILED``：这一支接的是**没有分类**的异常，
            # 而 ``FAILED`` 留给"知道坏在哪一步"的那些（§4.6.1 的错误码表两者都有）。
            return PublishResult.failure(
                ErrorCode.PUBLISH_UNKNOWN,
                f"发布过程未预期的失败：{type(exc).__name__}: {str(exc)[:200]}",
                elapsed_ms=_ms(started),
            )

    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        """读一条作品的数据（T5.4 · §06.6）：打**管理页**，在列表里找这一条。

        为什么走管理页而不是作品详情页
        ----------------------------
        详情页每个平台都不一样（有的压根没有），而"列表里一行、四个数字"是四个平台
        共同的结构。选择器因此只需要一组：``metric_row``（怎么定位那一行）+
        四个 ``metric_*``（数字在哪）。

        读不出来**抛**而不是返回一堆 ``None``
        ------------------------------------
        这一层答不上来（登录态没了 / 那一行不在 / 选择器过期）与"平台说这条还没数据"
        是两件事：前者要顺延重试（§06.6 的失败处置），后者是一个**有效的读数**。
        把它们都压成 ``None``，"选择器失效"就会以"播放量一直是空"的形式安静地烂在库里。
        """
        url = self._pack.urls.get("manage") or self._pack.url("upload")
        async with self._session_factory(self._ctx) as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            if not await self._logged_in(page):
                health = await self._not_logged_in(page)
                raise PublishError(
                    f"登录态不过，采不到数据：{health.hint}",
                    code=ErrorCode.PUBLISH_LOGIN_EXPIRED,
                    context={"platform_post_id": platform_post_id, "platform": self.platform},
                    remediation="人工扫码登录之后再等下一次回收（**不自动登录** · R13）",
                )
            return await self._read_metrics(page, platform_post_id)

    async def _read_metrics(self, page: PageLike, platform_post_id: str) -> PublishMetrics:
        """在管理页上定位这一行并读四个计数（见 :meth:`fetch_metrics` 的两条边界）。"""
        template = self._pack.selectors.get("metric_row", "")
        if not template:
            raise PublishError(
                f"{self.platform} 的选择器包里没有 metric_row",
                code=ErrorCode.PUBLISH_NOT_IMPLEMENTED,
                context={"platform": self.platform, "version": self.selectors_version},
                remediation="在 selectors/<platform>.yaml 里补上数据回收那一组键（T5.4）",
            )
        row = template.replace(POST_ID_PLACEHOLDER, platform_post_id)
        if await page.query_selector(row) is None:
            raise PublishError(
                f"管理页上找不到作品 {platform_post_id}",
                code=ErrorCode.PUBLISH_SELECTOR_MISS,
                context={"platform": self.platform, "selector": row, "version": self.selectors_version},
                remediation=(
                    "两种可能，都要看一眼：① 这条作品被删了；② 选择器过期了"
                    "（见 docs/runbook/publish_selector.md）"
                ),
            )

        values: dict[str, int | None] = {}
        for key in METRIC_KEYS:
            selector = self._pack.selectors.get(f"metric_{key}", "")
            if not selector:
                values[key] = None
                continue
            # 后代组合器而不是"再查一次"：`text_content` 只收一个选择器，
            # 而"这一行里的那个数字"正好是 CSS 能表达的事。
            text = await self._optional_text(page, f"{row} {selector}")
            values[key] = parse_metric_count(text)

        if all(value is None for value in values.values()):
            # **不是失败**：刚发出去的作品四个数都可能是"—"（平台还没开始统计）。
            # 记一条 warn 是为了"四个选择器同时过期"时有人在日志里看得见 ——
            # 只把它当成功的话，那种坏法在面板上表现为"趋势图一直是空的"。
            logger.warning(
                "metrics.all_empty",
                platform=self.platform,
                platform_post_id=platform_post_id,
                selector_version=self.selectors_version,
            )
        return PublishMetrics(collected_at=now_iso(), **values)

    # ── 八步 ────────────────────────────────────────────────────────

    async def _run(self, page: PageLike, req: PublishRequest, *, started: float) -> PublishResult:
        try:
            await self._step_open(page, req)
            await self._step_upload(page, req)
            await self._step_fill(page, req)
            await self._step_cover(page, req)
            await self._step_readback(page, req)
            if req.dry_run:
                evidence = await self._capture(page, req, stage=STAGE_BEFORE_PUBLISH)
                logger.info("dry-run 停在第 ⑥ 步之前（未点发布）", extra={"task_id": req.task_id})
                return PublishResult.stopped_before_publish(evidence=evidence, elapsed_ms=_ms(started))
            await self._step_publish(page, req)
            return await self._step_result(page, req, started=started)
        except PublishError as exc:
            evidence = await self._capture_failure(page, req, exc)
            return PublishResult.failure(exc.code, exc.message, evidence=evidence, elapsed_ms=_ms(started))

    async def _step_open(self, page: PageLike, req: PublishRequest) -> None:
        await page.goto(self._pack.url("upload"), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if not await self._logged_in(page):
            # 复用 health 的那句判据（"尚未登录" / "登录态已过期"）：**同一个事实**
            # 在两个入口上必须说同一句话，否则操作员会以为是两个不同的问题。
            health = await self._not_logged_in(page)
            raise PublishError(
                f"打开创作页后发现未登录：{health.hint}",
                code=ErrorCode.PUBLISH_LOGIN_EXPIRED,
                context={"url": self._pack.url("upload"), "account_id": req.account_id},
                remediation=f"人工扫码登录一次（profile：{self._ctx.profile_dir}）后重试",
            )
        await self._capture(page, req, stage=STAGE_OPEN)

    async def _step_upload(self, page: PageLike, req: PublishRequest) -> None:
        if not req.video_path.is_file():
            raise PublishError(
                f"成片不存在：{req.video_path}",
                code=ErrorCode.PUBLISH_UPLOAD_FAILED,
                context={"path": req.video_path.as_posix()},
                remediation="先在渲染面板确认成片还在（可能被清理策略回收了）",
            )
        await page.set_input_files(self._pack.selector("upload_input"), req.video_path)
        await self._wait_upload(page, req)
        await self._capture(page, req, stage=STAGE_UPLOAD)

    async def _step_fill(self, page: PageLike, req: PublishRequest) -> None:
        title_selector = self._pack.selector("title_input")
        await page.fill(title_selector, req.title)
        caption_selector = self._pack.selectors.get("caption_input", "")
        # 有些平台标题与文案是**同一个框**（选择器写成一样）。填两次会把标题冲掉，
        # 所以这里只填一次 —— 而"要不要合并"是配置问题，不是代码问题。
        if caption_selector and caption_selector != title_selector and req.caption:
            await page.fill(caption_selector, req.caption)
        await self._capture(page, req, stage=STAGE_FILL)

    async def _step_cover(self, page: PageLike, req: PublishRequest) -> None:
        if req.cover_path is None:
            return
        input_selector = self._pack.selectors.get("cover_input", "")
        if not input_selector:
            return
        trigger = self._pack.selectors.get("cover_trigger", "")
        if trigger:
            try:
                await page.click(trigger, timeout=SHORT_TIMEOUT_MS)
            except Exception:
                logger.info("封面弹层没打开，直接给隐藏的文件框喂图", extra={"task_id": req.task_id})
        try:
            await page.set_input_files(input_selector, req.cover_path)
        except Exception as exc:
            # 与 T3.2 的水印同一条裁定：装饰品不值得把一条能发的片子卡死在这里。
            logger.warning(
                "封面没能选上，继续发布",
                extra={"task_id": req.task_id, "error": type(exc).__name__},
            )
            return
        await self._capture(page, req, stage=STAGE_COVER)

    async def _step_readback(self, page: PageLike, req: PublishRequest) -> None:
        title_selector = self._pack.selector("title_input")
        caption_selector = self._pack.selectors.get("caption_input", "")
        problems: list[str] = []
        for attempt in range(READBACK_ATTEMPTS):
            problems = []
            title = await self._read_field(page, title_selector, self._pack.readback_kind("title"))
            diff = compare_readback(req.title, title)
            if not diff.matched:
                problems.append(f"标题：{diff.detail}")
            if caption_selector and req.caption:
                caption = await self._read_field(page, caption_selector, self._pack.readback_kind("caption"))
                caption_diff = compare_readback(req.caption, caption)
                if not caption_diff.matched:
                    problems.append(f"文案：{caption_diff.detail}")
            if not problems:
                return
            if attempt < READBACK_ATTEMPTS - 1:
                logger.warning(
                    "回读不一致，重填后重试",
                    extra={"task_id": req.task_id, "attempt": attempt + 1, "problems": problems},
                )
                await self._step_fill(page, req)
        # 把"哪一种吞法"写进 **message** 而不只是 context：操作员在 CLI 上看到的是
        # message（context 要 --json 才看得到），而 `emoji_stripped` 与 `mismatch`
        # 的下一步动作完全不同（去掉 emoji 重发 vs 去查选择器是不是点错了框）。
        raise PublishError(
            "回读比对连续失败（已重填 2 次）：" + "；".join(problems),
            code=ErrorCode.PUBLISH_UPLOAD_FAILED,
            context={"task_id": req.task_id, "problems": problems, "attempts": READBACK_ATTEMPTS},
            remediation="多半是平台编辑器吞字：按上面说的那种吞法改文案（去掉 emoji / 换行）再发",
        )

    async def _step_publish(self, page: PageLike, req: PublishRequest) -> None:
        await page.click(self._pack.selector("publish_button"))

    async def _step_result(self, page: PageLike, req: PublishRequest, *, started: float) -> PublishResult:
        deadline = time.monotonic() + self._publish_timeout_sec
        reject_selector = self._pack.selectors.get("reject_marker", "")
        success_selector = self._pack.selectors.get("success_marker", "")
        while time.monotonic() < deadline:
            if reject_selector and await self._present(page, reject_selector):
                text = await self._optional_text(page, reject_selector)
                raise PublishError(
                    f"平台审核不通过：{text or '（页面未给出文案）'}",
                    code=ErrorCode.PUBLISH_REVIEW_REJECTED,
                    context={"task_id": req.task_id, "platform_text": text},
                    remediation="人工看平台给的驳回理由；必要时回写稿阶段改稿",
                )
            if success_selector and await self._present(page, success_selector):
                url, post_id = await self._read_post_link(page)
                evidence = await self._capture(page, req, stage=STAGE_RESULT)
                return PublishResult.published(
                    url=url,
                    platform_post_id=post_id,
                    evidence=evidence,
                    elapsed_ms=_ms(started),
                )
            await asyncio.sleep(POLL_SEC)
        raise PublishError(
            "点下发布后等不到结果页",
            code=ErrorCode.PUBLISH_TIMEOUT,
            context={"task_id": req.task_id, "timeout_sec": self._publish_timeout_sec},
            remediation="看 evidence 里的截图：可能停在验证码 / 二次确认 / 平台改版",
        )

    # ── 小工具 ──────────────────────────────────────────────────────

    async def _wait_upload(self, page: PageLike, req: PublishRequest) -> None:
        """等上传完成。

        有进度元素就轮询它（消失或出现 100% 都算完成）；**没有就干等一拍**：
        选完文件那一刻，平台的上传还没开始，立刻走第 ③ 步会对着一个"还在转圈"的
        页面填标题 —— 填得进去，但随后的回读会读到空（编辑器要等上传完才启用）。
        干等不好，但比"猜一个不存在的元素"好：至少它是**一个明确的、可调的**假设。
        """
        progress_selector = self._pack.selectors.get("upload_progress", "")
        if not progress_selector:
            await asyncio.sleep(self._settle_sec)
            return
        deadline = time.monotonic() + self._upload_timeout_sec
        while time.monotonic() < deadline:
            handle = await page.query_selector(progress_selector)
            if handle is None:
                return
            text = await page.text_content(progress_selector) or ""
            if "100" in text:
                return
            await asyncio.sleep(POLL_SEC)
        raise PublishError(
            "等待上传完成超时",
            code=ErrorCode.PUBLISH_TIMEOUT,
            context={"task_id": req.task_id, "timeout_sec": self._upload_timeout_sec},
            remediation="看网络与文件大小；大文件可以把 publish 池的租约调长（§03.4.4）",
        )

    async def _not_logged_in(self, page: PageLike) -> PublishHealth:
        """未登录时给一句**能照做的**提示（§06.10）。

        为什么要区分"从未登录"与"登录态已过期"：两者都要人工扫码，但**操作员的动作
        不一样** —— 前者是"第一次用这个账号"，后者是"昨天还好好的，今天掉了"
        （后者往往意味着 profile 被清、或者平台踢了下线，值得多看一眼）。
        判据是页面上的文案（``markers.login_expired_text``），不是我们的推测。
        """
        body = await self._page_text(page)
        expired = any(marker in body for marker in self._pack.texts("login_expired_text"))
        return PublishHealth(
            ready=False,
            logged_in=False,
            last_check_at=now_iso(),
            hint="登录态已过期，需人工重新扫码登录" if expired else "尚未登录，需人工扫码登录",
        )

    async def _page_text(self, page: PageLike) -> str:
        """整页可见文本（拿不到 ⇒ 空串）。**只用于认提示语**，不做断言。"""
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def _logged_in(self, page: PageLike) -> bool:
        """先问"未登录标志在不在"，再问"已登录标志在不在"。

        顺序不能反：登录页上通常**同时**有"请扫码登录"和某个残留的头部占位元素，
        先问后者会把一个未登录的页面判成已登录 —— 而那个错误直到上传完才暴露。
        """
        if await self._present(page, self._pack.selector("login_required")):
            return False
        return await self._present(page, self._pack.selector("login_ok"))

    async def _present(self, page: PageLike, selector: str) -> bool:
        if not selector:
            return False
        try:
            return await page.query_selector(selector) is not None
        except Exception:
            return False

    async def _optional_text(self, page: PageLike, selector: str) -> str:
        try:
            return (await page.inner_text(selector)).strip()
        except Exception:
            return ""

    async def _read_field(self, page: PageLike, selector: str, kind: str) -> str:
        """按 pack 声明的方式读一个字段，**读不动就换另一种**。

        换法不是"多写几行保险"：``<textarea>`` 上读 ``inner_text`` 得到的是**初始内容**
        （一个安静的错值，会让回读永远不一致），``contenteditable`` 上读 ``input_value``
        会直接抛。两种错法都不该由运维在真机上撞出来。
        """
        if kind == "value":
            try:
                return await page.input_value(selector)
            except Exception:
                return await self._optional_text(page, selector)
        text = await self._optional_text(page, selector)
        if text:
            return text
        try:
            return await page.input_value(selector)
        except Exception:
            return text

    async def _read_post_link(self, page: PageLike) -> tuple[str | None, str | None]:
        selector = self._pack.selectors.get("post_link", "")
        if not selector:
            return None, None
        try:
            href = await page.get_attribute(selector, "href")
        except Exception:
            return None, None
        if not href:
            return None, None
        return href, href.rstrip("/").rsplit("/", 1)[-1] or None

    async def _capture(self, page: PageLike, req: PublishRequest, *, stage: str) -> PublishEvidence:
        """关键节点截图（§06.5.2「每步关键节点截图」）。

        取证失败**不抛**：截图是"事后排障"用的，而它失败的时刻恰恰是流程本身
        已经出问题的时刻 —— 那时候把异常抛出去，等于用"截图失败"盖住真正的失败。
        """
        directory = self._ctx.paths.publish_evidence_dir(req.task_id, self.platform)
        stamp = file_stamp()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            shot = directory / f"{stamp}_{stage}.png"
            await page.screenshot(path=str(shot), full_page=True)
        except Exception:
            return PublishEvidence(selector_version=self.selectors_version, stage=stage)
        return PublishEvidence(
            screenshot_path=shot,
            selector_version=self.selectors_version,
            stage=stage,
        )

    async def _capture_failure(
        self, page: PageLike, req: PublishRequest, exc: PublishError
    ) -> PublishEvidence:
        """失败取证 = 截图 + DOM 快照 + 异常尾巴（§06.10「截图 + DOM 快照」）。"""
        base = await self._capture(page, req, stage=STAGE_FAILURE)
        directory = self._ctx.paths.publish_evidence_dir(req.task_id, self.platform)
        dom: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            dom = directory / f"{file_stamp()}_{STAGE_FAILURE}.html"
            dom.write_text(await page.content(), encoding="utf-8", newline="")
        except Exception:
            dom = None
        return PublishEvidence(
            screenshot_path=base.screenshot_path,
            dom_snapshot_path=dom,
            stderr_tail=_tail(exc),
            selector_version=self.selectors_version,
            platform_text=str(exc.context.get("platform_text") or "") or None,
            stage=STAGE_FAILURE,
        )


def _default_session(ctx: PublisherContext) -> Any:
    return open_session(profile_dir=ctx.profile_dir, headless=ctx.headless)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _tail(exc: PublishError) -> str:
    text = f"[{exc.code}] {exc.message}"
    if exc.remediation:
        text = f"{text}\n修复：{exc.remediation}"
    return text[:TAIL_LIMIT]
