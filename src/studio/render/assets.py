"""素材选取：``os.listdir()`` 随机挑一个（T3.3 前置）。

口径**刻意保持粗糙**
--------------------
只做两件事：列出目录里的 ``*.mp4``、随机挑一个。**不做**内容校验、不查时长、
不算 pHash、不排黑帧、不做"疑似重复"判定 —— 那些都是"挑得更好"，而现在的目标是
"先出片"。素材是人工收集的跑酷循环，能进这个目录就默认可用；真挑到一条坏片子，
渲染会以 ffmpeg 的报错说话（那是**如实**的失败，比一个假绿勾强）。

唯一的例外：**"停用"必须生效**
-------------------------------
上面那条"能进目录就默认可用"有一个后果：面板上点了「停用」，出片照样会挑到它。
T4.8 的验收写着「禁用 ⇒ 随机化不再选中」，而这句话在这里落地的方式**不是**去读库
—— 本模块不认识素材 id、也不碰数据库（``render/`` 只依赖 ``core/`` 与 ``domain/``，
§02.1）。调用方（``services/render_service``）从库里查出"被停用的**文件名**"，
以 ``exclude=`` 传进来，这里只做一次集合减法。

所以从库到渲染器的信息**只有一个方向、一样东西**：一串文件名。库里没有行的文件
仍然默认可用（"能进目录就算数"照旧）—— 只有**明确被停用**的才被剔掉。这不是
"补一个校验模块"：校验是"这条素材够不够格"，而这里是"人有没有把它关掉"。
少读这一样东西的代价是面板与出片各说各话（面板说 0 条、片子里却在放跑酷），
多读的代价才是把这一层养成重资产校验。

挑不到 ⇒ 返回 ``None``，由调用方走**黑屏降级**（§04.2.8.6：素材为空 ⇒ ``bg_fill='black'``，
水印照常、照常出片，只把 ``degrade_reason='no_broll_assets'`` 写进 ``quality_json``）。
曾经这里是抛 ``RENDER_BROLL_MISSING`` 的，改成 ``None`` 是对着"无人值守"这条要求改的：
素材是人工收集的、随时可能被清空或全部禁用，而"没有底片"不该等于"今天出不了片" ——
纯黑底配字幕仍然是一条能发的片子（画质差，但不是废品）。

"全部被停用"与"目录是空的"在这里**走同一条路**（都返回 ``None``），这是有意的：
它们对出片链路是同一件事，区别只在留痕（``degrade_reason`` 说清是哪一种）。

``RENDER_BROLL_MISSING`` 这个错误码保留在 ``core/errors.py`` 里（历史任务的
``error_code`` 列里还存着它），但**本模块不再抛**。
"""

from __future__ import annotations

import random
from collections.abc import Collection, Sequence
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
    exclude: Collection[str] | None = None,
) -> Path | None:
    """从目录里随机挑一个；目录空 / 不存在 / 候选全被排除 ⇒ ``None``（**不抛**）。

    :param exclude: 要**跳过**的文件名集合（是文件名，不是路径）。由调用方从库里
        查出来（``services.asset_service.disabled_assets``）。比对的是 ``Path.name``：
        库里存的是绝对路径，而同一个素材在不同机器上的前缀可以完全不同，
        能稳定对上的只有文件名。
    """
    candidates = list_files(directory, suffixes)
    if exclude:
        candidates = [item for item in candidates if item.name not in exclude]
    if not candidates:
        return None
    return (rng or random).choice(candidates)


def pick_parkour_clip(
    paths: StudioPaths,
    *,
    rng: random.Random | None = None,
    exclude: Collection[str] | None = None,
) -> Path | None:
    """随机挑一条跑酷底片（渲染的画面来源）。挑不到 ⇒ ``None``（黑屏降级）。

    与水印同一条口径：**缺了不阻塞出片**。区别只在留痕 —— 水印缺失只写
    ``watermark.skipped_reason``，底片缺失还会置 ``degraded=true``，因为纯黑底是
    观众一眼看得出来的画质降级，而水印没了没人知道。

    :param exclude: 库里被**停用**的素材文件名（见模块 docstring）。全被排除 ⇒
        ``None`` ⇒ 黑屏降级 —— 这是对的："我把所有底片都停用了"应当看到纯黑底，
        而不是"停用没生效、照样随机挑一条"。
    """
    return pick_file(paths.mc_parkour_dir, CLIP_SUFFIXES, rng=rng, exclude=exclude)


def pick_bgm(
    paths: StudioPaths,
    *,
    rng: random.Random | None = None,
    exclude: Collection[str] | None = None,
) -> Path | None:
    """随机挑一条 BGM；没有 ⇒ ``None``（**跳过背景音乐**，不是失败）。

    与水印同一条口径：锦上添花的输入缺了不阻塞出片。

    :param exclude: 库里被**停用**的 BGM 文件名（同上）。
    """
    return pick_file(paths.bgm_dir, BGM_SUFFIXES, rng=rng, exclude=exclude)
