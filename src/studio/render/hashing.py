"""合成指纹（T3.7 · §04.2.8.7）—— "这次要渲的东西和盘上那支是不是同一支"的判据。

为什么要指纹
------------
M3 门禁里有一句「换稿不重剪」：**同素材 + 同水印，换稿件直接出片**。要兑现它就得能回答
一个问题 —— 这一次要渲的，和已经渲好的那支，是不是同一个？判据不能是"文件名像不像"
（每次渲染都带时间戳），也不能是"配置有没有变过"（素材是随机挑的、每次都不一样）——
只能把**所有会改变像素的东西**摊平成一个哈希。

什么进哈希、什么不进
--------------------
**进**：编码参数、总时长、画面来源（底片路径或"纯黑"）、配音、BGM、水印及其位置、字幕
ASS、混音参数，以及上述文件的 ``sha256``（路径没变而内容换了是常事 —— 重录一遍配音、
换一条同名素材，光看路径是看不出来的）。

**不进**：输出路径与文件名（带时间戳，每次都不同）、``-threads``（只影响编码快慢，不影响
画面）、以及任何时间戳。

排除的那几样是**刻意**的：把它们算进去，哈希就永远不可能命中，"缓存"会退化成一段看起来
在工作、实际从不生效的代码 —— 而那种代码比没有更糟，因为它会让人以为省了时间。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from studio.render.composite import CompositeRequest, bg_fill

__all__ = [
    "HASH_LENGTH",
    "canonical_plan",
    "composite_hash",
    "file_digest",
    "input_digests",
]

#: 指纹长度（hex 字符数）。§04.2.8.7 定的是 32 —— 128 位，碰撞概率对"同一台机器上
#: 几百支片子"这个量级来说远在噪声以下，而且短到能直接印在 manifest 里肉眼比对。
HASH_LENGTH: Final[int] = 32

#: 读文件的块大小。素材动辄几十 MB，``read_bytes()`` 会一次性顶出一大块内存 ——
#: 渲染本来就吃内存，没必要在这里再抢一块。
_CHUNK: Final[int] = 1 << 20


def file_digest(path: Path) -> str:
    """文件的 ``sha256``（hex，分块读）。

    文件不在 ⇒ ``"missing"`` 而不是抛异常：指纹是**留痕与优化**用的，不是门禁。为它
    让整支片子渲不出来，是拿优化堵主链路。
    """
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def input_digests(req: CompositeRequest) -> dict[str, str]:
    """每一路**真实输入文件**的 ``sha256``。

    纯黑底（``clip is None``）没有文件，所以不出现这个键 —— 它的"身份"已经由
    :func:`canonical_plan` 里的 ``video_source='black'`` 表达了。
    """
    digests: dict[str, str] = {"voice": file_digest(req.voice)}
    if req.clip is not None:
        digests["clip"] = file_digest(req.clip)
    if req.bgm is not None:
        digests["bgm"] = file_digest(req.bgm)
    if req.subtitle is not None:
        digests["subtitle"] = file_digest(req.subtitle)
    if req.watermark is not None and req.watermark.placement is not None:
        digests["watermark"] = file_digest(req.watermark.spec.image_path)
    return digests


def canonical_plan(req: CompositeRequest) -> dict[str, Any]:
    """会改变成片内容的那部分请求（**不含**输出路径 / 线程数 / 时间戳）。

    ``encoder`` 用 ``profile.output_args()`` 而不是 ``profile.name``：改一个档位的
    CRF 而不改名字，画面就变了；只哈希名字会把这种改动放过去。
    """
    plan: dict[str, Any] = {
        "profile": req.profile.name,
        "encoder": req.profile.output_args(),
        "canvas": list(req.profile.canvas),
        "fps": req.profile.fps,
        "duration_ms": req.duration_ms,
        "video_source": req.clip.as_posix() if req.clip is not None else bg_fill(req),
        "voice": req.voice.as_posix(),
        "bgm": req.bgm.as_posix() if req.bgm is not None else None,
        "subtitle": req.subtitle.as_posix() if req.subtitle is not None else None,
        "font_dir": req.subtitle_font_dir.as_posix() if req.subtitle_font_dir is not None else None,
        "mix": asdict(req.mix),
    }
    watermark = req.watermark
    if watermark is not None and watermark.placement is not None:
        # 只取**影响画面**的那几样（位置 / 尺寸 / 透明度 / 图源），不取 asset 里
        # 那些"描述这张图是什么"的元数据 —— 后者变了但像素没变时不该让缓存失效。
        plan["watermark"] = {
            "image": watermark.spec.image_path.as_posix(),
            "opacity": watermark.spec.opacity,
            "placement": watermark.placement.to_dict(),
        }
    else:
        plan["watermark"] = None
    return plan


def composite_hash(req: CompositeRequest, *, digests: Mapping[str, str] | None = None) -> str:
    """``sha256(canonical_plan | digests)`` 的前 :data:`HASH_LENGTH` 位。

    ``digests`` 可注入：调用方已经算过一遍时传进来，避免对同一个几十 MB 的素材连读两次
    （每次渲染都要算，那是实打实的 I/O）。
    """
    payload = {
        "plan": canonical_plan(req),
        "inputs": dict(digests if digests is not None else input_digests(req)),
    }
    # sort_keys + 紧凑分隔符：字典序稳定、字节数固定。少了这两样，同样的内容会因为
    # 键序或空格不同而算出不同的哈希 —— 那是最难查的一类"缓存莫名其妙不命中"。
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_LENGTH]
