"""平台实现（§4.6.1 的注册表在这里**装配**）。

为什么注册表在导入期就填满
--------------------------
"某个平台有没有实现"是一个**静态事实**，不该等到第一次发布才知道。所以七个平台
（一线三个真实现 + 二线四个空实现 + 靶页）都在 import 时登记进
:data:`studio.publish.base.PUBLISHERS`，而契约测试拿 ``config/publish.yaml`` 的
``platforms`` 键去比对 —— 少一个、多一个都红灯。

``REAL_PLATFORMS`` 是**从注册表里减掉非平台代号**得来的，不是手抄的清单：
手抄的清单会在"加了平台但忘了改清单"时静默过期，而那个清单正是发布面板与
契约测试的依据。
"""

from __future__ import annotations

from studio.publish.base import PUBLISHERS
from studio.publish.platforms.douyin import DouyinPublisher
from studio.publish.platforms.fixture import FIXTURE_PAGE, FixturePublisher
from studio.publish.platforms.kuaishou import KuaishouPublisher
from studio.publish.platforms.second_tier import (
    BilibiliPublisher,
    WeiboPublisher,
    XiaohongshuPublisher,
    XiguaPublisher,
)
from studio.publish.platforms.shipinhao import ShipinhaoPublisher

__all__ = [
    "FIXTURE_PAGE",
    "NON_PLATFORM_CODES",
    "REAL_PLATFORMS",
    "BilibiliPublisher",
    "DouyinPublisher",
    "FixturePublisher",
    "KuaishouPublisher",
    "ShipinhaoPublisher",
    "WeiboPublisher",
    "XiaohongshuPublisher",
    "XiguaPublisher",
]

#: 演练用的代号，**不是**平台（见 ``platforms/fixture.py``）。
NON_PLATFORM_CODES: frozenset[str] = frozenset({"fixture"})

#: 真实平台代号（§06.2.1 的七行）。
REAL_PLATFORMS: tuple[str, ...] = tuple(sorted(code for code in PUBLISHERS if code not in NON_PLATFORM_CODES))
