"""浏览器缝（T5.2 · §06.5）—— 把 Playwright 关在一个 Protocol 后面。

为什么要这条缝
--------------
§06.5.3 的八步里，七步是**判据**（该点哪个元素、回读到什么、什么算成功），只有
"谁来执行这些动作"是 Playwright 的事。缝起来之后：

- 单元测试用**假页面**把八步逐条验完（毫秒级、不起浏览器、不联网）；
- 真机演练用**真 Playwright** 打本地靶页（``fixtures/upload_form.html``）；
- 两者跑的是**同一份流程代码** ⇒ "单测全绿但真机点错按钮"这类事不会发生。

（与 T2.8 把 ``STUDIO_FAULT`` 套在**引擎缝**上是同一个手法：注入点必须落在真正会
出错的那一层，否则验的是假的。）

为什么 ``playwright`` 只在函数内 import
---------------------------------------
它是个带浏览器二进制（数百 MB）的可选依赖，而 ``publish/`` 会被 ``cli.py``、
``publish_service`` 与契约测试导入。放模块顶，一条"没装 playwright"的环境会在
**import 期**就炸 —— 而那时用户可能只是想跑 ``studio publish precheck``。

R13 合规底线（**写在这里，免得以后有人"顺手优化"掉**）
------------------------------------------------------
不自动登录、不绕过验证码、不做反检测伪装。所以本文件里**不会**出现
``--disable-blink-features=AutomationControlled`` 之类的启动参数，也不会出现
任何"模拟真人操作节奏"的逻辑。登录只发生在人扫码那一下。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from studio.core.errors import ErrorCode, PublishError
from studio.publish.base import PublisherContext

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "NAV_TIMEOUT_MS",
    "BrowserSession",
    "PageLike",
    "PlaywrightSession",
    "SessionFactory",
    "open_session",
]

#: 单个动作的默认超时（§06.5.2：上传 300s / 整体 600s = job 租约）。
DEFAULT_TIMEOUT_MS = 60_000
#: 打开页面的超时。比动作短：打不开就是打不开，等 60s 只是把失败拖长。
NAV_TIMEOUT_MS = 30_000


@runtime_checkable
class PageLike(Protocol):
    """八步真正用到的那几个动作（**这就是发布器对浏览器的全部要求**）。

    刻意**不**照抄 Playwright 的 ``Page``：抄一遍等于把"我们依赖 Playwright 的哪些
    行为"这件事藏进一个巨大的接口里，而换实现（或写假页面）时要实现的是一整个
    ``Page``。这里列出来的每一条都有明确的调用点。
    """

    # 下面三处的 ``timeout`` 参数是**照抄 Playwright 的签名**（超时由它自己管，
    # 我们不需要 asyncio.timeout）—— ASYNC109 在这里是误报，逐处注明。
    async def goto(
        self,
        url: str,
        *,
        wait_until: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any: ...

    async def query_selector(self, selector: str) -> Any | None:
        """元素在不在。**不等待** —— 等待会拖长"未登录"这种本该立刻返回的判定。"""
        ...

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        state: str | None = None,
    ) -> Any: ...

    async def set_input_files(self, selector: str, files: str | Path | Sequence[str | Path]) -> None: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def click(
        self,
        selector: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None: ...

    async def inner_text(self, selector: str) -> str:
        """**可见**文本。富文本区（``contenteditable``）回读用它。"""
        ...

    async def text_content(self, selector: str) -> str | None:
        """文本内容，**不看可见性**。进度条这类"藏着但仍在 DOM 里"的元素用它。"""
        ...

    async def input_value(self, selector: str) -> str:
        """``<input>`` / ``<textarea>`` 的**当前值**（不是初始内容）。"""
        ...

    async def get_attribute(self, selector: str, name: str) -> str | None:
        """读一个属性（第 ⑦ 步取作品链接用）。"""
        ...

    async def screenshot(self, *, path: str | Path, full_page: bool = False) -> bytes: ...

    async def content(self) -> str: ...

    async def evaluate(self, expression: str) -> Any: ...

    def set_default_timeout(self, timeout: float) -> None: ...


class BrowserSession(Protocol):
    """一次浏览器会话（异步上下文管理器，``__aenter__`` 交出页面）。"""

    async def __aenter__(self) -> PageLike: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


#: 会话工厂：``(ctx) -> BrowserSession``。测试与演练靠替换它来注入假页面。
SessionFactory = Callable[[PublisherContext], BrowserSession]


class PlaywrightSession:
    """真实实现：持久化 ``user_data_dir`` 的 Chromium（§06.5.2）。

    用 ``launch_persistent_context`` 而不是 ``new_context(storage_state=...)``：
    登录态是**一整个 profile 目录**（cookie + localStorage + IndexedDB），
    序列化成一份 storage_state 会在某次平台改版后悄悄丢掉一半 —— 而症状是
    "昨天还好好的，今天要重新扫码"，查起来很贵。
    """

    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        viewport: tuple[int, int] = (1440, 900),
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._viewport = viewport
        self._playwright: Any = None
        self._context: Any = None

    async def __aenter__(self) -> PageLike:
        from playwright.async_api import async_playwright  # noqa: PLC0415 —— 见模块注释

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                viewport={"width": self._viewport[0], "height": self._viewport[1]},
            )
        except Exception as exc:
            await self._shutdown()
            raise _launch_error(exc) from exc

        self._context.set_default_timeout(self._timeout_ms)
        pages = list(getattr(self._context, "pages", []) or [])
        # playwright 那边全是 ``Any``；注解在这里把返回值收窄成我们真正要的那几个动作。
        page: PageLike = pages[0] if pages else await self._context.new_page()
        page.set_default_timeout(self._timeout_ms)
        return page

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        context, playwright = self._context, self._playwright
        self._context, self._playwright = None, None
        # 关不掉不该盖住真正的失败：调用方此时多半正拿着一个"为什么发不出去"的异常。
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()


def _launch_error(exc: Exception) -> PublishError:
    """把 Playwright 的启动异常翻成"照着做就能修"的一条。"""
    text = str(exc)
    lowered = text.lower()
    if "executable doesn" in lowered or "please run the following command" in lowered:
        return PublishError(
            "没有可用的 Chromium（Playwright 浏览器未安装）",
            code=ErrorCode.PUBLISH_DISABLED,
            context={"error": text[:500]},
            remediation="跑一次 `python -m playwright install chromium`"
            "（浏览器落在 PLAYWRIGHT_BROWSERS_PATH）",
        )
    return PublishError(
        f"启动浏览器失败：{text[:200]}",
        code=ErrorCode.PUBLISH_FAILED,
        context={"error": text[:500]},
        remediation="确认该账号的 profile 目录没被另一个进程占用（同一账号同时只能开一个）",
    )


def open_session(
    *,
    profile_dir: Path,
    headless: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> BrowserSession:
    """默认会话工厂（真 Playwright）。"""
    return PlaywrightSession(profile_dir=profile_dir, headless=headless, timeout_ms=timeout_ms)
