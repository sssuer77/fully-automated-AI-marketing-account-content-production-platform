"""二线平台的**空实现**（§06.2.1 · Q9：接口已定，实现可空）。

为什么留一个空实现而不是干脆不登记
----------------------------------
"没有这个平台"与"这个平台还没做"对调用方是两件事：
- 前者 ⇒ ``PUBLISH_NOT_IMPLEMENTED``（我们的注册表里查不到）；
- 后者 ⇒ 同样是 ``PUBLISH_NOT_IMPLEMENTED``，但 ``context`` 里带着**为什么**。

而 ``health()`` 必须能回答（哪怕答案是"不可用"）：发布面板要列全部七个平台
（§06.12），列表里那些灰色的行不该让面板整个报错。

``health()`` 返回 ``ready=False`` 而**不是抛异常**：探测一个还没实现的平台
不是异常情况，是正常情况（Q9 就是这么定的）。
"""

from __future__ import annotations

from typing import ClassVar

from studio.core.errors import ErrorCode, PublishError
from studio.publish.base import (
    Publisher,
    PublisherContext,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
    register_publisher,
)

__all__ = [
    "BilibiliPublisher",
    "WeiboPublisher",
    "XiaohongshuPublisher",
    "XiguaPublisher",
]

_HINT = "二期平台：一期只保留接口与 profile（§06.2.1 · Q9）"


class _SecondTierPublisher(Publisher):
    """二线平台的公共行为（**只声明 platform 即可**）。"""

    platform: ClassVar[str] = ""

    def __init__(self, ctx: PublisherContext) -> None:
        super().__init__(ctx)

    async def health(self) -> PublishHealth:
        return PublishHealth.unknown(f"{self.platform} {_HINT}")

    async def publish(self, req: PublishRequest) -> PublishResult:
        return PublishResult.failure(
            ErrorCode.PUBLISH_NOT_IMPLEMENTED,
            f"{self.platform} {_HINT}",
        )

    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        raise PublishError(
            f"{self.platform} {_HINT}",
            code=ErrorCode.PUBLISH_NOT_IMPLEMENTED,
            context={"platform": self.platform, "platform_post_id": platform_post_id},
        )


@register_publisher
class XiaohongshuPublisher(_SecondTierPublisher):
    platform: ClassVar[str] = "xiaohongshu"


@register_publisher
class BilibiliPublisher(_SecondTierPublisher):
    platform: ClassVar[str] = "bilibili"


@register_publisher
class XiguaPublisher(_SecondTierPublisher):
    platform: ClassVar[str] = "xigua"


@register_publisher
class WeiboPublisher(_SecondTierPublisher):
    platform: ClassVar[str] = "weibo"
