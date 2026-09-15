"""素材库的目录约定与扫盘发现（T4.8 · §3.3.14 / §04.2.8 / §4.3.1）。

三条纪律
--------
1. **只认约定内的文件**：名字不合规的东西一律进 :attr:`Discovery.strays`，
   **既不静默忽略、也不猜**。静默忽略会让用户以为"放进去了"，而实际上什么都没发生；
2. **发现与校验分开**：本模块只回答"磁盘上有什么、它叫什么名字"，
   "它够不够格"是 :mod:`studio.assets.validate` 的事 —— 混在一起的后果是
   "扫盘失败"与"素材不合格"在面板上分不出来；
3. **不碰数据库**：发现是纯文件系统的事，入库（写 ``broll_clips`` / ``bgm_tracks``）
   在别处。这样"磁盘上有什么"这个问题不需要一个能写的连接就能回答。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from studio.core.paths import StudioPaths

__all__ = [
    "ASSET_ID_PATTERN",
    "AUDIO_SUFFIXES",
    "BGM_PREFIX",
    "BROLL_PREFIX",
    "REF_STEM_PATTERN",
    "VIDEO_SUFFIXES",
    "VOICE_PROFILE_NAME",
    "VOICE_TEXT_NAME",
    "AssetCandidate",
    "AssetKind",
    "Discovery",
    "asset_id_for",
    "discover",
    "discover_all",
    "prefix_for",
    "root_for",
    "suffixes_for",
    "voice_profile",
    "voice_refs",
    "voice_text",
]


class AssetKind(StrEnum):
    """三类素材（与 §3.3.14 的两张表 + §4.3.1 的音色目录一一对应）。"""

    BROLL = "broll"
    VOICE = "voice"
    BGM = "bgm"


#: 素材 id 白名单（与 ``PERSONA_ID_PATTERN`` 同一条口径）。
#: 收紧到"小写字母 / 数字开头，后面可接 ``_`` ``-``"是**有意的**：id 会进 SQL 参数、
#: 会拼进文件名、会在 URL 里当 query，一个空格或一个 ``..`` 都是后面某处的事故。
ASSET_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_\-]{0,63}$"

#: 跑酷 / BGM 的文件名前缀（模板配置的通配就写在这上面，**不能改**）。
BROLL_PREFIX: Final[str] = "parkour_"
BGM_PREFIX: Final[str] = "bgm_"

#: 认得的扩展名。**同时要求前缀与扩展名**：``parkour_017.txt`` 不是素材。
VIDEO_SUFFIXES: Final[frozenset[str]] = frozenset({".mp4", ".mov", ".mkv", ".webm"})
AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})

#: 音色参考音的命名（``ref_01.wav`` … ``ref_03.wav``）。两位数是**有意的**：
#: 排序即顺序，而 ``ref.txt`` 的第 N 行对应的就是第 N 段。
REF_STEM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ref_(\d{2})$")

#: 音色目录里的两个旁车文件（§4.3.1 的目录约定，名字**冻结**）。
VOICE_TEXT_NAME: Final[str] = "ref.txt"
VOICE_PROFILE_NAME: Final[str] = "profile.json"


@dataclass(frozen=True, slots=True)
class AssetCandidate:
    """磁盘上的一个素材（**只描述，不打开**）。

    :param path: 主路径 —— 跑酷 / BGM 是那个文件，音色是那个目录；
    :param files: 属于这个素材的全部文件（音色 = 目录里的所有文件；其余 = 主文件一项）。
    """

    kind: AssetKind
    id: str
    path: Path
    files: tuple[Path, ...]

    @property
    def primary(self) -> Path:
        """拿去探测的那个文件（音色取第一段参考音；没有 ⇒ 主路径本身）。"""
        if self.kind is not AssetKind.VOICE:
            return self.path
        refs = voice_refs(self)
        return refs[0] if refs else self.path

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "path": str(self.path),
            "files": [str(item) for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class Discovery:
    """一次扫盘的结果。

    :param strays: 在素材目录里、但**不符合约定**的路径。面板必须把它们列出来并说清
        为什么 —— "我放进去了怎么没出现"是这类面板最常见的第一个问题；
    :param root_missing: 目录本身不在（全新机器、还没跑过 ``ensure_runtime_dirs``）。
        与"目录在、但是空的"分开：一个要去建目录，一个要去放素材。
    """

    kind: AssetKind
    root: Path
    candidates: tuple[AssetCandidate, ...]
    strays: tuple[Path, ...]
    root_missing: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "root": str(self.root),
            "root_missing": self.root_missing,
            "candidates": [item.to_dict() for item in self.candidates],
            "strays": [str(item) for item in self.strays],
        }


def root_for(paths: StudioPaths, kind: AssetKind) -> Path:
    """每类素材的根目录（**唯一真相**：路径不在别处再拼一遍）。"""
    if kind is AssetKind.BROLL:
        return paths.mc_parkour_dir
    if kind is AssetKind.BGM:
        return paths.bgm_dir
    return paths.voice_src_dir


def prefix_for(kind: AssetKind) -> str:
    """平铺素材的文件名前缀；音色没有前缀（它是目录名）。"""
    if kind is AssetKind.BROLL:
        return BROLL_PREFIX
    if kind is AssetKind.BGM:
        return BGM_PREFIX
    return ""


def suffixes_for(kind: AssetKind) -> frozenset[str]:
    return VIDEO_SUFFIXES if kind is AssetKind.BROLL else AUDIO_SUFFIXES


def asset_id_for(kind: AssetKind, path: Path) -> str | None:
    """路径 ⇒ 素材 id；不符合约定 ⇒ ``None``（调用方把它记成 stray）。

    音色取**目录名**，跑酷 / BGM 取**文件名去掉扩展名**。
    """
    candidate = path.name if kind is AssetKind.VOICE else path.stem
    if not re.fullmatch(ASSET_ID_PATTERN, candidate):
        return None
    prefix = prefix_for(kind)
    if prefix and not candidate.startswith(prefix):
        return None
    return candidate


def discover(paths: StudioPaths, kind: AssetKind) -> Discovery:
    """扫一遍某一类素材的根目录（**不递归**、不打开任何文件）。"""
    root = root_for(paths, kind)
    if not root.is_dir():
        return Discovery(kind=kind, root=root, candidates=(), strays=(), root_missing=True)
    if kind is AssetKind.VOICE:
        return _discover_voice(root)
    return _discover_flat(root, kind)


def discover_all(paths: StudioPaths) -> tuple[Discovery, ...]:
    """三类一起扫（面板首屏那一次请求）。"""
    return tuple(discover(paths, kind) for kind in AssetKind)


def _discover_flat(root: Path, kind: AssetKind) -> Discovery:
    """跑酷 / BGM：根目录下的平铺文件。子目录一律进 strays（约定里没有它们的位置）。"""
    candidates: list[AssetCandidate] = []
    strays: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            strays.append(entry)
            continue
        if entry.suffix.lower() not in suffixes_for(kind):
            strays.append(entry)
            continue
        asset_id = asset_id_for(kind, entry)
        if asset_id is None:
            strays.append(entry)
            continue
        candidates.append(AssetCandidate(kind=kind, id=asset_id, path=entry, files=(entry,)))
    return Discovery(
        kind=kind, root=root, candidates=tuple(candidates), strays=tuple(strays), root_missing=False
    )


def _discover_voice(root: Path) -> Discovery:
    """音色：根目录下的**子目录**，每个子目录是一个音色。

    目录里再套子目录 ⇒ 进 strays（"一音色一目录"是约定，嵌套会让"这个音色到底
    包含哪些文件"变成一个要递归回答的问题）。
    """
    candidates: list[AssetCandidate] = []
    strays: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            strays.append(entry)
            continue
        asset_id = asset_id_for(AssetKind.VOICE, entry)
        if asset_id is None:
            strays.append(entry)
            continue
        files: list[Path] = []
        for item in sorted(entry.iterdir()):
            if item.is_file():
                files.append(item)
            else:
                strays.append(item)
        candidates.append(AssetCandidate(kind=AssetKind.VOICE, id=asset_id, path=entry, files=tuple(files)))
    return Discovery(
        kind=AssetKind.VOICE,
        root=root,
        candidates=tuple(candidates),
        strays=tuple(strays),
        root_missing=False,
    )


def voice_refs(candidate: AssetCandidate) -> tuple[Path, ...]:
    """参考音片段（``ref_NN.<音频扩展名>``），**按文件名排序**。

    排序就是顺序：``ref.txt`` 的第 N 行对应第 N 段（§4.3.1），所以这里的顺序
    是契约的一部分，不能靠"文件系统返回的顺序"。
    """
    found = [
        item
        for item in candidate.files
        if item.suffix.lower() in AUDIO_SUFFIXES and REF_STEM_PATTERN.fullmatch(item.stem)
    ]
    return tuple(sorted(found, key=lambda item: item.name))


def voice_text(candidate: AssetCandidate) -> Path | None:
    """逐字文本 ``ref.txt``（与参考音一一对应）。"""
    return _sidecar(candidate, VOICE_TEXT_NAME)


def voice_profile(candidate: AssetCandidate) -> Path | None:
    """来源登记 ``profile.json``（R2 合规留档）。"""
    return _sidecar(candidate, VOICE_PROFILE_NAME)


def _sidecar(candidate: AssetCandidate, name: str) -> Path | None:
    for item in candidate.files:
        if item.name == name:
            return item
    return None
