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

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.app.schemas.common import MAX_REASON
from studio.assets.layout import AssetKind

__all__ = [
    "NULLABLE_PATCH_FIELDS",
    "AssetCheckModel",
    "AssetIngestRequest",
    "AssetItemModel",
    "AssetKindSectionModel",
    "AssetKindStatsModel",
    "AssetLibraryModel",
    "AssetPageModel",
    "AssetPatchRequest",
    "AssetPruneRequest",
    "AssetStatsModel",
    "AssetStatsResponse",
    "BgmItemModel",
    "BrollItemModel",
    "KeptOrphanModel",
    "MediaInfoModel",
    "PendingAssetModel",
    "ProblemModel",
    "PruneReportModel",
    "PrunedOrphanModel",
    "ScanCountsModel",
    "ScanReportModel",
    "ScanSectionModel",
    "ScanTotalsModel",
    "ScannedAssetModel",
    "UploadResultModel",
    "UploadedFileModel",
    "VoiceItemModel",
    "VoiceSegmentModel",
    "VoiceSegmentRemovalModel",
    "VoiceSegmentsModel",
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


class AssetPageModel(_Response):
    """一类素材的**一页**（T4.8：跑酷 / 音色 / BGM **各一个菜单**，各翻各的页）。

    为什么不是一个菜单看三类
    ------------------------
    三类素材的可管理字段**本来就不一样**（跑酷有可用区间与 `has_text`，BGM 有
    `bpm` / `mood` / `loopable`，音色有 `ref_count` / `text_path`），放在一屏里
    只能把三类字段摊成一张"大部分格子是空的"大表。分菜单之后，每一屏的表头与编辑器
    都只画这一类真正有的东西。

    ``stats`` 与 ``total`` **是两个数**，不能合并
    -------------------------------------------
    - ``stats``：这一类的**家底**（库里有几条 / 启用几条 / 共多少时长），**不受筛选影响**；
    - ``total``：**这一页所在的筛选结果**有几条。

    合成一个数的后果很具体：筛出 3 条时面板会说"这一类只有 3 条素材"，而库里明明有
    60 条 —— 用户接着就去补素材了。

    ``page`` 是**服务端钳过**的页码：翻过头（比如最后一页被删空了）返回的是最后一页，
    而不是一页空白。前端照着它画页码，不要自己算。
    """

    kind: str
    root: str
    root_missing: bool
    items: list[AssetItemModel]
    stats: AssetStatsModel
    usable: int
    disk_total: int
    shortfall: str | None
    pending: list[PendingAssetModel]
    strays: list[str]
    total: int
    page: int
    page_size: int
    pages: int


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
# 写：浏览器上传（T4.8 的图形化入库入口）
# ══════════════════════════════════════════════════════════════════════


class UploadedFileModel(_Response):
    """一个上传文件的结局（面板逐条画一行）。

    ``status`` 只有三种，正好对应面板上的三种颜色：

    - ``stored``：新写进去的；
    - ``replaced``：覆盖了同名的旧文件（**只在用户勾了「覆盖同名」时才会出现**）；
    - ``skipped``：一个字节都没写，``message`` 说清为什么（扩展名不对 / 目标已存在…）。

    ``asset_id`` 是**落盘之后**的素材 id，不是原始文件名 —— 上传会把
    ``跑酷 01.MP4`` 规范成 ``parkour_01``，面板要显示后者：否则用户回头在库里
    按刚传的那个名字找不到东西。

    ``message`` 里带上"原名 ⇒ 新名"这件事：改名是可以的，**静默**改名不行。
    """

    filename: str
    asset_id: str | None
    status: Literal["stored", "replaced", "skipped"]
    message: str


class UploadResultModel(_Response):
    """一次上传的回执：逐文件结局 + 这一趟的入库报告。

    ``report`` 可以缺席（``None``）：一个字节都没落盘时没有什么可入库的，此时回一份
    空的 ``ScanReportModel`` 会假装「扫过了」（而它的 ``missing`` 字段还会把整个库
    列成「不见了」）。

    ``removed`` / ``notes`` 是**覆盖语义**的账（裁定 381）：勾了「覆盖同名」之后，
    盘上没被这次写到的东西会被清掉 —— 清掉了什么（``removed``）与「什么没动但你
    应该知道」（``notes``）都要报出来，否则用户看到的是「我覆盖了，可它还是 3 段」。
    """

    kind: str
    root: str
    overwrite: bool
    files: list[UploadedFileModel]
    stored: int
    replaced: int
    skipped: int
    #: 覆盖上传时**顺带清掉**的旧段（裁定 381）。它们不在 ``files`` 里 —— 那不是这次
    #: 传上来的东西，混进去会让人以为「我传了它」。
    removed: list[str]
    #: 这次没写、但用户**必须知道**的事（比如「ref.txt 没动，还是上一次那份」）。
    notes: list[str]
    report: ScanReportModel | None


# ══════════════════════════════════════════════════════════════════════
# 读 / 写：音色的逐段管理（裁定 381）
# ══════════════════════════════════════════════════════════════════════


class VoiceSegmentModel(_Response):
    """一个音色里的一段参考音（逐段管理那一屏的一行）。

    ``text`` 是 ``ref.txt`` 里**同一位置**那一行 —— 位置即对应（第 N 行 ↔ 第 N 段）。
    它是 ``None`` 就说明这一段没有对应文本，克隆质量会打折。
    """

    index: int
    name: str
    duration_ms: int | None
    sample_rate: int | None
    peak_db: float | None
    text: str | None
    usable: bool
    problems: list[ProblemModel]


class VoiceSegmentsModel(_Response):
    """一个音色目录的逐段现状。

    ``ref_count`` 与 ``text_lines`` 分开报：两者不等就是「文本与参考音对不上」，
    面板要能一眼指出是哪一段对不上（而不是只显示一句「有 warning」）。
    """

    voice_id: str
    root: str
    ref_count: int
    text_lines: int
    segments: list[VoiceSegmentModel]
    problems: list[ProblemModel]
    warnings: list[ProblemModel]
    enabled: bool | None
    in_library: bool


class VoiceSegmentRemovalModel(_Response):
    """删掉一段参考音的结局（**重编号是这件事的一部分**，所以必须报出来）。"""

    voice_id: str
    removed: str
    removed_text: str | None
    renamed: list[dict[str, str]]
    text_rewritten: bool
    notes: list[str]
    segments: VoiceSegmentsModel


class AssetDeleteModel(_Response):
    """一次删除的结局（**库里那一行没了**，盘上那份要看 ``purged``）。

    ``purge`` 是**请求里**那个开关，``purged`` 是**真的从盘上删掉的路径**。两者
    分开报，因为可以不一致：``purge=true`` 但文件本来就不在盘上 ⇒ 前者 ``true``、
    后者是空表。面板上写"盘上文件已删除"而其实什么都没删，与写"已移除"而盘上
    还留着一个目录，是同一类谎话。
    """

    kind: str
    id: str
    purge: bool
    purged: list[str]


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


#: 显式传 ``null`` 等于**清空**的字段（其余字段的 ``null`` 一律当成"没给"）。
#:
#: 这份名单是"这个字段能不能空"的**唯一**一处声明：``null`` 落进 SQL 之前，只有
#: 名单里的字段允许它是 ``None``。名单外的字段（``enabled`` / ``has_text`` /
#: ``loopable``）在 DDL 里是 ``INTEGER NOT NULL CHECK IN (0,1)`` —— 把 ``None``
#: 交给它们换来的是一条 500，而不是一句"这个字段不能清空"。
NULLABLE_PATCH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "license",
        "tags",
        "mood",
        "bpm",
        "usable_to_ms",
        "source_url",
        "proof_path",
        "licensed_to",
    }
)


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

        ``kind`` **不进** ``changes()``：它是**选路**用的（同一个 id 在两类里都有时，
        靠它决定改哪一行 —— 见 ``AssetService.resolve``），不是表里的一列。把它一起交
        下去，仓储层会把它当成"不可改的字段"拒掉（``_patch`` 的白名单），于是
        **面板上每一次启停 / 改授权都变成 422** —— 而面板正是每次都带着 ``kind`` 的
        （它得防"两类同名"那件事）。所以在这里就把选路字段摘掉。

        为什么是 ``exclude_unset`` 而不是 ``exclude_none``
        ------------------------------------------------
        ``exclude_none`` 把「字段没给」与「字段给了 ``null``」当成同一件事 —— 而这两件事
        在 PATCH 里**正好相反**：前者是"别动它"，后者是"把它清掉"。用 ``exclude_none``
        的后果很具体：面板上把「来源地址」那一框擦干净、点保存，**什么都没发生**，
        用户看到的是"改了、也保存了、值还在"，于是以为是自己没点到。
        （``usable_to_ms`` 留空 = 到片尾、``bpm`` 清空 = 没有 BPM，走的是同一条路。）

        ``exclude_unset`` 只交**请求里真的出现过**的键，于是 ``null`` 能落库。剩下的
        一件事是把"不该为空的字段"上的 ``null`` 摘掉 —— 名单见
        :data:`NULLABLE_PATCH_FIELDS`。
        """
        payload = self.model_dump(exclude={"kind", "reason"}, exclude_unset=True)
        return {
            key: value for key, value in payload.items() if value is not None or key in NULLABLE_PATCH_FIELDS
        }


# ══════════════════════════════════════════════════════════════════════
# 写：孤儿清理（裁定 384）
# ══════════════════════════════════════════════════════════════════════


class PrunedOrphanModel(_Response):
    """一个被清掉的孤儿（``problems`` 是它该被清的理由，人话）。"""

    kind: str
    id: str
    path: str
    problems: list[str]


class KeptOrphanModel(_Response):
    """一个**没被清**的孤儿，以及为什么留着。"""

    kind: str
    id: str
    path: str
    reason: str


class PruneReportModel(_Response):
    """一次孤儿清理的结论。

    ``removed`` / ``kept`` / ``strays`` 分开报的理由见服务层
    :class:`~studio.services.asset_service.PruneReport`：三种"盘上有、库里没有"
    该做的动作完全不同，合成一个数字，面板就只能说"清掉 3 个" —— 而其中两个
    可能是**该入库**的。
    """

    kind: str
    dry_run: bool
    removed: list[PrunedOrphanModel]
    kept: list[KeptOrphanModel]
    strays: list[str]


class AssetPruneRequest(_Body):
    """孤儿清理请求。

    ``kind`` **必填**（与 :class:`AssetIngestRequest` 的"留空 = 三类都扫"刻意不同）：
    这个动作会删盘上的东西，而"删哪一类"必须由人说出来。给一个留空的默认值，等于
    让一次手滑的请求在三类目录里同时动手 —— 而扫盘留空的代价只是多读两个目录。

    ``dry_run=true`` ⇒ 只报"会清掉哪些"，一个字节都不动（面板点第一下用它）。
    """

    kind: AssetKind
    dry_run: bool = False
