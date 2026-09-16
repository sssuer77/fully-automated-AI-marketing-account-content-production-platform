"""音频总线：侧链避让 → 混音 → 两遍响度归一 → 限幅（T3.6 · §04.2.8.3）。

三段式，固定形态
----------------
```text
[1:a] aformat=fltp/48k/stereo, volume=<voice_gain>dB            → a_voice
[2:a] aformat=fltp/48k/stereo, volume=<bgm_gain>dB              → a_bgm（输入侧 -stream_loop -1）
[a_voice] asplit=2 → a_voice_sc / a_voice_mix                   ★ 人声必须分两路，见下
[a_bgm][a_voice_sc] sidechaincompress=threshold:ratio:attack:release → a_bgm_duck
[a_voice_mix][a_bgm_duck] amix=inputs=2:duration=first:normalize=0  → a_mix
[a_mix] loudnorm=I=…:TP=…:LRA=…:measured_*=…:offset=…               → a_norm
[a_norm] aresample=48000, alimiter=limit=<门禁值−0.3dB>:level=0         → aout
```

四处必须写对、写错也不报错的地方
--------------------------------
1. **``amix`` 必须 ``normalize=0``**。默认的 ``normalize=1`` 会按输入路数把人声
   衰减 1/N —— 两路输入就是 −6dB。这是"人声偏小"最常见的根因，而它**不会报错**，
   只会让人声比 BGM 小一大截。
2. **``sidechaincompress`` 的输入顺序是 ``[主路][侧链]``**，即**BGM 在前、人声在后**。
   写反了 ffmpeg **一个字都不说**，但被压的是人声、``a_bgm_duck`` 那一路其实是"被压过的
   人声"：BGM 整条丢失，人声被叠了一份（峰值顶穿 0dBFS），后面 ``loudnorm`` 的真峰值
   约束就被卡死，成品响度掉到门禁边缘。**规格 §04.2.8.3 与本节旧稿都把这个顺序写成了
   "``[侧链][主路]``"，那是错的** —— ``ffmpeg -h filter=sidechaincompress`` 写明
   ``#0: main`` / ``#1: sidechain``。
3. **人声必须先 ``asplit``**。侧链要一路人声、混音还要一路，而 ffmpeg 的滤镜图里
   **一个标签只能被消费一次** —— 直接写两遍 ``[a_voice]`` 会得到
   ``Stream specifier 'a_voice' in filtergraph ... matches no streams``，一条完全
   指不到"标签被复用"的错。规格 §04.2.8.3 的模板就是直接用了两遍，这里是**实测**
   出来的修正。
4. **``aloop`` 不要用**。规格里写的 ``aloop=loop=-1:size=2000000000`` 里的 ``size``
   是**采样缓冲的样本数**，填 20 亿就是让 ffmpeg 申请几 GB 内存。BGM 的循环放在
   **输入侧**（``-stream_loop -1``）由 demuxer 做，零缓冲、零内存风险。

为什么两遍 loudnorm
-------------------
一遍 ``loudnorm`` 是**动态**的：它边播边调增益，遇到"人声前 5 秒很轻、后 5 秒很响"
就会把前段推上去、后段压下来，听感是被压扁的。两遍法先量一遍（不入图、不改音频），
再把 ``measured_*`` 喂给第二遍的 ``linear=true``，于是第二遍只做一次**静态**增益 +
线性限制 —— 这是 §04.2.8.3 与 EBU R128 的标准做法。

代价是**多跑一次纯音频的 ffmpeg**（不解视频、不编码，只解码 + 过滤镜）。3 分钟的
片子大约多花 3–8 秒，换来的是响度真的落在 ``[-16.5, -15.5]`` 这个发布门禁里。

量不出来（老 ffmpeg / 输出格式变了）⇒ 退回**一遍** loudnorm 并记一句 warn。
响度归一是"让片子听起来正常"，不是"能不能发布"的判据；为了它让整支片子渲不出来，
是拿装饰品堵主链路 —— 这个项目已经在"水印"上踩过一次（见 ``render/watermark.py``）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.config import AudioConfig
from studio.core.media import ffmpeg_binary, run_command

__all__ = [
    "CODEC_PEAK_MARGIN_DB",
    "LIMITER_AUTO_LEVEL",
    "MEASURE_TIMEOUT_SEC",
    "MIX_SAMPLE_RATE",
    "TARGET_LRA",
    "LoudnessMeasurement",
    "MixSettings",
    "build_audio_chain",
    "build_measure_argv",
    "build_measure_file_argv",
    "limiter_limit",
    "measure_file",
    "measure_mix",
    "parse_loudnorm_json",
]

#: 混音总线的采样率。与 profile 的 ``audio_sample_rate`` 一致（48k），
#: 于是编码那一步不需要再做一次采样率转换。
MIX_SAMPLE_RATE: Final[int] = 48_000

#: ``loudnorm`` 的目标响度范围（LRA）。规格 §03-data-model 的 ``audio_bus.loudness``
#: 写死 11 LU —— 短视频口播的动态范围本来就窄，给宽了会把 BGM 抬起来。
TARGET_LRA: Final[float] = 11.0

#: 编码过冲的余量（dB）。限幅器压的是**编码前**的样本峰值，而门禁量的是
#: **解码后**的真峰值 —— AAC 的重建会过冲零点几 dB。实测：限幅器设到 −1.0 dBFS 时
#: 解码出来是 −0.85 dBTP（超了 −1.0 的门禁），再让 0.3 dB 才稳定落在 −1.2 dBTP。
#:
#: **余量必须加在 `alimiter` 上，不能加到 `loudnorm` 的 TP 目标上。** 看着像等价的
#: 两处，其实不是：`loudnorm` 的 `TP` 只是**第一遍算 offset 时的参考值**，第二遍
#: `linear=true` 施加的是"把响度归到目标"的那一个静态增益。实测 `measured_I=-17.35`
#: 配 `offset=0.86` 时它老老实实 +0.86dB，把峰值推到 +2.78dBFS，**一点都没拦**。
#: 真正压住峰值的是后面的 `alimiter` —— 所以余量放在 `loudnorm` 上等于没人管峰值。
CODEC_PEAK_MARGIN_DB: Final[float] = 0.3

#: ``alimiter`` 的**自动电平必须关掉**。它的 ``level`` 选项默认是开的，语义是
#: "把输出抬到 0dBFS" —— 一个上限幅器之后再加的**补偿增益**。后果是：loudnorm 好
#: 不容易把响度归到 −16.0 LUFS，限幅器又给抬回 −15.5 左右，正好把发布门禁
#: （``[-16.5, -15.5]``）顶出去。规格 §04.2.8.3 的模板里写的是裸的
#: ``alimiter=limit=0.95``，那是踩了这个坑的版本；这里是**实测**出来的修正。
LIMITER_AUTO_LEVEL: Final[bool] = False

#: 测量那一遍的超时（纯音频，比整片渲染快得多）。
MEASURE_TIMEOUT_SEC: Final[int] = 600

#: loudnorm 的 JSON 报告块。ffmpeg 的日志里混着普通文本与 ``[Parsed_...]`` 前缀，
#: 整段当 JSON 解会失败，所以只抠"含 input_i 的那一对花括号"。
_LOUDNORM_JSON: Final[re.Pattern[str]] = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class LoudnessMeasurement:
    """``loudnorm`` 第一遍的读数（喂给第二遍的 ``measured_*``）。"""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    def to_dict(self) -> dict[str, float]:
        return {
            "input_i": self.input_i,
            "input_tp": self.input_tp,
            "input_lra": self.input_lra,
            "input_thresh": self.input_thresh,
            "target_offset": self.target_offset,
        }


@dataclass(frozen=True, slots=True)
class MixSettings:
    """音频总线的全部参数（**只描述，不执行**）。"""

    voice_gain_db: float = 0.0
    bgm_gain_db: float = -21.0
    duck_threshold: float = 0.05
    duck_ratio: float = 8.0
    duck_attack_ms: int = 20
    duck_release_ms: int = 420
    target_lufs: float = -16.0
    true_peak_dbtp: float = -1.0

    @classmethod
    def from_config(cls, audio: AudioConfig) -> MixSettings:
        """从 ``config/outputs.yaml → audio`` 取一份快照。

        参数值只在 YAML 里定义一次：``AudioConfig`` 是**校验**（范围、类型），这里是
        **搬运**。两边各写一份默认值，某天改了 YAML 里的 ducking 阈值而渲染器还在用
        代码里的旧值，表现是"配置改了没反应" —— 这类问题查起来很贵。
        """
        return cls(
            voice_gain_db=audio.voice_gain_db,
            bgm_gain_db=audio.bgm_gain_db,
            duck_threshold=audio.duck_threshold,
            duck_ratio=audio.duck_ratio,
            duck_attack_ms=audio.duck_attack_ms,
            duck_release_ms=audio.duck_release_ms,
            target_lufs=audio.target_lufs,
            true_peak_dbtp=audio.true_peak_dbtp,
        )


def parse_loudnorm_json(text: str) -> LoudnessMeasurement | None:
    """从 ffmpeg 日志里抠出 loudnorm 的 JSON 报告（纯函数）。

    取**最后**一个匹配块：日志里可能打印多次（``-t`` 截断、多段输出），最后一份
    才是这一遍的最终读数。缺任何一个字段 ⇒ ``None``（宁可退回一遍法，也不拿
    半份读数去喂第二遍 —— 缺 ``measured_thresh`` 的线性模式会算出离谱的增益）。
    """
    matches = _LOUDNORM_JSON.findall(text)
    if not matches:
        return None
    try:
        payload: dict[str, Any] = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None

    values: dict[str, float] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        raw = payload.get(key)
        try:
            values[key] = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return LoudnessMeasurement(**values)


def limiter_limit(settings: MixSettings) -> float:
    """限幅阈值（线性）= ``true_peak_dbtp`` 再让出编码过冲的余量。

    **这是整条链上唯一真正压峰值的地方**：``loudnorm`` 第二遍只施加静态增益
    （见 ``CODEC_PEAK_MARGIN_DB``），所以这里设多宽，解码后的真峰值就有多高。

    不从规格里那个写死的 ``0.95`` 来：``0.95`` 是 −0.45 dBFS，比门禁的
    ``true_peak_max_dbtp = −1.0`` **宽**了半 dB —— 照抄它，片子会被发布门禁拦下，
    而报错只会说"真峰值超标"，指不回是限幅器设宽了。
    """
    return 10 ** ((settings.true_peak_dbtp - CODEC_PEAK_MARGIN_DB) / 20)


def _aformat(layout: str) -> str:
    """统一的中间格式：``fltp`` / 48k / 指定声道布局。

    用 ``fltp``（32 位浮点平面）而不是 ``s16``：``sidechaincompress`` 与
    ``loudnorm`` 内部都按浮点算，喂整数进去等于在链路上多插两次转换。
    """
    return f"aformat=sample_fmts=fltp:sample_rates={MIX_SAMPLE_RATE}:channel_layouts={layout}"


def build_audio_chain(
    *,
    voice_index: int,
    bgm_index: int | None,
    settings: MixSettings,
    measured: LoudnessMeasurement | None,
    warn: list[str] | None = None,
) -> list[str]:
    """拼音频那一段滤镜图（纯函数；返回的每一段都以 ``[aout]`` 收尾）。

    ``bgm_index is None`` ⇒ **单轨人声**：不插侧链、不插 ``amix``，直接走
    ``anull`` 占位。这条分支必须存在且必须**不报错** —— BGM 是可选输入，
    没有它的时候"少一层背景音乐"，不是"这条片子渲不出来"。
    """
    parts: list[str] = []

    if bgm_index is None:
        parts.append(
            f"[{voice_index}:a]{_aformat('stereo')},volume={settings.voice_gain_db:g}dB,anull[a_mix]"
        )
    else:
        parts.append(f"[{voice_index}:a]{_aformat('stereo')},volume={settings.voice_gain_db:g}dB[a_voice]")
        # 人声分两路：一路当侧链去压 BGM，一路原样进混音。
        # 不加这个 asplit，下面两处 [a_voice] 会让 ffmpeg 报"标签匹配不到流"。
        parts.append("[a_voice]asplit=2[a_voice_sc][a_voice_mix]")
        parts.append(f"[{bgm_index}:a]{_aformat('stereo')},volume={settings.bgm_gain_db:g}dB[a_bgm]")
        # 输入顺序 = [主路][侧链]（`ffmpeg -h filter=sidechaincompress` 写明
        # `#0: main` / `#1: sidechain`）：BGM 是被压的那一路，人声是触发的那一路。
        # 写反了 ffmpeg 不报错，但 `a_bgm_duck` 会变成"被压过的人声" —— BGM 整条
        # 丢失、人声被叠一份，峰值顶穿 0dBFS，后面 loudnorm 的真峰值约束直接卡死。
        parts.append(
            f"[a_bgm][a_voice_sc]sidechaincompress="
            f"threshold={settings.duck_threshold:g}:ratio={settings.duck_ratio:g}:"
            f"attack={settings.duck_attack_ms}:release={settings.duck_release_ms}:"
            f"makeup=1[a_bgm_duck]"
        )
        # duration=first：以**人声**长度为准（它是第 0 路）；BGM 被 -stream_loop 拉长，
        # 反过来（duration=longest）会让成片被 BGM 拖着走。
        parts.append("[a_voice_mix][a_bgm_duck]amix=inputs=2:duration=first:normalize=0[a_mix]")

    loudnorm = f"loudnorm=I={settings.target_lufs:g}:TP={settings.true_peak_dbtp:g}:LRA={TARGET_LRA:g}"
    if measured is not None:
        loudnorm += (
            f":linear=true:measured_I={measured.input_i:g}:measured_TP={measured.input_tp:g}"
            f":measured_LRA={measured.input_lra:g}:measured_thresh={measured.input_thresh:g}"
            f":offset={measured.target_offset:g}"
        )
    elif warn is not None:
        warn.append("响度只量了一遍（loudnorm 走动态模式）；要更稳就确认 ffmpeg 能输出 JSON 报告")
    parts.append(f"[a_mix]{loudnorm}[a_norm]")
    level = "1" if LIMITER_AUTO_LEVEL else "0"
    parts.append(
        f"[a_norm]aresample={MIX_SAMPLE_RATE},"
        f"alimiter=limit={limiter_limit(settings):.4f}:level={level}[aout]"
    )
    return parts


def build_measure_argv(
    *,
    voice: Path,
    bgm: Path | None,
    settings: MixSettings,
    duration_ms: int,
) -> list[str]:
    """第一遍：只量响度，不解视频、不编码（``-f null -``）。

    音频链与正式渲染**同一条**（含侧链与混音）—— 量的是"最终混出来的那条音轨"
    的响度。只量人声母带的话，第二遍的 ``measured_I`` 会偏低（少了 BGM 那一路），
    归一化就会把人声推得偏高，正好撞上限幅器。
    """
    argv: list[str] = [ffmpeg_binary(), "-hide_banner", "-nostats", "-y"]
    argv += ["-i", str(voice)]
    bgm_index: int | None = None
    if bgm is not None:
        bgm_index = 1
        argv += ["-stream_loop", "-1", "-i", str(bgm)]

    chain = build_audio_chain(voice_index=0, bgm_index=bgm_index, settings=settings, measured=None)
    # 把最后一段的 loudnorm 换成"只报告、不入图"的那一版。
    chain[-2] = (
        f"[a_mix]loudnorm=I={settings.target_lufs:g}:TP={settings.true_peak_dbtp:g}:"
        f"LRA={TARGET_LRA:g}:print_format=json[a_norm]"
    )
    argv += ["-filter_complex", ";".join(chain)]
    argv += ["-map", "[aout]", "-t", f"{max(1, duration_ms) / 1000:.3f}", "-f", "null", "-"]
    return argv


def measure_mix(
    *,
    voice: Path,
    bgm: Path | None,
    settings: MixSettings,
    duration_ms: int,
    timeout: int = MEASURE_TIMEOUT_SEC,
) -> LoudnessMeasurement | None:
    """跑第一遍并解析读数；量不出来 ⇒ ``None``（调用方退回一遍法，不报错）。"""
    argv = build_measure_argv(voice=voice, bgm=bgm, settings=settings, duration_ms=duration_ms)
    result = run_command(argv, timeout=timeout)
    if not result.ok:
        return None
    return parse_loudnorm_json(result.stderr + "\n" + result.stdout)


def build_measure_file_argv(path: Path, *, settings: MixSettings) -> list[str]:
    """量**已经落盘的成片**那一遍的 argv（发布门禁读的就是这个数）。"""
    return [
        ffmpeg_binary(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "a:0",
        "-af",
        f"loudnorm=I={settings.target_lufs:g}:TP={settings.true_peak_dbtp:g}:"
        f"LRA={TARGET_LRA:g}:print_format=json",
        "-f",
        "null",
        "-",
    ]


def measure_file(
    path: Path, *, settings: MixSettings, timeout: int = MEASURE_TIMEOUT_SEC
) -> LoudnessMeasurement | None:
    """量**成片自己**的响度与真峰值（``input_*`` 在这里读作"这个文件的读数"）。

    为什么不能拿 :func:`measure_mix` 的读数充数
    ------------------------------------------
    ``loudnorm`` 报的 ``input_i`` / ``input_tp`` 是**归一化之前**的输入读数 ——
    那是"我们打算把它抬到 −16 LUFS"的**起点**，不是终点。把它写进
    ``quality_json.lufs``，一条听起来正常的片子会显示成 −22 LUFS，发布门禁再按这个
    数把好片子拦下来。要回答"这条片子到底多响"，只能量这条片子。

    代价是一次纯解码（``-f null``，不编码、不落盘）：成片多长就解多久，2 分钟的
    片子几秒。换来的是 QC 的数字与发布门禁量的是**同一个东西**。

    量不出来（老 ffmpeg / 没有音轨）⇒ ``None``：QC 是**报告**，不是出片的前提。
    """
    result = run_command(build_measure_file_argv(path, settings=settings), timeout=timeout)
    if not result.ok:
        return None
    return parse_loudnorm_json(result.stderr + "\n" + result.stdout)
