"""单句合成（T2.6 · §04.3.6 / §04.3.3）。

这一层**不跑真引擎、也不跑 ffprobe**：假引擎用标准库 ``wave`` 写一段真 WAV，
假探针用 ``wave`` 读它的时长 —— 于是"命中缓存就不调引擎"这类断言是**数出来的**，
不是"看起来对"。真 ffmpeg 那条路（占位静音落盘）由
``tests/integration/test_sentence_resume.py`` 覆盖，这里只测它的失败口径。
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.media import CommandResult, MediaInfo
from studio.pools import worker_base  # noqa: F401  （保持 import 顺序稳定）
from studio.tts import sentence as sentence_module
from studio.tts.cache import TtsCache
from studio.tts.sentence import (
    PLACEHOLDER_MAX_MS,
    PLACEHOLDER_MIN_MS,
    SAPI_ENGINE,
    SapiEngine,
    estimate_duration_ms,
    sapi_rate_for,
    synthesize_sentence,
    write_placeholder,
)

SAMPLE_RATE = 22_050


# ── 假件：真 WAV、真时长，零外部依赖 ────────────────────────────────────


def _write_wav(path: Path, duration_ms: int, *, sample_rate: int = SAMPLE_RATE) -> None:
    """写一段**真的** PCM WAV（标准库 ``wave``）—— 假探针要能读出它的时长。"""
    frames = max(1, round(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


class FakeEngine:
    """数得清调用次数的假引擎（T2.6 验收里"引擎调用为 0"就是数它）。"""

    name = "fake"
    revision = "test"

    def __init__(self, *, duration_ms: int = 1000, fail: bool = False) -> None:
        self.calls: list[tuple[str, int]] = []
        self._duration_ms = duration_ms
        self._fail = fail

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append((text, rate))
        if self._fail:
            raise StudioError("引擎炸了", code=ErrorCode.TTS_SENTENCE_FAILED)
        _write_wav(out_path, self._duration_ms)


@pytest.fixture(autouse=True)
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``probe_media`` 换成"读 WAV 头"的版本（单测不依赖 ffprobe）。"""

    def probe(path: Path | str, **_kwargs: object) -> MediaInfo:
        target = Path(path)
        with wave.open(str(target), "rb") as handle:
            frames, rate = handle.getnframes(), handle.getframerate()
        return MediaInfo(
            path=target.as_posix(),
            size_bytes=target.stat().st_size,
            duration_ms=round(frames * 1000 / rate),
            video_codec=None,
            audio_codec="pcm_s16le",
            width=None,
            height=None,
            fps=None,
            pix_fmt=None,
            sample_rate=rate,
            channels=1,
            bitrate_kbps=None,
        )

    monkeypatch.setattr(sentence_module, "probe_media", probe)


@pytest.fixture
def cache(tmp_path: Path) -> TtsCache:
    return TtsCache(tmp_path / "cache" / "tts")


# ══════════════════════════════════════════════════════════════════════
# 缓存：命中就是"0 次引擎调用"
# ══════════════════════════════════════════════════════════════════════


def test_miss_calls_the_engine_once(tmp_path: Path, cache: TtsCache) -> None:
    engine = FakeEngine(duration_ms=1200)
    out = tmp_path / "s001.wav"

    result = synthesize_sentence("今天聊三件事", out_path=out, cache=cache, engine=engine, voice="Huihui")

    assert engine.calls == [("今天聊三件事", 0)]
    assert result.cache_hit is False
    assert result.duration_ms == 1200
    assert result.sample_rate == SAMPLE_RATE
    assert len(result.tts_hash) == 32
    assert out.is_file()
    assert cache.get(result.tts_hash) is not None, "合成完必须顺手收进缓存"


def test_hit_does_not_call_the_engine_again(tmp_path: Path, cache: TtsCache) -> None:
    """★ 这就是 §04.3.6 的 P2 前提：第二遍是**零**次引擎调用，不是"快一点"。"""
    engine = FakeEngine(duration_ms=800)
    first = synthesize_sentence("今天聊三件事", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    second_out = tmp_path / "s002.wav"

    second = synthesize_sentence("今天聊三件事", out_path=second_out, cache=cache, engine=engine)

    assert len(engine.calls) == 1
    assert second.cache_hit is True
    assert second.tts_hash == first.tts_hash
    assert second.duration_ms == first.duration_ms
    assert second_out.is_file(), "命中也要把音频拷到这一句的交付路径上"


def test_changed_text_is_a_miss(tmp_path: Path, cache: TtsCache) -> None:
    engine = FakeEngine()
    synthesize_sentence("第一版台词", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    synthesize_sentence("第二版台词", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    assert len(engine.calls) == 2


def test_voice_change_is_a_miss(tmp_path: Path, cache: TtsCache) -> None:
    """换音色必须重念：同一句话用不同音色念出来是两段不同的音频。"""
    engine = FakeEngine()
    synthesize_sentence("同一句话", out_path=tmp_path / "s001.wav", cache=cache, engine=engine, voice="A")
    synthesize_sentence("同一句话", out_path=tmp_path / "s001.wav", cache=cache, engine=engine, voice="B")
    assert len(engine.calls) == 2


# ══════════════════════════════════════════════════════════════════════
# 归一化：念的是归一化后的文本，键也是
# ══════════════════════════════════════════════════════════════════════


def test_engine_receives_the_normalized_text(tmp_path: Path, cache: TtsCache) -> None:
    """归一化必须发生在**送引擎之前**：否则 emoji / Markdown 会被念出来。"""
    engine = FakeEngine()
    result = synthesize_sentence(
        "**今天**聊三件事😀", out_path=tmp_path / "s001.wav", cache=cache, engine=engine
    )
    assert engine.calls[0][0] == "今天聊三件事"
    assert result.text == "今天聊三件事"


def test_normalization_does_not_change_the_key_when_output_is_the_same(
    tmp_path: Path, cache: TtsCache
) -> None:
    """``**今天**`` 与 ``今天`` 归一化后是同一句话 ⇒ 第二次必须命中（不能白念一遍）。"""
    engine = FakeEngine()
    first = synthesize_sentence("今天聊三件事", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    second = synthesize_sentence(
        "**今天**聊三件事", out_path=tmp_path / "s002.wav", cache=cache, engine=engine
    )
    assert len(engine.calls) == 1
    assert second.tts_hash == first.tts_hash


def test_text_that_normalizes_to_nothing_is_rejected(tmp_path: Path, cache: TtsCache) -> None:
    """纯 emoji / 纯符号：归一化后没有可念的东西 ⇒ 明确报错，而不是合成一段没声音的 wav。"""
    engine = FakeEngine()
    with pytest.raises(StudioError) as excinfo:
        synthesize_sentence("😀😀", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    assert excinfo.value.code is ErrorCode.TTS_SENTENCE_FAILED
    assert engine.calls == []


# ══════════════════════════════════════════════════════════════════════
# 失败口径
# ══════════════════════════════════════════════════════════════════════


def test_engine_failure_propagates_and_leaves_no_cache_entry(tmp_path: Path, cache: TtsCache) -> None:
    engine = FakeEngine(fail=True)
    with pytest.raises(StudioError):
        synthesize_sentence("今天聊三件事", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    assert cache.stats().files == 0


def test_zero_duration_output_is_a_qc_failure(tmp_path: Path, cache: TtsCache) -> None:
    """0 秒的产物在下游看起来"成功"了 —— 那是最坏的一种失败，必须当场拦住。"""
    engine = FakeEngine(duration_ms=0)
    with pytest.raises(StudioError) as excinfo:
        synthesize_sentence("今天聊三件事", out_path=tmp_path / "s001.wav", cache=cache, engine=engine)
    assert excinfo.value.code is ErrorCode.TTS_AUDIO_QC_FAILED
    assert cache.stats().files == 0


# ══════════════════════════════════════════════════════════════════════
# 语速映射 / 时长估算 / 占位静音
# ══════════════════════════════════════════════════════════════════════


def test_speed_maps_onto_the_sapi_rate_scale() -> None:
    """两套量纲不能直接互换：``speed=1.0`` 必须落在 SAPI 的 0 档（正常语速）。"""
    assert sapi_rate_for(1.0) == 0
    assert sapi_rate_for(1.5) == 5
    assert sapi_rate_for(0.5) == -5
    assert sapi_rate_for(2.0) == 10
    assert sapi_rate_for(3.0) == 10, "越界夹取，不能把 30 传给 SAPI"
    assert sapi_rate_for(0.0) == -10


def test_speed_reaches_the_engine_as_a_rate(tmp_path: Path, cache: TtsCache) -> None:
    engine = FakeEngine()
    synthesize_sentence("今天聊三件事", out_path=tmp_path / "s001.wav", cache=cache, engine=engine, speed=1.5)
    assert engine.calls == [("今天聊三件事", 5)]


def test_estimate_grows_with_length_and_stays_in_bounds() -> None:
    short = estimate_duration_ms("好。")
    long = estimate_duration_ms("这是一句明显更长的台词，用来确认估算随时长单调增加。" * 3)
    assert PLACEHOLDER_MIN_MS <= short < long <= PLACEHOLDER_MAX_MS


def test_estimate_is_calibrated_to_the_measured_speech_rate() -> None:
    """标定数据见模块 docstring（本机实测：``860 + 208 × 字数``）。

    允许 ±10% 的偏差 —— 这条断言要钉住的是"常数别被随手改成 50 或 5000"，
    不是"估算必须分毫不差"（它是估算）。
    """
    text = "第一件事，跑酷地图里最容易翻车的地方其实是落脚点。"  # 23 字
    expected = 900 + 210 * 23
    assert abs(estimate_duration_ms(text) - expected) / expected < 0.10


def test_write_placeholder_reports_the_measured_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 ffmpeg 那条路由集成测试覆盖；这里用假 ``run_command`` 验"返回实测值"。"""

    def fake_run(argv: list[str], **_kwargs: object) -> CommandResult:
        Path(argv[-1]).write_bytes(b"RIFF")
        return CommandResult(returncode=0, stdout="", stderr="")

    def fake_probe(path: Path | str, **_kwargs: object) -> MediaInfo:
        return MediaInfo(
            path=str(path),
            size_bytes=4,
            duration_ms=1980,
            video_codec=None,
            audio_codec="pcm_s16le",
            width=None,
            height=None,
            fps=None,
            pix_fmt=None,
            sample_rate=48_000,
            channels=1,
            bitrate_kbps=None,
        )

    monkeypatch.setattr(sentence_module, "run_command", fake_run)
    monkeypatch.setattr(sentence_module, "probe_media", fake_probe)
    out = tmp_path / "s001.wav"

    measured = write_placeholder("今天聊三件事", out_path=out)

    assert measured == 1980, "返回的是实测值，不是估算值（时间轴只认实测）"
    assert out.is_file()


def test_write_placeholder_raises_when_ffmpeg_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(sentence_module, "run_command", fake_run)
    with pytest.raises(StudioError) as excinfo:
        write_placeholder("今天聊三件事", out_path=tmp_path / "s001.wav")
    assert excinfo.value.code is ErrorCode.TTS_SENTENCE_FAILED


def test_sapi_engine_exposes_the_cache_key_identity() -> None:
    """引擎名与修订串是缓存键的一部分：改了它们 = 让旧缓存整体失效（有意的）。"""
    engine = SapiEngine()
    assert engine.name == SAPI_ENGINE == "sapi"
    assert engine.revision


def test_wav_helper_is_a_real_wav(tmp_path: Path) -> None:
    """夹具自检：``_write_wav`` 造的必须是真 WAV，否则上面的断言全是假的。"""
    target = tmp_path / "x.wav"
    _write_wav(target, 500)
    with wave.open(str(target), "rb") as handle:
        assert handle.getnframes() == pytest.approx(SAMPLE_RATE * 0.5, rel=0.01)
    assert target.read_bytes()[:4] == b"RIFF"
    assert struct.unpack("<I", target.read_bytes()[4:8])[0] == target.stat().st_size - 8
