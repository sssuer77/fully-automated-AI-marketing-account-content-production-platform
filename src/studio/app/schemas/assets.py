"""素材库契约（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。

面板要回答的五个问题，一个模型对一块
------------------------------------
① 「库里有什么、够不够用？」 ⇒ :class:`AssetLibraryModel`（三类分节 + ``shortfall``）
② 「盘上还有没入库的吗、坏在哪？」 ⇒ :class:`ScanReportModel`（逐条 ``check``）
③ 「这条到底能不能用、为什么不能？」 ⇒ :class:`AssetCheckModel` / :class:`MediaInfoModel`
④ 「启用还是停用、授权怎么填？」 ⇒ :class:`AssetPatchRequest`
⑤ 「素材长什么样？」 ⇒ 缩略图 / 原文件两个 GET（二进制，不走 JSON）

为什么条目是**按 kind 判别的联合**，而不是一个大而全的模型
----------------------------------------------------------
三类素材的字段**本来就不同**（跑酷有 ``has_text`` / 可用区间，BGM 有 ``loudness_lufs`` /
``bpm``，音色有 ``ref_count`` / ``peak_db``）。摊平成「全字段可空」的单一模型，前端就再也
分不清「这个字段对这条素材没意义」与「这个字段恰好是空的」—— 而面板要按 kind 画完全
不同的列。``Field(discriminator="kind")`` 让这条边界在 OpenAPI 里就是一个判别联合。

请求体一律 ``extra="forbid"``
-----------------------------
把 ``usable_from_ms`` 写成 ``usableFrom`` 被静默忽略，人会以为「区间已经标好了」，
而出片时抽到的还是整段 —— 这种「以为生效了」最贵（与人物库 / 合成配置同一条）。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.app.schemas.common import MAX_REASON
from studio.assets.layout import AssetKind

__all__ = [
    "AssetCheckModel",
    "AssetIngestRequest",
    "AssetItemModel",
    "AssetKindSectionModel",
    "AssetKindStatsModel",
    "AssetLibraryModel",
    "AssetPatchRequest",
    "AssetStatsModel",
    "AssetStatsResponse",
    "BgmItemModel",
    "BrollItemModel",
    "MediaInfoModel",
    "PendingAssetModel",
    "ProblemModel",
    "ScanCountsModel",
    "ScanReportModel",
    "ScanSectionModel",
    "ScanTotalsModel",
    "ScannedAssetModel",
    "VoiceItemModel",
]


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    """响应基类（字段全部显式声明）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读：体检结论
# ══════════════════════════════════════════════════════════════════════


class ProblemModel(_Response):
    """一条不合格 / 提醒。``code`` 机器可读（面板据此分类），``message`` 给人看。"""

    code: str
    message: str


class MediaInfoModel(_Response):
    """``ffprobe`` 的结论（``MediaInfo.to_dict()``）。

    ``duration_ms`` / ``width`` 这类字段是 ``None`` 而不是 0：图片没有时长、
    纯音频没有宽高，写 0 会让面板把「不适用」画成「零」。
    """

    path: str
    size_bytes: int
    duration_ms: int | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    pix_fmt: str | None
    sample_rate: int | None
    channels: int | None
    bitrate_kbps: int | None
    has_alpha: bool
    is_image: bool


class AssetCheckModel(_Response):
    """一个素材的体检结论（``ok=false`` 时 ``problems`` 逐条给原因）。

    ``problems`` 与 ``warnings`` **分开**：前者是拒绝入库的判据，后者是「能用，
    但你会想知道的」。混在一起，面板就没法只用一个红点表达「这条到底进没进库」。
    """

    kind: str
    id: str
    ok: bool
    problems: list[ProblemModel]
    warnings: list[ProblemModel]
    info: MediaInfoModel | None
    segments: list[MediaInfoModel]
    peaks: list[float | None]


# ══════════════════════════════════════════════════════════════════════
# 读：库里有什么
# ══════════════════════════════════════════════════════════════════════


class BrollItemModel(_Response):
    """一条跑酷素材（``kind`` 是判别字段，下同）。"""

    kind: Literal["broll"]
    id: str
    path: str
    duration_ms: int
    usable_from_ms: int
    usable_to_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    has_text: bool
    tags: list[str]
    license: str
    thumb_path: str | None
    source_url: str | None
    proof_path: str | None
    licensed_to: str | None
    use_count: int
    last_used_at: str | None
    enabled: bool
    on_disk: bool
    created_at: str | None


class BgmItemModel(_Response):
    """一条 BGM。"""

    kind: Literal["bgm"]
    id: str
    path: str
    duration_ms: int
    sample_rate: int | None
    channels: int | None
    bitrate_kbps: int | None
    loudness_lufs: float | None
    bpm: float | None
    mood: str | None
    tags: list[str]
    loopable: bool
    license: str
    source_url: str | None
    proof_path: str | None
    licensed_to: str | None
    use_count: int
    last_used_at: str | None
    enabled: bool
    on_disk: bool
    created_at: str | None


class VoiceItemModel(_Response):
    """一个零样本音色（``path`` 是**目录**：``ref_NN`` + ``ref.txt`` + ``profile.json``）。"""

    kind: Literal["voice"]
    id: str
    path: str
    ref_count: int
    total_duration_ms: int
    sample_rate: int | None
    peak_db: float | None
    text_path: str | None
    proof_path: str | None
    license: str | None
    source_url: str | None
    licensed_to: str | None
    use_count: int
    last_used_at: str | None
    enabled: bool
    on_disk: bool
    created_at: str | None


#: 三类条目的判别联合（``kind`` 一出现，前端就能收窄到具体那一支）
AssetItemModel = Annotated[BrollItemModel | BgmItemModel | VoiceItemModel, Field(discriminator="kind")]


class AssetStatsModel(_Response):
    """一类素材的家底。

    ``total_duration_ms`` 与 ``enabled_duration_ms`` **分开报**：随机化只用启用的那些，
    而「我到底有多少素材」是另一个问题。合成一个数，用户停用一批之后就再也说不清
    「是素材真的不够，还是我把它们关掉了」。
    """

    total: int
    enabled: int
    total_duration_ms: int
    enabled_duration_ms: int


class PendingAssetModel(_Response):
    """盘上有、库里没有的一条素材（面板上「还没入库」那一档）。

    它**照样会被出片挑到**（``render/assets.py`` 的口径是"能进目录就算数"），
    缺的只是留痕：授权、时长、指纹、缩略图都还没登记。面板把它列出来 + 给一个
    入库入口 —— 一个隐形但会被用到的素材，正是"面板与出片各说各话"的另一半。
    """

    kind: str
    id: str
    path: str


class AssetKindSectionModel(_Response):
    """一类素材的「库里有什么 + 盘上有什么」。

    两组数字**分开报**是有意的：``stats`` 是**库**的家底，``disk_total`` / ``pending``
    是**盘**的事实。它们对不上是常态（刚丢进去还没入库、入了库又被人删了文件），
    合成一个数就会让"到底是哪一种"再也说不清。

    ``usable`` 是面板上**唯一一个与出片同口径**的数字（出片真能挑到的条数），
    ``degraded`` / ``shortfall`` 都由它算出来。
    """

    kind: str
    root: str
    stats: AssetStatsModel
    items: list[AssetItemModel]
    shortfall: str | None
    disk_total: int
    pending: list[PendingAssetModel]
    strays: list[str]
    root_missing: bool
    usable: int


class AssetLibraryModel(_Response):
    """素材库全貌（面板首屏就这一个请求）。

    ``degraded=true`` ⇒ 跑酷素材**一条都挑不到**（目录是空的 / 全被停用），出片会走
    **黑屏降级**（``note`` 说清原因）。

    它**只**回答"出片会不会真的黑屏"，不回答"素材够不够多" —— 后者是 ``shortfall``。
    两者混在一起的后果是面板上写着"当前为黑屏降级模式"，而片子里正放着跑酷：同一句
    谎话换了个说法（裁定：判据与建议分开）。
    """

    degraded: bool
    note: str | None
    sections: list[AssetKindSectionModel]


class AssetKindStatsModel(_Response):
    """一类素材的家底（**不带条目**：总览台只要数字，不该为了几行数把整库拖过来）。

    ``usable`` / ``disk_total`` 与 ``GET /assets`` 是同一套口径（出片真能挑到几条、
    盘上共有几条），总览台的「素材够不够」小卡片据此上色。
    """

    kind: str
    root: str
    stats: AssetStatsModel
    shortfall: str | None
    disk_total: int
    usable: int


class AssetStatsResponse(_Response):
    """``GET /assets/stats``：三类家底 + 够不够用的判据线。

    ``thresholds`` 从 ``studio.assets.validate`` 的常量**现取**（前端不抄第二份）：
    某天把「跑酷 ≥ 60 条」调成 80，面板的进度条跟着变，不需要改前端。
    """

    sections: list[AssetKindStatsModel]
    degraded: bool
    note: str | None
    thresholds: dict[str, Any]
    licenses: list[str]


# ══════════════════════════════════════════════════════════════════════
# 读：扫盘报告
# ══════════════════════════════════════════════════════════════════════


class ScannedAssetModel(_Response):
    """扫盘报告里的一条素材（盘上发现的）。

    ``action=None`` 表示**这次没写库**（入库过程中失败）—— 与 ``unchanged``（比对过、
    没变化）是两件事，面板上一个是灰的、一个是红的。
    """

    kind: str
    id: str
    path: str
    stored: bool
    enabled: bool | None
    usable: bool
    action: str | None
    note: str | None
    thumb_path: str | None
    check: AssetCheckModel


class ScanCountsModel(_Response):
    """一类素材这一趟干了什么（面板顶部那行「新增 3 / 刷新 1 / 拒绝 2」）。"""

    found: int
    created: int
    refreshed: int
    unchanged: int
    duplicate: int
    rejected: int
    strays: int
    missing: int


class ScanSectionModel(_Response):
    """一类素材的扫盘结果。

    ``strays`` 是「目录里存在、但命名不合规」的文件：**如实报出来，绝不悄悄忽略**
    —— 用户把 ``跑酷1.mp4`` 丢进去，看到的应该是「它没被认出来，改名成
    ``parkour_xxx.mp4``」，而不是「扫了 0 条」。
    """

    kind: str
    root: str
    root_missing: bool
    assets: list[ScannedAssetModel]
    strays: list[str]
    missing: list[str]
    stats: AssetStatsModel
    counts: ScanCountsModel


class ScanTotalsModel(_Response):
    """三类的合计（``GET /assets/stats`` 与入库回执共用同一套口径）。"""

    created: int
    refreshed: int
    unchanged: int
    duplicate: int
    rejected: int
    missing: int
    strays: int


class ScanReportModel(_Response):
    """一次扫盘（``dry_run=true``）或一次入库（``dry_run=false``）的完整结论。

    **扫盘是只读的**：``dry_run=true`` 一个字节都不写库，面板「先看看会怎样」用它。
    """

    dry_run: bool
    license: str | None
    sections: list[ScanSectionModel]
    totals: ScanTotalsModel


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════


class AssetIngestRequest(_Body):
    """扫盘 / 入库请求。

    ``kind`` 留空 = 三类都扫（面板「扫全部」按钮）。``ids`` 留空 = 该目录全扫，
    给了 = 只处理这几条（「重扫这一条」按钮，坏文件修好之后不用整批重来）。

    ``license`` 只对**本次新入库**的条目生效；已入库的按行里存的那份走 ——
    否则一次「顺手全扫」会把每条素材的授权都改成本次请求里填的那个。
    """

    kind: AssetKind | None = None
    ids: list[str] | None = None
    license: str | None = None
    dry_run: bool = False
    reason: str | None = Field(default=None, max_length=MAX_REASON)


class AssetPatchRequest(_Body):
    """改一条素材（启用 / 停用 / 授权 / 标签 / 可用区间 / 情绪…）。

    ``kind`` 可以不给：服务层按 id 跨类反查（同一个 id 在两类里都有 ⇒ 明确报错，
    绝不猜一个然后改错行）。

    字段级约束（``license`` 的枚举、可用区间非空、``usable_from_ms ≥ 0``）**不在这里**：
    它们住在服务层 :func:`~studio.services.asset_service._clean_fields`。请求体再抄一份，
    就是「面板放行、保存却 422」的来源（裁定：真判定只有一次）。
    """

    kind: AssetKind | None = None
    enabled: bool | None = None
    license: str | None = None
    tags: list[str] | None = None
    has_text: bool | None = None
    usable_from_ms: int | None = None
    usable_to_ms: int | None = None
    mood: str | None = None
    bpm: float | None = None
    loopable: bool | None = None
    source_url: str | None = None
    proof_path: str | None = None
    licensed_to: str | None = None
    text_path: str | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    def changes(self) -> dict[str, Any]:
        """只把**显式给了**的字段交出去。

        不这么做的话，「面板只点了停用、却把整行发上来」会把没动过的字段也重写一遍
        —— 而重写用的是**提交那一刻的旧值**，两个标签页同时开着就会互相覆盖。
        """
        return self.model_dump(exclude={"reason"}, exclude_none=True)
