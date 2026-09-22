"""素材库三张表的仓储（T4.8 · §3.3.14 / §4.3.1）。

重扫只刷新"机器算得出来的事实"，**绝不覆盖"人做过的决定"**
--------------------------------------------------------
``upsert`` 的语义按列分成两类，这条线不能含糊：

- **机器事实**（``sha256`` / ``duration_ms`` / 宽高帧率 / 采样率 / 响度 / 段数…）：
  每次扫盘重算，重算结果直接覆盖 —— 文件被重新压制过，旧时长就是错的；
- **人的决定**（``enabled`` / ``license`` / ``usable_from_ms`` / ``usable_to_ms`` /
  ``has_text`` / ``tags`` / ``mood`` / ``bpm`` / ``source_url`` / ``proof_path``…）：
  重扫**一个字都不动**。它们是"这条素材能不能用、按什么授权用"的凭据，
  而扫描是**每天都会跑的机器动作**：让它顺手把 ``enabled`` 刷回 1，
  用户"停用某条素材"的操作就会在第二天早上静默失效（陷阱 #188）。

为什么"重复按 sha256 拒绝"落在仓储层
------------------------------------
因为它是**唯一知道全表内容的地方**：``broll_clips.sha256`` 没有唯一索引
（同一段跑酷允许以不同 usable 区间再入库，那是产品决定），所以"这条内容已经
在库里了"只能靠查询回答。放到服务层去查，等于让每个调用方各写一遍这段 SQL。

为什么 upsert 返回动作而不是布尔
--------------------------------
面板要说清"这次扫盘干了什么"：``created``（新入库）/ ``refreshed``（刷新了事实）
/ ``unchanged``（一个字节没变）/ ``duplicate``（内容重复，没入库）。
一个 ``True/False`` 会让"扫了 60 条、0 条新增"与"扫了 60 条、60 条都是重复"
在界面上长得一模一样。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from studio.db.engine import transaction
from studio.db.models import BgmTrackRow, BrollClipRow, VoiceProfileRow

__all__ = [
    "CLIP_PATCH_FIELDS",
    "TRACK_PATCH_FIELDS",
    "VOICE_PATCH_FIELDS",
    "AssetStats",
    "BgmTrackRepo",
    "BrollClipRepo",
    "IngestAction",
    "UpsertResult",
    "VoiceProfileRepo",
]


class IngestAction(StrEnum):
    """一次入库调用干了什么。"""

    CREATED = "created"
    REFRESHED = "refreshed"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """入库结果。``reason`` 只在 ``duplicate`` 时有话要说。"""

    action: IngestAction
    id: str
    reason: str | None = None

    @property
    def stored(self) -> bool:
        """这条素材现在在库里吗（``duplicate`` 是唯一没进去的一种）。"""
        return self.action is not IngestAction.DUPLICATE

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "action": str(self.action), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class AssetStats:
    """一类素材的家底（面板顶部那几行数字 + "素材够不够"的判据输入）。

    ``total_duration_ms`` 与 ``enabled_duration_ms`` **分开报**：随机化只用启用的
    那些，而"我到底有多少素材"是另一个问题。合成一个数，用户停用一批素材之后就
    再也说不清"是素材真的不够，还是我把它们关掉了"。
    """

    total: int
    enabled: int
    total_duration_ms: int
    enabled_duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "enabled": self.enabled,
            "total_duration_ms": self.total_duration_ms,
            "enabled_duration_ms": self.enabled_duration_ms,
        }


#: 三类素材各自**允许 PATCH 的列**（白名单：列名要拼进 SQL，绝不允许来自请求）。
#: 这里只放"人能改的" —— 机器事实（``sha256`` / ``duration_ms`` / ``ref_count``…）
#: 不在里面：让请求能把时长改成 0，等于让面板与文件系统对不上账。
CLIP_PATCH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "license",
        "tags",
        "has_text",
        "usable_from_ms",
        "usable_to_ms",
        "source_url",
        "proof_path",
        "licensed_to",
        "enabled",
    }
)

TRACK_PATCH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "license",
        "tags",
        "mood",
        "bpm",
        "loopable",
        "source_url",
        "proof_path",
        "licensed_to",
        "enabled",
    }
)

#: 音色可改的列。
#:
#: ``proof_path`` 在列表里**是有原因的**：合规快照（``publish/compliance.py``）把音色的
#: 来源登记读成 ``proof_path``（目录里的 ``profile.json``），而它是三条素材线里**唯一**
#: 一条"面板上没有入口去补"的留痕 —— 一条入库时还没有 ``profile.json`` 的音色，于是
#: 永远补不上。补它不是"多给一个可改字段"，是让那一栏的红字**有地方修**。
VOICE_PATCH_FIELDS: Final[frozenset[str]] = frozenset(
    {"license", "source_url", "proof_path", "licensed_to", "enabled"}
)

#: PATCH 的字段名 ⇄ 列名（只有需要换名的才在这里；其余同名直通）
_RENAMED_COLUMNS: Final[dict[str, str]] = {"tags": "tags_json"}

#: 布尔字段（DDL 里是 INTEGER + CHECK IN (0,1)）
_BOOL_FIELDS: Final[frozenset[str]] = frozenset({"has_text", "loopable", "enabled"})


def _patch(
    connection: sqlite3.Connection,
    *,
    table: str,
    asset_id: str,
    fields: dict[str, Any],
    allowed: frozenset[str],
) -> int:
    """按白名单改几列；返回命中行数（``0`` = 这个 id 不在表里）。

    ``fields`` 为空 ⇒ :class:`ValueError`：一个"什么都没改"的 PATCH 要么是前端
    把空表单提交了，要么是调用方漏传 —— 静默返回成功会让面板显示"已保存"，
    而盘上什么都没变。
    """
    if not fields:
        raise ValueError("PATCH 至少要带一个字段")
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(f"不可改的字段：{unknown}（允许：{sorted(allowed)}）")
    assignments: list[str] = []
    params: list[Any] = []
    for name, raw in fields.items():
        column = _RENAMED_COLUMNS.get(name, name)
        if name == "tags":
            stored: Any = json.dumps([str(item) for item in (raw or ())], ensure_ascii=False)
        elif name in _BOOL_FIELDS:
            stored = 1 if raw else 0
        else:
            stored = raw
        assignments.append(f"{column} = ?")
        params.append(stored)
    params.append(asset_id)
    cursor = connection.execute(f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?", params)
    return cursor.rowcount


def _norm_path(path: str | Path) -> str:
    """库里的路径一律 **绝对 + 正斜杠**（§3.3.14 的列注释就是这个形状）。

    正斜杠而不是平台分隔符：``path`` 有 UNIQUE 约束，而同一个文件写成
    ``data\\assets\\x.mp4`` 与 ``data/assets/x.mp4`` 是两行 —— 重复入库时
    第二条会被 UNIQUE 拒绝，而报错信息说的是"路径重复"，看起来像 bug。
    """
    return Path(path).resolve().as_posix()


_CLIP_COLUMNS: Final[str] = (
    "id, path, sha256, duration_ms, usable_from_ms, usable_to_ms, width, height, fps, tags_json, "
    "has_text, thumb_path, phash, frame_hashes_json, license, source_url, proof_path, licensed_to, "
    "use_count, last_used_at, enabled, created_at"
)

_INSERT_CLIP: Final[str] = """
INSERT INTO broll_clips(
    id, path, sha256, duration_ms, width, height, fps, license,
    usable_from_ms, usable_to_ms, thumb_path, tags_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: 刷新**只动机器事实**（见模块 docstring 的列分类）。
_REFRESH_CLIP: Final[str] = """
UPDATE broll_clips
   SET path = ?, sha256 = ?, duration_ms = ?, width = ?, height = ?, fps = ?,
       thumb_path = COALESCE(?, thumb_path)
 WHERE id = ?
"""

_TRACK_COLUMNS: Final[str] = (
    "id, path, sha256, duration_ms, sample_rate, channels, bitrate_kbps, loudness_lufs, bpm, mood, "
    "tags_json, loopable, license, source_url, proof_path, licensed_to, use_count, last_used_at, "
    "enabled, created_at"
)

_INSERT_TRACK: Final[str] = """
INSERT INTO bgm_tracks(
    id, path, sha256, duration_ms, sample_rate, channels, bitrate_kbps, loudness_lufs,
    license, tags_json, loopable
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_REFRESH_TRACK: Final[str] = """
UPDATE bgm_tracks
   SET path = ?, sha256 = ?, duration_ms = ?, sample_rate = ?, channels = ?, bitrate_kbps = ?,
       loudness_lufs = COALESCE(?, loudness_lufs)
 WHERE id = ?
"""

_VOICE_COLUMNS: Final[str] = (
    "id, path, ref_count, total_duration_ms, sample_rate, peak_db, text_path, proof_path, license, "
    "source_url, licensed_to, enabled, use_count, last_used_at, created_at"
)

_INSERT_VOICE: Final[str] = """
INSERT INTO voice_profiles(
    id, path, ref_count, total_duration_ms, sample_rate, peak_db, text_path, proof_path,
    license, source_url, licensed_to
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_REFRESH_VOICE: Final[str] = """
UPDATE voice_profiles
   SET path = ?, ref_count = ?, total_duration_ms = ?, sample_rate = ?, peak_db = ?,
       text_path = ?, proof_path = ?
 WHERE id = ?
"""


def _stats(connection: sqlite3.Connection, table: str, duration_column: str) -> AssetStats:
    """家底统计（一次查询取四个数）。

    ``SUM(CASE WHEN enabled = 1 …)`` 而不是跑四条 ``SELECT COUNT(*)``：
    面板刷新是**每个视图切换都发生**的事，四条查询里任意两条之间素材被停用，
    面板上就会出现"启用 5 条、启用总时长 3 分钟"这种自相矛盾的一屏。
    """
    # 表名与列名都来自本模块的常量（不含用户输入），所以这里是拼接而不是参数化。
    # 时长列名要传进来：``voice_profiles`` 那一列叫 ``total_duration_ms``
    # （音色是"几段参考音之和"，不是"一个文件的时长"）—— 名字不同是对的，
    # 硬凑成一个名字只会让另一个表的含义变模糊。
    row = connection.execute(
        f"SELECT COUNT(*), "
        f"COALESCE(SUM(enabled), 0), "
        f"COALESCE(SUM({duration_column}), 0), "
        f"COALESCE(SUM(CASE WHEN enabled = 1 THEN {duration_column} ELSE 0 END), 0) "
        f"FROM {table}"
    ).fetchone()
    if row is None:
        return AssetStats(total=0, enabled=0, total_duration_ms=0, enabled_duration_ms=0)
    return AssetStats(
        total=int(row[0]),
        enabled=int(row[1]),
        total_duration_ms=int(row[2]),
        enabled_duration_ms=int(row[3]),
    )


class BrollClipRepo:
    """``broll_clips`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert(
        self,
        *,
        clip_id: str,
        path: str | Path,
        sha256: str,
        duration_ms: int,
        license: str,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        usable_from_ms: int = 0,
        usable_to_ms: int | None = None,
        thumb_path: str | Path | None = None,
        tags: Sequence[str] = (),
    ) -> UpsertResult:
        """入库或刷新一条跑酷素材。

        ``license`` 是**必填**（DDL 就是 ``NOT NULL``）：扫盘时读不出授权，
        所以缺授权的素材根本走不到这里 —— 它在扫描报告里是一条
        ``license_missing``，由人在面板上补齐授权后再入库（T4.8 裁定 188）。
        """
        normalized = _norm_path(path)
        thumb = None if thumb_path is None else _norm_path(thumb_path)
        with transaction(self._connection, immediate=True):
            existing = self.get(clip_id)
            if existing is None:
                duplicate = self.find_by_sha256(sha256)
                if duplicate is not None and duplicate.id != clip_id:
                    return UpsertResult(
                        action=IngestAction.DUPLICATE,
                        id=clip_id,
                        reason=f"内容与 {duplicate.id} 完全相同（sha256 相同），未入库",
                    )
                self._connection.execute(
                    _INSERT_CLIP,
                    (
                        clip_id,
                        normalized,
                        sha256,
                        int(duration_ms),
                        width,
                        height,
                        fps,
                        license,
                        int(usable_from_ms),
                        usable_to_ms,
                        thumb,
                        json.dumps(list(tags), ensure_ascii=False),
                    ),
                )
                return UpsertResult(action=IngestAction.CREATED, id=clip_id)
            if (
                existing.sha256 == sha256
                and existing.duration_ms == int(duration_ms)
                and existing.width == width
                and existing.height == height
                and existing.fps == fps
                and existing.path == normalized
                and (thumb is None or existing.thumb_path == thumb)
            ):
                return UpsertResult(action=IngestAction.UNCHANGED, id=clip_id)
            self._connection.execute(
                _REFRESH_CLIP,
                (normalized, sha256, int(duration_ms), width, height, fps, thumb, clip_id),
            )
            return UpsertResult(action=IngestAction.REFRESHED, id=clip_id)

    def get(self, clip_id: str) -> BrollClipRow | None:
        row = self._connection.execute(
            f"SELECT {_CLIP_COLUMNS} FROM broll_clips WHERE id = ?", (clip_id,)
        ).fetchone()
        return None if row is None else BrollClipRow.from_row(row)

    def find_by_sha256(self, sha256: str) -> BrollClipRow | None:
        """按内容指纹找（"这条素材是不是已经在库里了"）。"""
        row = self._connection.execute(
            f"SELECT {_CLIP_COLUMNS} FROM broll_clips WHERE sha256 = ? ORDER BY id LIMIT 1",
            (sha256,),
        ).fetchone()
        return None if row is None else BrollClipRow.from_row(row)

    def list_all(self, *, enabled_only: bool = False) -> list[BrollClipRow]:
        clause = " WHERE enabled = 1" if enabled_only else ""
        rows = self._connection.execute(
            f"SELECT {_CLIP_COLUMNS} FROM broll_clips{clause} ORDER BY id"
        ).fetchall()
        return [BrollClipRow.from_row(row) for row in rows]

    def set_enabled(self, clip_id: str, enabled: bool) -> bool:
        """启用 / 停用（**只改这一列** —— 素材文件原地不动，§T4.8 误删保护）。

        返回是否命中：没命中就是 id 打错了，调用方据此回 404 而不是假装成功
        （面板上"停用成功"但列表里还亮着，是最难查的一类 bug）。
        """
        cursor = self._connection.execute(
            "UPDATE broll_clips SET enabled = ? WHERE id = ?", (1 if enabled else 0, clip_id)
        )
        return cursor.rowcount > 0

    def patch(self, clip_id: str, **fields: Any) -> BrollClipRow | None:
        """改人填字段（白名单见 :data:`CLIP_PATCH_FIELDS`）；行不存在 ⇒ ``None``。"""
        if (
            _patch(
                self._connection,
                table="broll_clips",
                asset_id=clip_id,
                fields=fields,
                allowed=CLIP_PATCH_FIELDS,
            )
            == 0
        ):
            return None
        return self.get(clip_id)

    def delete(self, clip_id: str) -> bool:
        """删掉这一行（**只删库里的行** —— 盘上那个文件由调用方决定，见 ``AssetService.delete``）。

        返回是否命中：没命中就是 id 打错了，调用方据此回 404，而不是回一句
        "删掉了"、而列表里那一行还亮着。``broll_usage`` 是 ``ON DELETE CASCADE``，
        所以这一条的历史用量跟着一起走（外键在 ``migrate.py`` 里已开）。
        """
        cursor = self._connection.execute("DELETE FROM broll_clips WHERE id = ?", (clip_id,))
        return cursor.rowcount > 0

    def stats(self) -> AssetStats:
        return _stats(self._connection, "broll_clips", "duration_ms")


class BgmTrackRepo:
    """``bgm_tracks`` 的唯一读写入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert(
        self,
        *,
        track_id: str,
        path: str | Path,
        sha256: str,
        duration_ms: int,
        license: str,
        sample_rate: int | None = None,
        channels: int | None = None,
        bitrate_kbps: int | None = None,
        loudness_lufs: float | None = None,
        tags: Sequence[str] = (),
        loopable: bool = False,
    ) -> UpsertResult:
        """入库或刷新一条 BGM（``license`` 必填，理由同 :meth:`BrollClipRepo.upsert`）。"""
        normalized = _norm_path(path)
        with transaction(self._connection, immediate=True):
            existing = self.get(track_id)
            if existing is None:
                duplicate = self.find_by_sha256(sha256)
                if duplicate is not None and duplicate.id != track_id:
                    return UpsertResult(
                        action=IngestAction.DUPLICATE,
                        id=track_id,
                        reason=f"内容与 {duplicate.id} 完全相同（sha256 相同），未入库",
                    )
                self._connection.execute(
                    _INSERT_TRACK,
                    (
                        track_id,
                        normalized,
                        sha256,
                        int(duration_ms),
                        sample_rate,
                        channels,
                        bitrate_kbps,
                        loudness_lufs,
                        license,
                        json.dumps(list(tags), ensure_ascii=False),
                        1 if loopable else 0,
                    ),
                )
                return UpsertResult(action=IngestAction.CREATED, id=track_id)
            if (
                existing.sha256 == sha256
                and existing.duration_ms == int(duration_ms)
                and existing.sample_rate == sample_rate
                and existing.channels == channels
                and existing.bitrate_kbps == bitrate_kbps
                and (loudness_lufs is None or existing.loudness_lufs == loudness_lufs)
                and existing.path == normalized
            ):
                return UpsertResult(action=IngestAction.UNCHANGED, id=track_id)
            self._connection.execute(
                _REFRESH_TRACK,
                (
                    normalized,
                    sha256,
                    int(duration_ms),
                    sample_rate,
                    channels,
                    bitrate_kbps,
                    loudness_lufs,
                    track_id,
                ),
            )
            return UpsertResult(action=IngestAction.REFRESHED, id=track_id)

    def get(self, track_id: str) -> BgmTrackRow | None:
        row = self._connection.execute(
            f"SELECT {_TRACK_COLUMNS} FROM bgm_tracks WHERE id = ?", (track_id,)
        ).fetchone()
        return None if row is None else BgmTrackRow.from_row(row)

    def find_by_sha256(self, sha256: str) -> BgmTrackRow | None:
        row = self._connection.execute(
            f"SELECT {_TRACK_COLUMNS} FROM bgm_tracks WHERE sha256 = ? ORDER BY id LIMIT 1",
            (sha256,),
        ).fetchone()
        return None if row is None else BgmTrackRow.from_row(row)

    def list_all(self, *, enabled_only: bool = False) -> list[BgmTrackRow]:
        clause = " WHERE enabled = 1" if enabled_only else ""
        rows = self._connection.execute(
            f"SELECT {_TRACK_COLUMNS} FROM bgm_tracks{clause} ORDER BY id"
        ).fetchall()
        return [BgmTrackRow.from_row(row) for row in rows]

    def set_enabled(self, track_id: str, enabled: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE bgm_tracks SET enabled = ? WHERE id = ?", (1 if enabled else 0, track_id)
        )
        return cursor.rowcount > 0

    def patch(self, track_id: str, **fields: Any) -> BgmTrackRow | None:
        """改人填字段（白名单见 :data:`TRACK_PATCH_FIELDS`）；行不存在 ⇒ ``None``。"""
        if (
            _patch(
                self._connection,
                table="bgm_tracks",
                asset_id=track_id,
                fields=fields,
                allowed=TRACK_PATCH_FIELDS,
            )
            == 0
        ):
            return None
        return self.get(track_id)

    def delete(self, track_id: str) -> bool:
        """删掉这一行（**只删库里的行**，盘上那个文件由调用方决定）。"""
        cursor = self._connection.execute("DELETE FROM bgm_tracks WHERE id = ?", (track_id,))
        return cursor.rowcount > 0

    def stats(self) -> AssetStats:
        return _stats(self._connection, "bgm_tracks", "duration_ms")


class VoiceProfileRepo:
    """``voice_profiles`` 的唯一读写入口（迁移 ``0009``）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert(
        self,
        *,
        voice_id: str,
        path: str | Path,
        ref_count: int,
        total_duration_ms: int,
        sample_rate: int | None = None,
        peak_db: float | None = None,
        text_path: str | Path | None = None,
        proof_path: str | Path | None = None,
        license: str | None = None,
    ) -> UpsertResult:
        """入库或刷新一个音色。

        与跑酷 / BGM 的区别：音色**没有 sha256**（它是一组文件），所以没有
        "重复拒绝"这一支 —— 幂等由 ``id``（目录名）本身保证，同一个目录名永远只有
        一行。``license`` 允许为空（§4.3.1 对音色的前置是 ``profile.json``，
        授权类型可能写在里面、也可能根本没写；替用户填一个默认值等于伪造合规留痕）。
        """
        normalized = _norm_path(path)
        text = None if text_path is None else _norm_path(text_path)
        proof = None if proof_path is None else _norm_path(proof_path)
        with transaction(self._connection, immediate=True):
            existing = self.get(voice_id)
            if existing is None:
                self._connection.execute(
                    _INSERT_VOICE,
                    (
                        voice_id,
                        normalized,
                        int(ref_count),
                        int(total_duration_ms),
                        sample_rate,
                        peak_db,
                        text,
                        proof,
                        license,
                        None,
                        None,
                    ),
                )
                return UpsertResult(action=IngestAction.CREATED, id=voice_id)
            if (
                existing.ref_count == int(ref_count)
                and existing.total_duration_ms == int(total_duration_ms)
                and existing.sample_rate == sample_rate
                and existing.peak_db == peak_db
                and existing.path == normalized
                and existing.text_path == text
                and existing.proof_path == proof
            ):
                return UpsertResult(action=IngestAction.UNCHANGED, id=voice_id)
            self._connection.execute(
                _REFRESH_VOICE,
                (
                    normalized,
                    int(ref_count),
                    int(total_duration_ms),
                    sample_rate,
                    peak_db,
                    text,
                    proof,
                    voice_id,
                ),
            )
            return UpsertResult(action=IngestAction.REFRESHED, id=voice_id)

    def get(self, voice_id: str) -> VoiceProfileRow | None:
        row = self._connection.execute(
            f"SELECT {_VOICE_COLUMNS} FROM voice_profiles WHERE id = ?", (voice_id,)
        ).fetchone()
        return None if row is None else VoiceProfileRow.from_row(row)

    def list_all(self, *, enabled_only: bool = False) -> list[VoiceProfileRow]:
        clause = " WHERE enabled = 1" if enabled_only else ""
        rows = self._connection.execute(
            f"SELECT {_VOICE_COLUMNS} FROM voice_profiles{clause} ORDER BY id"
        ).fetchall()
        return [VoiceProfileRow.from_row(row) for row in rows]

    def set_enabled(self, voice_id: str, enabled: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE voice_profiles SET enabled = ? WHERE id = ?", (1 if enabled else 0, voice_id)
        )
        return cursor.rowcount > 0

    def patch(self, voice_id: str, **fields: Any) -> VoiceProfileRow | None:
        """改人填字段（白名单见 :data:`VOICE_PATCH_FIELDS`）；行不存在 ⇒ ``None``。"""
        if (
            _patch(
                self._connection,
                table="voice_profiles",
                asset_id=voice_id,
                fields=fields,
                allowed=VOICE_PATCH_FIELDS,
            )
            == 0
        ):
            return None
        return self.get(voice_id)

    def delete(self, voice_id: str) -> bool:
        """删掉这一行（**只删库里的行** —— 参考音目录由调用方决定）。

        这一条与跑酷 / BGM 有一处**实质差别**：配音只认 ``voice_profiles`` 表，
        所以删了行 = 这个音色立刻从配音池里消失；而跑酷删了行照样会被出片挑到
        （渲染器只列目录）。面板上那句话得跟着分开说。
        """
        cursor = self._connection.execute("DELETE FROM voice_profiles WHERE id = ?", (voice_id,))
        return cursor.rowcount > 0

    def stats(self) -> AssetStats:
        return _stats(self._connection, "voice_profiles", "total_duration_ms")
