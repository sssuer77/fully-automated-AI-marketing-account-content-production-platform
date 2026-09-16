"""配音合成：口播文本 → 逐句 WAV → 拼接成 ``voice_master.wav``（T2.6 的可跑通版本）。

三步，各自都有明确的失败口径
----------------------------
```text
split_for_tts(文本)                切成"一次合成"的片段（≤28 字，T2.5 的口径）
      │
      ├─ synthesize(片段) → s001.wav  逐句落盘：可续传、可单句重试
      │                                （已存在且非空的句子**跳过**，省掉重跑）
      └─ concat → voice_master.wav    拼接 + 统一到 48kHz 单声道
```

为什么逐句落盘而不是整段一次合成
--------------------------------
① 断点续传：跑到第 30 句挂了，重跑从第 30 句接着来，不必把前 29 句再合成一遍；
② 单句重试：某句读错了只重合成那一句，而不是整条音轨；
③ 对齐的余地：将来要做"句 ↔ 镜头"对齐时，句子边界就是现成的锚点。

``voice_master.wav`` 统一成 **48 kHz 单声道 PCM**：SAPI 出的是 22.05 kHz，
而 profile 的音频编码参数写的是 48 kHz —— 在这里统一，下游就只剩一次采样率转换
（编码那一步），而不是"每句一个采样率"。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.media import ffmpeg_binary, probe_media, run_command
from studio.core.paths import StudioPaths
from studio.tts.sapi import DEFAULT_RATE, pick_voice, synthesize
from studio.tts.segmenter import split_for_tts

__all__ = [
    "CONCAT_TIMEOUT_SEC",
    "VOICE_MASTER_CHANNELS",
    "VOICE_MASTER_SAMPLE_RATE",
    "VoiceResult",
    "concat_wavs",
    "synthesize_script",
]

#: 人声母带的采样率 / 声道（与 `config/outputs.yaml` 的 profile 对齐）。
VOICE_MASTER_SAMPLE_RATE: Final[int] = 48_000
VOICE_MASTER_CHANNELS: Final[int] = 1

#: 拼接的超时（纯音频拼接，秒级；给 5 分钟足够长音轨）。
CONCAT_TIMEOUT_SEC: Final[int] = 300


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
    reused: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "engine": self.engine,
            "voice": self.voice,
            "sentence_count": len(self.sentences),
            "reused": self.reused,
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
    text: str,
    *,
    paths: StudioPaths,
    task_id: str,
    voice: str | None = None,
    rate: int = DEFAULT_RATE,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> VoiceResult:
    """口播文本 → ``voice_master.wav``（配音这一环的**唯一**入口）。

    :param on_progress: ``(第几句, 共几句, 这一句的文本)`` —— 长稿子要让人看到进度，
        否则面板上就是"转圈两分钟"，而人不知道它是在干活还是卡住了。
    """
    sentences = split_for_tts(text)
    if not sentences:
        raise StudioError(
            "切分后没有可合成的句子",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"task_id": task_id, "chars": len(text)},
            remediation="确认稿件正文非空（空白与纯标点会被切分器滤掉）",
        )

    chosen_voice = voice or pick_voice()
    total = len(sentences)
    files: list[Path] = []
    durations: list[int] = []
    reused = 0

    for index, sentence in enumerate(sentences, start=1):
        target = paths.sentence_wav(task_id, index)
        # 断点续传：已经有非空的产物就跳过（重跑不重合成）。
        if target.is_file() and target.stat().st_size > 0:
            reused += 1
        else:
            synthesize(sentence, target, voice=chosen_voice, rate=rate)
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
        engine="sapi",
        voice=chosen_voice,
        sentences=tuple(sentences),
        sentence_files=tuple(files),
        sentence_durations_ms=tuple(durations),
        voice_master=master,
        duration_ms=info.duration_ms,
        reused=reused,
    )
