"""微信视频号（一线 · §06.2.1）。

这个文件里**没有**任何逻辑，这是设计目标而不是偷懒
--------------------------------------------------
平台差异只允许出现在三个地方（§4.6.1）：选择器 yaml、``config/publish.yaml`` 的
平台段、以及本目录的子类。视频号与抖音在 §06.5.3 那八步上**没有任何差别** ——
差别全在"页面上那个框叫什么"（选择器）与"标题能写多少字"（profile）。

所以子类只声明 ``platform``。哪天某个平台真的需要一段特殊流程（比如 B 站必须
选分区），那一段写在这里，而**不是**往 ``playwright_publisher.py`` 里加
``if platform == "bilibili"``。
"""

from __future__ import annotations

from typing import ClassVar

from studio.publish.base import register_publisher
from studio.publish.playwright_publisher import PlaywrightPublisher

__all__ = ["ShipinhaoPublisher"]


@register_publisher
class ShipinhaoPublisher(PlaywrightPublisher):
    """微信视频号助手。"""

    platform: ClassVar[str] = "shipinhao"
