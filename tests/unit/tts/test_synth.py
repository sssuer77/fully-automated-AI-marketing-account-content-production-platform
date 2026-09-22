"""渲染那条路的配音（``tts/synth.py::synthesize_script``）。

为什么这个文件到 2026-09-21 才出现
----------------------------------
它以前没有测试，因为它只做「切句 → 逐句 SAPI → 拼接」。真机 ``r0001`` 挂在
``voice 55/58`` 之后才知道：渲染面板的音色下拉来自**常驻引擎**（``GET /api/v1/voices``），
而这里直连 **SAPI** —— 选 ``bigbear`` 就 ``SelectVoice`` 抛、每句失败 3 次。所以这里
钉三件事：

1. 引擎判据与配音池**同一份**（常驻服务可用就用它）；
2. 念不出来的音色退回兜底音色，并**留下说明**（否则只能靠听出来嗓子变了）；
3. 给进来的句子**不再切一遍** —— 重切会得到另一个句数（真机 55 句 vs 58 句），
   字幕与时间轴从此与音频对不上。
"""

from __future__ import annotations

import wave
from collections.abc import Sequence
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo
from studio.core.paths import StudioPaths
from studio.tts import synth as synth_module
from studio.tts.engine import SentenceEngine
from tests.unit.tts.fakes import patch_analyze_volume, tone_frames

SAMPLE_RATE = 24_000


def _write_wav(path: Path, duration_ms: int) -> None:
    """一段**真的有声音**的 WAV（不是全零 —— 全零会被音频 QC 判成静音）。"""
    frames = max(1, round(SAMPLE_RATE * duration_ms / 1000))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(tone_frames(frames, sample_rate=SAMPLE_RATE))


class FakeEngine:
    """数得清「念了哪几句、用的是谁的声音」的假引擎。"""

    def __init__(self, *, name: str = "cosyvoice2", revision: str = "rev1") -> None:
        self.name = name
        self.revision = revision
        self.calls: list[tuple[str, str | None]] = []

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append((text, voice))
        _write_wav(out_path, 800)


class FakePicker:
    """引擎选择器的替身（真的那个要探测常驻服务，单测不碰网络）。"""

    def __init__(self, engine: SentenceEngine, voice: str | None, speakable: tuple[str, ...]) -> None:
        self._triple = (engine, voice, speakable)

    def __call__(self) -> tuple[SentenceEngine, str | None, tuple[str, ...]]:
        return self._triple


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch, tmp_paths: StudioPaths) -> None:
    """单测不跑 ffmpeg / ffprobe：探针读 WAV 头，拼接写一段占位母带。"""

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

    def concat(files: Sequence[Path], out_path: Path, **_kwargs: object) -> Path:
        assert files, "拼接这一步不该收到空列表"
        _write_wav(out_path, 1500)
        return out_path

    monkeypatch.setattr(synth_module, "probe_media", probe)
    monkeypatch.setattr(synth_module, "concat_wavs", concat)
    patch_analyze_volume(monkeypatch)
    # 读音词表：单测里给一份**空的**（真的那份在 prompts/ 下，热重载由 T2.5 自己测）
    tmp_paths.glossary_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_paths.glossary_file.write_text("", encoding="utf-8")


def _patch_picker(monkeypatch: pytest.MonkeyPatch, picker: FakePicker) -> None:
    monkeypatch.setattr(synth_module, "EnginePicker", lambda _paths: picker)


def test_the_resident_engine_is_used_when_it_is_up(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 真机坑（2026-09-21）：面板的音色来自常驻引擎 ⇒ 这里也必须用它念。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear", "littlebear")))

    result = synth_module.synthesize_script(
        "第一句。第二句。", paths=tmp_paths, task_id="t1", voice="bigbear"
    )

    assert result.engine == "cosyvoice2"
    assert result.voice == "bigbear"
    assert [voice for _text, voice in engine.calls] == ["bigbear", "bigbear"]
    assert result.warnings == ()


def test_a_voice_this_engine_cannot_speak_falls_back(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要的音色这一档念不出来 ⇒ 退回兜底音色 + 一句人话（而不是每句失败 3 次）。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear", "littlebear")))

    result = synth_module.synthesize_script(
        "就一句。", paths=tmp_paths, task_id="t2", voice="Microsoft Huihui Desktop"
    )

    assert result.voice == "bigbear"
    assert [voice for _text, voice in engine.calls] == ["bigbear"]
    assert result.warnings, "换了音色就得留下说明：库里写着换成功、听起来却是另一个嗓子"
    assert "Microsoft Huihui Desktop" in result.warnings[0]
    assert "cosyvoice2" in result.warnings[0]


def test_the_system_voice_pack_is_used_when_the_service_is_down(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine(name="sapi", revision="win-sapi")
    _patch_picker(monkeypatch, FakePicker(engine, "Microsoft Huihui Desktop", ("Microsoft Huihui Desktop",)))

    result = synth_module.synthesize_script("就一句。", paths=tmp_paths, task_id="t3")

    assert result.engine == "sapi"
    assert result.voice == "Microsoft Huihui Desktop"


def test_given_sentences_are_not_split_again(tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 真机坑：55 句的稿子在这里被重切成 58 句 ⇒ 字幕 / 时间轴与音频错位。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))
    long_line = "这一句很长很长很长很长很长很长很长很长很长很长。"  # 远超 28 字的上限

    result = synth_module.synthesize_script(
        sentences=(long_line, "短句。"), paths=tmp_paths, task_id="t4", voice="bigbear"
    )

    assert result.sentences == (long_line, "短句。")
    assert [text for text, _voice in engine.calls] == [long_line, "短句。"]


def test_only_text_given_still_falls_back_to_the_splitter(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 直接给文案那条路：没有句子边界，只能按 28 字的上限切一遍（老口径）。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))
    long_text = "甲乙丙丁戊己庚辛壬癸。" * 4  # 44 字 ⇒ 切分器必然切不止一段

    result = synth_module.synthesize_script(long_text, paths=tmp_paths, task_id="t5", voice="bigbear")

    assert len(result.sentences) > 1


def test_sentences_already_on_disk_are_not_synthesized_again(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断点续传：盘上已有非空产物 ⇒ **一次引擎调用都不发生**。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))
    _write_wav(tmp_paths.sentence_wav("t6", 1), 700)

    result = synth_module.synthesize_script(
        sentences=("第一句。", "第二句。"), paths=tmp_paths, task_id="t6", voice="bigbear"
    )

    assert result.reused == 1
    assert [text for text, _voice in engine.calls] == ["第二句。"]


def test_an_empty_script_is_refused(tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))

    with pytest.raises(StudioError):
        synth_module.synthesize_script("   ", paths=tmp_paths, task_id="t7")


def test_per_sentence_voices_beat_the_single_panel_voice(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 多角色稿子：逐句音色优先于面板那一格（渲染这条路按角色解析出来的）。

    真机 ``01M2Z9BP1CR70TBW0CJ05FQ12Z`` 是熊大 17 句 / 熊二 17 句 / 旁白 21 句 ——
    整篇套一个嗓子会把旁白也念成熊大，而且**不报错**（只是听起来不对）。
    """
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear", "littlebear")))

    result = synth_module.synthesize_script(
        sentences=("熊大说话。", "熊二说话。"),
        voices=("bigbear", "littlebear"),
        paths=tmp_paths,
        task_id="t8",
        voice="bigbear",
    )

    assert [voice for _text, voice in engine.calls] == ["bigbear", "littlebear"]
    assert result.sentence_voices == ("bigbear", "littlebear")
    assert result.voice == "bigbear+littlebear", "多角色时不能只写一个名字"
    assert result.warnings == (), "每一句都有自己的嗓子时，不该报『面板那个音色被退回』"


def test_a_speaker_without_a_voice_falls_back_to_the_panel_one(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐句里那个 ``None`` = 这个角色没配音色 ⇒ 用面板那一格，而不是"没声音"。"""
    engine = FakeEngine()
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear", "littlebear")))

    result = synth_module.synthesize_script(
        sentences=("有角色的。", "没角色的。"),
        voices=("littlebear", None),
        paths=tmp_paths,
        task_id="t9",
        voice="bigbear",
    )

    assert [voice for _text, voice in engine.calls] == ["littlebear", "bigbear"]
    assert result.voice == "littlebear+bigbear"


class FlakyEngine(FakeEngine):
    """前 ``fail_times`` 次抛 ``TTS_SILENT``，之后正常 —— 真机那次抽风就长这样。"""

    def __init__(self, *, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.attempts = 0

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            # 坏音频**已经落在交付路径上**（引擎是直接写 out_path 的）
            _write_wav(out_path, 500)
            raise StudioError("合成产物没过音频 QC：TTS_SILENT", code=ErrorCode.TTS_SILENT)
        super().synthesize(text, out_path, voice=voice, rate=rate)


def test_a_flaky_sentence_is_retried(tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 真机 2026-09-22：同一句第一次回来 RMS −51.3 dBFS，第二次就正常。

    没有重试的话，55 句的长稿只要**任何一句**赶上一次抽风，整支片子的渲染就白跑。
    """
    engine = FlakyEngine(fail_times=1)
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))

    result = synth_module.synthesize_script(
        sentences=("抽风的一句。",), paths=tmp_paths, task_id="t10", voice="bigbear"
    )

    assert result.sentences == ("抽风的一句。",)
    assert engine.attempts == 2, "抽风一次就该再来一次"


def test_a_sentence_that_never_succeeds_leaves_no_bad_audio_behind(
    tmp_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """念不出来的那一句**不许**在交付路径上留半成品。

    留着它，下一次重跑会把那段坏音频当成「这一句已经好了」跳过 —— 静音被永久焊进母带，
    而每一步日志都写着成功（这正是"已存在且非空就跳过"那条断点续传的阴暗面）。
    """
    engine = FlakyEngine(fail_times=99)
    _patch_picker(monkeypatch, FakePicker(engine, "bigbear", ("bigbear",)))

    with pytest.raises(StudioError) as info:
        synth_module.synthesize_script(
            sentences=("念不出来的一句。",), paths=tmp_paths, task_id="t11", voice="bigbear"
        )

    assert str(info.value.code) == ErrorCode.TTS_SILENT.value
    assert engine.attempts == synth_module.SENTENCE_ATTEMPTS, "重试次数有上限，不能无限重试"
    assert not tmp_paths.sentence_wav("t11", 1).exists()
