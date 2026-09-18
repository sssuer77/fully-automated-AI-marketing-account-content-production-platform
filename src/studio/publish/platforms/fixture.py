"""本地靶页（T5.2 · **不是平台**）。

它为什么存在
------------
§06.5.3 的八步里，有五步是"页面上的动作"，而验证它们需要**一个能打开的页面**。
真平台的创作页要登录、要联网、还会随时间改版 —— 拿它当验收靶子，等于把
"我们的流程对不对"绑在"今天能不能连上抖音"上。

靶页把这件事解耦：同一个 ``PlaywrightPublisher``，同一份流程代码，打
``file://`` 上的本地表单。**真浏览器、真截图、真回读**，但不需要账号。

★ 它从 T5.9 起能"真发布"到本地靶页 —— 但仍然发不到任何真平台
-----------------------------------------------------------
改这一条的理由是**链路的最后两段此前没法验**：``dry-run`` 只能验到"停在第 ⑥ 步
之前"，而"点下发布 → 作品号回到库里 → 播放量/点赞量/完播率真的采回来 → 面板上
真的出现数字"这件事，在没有真账号时**没有任何办法验**。演练台把它解耦：同一个
:class:`~studio.publish.playwright_publisher.PlaywrightPublisher`、同一份八步代码、
同一套选择器机制，只是打本地 ``file://`` 靶页。

三道保护，替代原来那条"拒绝 ``dry_run=False``"：

① **只认本地靶页**。页面地址由 :data:`FIXTURE_PAGE` 决定（``file://`` 或测试挂的
   本地 http），配置里没有第二个地方能改它 —— 它发不到 ``https://`` 上的任何东西；
② **只服务演练台**。真发布（``dry_run=False``）时 ``req.platform`` 必须是
   :data:`REHEARSAL_PLATFORMS` 里的代号；有人把 ``platforms.douyin.publisher``
   改成 ``fixture`` 想"发抖音"，这里会直接拒 —— 否则库里会出现一条
   ``platform='douyin'`` 而其实什么都没发的记录；
③ **数据库那道仍然在**。``publications.platform`` 的 CHECK 里**没有** ``fixture``，
   所以演练台落库时用的是 ``other``（§03.3.15 本来就有的那一档）。

``dry-run`` 演练（``--target fixture``）不受 ② 约束：那时候 ``req.platform`` 是
**被演练的那个真平台**（``douyin``），而这一步根本不会点发布按钮。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Final

from studio.core.errors import ErrorCode
from studio.publish.base import (
    PublisherContext,
    PublishRequest,
    PublishResult,
    register_publisher,
)
from studio.publish.browser import SessionFactory
from studio.publish.playwright_publisher import PlaywrightPublisher
from studio.publish.selectors import (
    POST_ID_PLACEHOLDER,
    SelectorPack,
    load_selector_pack,
)

__all__ = ["FIXTURE_PAGE", "REHEARSAL_PLATFORMS", "FixturePublisher"]

#: 靶页文件（随包走）。
FIXTURE_PAGE: Path = Path(__file__).resolve().parent.parent / "fixtures" / "upload_form.html"

#: 允许**真发布**到靶页的平台代号（见模块 docstring 的保护 ②）。
#: ``other`` 是演练台落库用的那一档（§03.3.15）；``fixture`` 留着是因为测试里会直接
#: 拿它当平台代号造请求 —— 两条都指向同一个靶页，多认一个不会放宽任何边界。
REHEARSAL_PLATFORMS: Final[frozenset[str]] = frozenset({"other", "fixture"})


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
        # 管理页要**指明看哪一条作品**：真平台的管理页会列出全部作品，而靶页没有
        # 后端，只能把"有哪些作品"写在地址里（`{post_id}` 由 `fetch_metrics` 填）。
        separator = "&" if "?" in url else "?"
        manage = f"{url}{separator}post={POST_ID_PLACEHOLDER}"
        super().__init__(
            ctx,
            pack=replace(base, urls={**base.urls, "upload": url, "manage": manage}),
            session_factory=session_factory,
        )

    async def publish(self, req: PublishRequest) -> PublishResult:
        """八步照跑，**真发布也只打到本地靶页**（见模块 docstring 的三道保护）。"""
        if not req.dry_run and req.platform not in REHEARSAL_PLATFORMS:
            return PublishResult.failure(
                ErrorCode.PUBLISH_NOT_IMPLEMENTED,
                f"本地靶页只服务演练台（{'/'.join(sorted(REHEARSAL_PLATFORMS))}），不发 {req.platform}",
            )
        return await super().publish(req)
