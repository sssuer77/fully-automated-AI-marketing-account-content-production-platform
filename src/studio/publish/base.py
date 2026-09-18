"""``Publisher`` 抽象（T5.2 · §4.6.1 · A1 多平台）。

平台差异只允许出现在三个地方（§4.6.1 逐字）
-------------------------------------------
① ``selectors/<platform>.yaml`` —— 页面选择器，**可热修**（R13）；
② ``config/publish.yaml`` 的平台段 —— 长度上限 / 话题语法 / 限频；
③ ``publish/platforms/<platform>.py`` —— 本 ABC 的子类实现。

除此之外**任何地方**都不该出现 ``if platform == "douyin"``。那种分支一旦散开，
"平台改版"就变成"满仓库找分支"，而 R13 要的"选择器集中化便于热修"当场作废。

为什么请求 / 结果用 dataclass 而不是 Pydantic
---------------------------------------------
它们**不过 JSON 边界**：HTTP 面是 T5.5 的发布面板，那一层会自己定义 Pydantic 模型
再映射过来。这里用冻结 dataclass（与 ``render/``、``publish/cover.py`` 一致）：
不可变、可哈希、可比较，而且构造一次"我要发这个"不必先过一遍校验器。

为什么是 async
--------------
``health`` / ``publish`` / ``fetch_metrics`` 的主体是**等浏览器**（§4.6.1 的签名就是
``async``）。同步实现会把 publish worker 的线程占满整个上传过程（300s 超时 × 单并发），
而队列那侧还指望着同一个进程能响应租约续期。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final

from studio.core.clock import now_iso
from studio.core.config import AccountConfig, PlatformConfig
from studio.core.errors import ErrorCode, PublishError
from studio.core.paths import StudioPaths

__all__ = [
    "PUBLISHERS",
    "REHEARSAL_PUBLISHER",
    "PublishEvidence",
    "PublishHealth",
    "PublishMetrics",
    "PublishRequest",
    "PublishResult",
    "PublishStatus",
    "Publisher",
    "PublisherContext",
    "PublisherFactory",
    "get_publisher",
    "register_publisher",
]


class PublishStatus(StrEnum):
    """发布状态机（§06.5.4 · 与任务状态机**解耦**）。

    ``publish/`` 的失败**不回退任务状态**：任务停在 ``completed``，成片仍然有效，
    可人工下载或换平台重发（§06.5.4「关键约定」）。
    """

    QUEUED = "queued"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class PublisherContext:
    """装配一个 Publisher 需要的全部东西 —— **子类构造函数的唯一参数**。

    为什么不把 ``account`` / ``platform`` / ``paths`` 摊成三个位置参数：那种签名会
    随每加一个平台而长（再来个 ``browser``、``timeout``…），而"加字段"要动**每一个**
    子类的签名。打成一包之后，子类只关心"我怎么发这个平台"，不关心"装配时都给了什么"。
    """

    paths: StudioPaths
    account: AccountConfig
    platform: PlatformConfig
    #: ``False`` 时浏览器可见（首次扫码登录 / 排障，§06.5.2）。**默认 headless**。
    headless: bool = True
    #: 演练靶页的查询串（``--target fixture``，形如 ``"?logged_out=1"``）。
    #: **真实平台实现一律忽略它** —— 它存在的理由是：靶页的"制造故障"开关要走 URL，
    #: 而给 ``Publisher`` 的工厂签名再加一个参数，就得让每个平台子类都多接一个用不上的 kwarg。
    probe: str = ""

    @property
    def account_id(self) -> str:
        return self.account.account_id

    @property
    def platform_code(self) -> str:
        return self.account.platform

    @property
    def profile_dir(self) -> Path:
        """该账号的持久化登录态目录（**按账号隔离**，§06.2.4）。"""
        return self.paths.browser_profile_dir / self.account.account_id


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """一次发布请求（§4.6.1 的字段集，去掉 HTTP 专属的 ``model_config``）。"""

    task_id: str
    platform: str
    account_id: str
    video_path: Path
    title: str = ""
    caption: str = ""
    tags: tuple[str, ...] = ()
    cover_path: Path | None = None
    scheduled_at: str | None = None
    #: ★ 演练模式：走完流程到"确认发布"前一步停下并截图（§06.5.3 第 ⑥ 步）。
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PublishEvidence:
    """失败 / 成功的取证（R13：失败可排查）。"""

    screenshot_path: Path | None = None
    dom_snapshot_path: Path | None = None
    stderr_tail: str | None = None
    selector_version: str | None = None
    platform_text: str | None = None
    #: 走到第几步停下的（§06.5.3 的八步编号）。dry-run 靠它回答"停在哪"。
    stage: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    ok: bool
    status: PublishStatus
    url: str | None = None
    platform_post_id: str | None = None
    published_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    evidence: PublishEvidence | None = None
    elapsed_ms: int = 0

    @classmethod
    def failure(
        cls,
        error_code: ErrorCode | str,
        message: str,
        *,
        status: PublishStatus = PublishStatus.MANUAL_REQUIRED,
        evidence: PublishEvidence | None = None,
        elapsed_ms: int = 0,
    ) -> PublishResult:
        """失败结果的**唯一构造入口**（默认落 ``manual_required``，§06.10）。

        默认值是 ``manual_required`` 而不是 ``failed``：§06.10 的六种触发场景里有五种
        的处置都是转人工，而 ``failed`` 是"还会自动重试"的中间态 —— 让"忘记传状态"
        落到"还要重试"上，会把一条发不出去的片子重试到天亮。
        """
        return cls(
            ok=False,
            status=status,
            error_code=str(error_code),
            error_message=message,
            evidence=evidence,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def published(
        cls,
        *,
        url: str | None,
        platform_post_id: str | None,
        published_at: str | None = None,
        evidence: PublishEvidence | None = None,
        elapsed_ms: int = 0,
    ) -> PublishResult:
        return cls(
            ok=True,
            status=PublishStatus.PUBLISHED,
            url=url,
            platform_post_id=platform_post_id,
            published_at=published_at or now_iso(),
            evidence=evidence,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def stopped_before_publish(
        cls, *, evidence: PublishEvidence | None = None, elapsed_ms: int = 0
    ) -> PublishResult:
        """dry-run 的落点（§06.5.3 第 ⑥ 步）：``ok=true, status='queued'``。

        ``ok`` 为真而 ``status`` 不是 ``published``：这次演练**该做的事都做成了**
        （页面打开、文件选中、文案填对、回读一致、截图留下），只是最后那一下没点。
        把它写成 ``ok=false`` 会让"演练成功"在面板上与"演练失败"长得一样。
        """
        return cls(
            ok=True,
            status=PublishStatus.QUEUED,
            evidence=evidence,
            elapsed_ms=elapsed_ms,
        )


@dataclass(frozen=True, slots=True)
class PublishMetrics:
    """一条作品的数据读数（§06.6 · T5.4 采集）。

    ★ 增补 ``completion_rate``（完播率）
    ---------------------------------
    §4.6.1 那张字段表只有四个**计数**，而"这条片子留不留得住人"是完播率回答的 ——
    播放量高、完播率低，说明标题骗进来了、内容没接住，这两条给选题的指令正好相反。
    计数与比率在这里**分成两类**：计数走 :func:`~studio.publish.metrics.parse_metric_count`
    （认 ``1.2万``），比率走 :func:`~studio.publish.metrics.parse_metric_ratio`（认 ``42.3%``），
    两类各有各的解析器，混用会让 ``42.3%`` 被读成 42（差 100 倍）。

    ``None`` 一律表示"我们不知道"（平台没公开 / 还没统计出来），**不是 0**。
    """

    collected_at: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    #: 完播率，**0–1 的比值**（``0.423`` = 42.3%）。存比值而不是百分数：百分数
    #: 一旦在某个环节被当成比值用（或反过来），误差是 100 倍，而两者都是"看着正常"的数。
    completion_rate: float | None = None


@dataclass(frozen=True, slots=True)
class PublishHealth:
    """登录态探测结果（§4.6.1 · **不自动登录**）。"""

    ready: bool
    logged_in: bool
    last_check_at: str
    account_name: str | None = None
    #: ``'需人工扫码登录'`` / ``'登录态已过期'`` / 其他一句人话。
    hint: str | None = None

    @classmethod
    def unknown(cls, hint: str) -> PublishHealth:
        """**探不出来**时的诚实答案：``logged_in=False`` 而不是默认放行。

        把"探测失败"写成 ``ready=True`` 会让后面的发布在真机上以
        ``PUBLISH_UPLOAD_FAILED`` 收场 —— 同一个问题从"登录态过期"（一眼可修）
        变成了"上传失败"（要去翻截图）。
        """
        return cls(ready=False, logged_in=False, last_check_at=now_iso(), hint=hint)


class Publisher(ABC):
    """平台适配层的抽象（§4.6.1）。"""

    #: 平台代号，与 ``config/publish.yaml`` 的 ``platforms`` 键、``platforms/<x>.py`` 一致。
    platform: ClassVar[str]

    def __init__(self, ctx: PublisherContext) -> None:
        """装配期把上下文交给实例。

        §4.6.1 规定的是三个**抽象方法**，构造函数不在其中 —— 但 :data:`PUBLISHERS`
        里存的是**类**，服务层要能拿 ``get_publisher(code)(ctx)`` 直接造实例。
        把签名写在基类上，"每个实现都收同一个上下文"就成了类型上看得见的事；
        不写的话，服务层那一行只能靠 ``cast`` 蒙混过去，而漏掉一个参数要等真机才发现。
        """
        self._ctx = ctx

    @property
    def context(self) -> PublisherContext:
        """本次发布的上下文（账号、路径、探针）。"""
        return self._ctx

    @abstractmethod
    async def health(self) -> PublishHealth:
        """登录态探测（**只探测，不登录**）。"""

    @abstractmethod
    async def publish(self, req: PublishRequest) -> PublishResult:
        """走完 §06.5.3 的八步；``req.dry_run=True`` 时停在第 ⑥ 步之前。"""

    @abstractmethod
    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        """取一条作品的数据（T5.4 用；一期实现可以抛未实现）。"""


#: 装配工厂：``(ctx) -> Publisher``。注册表只存**类**，实例化由服务层按账号做 ——
#: 同一个平台的两个账号是两份登录态，不能共用一个实例（§06.2.4「登录态隔离」）。
PublisherFactory = Callable[[PublisherContext], Publisher]

#: 平台实现注册表（§4.6.1）。由 ``publish/platforms/*.py`` 在导入时登记。
PUBLISHERS: dict[str, type[Publisher]] = {}

#: 演练台发布器的代号（T5.9）。它**不是平台**：
#: :class:`~studio.publish.platforms.fixture.FixturePublisher` 只打本地靶页，发不出去任何东西。
#: 放这里是因为有三处要认它（投递的默认目标、池的开关守卫、契约测试），
#: 而三处各写一遍字面量会在改名时漏掉一处 —— 漏掉的那处表现为"任务发完顺手多了一条演练发布"。
REHEARSAL_PUBLISHER: Final[str] = "fixture"


def register_publisher(cls: type[Publisher]) -> type[Publisher]:
    """把一个 Publisher 子类登记进 :data:`PUBLISHERS`（装饰器）。"""
    code = getattr(cls, "platform", "")
    if not code:
        raise PublishError(
            f"{cls.__name__} 没有声明 platform",
            code=ErrorCode.PUBLISH_FAILED,
            remediation="给子类加 platform: ClassVar[str] = '<平台代号>'",
        )
    existing = PUBLISHERS.get(code)
    if existing is not None and existing is not cls:
        raise PublishError(
            f"平台 {code} 被登记了两次：{existing.__name__} / {cls.__name__}",
            code=ErrorCode.PUBLISH_FAILED,
            remediation="一个平台只允许一个 Publisher 实现",
        )
    PUBLISHERS[code] = cls
    return cls


def get_publisher(platform: str) -> type[Publisher]:
    """按平台代号取实现；没有 ⇒ 抛 ``PUBLISH_NOT_IMPLEMENTED``。"""
    try:
        return PUBLISHERS[platform]
    except KeyError:
        raise PublishError(
            f"没有 {platform} 的发布实现",
            code=ErrorCode.PUBLISH_NOT_IMPLEMENTED,
            context={"platform": platform, "known": sorted(PUBLISHERS)},
            remediation="一期只实现 douyin / kuaishou / shipinhao（§06.2.1）",
        ) from None
