"""配音合成：口播文本 → 逐句 WAV → 拼接成 ``voice_master.wav``（T2.6 的可跑通版本）。

三步，各自都有明确的失败口径
----------------------------
```text
切句：稿件逐句（渲染这条路）或 split_for_tts(文本)（只给了文本时）
      │
      ├─ synthesize(片段) → s001.wav  逐句落盘：可续传、可单句重试
      │                                （没变的话走缓存复制，变了就真的重念）
      └─ concat → voice_master.wav    拼接 + 统一到 48kHz 单声道
```

为什么逐句落盘而不是整段一次合成
--------------------------------
① 断点续传：跑到第 30 句挂了，重跑从第 30 句接着来 —— 前 29 句**命中缓存**
   （键里含引擎、音色、参考音指纹与文本），是一次文件复制，不必再合成一遍；
② 单句重试：某句读错了只重合成那一句，而不是整条音轨；
③ 对齐的余地：将来要做"句 ↔ 镜头"对齐时，句子边界就是现成的锚点。

``voice_master.wav`` 统一成 **48 kHz 单声道 PCM**：SAPI 出的是 22.05 kHz，
而 profile 的音频编码参数写的是 48 kHz —— 在这里统一，下游就只剩一次采样率转换
（编码那一步），而不是"每句一个采样率"。

引擎判据只有一份（2026-09-21）
----------------------------
这里原先**直连 SAPI**，而渲染面板的音色下拉来自**常驻引擎**（``GET /api/v1/voices``）——
选了 ``bigbear`` 就 ``SelectVoice`` 抛、每句失败 3 次（真机 ``r0001`` 挂在 55/58）。
判据现在与配音池共用一份（:mod:`studio.tts.engine_picker`），逐句合成也改走
:func:`~studio.tts.sentence.synthesize_sentence`（归一化 + 缓存 + 音频 QC）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.media import ffmpeg_binary, probe_media, run_command
from studio.core.paths import StudioPaths
from studio.tts.cache import TtsCache
from studio.tts.engine_picker import EnginePicker, pick_speakable_voice
from studio.tts.fallback import DEGRADE_AFTER_ATTEMPTS
from studio.tts.refprint import voice_fingerprint
from studio.tts.sapi import DEFAULT_RATE
from studio.tts.segmenter import split_for_tts
from studio.tts.sentence import SentenceEngine, SentenceSynthesis, synthesize_sentence
from studio.tts.text_normalize import Glossary, GlossaryStore

__all__ = [
    "CONCAT_TIMEOUT_SEC",
    "VOICE_MASTER_CHANNELS",
    "VOICE_MASTER_SAMPLE_RATE",
    "VoiceResult",
    "concat_wavs",
    "synthesize_script",
]

#: 人声母带的采样率 / 声道（与 `config/outputs.yaml` 的 profile 对齐）。
logger = get_logger("studio.tts.synth")

VOICE_MASTER_SAMPLE_RATE: Final[int] = 48_000
VOICE_MASTER_CHANNELS: Final[int] = 1

#: 拼接的超时（纯音频拼接，秒级；给 5 分钟足够长音轨）。
CONCAT_TIMEOUT_SEC: Final[int] = 300

#: 一句念不出来时**这一条路**重试几次（与 §04.3.3 的 ``DEGRADE_AFTER_ATTEMPTS`` 同数）。
#:
#: 为什么这条路也要重试（真机 2026-09-22）
#: -------------------------------------
#: 同一句「熊二嘴张着，半天没蹦出一个字。」第一次回来 RMS −51.3 dBFS
#: （``TTS_SILENT``），紧接着的第二次就正常出 12 秒音频 —— 这是**抽风**，不是坏文本。
#: 没有重试的话，55 句的长稿只要**任何一句**赶上一次抽风，整支片子的渲染就白跑。
SENTENCE_ATTEMPTS: Final[int] = DEGRADE_AFTER_ATTEMPTS

#: 这条路**愿意重试**的错误码：抽风型（同一句下一次多半能成）。
#:
#: 与决策表的分工：表管「该换招了没有」（去 emotion / 换引擎 / 占位），这里只管
#: 「再来一次」。**明确的配置错误不在里面**（音色没给、这台引擎念不出来）——
#: 重试只会把一次说得清的失败变成三次，而每次都要等十几秒。
RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {
        ErrorCode.TTS_SILENT.value,
        ErrorCode.TTS_CLIP.value,
        ErrorCode.TTS_SENTENCE_FAILED.value,
        ErrorCode.TTS_TIMEOUT.value,
    }
)


@dataclass(frozen=True, slots=True)
class VoiceResult:
    """一次配音的结论（进 ``manifest.json`` 与面板）。

    ``sentence_durations_ms`` 是**逐句实测**时长，由 :func:`synthesize_script` 在
    合成每一句之后立刻 ``ffprobe`` 得到。它是字幕计时的**唯一**来源（§04.2.6 的
    "时间只取自 voice_master 实测的句级时长"）—— 按字数估时长，遇上"数字读得慢、
    英文读得快"就会让字幕比人声早半秒，而这种错位在成片里极其显眼。
    """

    task_id: str
    engine: str
    voice: str | None
    sentences: tuple[str, ...]
    sentence_files: tuple[Path, ...]
    sentence_durations_ms: tuple[int, ...]
    voice_master: Path
    duration_ms: int
    #: 这次**一句引擎都没碰**的句数（缓存命中）。它**不是**「盘上有文件就跳过」
    #: —— 那个判据分不清那份文件是谁念的（见 ``synthesize_script`` 里的说明）。
    reused: int = 0
    #: 逐句音色（与 :attr:`sentences` 同序）。多角色稿子时与 :attr:`voice` 不是一回事
    sentence_voices: tuple[str | None, ...] = ()
    #: 这次配音**自己**产生的说明（音色退回之类）。面板与 ``manifest.json`` 直接显示它：
    #: 库里写着换音色成功、听起来却是另一个嗓子时，这是唯一的线索。
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "engine": self.engine,
            "voice": self.voice,
            "sentence_voices": list(self.sentence_voices),
            "sentence_count": len(self.sentences),
            "reused": self.reused,
            "warnings": list(self.warnings),
            "sentences": list(self.sentences),
            "sentence_files": [path.as_posix() for path in self.sentence_files],
            "sentence_durations_ms": list(self.sentence_durations_ms),
            "voice_master": self.voice_master.as_posix(),
            "duration_ms": self.duration_ms,
        }


def _concat_list_body(files: Sequence[Path]) -> str:
    """ffmpeg concat demuxer 的清单（``file '...'`` 每行一条）。

    两个细节都是**正确性**问题，不该依赖"输入恰好干净"：

    1. 路径里的单引号按 demuxer 的规矩转义成 ``'\''`` —— 文件名是我们自己生成的
       （``s001.wav`` / 任务 id），正常不会出现引号，但转义不花什么代价。
    2. 路径必须**绝对**。demuxer 把清单里的相对路径按**清单文件所在目录**解析，
       而清单在 ``data/work/<task>/tts/`` 下、句子音频在 ``data/output/voice/`` 下 ——
       ``STUDIO_HOME`` 一旦是相对路径（``studio serve`` 从仓库根跑就是这样），
       拼出来的路径会重复一截，报错是 ``Impossible to open .../tts/data/tmp/.../s001.wav``。
       绝对路径把这条隐式规则整个绕开。
    """
    lines: list[str] = []
    for path in files:
        escaped = path.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def concat_wavs(
    files: Sequence[Path],
    out_path: Path,
    *,
    sample_rate: int = VOICE_MASTER_SAMPLE_RATE,
    channels: int = VOICE_MASTER_CHANNELS,
    timeout: int = CONCAT_TIMEOUT_SEC,
) -> Path:
    """把逐句 WAV 拼成一条人声母带（``-c copy`` 不行 —— 这里要顺带统一采样率）。"""
    if not files:
        raise StudioError(
            "没有可拼接的句子音频",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"out_path": out_path.as_posix()},
            remediation="确认切分这一步产出了至少一句（split_for_tts 返回空列表说明文本是空的）",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(_concat_list_body(files), encoding="utf-8")

    partial = out_path.with_name(f"{out_path.stem}.partial{out_path.suffix}")
    if partial.exists():
        partial.unlink()

    result = run_command(
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "pcm_s16le",
            str(partial),
        ],
        timeout=timeout,
    )

    if not result.ok or not partial.is_file():
        if partial.exists():
            partial.unlink()
        raise StudioError(
            f"人声拼接失败（rc={result.returncode}）：{result.tail()}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"files": [p.as_posix() for p in files], "stderr": result.tail()},
            remediation="确认 ffmpeg 可用，且逐句 WAV 都是完整文件",
        )

    partial.replace(out_path)
    return out_path


def synthesize_script(
    text: str = "",
    *,
    sentences: Sequence[str] | None = None,
    paths: StudioPaths,
    task_id: str,
    voice: str | None = None,
    voices: Sequence[str | None] | None = None,
    rate: int = DEFAULT_RATE,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> VoiceResult:
    """口播文本 → ``voice_master.wav``（配音这一环的**唯一**入口）。

    :param sentences: **已经切好的句子**（库里那一版稿件的逐句）。给了它就**不再切
        一遍**：渲染这条路读的是 ``script_sentences``，配音池读的也是它们 —— 在这里
        用 :func:`split_for_tts` 再切一次会得到另一个句数（真机：稿 55 句、切出 58
        句），字幕与时间轴从此与音频对不上。
    :param voices: **逐句**音色（与 ``sentences`` 同序；渲染这条路按角色解析出来的）。
        某一句是 ``None``（这个角色没配）⇒ 退回 ``voice``。条目必须是**已解析**的
        音色名 —— 「这台引擎念不念得出来」由调用方按配音池那一份判据先判完。
    :param rate: SAPI 量纲的语速（-10..10）。常驻引擎要的是倍率，见 :func:`_speed_for_rate`。
    :param on_progress: ``(第几句, 共几句, 这一句的文本)`` —— 长稿子要让人看到进度，
        否则面板上就是转圈两分钟，而人不知道它是在干活还是卡住了。
    """
    resolved = (
        tuple(part for part in (raw.strip() for raw in sentences) if part)
        if sentences is not None
        else tuple(split_for_tts(text))
    )
    if not resolved:
        raise StudioError(
            "切分后没有可合成的句子",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"task_id": task_id, "chars": len(text), "given": len(sentences or ())},
            remediation="确认稿件正文非空（空白与纯标点会被切分器滤掉）",
        )

    # 引擎判据只有一份（`tts/engine_picker.py`）：配音池与这里问的是同一个问题。
    # 以前这里直连 SAPI，而面板的音色下拉来自常驻引擎 ⇒ 选 bigbear 就 SelectVoice 抛。
    picker = EnginePicker(paths)
    engine, default_voice, speakable = picker()
    chosen_voice, note = pick_speakable_voice(
        voice, speakable=speakable, default=default_voice, engine=engine.name
    )
    per_sentence = tuple(voices) if voices is not None else ()
    cache = TtsCache(paths.tts_cache_dir)
    glossary = GlossaryStore(paths.glossary_file).current()
    speed = _speed_for_rate(rate)
    total = len(resolved)
    files: list[Path] = []
    durations: list[int] = []
    reused = 0
    used_voices: list[str | None] = []

    # 参考音指纹**这一次配音里只算一遍**（哈希要读盘，而逐句重算是白读）。
    # 键用 ``str`` 而不是 ``str | None``：``None``（交给引擎自己挑）也要占一格。
    fingerprints: dict[str, str] = {}

    def fingerprint_for(voice_name: str | None) -> str:
        key = voice_name or ""
        if key not in fingerprints:
            fingerprints[key] = (
                "" if voice_name is None else voice_fingerprint(paths.voice_src_dir, voice_name)
            )
        return fingerprints[key]

    for index, sentence in enumerate(resolved, start=1):
        target = paths.sentence_wav(task_id, index)
        sentence_voice = _voice_for(per_sentence, index, chosen_voice)
        used_voices.append(sentence_voice)
        # 这里原先有一条「盘上有非空产物就跳过」的捷径（断点续传）。它被**删掉**了：
        # 它判的是「这个文件在不在」，而不是「它是不是**这一轮要念的东西**」—— 换了
        # 参考音（同名重传）或换了音色之后重跑，它会照旧把旧嗓子拼进母带，而每一步
        # 日志都写着成功。现在每一句都过一遍缓存：键里含引擎、音色、**参考音指纹**
        # 与文本 ⇒ 没变就是一次文件复制（毫秒级），变了就真的重念。
        synthesis = _synthesize_with_retries(
            sentence,
            out_path=target,
            cache=cache,
            engine=engine,
            voice=sentence_voice,
            voice_fingerprint=fingerprint_for(sentence_voice),
            speed=speed,
            glossary=glossary,
        )
        if synthesis.cache_hit:
            reused += 1
        files.append(target)
        # 逐句实测：字幕计时只认这个数（见 VoiceResult 的注释）。
        durations.append(max(0, probe_media(target).duration_ms))
        if on_progress is not None:
            on_progress(index, total, sentence)

    master = concat_wavs(files, paths.voice_master(task_id))
    info = probe_media(master)
    if info.duration_ms <= 0:
        raise StudioError(
            "人声母带时长为 0，配音这一步没有真正产出音频",
            code=ErrorCode.TTS_AUDIO_QC_FAILED,
            context={"voice_master": master.as_posix(), "sentences": total},
            remediation="逐句 WAV 可能是静音（音色没选对）；换一个音色重试",
        )

    return VoiceResult(
        task_id=task_id,
        engine=engine.name,
        voice=_voice_label(used_voices),
        sentences=resolved,
        sentence_files=tuple(files),
        sentence_durations_ms=tuple(durations),
        voice_master=master,
        duration_ms=info.duration_ms,
        reused=reused,
        sentence_voices=tuple(used_voices),
        # 「你要的音色被退回了」这句话只在**那个音色真的被用上**时才有意义：
        # 多角色稿子里每一句都有自己的嗓子时，替面板那一格报一句警告只会误导人。
        warnings=(note,) if note is not None and chosen_voice in used_voices else (),
    )


def _speed_for_rate(rate: int) -> float:
    """SAPI 量纲的语速（-10..10）⇒ 倍率（``sapi_rate_for`` 的反函数）。"""
    return 1.0 + max(-10, min(10, rate)) / 10.0


def _synthesize_with_retries(
    sentence: str,
    *,
    out_path: Path,
    cache: TtsCache,
    engine: SentenceEngine,
    voice: str | None,
    voice_fingerprint: str,
    speed: float,
    glossary: Glossary,
) -> SentenceSynthesis:
    """念这一句；抽风型失败就再来（最多 :data:`SENTENCE_ATTEMPTS` 次）。

    返回这一句的结论（调用方按 ``cache_hit`` 数「一句都没碰引擎」的句数）。

    **失败时把产物删掉**：``synthesize_sentence`` 是让引擎**直接写** ``out_path`` 的，
    所以没过 QC 的那一段坏音频就躺在交付路径上。留着它，下一次重跑会把它当成
    「这一句已经好了」跳过 —— 于是静音被**永久**焊进母带，而每一步日志都写着成功。
    """
    for attempt in range(1, SENTENCE_ATTEMPTS + 1):
        try:
            return synthesize_sentence(
                sentence,
                out_path=out_path,
                cache=cache,
                engine=engine,
                voice=voice,
                voice_fingerprint=voice_fingerprint,
                speed=speed,
                glossary=glossary,
            )
        except StudioError as exc:
            if str(exc.code) not in RETRYABLE_CODES or attempt == SENTENCE_ATTEMPTS:
                out_path.unlink(missing_ok=True)
                raise
            logger.warning(
                "tts.sentence_retry",
                attempt=attempt,
                code=str(exc.code),
                voice=voice,
                text=sentence[:40],
            )
    raise AssertionError("SENTENCE_ATTEMPTS 必须 ≥ 1（上面的循环一次都不跑）")


def _voice_for(voices: Sequence[str | None], index: int, fallback: str | None) -> str | None:
    """第 ``index`` 句用谁的嗓子：逐角色解析的结果优先，没配就退回这一次的音色。"""
    if index > len(voices):
        return fallback
    return voices[index - 1] or fallback


def _voice_label(voices: Sequence[str | None]) -> str | None:
    """这一支片子实际用了哪些嗓子（单一 ⇒ 那个名字；多个 ⇒ ``甲+乙``）。

    进 ``manifest.json`` 与面板。多角色时**不能**只写一个名字 —— 那正是
    「库里写着熊大、听起来是熊二」这类悬案的开头。
    """
    distinct = tuple(dict.fromkeys(voice for voice in voices if voice))
    if not distinct:
        return None
    return distinct[0] if len(distinct) == 1 else "+".join(distinct)
