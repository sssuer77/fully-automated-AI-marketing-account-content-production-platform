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
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import quote

from studio.core.clock import file_stamp, now_iso
from studio.core.errors import ErrorCode, PublishError
from studio.core.logging import get_logger
from studio.domain.publish import ReadbackDiff, compare_readback
from studio.publish.base import (
    LOGIN_TIMEOUT_SEC,
    Publisher,
    PublisherContext,
    PublishEvidence,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
)
from studio.publish.browser import NAV_TIMEOUT_MS, PageLike, SessionFactory, open_session
from studio.publish.metrics import parse_metric_count, parse_metric_ratio
from studio.publish.selectors import (
    METRIC_KEYS,
    POST_ID_PLACEHOLDER,
    RATIO_METRIC_KEYS,
    SelectorPack,
    load_selector_pack,
)

__all__ = [
    "LOGIN_POLL_SEC",
    "MARKER_WAIT_SEC",
    "POLL_SEC",
    "PROGRESS_GRACE_SEC",
    "PUBLISH_TIMEOUT_SEC",
    "READBACK_ATTEMPTS",
    "TOLERATED_READBACK_REASONS",
    "UPLOAD_TIMEOUT_SEC",
    "PlaywrightPublisher",
]

logger = get_logger(__name__)

#: 回读比对的总尝试次数（1 次原样 + 2 次重填 · §06.5.3 第 ⑤ 步）。
READBACK_ATTEMPTS: Final[int] = 3
#: 回读差异里**可以放行**的那几种（§06.5.3 第 ⑤ 步 · 真机 2026-09-23）。
#:
#: 只有 ``invisible_only``：抖音的富文本编辑器会在文案末尾塞一个零宽空格（U+200B），
#: 而那不是丢字 —— 内容一个字都没少，只是多了一个看不见的哨兵。把它当失败会让一条
#: "码也扫了、视频也传完了"的发布死在一个**看不见的字符**上（真机就是这么卡的）。
#:
#: ``whitespace_only`` / ``emoji_stripped`` **仍然拦**，这是刻意的：它们代表"文案真的
#: 变了"，该改的是文案生成（有用例钉着，别顺手放宽 —— 放宽之后"平台把换行吃了"
#: 与"平台把 emoji 吃了"会一起静默通过，而那正是第 ⑤ 步存在的理由）。
TOLERATED_READBACK_REASONS: Final[frozenset[str]] = frozenset({"invisible_only"})
#: 轮询间隔。0.5s 是"人眼看着像实时"与"不打满 CPU"之间的折中。
POLL_SEC: Final[float] = 0.5
#: 等扫码时的轮询间隔。比 POLL_SEC 慢：这一步等的是**人的动作**（掏手机、对准、
#: 确认），1s 的延迟没人感觉得到，而 0.5s 会在三分钟里问上三百多次。
LOGIN_POLL_SEC: Final[float] = 1.0
#: 登录标志最多等多久出现（秒）。**不能采样一次就走**：``goto`` 用的是
#: ``domcontentloaded``，而平台页面是 SPA —— 2026-09-23 真机实测，已登录的创作中心
#: 在 ``domcontentloaded`` 后 **+0.1s 什么都没有、+1.0s 才出现头像**（未登录那侧
#: 的二维码在 +1.3s）。问一次就把"还没渲染完"读成了"没登录"，而那个错值会一路
#: 传到面板上变成"尚未登录，去扫一次码" —— 人于是去重扫一个其实好好的号。
#: 8s 是"最慢的首屏"与"人不会觉得卡"之间的折中；真等满了也不说"没登录"，
#: 而是说"这页没渲染出可判断的东西"（见 :meth:`health`）。
MARKER_WAIT_SEC: Final[float] = 8.0
#: 上传超时（§06.5.2：上传 300s）。
UPLOAD_TIMEOUT_SEC: Final[float] = 300.0
#: 整体超时（§06.5.2：600s = job 租约）。超过它，job 会被别的 worker 抢走，
#: 我们继续跑只会让**两个**浏览器同时操作同一个账号。
PUBLISH_TIMEOUT_SEC: Final[float] = 600.0
#: **有人在场**时第 ⑦ 步的等待上限（秒）—— 见 ``PublisherContext.await_manual_verify``。
#:
#: 为什么比 ``PUBLISH_TIMEOUT_SEC`` 长：那 600s 是给**机器**的（网络抖一下、平台慢一拍），
#: 而这一步等的是**人**（掏手机、等短信、输六位数字）。同一个人在这两件事上的合理上限
#: 差一个量级 —— 拿机器那个数去卡人，症状是"我正输着呢，它说超时了"。
#:
#: 为什么不是"无限等"：等不到就永远占着那个 profile 目录（同账号只能开一个浏览器），
#: 而用户已经走开时没有任何东西能把它收回来。给一个数、到了如实报"没等到"，
#: 比留一个会一直挂着的会话好。
MANUAL_VERIFY_WAIT_SEC: Final[float] = 900.0
#: 点开弹层这类"顺手试一下"的动作超时（失败了也无所谓）。
SHORT_TIMEOUT_MS: Final[float] = 5_000
#: 没有进度元素时，选完文件后干等这么久（见 :meth:`PlaywrightPublisher._wait_upload`）。
UPLOAD_SETTLE_SEC: Final[float] = 3.0
#: 等"上传面板渲染出来"的宽限期（秒）。选完文件之后页面**不是**立刻就有进度条的：
#: 真机 2026-09-23 实测，抖音先给一屏"加载中，请稍候…"，约 1~2s 后表单与进度条才出现。
#: 宽限期之内没看到进度元素 ⇒ 认定这个页面没有进度 UI，退回"等一拍"
#: （见 :meth:`PlaywrightPublisher._wait_upload`）。
PROGRESS_GRACE_SEC: Final[float] = 10.0
#: "传完了"的读数：**百分比**到 100。
#:
#: 为什么不是 ``"100" in text``：进度元素外面常常还裹着一层（"已上传 100.0MB/182.1MB"
#: 这种），在一个大段文字里找 ``100`` 会把 **100MB 的成片**在 25% 时判成传完 ——
#: 而"提前往下走"正是这一整块判据要防的事。要的是 ``100%``。
_FULL_PROGRESS: Final[re.Pattern[str]] = re.compile(r"100\s*%")
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
        progress_grace_sec: float = PROGRESS_GRACE_SEC,
        upload_timeout_sec: float = UPLOAD_TIMEOUT_SEC,
        publish_timeout_sec: float = PUBLISH_TIMEOUT_SEC,
        marker_wait_sec: float = MARKER_WAIT_SEC,
        manual_verify_wait_sec: float = MANUAL_VERIFY_WAIT_SEC,
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
        self._progress_grace_sec = progress_grace_sec
        self._upload_timeout_sec = upload_timeout_sec
        self._publish_timeout_sec = publish_timeout_sec
        self._marker_wait_sec = marker_wait_sec
        self._manual_verify_wait_sec = manual_verify_wait_sec

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
                logged_in, on_login_page = await self._login_markers(page)
                if on_login_page:
                    return await self._not_logged_in(page)
                if not logged_in:
                    # 两个标志都没出现 ⇒ **不知道**，不是"没登录"。这句话必须说准：
                    # 说成"没登录"会把人赶去重扫一个其实好好的号（真机坑 2026-09-23，
                    # 见 :data:`MARKER_WAIT_SEC`）。
                    return PublishHealth.unknown(self._markers_missing_hint())
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

    async def login(self, *, timeout_sec: float = LOGIN_TIMEOUT_SEC) -> PublishHealth:
        """打开**可见**的浏览器窗口，等人扫码（T6.4 · R13：登录只发生在人扫码那一下）。

        为什么这个方法住在发布器里，而不是服务层自己开一个浏览器
        ------------------------------------------------------
        "打哪个页面、登录成功的标志是什么"是**平台知识**，与 :meth:`health` 用的是
        同一份选择器包（``urls.manage`` / ``login_ok`` / ``login_required``）。
        服务层自己开浏览器就得把这两样抄第二遍 —— 而抄出来的那一份**不会**跟着
        ``selectors/<platform>.yaml`` 一起热修（R13 的全部意义就在那份文件可热修）。

        为什么窗口开了之后**只轮询、不重新导航**
        --------------------------------------
        登录页上的码是**一次性的**：每隔十几秒重新导航一次，等于每十几秒换一张码，
        用户扫的那张在他按下确认的那一刻已经作废 —— 症状是"我明明扫了，它说没扫到"。
        所以这里只问"登录成功的标志出现没有"，把刷新交给页面自己。
        （人手动在窗口里点一下也没关系：那正是可见窗口存在的理由。）

        超时**不抛异常**：等不到人扫码不是故障，是一句要如实报出来的话（``hint``）。
        真出故障（浏览器起不来 / 页面打不开）才由 ``PublishError`` 往上抛。

        为什么轮询时把 ``wait_sec`` 传成 0
        ----------------------------------
        这一步**自己就是那个等待循环**（``LOGIN_POLL_SEC`` 一次），再让每次问都
        额外等 ``MARKER_WAIT_SEC`` 等于把"扫完了没有"的判定拖成 8 秒一档。
        """
        url = self._pack.urls.get("manage") or self._pack.url("upload")
        deadline = time.monotonic() + max(timeout_sec, 0.0)
        async with self._session_factory(self._ctx) as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            while True:
                if await self._logged_in(page, wait_sec=0.0):
                    name = await self._optional_text(page, self._pack.selector("login_ok"))
                    return PublishHealth(
                        ready=True,
                        logged_in=True,
                        last_check_at=now_iso(),
                        account_name=name or None,
                    )
                if time.monotonic() >= deadline:
                    return PublishHealth(
                        ready=False,
                        logged_in=False,
                        last_check_at=now_iso(),
                        hint=(
                            f"等了 {int(timeout_sec)} 秒没等到扫码完成 —— 窗口已经关掉，"
                            "可以再点一次「扫码登录」"
                        ),
                    )
                await asyncio.sleep(LOGIN_POLL_SEC)

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
        # ``manage`` 里可以带 ``{post_id}`` 占位符：真平台的管理页是"列出全部作品"
        # （不需要占位符），而本地靶页没有后端、只能把"有哪些作品"写在地址里。
        # 引用之前先转义：作品号将来可能带 ``?`` / ``&``（平台改 ID 格式时），
        # 不转义会让后面半截变成另一个查询参数 —— 一个安静的错地址。
        url = url.replace(POST_ID_PLACEHOLDER, quote(platform_post_id, safe=""))
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

        values: dict[str, float | int | None] = {}
        # 计数与比率**分开遍历**：两者的解析器不同（`1.2万` vs `42.3%`），
        # 而混用会让完播率被读成 42 —— 一个看着正常的错值（见 RATIO_METRIC_KEYS）。
        for key in (*METRIC_KEYS, *RATIO_METRIC_KEYS):
            selector = self._pack.selectors.get(f"metric_{key}", "")
            if not selector:
                values[key] = None
                continue
            # 后代组合器而不是"再查一次"：`text_content` 只收一个选择器，
            # 而"这一行里的那个数字"正好是 CSS 能表达的事。
            text = await self._optional_text(page, f"{row} {selector}")
            values[key] = parse_metric_ratio(text) if key in RATIO_METRIC_KEYS else parse_metric_count(text)

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
        # 值是按 :data:`METRIC_KEYS` / :data:`RATIO_METRIC_KEYS` **两组动态拼**出来的，
        # 而静态类型表达不了"这组键恰好等于那几个字段名"。加一个"加一个维度只改一处"
        # 的取舍放在这里，比把那五个键名在这里再抄一遍好 —— 抄一遍就又多了一处会过期的地方。
        return PublishMetrics(collected_at=now_iso(), **values)  # type: ignore[arg-type]

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
            if _tolerated(diff):
                _log_tolerated(req, field="标题", diff=diff)
            elif not diff.matched:
                problems.append(f"标题：{diff.detail}")
            if caption_selector and req.caption:
                caption = await self._read_field(page, caption_selector, self._pack.readback_kind("caption"))
                caption_diff = compare_readback(req.caption, caption)
                if _tolerated(caption_diff):
                    _log_tolerated(req, field="文案", diff=caption_diff)
                elif not caption_diff.matched:
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
        await self._dismiss_overlay(page, req)
        await page.click(self._pack.selector("publish_button"))

    async def _dismiss_overlay(self, page: PageLike, req: PublishRequest) -> None:
        """点掉挡住发布按钮的平台提示弹层（**尽力而为**）。

        真机 2026-09-23：第一次上传成片时，抖音会在右下角弹一个"视频预览功能"的说明层，
        右下角那个"我知道了"按钮正好压在发布按钮的命中区上。Playwright 的 ``click`` 会
        **等**元素能收到指针事件（默认 60s），于是"点发布"这一步会安静地耗掉一整分钟。

        与 ``cover_trigger`` 同一条取舍：装饰性的弹层不值得把一条能发的片子卡死在这里 ——
        找不到、点不动都只是少做一步，真正的失败由后面那句 ``click`` 去报。
        """
        selector = self._pack.selectors.get("dismiss_overlay", "")
        if not selector:
            return
        try:
            await page.click(selector, timeout=SHORT_TIMEOUT_MS)
        except Exception:
            return
        logger.info("关掉了挡住发布按钮的弹层", extra={"task_id": req.task_id, "selector": selector})

    def _result_budget_sec(self) -> float:
        """第 ⑦ 步最多等多久（秒）。

        **有人在场**时至少给 ``MANUAL_VERIFY_WAIT_SEC``：那一步等的是人（掏手机、
        等短信、输六位数字），不是网络。取 ``max`` 而不是"直接换掉" —— 调用方
        显式调长了 ``publish_timeout_sec`` 时不该被这一条又缩回去。
        """
        if self._ctx.await_manual_verify:
            return max(self._publish_timeout_sec, self._manual_verify_wait_sec)
        return self._publish_timeout_sec

    async def _step_result(self, page: PageLike, req: PublishRequest, *, started: float) -> PublishResult:
        deadline = time.monotonic() + self._result_budget_sec()
        reject_selector = self._pack.selectors.get("reject_marker", "")
        success_selector = self._pack.selectors.get("success_marker", "")
        verify_selector = self._pack.selectors.get("verify_marker", "")
        # 第二个成功判据：平台把页面**跳走**也算"收下了"。有些平台（抖音）不发
        # "发布成功"这几个字，而是直接落到内容管理列表 —— 只认元素的话，一条**已经
        # 发出去**的内容会在 600s 之后被记成失败，而那时人已经在平台上看到它了。
        success_urls = self._pack.texts("success_url_contains")
        # 只用来"日志里说一次"与拼超时文案 —— **判定本身不看它**（看它就等于把
        # "人有没有在动手"当成结论，而它只是过程）。
        saw_verify = False
        left_verify = False
        while time.monotonic() < deadline:
            if reject_selector and await self._present(page, reject_selector):
                text = await self._optional_text(page, reject_selector)
                raise PublishError(
                    f"平台审核不通过：{text or '（页面未给出文案）'}",
                    code=ErrorCode.PUBLISH_REVIEW_REJECTED,
                    context={"task_id": req.task_id, "platform_text": text},
                    remediation="人工看平台给的驳回理由；必要时回写稿阶段改稿",
                )
            # 平台要求短信 / 人脸验证 ⇒ 这是**人的活**（R13：不替人过验证）。
            #
            # 两种收场，差别**只在于窗口前面有没有人**（``ctx.await_manual_verify``）：
            # - 没人 ⇒ 立刻收场，转 manual_required 并说清动作。不认这一条的话，现场
            #   看到的是"卡在那里直到 600s 超时"，而屏幕上明明写着要做什么
            #   （真机 2026-09-23：点下发布后弹出「接收短信验证码」）。
            # - 有人 ⇒ 这不是收场，是"轮到你了"：窗口就在他面前，继续等他把码输进去。
            #   判据**一条都没变** —— 过掉之后照旧由上面那三条（驳回 / 成功元素 /
            #   地址跳转）收场。
            on_verify = bool(verify_selector) and await self._present(page, verify_selector)
            if on_verify and not self._ctx.await_manual_verify:
                evidence = await self._capture(page, req, stage=STAGE_FAILURE)
                raise PublishError(
                    "平台要求短信验证：这一步要人来做，自动流程到此为止",
                    code=ErrorCode.PUBLISH_FAILED,
                    context={"task_id": req.task_id, "account_id": req.account_id},
                    remediation="在平台上人工完成这次验证（或改用不触发验证的网络环境），"
                    "然后在这条记录上点「重试」",
                )
            if on_verify:
                if not saw_verify:
                    saw_verify = True
                    logger.info(
                        "平台要求人工验证，窗口已就绪 —— 请在窗口里点「获取验证码」并输入收到的码",
                        extra={"task_id": req.task_id, "account_id": req.account_id},
                    )
                await asyncio.sleep(POLL_SEC)
                continue
            if saw_verify and not left_verify:
                # 框没了。**不当作成功** —— 人可能只是点了「取消」，也可能是输错了重来。
                # 这一句只是让日志能回答"他到底动没动手"。
                left_verify = True
                logger.info(
                    "验证框已经消失，继续等结果页",
                    extra={"task_id": req.task_id, "url": page.url},
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
            if success_urls and any(fragment in page.url for fragment in success_urls):
                url, post_id = await self._read_post_link(page)
                evidence = await self._capture(page, req, stage=STAGE_RESULT)
                logger.info(
                    "页面已经跳走，按平台收下处理",
                    extra={"task_id": req.task_id, "url": page.url},
                )
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
            context={
                "task_id": req.task_id,
                "timeout_sec": self._result_budget_sec(),
                "saw_manual_verify": saw_verify,
                "left_manual_verify": left_verify,
            },
            remediation=(
                # 三种"没等到"要说三句不同的话 —— 它们下一步该做的事不一样：
                # 框没了 ⇒ 最要紧的是**先去平台上确认它到底发出去没有**。这时**不要**
                # 引导他点重试：万一已经发出去了，重试就是第二条（R14 不可逆）。
                "验证框已经没了但没看到结果页 —— 先到平台上确认这条到底发出去没有，"
                "再决定是点「重试」还是点「标记已处理」"
                if left_verify
                else (
                    "验证框一直在：可能没输码 / 码输错了 / 短信还没到。"
                    "窗口已经关了，要接着发就在平台上手工发这一条（或再点一次「人工过验证」）"
                    if saw_verify
                    else "看 evidence 里的截图：可能停在二次确认 / 平台改版"
                )
            ),
        )

    # ── 小工具 ──────────────────────────────────────────────────────

    async def _wait_upload(self, page: PageLike, req: PublishRequest) -> None:
        """等上传完成。

        判据是"**进度元素出现过、然后不见了（或走到 100%）**"，而**不是**"现在看不到它"。

        这个区别是真机 2026-09-23 换来的：选完文件那一刻页面还没渲染出上传面板，
        于是"看不到进度条"被读成了"已经传完"，第 ③ 步立刻开跑 —— 190MB 的成片还在
        0%，标题文案已经填完、发布按钮已经点下去了，平台回一句"你还有上次未发布的
        视频，是否继续编辑？"。整条发布于是卡在 600s 的单元超时上，而库里那两条记录
        一直停在 ``uploading``。**先看见、再消失**才是"传完了"。

        有进度元素就轮询它；没声明进度选择器（或宽限期内始终没出现）就**干等一拍**：
        干等不好，但比"猜一个不存在的元素"好 —— 它是一个明确的、可调的假设
        （``UPLOAD_SETTLE_SEC``）。
        """
        progress_selector = self._pack.selectors.get("upload_progress", "")
        if not progress_selector:
            await asyncio.sleep(self._settle_sec)
            return
        started = time.monotonic()
        deadline = started + self._upload_timeout_sec
        seen = False
        while time.monotonic() < deadline:
            handle = await page.query_selector(progress_selector)
            if handle is not None:
                seen = True
                text = await page.text_content(progress_selector) or ""
                if _FULL_PROGRESS.search(text):
                    return
            elif seen:
                return
            elif time.monotonic() - started > self._progress_grace_sec:
                logger.info(
                    "上传面板没有进度元素，等一拍再往下走",
                    extra={"task_id": req.task_id, "selector": progress_selector},
                )
                await asyncio.sleep(self._settle_sec)
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

    async def _logged_in(self, page: PageLike, *, wait_sec: float | None = None) -> bool:
        """这个页面看着像登录过吗（判据与两条边界都在 :meth:`_login_markers`）。"""
        logged_in, _ = await self._login_markers(page, wait_sec=wait_sec)
        return logged_in

    async def _login_markers(
        self,
        page: PageLike,
        *,
        wait_sec: float | None = None,
    ) -> tuple[bool, bool]:
        """等两个登录标志里**任意一个**出现，回 ``(已登录, 在登录页)``。

        为什么是"等任意一个"而不是只等 ``login_ok``
        ------------------------------------------
        未登录那侧也要能**立刻**收场：只等已登录标志的话，每一次"没登录"的探测
        都要空等满 ``wait_sec``，而这个答案页面上一秒前就写着。

        为什么两个都没等到时回的是 ``(False, False)`` 而不是"没登录"
        ------------------------------------------------------------
        这两件事的下一步动作完全不同：``(False, True)`` 是"去扫码"，
        ``(False, False)`` 是"这页没渲染出可判断的东西"（改版 / 网慢）。
        压成一个布尔值就等于替平台断言"你没登录" —— 而那正是真机上翻过的那次车。
        """
        budget = self._marker_wait_sec if wait_sec is None else max(wait_sec, 0.0)
        deadline = time.monotonic() + budget
        while True:
            # 顺序不能反：登录页上通常**同时**有"请扫码登录"和某个残留的头部占位元素，
            # 先问后者会把一个未登录的页面判成已登录 —— 而那个错误直到上传完才暴露。
            if await self._present(page, self._pack.selector("login_required")):
                return False, True
            if await self._present(page, self._pack.selector("login_ok")):
                return True, False
            if time.monotonic() >= deadline:
                return False, False
            await asyncio.sleep(POLL_SEC)

    def _markers_missing_hint(self) -> str:
        """两个标志都没出现时那句**能照做的**话（不是"你没登录"）。"""
        return (
            f"页面里两个登录标志都没出现（选择器版本 {self.selectors_version}）—— "
            "可能只是还没渲染完（再点一次「检测登录态」），也可能是平台改版了："
            "改选择器见 docs/runbook/publish_selector.md"
        )

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


def _tolerated(diff: ReadbackDiff) -> bool:
    """这条回读差异是不是"可以放行"的那一类（见 :data:`TOLERATED_READBACK_REASONS`）。"""
    return not diff.matched and diff.reason in TOLERATED_READBACK_REASONS


def _log_tolerated(req: PublishRequest, *, field: str, diff: ReadbackDiff) -> None:
    """放行一条回读差异时**留一句话**。

    为什么不静默：这是整条链路里唯一一处"两边对不上、我们照样往下发"的地方。留一句
    info 之后，"这条到底逐字对上没有"在日志里答得出来 —— 而容忍本身**每次都会命中**
    （平台每次都塞那个哨兵），所以它不能是 debug（默认级别下等于没写）。
    """
    logger.info(
        "回读放行（只差不可见字符）",
        extra={"task_id": req.task_id, "field": field, "detail": diff.detail},
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _tail(exc: PublishError) -> str:
    text = f"[{exc.code}] {exc.message}"
    if exc.remediation:
        text = f"{text}\n修复：{exc.remediation}"
    return text[:TAIL_LIMIT]
