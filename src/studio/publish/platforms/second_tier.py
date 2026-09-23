"""二线平台的**真实现**（§06.2.1 平台矩阵的后四行）。

这一档原本是**空实现**（Q9：一期只做一线，二线保留接口）
--------------------------------------------------------
改成真实现的理由很具体：空实现把"没做"与"做了但**没在真机上校准**"变成了
**看起来一样的**两件事 —— 两者都只有一行灰字、都发不出去。而现在这两件事分得开了：

- **没做** ⇒ 注册表里查不到这个代号（``PUBLISH_NOT_IMPLEMENTED``）；
- **做了、但没真机校准** ⇒ 是**真实现**：能投递、能演练、能采数，而 pack 自己声明
  ``calibrated: false``，面板上那一列照实显示，``known_gaps`` 说清还差什么。

后者比前者诚实得多：它把"这个平台还差一次校准"变成一句**能照着做**的话
（``studio publish calibrate --platform xiaohongshu``），而不是一句"未实现"。

⚠️ **代价要说在前面**：这四份 pack 的 CSS **一条都没在真机上验过**（连 ``urls``
也是初稿）。所以 ``config/publish.yaml`` 里它们的 ``enabled`` 仍然是 ``false`` ——
打开它等于"我知道它会失败，我就想看看它怎么失败"。真要用，先跑 calibrate。

为什么四个类都只有一行 ``platform``
-----------------------------------
平台差异只允许出现在三个地方（§4.6.1）：选择器 yaml、``config/publish.yaml`` 的平台段、
以及本目录的子类。这四家在那八步上与抖音**没有任何差别**，所以子类只声明代号 ——
与 ``douyin.py`` 里那段注释是同一条规矩。

⚠️ **已知还缺的平台特有流程**（写在 pack 的 ``known_gaps`` 里，面板上跟着显示）：
B 站的**必选分区**是个级联下拉，八步里没有这一步。真要做，那一段写在
``platforms/bilibili.py`` 里，**不许**往 ``playwright_publisher.py`` 里加
``if platform == "bilibili"``。
"""

from __future__ import annotations

from typing import ClassVar

from studio.publish.base import register_publisher
from studio.publish.playwright_publisher import PlaywrightPublisher

__all__ = [
    "BilibiliPublisher",
    "WeiboPublisher",
    "XiaohongshuPublisher",
    "XiguaPublisher",
]


@register_publisher
class XiaohongshuPublisher(PlaywrightPublisher):
    """小红书创作服务平台。"""

    platform: ClassVar[str] = "xiaohongshu"


@register_publisher
class BilibiliPublisher(PlaywrightPublisher):
    """B 站创作中心（⚠️ 必选分区那一步还没做，见模块注释与 pack 的 ``known_gaps``）。"""

    platform: ClassVar[str] = "bilibili"


@register_publisher
class XiguaPublisher(PlaywrightPublisher):
    """西瓜视频创作平台。"""

    platform: ClassVar[str] = "xigua"


@register_publisher
class WeiboPublisher(PlaywrightPublisher):
    """微博视频号（创作中心）。"""

    platform: ClassVar[str] = "weibo"
