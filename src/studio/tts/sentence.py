"""单句合成（T2.6 · §04.3.6 / §04.3.3）—— **一句 = 一次引擎调用 = 一个可续传的原子**。

一条句子的合成路径
------------------
```text
normalize(文本)          归一化：数字/缩写/多音字（T2.5，幂等）
      │
      ├─ 算 tts_hash ──▶ 缓存命中？ ── 是 ─▶ 拷到 s00N.wav（**0 次引擎调用**）
      │                                    │
      └─ 否 ─▶ 引擎合成 ─▶ ffprobe 实测 ─▶ 收进缓存
```
"实测"这一步不是仪式：SAPI / CosyVoice 都可能返回一个与落盘音频对不上的时长
（引擎报的是"计划时长"），而字幕计时与时间轴只认 ffprobe（§04.3.4 C12）。

为什么这里自己定一个最小引擎缝（而不是等 T2.3 的 ``VoiceEngine``）
----------------------------------------------------------------
T2.3 要的是"多引擎路由 + 熔断 + §04.3.3 决策表"，而它依赖 T2.2 的常驻推理服务
（GPU 权重，E5 未到位）。T2.6 却现在就要能跑、能测 —— 尤其是验收里那条硬断言
"重启后已完成句的**引擎调用次数为 0**"：数不出调用次数就没法断言。
所以先定一个只有"一句进、一段 WAV 出"的 Protocol，SAPI 是它的第一个实现。
T2.3 落地时把 :class:`SapiEngine` 换成路由后的实现即可，**调用方（voice 池）不用改**。

降级句的"等长静音"是**估算**，而且必须说明白
--------------------------------------------
一句念不出来时，成片要留一段静音占位（§04.3.3 的 ``PLACEHOLDER``：字幕照留）。
"等长"里的"长"没有任何东西可测 —— 音频压根没生成出来，所以只能估。
估算系数是**本机实测标定**的（Huihui Desktop / rate 1，3 个样本共 86 字）：
``860 + 208 × 字数``（毫秒），取整成 :data:`PLACEHOLDER_BASE_MS` +
:data:`PLACEHOLDER_PER_CHAR_MS`。它只决定"这段静音多长"，而字幕与时间轴用的是
ffprobe 实测值 —— 估偏 20% 的后果是"这一句的停顿长一点"，不是字幕错位。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.media import ffmpeg_binary, probe_media, run_command
from studio.domain.text import count_chars
from studio.tts.cache import TtsCache, tts_cache_key
from studio.tts.sapi import synthesize
from studio.tts.text_normalize import Glossary, normalize

__all__ = [
    "PLACEHOLDER_BASE_MS",
    "PLACEHOLDER_MAX_MS",
    "PLACEHOLDER_MIN_MS",
    "PLACEHOLDER_PER_CHAR_MS",
    "PLACEHOLDER_SAMPLE_RATE",
    "SAPI_ENGINE",
    "SAPI_REVISION",
    "SapiEngine",
    "SentenceEngine",
    "SentenceSynthesis",
    "copy_audio",
    "estimate_duration_ms",
    "sapi_rate_for",
    "synthesize_sentence",
    "write_placeholder",
]

logger = get_logger("studio.tts.sentence")

#: 引擎标识（进缓存键）。SAPI 是"本机自带"的降级档，CosyVoice 到位后换掉这个值
#: 即可让旧缓存整体失效 —— 这正是"引擎名进键"的用处。
SAPI_ENGINE: Final[str] = "sapi"

#: 引擎版本标识。SAPI 的版本取决于本机装了哪个语音包，而**音色名已经在键里**了
#: （语音包换了，音色名通常跟着变），所以这里用一个固定的修订串。
SAPI_REVISION: Final[str] = "win-sapi"

#: 请求的采样率（进缓存键）。SAPI 实际只出 22.05 kHz 单声道，统一到 48 kHz 是
#: 拼接那一步的事（``synth.concat_wavs``）—— 键里记的是**请求**，不是产物属性。
SENTENCE_SAMPLE_RATE: Final[int] = 48_000

#: 占位静音的时长估算（见模块 docstring 的标定数据）
PLACEHOLDER_BASE_MS: Final[int] = 900
PLACEHOLDER_PER_CHAR_MS: Final[int] = 210
PLACEHOLDER_MIN_MS: Final[int] = 500
PLACEHOLDER_MAX_MS: Final[int] = 15_000

#: 占位静音的采样率（与 ``voice_master`` 对齐，拼接时不用再转一次）
PLACEHOLDER_SAMPLE_RATE: Final[int] = 48_000

#: 生成占位静音的超时（纯合成一段无声 WAV，秒级）
PLACEHOLDER_TIMEOUT_SEC: Final[int] = 60


class SentenceEngine(Protocol):
    """一句文本 ⇒ 一段 WAV 的最小引擎缝（T2.3 的路由落地后替换实现）。"""

    name: str
    revision: str

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None: ...


class SapiEngine:
    """:class:`SentenceEngine` 的 SAPI 实现（Windows 自带、零下载、零显存）。"""

    name: str = SAPI_ENGINE
    revision: str = SAPI_REVISION

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        synthesize(text, out_path, voice=voice, rate=rate)


def sapi_rate_for(speed: float) -> int:
    """``speed``（0.5–2.0，DB 的口径）⇒ SAPI 的 ``rate``（−10…10）。

    线性映射 + 夹取：``speed=1.0`` ⇒ 0（正常），0.5 ⇒ −5，2.0 ⇒ 10。
    为什么不直接把 DB 的 ``speed`` 当 ``rate``：两者量纲不同，直接把 1.0 传给
    SAPI 会得到"语速 −10 档"（慢到没法听），而那种错**不会报错**。
    """
    return max(-10, min(10, round((speed - 1.0) * 10)))


def estimate_duration_ms(text: str) -> int:
    """一句话的**估算**时长（毫秒）—— 只给占位静音用（见模块 docstring）。"""
    return max(
        PLACEHOLDER_MIN_MS,
        min(PLACEHOLDER_MAX_MS, PLACEHOLDER_BASE_MS + PLACEHOLDER_PER_CHAR_MS * count_chars(text)),
    )


@dataclass(frozen=True, slots=True)
class SentenceSynthesis:
    """一句合成的结论（进 ``script_sentences`` 的那几列 + 缓存是否命中）。"""

    text: str
    audio_path: Path
    duration_ms: int
    sample_rate: int | None
    tts_hash: str
    cache_hit: bool
    engine: str
    voice_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "audio_path": self.audio_path.as_posix(),
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "tts_hash": self.tts_hash,
            "cache_hit": self.cache_hit,
            "engine": self.engine,
            "voice_id": self.voice_id,
        }


def synthesize_sentence(
    text: str,
    *,
    out_path: Path,
    cache: TtsCache,
    engine: SentenceEngine | None = None,
    voice: str | None = None,
    speed: float = 1.0,
    emotion: str = "neutral",
    seed: int | None = None,
    sample_rate: int = SENTENCE_SAMPLE_RATE,
    glossary: Glossary | None = None,
) -> SentenceSynthesis:
    """合成一句（命中缓存则**一次引擎调用都不发生**）。

    :param text: 原始文本（未归一化；归一化在这里做，因为缓存键吃的是归一化后的）
    :param out_path: 产物落点（``data/output/voice/<task_id>/s00N.wav``）
    :param voice: 已解析的音色名。``None`` ⇒ 交给引擎自己挑 —— 但那样**每句都要
        重挑一次**（列音色要起一个 PowerShell，1–2 秒），所以池的装配方会解析一次
        再逐句传进来。
    :raises StudioError: 文本空 / 引擎失败 / 产物为 0 秒
    """
    chosen = engine if engine is not None else SapiEngine()
    spoken = normalize(text, glossary=glossary if glossary is not None else Glossary())
    if not spoken.strip():
        raise StudioError(
            "归一化之后没有可念的文本",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"text": text[:80]},
            remediation="检查这一句是不是纯符号 / emoji（T2.5 的归一化会把这些清掉）",
        )

    key = tts_cache_key(
        engine=chosen.name,
        engine_revision=chosen.revision,
        voice_id=voice or "",
        normalized_text=spoken,
        speed=speed,
        emotion=emotion,
        seed=seed,
        sample_rate=sample_rate,
    )

    hit = cache.get(key)
    if hit is not None:
        copy_audio(hit, out_path)
        info = probe_media(out_path)
        logger.info("tts.sentence_cache_hit", key=key, duration_ms=info.duration_ms)
        return SentenceSynthesis(
            text=spoken,
            audio_path=out_path,
            duration_ms=max(0, info.duration_ms),
            sample_rate=info.sample_rate,
            tts_hash=key,
            cache_hit=True,
            engine=chosen.name,
            voice_id=voice,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    chosen.synthesize(spoken, out_path, voice=voice, rate=sapi_rate_for(speed))
    info = probe_media(out_path)
    if info.duration_ms <= 0:
        raise StudioError(
            "合成产物时长为 0，这一句没有真正产出音频",
            code=ErrorCode.TTS_AUDIO_QC_FAILED,
            context={"out_path": out_path.as_posix(), "engine": chosen.name, "voice": voice},
            remediation="换一个音色重试（音色选错时会得到一段能播但没声音的 wav）",
        )

    cache.put(key, out_path)
    return SentenceSynthesis(
        text=spoken,
        audio_path=out_path,
        duration_ms=info.duration_ms,
        sample_rate=info.sample_rate,
        tts_hash=key,
        cache_hit=False,
        engine=chosen.name,
        voice_id=voice,
    )


def write_placeholder(
    text: str,
    *,
    out_path: Path,
    duration_ms: int | None = None,
    timeout: int = PLACEHOLDER_TIMEOUT_SEC,
) -> int:
    """写一段"等长"静音占位（§04.3.3 的 ``PLACEHOLDER``），返回实测时长。

    ``duration_ms`` 缺省 ⇒ 用 :func:`estimate_duration_ms` 估。**返回实测值**：
    调用方要拿它去填 ``tts_duration_ms``，而那一列是时间轴的输入 —— 填估算值
    会让时间轴与盘上的音频对不上（估 2.0 秒、实际 1.98 秒，几百句累起来就是
    字幕整体偏移）。
    """
    ms = duration_ms if duration_ms is not None else estimate_duration_ms(text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(f"{out_path.stem}.partial{out_path.suffix}")
    result = run_command(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={PLACEHOLDER_SAMPLE_RATE}:cl=mono",
            "-t",
            f"{ms / 1000:.3f}",
            "-c:a",
            "pcm_s16le",
            str(partial),
        ],
        timeout=timeout,
    )
    if not result.ok or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise StudioError(
            f"占位静音生成失败（rc={result.returncode}）：{result.tail()}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"out_path": out_path.as_posix(), "duration_ms": ms},
            remediation="确认 ffmpeg 可用（`studio doctor`）",
        )
    partial.replace(out_path)
    info = probe_media(out_path)
    if info.duration_ms <= 0:
        raise StudioError(
            "占位静音时长为 0",
            code=ErrorCode.TTS_AUDIO_QC_FAILED,
            context={"out_path": out_path.as_posix(), "requested_ms": ms},
            remediation="确认 ffmpeg 的 lavfi/anullsrc 可用（换一套 ffmpeg 构建）",
        )
    return info.duration_ms


def copy_audio(source: Path, target: Path) -> None:
    """把一段音频拷到交付路径（先 ``.partial`` 再改名，不留半成品）。

    **公开**：缓存命中（本模块）与"已完成的句子从缓存找回音频"（配音池）是
    同一件事，各写一份迟早会有一边忘了原子改名 —— 那种半成品在成片里表现为
    "这一句播到一半断了"。
    """
    if source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")
    try:
        shutil.copyfile(source, partial)
        partial.replace(target)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise StudioError(
            f"缓存音频拷到产物路径失败：{exc}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"source": source.as_posix(), "target": target.as_posix()},
            remediation="确认磁盘有空间、目标目录可写",
        ) from exc
