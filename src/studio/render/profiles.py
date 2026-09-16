"""合成 profile：`config/outputs.yaml` ⇒ ffmpeg 输出参数（T3.2 · §01.5.5 / §04.2.8.3）。

这一层解决什么问题
------------------
`config/outputs.yaml` 里的 profile 是**给人看的**：宽 / 高 / 帧率 / 质量，四个旋钮。
ffmpeg 要的是 `-c:v libx264 -crf 21 -profile:v high -level 4.1 -keyint_min 60
-sc_threshold 0 -colorspace bt709 …` 这一串。本模块是**唯一**的翻译点：
T3.3 的编译器、T3.4 的执行器、`studio render profile --show` 都从这里取参数。
抄第二份的后果很具体 —— 面板把 CRF 从 21 调到 19，编译出来的命令还是 21。

哪些参数**不给用户调**
----------------------
`-profile:v high -level 4.1 -keyint_min -sc_threshold` 这几个不在 `outputs.yaml` 里，
由本模块按编码器家族补上（§01.5.5「成片 final」一行）。理由：它们不是"画质旋钮"，
是**平台兼容性军规** —— 放开只会让人把 level 调低、把 `sc_threshold` 调回默认，
然后成片在某些平台上放不出来或体积暴涨。真正该由人调的是宽高 / 帧率 / 质量，
那三个已经在面板上了（T4.7）。

720P 保底档
-----------
`fallback_720x1280_v1` 是**降级链**的第三级（§04.2.8.6：单遍失败 ⇒ 分块 ⇒ 720P 保底）。
它不在"按平台选 profile"的路径上（`platforms: []`），只能由降级逻辑显式点名 ——
`is_fallback` 就是给那个判断用的，免得降级代码里硬编码一个字符串名字。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.config import EncodingProfileConfig, OutputsConfig
from studio.core.errors import ConfigError, ErrorCode
from studio.render.watermark import WatermarkPlan, plan_watermark

__all__ = [
    "FALLBACK_PROFILE_NAME",
    "H264_LEVEL",
    "H264_PROFILE",
    "NVENC_PRESET_DEFAULT",
    "X264_PRESET_DEFAULT",
    "CompositeProfile",
    "RenderProfileReport",
    "build_render_profile_report",
    "resolve_profile",
]

#: 降级保底档的名字（§04.2.8.6）。降级逻辑点名它，不要散落字符串字面量。
FALLBACK_PROFILE_NAME: Final[str] = "fallback_720x1280_v1"

#: §01.5.5「成片 final」：High profile + level 4.1（1080×1920/30fps 的通用上限）
H264_PROFILE: Final[str] = "high"
H264_LEVEL: Final[str] = "4.1"

#: 缺省 preset（配置文件没写时的兜底；§01.5.5 成片用 medium，NVENC 用 p4）
X264_PRESET_DEFAULT: Final[str] = "medium"
NVENC_PRESET_DEFAULT: Final[str] = "p4"


def _quality_of(profile: EncodingProfileConfig) -> tuple[str, int]:
    """质量参数写到哪个键、值是多少：libx264 用 ``crf``，NVENC 用 ``cq``。

    与 `core/outputs_store.quality_field_of` **同一条规则**，但那边是给面板显示用的
    （"这个框调的是 CRF 还是 CQ"），这边是给 ffmpeg 用的。两处都读同一个模型字段，
    不存在"面板说 CQ、命令写 crf"的错法：字段本身只有一个是非空的（见
    `EncodingProfileConfig._encoder_params_and_alignment`）。
    """
    if profile.vcodec == "libx264":
        return "crf", int(profile.crf or 0)
    return "cq", int(profile.cq or 0)


@dataclass(frozen=True, slots=True)
class CompositeProfile:
    """一档**已解析**的合成 profile：画布 + 编码器 argv 的全部输入。"""

    name: str
    width: int
    height: int
    fps: int
    vcodec: str
    quality_field: str
    quality: int
    preset: str
    pix_fmt: str
    colorspace: str
    gop: int
    faststart: bool
    acodec: str
    audio_bitrate: str
    audio_sample_rate: int
    audio_channels: int
    platforms: tuple[str, ...]

    @property
    def canvas(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def is_fallback(self) -> bool:
        """是不是 720P 保底档（降级链用）。"""
        return self.name == FALLBACK_PROFILE_NAME

    def video_args(self) -> list[str]:
        """视频编码参数（§04.2.8.3「输出参数」+ §01.5.5 成片行）。

        参数顺序照 §04.2.8.3 写，便于与规格逐字对照 —— 顺序对 ffmpeg 语义无影响，
        但"能一眼看出和规格一致"本身有价值（这条链将来要进 golden 比对）。
        """
        args = ["-c:v", self.vcodec]
        if self.vcodec == "libx264":
            args += [
                "-crf",
                str(self.quality),
                "-preset",
                self.preset,
                "-pix_fmt",
                self.pix_fmt,
                "-profile:v",
                H264_PROFILE,
                "-level",
                H264_LEVEL,
            ]
        else:
            # NVENC：cq 只在 vbr 速率控制下有意义，且必须显式 `-b:v 0`，
            # 否则 cq 会被"目标码率"覆盖（§01.5.5 的 scene 行就是这么写的）。
            args += [
                "-preset",
                self.preset,
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                str(self.quality),
                "-b:v",
                "0",
                "-pix_fmt",
                self.pix_fmt,
            ]

        args += ["-r", str(self.fps), "-g", str(self.gop)]
        if self.vcodec == "libx264":
            # 关键帧间隔 = gop，且禁用场景切换插帧：CFR + 固定 GOP 是平台转码
            # 与"逐帧拖动"体验的前提（§01.5.5）。
            args += ["-keyint_min", str(self.gop), "-sc_threshold", "0"]
        args += [
            "-colorspace",
            self.colorspace,
            "-color_primaries",
            self.colorspace,
            "-color_trc",
            self.colorspace,
        ]
        return args

    def audio_args(self) -> list[str]:
        """音频编码参数（§01.5.5「音频」行）。"""
        return [
            "-c:a",
            self.acodec,
            "-b:a",
            self.audio_bitrate,
            "-ar",
            str(self.audio_sample_rate),
            "-ac",
            str(self.audio_channels),
        ]

    def container_args(self) -> list[str]:
        """封装参数。``+faststart`` 让 moov 前置 ⇒ 平台边下边播（§01.8.2）。"""
        return ["-movflags", "+faststart"] if self.faststart else []

    def output_args(self) -> list[str]:
        """**不含** ``-map`` / ``-t`` 的那部分输出参数（那两个由 T3.3 按计划给）。"""
        return self.video_args() + self.audio_args() + self.container_args()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "quality_field": self.quality_field,
            "quality": self.quality,
            "preset": self.preset,
            "pix_fmt": self.pix_fmt,
            "colorspace": self.colorspace,
            "gop": self.gop,
            "faststart": self.faststart,
            "acodec": self.acodec,
            "audio_bitrate": self.audio_bitrate,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
            "platforms": list(self.platforms),
            "is_fallback": self.is_fallback,
            "output_args": self.output_args(),
        }


def _to_composite(name: str, profile: EncodingProfileConfig) -> CompositeProfile:
    quality_field, quality = _quality_of(profile)
    if profile.vcodec == "libx264":
        preset = profile.preset or X264_PRESET_DEFAULT
    else:
        preset = profile.nvenc_preset or NVENC_PRESET_DEFAULT
    return CompositeProfile(
        name=name,
        width=profile.width,
        height=profile.height,
        fps=profile.fps,
        vcodec=profile.vcodec,
        quality_field=quality_field,
        quality=quality,
        preset=preset,
        pix_fmt=profile.pix_fmt,
        colorspace=profile.colorspace,
        gop=profile.gop,
        faststart=profile.faststart,
        acodec=profile.acodec,
        audio_bitrate=profile.audio_bitrate,
        audio_sample_rate=profile.audio_sample_rate,
        audio_channels=profile.audio_channels,
        platforms=tuple(profile.platforms),
    )


def resolve_profile(outputs: OutputsConfig, name: str | None = None) -> CompositeProfile:
    """取一档 profile（``name=None`` ⇒ ``default_profile``）；名字不存在 ⇒ 报错。

    **不静默回落**到默认档：降级链点名 `fallback_720x1280_v1` 时若这个名字被删了，
    悄悄换回 1080P 等于"降级了但没降"，最后仍会以同样的理由失败一次。宁可当场说
    "这个名字不在配置里"。
    """
    chosen = name or outputs.default_profile
    profile = outputs.profiles.get(chosen)
    if profile is None:
        raise ConfigError(
            f"outputs.yaml 里没有这一档 profile：{chosen}",
            code=ErrorCode.OUTPUTS_NOT_FOUND,
            context={"profile": chosen, "known": sorted(outputs.profiles)},
            remediation=f"改 config/outputs.yaml（现有档：{'、'.join(sorted(outputs.profiles))}）",
        )
    return _to_composite(chosen, profile)


@dataclass(frozen=True, slots=True)
class RenderProfileReport:
    """`studio render profile --show` 的全部内容。

    这里**没有**"能不能出片"的门禁字段：一期没有任何一项输入能挡住出片
    （水印是可选装饰 —— 有就贴、没有就跳过；素材为空走降级，§04.2.8.6）。
    原先的 ``ready`` / ``blockers`` 把"水印缺失"报成阻塞项，而它并不阻塞任何东西，
    只会让人误以为链路坏了。
    """

    source: Path
    default_profile: str
    profile: CompositeProfile
    profiles: tuple[CompositeProfile, ...]
    watermark: WatermarkPlan

    @property
    def watermark_enabled(self) -> bool:
        """这一档 profile 下，成片会不会带上水印。"""
        return self.watermark.enabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_posix(),
            "default_profile": self.default_profile,
            "profile": self.profile.to_dict(),
            "profiles": [item.to_dict() for item in self.profiles],
            "watermark": self.watermark.to_dict(),
        }


def build_render_profile_report(
    outputs: OutputsConfig,
    *,
    source: Path,
    home: Path,
    name: str | None = None,
) -> RenderProfileReport:
    """组装 `--show` 的报告：profile + 水印实测 + 摆放（贴 / 跳过）。

    水印的判断只在 :func:`studio.render.watermark.plan_watermark` 一处，
    所以"CLI 说会贴"与"渲染真的贴了"不可能对不上。
    """
    profile = resolve_profile(outputs, name)
    return RenderProfileReport(
        source=source,
        default_profile=outputs.default_profile,
        profile=profile,
        profiles=tuple(_to_composite(key, value) for key, value in outputs.profiles.items()),
        watermark=plan_watermark(
            outputs.watermark,
            canvas_width=profile.width,
            canvas_height=profile.height,
            home=home,
        ),
    )
