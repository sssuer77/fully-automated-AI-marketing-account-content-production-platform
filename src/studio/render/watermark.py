"""固定水印：参数模型 + PNG 实测 + 编译期摆放（T3.2 · §04.2.8.2）。

口径：**有就贴，没有就跳过**
----------------------------
水印是**可选**输入。盘上有可用的 PNG ⇒ 贴上去；没有 / 读不出来 / 不带透明通道
⇒ **跳过水印继续出片**，并把原因如实写进计划（``skipped_reason``）。

这条口径是**刻意**的：早先的版本把水印做成"缺了就拒绝渲染"的硬门禁（D5），
后果是 ``templates/`` 下没有 ``watermark.png`` 时**一支片子都出不来** ——
门禁把"锦上添花的装饰"变成了"整条链路的单点阻塞"。成片优先，水印其次。

本模块做三件事，且**只做这三件事**：

1. **参数模型** ``WatermarkSpec``：编译器直接引用它，不另起一份"计划里的水印参数"；
2. **资产实测** ``probe_watermark``：**永不抛**，如实报告"在不在 / 多大 / 有没有 alpha"；
3. **摆放** ``place_watermark``：编译期算出 x/y；放不下 ⇒ 返回 ``None``（跳过，不炸）。

:func:`plan_watermark` 把这三步串成一个结论（:class:`WatermarkPlan`）：渲染路径与
``studio render profile --show`` 共用它 —— 一处判断，两处消费。

为什么 x/y 算成字面量，而不是 ``overlay=x=W-w-40`` 表达式
--------------------------------------------------------
字面量走常量路径，且测试能直接断言"x 是偶数"（§04.2.8.4 的 yuv420p 色度对齐要求）；
表达式求值要求 ``overlay`` 带 ``eval=init`` 并走"每帧算一次"的慢路径。

代价如实说明：``CompositePlan`` 里存的是**这一块画布下的具体像素**。换画布
（1080×1920 ⇒ 720×1280 的保底档）必须**重算**，不能拿旧计划改个数字继续用。
:func:`resolve_watermark` 就是那个重算入口。

为什么不引 Pillow 读 PNG
------------------------
只为拿「宽 / 高 / 有没有透明通道」三个事实就拖进一个图像库不划算。PNG 的 IHDR 是
**固定结构**的定长块，``tRNS`` 只是一个块类型 —— 扫一遍块表就够，**零第三方依赖**。

实测口径（``probe_watermark`` 认什么）
------------------------------------
- 文件头必须是 PNG 签名 ``\\x89PNG\\r\\n\\x1a\\n``（扩展名叫 .png 不算数）；
- 第一个块必须是 ``IHDR``，宽高必须 > 0；
- **透明通道**：颜色类型 4（灰度+alpha）或 6（真彩+alpha）⇒ 有；
  颜色类型 0/2 配 ``tRNS``（透明色键）⇒ 也算有；调色板（3）配 ``tRNS`` ⇒ 有。
  调色板**没有** ``tRNS`` ⇒ 整张图不透明 ⇒ 判为不可用：那会是一块盖住画面的实心方块。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from studio.core.config import (
    CANVAS_WIDTH_DIVISOR,
    WatermarkConfig,
    WatermarkPosition,
)
from studio.core.errors import ErrorCode, RenderError
from studio.core.files import file_sha256

__all__ = [
    "CANVAS_WIDTH_DIVISOR",
    "PNG_SIGNATURE",
    "ResolvedWatermark",
    "WatermarkAsset",
    "WatermarkPlacement",
    "WatermarkPlan",
    "WatermarkSpec",
    "place_watermark",
    "plan_watermark",
    "probe_watermark",
    "resolve_watermark",
]

#: PNG 文件头签名（8 字节，固定）
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: 水印宽度上限 = 画布宽 / 这个数（§04.2.8.2：禁止超过画布 1/4）。
#: 定义在 `core/config.py`（面板与编译器共用同一个口径），这里只是转出。
#: 扫块表的上限：``tRNS`` 必在 ``IDAT`` 之前，正常 PNG 前几个块就能看到。
#: 这个上限只防"块表被构造得极长"的病态文件，不是正常路径。
_MAX_CHUNKS: Final[int] = 64

#: 块数据长度上限（1 MiB）：超过这个数说明不是水印图（正常水印 < 1 MB），
#: 直接判不可用，避免对着一个几 GB 的"PNG"做 seek 循环。
_MAX_CHUNK_BYTES: Final[int] = 1 << 20


class _PngError(Exception):
    """内部信号：PNG 读不出来 / 不合规。**不对外抛** —— 由 :func:`probe_watermark`
    转成"如实报告的问题"。

    为什么不直接抛 ``RenderError``：``probe`` 的契约是"永远给出一份事实"，
    它同时服务面板（缺文件是**正常状态**，要显示成一行红字而不是 500）与渲染路径
    （缺文件 ⇒ 跳过水印）。
    """


class WatermarkSpec(BaseModel):
    """★ 固定水印。"固定" = 位置与尺寸恒定，不随机、不随时间变化。

    字段与默认值照抄 §04.2.8.2。两处**有意的收紧**，理由写在各自的校验器里：
    ``margin_px`` 必须为偶数、``width_px`` 必须为偶数（yuv420p 色度对齐）。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    image_path: Path
    position: WatermarkPosition = "bottom_right"
    margin_px: tuple[int, int] = (40, 40)
    width_px: int = Field(default=220, ge=2)
    opacity: float = Field(default=0.85, ge=0.0, le=1.0)
    apply_to: Literal["full", "tail"] = "full"

    @field_validator("margin_px")
    @classmethod
    def _margins_must_be_even(cls, value: tuple[int, int]) -> tuple[int, int]:
        x, y = value
        if x < 0 or y < 0:
            raise ValueError(f"水印边距不得为负：{value}")
        if x % 2 or y % 2:
            raise ValueError(f"水印边距必须为偶数（overlay 色度对齐）：{value}")
        return value

    @field_validator("width_px")
    @classmethod
    def _width_must_be_even(cls, value: int) -> int:
        if value % 2:
            raise ValueError(f"水印宽度必须为偶数（overlay 色度对齐）：{value}")
        return value

    def to_dict(self) -> dict[str, object]:
        """写进 ``manifest.json`` 的形状（路径统一正斜杠，跨机可读）。"""
        return {
            "enabled": self.enabled,
            "image_path": self.image_path.as_posix(),
            "position": self.position,
            "margin_px": list(self.margin_px),
            "width_px": self.width_px,
            "opacity": self.opacity,
            "apply_to": self.apply_to,
        }


@dataclass(frozen=True, slots=True)
class WatermarkAsset:
    """盘上那个 PNG 的**实测事实**（不是配置里写的，是量出来的）。

    ``problem`` 为空且 ``exists`` 为真且 ``has_alpha`` 为真 ⇒ 可用。
    ``probe_watermark`` 永远返回本对象（**不抛**）：面板要显示"缺文件"这一行红字，
    而"缺文件"不是异常，是一个状态。
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


@dataclass(frozen=True, slots=True)
class ResolvedWatermark:
    """``config/outputs.yaml`` 的水印段 + **一块具体画布** ⇒ 计划里的水印参数。

    ``warnings`` 是"照做但如实记账"的条目（目前只有一条：宽度被夹取到画布 1/4）。
    """

    spec: WatermarkSpec
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"spec": self.spec.to_dict(), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class WatermarkPlacement:
    """编译期算好的摆放位置（**像素字面量**，见模块 docstring）。"""

    x: int
    y: int
    width_px: int
    height_px: int

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width_px": self.width_px, "height_px": self.height_px}


@dataclass(frozen=True, slots=True)
class WatermarkPlan:
    """水印这一环的**结论**：贴还是不贴，不贴是因为什么。

    ``placement is None`` ⇒ 不贴（跳过），原因在 ``skipped_reason``。
    渲染编译器只看 ``placement``：有就加一层 ``overlay``，没有就直接跳过那一层 ——
    **没有第三条分支**（不存在"报错退出"）。
    """

    spec: WatermarkSpec
    asset: WatermarkAsset
    placement: WatermarkPlacement | None = None
    skipped_reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.placement is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_dict(),
            "asset": self.asset.to_dict(),
            "placement": self.placement.to_dict() if self.placement else None,
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "warnings": list(self.warnings),
        }


# ══════════════════════════════════════════════════════════════════════
# 配置 ⇒ 计划参数
# ══════════════════════════════════════════════════════════════════════


def resolve_watermark(
    config: WatermarkConfig,
    *,
    canvas_width: int,
    home: Path,
) -> ResolvedWatermark:
    """把配置里的**比例**换算成这块画布下的**像素**（宽度的口径只在 ``WatermarkConfig`` 里）。

    相对路径按 ``STUDIO_HOME`` 解析成绝对路径 —— 渲染 worker 的工作目录不保证是仓库根，
    而 ``WatermarkSpec`` 会原样写进 ``manifest.json`` 并被拿去算缓存哈希，
    存相对路径会让"同一份配置"在两个 CWD 下算出两个哈希。

    这里**不碰盘**：水印在不在是 :func:`probe_watermark` 的事。分开的理由是
    面板/CLI 想显示"参数是什么 + 文件在不在"两件事，而它们各自会失败。
    """
    if canvas_width < 8:
        raise RenderError(
            f"画布宽太小，放不下水印：{canvas_width}px",
            code=ErrorCode.RENDER_WATERMARK_INVALID,
            context={"canvas_width": canvas_width},
            remediation="检查 config/outputs.yaml 里这一档 profile 的 width",
        )

    width_px = config.width_px_for(canvas_width)
    raw_px = config.width_px_raw(canvas_width)
    warnings: tuple[str, ...] = ()
    if width_px != raw_px:
        # 两种原因都会走到这里：超过画布 1/4（夹取）、以及奇数画布宽下的取偶。
        # 不分开写两条：人看到的都是「比例算出来的宽度被改了」，修法也都是改 width_ratio。
        warnings = (
            f"水印宽度按画布修正：{raw_px}px ⇒ {width_px}px"
            f"（画布宽 {canvas_width}px；上限 1/{CANVAS_WIDTH_DIVISOR}，并取偶）",
        )

    path = config.path if config.path.is_absolute() else home / config.path
    spec = WatermarkSpec(
        image_path=path,
        position=config.position,
        margin_px=(config.margin_x, config.margin_y),
        width_px=width_px,
        opacity=config.opacity,
    )
    return ResolvedWatermark(spec=spec, warnings=warnings)


# ══════════════════════════════════════════════════════════════════════
# 资产实测
# ══════════════════════════════════════════════════════════════════════


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


def probe_watermark(path: Path) -> WatermarkAsset:
    """实测水印 PNG（**永不抛**）。面板与 CLI 用它显示"水印现在什么状态"。"""
    if not path.is_file():
        return WatermarkAsset(
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
        return WatermarkAsset(
            path=path,
            exists=True,
            width_px=None,
            height_px=None,
            has_alpha=False,
            sha256=digest,
            problem=str(exc),
        )
    except OSError as exc:  # 权限 / 被占用 / 读到一半被删
        return WatermarkAsset(
            path=path,
            exists=True,
            width_px=None,
            height_px=None,
            has_alpha=False,
            sha256=digest,
            problem=f"读不出来：{exc}",
        )

    if not has_alpha:
        return WatermarkAsset(
            path=path,
            exists=True,
            width_px=width,
            height_px=height,
            has_alpha=False,
            sha256=digest,
            problem="PNG 没有透明通道（会盖住一块实心画面）",
        )

    return WatermarkAsset(
        path=path,
        exists=True,
        width_px=width,
        height_px=height,
        has_alpha=True,
        sha256=digest,
    )


# ══════════════════════════════════════════════════════════════════════
# 编译期摆放
# ══════════════════════════════════════════════════════════════════════


def _even_floor(value: int) -> int:
    """向下取到偶数（yuv420p 色度对齐；``-2`` 的语义就是"取偶"）。"""
    return value - value % 2


def place_watermark(
    spec: WatermarkSpec,
    asset: WatermarkAsset,
    *,
    canvas_width: int,
    canvas_height: int,
) -> WatermarkPlacement | None:
    """算出水印在画布上的 ``x/y`` 与缩放后的 ``宽/高``；放不下 ⇒ ``None``（跳过）。

    高度按素材原始宽高比推出来（``scale=W:-2`` 的语义：等比缩放 + 高度取偶），
    这样越界判断用的是**真实占位**，不是"假设水印是正方形"的估算 —— 一个 4:1 的
    长条水印在 ``bottom_right`` 下是往左占位，估算成正方形就会漏判。

    ``center`` 位置忽略边距（规格里它和四个角并列，边距对居中无意义）。

    放不下时**返回 None 而不是抛错**：水印是装饰，配错了不该让整支片子出不来
    （调用方 :func:`plan_watermark` 会把原因记进 ``skipped_reason``）。
    """
    if asset.width_px is None or asset.height_px is None or asset.width_px <= 0:
        return None

    width = spec.width_px
    height = _even_floor(round(width * asset.height_px / asset.width_px))
    height = max(height, 2)
    margin_x, margin_y = spec.margin_px

    if spec.position == "center":
        x = _even_floor((canvas_width - width) // 2)
        y = _even_floor((canvas_height - height) // 2)
    else:
        x = margin_x if spec.position.endswith("left") else canvas_width - width - margin_x
        y = margin_y if spec.position.startswith("top") else canvas_height - height - margin_y

    if x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
        return None

    return WatermarkPlacement(x=x, y=y, width_px=width, height_px=height)


# ══════════════════════════════════════════════════════════════════════
# 结论：贴还是不贴
# ══════════════════════════════════════════════════════════════════════


def plan_watermark(
    config: WatermarkConfig,
    *,
    canvas_width: int,
    canvas_height: int,
    home: Path,
) -> WatermarkPlan:
    """水印这一环的**唯一**判断点：能用 ⇒ 给出摆放；不能用 ⇒ 跳过 + 记原因。

    渲染路径与 ``studio render profile --show`` 都调它，所以"CLI 说会贴"与
    "渲染真的贴了"不可能对不上。
    """
    resolved = resolve_watermark(config, canvas_width=canvas_width, home=home)
    spec = resolved.spec
    asset = probe_watermark(spec.image_path)

    if not spec.enabled:
        return WatermarkPlan(
            spec=spec,
            asset=asset,
            skipped_reason="配置里关掉了水印（WatermarkSpec.enabled=False）",
            warnings=resolved.warnings,
        )
    if not asset.exists:
        return WatermarkPlan(
            spec=spec,
            asset=asset,
            skipped_reason=f"水印文件不存在，跳过：{spec.image_path}",
            warnings=resolved.warnings,
        )
    if not asset.usable:
        return WatermarkPlan(
            spec=spec,
            asset=asset,
            skipped_reason=f"水印不可用（{asset.problem}），跳过",
            warnings=resolved.warnings,
        )

    placement = place_watermark(spec, asset, canvas_width=canvas_width, canvas_height=canvas_height)
    if placement is None:
        return WatermarkPlan(
            spec=spec,
            asset=asset,
            skipped_reason=(
                f"水印放不进画布：位置 {spec.position}、边距 {spec.margin_px}、"
                f"宽度 {spec.width_px}px、画布 {canvas_width}x{canvas_height}"
            ),
            warnings=resolved.warnings,
        )

    return WatermarkPlan(spec=spec, asset=asset, placement=placement, warnings=resolved.warnings)
