"""本地靶页（T5.2 · **不是平台**）。

它为什么存在
------------
§06.5.3 的八步里，有五步是"页面上的动作"，而验证它们需要**一个能打开的页面**。
真平台的创作页要登录、要联网、还会随时间改版 —— 拿它当验收靶子，等于把
"我们的流程对不对"绑在"今天能不能连上抖音"上。

靶页把这件事解耦：同一个 ``PlaywrightPublisher``，同一份流程代码，打
``file://`` 上的本地表单。**真浏览器、真截图、真回读**，但不需要账号。

★ 它**不能**真的发布
--------------------
``platform = "fixture"`` 不在 §06.2.1 的平台矩阵里，也不在
``publications.platform`` 的 CHECK 约束里 ⇒ 就算有人把它接进发布池，落库那一步
也会被数据库挡下（纵深防御）。此外本类自己就拒绝 ``dry_run=False``。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from studio.core.errors import ErrorCode
from studio.publish.base import (
    PublisherContext,
    PublishRequest,
    PublishResult,
    register_publisher,
)
from studio.publish.browser import SessionFactory
from studio.publish.playwright_publisher import PlaywrightPublisher
from studio.publish.selectors import SelectorPack, load_selector_pack

__all__ = ["FIXTURE_PAGE", "FixturePublisher"]

#: 靶页文件（随包走）。
FIXTURE_PAGE: Path = Path(__file__).resolve().parent.parent / "fixtures" / "upload_form.html"


@register_publisher
class FixturePublisher(PlaywrightPublisher):
    """打本地靶页的发布器（**只接受 dry_run**）。"""

    platform: ClassVar[str] = "fixture"

    def __init__(
        self,
        ctx: PublisherContext,
        *,
        pack: SelectorPack | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        base = pack or load_selector_pack(self.platform)
        # 靶页地址：**pack 里给了就用它**（集成测试把同一份 HTML 挂在本地 http 上 ——
        # 那样"点下发布"能回报到服务器，于是"dry-run 一次都没点"变成一条可断言的
        # 事实，而不是"看截图里像没有结果块"）。没给才回落到 file://。
        declared = base.urls.get("upload", "")
        page_url = declared if declared and declared != "about:blank" else FIXTURE_PAGE.as_uri()
        # 查询串从 ``ctx.probe`` 来（见 PublisherContext.probe 的注释）：靶页靠它
        # 制造"未登录 / 吞字 / 审核不通过"，好让演练能验到失败分支。
        url = page_url + (ctx.probe or "")
        super().__init__(
            ctx,
            pack=replace(base, urls={**base.urls, "upload": url, "manage": url}),
            session_factory=session_factory,
        )

    async def publish(self, req: PublishRequest) -> PublishResult:
        if not req.dry_run:
            return PublishResult.failure(
                ErrorCode.PUBLISH_NOT_IMPLEMENTED,
                "本地靶页只用于演练（dry_run），不发布任何东西",
            )
        return await super().publish(req)
