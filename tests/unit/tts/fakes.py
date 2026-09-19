"""配音测试的共享假件：**一段真的有声音的** PCM。

为什么专门有个"非静音"的写法
----------------------------
T2.3 起，``synthesize_sentence`` 会把产物**听一遍**再收进缓存（§04.3.3 的静音 /
爆音判据，见 :func:`studio.tts.sentence.check_audio_qc`）。于是"假引擎写一段全零
PCM"这条用了很久的捷径**不再等价于"合成成功"** —— 它现在被判成 ``TTS_SILENT``，
而那正是判据要拦住的东西。所以假引擎改写成一段正弦音：它有真实的 RMS 与峰值，
判据放行；想验判据本身，就让 ``analyze_volume`` 报出你想要的读数
（:func:`patch_analyze_volume`），而不是靠"写一段坏音频"。

为什么不放进 ``tests/conftest.py``
----------------------------------
``tests/integration/`` 与 ``tests/contract/`` 没有 ``__init__.py``，从那里 import
根 ``conftest`` 会让同一个文件挂在两个模块名下（mypy 报 duplicate module）。
这里跟 ``tests/unit/agents/fakes.py`` 的先例，放进有 ``__init__.py`` 的包里。
"""

from __future__ import annotations

import array
import math
import sys
import wave
from pathlib import Path

import pytest

from studio.core.media import VolumeStats
from studio.tts import sentence as sentence_module

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "HEALTHY_MAX_DB",
    "HEALTHY_MEAN_DB",
    "TONE_AMPLITUDE",
    "TONE_FREQ_HZ",
    "healthy_volume",
    "patch_analyze_volume",
    "tone_frames",
    "write_tone",
]

#: 配音产物的采样率（跟 ``studio.tts.sentence.SENTENCE_SAMPLE_RATE`` 一致）
DEFAULT_SAMPLE_RATE = 48_000

#: 440 Hz（标准音高）：随便挑的，只要**不是直流**就行
TONE_FREQ_HZ = 440.0

#: 0.25 满量程 ⇒ 峰值 −12.0 dBFS / RMS −15.1 dBFS —— 落在 §04.3.3 的判据**里面**
#: （既不远得像静音，也不顶到 −0.5 dBFS 的爆音线）
TONE_AMPLITUDE = 0.25

#: "一段正常朗读"的读数：判据放行（-50 < -20 且 -6 < -0.5）
HEALTHY_MEAN_DB = -20.0
HEALTHY_MAX_DB = -6.0


def tone_frames(
    frames: int,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    freq: float = TONE_FREQ_HZ,
    amplitude: float = TONE_AMPLITUDE,
) -> bytes:
    """``frames`` 个 16-bit 单声道采样（小端）：一段正弦音，**不是静音**。"""
    peak = amplitude * 32_767.0
    step = 2.0 * math.pi * freq / sample_rate
    samples = array.array("h", (round(peak * math.sin(step * index)) for index in range(frames)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def write_tone(
    path: Path,
    *,
    duration_ms: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Path:
    """在 ``path`` 落一段真 WAV：时长真的对得上，内容真的有声音。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(tone_frames(frames, sample_rate=sample_rate))
    return path


def healthy_volume() -> VolumeStats:
    """判据会放行的读数（单测里替掉真 ffmpeg 的那一份）。"""
    return VolumeStats(max_db=HEALTHY_MAX_DB, mean_db=HEALTHY_MEAN_DB)


def patch_analyze_volume(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mean_db: float | None = HEALTHY_MEAN_DB,
    max_db: float | None = HEALTHY_MAX_DB,
) -> None:
    """把 ``analyze_volume`` 换成"读数是这些"的假件（**单测不跑 ffmpeg**）。

    ``None`` 表示"量不出来"（ffmpeg 不在 / 文件读不了）—— 判据对这种情况**不判**。
    """

    def fake(path: Path | str, **_kwargs: object) -> VolumeStats:
        del path
        return VolumeStats(max_db=max_db, mean_db=mean_db)

    monkeypatch.setattr(sentence_module, "analyze_volume", fake)
