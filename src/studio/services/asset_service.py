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

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.assets import validate
from studio.assets.layout import (
    AssetCandidate,
    AssetKind,
    discover,
    root_for,
    voice_profile,
    voice_text,
)
from studio.assets.validate import (
    BROLL_LIBRARY_MIN_CLIPS,
    BROLL_LIBRARY_MIN_MS,
    AssetCheck,
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
    "LICENSES",
    "AssetKindSection",
    "AssetLibrary",
    "AssetService",
    "ScanReport",
    "ScanSection",
    "ScannedAsset",
    "row_to_dict",
]

logger = get_logger("studio.services.asset")

#: ``system_logs.source``（日志面板按来源过滤时的口径）
LOG_SOURCE: str = "assets"

#: ``audit_ops.target_type``
TARGET_TYPE: str = "asset"

#: DDL 的 ``license`` CHECK（§3.3.14）。写之前校验：让 CHECK 在运行期炸掉，
#: 等于把"参数写错了"变成"入库失败"，而失败信息里只有一句 SQLite 的约束名。
LICENSES: frozenset[str] = frozenset({"self_recorded", "authorized", "cc0", "purchased"})

_AssetRow = BrollClipRow | BgmTrackRow | VoiceProfileRow
type _AnyRepo = BrollClipRepo | BgmTrackRepo | VoiceProfileRepo


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
class AssetKindSection:
    """一类素材的"库里有什么"（含够不够用的判定）。"""

    kind: AssetKind
    root: str
    stats: AssetStats
    items: tuple[_AssetRow, ...]
    shortfall: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "root": self.root,
            "stats": self.stats.to_dict(),
            "items": [row_to_dict(item) for item in self.items],
            "shortfall": self.shortfall,
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


def row_to_dict(row: _AssetRow) -> dict[str, Any]:
    """行 → JSON（``dataclasses.asdict`` 会把 ``Path`` 与枚举一起翻掉，这里显式写）。

    手写而不是 ``asdict``：面板要的是**稳定字段名**，而 ``asdict`` 会跟着 dataclass
    的字段改名一起变 —— 那正是前端契约测试要防的漂移。
    """
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
        """库里有什么（三类分节 + 够不够用）。"""
        sections: list[AssetKindSection] = []
        for kind in AssetKind:
            repo = self._repo(kind)
            sections.append(
                AssetKindSection(
                    kind=kind,
                    root=str(root_for(self._paths, kind)),
                    stats=repo.stats(),
                    items=tuple(repo.list_all()),
                    shortfall=self._shortfall(kind, repo.stats()),
                )
            )
        broll = sections[0].stats
        degraded = broll.enabled < BROLL_LIBRARY_MIN_CLIPS or broll.enabled_duration_ms < BROLL_LIBRARY_MIN_MS
        return AssetLibrary(
            sections=tuple(sections),
            degraded=degraded,
            note=(
                "跑酷素材不足：当前为黑屏降级模式（片头片尾仍可出片，画面只有水印与字幕）"
                if degraded
                else None
            ),
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
        if license is not None and license not in LICENSES:
            raise StudioError(
                f"授权类型不合法：{license}",
                code=ErrorCode.ASSET_INVALID,
                context={"license": license, "allowed": sorted(LICENSES)},
                remediation="从 self_recorded / authorized / cc0 / purchased 里选一个",
            )
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

    # ── 内部 ────────────────────────────────────────────────────────

    def _repo(self, kind: AssetKind) -> _AnyRepo:
        if kind is AssetKind.BROLL:
            return self._broll
        if kind is AssetKind.BGM:
            return self._bgm
        return self._voice

    def _shortfall(self, kind: AssetKind, stats: AssetStats) -> str | None:
        if kind is AssetKind.BROLL:
            if stats.enabled < BROLL_LIBRARY_MIN_CLIPS:
                return f"启用的跑酷素材只有 {stats.enabled} 条（建议 ≥ {BROLL_LIBRARY_MIN_CLIPS}）"
            if stats.enabled_duration_ms < BROLL_LIBRARY_MIN_MS:
                minutes = stats.enabled_duration_ms / 60_000
                return f"启用的跑酷素材共 {minutes:.1f} 分钟（建议 ≥ {BROLL_LIBRARY_MIN_MS // 60_000} 分钟）"
            return None
        if stats.enabled == 0:
            return (
                "一条都没有启用（"
                + ("没有 BGM ⇒ 出片走单轨人声" if kind is AssetKind.BGM else "没有音色 ⇒ 配音无法开始")
                + "）"
            )
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
    "asset.enable": "素材启用",
    "asset.disable": "素材停用",
    "asset.update": "素材更新",
}


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
