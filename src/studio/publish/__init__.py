"""发布子系统（T5.x · 第六部分重建）。

目录里为什么一开始是空的
----------------------------
``publish/`` 自 T1.1 建骨架时就在，但一直只有 ``platforms/.gitkeep`` 与 ``selectors/.gitkeep``：
一期的排期把"发布"放在最后（T5），而**发布默认关闭**（``publish.enabled=false`` · R14 不可逆）。
在那之前往里塞空壳，只会让"看起来有发布能力"。

现在已经落地的四块
------------------
- :mod:`studio.publish.cover` —— 封面合成（T5.1：抽帧 + 文字 + 降级）；
- :mod:`studio.publish.precheck` —— 发布前二次校验（T5.1：三道门禁 + 禁区扫描）；
- :mod:`studio.publish.base` / :mod:`~studio.publish.playwright_publisher` ——
  ``Publisher`` 抽象与通用八步实现（T5.2）；
- :mod:`studio.publish.selectors` + ``selectors/*.yaml`` —— 选择器集中化（T5.2 · R13）。

其中**只有** ``playwright_publisher`` 会真的点下"发布"那一下，而它要同时满足
``publish.enabled=true`` + ``require_confirm`` 的确认闸（§06.4）才会被服务层调到。
本模块的导入**不产生任何副作用**：不连数据库、不起浏览器、不读配置。
"""

from __future__ import annotations

from studio.publish.base import (
    PUBLISHERS,
    Publisher,
    PublisherContext,
    PublisherFactory,
    PublishEvidence,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
    PublishStatus,
    get_publisher,
    register_publisher,
)
from studio.publish.cover import (
    CoverResult,
    build_cover,
    measure_text_width,
    resolve_cover_font,
    resolve_frame_at_ms,
    write_cover_atomically,
)
from studio.publish.platforms import (
    NON_PLATFORM_CODES,
    REAL_PLATFORMS,
    DouyinPublisher,
    FixturePublisher,
    KuaishouPublisher,
    ShipinhaoPublisher,
)
from studio.publish.playwright_publisher import PlaywrightPublisher
from studio.publish.precheck import (
    GateResult,
    PrecheckReport,
    PublishContent,
    run_precheck,
)
from studio.publish.selectors import SelectorPack, load_selector_pack, selector_root

__all__ = [
    "NON_PLATFORM_CODES",
    "PUBLISHERS",
    "REAL_PLATFORMS",
    "CoverResult",
    "DouyinPublisher",
    "FixturePublisher",
    "GateResult",
    "KuaishouPublisher",
    "PlaywrightPublisher",
    "PrecheckReport",
    "PublishContent",
    "PublishEvidence",
    "PublishHealth",
    "PublishMetrics",
    "PublishRequest",
    "PublishResult",
    "PublishStatus",
    "Publisher",
    "PublisherContext",
    "PublisherFactory",
    "SelectorPack",
    "ShipinhaoPublisher",
    "build_cover",
    "get_publisher",
    "load_selector_pack",
    "measure_text_width",
    "register_publisher",
    "resolve_cover_font",
    "resolve_frame_at_ms",
    "run_precheck",
    "selector_root",
    "write_cover_atomically",
]
