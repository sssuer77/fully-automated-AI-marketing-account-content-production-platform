"""发布操作面契约（T5.3 · §06.5.4 / §06.10）。

这一层回答三个问题，一个模型对一块
----------------------------------
① "有哪些发布记录、现在都什么状态？" ⇒ :class:`PublicationList`（六状态计数 + 各若干条）
② "这条现在能不能重试 / 取消？" ⇒ :class:`PublicationView` 的 ``can_*`` 三个布尔
③ "我刚才那一下的结果是什么？" ⇒ :class:`PublishActionResponse`

为什么 ``can_retry`` / ``can_cancel`` / ``can_mark_done`` 由服务端算
-------------------------------------------------------------------
判据（``published`` 不能取消、``manual_required`` 才能标记已处理……）是**服务端**的
状态机规则。发给面板三个布尔，比让前端记住"哪些状态能点"可靠 —— 规则改一次就漏一处，
而漏的那一处会变成"点了按钮报 400"，用户看到的是"这个按钮坏了"。

为什么 ``evidence`` 里的路径**原样**给出
----------------------------------------
它指的是盘上那份截图 / DOM 快照，面板要用它开一个可点开的预览（与渲染面板的
``/render/videos/{name}`` 同一条）。后端只保证"这个字符串是发布器当时写下来的"，
不去猜它现在还在不在 —— 文件可能已经被 GC 收走了，那时面板显示"打不开"才是诚实的。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from studio.db.repositories.publication_repo import (
    CANCELED,
    MANUAL_REQUIRED,
    PUBLISHED,
    PublicationRow,
)
from studio.services.publish_service import PlatformOption

__all__ = [
    "NO_PUBLICATION_HINT",
    "ComplianceItemView",
    "ComplianceView",
    "HandoffItemView",
    "HandoffPreview",
    "HandoffResponse",
    "MemorySinkView",
    "MetricsTickView",
    "PublicationList",
    "PublicationView",
    "PublishAccountOutcome",
    "PublishAccountRequest",
    "PublishAccountView",
    "PublishAccountsView",
    "PublishActionRequest",
    "PublishActionResponse",
    "PublishCoverOutcome",
    "PublishCoverRequest",
    "PublishEnqueueRequest",
    "PublishEnqueueResponse",
    "PublishPlatformOption",
    "PublishPlatformsView",
    "compliance_view",
    "delivery_item_view",
    "handoff_preview",
    "platforms_view",
    "publication_view",
]

#: 一条记录都没有时的提示（面板第一屏直接显示它）
NO_PUBLICATION_HINT: str = (
    "还没有发布记录：任务出片后在发布面板点「确认发布」会投递一条，"
    "或跑 studio publish enqueue --task <任务号>（出厂 publish.enabled=false，"
    "投递的作业会带 PUBLISH_DISABLED 进死信（在「四池调度」里看）—— 那是 R14 的"
    "不可逆防护，不是故障）"
)


class PublicationView(BaseModel):
    """一条发布记录（``PublicationRow.to_dict()`` 的展示模型）。"""

    id: str
    task_id: str
    platform: str
    account_id: str
    status: str
    title: str
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
    video_path: str
    cover_path: str | None = None
    profile_key: str | None = None
    dry_run: bool = False
    url: str | None = None
    platform_post_id: str | None = None
    published_at: str | None = None
    scheduled_at: str | None = None
    next_metric_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    #: 时间序列 ``[{at, views, likes, comments, shares}]``（面板画趋势图用 · T5.4）。
    metrics_history: list[dict[str, Any]] = Field(default_factory=list)
    #: 当前时点**失败**了几次（§06.6：顺延重试 ≤3，用尽即停止采集这一条）。
    metric_attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    #: 三个"现在能做什么"（服务端判据，见模块注释）。
    can_retry: bool = False
    can_cancel: bool = False
    can_mark_done: bool = False


class MetricsTickView(BaseModel):
    """跑一轮数据回收的结论（T5.4 · §06.6）。"""

    collected: list[str] = Field(default_factory=list)
    deferred: list[str] = Field(default_factory=list)
    stopped: list[str] = Field(default_factory=list)
    #: 发布池正忙 ⇒ 这一拍什么都没做（**不是失败**，下一拍照跑）。
    yielded: bool = False
    #: 配置里的时点（面板要显示"什么时候会去采"）。
    schedule_hours: list[int] = Field(default_factory=list)
    #: 这一轮之后还有几条到点未采（``yielded`` 时不算 —— 那一条都没动）。
    pending: int = 0


class MemorySinkView(BaseModel):
    """记忆沉淀的结论（§4.6.2 的 ``MemorySinkResult``）。"""

    feedback_items_created: int
    topics_demoted: int
    digest_path: str
    planner_consumable: bool
    comments_seen: int = 0
    low_engagement: bool = False


def publication_view(row: PublicationRow) -> PublicationView:
    """行 ⇒ 展示模型（``can_*`` 三个布尔在这里补上）。"""
    payload = row.to_dict()
    terminal = row.status in (PUBLISHED, CANCELED)
    return PublicationView(
        **payload,
        can_retry=not terminal,
        can_cancel=not terminal,
        can_mark_done=row.status == MANUAL_REQUIRED,
    )


class PublicationList(BaseModel):
    """一份发布面板快照（六状态计数 + 各若干条）。"""

    counts: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, list[PublicationView]] = Field(default_factory=dict)
    #: 只有待人工那几条（T5.3 验收点名的 ``GET /api/v1/publish/queue`` 用它）。
    manual_required: list[PublicationView] = Field(default_factory=list)
    hint: str | None = None


class HandoffItemView(BaseModel):
    """交付包里的一件（预览与打包共用 —— 面板上"七件齐没齐"就是它）。"""

    kind: str
    label: str
    required: bool
    present: bool
    source: str | None = None
    bytes: int | None = None
    note: str | None = None


class HandoffPreview(BaseModel):
    """交付包预览：**还没写任何东西**，只回答"包里会有什么、缺哪件"。"""

    task_id: str
    ready: bool
    items: list[HandoffItemView] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    adapter: str
    output_dir: str
    #: 发布时**自动**交付一份（``app.yaml → handoff.enabled``）。手动导出**不看它**：
    #: 人按下的这一下就是意图本身，开关管的是"自动那条路要不要顺手带一份"。
    auto_on_publish: bool = False
    note: str | None = None


class HandoffResponse(BaseModel):
    """打包结论（包里有什么、落在哪、清单在哪）。"""

    task_id: str
    adapter: str
    root: str
    manifest: str
    copied: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    bytes: int = 0


class ComplianceItemView(BaseModel):
    """一件素材的来源登记（R2 留档的一行）。"""

    kind: str
    id: str
    license: str | None = None
    proof: str | None = None
    proof_present: bool = False
    enabled: bool = True
    note: str | None = None


class ComplianceView(BaseModel):
    """R2 合规留档快照（发布面板与素材库**共用**这一份）。"""

    notice: str
    ok: bool
    items: list[ComplianceItemView] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


def delivery_item_view(item: Any) -> HandoffItemView:
    """``DeliveryItem`` ⇒ 展示模型（字段名逐字对应，不在这里改语义）。"""
    return HandoffItemView(**item.to_dict())


def compliance_view(snapshot: Any) -> ComplianceView:
    """``ComplianceSnapshot`` ⇒ 展示模型。"""
    return ComplianceView(
        notice=snapshot.notice,
        ok=snapshot.ok,
        items=[ComplianceItemView(**item.to_dict()) for item in snapshot.items],
        gaps=list(snapshot.gaps),
    )


def handoff_preview(package: Any, *, adapter: str, output_dir: str, auto: bool) -> HandoffPreview:
    """``DeliveryPackage`` ⇒ 面板要的那一份预览。"""
    return HandoffPreview(
        task_id=package.task_id,
        ready=package.ready,
        items=[delivery_item_view(item) for item in package.items],
        missing=[item.kind for item in package.missing],
        adapter=adapter,
        output_dir=output_dir,
        auto_on_publish=auto,
        note=None if package.ready else "必需的那一件（成片）还不在盘上 —— 先把这条任务渲染出来",
    )


class PublishPlatformOption(BaseModel):
    """投递面板上的一个平台选项（T5.10）。

    ``selectable`` 与 ``note`` **由服务端算**：判据（平台启用 / 这个平台有没有启用账号）
    与投递期跳过它的那两条是同一套。面板自己再判一遍的代价是"显示点得动、点了被跳过"。

    ``calibration`` / ``calibration_note`` / ``known_gaps``（T5.14）同一条理由：
    "这个平台的选择器验过没有"是**文件里的一份事实**（``selectors/<x>.yaml``），
    面板要显示它，而**不许**自己猜。四个状态见
    :data:`~studio.services.publish_service.CALIBRATED` 那四个常量。
    """

    code: str
    publisher: str
    enabled: bool
    #: 本地演练台（发到本机靶页，不是真平台）。
    rehearsal: bool = False
    accounts: list[str] = Field(default_factory=list)
    selectable: bool = False
    note: str = ""
    #: ``calibrated`` / ``uncalibrated`` / ``broken`` / ``n/a``（七份 pack 里六份是
    #: ``uncalibrated``：它们是真实现，但那些 CSS 没在真机上验过）。
    calibration: str = ""
    calibration_note: str = ""
    #: 这份 pack 已经确认没做的部分（比如 B 站的必选分区）。
    known_gaps: list[str] = Field(default_factory=list)


class PublishPlatformsView(BaseModel):
    """投递面板的选项清单（T5.10）。"""

    items: list[PublishPlatformOption] = Field(default_factory=list)
    #: **一个平台都不选**时后端会投哪几个（顺序 = 投递顺序）。出厂就一个 ``douyin``，
    #: 演练台不在里面（T5.9）—— 面板要把这句话显示出来，否则"不选"看起来像"都不发"。
    default_platforms: list[str] = Field(default_factory=list)
    #: ``config/publish.yaml → enabled``。出厂 ``false``（R14）：投真平台会**直接死信**
    #: （不是转人工，见 :func:`~studio.services.publish_service.platform_options`）。
    #: 面板拿它来在"选了真平台"时给出显眼的警告，而不是等人投完发现面板上什么都没有。
    publish_enabled: bool = False
    #: ``config/publish.yaml → dry_run``：投递不带 ``dry_run`` 时的缺省。
    dry_run: bool = False


def platforms_view(
    options: Sequence[PlatformOption],
    *,
    defaults: Sequence[str],
    publish_enabled: bool = False,
    dry_run: bool = False,
) -> PublishPlatformsView:
    """服务层的选项清单 ⇒ 面板要的那一份。"""
    return PublishPlatformsView(
        items=[PublishPlatformOption(**option.to_dict()) for option in options],
        default_platforms=list(defaults),
        publish_enabled=publish_enabled,
        dry_run=dry_run,
    )


class PublishCoverRequest(BaseModel):
    """出封面的请求体（T5.1 追加）。"""

    use_agent: bool = Field(
        default=True,
        description="走 Cover Agent 写封面文案；false = 直接用稿件标题兜底（离线 / 省钱 / 复现同一张图）",
    )


class PublishCoverOutcome(BaseModel):
    """一张封面的结论（§06.3）。

    ``ok=False`` **不是错误**：封面是可选装饰，没有它就用平台首帧 —— 契约里写明的
    合法结局。所以这一屏回 200 + 一份 ``plan``，而不是 4xx；真正的用法错误（任务号
    不存在）才抛。

    ``plan`` 原样下发（与渲染面板的 ``manifest`` 同一条）：面板要能回答"这张封面为什么
    长这样" —— 抽的是哪一帧、字号被缩过吗、人物贴图用的是哪一层。面板自己再拼一遍
    这些结论，就等于同一件事有两份口径。
    """

    task_id: str
    ok: bool = False
    cover_path: str | None = None
    frame_at_ms: int = 0
    duration_ms: int = 0
    #: 文案从哪来：``agent``（模型）/ ``rule``（规则兜底）。
    source: str = ""
    #: 抽帧失败、退成纯色底了吗（§06.3 的第一级降级）。
    fallback_background: bool = False
    #: 模型那条路为什么没走通（``None`` = 走通了 / 没试）。
    agent_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    plan: dict[str, Any] = Field(default_factory=dict)


class PublishEnqueueRequest(BaseModel):
    """投递请求：把这条任务排进发布池。"""

    platforms: list[str] | None = Field(default=None, description="目标平台；缺省 = 所有启用账号所在的平台")
    account_ids: list[str] | None = Field(
        default=None,
        description="指定账号（可多个）；缺省 = 这些平台下的**全部**启用账号（T5.8 矩阵分发）",
    )
    dry_run: bool | None = Field(
        default=None, description="演练（走完前七步停在第 ⑥ 步之前）；缺省 = 跟随 publish.yaml"
    )
    scheduled_at: str | None = Field(default=None, description="定时发布时刻（T5.6 用）")


class PublishEnqueueResponse(BaseModel):
    """投递结论。"""

    task_id: str
    platforms: list[str] = Field(default_factory=list)
    queued: int = 0
    #: 没投出去的目标及原因（``"douyin/acc_b：已经投过（幂等命中）"`` —— 带账号，
    #: 同一个平台上两个账号的两种命运分得开）。
    skipped: list[str] = Field(default_factory=list)
    #: 其中"早就投过"的那些目标（幂等命中）。单独列出来是因为它的处置动作是**什么都不用做**，
    #: 而其余跳过要人去改配置 —— 让调用方去猜那句中文，等于把"这不是故障"绑在一句文案上。
    duplicates: list[str] = Field(default_factory=list)
    missing: bool = False


class PublishActionRequest(BaseModel):
    """人工处置的请求体（重试 / 取消 / 标记已人工处理共用）。"""

    actor: str = Field(default="user", max_length=32)
    actor_ref: str | None = Field(default=None, max_length=64, description="操作人标识（写进留痕）")
    reason: str | None = Field(default=None, max_length=500, description="说明（标记已处理时必填）")


class PublishActionResponse(BaseModel):
    """人工处置的结论。"""

    publication: PublicationView
    action: str
    #: 一句人话：这条动作**到底改了什么**（面板直接显示，不必自己拼）。
    message: str
    job_changed: bool = False


class PublishAssistOutcome(BaseModel):
    """「人工过验证」的结论（T6.4 · 真机 2026-09-23）。

    与 :class:`PublishActionResponse` 同一个形状 + 一个 ``waited_sec``：这一个动作
    **会开一个浏览器窗口、并且可能等上十几分钟**，而"它到底在干什么"是用户唯一
    拿得到的信息 —— 等了多久是这个形状里最该有的那个数（扫码登录那一条同理）。

    没有 ``job_changed``：这条路**不碰作业队列**（它直接对着这条记录发），
    留着那个字段会让人以为它重排了什么。
    """

    publication: PublicationView
    action: str = "assist"
    message: str
    #: 这一次从开窗口到收场等了多久（秒）。
    waited_sec: float = 0.0


# ── 账号区块（T6.4：面板上直接加号 / 改号 / 停用 / 删号）────────────────


class PublishAccountView(BaseModel):
    """一个发布账号：配置里那条 ``accounts[]`` + 它在盘上的**状态**。

    ``profile_dir`` 与 ``runtime_profile_dir`` 分开给，是因为它们**可能不是一个东西**：
    运行期真正用的登录态目录永远是 ``data/browser_profile/<account_id>``
    （``publish/base.py`` 的 ``PublisherContext.profile_dir``），而配置里那一列是
    "登录态必须按账号隔离"的声明。两者不一致时面板要说出来 —— 否则用户改了半天
    那个值、发现毫无效果，又是一次静默失效。
    """

    account_id: str
    platform: str
    display_name: str = ""
    profile_dir: str
    runtime_profile_dir: str
    #: 登录态目录**存在**（⇒ 至少登录过一次）。**不等于**会话仍有效：那要真发一次才知道。
    profile_dir_exists: bool = False
    profile_dir_matches_runtime: bool = True
    enabled: bool = True
    daily_limit: int = 3
    min_gap_min: int = 30
    #: 这一行现在什么状态、下一步该做什么（**服务端算**，面板直接显示）。
    note: str = ""


class PublishAccountsView(BaseModel):
    """账号区块的首屏：账号清单 + 平台清单 + 表单上下限 + 必须说的话。

    平台清单复用投递区块那一份（:class:`PublishPlatformOption`）：账号要挂在平台上，
    而"这个平台现在能不能投"的判据与投递期**必须是同一套** —— 面板上另列一份的代价是
    "给一个不会生效的平台配了账号，而面板显示一切正常"。
    """

    generated_at: str
    config_path: str
    publish_enabled: bool = False
    require_confirm: bool = True
    accounts: list[PublishAccountView] = Field(default_factory=list)
    platforms: list[PublishPlatformOption] = Field(default_factory=list)
    #: 新增账号时 ``profile_dir`` 的默认前缀（``data/browser_profile``）——
    #: 面板不硬编码这个字符串，它随 ``STUDIO_HOME`` 走。
    profile_dir_prefix: str = ""
    #: 表单上下限（**从配置模型上取**，不手抄：手抄的那份迟早与 ``Field(ge=…)`` 分叉，
    #: 而分叉的表现是"面板让填、后端拒收"）。
    limits: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class PublishAccountRequest(BaseModel):
    """新增 / 改写一个账号的请求体。

    ``account_id`` **只从路径来**，这里没有这个字段：身份只有一个来源，
    否则"想改 A、结果新增了 B"这种错法迟早出现。

    这里**不重复**声明上下限（``ge`` / ``le``）：判据只有一处 ——
    ``core.config.AccountConfig`` 上的那几条约束（服务层用它校验，失败 ⇒ 422 +
    ``context.errors``，面板把红字标到输入框上）。两处各写一份的结果是
    "面板按 A 拦、后端按 B 拦"，而用户看到的是"这个框明明填对了还是红的"。
    """

    platform: str
    display_name: str = ""
    #: 留空 ⇒ 服务层按 ``data/browser_profile/<account_id>`` 补上。
    profile_dir: str | None = None
    enabled: bool = True
    daily_limit: int = 3
    min_gap_min: int = 30
    reason: str | None = Field(default=None, max_length=500, description="说明（写进留痕）")


class PublishAccountOutcome(PublishAccountsView):
    """写操作的结论：**刷新后的整屏** + 这一次到底改了什么。

    回整屏而不是只回那一条：面板不必再发一次 GET，也就不存在"两次请求之间显示旧值"
    的那一帧（与设置面板的 ``PUT /settings/llm`` 同一条）。
    """

    changed: bool = False
    created: bool = False
    removed: str | None = None
    account_id: str = ""
    reason: str | None = None


class PublishAccountHealth(BaseModel):
    """一次登录态探测的结论（T6.4 · **面板直接显示，不自己拼**）。

    ``ready`` 与 ``logged_in`` 都留着：前者是"这一趟能用它发了吗"，后者是"平台说
    登录了吗"。``ready=False`` + ``logged_in=False`` 才是"没登录"；探测本身没跑通
    （浏览器起不来 / 页面打不开）走的是 :meth:`PublishHealth.unknown`，那一条也是
    ``ready=False`` —— 两者在 ``hint`` 里分得开（"需人工扫码" vs "探测失败：…"）。
    """

    ready: bool = False
    logged_in: bool = False
    last_check_at: str = ""
    account_name: str | None = None
    hint: str | None = None


class PublishAccountHealthOutcome(PublishAccountsView):
    """探测 / 扫码登录的结论：**刷新后的整屏** + 这一次的 health + 一句"下一步"。

    与 :class:`PublishAccountOutcome` 同一个形状（回整屏而不是只回那一条）：面板
    拿到它就能整块重画，不必再发一次 GET —— 也就不存在"两次请求之间显示旧值"那一帧。
    """

    account_id: str = ""
    #: ``probe``（只看一眼）/ ``login``（开了窗口等人扫码）。
    action: str = ""
    health: PublishAccountHealth | None = None
    #: 一句人话：现在什么状态、下一步做什么（**服务端算**）。
    note: str = ""
    #: 这一次等了多久（秒）。扫码那一步的耗时是用户唯一能看到的"它到底在干什么"。
    waited_sec: float = 0.0
