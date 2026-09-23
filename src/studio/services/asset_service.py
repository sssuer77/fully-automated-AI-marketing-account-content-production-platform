"""素材库服务（T4.8 · §3.3.14 / §4.3.1 / §04.4.5 面板 7）。

扫盘是**读**，入库是**写**，两者共用一条代码路径
------------------------------------------------
:meth:`AssetService.scan` 与 :meth:`AssetService.ingest` 走的是同一个循环
（``dry_run`` 开关），差别只在"要不要落库"。分成两套实现的话，面板预览看到的
"这条能入库"与真正入库时算出来的结论会慢慢分叉 —— 而分叉的那天，
用户看到的是"预览说 60 条都能用，入库完只有 12 条"。

授权从哪来（T4.8 裁定 188）
---------------------------
扫盘**读不出**授权：目录里没有任何字段能说明"这段跑酷是你自己录的"。
所以：

- 已经入库的行 ⇒ 用它自己的 ``license``（重扫不覆盖人的决定，T4.8.3）；
- 还没入库的 ⇒ 用本次请求带的 ``license``；没带就**不入库**，在报告里如实说
  "缺授权"（``license_missing``）。替用户填一个默认值 = 伪造 R2 合规留痕。

音色是唯一的例外：§4.3.1 对音色的前置是 ``profile.json``（来源登记），
授权类型可以缺 —— 缺了就是 NULL，面板如实显示"未登记授权"。

坏文件不中断整批
----------------
逐条捕获（探测失败 / 指纹算不出来 / 缩略图生成失败），把这一条记成"未入库 + 原因"
继续跑下一条。一次扫盘 60 个文件，不能因为第 3 个是截断的 mp4 就什么都不入库。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

from studio.assets import validate
from studio.assets.layout import (
    REF_STEM_PATTERN,
    AssetCandidate,
    AssetKind,
    Discovery,
    discover,
    root_for,
    voice_profile,
    voice_refs,
    voice_text,
)
from studio.assets.upload import ref_name
from studio.assets.validate import (
    BROLL_LIBRARY_MIN_CLIPS,
    BROLL_LIBRARY_MIN_MS,
    AssetCheck,
    Problem,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.files import stream_sha256
from studio.core.logging import get_logger
from studio.core.media import (
    MediaInfo,
    analyze_volume,
    extract_thumbnail,
    measure_loudness,
    probe_media,
)
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db.models import BgmTrackRow, BrollClipRow, VoiceProfileRow
from studio.db.repositories import (
    AssetStats,
    BgmTrackRepo,
    BrollClipRepo,
    IngestAction,
    VoiceProfileRepo,
)
from studio.db.repositories.audit_repo import AuditRepo
from studio.services.log_service import LogSink

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "LICENSES",
    "MAX_PAGE_SIZE",
    "AssetDelete",
    "AssetKindSection",
    "AssetLibrary",
    "AssetPage",
    "AssetService",
    "DisabledAssets",
    "KeptOrphan",
    "PendingAsset",
    "PruneReport",
    "PrunedOrphan",
    "ScanReport",
    "ScanSection",
    "ScannedAsset",
    "check_license",
    "disabled_assets",
    "row_to_dict",
]

logger = get_logger("studio.services.asset")

#: ``system_logs.source``（日志面板按来源过滤时的口径）
LOG_SOURCE: str = "assets"

#: ``audit_ops.target_type``
TARGET_TYPE: str = "asset"

#: 分页的默认每页条数（面板首屏那一页）。
DEFAULT_PAGE_SIZE: int = 20

#: 分页的**上限**。前端给 20 / 50 / 100 三个选项，但入参是任意的 ——
#: 不钳住的话，一个 ``page_size=100000`` 就能让这个接口变成"把整库拖过来"，
#: 而那正是分页要避免的事。
MAX_PAGE_SIZE: int = 200

#: DDL 的 ``license`` CHECK（§3.3.14）。写之前校验：让 CHECK 在运行期炸掉，
#: 等于把"参数写错了"变成"入库失败"，而失败信息里只有一句 SQLite 的约束名。
LICENSES: frozenset[str] = frozenset({"self_recorded", "authorized", "cc0", "purchased"})


def check_license(license: str | None) -> None:
    """授权类型必须落在 :data:`LICENSES` 里（``None`` = 本次不带，交给别处决定）。

    为什么把它提出来当一个模块级函数：上传那条路要在**写盘之前**问清楚这件事。
    写了一半才 422，会在素材目录里留下一批"没人认领"的文件 —— 而它们看上去
    跟正常素材一模一样（这正是上传接口最容易踩的坑）。
    """
    if license is not None and license not in LICENSES:
        raise StudioError(
            f"授权类型不合法：{license}",
            code=ErrorCode.ASSET_INVALID,
            context={"license": license, "allowed": sorted(LICENSES)},
            remediation="从 self_recorded / authorized / cc0 / purchased 里选一个",
        )


_AssetRow = BrollClipRow | BgmTrackRow | VoiceProfileRow
type _AnyRepo = BrollClipRepo | BgmTrackRepo | VoiceProfileRepo


# ══════════════════════════════════════════════════════════════════════
# 库 → 渲染器：唯一一样流过去的东西
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DisabledAssets:
    """库里被**停用**的素材文件名（按类别分）。

    这是这条链路上从库流向渲染器的**全部**信息（见 ``render/assets.py`` 的模块
    docstring）。渲染器不认识素材 id、不碰数据库；它只做一次集合减法。
    """

    clips: frozenset[str] = frozenset()
    bgm: frozenset[str] = frozenset()

    def for_kind(self, kind: AssetKind) -> frozenset[str]:
        """某一类要剔除的文件名（音色不在其中：配音那条路读的是 ``voice_profiles`` 表）。"""
        if kind is AssetKind.BROLL:
            return self.clips
        if kind is AssetKind.BGM:
            return self.bgm
        return frozenset()


def disabled_assets(connection: sqlite3.Connection) -> DisabledAssets:
    """查出"被停用的素材文件名"（跑酷 / BGM 两类）。

    为什么是**文件名**而不是 id、也不是整条路径：渲染器只列目录，库里存的是绝对
    路径（换台机器前缀就不同），能稳定对上的只有文件名。

    为什么取反 ``enabled`` 而不是用 ``list_all(enabled_only=True)``：那个参数问的是
    "启用的有哪些"，这里问的是"**被人明确关掉的有哪些**"，语义相反 —— 少读一条的
    后果是"停用没生效"，那正是这个函数存在的原因。

    读不出来（表还没建 / 连接只读打不开）⇒ 空集合，**不抛**：一个查不到的停用名单
    不该让出片失败，代价只是退回到"能进目录就算数"的老口径。
    """
    try:
        clips = frozenset(
            Path(str(row.path)).name for row in BrollClipRepo(connection).list_all() if not row.enabled
        )
        bgm = frozenset(
            Path(str(row.path)).name for row in BgmTrackRepo(connection).list_all() if not row.enabled
        )
    except sqlite3.Error:
        logger.warning("读停用素材名单失败，本次按「全部可用」处理")
        return DisabledAssets()
    return DisabledAssets(clips=clips, bgm=bgm)


# ══════════════════════════════════════════════════════════════════════
# 扫盘报告
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ScannedAsset:
    """扫盘报告里的一条素材（盘上发现的）。"""

    kind: AssetKind
    id: str
    path: str
    stored: bool
    enabled: bool | None
    check: AssetCheck
    action: IngestAction | None = None
    note: str | None = None
    thumb_path: str | None = None

    @property
    def usable(self) -> bool:
        """能不能入库（体检结论 + 本次动作）—— 面板据此上色。"""
        return self.check.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "path": self.path,
            "stored": self.stored,
            "enabled": self.enabled,
            "usable": self.usable,
            "action": None if self.action is None else str(self.action),
            "note": self.note,
            "thumb_path": self.thumb_path,
            "check": self.check.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScanSection:
    """一类素材的扫盘结果。"""

    kind: AssetKind
    root: str
    root_missing: bool
    assets: tuple[ScannedAsset, ...]
    strays: tuple[str, ...]
    missing: tuple[str, ...]
    stats: AssetStats

    def count(self, action: IngestAction) -> int:
        return sum(1 for item in self.assets if item.action is action)

    @property
    def rejected(self) -> tuple[ScannedAsset, ...]:
        return tuple(item for item in self.assets if not item.check.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "root": self.root,
            "root_missing": self.root_missing,
            "assets": [item.to_dict() for item in self.assets],
            "strays": list(self.strays),
            "missing": list(self.missing),
            "stats": self.stats.to_dict(),
            "counts": {
                "found": len(self.assets),
                "created": self.count(IngestAction.CREATED),
                "refreshed": self.count(IngestAction.REFRESHED),
                "unchanged": self.count(IngestAction.UNCHANGED),
                "duplicate": self.count(IngestAction.DUPLICATE),
                "rejected": len(self.rejected),
                "strays": len(self.strays),
                "missing": len(self.missing),
            },
        }


@dataclass(frozen=True, slots=True)
class ScanReport:
    """一次扫盘（``dry_run=True``）或一次入库（``dry_run=False``）的完整结论。"""

    sections: tuple[ScanSection, ...]
    dry_run: bool
    license: str | None = None

    def section(self, kind: AssetKind) -> ScanSection:
        for item in self.sections:
            if item.kind is kind:
                return item
        raise KeyError(str(kind))

    @property
    def total_created(self) -> int:
        return sum(item.count(IngestAction.CREATED) for item in self.sections)

    @property
    def total_rejected(self) -> int:
        return sum(len(item.rejected) for item in self.sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "license": self.license,
            "sections": [item.to_dict() for item in self.sections],
            "totals": {
                "created": sum(item.count(IngestAction.CREATED) for item in self.sections),
                "refreshed": sum(item.count(IngestAction.REFRESHED) for item in self.sections),
                "unchanged": sum(item.count(IngestAction.UNCHANGED) for item in self.sections),
                "duplicate": sum(item.count(IngestAction.DUPLICATE) for item in self.sections),
                "rejected": self.total_rejected,
                "missing": sum(len(item.missing) for item in self.sections),
                "strays": sum(len(item.strays) for item in self.sections),
            },
        }


# ══════════════════════════════════════════════════════════════════════
# 素材库（库里的家底）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PendingAsset:
    """盘上有、库里没有的一条素材（面板上「还没入库」那一档）。

    它**不是错误**，也**不是不可用**：出片照样会挑到它（``render/assets.py`` 的口径
    是"能进目录就算数"）。它缺的是**留痕** —— 授权、时长、指纹、缩略图都还没登记。
    所以面板要把它显眼地列出来 + 给一个「入库」入口，而不是让它隐形：一个隐形但
    会被出片用到的素材，是"面板与出片各说各话"的另一半。
    """

    kind: AssetKind
    id: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "id": self.id, "path": self.path}


@dataclass(frozen=True, slots=True)
class AssetKindSection:
    """一类素材的"库里有什么 + 盘上有什么"（含够不够用的判定）。

    两组数字**分开报**是有意的：``stats`` 是**库**的家底（入了库、开了开关），
    ``disk_total`` / ``pending`` 是**盘**的事实。它们对不上是常态（刚丢进去还没入库、
    入了库又被人从资源管理器删了），而把两者合成一个数就会让"到底是哪一种"再也说不清。

    :param usable: 出片真能挑到的条数 —— 面板上唯一一个"与出片同口径"的数字。
    :param pending: 盘上有、库里没有的那些（**出片照样会挑到它们**）。
    :param strays: 目录里命名不合规、没被认出来的路径（如实报出，绝不静默忽略）。
    :param root_missing: 素材根目录不在（全新机器 / 还没跑过 ``ensure_runtime_dirs``）。
    """

    kind: AssetKind
    root: str
    stats: AssetStats
    items: tuple[_AssetRow, ...]
    shortfall: str | None = None
    disk_total: int = 0
    pending: tuple[PendingAsset, ...] = ()
    strays: tuple[str, ...] = ()
    root_missing: bool = False
    usable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "root": self.root,
            "stats": self.stats.to_dict(),
            "items": [row_to_dict(item) for item in self.items],
            "shortfall": self.shortfall,
            "disk_total": self.disk_total,
            "pending": [item.to_dict() for item in self.pending],
            "strays": list(self.strays),
            "root_missing": self.root_missing,
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class AssetLibrary:
    """素材库全貌（面板首屏）。"""

    sections: tuple[AssetKindSection, ...]
    degraded: bool
    note: str | None = None

    def section(self, kind: AssetKind) -> AssetKindSection:
        for item in self.sections:
            if item.kind is kind:
                return item
        raise KeyError(str(kind))

    def to_dict(self) -> dict[str, Any]:
        return {
            "degraded": self.degraded,
            "note": self.note,
            "sections": [item.to_dict() for item in self.sections],
        }


@dataclass(frozen=True, slots=True)
class AssetPage:
    """一类素材的**一页**（T4.8：每个类别一个菜单，各自翻各自的页）。

    为什么 ``stats`` 与 ``total`` 是两个数
    --------------------------------------
    ``stats`` 是**这一类的家底**（库里有几条、启用几条、多少时长），**不受筛选影响**；
    ``total`` 是**这一页所在的筛选结果**有几条。合成一个数的后果很具体：筛到 3 条时
    面板会说"这一类只有 3 条素材"，而库里明明有 60 条 —— 用户接着就去补素材了。

    ``items`` 只装**这一页**。上一页 / 下一页的边界由 :meth:`AssetService.page` 钳住：
    翻过头（比如删到只剩一页）返回的是最后一页，而不是一页空白。
    """

    kind: AssetKind
    root: str
    root_missing: bool
    stats: AssetStats
    usable: int
    shortfall: str | None
    disk_total: int
    pending: tuple[PendingAsset, ...]
    strays: tuple[str, ...]
    items: tuple[_AssetRow, ...]
    total: int
    page: int
    page_size: int
    pages: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "root": self.root,
            "root_missing": self.root_missing,
            "stats": self.stats.to_dict(),
            "usable": self.usable,
            "shortfall": self.shortfall,
            "disk_total": self.disk_total,
            "pending": [item.to_dict() for item in self.pending],
            "strays": list(self.strays),
            "items": [row_to_dict(item) for item in self.items],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }


@dataclass(frozen=True, slots=True)
class AssetDelete:
    """一次删除的结局（面板据此把"删了什么"说清楚）。

    ``purged`` 是**真的从盘上删掉的那几个路径**（默认是空的）。它与请求里的
    ``purge`` 分开报，因为两者可以不一致：``purge=true`` 但文件本来就不在盘上
    ⇒ ``purge`` 是 ``true``、``purged`` 是空的。面板上写"已删除盘上文件"而其实
    没删，与写"已从库里移除"而盘上还留着一个目录，是同一类谎话。
    """

    kind: AssetKind
    id: str
    purge: bool
    purged: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "purge": self.purge,
            "purged": list(self.purged),
        }


@dataclass(frozen=True, slots=True)
class _PurgeAttempt:
    """一次「删盘上那份」的尝试：``path`` 与 ``reason`` **恰有一个**非空。

    为什么要一个只有两个字段的小对象，而不是 ``str | None``
    ------------------------------------------------------
    "没删成"有**两个完全不同**的原因，而它们该不该出现在面板上是相反的：
    「盘上已经没有了」是正常的（用户自己在资源管理器里删过），「守卫拒绝」是一件
    要查的事。合成一个 ``None``，调用方就只能二选一 —— 要么把正常情况报成异常，
    要么把异常咽掉。
    """

    path: str | None = None
    reason: str | None = None


#: 体检结论里那些**只说库里的登记状态、不说盘上文件**的 code。
#:
#: 目前只有一条：``license_missing``。它进 ``problems`` 是对的（没有授权的素材不该
#: 入库），但**不能拿它判断"盘上这份该不该删"** —— 孤儿之所以是孤儿，正是因为它还
#: 没有库里那一行，而那一行才是存授权的地方。见 :meth:`AssetService.prune_orphans`。
_REGISTRATION_ONLY_CODES: frozenset[str] = frozenset({"license_missing"})


@dataclass(frozen=True, slots=True)
class PrunedOrphan:
    """一个被清掉的孤儿（``problems`` 是它该被清的理由，人话，面板直接显示）。"""

    kind: AssetKind
    id: str
    path: str
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "path": self.path,
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class KeptOrphan:
    """一个**没被清**的孤儿，以及为什么留着（``reason`` 必须能直接给人看）。"""

    kind: AssetKind
    id: str
    path: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "path": self.path,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PruneReport:
    """一次孤儿清理的结论（裁定 384）。

    ``removed`` / ``kept`` / ``strays`` 三样**必须分开报**，因为它们对应三种
    "盘上有、库里没有"，而用户对它们该做的动作完全不同：

    - ``removed``：本身不合格 ⇒ 已经清掉（这才是这个动作做的事）；
    - ``kept``：本身合格、只是没入库 ⇒ **该入库**。面板要说清是这一种，否则用户会
      以为"清了一遍，怎么还剩着"；
    - ``strays``：名字不合规、压根没被认出来 ⇒ 一个都没动（可能是他自己的原始
      素材），但要如实列出来，让他知道这些**不在**这次清理的范围里。
    """

    kind: AssetKind
    dry_run: bool
    removed: tuple[PrunedOrphan, ...]
    kept: tuple[KeptOrphan, ...]
    strays: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "dry_run": self.dry_run,
            "removed": [item.to_dict() for item in self.removed],
            "kept": [item.to_dict() for item in self.kept],
            "strays": list(self.strays),
        }


@dataclass(frozen=True, slots=True)
class VoiceSegment:
    """音色目录里的**一段**参考音（逐段管理那一屏的一行）。

    ``text`` 是 ``ref.txt`` 里**同一位置**那一行 —— 位置即对应（第 N 行 ↔ 第 N 段），
    不是按文件名里的编号去查。对不上时它是 ``None``，面板据此画"这段没有对应文本"。
    """

    index: int
    name: str
    duration_ms: int | None
    sample_rate: int | None
    peak_db: float | None
    text: str | None
    problems: tuple[Problem, ...]

    @property
    def usable(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "peak_db": self.peak_db,
            "text": self.text,
            "usable": self.usable,
            "problems": [item.to_dict() for item in self.problems],
        }


@dataclass(frozen=True, slots=True)
class VoiceSegments:
    """一个音色目录的**逐段现状**（``GET /assets/voice/{id}/segments``）。

    ``ref_count`` 与 ``text_lines`` **分开报**：两者不等就是"文本与参考音对不上"，
    而那是克隆质量最直接的来源 —— 面板要能一眼看出对不上的是哪一段，而不是只收到
    一句"有 warning"。
    """

    voice_id: str
    root: str
    ref_count: int
    text_lines: int
    segments: tuple[VoiceSegment, ...]
    problems: tuple[Problem, ...]
    warnings: tuple[Problem, ...]
    enabled: bool | None
    in_library: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "root": self.root,
            "ref_count": self.ref_count,
            "text_lines": self.text_lines,
            "segments": [item.to_dict() for item in self.segments],
            "problems": [item.to_dict() for item in self.problems],
            "warnings": [item.to_dict() for item in self.warnings],
            "enabled": self.enabled,
            "in_library": self.in_library,
        }


@dataclass(frozen=True, slots=True)
class VoiceSegmentRemoval:
    """删掉一段参考音的结局（``DELETE /assets/voice/{id}/segments/{name}``）。

    ``renamed`` 是**重编号**那一步的流水账：删掉第 2 段之后，原来的 ``ref_03.wav``
    会变成 ``ref_02.wav`` —— 位置即对应，不重编号的话 ``ref.txt`` 第 2 行会配到
    原来的第 3 段音频上。这件事必须报出来：用户手上的文件名变了，而**静默改名不行**。
    """

    voice_id: str
    removed: str
    removed_text: str | None
    renamed: tuple[tuple[str, str], ...]
    text_rewritten: bool
    notes: tuple[str, ...]
    segments: VoiceSegments

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "removed": self.removed,
            "removed_text": self.removed_text,
            "renamed": [{"from": src, "to": dst} for src, dst in self.renamed],
            "text_rewritten": self.text_rewritten,
            "notes": list(self.notes),
            "segments": self.segments.to_dict(),
        }


def _matches(row: _AssetRow, query: str | None) -> bool:
    """筛选：id 或标签里含这个子串（**不区分大小写**）。

    为什么把标签也算进来：标签是**人自己填的**（"备用" / "雨天" / "夜景"），
    按它筛是管理一柜子素材时最自然的一件事；而 id 是机器起的，记不住。
    """
    if query is None:
        return True
    wanted = query.strip().lower()
    if wanted == "":
        return True
    if wanted in row.id.lower():
        return True
    return any(wanted in str(tag).lower() for tag in (getattr(row, "tags", None) or ()))


def _on_disk(row: _AssetRow) -> bool:
    """这条素材的文件（音色是**目录**）还在不在盘上。

    "启用"是一个**库里的标记**，"出片会不会挑到它"是另一个问题：文件被挪走之后，
    行还留在库里、开关还是绿的，而出片再也挑不到它。多一次 ``stat`` 就能让这两件事
    在面板上分开显示 —— 这正是"面板与出片看到同一个真相"的一半。
    """
    path = Path(str(row.path))
    return path.is_dir() if isinstance(row, VoiceProfileRow) else path.is_file()


def row_to_dict(row: _AssetRow) -> dict[str, Any]:
    """行 → JSON（``dataclasses.asdict`` 会把 ``Path`` 与枚举一起翻掉，这里显式写）。

    手写而不是 ``asdict``：面板要的是**稳定字段名**，而 ``asdict`` 会跟着 dataclass
    的字段改名一起变 —— 那正是前端契约测试要防的漂移。
    """
    on_disk = _on_disk(row)
    if isinstance(row, BrollClipRow):
        return {
            "kind": "broll",
            "id": row.id,
            "path": row.path,
            "duration_ms": row.duration_ms,
            "usable_from_ms": row.usable_from_ms,
            "usable_to_ms": row.usable_to_ms,
            "width": row.width,
            "height": row.height,
            "fps": row.fps,
            "has_text": row.has_text,
            "tags": list(row.tags),
            "license": row.license,
            "thumb_path": row.thumb_path,
            "source_url": row.source_url,
            "proof_path": row.proof_path,
            "licensed_to": row.licensed_to,
            "use_count": row.use_count,
            "last_used_at": row.last_used_at,
            "enabled": row.enabled,
            "on_disk": on_disk,
            "created_at": row.created_at,
        }
    if isinstance(row, BgmTrackRow):
        return {
            "kind": "bgm",
            "id": row.id,
            "path": row.path,
            "duration_ms": row.duration_ms,
            "sample_rate": row.sample_rate,
            "channels": row.channels,
            "bitrate_kbps": row.bitrate_kbps,
            "loudness_lufs": row.loudness_lufs,
            "bpm": row.bpm,
            "mood": row.mood,
            "tags": list(row.tags),
            "loopable": row.loopable,
            "license": row.license,
            "source_url": row.source_url,
            "proof_path": row.proof_path,
            "licensed_to": row.licensed_to,
            "use_count": row.use_count,
            "last_used_at": row.last_used_at,
            "enabled": row.enabled,
            "on_disk": on_disk,
            "created_at": row.created_at,
        }
    return {
        "kind": "voice",
        "id": row.id,
        "path": row.path,
        "ref_count": row.ref_count,
        "total_duration_ms": row.total_duration_ms,
        "sample_rate": row.sample_rate,
        "peak_db": row.peak_db,
        "text_path": row.text_path,
        "proof_path": row.proof_path,
        "license": row.license,
        "source_url": row.source_url,
        "licensed_to": row.licensed_to,
        "use_count": row.use_count,
        "last_used_at": row.last_used_at,
        "enabled": row.enabled,
        "on_disk": on_disk,
        "created_at": row.created_at,
    }


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class AssetService:
    """素材库的读写入口（扫盘 / 入库 / 启停 / 改字段）。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        paths: StudioPaths,
        log: LogSink | None = None,
        probe: Any = probe_media,
        volume: Any = analyze_volume,
        thumbnail: Any = extract_thumbnail,
        loudness: Any = measure_loudness,
        digest: Any = stream_sha256,
    ) -> None:
        """:param probe: / ``volume`` / ``thumbnail`` / ``loudness`` / ``digest`` 都是
        外部工具的实现（默认真 ffmpeg / ffprobe）。测试注入假件之后，扫盘用例
        不再依赖这台机器装没装 ffmpeg。
        """
        self._connection = connection
        self._paths = paths
        self._log = log
        self._probe = probe
        self._volume = volume
        self._thumbnail = thumbnail
        self._loudness = loudness
        self._digest = digest
        self._broll = BrollClipRepo(connection)
        self._bgm = BgmTrackRepo(connection)
        self._voice = VoiceProfileRepo(connection)
        self._audit = AuditRepo(connection)

    # ── 读 ──────────────────────────────────────────────────────────

    def library(self) -> AssetLibrary:
        """库里有什么 + **盘上有什么**（两者必须一起看，否则面板会说谎）。

        为什么把"盘上有什么"合进这一屏：出片挑素材走的是 ``render/assets.py``，它
        **只列目录、不读库**（"能进目录就默认可用"）。一个只看库的面板于是会说出最坏
        的那句话 ——「跑酷素材 0 条」，而片子里正放着跑酷；用户从此不再相信这一屏上的
        任何数字。合并之后口径只有一句：**出片能挑到的条数**（``usable``）。库里有什么、
        盘上有什么照旧分开报，但结论（``degraded`` / ``shortfall``）只认 ``usable``。

        代价是这一屏多两次 ``iterdir`` 与每条一次 ``stat``。素材目录是人工维护的平铺
        目录（几十条），这个量级不值得为它做缓存，也不值得为它加一层"资产索引"。
        """
        sections = tuple(self._section(kind) for kind in AssetKind)
        broll = sections[0]
        # `degraded` 只回答一个问题：**出片会不会真的黑屏**。
        #
        # 它曾经是"条数或时长不够"（也就是现在的 `shortfall`），后果是 note 上写着
        # "当前为黑屏降级模式"，而片子里明明放着一条跑酷 —— 同一句谎话换了个说法。
        # "不够多"是建议，"一条都挑不到"才是降级；两者混在一起，面板就再也说不清
        # "我到底该不该去补素材"。
        degraded = broll.usable == 0
        return AssetLibrary(
            sections=sections,
            degraded=degraded,
            note=(
                "跑酷素材一条都挑不到（目录是空的，或者全被停用了）：当前为黑屏降级模式"
                "（片头片尾仍可出片，画面只有水印与字幕）"
                if degraded
                else None
            ),
        )

    def _section(self, kind: AssetKind) -> AssetKindSection:
        """一类的「库里有什么 + 盘上有什么」（``library()`` 与 ``page()`` **共用这一份**）。

        两处各写一遍的后果，是「一次拿全」与「一页一页看」在同一个类别上给出不同的
        ``usable`` / ``shortfall`` —— 而面板正是拿它上色的。
        """
        repo = self._repo(kind)
        stats = repo.stats()
        items: tuple[_AssetRow, ...] = tuple(repo.list_all())
        discovery = discover(self._paths, kind)
        known = {row.id for row in items}
        pending = tuple(
            PendingAsset(kind=kind, id=candidate.id, path=str(candidate.path))
            for candidate in discovery.candidates
            if candidate.id not in known
        )
        usable = self._usable(
            kind, discovery=discovery, stats=stats, disabled=disabled_assets(self._connection)
        )
        return AssetKindSection(
            kind=kind,
            root=str(root_for(self._paths, kind)),
            stats=stats,
            items=items,
            shortfall=self._shortfall(kind, stats, usable=usable, pending=len(pending)),
            disk_total=len(discovery.candidates),
            pending=pending,
            strays=tuple(str(item) for item in discovery.strays),
            root_missing=discovery.root_missing,
            usable=usable,
        )

    def page(
        self,
        kind: AssetKind,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        query: str | None = None,
        enabled: bool | None = None,
    ) -> AssetPage:
        """一类素材的**一页**（每个类别一个菜单：跑酷 / 音色 / BGM 各看各的）。

        三件事都在这里定，路由那一层不重复实现：

        - ``page`` / ``page_size`` **钳在合法范围内**（页码翻过头 ⇒ 给最后一页，
          而不是一页空白 —— "翻过头"最常见的成因正是"你刚删掉了一页的素材"）；
        - ``query`` 匹配 id 或标签；
        - ``enabled`` 三态（``None`` = 全部）：**筛的是"库里那一行"的开关**，
          与"文件还在不在盘上"是两件事（后者由 ``items[].on_disk`` 单独报）。
        """
        section = self._section(kind)
        size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        rows = tuple(
            row
            for row in section.items
            if _matches(row, query) and (enabled is None or bool(row.enabled) == enabled)
        )
        total = len(rows)
        pages = max(1, ceil(total / size))
        current = min(max(int(page), 1), pages)
        start = (current - 1) * size
        return AssetPage(
            kind=section.kind,
            root=section.root,
            root_missing=section.root_missing,
            stats=section.stats,
            usable=section.usable,
            shortfall=section.shortfall,
            disk_total=section.disk_total,
            pending=section.pending,
            strays=section.strays,
            items=rows[start : start + size],
            total=total,
            page=current,
            page_size=size,
            pages=pages,
        )

    def get(self, kind: AssetKind, asset_id: str) -> _AssetRow | None:
        return self._repo(kind).get(asset_id)

    def resolve(self, asset_id: str) -> tuple[AssetKind, _AssetRow] | None:
        """按 id 反查它属于哪一类（``PATCH /assets/{id}`` 用）。

        同一个 id 在两类里都存在（比如一个音色目录叫 ``bgm_001``）⇒ 抛
        :class:`StudioError`（``ASSET_INVALID``）：猜一个然后改错行，比报错难查得多。
        """
        hits: list[tuple[AssetKind, _AssetRow]] = []
        for kind in AssetKind:
            row = self._repo(kind).get(asset_id)
            if row is not None:
                hits.append((kind, row))
        if not hits:
            return None
        if len(hits) > 1:
            raise StudioError(
                f"id {asset_id!r} 在 {[str(item[0]) for item in hits]} 里都存在，无法判断改哪一条",
                code=ErrorCode.ASSET_INVALID,
                context={"id": asset_id, "kinds": [str(item[0]) for item in hits]},
                remediation="请求里带上 kind（broll / bgm / voice）",
            )
        return hits[0]

    # ── 扫盘 / 入库 ─────────────────────────────────────────────────

    def scan(
        self,
        *,
        kind: AssetKind | None = None,
        ids: Iterable[str] | None = None,
        license: str | None = None,
    ) -> ScanReport:
        """**只读**扫盘（预览：盘上有什么、能不能入库）。"""
        return self._run(kind=kind, ids=ids, license=license, dry_run=True)

    def ingest(
        self,
        *,
        kind: AssetKind | None = None,
        ids: Iterable[str] | None = None,
        license: str | None = None,
    ) -> ScanReport:
        """扫盘 + 入库（已入库的刷新机器事实，新的按 ``license`` 落库）。"""
        check_license(license)
        report = self._run(kind=kind, ids=ids, license=license, dry_run=False)
        self._log_ingest(report)
        return report

    def _run(
        self,
        *,
        kind: AssetKind | None,
        ids: Iterable[str] | None,
        license: str | None,
        dry_run: bool,
    ) -> ScanReport:
        wanted = None if ids is None else frozenset(ids)
        kinds = tuple(AssetKind) if kind is None else (kind,)
        sections = tuple(
            self._scan_kind(item, ids=wanted, license=license, dry_run=dry_run) for item in kinds
        )
        return ScanReport(sections=sections, dry_run=dry_run, license=license)

    def _scan_kind(
        self,
        kind: AssetKind,
        *,
        ids: frozenset[str] | None,
        license: str | None,
        dry_run: bool,
    ) -> ScanSection:
        discovery = discover(self._paths, kind)
        repo = self._repo(kind)
        assets: list[ScannedAsset] = []
        seen: set[str] = set()
        for candidate in discovery.candidates:
            if ids is not None and candidate.id not in ids:
                continue
            seen.add(candidate.id)
            row = repo.get(candidate.id)
            effective = row.license if row is not None else license
            check = validate.check(
                candidate,
                license=effective,
                usable_from_ms=row.usable_from_ms if isinstance(row, BrollClipRow) else 0,
                usable_to_ms=row.usable_to_ms if isinstance(row, BrollClipRow) else None,
                probe=self._probe,
                volume=self._volume,
            )
            action: IngestAction | None = None
            note: str | None = None
            if not dry_run:
                if check.ok:
                    action, note = self._store(candidate, check, license=effective)
                else:
                    note = f"未入库：{check.problems[0].message}"
                if action is not None and action is not IngestAction.DUPLICATE:
                    # 入库后**回读一次**：报告里的"在不在库里 / 启用没有 / 有没有缩略图"
                    # 说的是这一轮跑完之后的状态，不是跑之前那一瞬间的
                    row = repo.get(candidate.id)
            assets.append(
                ScannedAsset(
                    kind=kind,
                    id=candidate.id,
                    path=str(candidate.path),
                    stored=row is not None,
                    enabled=None if row is None else row.enabled,
                    check=check,
                    action=action,
                    note=note,
                    thumb_path=_thumb_of(row),
                )
            )
        missing = tuple(item.id for item in repo.list_all() if item.id not in seen)
        return ScanSection(
            kind=kind,
            root=str(discovery.root),
            root_missing=discovery.root_missing,
            assets=tuple(assets),
            strays=tuple(str(item) for item in discovery.strays),
            missing=missing,
            stats=repo.stats(),
        )

    def _store(
        self, candidate: AssetCandidate, check: AssetCheck, *, license: str | None
    ) -> tuple[IngestAction | None, str | None]:
        """把一个体检合格的素材落库（返回动作 / 备注）。

        ``action`` 为 ``None`` 表示"这次没写库"（入库过程中失败），调用方据此
        **不回读**那一行 —— 否则报告会显示"在库里"，而库里那一行根本不存在。
        """
        try:
            if candidate.kind is AssetKind.BROLL:
                return self._store_broll(candidate, check, license=license)
            if candidate.kind is AssetKind.BGM:
                return self._store_bgm(candidate, check, license=license)
            return self._store_voice(candidate, check)
        except (StudioError, OSError) as exc:
            message = exc.message if isinstance(exc, StudioError) else str(exc)
            logger.warning("assets.store_failed", asset=candidate.id, error=message)
            return None, f"入库失败：{message}"

    def _store_broll(
        self, candidate: AssetCandidate, check: AssetCheck, *, license: str | None
    ) -> tuple[IngestAction, str | None]:
        assert license is not None  # 体检通过 ⇒ 一定有授权（license_missing 是 problem）
        info = check.info
        assert info is not None
        thumb, note = self._thumbnail_for(candidate.id, candidate.path, info)
        result = self._broll.upsert(
            clip_id=candidate.id,
            path=candidate.path,
            sha256=self._digest(candidate.path),
            duration_ms=info.duration_ms,
            license=license,
            width=info.width,
            height=info.height,
            fps=info.fps,
            thumb_path=thumb,
        )
        return result.action, note if note is not None else result.reason

    def _store_bgm(
        self, candidate: AssetCandidate, check: AssetCheck, *, license: str | None
    ) -> tuple[IngestAction, str | None]:
        assert license is not None
        info = check.info
        assert info is not None
        loudness, note = self._loudness_for(candidate.path)
        result = self._bgm.upsert(
            track_id=candidate.id,
            path=candidate.path,
            sha256=self._digest(candidate.path),
            duration_ms=info.duration_ms,
            license=license,
            sample_rate=info.sample_rate,
            channels=info.channels,
            bitrate_kbps=info.bitrate_kbps,
            loudness_lufs=loudness,
        )
        return result.action, note if note is not None else result.reason

    def _store_voice(self, candidate: AssetCandidate, check: AssetCheck) -> tuple[IngestAction, str | None]:
        total = sum(item.duration_ms for item in check.segments)
        rates = [item.sample_rate for item in check.segments if item.sample_rate is not None]
        peaks = [item for item in check.peaks if item is not None]
        text = voice_text(candidate)
        proof = voice_profile(candidate)
        result = self._voice.upsert(
            voice_id=candidate.id,
            path=candidate.path,
            ref_count=len(check.segments),
            total_duration_ms=total,
            sample_rate=min(rates) if rates else None,
            peak_db=max(peaks) if peaks else None,
            text_path=text,
            proof_path=proof,
        )
        return result.action, result.reason

    def _thumbnail_for(self, asset_id: str, source: Path, info: MediaInfo) -> tuple[Path | None, str | None]:
        """抽一帧做缩略图（**失败不阻塞入库**）。"""
        target = self._paths.thumb_file(str(AssetKind.BROLL), asset_id)
        # 抽帧点避开第 0 帧：很多素材的第一帧是黑场（转场/淡入），抽出来是一张纯黑图
        at_ms = min(1_000, max(0, info.duration_ms - 1))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._thumbnail(source, target, at_ms=at_ms)
        except (StudioError, OSError) as exc:
            message = exc.message if isinstance(exc, StudioError) else str(exc)
            logger.warning("assets.thumbnail_failed", asset=asset_id, error=message)
            return None, "缩略图没生成（素材本身可用，只是没有预览图）"
        return target, None

    def _loudness_for(self, source: Path) -> tuple[float | None, str | None]:
        """量一次响度（**失败不阻塞入库** —— §3.3.14 对 BGM 的硬判据只有时长与可解码）。"""
        try:
            value = self._loudness(source)
        except (StudioError, OSError) as exc:
            message = exc.message if isinstance(exc, StudioError) else str(exc)
            logger.warning("assets.loudness_failed", source=str(source), error=message)
            return None, "响度没量出来（入库照常，混音时会按默认响度对齐）"
        return value, None

    # ── 写：启停与改字段 ────────────────────────────────────────────

    def set_enabled(
        self,
        kind: AssetKind,
        asset_id: str,
        enabled: bool,
        *,
        actor: str = "user",
        source: str = "webui",
        request_id: str | None = None,
    ) -> _AssetRow:
        """启用 / 停用（**不动物理文件**，§T4.8 误删保护）。"""
        row = self._repo(kind).get(asset_id)
        if row is None:
            raise _not_found(kind, asset_id)
        if row.enabled == enabled:
            # 幂等：状态没变就不留痕（否则审计页会被"重复点同一下"刷满）
            return row
        changed = self._repo(kind).set_enabled(asset_id, enabled)
        if not changed:
            raise _not_found(kind, asset_id)
        self._record(
            kind,
            asset_id,
            action="asset.enable" if enabled else "asset.disable",
            before={"enabled": row.enabled},
            after={"enabled": enabled},
            actor=actor,
            source=source,
            request_id=request_id,
        )
        refreshed = self._repo(kind).get(asset_id)
        assert refreshed is not None
        return refreshed

    def patch(
        self,
        kind: AssetKind,
        asset_id: str,
        fields: dict[str, Any],
        *,
        actor: str = "user",
        source: str = "webui",
        request_id: str | None = None,
    ) -> _AssetRow:
        """改人填字段（授权 / 标签 / 可用区间 / 情绪…），并留痕。"""
        row = self._repo(kind).get(asset_id)
        if row is None:
            raise _not_found(kind, asset_id)
        cleaned = _clean_fields(kind, fields, row)
        if "enabled" in cleaned and cleaned["enabled"] != row.enabled:
            return self.set_enabled(
                kind,
                asset_id,
                bool(cleaned.pop("enabled")),
                actor=actor,
                source=source,
                request_id=request_id,
            )
        cleaned.pop("enabled", None)
        if not cleaned and "enabled" in fields:
            # 请求里**只有** `enabled`，而状态本来就对 ⇒ 幂等：不写库、不留痕，回当前行。
            # 不能把空字段集交给仓储层 —— 那一层对"一个字段都没有的 PATCH"是明确报错的
            # （它对空表单说不，是对的），于是"重复点同一下"会变成一条 422，而素材明明在。
            # 注意只在请求里真的带了 `enabled` 时才走这条路：整个请求是空表单
            # （`{}`）仍然是"调用方漏传了"，照样报错。
            return row
        before = _pick(row, cleaned)
        try:
            updated = self._repo(kind).patch(asset_id, **cleaned)
        except ValueError as exc:
            raise StudioError(
                f"字段不合法：{exc}",
                code=ErrorCode.ASSET_INVALID,
                context={"id": asset_id, "kind": str(kind)},
                remediation="检查字段名与取值（面板只应提交可见字段）",
            ) from exc
        if updated is None:
            raise _not_found(kind, asset_id)
        self._record(
            kind,
            asset_id,
            action="asset.update",
            before=before,
            after=_pick(updated, cleaned),
            actor=actor,
            source=source,
            request_id=request_id,
        )
        return updated

    def delete(
        self,
        kind: AssetKind,
        asset_id: str,
        *,
        purge: bool = False,
        actor: str = "user",
        source: str = "webui",
        request_id: str | None = None,
    ) -> AssetDelete:
        """把一条素材从库里删掉（``purge=True`` 时**连盘上那份一起删**）。

        为什么默认不删盘上文件
        ----------------------
        §T4.8 的原文是"只允许禁用（不物理删除）"—— 误删一柜子素材不可逆。裁定 369
        没有推翻它，而是把"删库里的行"与"删盘上的文件"**分成两件事**：默认只删行
        （重扫一次就回来了），``purge=True`` 才是不可逆的那一步，面板上要二次确认。

        音色为什么默认就该带上盘
        ------------------------
        跑酷 / BGM 删了行，文件还在、出片照样挑得到 —— 删掉的只是留痕。音色反过来：
        参考音目录留在盘上，下次扫盘又会变成一条"盘上有、库里没有"，用户刚删掉的
        东西自己回来了。所以音色的删除由面板默认勾上 ``purge``，把这件事说在明处，
        而不是让服务端替他决定。
        """
        repo = self._repo(kind)
        row = repo.get(asset_id)
        if row is None:
            raise _not_found(kind, asset_id)
        if not repo.delete(asset_id):
            # 行在、删不掉：并发下另一条请求刚删过。照样按"没这条"回，别假装成功。
            raise _not_found(kind, asset_id)
        purged = self._purge_files(kind, asset_id, row) if purge else ()
        self._record(
            kind,
            asset_id,
            action="asset.delete",
            before={"enabled": row.enabled},
            after={"purged": list(purged)},
            actor=actor,
            source=source,
            request_id=request_id,
        )
        return AssetDelete(kind=kind, id=asset_id, purge=purge, purged=purged)

    def _purge_files(self, kind: AssetKind, asset_id: str, row: _AssetRow) -> tuple[str, ...]:
        """删掉盘上那份（**只在它确实落在这一类的根目录里时**）。

        路径守卫不是形式主义：``row.path`` 是库里存的字符串，而音色的删除是
        **递归删目录**。一条被手改过的、或从别的机器同步过来的行，会让"删一条素材"
        变成"删掉另一个目录"。判据是"解析后的绝对路径**严格在**根目录之下"，
        不满足就一个字节都不动，并留一条 warning。
        """
        root = root_for(self._paths, kind).resolve()
        target = root / asset_id if kind is AssetKind.VOICE else Path(str(row.path))
        attempt = self._purge_path(kind, target, root=root, asset_id=asset_id)
        return () if attempt.path is None else (attempt.path,)

    def _purge_path(self, kind: AssetKind, target: Path, *, root: Path, asset_id: str) -> _PurgeAttempt:
        """删掉一个盘上路径（守卫 + 真删）。**一条路径的删法只有这一份**。

        为什么把它从 :meth:`_purge_files` 里提出来：孤儿那条路
        （:meth:`prune_orphans`）要删的是**库里没有行**的东西，手上只有"扫盘发现的
        那个路径"，没有 ``row``。两条路各写一遍守卫，就会出现"删库里那条时守住、
        删孤儿时忘了守"—— 而这里漏一次的代价是 ``shutil.rmtree`` 删掉一个不该删的
        目录，且不可逆。

        ``root`` 由调用方传进来（而不是这里现算）：它必须与"这次扫的是哪一类的
        根目录"是**同一个值**，而那个值来自 :func:`~studio.assets.layout.root_for`
        —— 这里不抄第二份。
        """
        try:
            resolved = target.resolve()
        except OSError as exc:
            logger.warning("assets.purge_unresolvable", asset=asset_id, error=str(exc))
            return _PurgeAttempt(reason=f"路径解析不了（{exc}）")
        if resolved == root or not resolved.is_relative_to(root):
            logger.warning("assets.purge_refused", asset=asset_id, path=str(resolved), root=str(root))
            return _PurgeAttempt(reason="路径不在这一类的根目录之下（守卫拒绝）")
        if not resolved.exists():
            # 库里有一行、盘上早就没了：这不是错误（用户可能自己在资源管理器里删了）。
            return _PurgeAttempt(reason="盘上已经没有了")
        if kind is AssetKind.VOICE:
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        return _PurgeAttempt(path=str(resolved))

    def prune_orphans(
        self,
        kind: AssetKind,
        *,
        dry_run: bool = False,
        actor: str = "user",
        source: str = "webui",
        request_id: str | None = None,
    ) -> PruneReport:
        """清掉这一类的**孤儿**：盘上认得出、库里没有、**而且本身就不合格**。

        为什么这件事需要一个动作
        ------------------------
        「盘上有、库里没有」的东西以前**删不掉**：:meth:`delete` 手上没有行就直接
        404，而面板上那句「盘上有 N 条还没入库」于是变成一条**永远动不了的警告**。
        用户能做的只有去资源管理器里手工删 —— 而素材目录名是约定的一部分（音色的
        目录名就是它的 id），手工删很容易顺手删错一个。

        为什么"孤儿"不等于"该删"
        ------------------------
        合格的孤儿**不该删**，该**入库**（那是另一颗按钮）：跑酷 / BGM 的未入库文件
        出片照样挑得到（``render/assets.py`` 只列目录），删了等于凭空少一条底片。
        所以这里只清**本身就不合格**的那些 —— 空目录、没有参考音、探测不了、没有
        视频流、太短、削波…… 它们没有别的出路。合格的那批进 :attr:`PruneReport.kept`，
        面板要照着它说"该入库，不是该删"。

        判据为什么不能直接用 ``check.ok``
        --------------------------------
        ``check_broll`` / ``check_bgm`` 会把「授权没填」算进 ``problems``
        （``license_missing``），而**孤儿之所以是孤儿，正是因为它还没有库里那一行**
        —— 那一行才是存授权的地方。拿 ``check.ok`` 当判据，一条完好无损、只是还没
        登记的底片会被当成垃圾删掉，而且删完**没有任何地方报错**。所以这里把
        "登记状态"那几条（:data:`_REGISTRATION_ONLY_CODES`）从阻塞项里摘出去：
        **它说的是库里的登记，不是盘上文件的毛病。**

        没被认出来的（``strays``）一个都不动：名字不合规的东西可能是用户自己的原始
        素材，而"认不出"不等于"没用"。报告里如实列出来。
        """
        section = self._scan_kind(kind, ids=None, license=None, dry_run=True)
        root = root_for(self._paths, kind).resolve()
        removed: list[PrunedOrphan] = []
        kept: list[KeptOrphan] = []
        for asset in section.assets:
            if asset.stored:
                # 库里有的不是孤儿：删它走 DELETE /assets/{id}（那条路连审计一起写）
                continue
            blocking = tuple(
                item for item in asset.check.problems if item.code not in _REGISTRATION_ONLY_CODES
            )
            if not blocking:
                kept.append(
                    KeptOrphan(
                        kind=kind,
                        id=asset.id,
                        path=asset.path,
                        reason="本身合格，只是还没入库 —— 该入库，不是该删",
                    )
                )
                continue
            reasons = tuple(item.message for item in blocking)
            if dry_run:
                removed.append(PrunedOrphan(kind=kind, id=asset.id, path=asset.path, problems=reasons))
                continue
            attempt = self._purge_path(kind, Path(asset.path), root=root, asset_id=asset.id)
            if attempt.path is None:
                kept.append(
                    KeptOrphan(
                        kind=kind,
                        id=asset.id,
                        path=asset.path,
                        reason=attempt.reason or "没删",
                    )
                )
                continue
            removed.append(PrunedOrphan(kind=kind, id=asset.id, path=attempt.path, problems=reasons))
        report = PruneReport(
            kind=kind,
            dry_run=dry_run,
            removed=tuple(removed),
            kept=tuple(kept),
            strays=section.strays,
        )
        if not dry_run and removed:
            self._record_prune(report, actor=actor, source=source, request_id=request_id)
        return report

    def _record_prune(self, report: PruneReport, *, actor: str, source: str, request_id: str | None) -> None:
        """孤儿清理的留痕（**只在真删了东西时**写）。

        ``target_id`` 用**类别**而不是某一个 id：这个动作一次动一批，而 ``audit_ops``
        一行只有一个 ``target_id``。删掉的 id 全进 ``after``，审计页照样看得到逐条明细。

        没删成任何东西时**一条都不写**：空跑留痕会把审计页淹掉，而"我今天点了一下、
        它说没什么可清的"这件事没有留档价值。
        """
        self._audit.record(
            actor=actor,
            action="asset.prune",
            target_type=TARGET_TYPE,
            target_id=str(report.kind),
            before={"kind": str(report.kind), "orphans": len(report.removed) + len(report.kept)},
            after={
                "removed": [item.id for item in report.removed],
                "kept": [item.id for item in report.kept],
            },
            source=source,
            request_id=request_id,
        )
        self._emit(
            "info",
            f"素材孤儿清理：{report.kind} 清掉 {len(report.removed)} 个"
            f"（{'、'.join(item.id for item in report.removed)}）",
            payload={"kind": str(report.kind), "removed": [item.id for item in report.removed]},
        )

    # ── 音色：逐段管理（裁定 381）──────────────────────────────────

    def voice_segments(self, voice_id: str) -> VoiceSegments:
        """一个音色目录的逐段现状（**读盘 + 与入库同一份体检判据**）。

        为什么这件事值得一个端点：音色的"能不能用"不取决于库里那一行，而取决于
        目录里躺着哪几段、每段多长、``ref.txt`` 有没有与它们一一对应。以前这三件事
        只有入库那条路知道，而它只在**入库的那一刻**说一次 —— 用户想"看看现在到底
        是什么样"、或者想删掉一段，面板上一个字都没有。
        """
        candidate = self._voice_candidate(voice_id)
        # 探测 / 音量走**注入的那一套**（与扫盘同源）：测试与排障都靠它把外部工具换掉，
        # 漏传的话这里会偷偷去跑真 ffprobe —— 而同一台机器上"扫盘说能用、这里说读不了"
        # 会是最难查的一种不一致。
        check = validate.check_voice(candidate, probe=self._probe, volume=self._volume)
        refs = voice_refs(candidate)
        lines = _text_lines(candidate)
        probed = {
            index: (info, peak)
            for index, info, peak in zip(check.segment_indexes, check.segments, check.peaks, strict=False)
        }
        rows: list[VoiceSegment] = []
        for position, ref in enumerate(refs, start=1):
            info, peak = probed.get(position, (None, None))
            rows.append(
                VoiceSegment(
                    index=position,
                    name=ref.name,
                    duration_ms=None if info is None else info.duration_ms,
                    sample_rate=None if info is None else info.sample_rate,
                    peak_db=peak,
                    text=lines[position - 1] if position <= len(lines) else None,
                    problems=_segment_problems(check, position),
                )
            )
        row = self._voice.get(voice_id)
        return VoiceSegments(
            voice_id=voice_id,
            root=str(candidate.path),
            ref_count=len(refs),
            text_lines=len(lines),
            segments=tuple(rows),
            problems=check.problems,
            warnings=check.warnings,
            enabled=None if row is None else row.enabled,
            in_library=row is not None,
        )

    def remove_voice_segment(
        self,
        voice_id: str,
        name: str,
        *,
        actor: str = "user",
        source: str = "webui",
        request_id: str | None = None,
    ) -> VoiceSegmentRemoval:
        """删掉一段参考音（**删完重编号 + 同步 ``ref.txt``**）。

        为什么必须重编号
        ----------------
        ``ref.txt`` 的第 N 行对应第 N 段 —— 位置即对应。删掉 ``ref_02.wav`` 却不重编号，
        ``ref_03.wav`` 会顶上第 2 位，而第 2 行文本说的是**被删掉的那一段**的话。
        于是克隆拿到的 prompt 是"这段音频 + 另一段音频的文本"，而复刻质量就是这么
        掉下去的（而且没有任何地方会报错）。

        为什么最后一段不能删
        --------------------
        删光之后这个音色就念不出来了，而库里那一行还在、面板上还是绿的。
        "想整条不要了"走素材库的删除，"想换掉这一段"走覆盖上传 —— 两条都说得清。
        """
        stem = Path(name).stem
        if REF_STEM_PATTERN.fullmatch(stem) is None or Path(name).name != name:
            raise StudioError(
                f"不是一段参考音的名字：{name}",
                code=ErrorCode.ASSET_INVALID,
                context={"voice_id": voice_id, "name": name},
                remediation="参考音的名字是 ref_01.wav / ref_02.mp3 这种形状（路径分隔符不算名字的一部分）",
            )
        candidate = self._voice_candidate(voice_id)
        refs = list(voice_refs(candidate))
        names = [item.name for item in refs]
        if name not in names:
            raise StudioError(
                f"这个音色里没有 {name}",
                code=ErrorCode.ASSET_NOT_FOUND,
                context={"voice_id": voice_id, "name": name, "refs": names},
                remediation="刷新一下这一屏（盘上的文件可能刚被别处改过）",
            )
        if len(refs) <= 1:
            raise StudioError(
                "这是最后一段参考音，删了这个音色就念不出来了",
                code=ErrorCode.ASSET_INVALID,
                context={"voice_id": voice_id, "name": name},
                remediation="整条不要 ⇒ 用素材库那一行的「删除」；想换掉这一段 ⇒ 勾「覆盖同名」重传",
            )
        position = names.index(name)
        lines = _text_lines(candidate)
        removed_text = lines[position] if position < len(lines) else None
        refs[position].unlink()
        kept = [item for index, item in enumerate(refs) if index != position]
        renamed: list[tuple[str, str]] = []
        # 升序重编号：目标序号恒 **≤** 原序号，所以不会撞上还没改名的段（被删掉的那个名字
        # 此刻已经空出来了）—— 不需要"先改成临时名"那两步。
        for index, item in enumerate(kept, start=1):
            target = item.with_name(ref_name(index, item.name))
            if target == item:
                continue
            item.rename(target)
            renamed.append((item.name, target.name))
        notes: list[str] = []
        text_rewritten = False
        text_path = voice_text(candidate)
        if text_path is not None:
            if len(lines) == len(refs):
                body = "\n".join(line for index, line in enumerate(lines) if index != position) + "\n"
                with text_path.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(body)
                text_rewritten = True
            else:
                # 本来就对不上：删哪一行是**猜**。留着，把话说清楚。
                notes.append(
                    f"ref.txt 原本有 {len(lines)} 行、参考音有 {len(refs)} 段（本来就对不上）"
                    f" ⇒ 这次没动它。现在剩 {len(kept)} 段，文本那几行要你自己对一遍。"
                )
        row = self._voice.get(voice_id)
        if row is not None:
            # 库里那行跟着刷（段数 / 总时长 / 最响那一段）。没入库的就只动盘 ——
            # 拿一次"段数变了"去替用户决定"这条音色要不要入库"，是替得太多。
            self.ingest(kind=AssetKind.VOICE, ids=[voice_id], license=None)
        self._record(
            AssetKind.VOICE,
            voice_id,
            action="asset.voice_segment_remove",
            before={"ref_count": len(refs), "text_lines": len(lines)},
            after={
                "removed": name,
                "renamed": [f"{src} ⇒ {dst}" for src, dst in renamed],
                "ref_count": len(kept),
                "text_rewritten": text_rewritten,
            },
            actor=actor,
            source=source,
            request_id=request_id,
        )
        return VoiceSegmentRemoval(
            voice_id=voice_id,
            removed=name,
            removed_text=removed_text,
            renamed=tuple(renamed),
            text_rewritten=text_rewritten,
            notes=tuple(notes),
            segments=self.voice_segments(voice_id),
        )

    def _voice_candidate(self, voice_id: str) -> AssetCandidate:
        """音色 id ⇒ 盘上那个目录的候选（**走与扫盘同一份发现逻辑**）。

        自己拼一遍 ``root / voice_id`` 再 ``iterdir`` 也能跑，但那样"什么算这个音色的
        文件"就有了第二份口径 —— 而这份口径决定的是"哪几段会被删掉"。
        """
        discovery = discover(self._paths, AssetKind.VOICE)
        for candidate in discovery.candidates:
            if candidate.id == voice_id:
                return candidate
        raise StudioError(
            f"盘上没有这个音色的目录：{voice_id}",
            code=ErrorCode.ASSET_NOT_FOUND,
            context={"voice_id": voice_id, "root": str(discovery.root)},
            remediation="先在这个面板上传参考音（POST /api/v1/assets/voice）",
        )

    # ── 内部 ────────────────────────────────────────────────────────

    def _repo(self, kind: AssetKind) -> _AnyRepo:
        if kind is AssetKind.BROLL:
            return self._broll
        if kind is AssetKind.BGM:
            return self._bgm
        return self._voice

    def _usable(
        self,
        kind: AssetKind,
        *,
        discovery: Discovery,
        stats: AssetStats,
        disabled: DisabledAssets,
    ) -> int:
        """出片真能挑到的条数（**与渲染器同一条口径**，见 ``render/assets.py``）。

        - 跑酷 / BGM：盘上候选 − 其中被停用的。渲染器只认目录，库里没有行的也算
          （"能进目录就算数"）；
        - 音色：库里启用的。配音**只认** ``voice_profiles`` —— 它与跑酷不同，得先入库
          才知道参考音有几段、逐字文本对不对，所以盘上一个没入库的目录还挑不了。

        这两条口径不一致是**如实**的：不是"设计不统一"，而是两条链路本来就读不同的东西。
        把它们写成一样，面板就会开始说第二种谎。
        """
        if kind is AssetKind.VOICE:
            return stats.enabled
        excluded = disabled.for_kind(kind)
        return sum(1 for candidate in discovery.candidates if candidate.path.name not in excluded)

    def _shortfall(self, kind: AssetKind, stats: AssetStats, *, usable: int, pending: int) -> str | None:
        """还差多少（**建议值**，不是出片的门禁）。

        判据只认 ``usable``，不认库里的家底 —— 否则"盘上 58 条、一条没入库"会被报成
        "0 条"，而那句话与事实正好相反。
        """
        if kind is AssetKind.BROLL:
            if usable < BROLL_LIBRARY_MIN_CLIPS:
                return f"出片能挑到的跑酷素材只有 {usable} 条（建议 ≥ {BROLL_LIBRARY_MIN_CLIPS}）"
            if stats.enabled_duration_ms < BROLL_LIBRARY_MIN_MS:
                minutes = stats.enabled_duration_ms / 60_000
                tail = f"；另有 {pending} 条还没入库，入库后才知道它们的时长" if pending else ""
                return (
                    f"已入库的跑酷素材共 {minutes:.1f} 分钟"
                    f"（建议 ≥ {BROLL_LIBRARY_MIN_MS // 60_000} 分钟）{tail}"
                )
            return None
        if usable == 0:
            if kind is AssetKind.BGM:
                return "出片一条都挑不到（盘上没有 BGM，或者全被停用了）⇒ 走单轨人声"
            # 音色不是"出片挑不到"：它压根不进画面，配音只认 ``voice_profiles`` 表。
            # 说成"出片挑不到"会让人以为音色是拿去当底片的（面板上那句话是要给人看的）。
            return "配音一条都用不了（盘上还没有音色入库，或者全被停用了）⇒ 配音无法开始"
        return None

    def _record(
        self,
        kind: AssetKind,
        asset_id: str,
        *,
        action: str,
        before: dict[str, Any],
        after: dict[str, Any],
        actor: str,
        source: str,
        request_id: str | None,
    ) -> None:
        """审计留痕 + 一条日志（**先落库再说话**：§04.4.6 的顺序铁律）。"""
        self._audit.record(
            actor=actor,
            action=action,
            target_type=TARGET_TYPE,
            target_id=asset_id,
            before={"kind": str(kind), **before},
            after={"kind": str(kind), **after},
            source=source,
            request_id=request_id,
        )
        self._emit(
            "info",
            f"{_ACTION_TEXT.get(action, '素材更新')}：{kind} {asset_id}",
            payload={"asset_id": asset_id, "kind": str(kind), "action": action},
        )

    def _log_ingest(self, report: ScanReport) -> None:
        totals = report.to_dict()["totals"]
        assert isinstance(totals, dict)
        self._emit(
            "info",
            "素材入库：新增 {created} / 刷新 {refreshed} / 未变 {unchanged} / 重复 {duplicate}"
            " / 未入库 {rejected}".format(**totals),
            payload={"totals": totals, "license": report.license},
        )

    def _emit(self, level: Severity, message: str, *, payload: dict[str, Any] | None = None) -> None:
        if self._log is None:
            return
        self._log(level=level, source=LOG_SOURCE, message=message, payload=payload)


#: 审计动作 ⇒ 日志里那句话的开头（审计页与日志面板看的是同一件事，用词要对得上）
_ACTION_TEXT: dict[str, str] = {
    "asset.delete": "素材删除",
    "asset.enable": "素材启用",
    "asset.disable": "素材停用",
    "asset.update": "素材更新",
    "asset.prune": "素材孤儿清理",
    "asset.voice_segment_remove": "音色参考音删除",
}


def _text_lines(candidate: AssetCandidate) -> list[str]:
    """``ref.txt`` 的非空行（**位置即对应**：第 N 行 ↔ 第 N 段）。

    与 ``tts/server.py::_ref_lines`` 是同一条口径：空白行不算一段（否则一段参考音配到
    一个空字符串，引擎那边会当成"这段没文本"）。
    """
    path = voice_text(candidate)
    if path is None:
        return []
    body = path.read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in body.splitlines() if line.strip()]


def _segment_problems(check: AssetCheck, index: int) -> tuple[Problem, ...]:
    """体检结论里**属于第 ``index`` 段**的那几条（判据的 code 就带着段号）。

    ``ref_02_too_short`` / ``ref_02_clipped`` 这种命名是 ``check_voice`` 刻意留下的
    —— 逐段管理这一屏靠它把"哪一段不合格"摆到那一段那一行上，而不是堆在页脚。
    """
    prefix = f"ref_{index:02d}_"
    return tuple(item for item in check.problems if item.code.startswith(prefix))


def _not_found(kind: AssetKind, asset_id: str) -> StudioError:
    return StudioError(
        f"素材不存在：{kind} / {asset_id}",
        code=ErrorCode.ASSET_NOT_FOUND,
        context={"kind": str(kind), "id": asset_id},
        remediation="先扫盘入库（POST /assets/ingest），或检查 id 拼写",
    )


def _thumb_of(row: _AssetRow | None) -> str | None:
    return row.thumb_path if isinstance(row, BrollClipRow) else None


def _clean_fields(kind: AssetKind, fields: dict[str, Any], row: _AssetRow) -> dict[str, Any]:
    """校验并归一化 PATCH 的字段（**在进 SQL 之前**）。

    ``license`` 对跑酷 / BGM 是必填（DDL ``NOT NULL``）⇒ 传 ``None`` 直接拒；
    对音色允许清空（§4.3.1 的前置是 ``profile.json`` 而不是授权类型）。
    """
    cleaned: dict[str, Any] = dict(fields)
    if "license" in cleaned:
        value = cleaned["license"]
        if value is None and kind is not AssetKind.VOICE:
            raise StudioError(
                f"{kind} 的授权类型不能清空",
                code=ErrorCode.ASSET_INVALID,
                context={"kind": str(kind), "id": row.id},
                remediation="从 self_recorded / authorized / cc0 / purchased 里选一个",
            )
        if value is not None and value not in LICENSES:
            raise StudioError(
                f"授权类型不合法：{value}",
                code=ErrorCode.ASSET_INVALID,
                context={"license": value, "allowed": sorted(LICENSES)},
                remediation="从 self_recorded / authorized / cc0 / purchased 里选一个",
            )
    if "mood" in cleaned and cleaned["mood"] is not None:
        cleaned["mood"] = str(cleaned["mood"])
    if "tags" in cleaned:
        cleaned["tags"] = [str(item) for item in (cleaned["tags"] or ())]
    if isinstance(row, BrollClipRow) and {"usable_from_ms", "usable_to_ms"} & set(cleaned):
        start = int(cleaned.get("usable_from_ms", row.usable_from_ms) or 0)
        end = cleaned.get("usable_to_ms", row.usable_to_ms)
        end_value = None if end is None else int(end)
        if start < 0:
            raise StudioError(
                "usable_from_ms 不能是负数",
                code=ErrorCode.ASSET_INVALID,
                context={"id": row.id, "usable_from_ms": start},
                remediation="从 0 开始计（毫秒）",
            )
        if end_value is not None and end_value <= start:
            raise StudioError(
                f"可用区间为空：from={start} to={end_value}",
                code=ErrorCode.ASSET_INVALID,
                context={"id": row.id, "from": start, "to": end_value},
                remediation="usable_to_ms 必须大于 usable_from_ms（不确定就留空 = 到片尾）",
            )
    return cleaned


def _pick(row: _AssetRow, fields: dict[str, Any]) -> dict[str, Any]:
    """从行里取出这次改动的**改动前 / 改动后**值（审计的 ``before`` / ``after``）。"""
    return {name: _value_of(row, name) for name in fields}


def _value_of(row: _AssetRow, name: str) -> Any:
    if name == "tags":
        return list(getattr(row, "tags", []) or [])
    return getattr(row, name, None)
