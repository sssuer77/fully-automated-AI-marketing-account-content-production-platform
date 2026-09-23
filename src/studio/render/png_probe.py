"""PNG 资产实测（T3.2 / T6.5）—— **水印与人物贴图共用同一份解析**。

为什么单独成一个模块
--------------------
水印（T3.2）与人物贴图（T6.5）问的是**同一个问题**："这张图在不在 / 多大 / 带不带
透明通道？" 抄第二份的代价不是几十行代码，而是**两份口径会漂**：某天给其中一份补上
"调色板 + tRNS 也算有透明"，另一份没跟上，于是同一张 PNG 在面板上显示"可用"、
在渲染路径里被判"不可用"。所以解析只写一遍，两个调用方各自决定**怎么用**这个结论。

为什么不引 Pillow 读 PNG
------------------------
只为拿「宽 / 高 / 有没有透明通道」三个事实就拖进一个图像库不划算。PNG 的 IHDR 是
**固定结构**的定长块，``tRNS`` 只是一个块类型 —— 扫一遍块表就够，**零第三方依赖**。

实测口径（:func:`probe_png` 认什么）
-----------------------------------
- 文件头必须是 PNG 签名 ``\\x89PNG\\r\\n\\x1a\\n``（扩展名叫 .png 不算数）；
- 第一个块必须是 ``IHDR``，宽高必须 > 0；
- **透明通道**：颜色类型 4（灰度+alpha）或 6（真彩+alpha）⇒ 有；
  颜色类型 0/2 配 ``tRNS``（透明色键）⇒ 也算有；调色板（3）配 ``tRNS`` ⇒ 有。
  调色板**没有** ``tRNS`` ⇒ 整张图不透明 ⇒ 判为不可用：那会是一块盖住画面的实心方块。

``probe_png`` **永不抛**：它同时服务面板（缺文件是**正常状态**，要显示成一行红字而不是
500）与渲染路径（缺文件 ⇒ 跳过这一层）。两条路径都只需要一份事实，不需要异常。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from studio.core.files import file_sha256

__all__ = [
    "PNG_SIGNATURE",
    "PngAsset",
    "probe_png",
]

#: PNG 文件头签名（8 字节，固定）
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: 扫块表的上限：``tRNS`` 必在 ``IDAT`` 之前，正常 PNG 前几个块就能看到。
#: 这个上限只防"块表被构造得极长"的病态文件，不是正常路径。
_MAX_CHUNKS: Final[int] = 64

#: 块数据长度上限（1 MiB）：超过这个数说明不是装饰图（正常贴图 < 1 MB），
#: 直接判不可用，避免对着一个几 GB 的"PNG"做 seek 循环。
_MAX_CHUNK_BYTES: Final[int] = 1 << 20


class _PngError(Exception):
    """内部信号：PNG 读不出来 / 不合规。**不对外抛** —— 由 :func:`probe_png` 转成
    "如实报告的问题"。
    """


@dataclass(frozen=True, slots=True)
class PngAsset:
    """盘上那个 PNG 的**实测事实**（不是配置里写的，是量出来的）。

    ``problem`` 为空且 ``exists`` 为真且 ``has_alpha`` 为真 ⇒ 可用。
    :func:`probe_png` 永远返回本对象（**不抛**）。
    """

    path: Path
    exists: bool
    width_px: int | None
    height_px: int | None
    has_alpha: bool
    sha256: str
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return (
            self.exists
            and self.problem is None
            and self.has_alpha
            and bool(self.width_px)
            and bool(self.height_px)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path.as_posix(),
            "exists": self.exists,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "has_alpha": self.has_alpha,
            "sha256": self.sha256,
            "problem": self.problem,
            "usable": self.usable,
        }


def _read_png_header(path: Path) -> tuple[int, int, bool]:
    """扫块表，返回 ``(宽, 高, 有没有透明通道)``；不合规 ⇒ ``_PngError``。"""
    with path.open("rb") as handle:
        if handle.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            raise _PngError("不是 PNG（文件头签名不匹配）")

        width = height = 0
        has_alpha = False
        seen_ihdr = False

        for _ in range(_MAX_CHUNKS):
            header = handle.read(8)
            if len(header) < 8:
                break
            length, chunk_type = struct.unpack(">I4s", header)
            if length > _MAX_CHUNK_BYTES:
                raise _PngError(f"PNG 块长度异常：{chunk_type!r} 声明 {length} 字节")

            if chunk_type == b"IHDR":
                if seen_ihdr:
                    raise _PngError("PNG 出现了两个 IHDR")
                data = handle.read(13)
                if len(data) < 13:
                    raise _PngError("PNG 被截断（IHDR 不足 13 字节）")
                width, height, _depth, color_type, _comp, _filt, _interlace = struct.unpack(">IIBBBBB", data)
                seen_ihdr = True
                # 颜色类型 4（灰度+alpha）/ 6（真彩+alpha）自带 alpha 通道；
                # 0/2/3 要看到 tRNS 才算有透明（调色板没 tRNS ⇒ 实心方块）。
                has_alpha = color_type in (4, 6)
                handle.seek(length - 13 + 4, 1)
                continue

            if chunk_type == b"IDAT":
                break
            if chunk_type == b"tRNS":
                has_alpha = True
            handle.seek(length + 4, 1)

    if not seen_ihdr:
        raise _PngError("PNG 缺 IHDR（文件损坏或不是 PNG）")
    if width <= 0 or height <= 0:
        raise _PngError(f"PNG 尺寸非法：{width}x{height}")
    return width, height, has_alpha


def probe_png(path: Path) -> PngAsset:
    """实测一张 PNG（**永不抛**）。面板与渲染路径共用它。"""
    if not path.is_file():
        return PngAsset(
            path=path,
            exists=False,
            width_px=None,
            height_px=None,
            has_alpha=False,
            sha256="",
            problem="文件不存在",
        )

    digest = file_sha256(path)
    try:
        width, height, has_alpha = _read_png_header(path)
    except _PngError as exc:
        return PngAsset(
            path=path,
            exists=True,
            width_px=None,
            height_px=None,
            has_alpha=False,
            sha256=digest,
            problem=str(exc),
        )
    except OSError as exc:  # 权限 / 被占用 / 读到一半被删
        return PngAsset(
            path=path,
            exists=True,
            width_px=None,
            height_px=None,
            has_alpha=False,
            sha256=digest,
            problem=f"读不出来：{exc}",
        )

    if not has_alpha:
        return PngAsset(
            path=path,
            exists=True,
            width_px=width,
            height_px=height,
            has_alpha=False,
            sha256=digest,
            problem="PNG 没有透明通道（会盖住一块实心画面）",
        )

    return PngAsset(
        path=path,
        exists=True,
        width_px=width,
        height_px=height,
        has_alpha=True,
        sha256=digest,
    )
