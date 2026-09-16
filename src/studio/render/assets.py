"""素材选取：``os.listdir()`` 随机挑一个（T3.3 前置）。

口径**刻意保持粗糙**
--------------------
只做两件事：列出目录里的 ``*.mp4``、随机挑一个。**不做**内容校验、不查时长、
不算 pHash、不排黑帧、不做"疑似重复"判定 —— 那些都是"挑得更好"，而现在的目标是
"先出片"。素材是人工收集的跑酷循环，能进这个目录就默认可用；真挑到一条坏片子，
渲染会以 ffmpeg 的报错说话（那是**如实**的失败，比一个假绿勾强）。

挑不到 ⇒ 返回 ``None``，由调用方走**黑屏降级**（§04.2.8.6：素材为空 ⇒ ``bg_fill='black'``，
水印照常、照常出片，只把 ``degrade_reason='no_broll_assets'`` 写进 ``quality_json``）。
曾经这里是抛 ``RENDER_BROLL_MISSING`` 的，改成 ``None`` 是对着"无人值守"这条要求改的：
素材是人工收集的、随时可能被清空或全部禁用，而"没有底片"不该等于"今天出不了片" ——
纯黑底配字幕仍然是一条能发的片子（画质差，但不是废品）。

``RENDER_BROLL_MISSING`` 这个错误码保留在 ``core/errors.py`` 里（历史任务的
``error_code`` 列里还存着它），但**本模块不再抛**。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from studio.core.paths import StudioPaths

__all__ = [
    "BGM_SUFFIXES",
    "CLIP_SUFFIXES",
    "list_files",
    "pick_bgm",
    "pick_file",
    "pick_parkour_clip",
]

#: 底片认的扩展名（跑酷素材库）
CLIP_SUFFIXES: Final[tuple[str, ...]] = (".mp4", ".mov", ".mkv")

#: BGM 认的扩展名（与 `config/outputs.yaml` 的 `bgm.glob` 同一批）
BGM_SUFFIXES: Final[tuple[str, ...]] = (".mp3", ".m4a", ".wav", ".flac")


def list_files(directory: Path, suffixes: Sequence[str]) -> list[Path]:
    """目录下符合扩展名的文件（**排序后**返回，保证"同一目录两次调用结果稳定"）。

    用 ``Path.iterdir``（= 列目录）而不是 ``Path.glob``：这里要的是"这个目录里
    有什么"，不递归、不匹配模式、不解析路径语法 —— 目录名里带 ``[`` ``]`` 时 glob
    会把它们当成字符集，而素材目录的名字是人工起的，没必要让它们遵守 glob 的元字符。

    排序是为了让 ``pick_file`` 的随机性**可复现**：同样的目录 + 同样的种子
    ⇒ 同样的选择（测试与"这条片子是用哪条底片渲的"排障都需要这个）。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    lowered = tuple(suffix.lower() for suffix in suffixes)
    found: list[Path] = []
    for entry in entries:
        if not entry.name.lower().endswith(lowered):
            continue
        if entry.is_file():
            found.append(entry)
    return sorted(found)


def pick_file(
    directory: Path,
    suffixes: Sequence[str],
    *,
    rng: random.Random | None = None,
) -> Path | None:
    """从目录里随机挑一个；目录空 / 不存在 ⇒ ``None``（**不抛**）。"""
    candidates = list_files(directory, suffixes)
    if not candidates:
        return None
    return (rng or random).choice(candidates)


def pick_parkour_clip(paths: StudioPaths, *, rng: random.Random | None = None) -> Path | None:
    """随机挑一条跑酷底片（渲染的画面来源）。挑不到 ⇒ ``None``（黑屏降级）。

    与水印同一条口径：**缺了不阻塞出片**。区别只在留痕 —— 水印缺失只写
    ``watermark.skipped_reason``，底片缺失还会置 ``degraded=true``，因为纯黑底是
    观众一眼看得出来的画质降级，而水印没了没人知道。
    """
    return pick_file(paths.mc_parkour_dir, CLIP_SUFFIXES, rng=rng)


def pick_bgm(paths: StudioPaths, *, rng: random.Random | None = None) -> Path | None:
    """随机挑一条 BGM；没有 ⇒ ``None``（**跳过背景音乐**，不是失败）。

    与水印同一条口径：锦上添花的输入缺了不阻塞出片。
    """
    return pick_file(paths.bgm_dir, BGM_SUFFIXES, rng=rng)
