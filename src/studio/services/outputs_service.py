"""合成配置服务（T4.7 · §04.2.8 / §04.5.9）—— 看 / 改 合成 profile · 水印 · 字幕。

面板要回答三个问题，一个模型对一块
----------------------------------
① "现在这一档是什么参数？" ⇒ :class:`OutputsProfileCard`（分辨率 / 帧率 / 质量 / 平台）
② "水印与字幕长什么样、会不会出问题？" ⇒ :class:`OutputsWatermarkCard` / :class:`OutputsSubtitleCard`
③ "改完存哪、会不会把配置写坏？" ⇒ :meth:`OutputsService.update`（**校验不过一个字节都不写**）

为什么状态变更**不走数据库**
----------------------------
`config/outputs.yaml` 是唯一真相，消费方是渲染编译器（T3.x）与发布前校验（T5.1）——
它们读的都是**文件**。面板写 DB 会让"表说 A、文件说 B"，而真正出片的是文件。
（T4.13 的人物库、T4.2 的「一键全自动」是同一条取舍，见 §04.5.8 / 裁定 139。）

文件型状态没有"同一事务"可共享
------------------------------
"留痕与状态变更同事务"是 DB 侧的硬约定（§03.4.3）。这里的状态在文件里，能做的就是把
**顺序钉死**：先落盘 → 再留痕。落盘失败 ⇒ 没有留痕（正确：什么都没变）；留痕失败 ⇒
状态已经变了，这时必须记 ``error`` 级日志，**不能静默**。

为什么把"上一份值"写进留痕
--------------------------
这份配置**没有备份目录**（与人物库不同）。于是 ``audit_ops.before`` 就是唯一的
"改之前是多少"—— 面板把 ``width`` 从 1080 改成 64 之后，人唯一能查到的原始值就在这里。
所以指纹里放的是**被改字段的旧值**，不是整份配置的摘要。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, get_args

from studio.core.clock import now_iso
from studio.core.config import (
    EncodingProfileConfig,
    OutputsConfig,
    SafeAreaConfig,
    StickerConfig,
    SubtitleConfig,
    WatermarkConfig,
)
from studio.core.errors import ConfigError, ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.outputs_store import (
    PROFILE_FIELDS,
    STICKER_FIELDS,
    SUBTITLE_FIELDS,
    WATERMARK_FIELDS,
    OutputsSnapshot,
    OutputsStore,
    quality_field_of,
    scalar_for_write,
)
from studio.db.repositories import AuditRepo
from studio.render.png_probe import PngAsset, probe_png
from studio.render.sticker import aspect_warnings, speaking_problem
from studio.services.log_service import LogSink

__all__ = [
    "ACTION_UPDATE",
    "LOG_SOURCE",
    "TARGET_TYPE",
    "OutputsConsoleSnapshot",
    "OutputsOutcome",
    "OutputsProfileCard",
    "OutputsService",
    "OutputsStickerCard",
    "OutputsSubtitleCard",
    "OutputsWatermarkCard",
    "outputs_limits",
]

logger = get_logger("studio.services.outputs")

#: `audit_ops.action`（"谁在什么时候调了什么参数"必须查得到）
ACTION_UPDATE: Final[str] = "outputs.update"

#: `audit_ops.target_type` —— 被改的是一份**配置**，不是一个业务实体
TARGET_TYPE: Final[str] = "outputs"

#: `system_logs.source`（§04.5.2 的命名空间表里新增的一行）
LOG_SOURCE: Final[str] = "outputs"


# ══════════════════════════════════════════════════════════════════════
# 读 · 数据形状
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class OutputsProfileCard:
    """一档输出 profile（面板上一行）。"""

    name: str
    width: int
    height: int
    fps: int
    vcodec: str
    #: 质量参数落在哪个键（``crf`` = libx264 / ``cq`` = NVENC）—— 面板据此
    #: 显示"CRF"还是"CQ"，不必让用户去猜这一档用的是哪种编码器。
    quality_field: str
    quality: int
    platforms: tuple[str, ...]
    is_default: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "quality_field": self.quality_field,
            "quality": self.quality,
            "platforms": list(self.platforms),
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True)
class OutputsWatermarkCard:
    """固定水印（**可选装饰**）。``exists=False`` ⇒ 这次出片不贴水印，**照样出片**。

    面板仍然要显著提示 —— 但理由变了：以前是"不修好就出不了片"，现在是
    "这次出来的片子上没有水印，你要是想要就补一张"。两者的措辞必须分开写，
    把后者说成前者会让人以为链路坏了。

    ``exists``（盘上有这么个文件）与 ``usable``（渲染**真的会贴上**）是**两件事**，
    两个字段都下发：扩展名叫 ``.png`` 的 WebP / JPEG 在盘上"存在"，渲染却会跳过它
    （见 :func:`studio.render.png_probe.probe_png`）。只报前者的面板会让人对着一个
    绿点找半天"为什么片子上没有水印"。
    """

    path: str
    position: str
    margin_x: int
    margin_y: int
    width_ratio: float
    #: 按默认 profile 的画布宽算出来的**像素宽**：面板上"多宽"比"0.22"直观得多。
    width_px: int
    opacity: float
    #: 水印 PNG 在不在（相对路径按 STUDIO_HOME 解析）
    exists: bool
    #: 渲染**真的会贴上**吗（= 存在 + 是 PNG + 带透明通道 + 尺寸读得出来）
    usable: bool
    #: 用不了时的原因（原样来自 :func:`probe_png`；``usable=True`` ⇒ ``None``）
    problem: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "position": self.position,
            "margin_x": self.margin_x,
            "margin_y": self.margin_y,
            "width_ratio": self.width_ratio,
            "width_px": self.width_px,
            "opacity": self.opacity,
            "exists": self.exists,
            "usable": self.usable,
            "problem": self.problem,
        }


@dataclass(frozen=True, slots=True)
class OutputsStickerCard:
    """一层人物贴图（T6.5）。``usable=False`` ⇒ 这一层不贴，**其余层照常**。

    与水印那张卡片的差别在**粒度**：水印是"有没有"，贴图是"**哪几层**有"。
    所以这里是**每层一张**卡片，``skipped`` 由渲染路径算（面板只报"图在不在"），
    而 ``height_px`` 是按**默认档**的画布高算出来的像素高 —— 面板上"多高"比
    "0.45"直观得多（与水印卡片显示 ``width_px`` 同一条理由）。

    ★ **"图在不在"不够**：这一层的判据与渲染路径**必须是同一条**
    （:func:`studio.render.sticker.plan_stickers` 里的 ``probe_png(...).usable``）。
    曾经这里只做 ``is_file()``，于是"人物贴图开着、面板显示在盘上（绿点）、渲染却
    悄悄跳过"能同时成立 —— 用户看到的是"开了没反应"，而面板什么都不说。
    ``problem`` 就是那句"为什么"，原样来自 :func:`probe_png`。
    """

    name: str
    enabled: bool
    path: str
    #: 这一层代表稿子里的哪个说话人（空 ⇒ 不换图）
    speaker: str
    #: 讲话时换的那张图（``None`` ⇒ 这一层不换图）
    speaking_path: str | None
    position: str
    margin_x: int
    margin_y: int
    height_ratio: float
    #: 按默认 profile 的画布高算出来的**像素高**（宽高比决定实际宽度，面板不猜）
    height_px: int
    opacity: float
    #: 这一层的 PNG 在不在（相对路径按 STUDIO_HOME 解析）
    exists: bool
    #: 渲染**真的会贴上**吗（= 存在 + 是 PNG + 带透明通道 + 尺寸读得出来）
    usable: bool
    #: 用不了时的原因（原样来自 :func:`probe_png`；``usable=True`` ⇒ ``None``）
    problem: str | None
    #: **讲话图**这一路：图在不在 / 能不能用（判据与普通图完全一样）
    speaking_usable: bool
    #: "换图换不成"的原因（**面板这一侧判得出来的那些**）。
    #:
    #: 面板**判不了**"这个人这条片子里有没有讲话区间" —— 那要看稿子与时间轴，
    #: 只有渲染路径知道。所以这里不写那一句：在面板上编一个"应该有词吧"的结论，
    #: 就是面板与成片各说各话的老毛病（陷阱 223）。
    speaking_problem: str | None
    #: "换得了图、但会难看"的条目（目前只有一条：两张图宽高比不一致 ⇒ 人物被压扁）。
    #: 与 :attr:`speaking_problem` 分开：一个是"换不成"，一个是"换成了但画风不对"。
    speaking_warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "path": self.path,
            "speaker": self.speaker,
            "speaking_path": self.speaking_path,
            "position": self.position,
            "margin_x": self.margin_x,
            "margin_y": self.margin_y,
            "height_ratio": self.height_ratio,
            "height_px": self.height_px,
            "opacity": self.opacity,
            "exists": self.exists,
            "usable": self.usable,
            "problem": self.problem,
            "speaking_usable": self.speaking_usable,
            "speaking_problem": self.speaking_problem,
            "speaking_warnings": list(self.speaking_warnings),
        }


@dataclass(frozen=True, slots=True)
class OutputsSubtitleCard:
    """字幕样式（Q11 开启）。"""

    enabled: bool
    font_name: str
    font_size: int
    outline: int
    shadow: int
    margin_bottom: int
    safe_area_bottom: int
    #: 真正生效的距底像素（= max(margin_bottom, safe_area_bottom)）—— 面板据此
    #: 把"你填的数"与"实际渲的数"分开说，见 ``SubtitleConfig.margin_v``。
    margin_v: int
    max_chars_per_line: int
    max_lines: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "font_name": self.font_name,
            "font_size": self.font_size,
            "outline": self.outline,
            "shadow": self.shadow,
            "margin_bottom": self.margin_bottom,
            "safe_area_bottom": self.safe_area_bottom,
            "margin_v": self.margin_v,
            "max_chars_per_line": self.max_chars_per_line,
            "max_lines": self.max_lines,
        }


@dataclass(frozen=True, slots=True)
class OutputsConsoleSnapshot:
    """一次读全（面板首屏就这一个请求）。

    配置读不出来时 ``profiles`` 为空、``watermark`` / ``subtitle`` 为 ``None``，
    但 ``limits`` **照常给**（它来自模型，不是文件）—— 面板因此仍能画出表单骨架，
    用户看到的是"这一份配置坏了，去修它"，而不是一个打不开的页面。
    """

    generated_at: str
    version: int
    sha256: str
    loaded_at: str
    path: str
    stale: bool
    error: str | None
    default_profile: str
    profiles: tuple[OutputsProfileCard, ...]
    watermark: OutputsWatermarkCard | None
    #: 贴图**每一层**一张卡片（含关掉的 —— 面板要让人看见并打开它们）。
    #: 配置里没有 ``stickers`` 段 ⇒ 空元组（不是 None：空列表本身就是"没有层"）。
    stickers: tuple[OutputsStickerCard, ...]
    subtitle: OutputsSubtitleCard | None
    limits: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "version": self.version,
            "sha256": self.sha256,
            "loaded_at": self.loaded_at,
            "path": self.path,
            "stale": self.stale,
            "error": self.error,
            "default_profile": self.default_profile,
            "profiles": [item.to_dict() for item in self.profiles],
            "watermark": None if self.watermark is None else self.watermark.to_dict(),
            "stickers": [item.to_dict() for item in self.stickers],
            "subtitle": None if self.subtitle is None else self.subtitle.to_dict(),
            "limits": dict(self.limits),
        }


@dataclass(frozen=True, slots=True)
class OutputsOutcome:
    """一次写动作的结果（改没改、改了哪几项、现在是什么指纹）。"""

    action: str
    changed: bool
    version: int
    sha256: str
    path: str
    fields: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "changed": self.changed,
            "version": self.version,
            "sha256": self.sha256,
            "path": self.path,
            "fields": list(self.fields),
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════════
# 表单上下限（从模型现取，不抄第二份 · 裁定 161）
# ══════════════════════════════════════════════════════════════════════


def _bounds(field: Any) -> dict[str, Any]:
    """取一个字段的上下限（``ge``/``gt``/``le``/``lt`` 与 ``min_length``/``max_length`` 都认）。

    参数刻意标 ``Any`` 而不是 ``pydantic.fields.FieldInfo``：那是 Pydantic 的内部
    类型，mypy 在这条路径上把它当成变量而不是类。这里真正依赖的只是"有个
    ``.metadata`` 序列"，结构式用法反而更稳。

    ``gt`` / ``lt`` 是**排他**边界，必须单独标出来：``width_ratio`` 是 ``gt=0.0``，
    面板若把它画成"最小 0"就会放行一个必然 422 的 0 —— 与"上下限跟着模型走"
    这件事的初衷正好相反。
    """
    minimum: float | None = None
    maximum: float | None = None
    exclusive_min = False
    exclusive_max = False
    for meta in field.metadata:
        if hasattr(meta, "ge"):
            minimum = float(meta.ge)
        elif hasattr(meta, "gt"):
            minimum = float(meta.gt)
            exclusive_min = True
        elif hasattr(meta, "min_length"):
            minimum = float(meta.min_length)
        if hasattr(meta, "le"):
            maximum = float(meta.le)
        elif hasattr(meta, "lt"):
            maximum = float(meta.lt)
            exclusive_max = True
        elif hasattr(meta, "max_length"):
            maximum = float(meta.max_length)
    return {
        "min": minimum,
        "max": maximum,
        "exclusive_min": exclusive_min,
        "exclusive_max": exclusive_max,
    }


def outputs_limits() -> dict[str, Any]:
    """输入框的上下限 —— **从配置模型现取**。

    抄一份的后果很具体：某天把 ``crf`` 的上限从 51 调到 63，后端放行了、前端还在拦；
    或者反过来 —— 前端放行、后端 422。同一份约束在两处各写一遍，迟早有一处是旧的。

    ``positions`` 同理：水印位置是个 ``Literal``，枚举值从字段的注解里现取 ——
    前端把它画成一个下拉框，而不是在 TS 里再抄四个字符串。
    """
    profile = EncodingProfileConfig.model_fields
    watermark = WatermarkConfig.model_fields
    sticker = StickerConfig.model_fields
    subtitle = SubtitleConfig.model_fields
    safe_area = SafeAreaConfig.model_fields
    return {
        "scalar_fields": ["default_profile"],
        "profile_fields": list(PROFILE_FIELDS),
        "watermark_fields": list(WATERMARK_FIELDS),
        "sticker_fields": list(STICKER_FIELDS),
        "subtitle_fields": list(SUBTITLE_FIELDS),
        "default_profile": _bounds(OutputsConfig.model_fields["default_profile"]),
        "profile": {
            "width": _bounds(profile["width"]),
            "height": _bounds(profile["height"]),
            "fps": _bounds(profile["fps"]),
            "crf": _bounds(profile["crf"]),
            "cq": _bounds(profile["cq"]),
        },
        "watermark": {
            "positions": list(get_args(watermark["position"].annotation)),
            "margin_x": _bounds(watermark["margin_x"]),
            "margin_y": _bounds(watermark["margin_y"]),
            "width_ratio": _bounds(watermark["width_ratio"]),
            "opacity": _bounds(watermark["opacity"]),
        },
        "sticker": {
            # 位置枚举与水域**同一份**（都是 ``WatermarkPosition``）—— 从同一个注解现取，
            # 于是"水印能放哪几个位置"与"贴图能放哪几个位置"不可能漂开。
            "positions": list(get_args(sticker["position"].annotation)),
            "margin_x": _bounds(sticker["margin_x"]),
            "margin_y": _bounds(sticker["margin_y"]),
            "height_ratio": _bounds(sticker["height_ratio"]),
            "opacity": _bounds(sticker["opacity"]),
        },
        "subtitle": {
            "font_size": _bounds(subtitle["font_size"]),
            "outline": _bounds(subtitle["outline"]),
            "margin_bottom": _bounds(subtitle["margin_bottom"]),
            # 嵌套字段（``subtitle.safe_area.bottom``）的上下限同样**现取**：
            # 抄一份数字到前端，某天改了模型约束就会出现"前端拦着、后端放行"。
            "safe_area_bottom": _bounds(safe_area["bottom"]),
            "max_chars_per_line": _bounds(subtitle["max_chars_per_line"]),
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class OutputsService:
    """合成配置的读写入口（面板唯一的写正门）。

    :param store: 热重载仓库（由 ``AppState`` 持有）
    :param audit: `audit_ops` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    :param home: 解析水印相对路径用的根（``None`` ⇒ ``store`` 的同级推断）
    """

    def __init__(
        self,
        store: OutputsStore,
        *,
        audit: AuditRepo | None = None,
        log: LogSink | None = None,
        home: Path | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._log = log
        self._home = home

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self) -> OutputsConsoleSnapshot:
        """一次拿全：profile 列表 + 水印 + 字幕 + 表单上下限。"""
        snapshot = self._current_or_none()
        if snapshot is None:
            error = self._store.last_error
            return OutputsConsoleSnapshot(
                generated_at=now_iso(),
                version=0,
                sha256="",
                loaded_at="",
                path=str(self._store.path),
                stale=True,
                error=None if error is None else error.message,
                default_profile="",
                profiles=(),
                watermark=None,
                stickers=(),
                subtitle=None,
                limits=outputs_limits(),
            )
        config = snapshot.config
        return OutputsConsoleSnapshot(
            generated_at=now_iso(),
            version=snapshot.version,
            sha256=snapshot.sha256,
            loaded_at=snapshot.loaded_at,
            path=str(snapshot.path),
            stale=self._store.last_error is not None,
            error=None if self._store.last_error is None else self._store.last_error.message,
            default_profile=config.default_profile,
            profiles=self._profile_cards(config),
            watermark=self._watermark_card(config),
            stickers=self._sticker_cards(config),
            subtitle=self._subtitle_card(config),
            limits=outputs_limits(),
        )

    @staticmethod
    def _profile_cards(config: OutputsConfig) -> tuple[OutputsProfileCard, ...]:
        cards: list[OutputsProfileCard] = []
        for name, profile in config.profiles.items():
            key = quality_field_of(profile.vcodec)
            cards.append(
                OutputsProfileCard(
                    name=name,
                    width=profile.width,
                    height=profile.height,
                    fps=profile.fps,
                    vcodec=profile.vcodec,
                    quality_field=key,
                    quality=int(getattr(profile, key) or 0),
                    platforms=tuple(profile.platforms),
                    is_default=name == config.default_profile,
                )
            )
        return tuple(cards)

    def _watermark_card(self, config: OutputsConfig) -> OutputsWatermarkCard:
        spec = config.watermark
        canvas_width = config.profiles[config.default_profile].width
        asset = self._asset_probe(spec.path)
        return OutputsWatermarkCard(
            path=spec.path.as_posix(),
            position=spec.position,
            margin_x=spec.margin_x,
            margin_y=spec.margin_y,
            width_ratio=spec.width_ratio,
            width_px=spec.width_px_for(canvas_width),
            opacity=spec.opacity,
            exists=asset.exists,
            usable=asset.usable,
            problem=asset.problem,
        )

    def _sticker_cards(self, config: OutputsConfig) -> tuple[OutputsStickerCard, ...]:
        """贴图**每一层**一张卡片，顺序 = YAML 里的声明顺序（= 叠放顺序）。

        ``usable`` / ``problem`` 逐层各测一次（**不是**看整段有没有图）：一层是坏图
        不该连累另一层 —— 与渲染路径"每一层各自判断"同一条。
        """
        canvas_height = config.profiles[config.default_profile].height
        cards: list[OutputsStickerCard] = []
        for name, spec in config.stickers.items():
            # 实测**只做一次**：`probe_png` 会读文件头 + 算 sha256，三层各测三遍
            # 就是三倍磁盘 IO，而且三份结论还可能不一致（文件在两次之间被换掉）。
            asset = self._asset_probe(spec.path)
            speaking_asset = self._asset_probe(spec.speaking_path) if spec.speaking_path is not None else None
            cards.append(
                OutputsStickerCard(
                    name=name,
                    enabled=spec.enabled,
                    path=spec.path.as_posix(),
                    speaker=spec.speaker,
                    speaking_path=None if spec.speaking_path is None else spec.speaking_path.as_posix(),
                    position=spec.position,
                    margin_x=spec.margin_x,
                    margin_y=spec.margin_y,
                    height_ratio=spec.height_ratio,
                    height_px=spec.height_px_for(canvas_height),
                    opacity=spec.opacity,
                    exists=asset.exists,
                    usable=asset.usable,
                    problem=asset.problem,
                    speaking_usable=speaking_asset is not None and speaking_asset.usable,
                    speaking_problem=speaking_problem(
                        speaking_path=spec.speaking_path,
                        speaker=spec.speaker,
                        asset=speaking_asset,
                    ),
                    speaking_warnings=(
                        () if speaking_asset is None else aspect_warnings(asset, speaking_asset)
                    ),
                )
            )
        return tuple(cards)

    def _asset_probe(self, path: Path) -> PngAsset:
        """这一层的图**实测**成什么样（水印与贴图共用一份判据）。

        相对路径按 **STUDIO_HOME** 解析（`config/outputs.yaml` 的注释就是这么写的：
        "路径相对 STUDIO_HOME"）。文件不在 / 不是 PNG / 没有透明通道 ⇒ 渲染**跳过这一层
        继续出片**，并把原因写进 `RenderJob.result`（判断只在
        `render.watermark.plan_watermark` / `render.sticker.plan_stickers` 一处）。

        ★ 这里**必须**用 `probe_png` 而不是 `is_file()`：面板与渲染路径报的要是
        **同一件事**。"盘上有这么个文件"与"渲染真的会贴上"之间的那段距离（扩展名叫
        `.png` 的 WebP / JPEG / 没 alpha 的 PNG），恰好就是用户唯一会来面板上问的那句
        "我开了它，为什么片子上没有"。只做 `is_file()` 时那个绿点会**替渲染撒谎**。

        `home` 为 `None`（单测里不传）⇒ 判不了，如实说"判不了"而不是"没有"。
        """
        if self._home is None:
            return PngAsset(
                path=path,
                exists=False,
                width_px=None,
                height_px=None,
                has_alpha=False,
                sha256="",
                problem="没有可用的 STUDIO_HOME，这张图判不了",
            )
        candidate = path if path.is_absolute() else self._home / path
        return probe_png(candidate)

    @staticmethod
    def _subtitle_card(config: OutputsConfig) -> OutputsSubtitleCard:
        spec = config.subtitle
        return OutputsSubtitleCard(
            enabled=spec.enabled,
            font_name=spec.font_name,
            font_size=spec.font_size,
            outline=spec.outline,
            shadow=spec.shadow,
            margin_bottom=spec.margin_bottom,
            safe_area_bottom=spec.safe_area.bottom,
            margin_v=spec.margin_v,
            max_chars_per_line=spec.max_chars_per_line,
            max_lines=spec.max_lines,
        )

    # ── 写 ──────────────────────────────────────────────────────────────

    def update(
        self,
        *,
        changes: Mapping[str, Any],
        expected_sha256: str | None = None,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> OutputsOutcome:
        """保存表单改动（**校验不过一个字节都不写**）。

        "没有改动"如实回给面板（``changed=False``）：点了一下保存、值却与盘上一模一样
        时，不该假装做了一次操作 —— 那会往审计表里刷一串什么都没改的记录。
        """
        before = self._current_or_none()
        with _translated():
            snapshot = self._store.update(changes, expected_sha256=expected_sha256)
        fields = _describe(changes)
        changed = before is None or before.sha256 != snapshot.sha256
        outcome = OutputsOutcome(
            action=ACTION_UPDATE,
            changed=changed,
            version=snapshot.version,
            sha256=snapshot.sha256,
            path=str(snapshot.path),
            fields=fields,
            note=(
                f"已保存合成配置（{len(fields)} 项：{'、'.join(fields)}）；"
                "**只影响后续渲染** —— 已入队/在跑的任务不中断、不重跑"
                if changed
                else "值与盘上那份完全一致，没有做任何改动"
            ),
        )
        if changed:
            self._record(
                outcome=outcome,
                before=_fingerprint(before, changes),
                after=_fingerprint(snapshot, changes),
                reason=reason or "WebUI 保存合成配置",
                actor=actor,
                source=source,
            )
        return outcome

    # ── 内部 ────────────────────────────────────────────────────────────

    def _current_or_none(self) -> OutputsSnapshot | None:
        """取当前快照；**首次加载就失败**（连上一份好的都没有）⇒ ``None``。"""
        try:
            return self._store.current()
        except StudioError as exc:
            logger.warning("outputs.current_unreadable", error=exc.message, code=str(exc.code))
            return None

    def _record(
        self,
        *,
        outcome: OutputsOutcome,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        reason: str,
        actor: str,
        source: str,
    ) -> None:
        """先落盘（调用方已做完）→ 再留痕 → 再写日志。两处失败都不许静默。"""
        if self._audit is not None:
            try:
                self._audit.record(
                    actor=actor,
                    action=outcome.action,
                    target_type=TARGET_TYPE,
                    target_id=str(outcome.path),
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            except Exception:  # 状态已经变了：这里只能记账，不能回滚文件
                logger.exception("outputs.audit_failed", path=outcome.path)
        if self._log is not None:
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"{outcome.note}（{reason}）",
                payload={
                    "action": outcome.action,
                    "path": outcome.path,
                    "fields": list(outcome.fields),
                    "version": outcome.version,
                    "sha256": outcome.sha256,
                    "changed": outcome.changed,
                },
            )
        logger.info(
            "outputs.changed",
            action=outcome.action,
            fields=list(outcome.fields),
            version=outcome.version,
        )


def _describe(changes: Mapping[str, Any]) -> tuple[str, ...]:
    """改动载荷 ⇒ 人看得懂的字段清单（``watermark.margin_x`` 这种点分名）。"""
    fields: list[str] = []
    if "default_profile" in changes:
        fields.append("default_profile")
    for section in ("profiles", "stickers"):
        for name, patch in (changes.get(section) or {}).items():
            fields.extend(f"{section}.{name}.{field}" for field in patch)
    for section in ("watermark", "subtitle"):
        fields.extend(f"{section}.{field}" for field in (changes.get(section) or {}))
    return tuple(fields)


def _fingerprint(snapshot: OutputsSnapshot | None, changes: Mapping[str, Any]) -> dict[str, Any]:
    """留痕里的 ``before`` / ``after``：**被改字段的值** + 一份整体指纹。

    这份配置没有备份目录（与人物库不同），所以 ``before`` 就是唯一的"改之前是多少"。
    放字段旧值而不是整份配置：审计表不是媒资库（§04.5.8 同一条口径）。
    """
    values: dict[str, Any] = {}
    if snapshot is not None:
        for field in _describe(changes):
            values[field] = _lookup(snapshot.config, field)
    return {
        "sha256": None if snapshot is None else snapshot.sha256,
        "version": None if snapshot is None else snapshot.version,
        "values": values,
    }


def _lookup(config: OutputsConfig, field: str) -> Any:
    """按点分名从配置里取值（``profiles.<name>.quality`` 会落到 crf / cq）。"""
    parts = field.split(".")
    head = parts[0]
    if head == "default_profile":
        return config.default_profile
    if head == "profiles" and len(parts) == 3:
        profile = config.profiles.get(parts[1])
        return None if profile is None else _profile_value(profile, parts[2])
    if head == "stickers" and len(parts) == 3:
        sticker = config.stickers.get(parts[1])
        return None if sticker is None else _literal(getattr(sticker, parts[2], None))
    if head in ("watermark", "subtitle") and len(parts) == 2:
        return getattr(getattr(config, head), parts[1], None)
    return None


def _literal(value: Any) -> Any:
    """留痕里记的**必须是字面量**（能进 JSON），不是模型对象。

    贴图的 ``path`` 在模型上是 ``Path`` —— 直接塞进 ``before``/``after`` 会让审计写入
    在 JSON 序列化那一步炸掉，而那时配置**已经落盘**了（"留痕失败 ⇒ 状态已经变了，
    必须记 error 级日志"）。所以在这里就换算成与写进 YAML 完全一样的那个字面量
    （正斜杠字符串），顺带让"审计里的值"与"文件里的值"逐字一致，方便比对。
    """
    return scalar_for_write(value)


def _profile_value(profile: EncodingProfileConfig, name: str) -> Any:
    """档内取值：``quality`` 落到这一档**真正的**键（libx264 的 crf / NVENC 的 cq）。"""
    if name == "quality":
        return getattr(profile, quality_field_of(profile.vcodec))
    return getattr(profile, name, None)


#: store 用的"通用配置码" ⇒ REST 面能用的"合成配置码"（与 §04.5.8 裁定 163 同一手法）。
#:
#: 为什么不直接把 store 的码改成 `OUTPUTS_*`：`CONFIG_MISSING` / `CONFIG_INVALID` 是
#: T1.2 就钉住的契约（单测逐条断言），而"配置不在"对面板来说是 404、对 CLI 来说是
#: "去 `studio doctor` 看看" —— 同一件事两种读法，翻译放在**面向调用方的那一层**
#: （本服务）比改 store 的既有语义便宜。
_NOT_FOUND_CODES: Final[frozenset[ErrorCode]] = frozenset({ErrorCode.CONFIG_MISSING})
_INVALID_CODES: Final[frozenset[ErrorCode]] = frozenset({ErrorCode.CONFIG_INVALID})


@contextmanager
def _translated() -> Iterator[None]:
    """把 store 抛出的通用配置错误翻成合成配置码（**不吞异常**：翻不了的原样上抛）。"""
    try:
        yield
    except ConfigError as exc:
        if exc.code in _NOT_FOUND_CODES:
            raise _rewrap(exc, ErrorCode.OUTPUTS_NOT_FOUND) from exc
        if exc.code in _INVALID_CODES:
            raise _rewrap(exc, ErrorCode.OUTPUTS_INVALID) from exc
        raise


def _rewrap(exc: ConfigError, code: ErrorCode) -> ConfigError:
    return ConfigError(
        exc.message,
        code=code,
        context=dict(exc.context),
        remediation=exc.remediation,
    )
