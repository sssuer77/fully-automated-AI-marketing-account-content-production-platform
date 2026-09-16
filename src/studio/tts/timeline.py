"""时长时间轴：逐句实测 → 停顿 → ``timeline.json``（T2.7 · §04.2.7）。

时间轴要回答的唯一问题
----------------------
"第 7 句在第 12.4 秒到第 15.6 秒之间"—— 字幕（T3.5）与二期的场景切分都只认这句话。
所以这里产出的每一个毫秒都必须是**量出来的**：

```text
逐句 ffprobe 实测（量的是盘上那一份音频，不是引擎说的时长）
      │  + pause_after_ms（库里的表演参数 + 由种子派生的 ±80ms 抖动）
      ├─ 累加 start_ms / end_ms（单调、不重叠）
      └─ 末尾 tail_ms ⇒ total_ms（**成片**时长基准）
             │
             ├─ voice_master.wav（apad 把停顿**真的**填进音频里）
             └─ timeline.json（字幕计时 / 面板诊断）
```

为什么重跑时**全量重算**（陷阱 #26）
-----------------------------------
增量拼接的诱惑是"只改了一句，前面那些 ``start_ms`` 不用动"—— 错位就是这么来的：
第 3 句重念之后短了 200ms，从第 4 句起每一句的 ``start_ms`` 都该往前挪 200ms，
字幕却还按旧值画。全量重算的代价是 N 次 ``ffprobe``（每次几毫秒），
而增量拼接的代价是"整条片子的字幕从某一句起全体偏移"。

``total_ms`` 含 ``tail_ms``，母带**不含**
----------------------------------------
两个数不是一回事，写在这里免得下一个人"顺手对齐"：``total_ms`` 是**成片**时长基准
（§04.2.7 / §04.2.4.2：场景时长 = Σ句实测 + Σ停顿 + tail），而 ``voice_master.wav``
只装**句子 + 句间停顿** —— 尾部那一段由合成那一步用
``-t = ffprobe(voice_master) + tail_ms`` 补上（§04.2.8.5）。把 tail 也塞进母带，
成片就会比预期长出一个 tail（两处各加一次）。

为什么不写 ``scenes[]`` 与 ``loudness``
---------------------------------------
§04.2.7 的示例里有这两块，但它们是**二期**的：一期单遍合成没有场景概念，
而响度要到混音（T3.6）之后才有实测值。凭空写一个空数组只会让下游以为
"场景算出来是空的"（与 ``services/render_service.write_timeline`` 同一条口径）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.media import ffmpeg_binary, probe_media, run_command
from studio.core.paths import data_relative

__all__ = [
    "MASTER_DRIFT_TOLERANCE_MS",
    "MASTER_TIMEOUT_SEC",
    "PAUSE_JITTER_MS",
    "TIMELINE_SCHEMA_VERSION",
    "VOICE_MASTER_CHANNELS",
    "VOICE_MASTER_SAMPLE_RATE",
    "Timeline",
    "TimelineSentence",
    "TimelineSource",
    "assemble_voice_master",
    "build_timeline",
    "has_audio",
    "measure_master_ms",
    "pause_after_ms",
    "probe_sentence_ms",
    "write_timeline",
]

logger = get_logger("studio.tts.timeline")

#: ``timeline.json`` 的结构版本（§04.2.7）。
TIMELINE_SCHEMA_VERSION: Final[str] = "1.0"

#: 句间停顿的抖动幅度（±毫秒，§04.2.7 第 2 步）。
#:
#: 为什么要抖：每句都停同一个 200ms，整条音轨会带上节拍器式的机器特征 ——
#: 而停顿恰恰是"人念的还是机器念的"最容易被听出来的地方。
PAUSE_JITTER_MS: Final[int] = 80

#: 母带的采样率 / 声道（与 ``config/outputs.yaml`` 的 profile 对齐；同 ``tts/synth``）。
VOICE_MASTER_SAMPLE_RATE: Final[int] = 48_000
VOICE_MASTER_CHANNELS: Final[int] = 1

#: 拼接超时（纯音频拼接，秒级；给 5 分钟足够长音轨）。
MASTER_TIMEOUT_SEC: Final[int] = 300

#: 母带实测时长与时间轴的对账容差（§04.2.7 的验收线：30ms）。
#:
#: 超线只记 ``warn``、不阻断：逐句 ``ffprobe`` 各自四舍五入到毫秒，N 句叠起来最坏
#: 就是 0.5×N 毫秒（60 句 = 30ms）—— 为一个四舍五入的账把整条片子拦下来，与
#: C12"诊断不阻断"同一条口径。真正该拦的（句子音频缺失 / 长度为 0）在别处拦。
MASTER_DRIFT_TOLERANCE_MS: Final[int] = 30


def pause_after_ms(base_ms: int, *, seed: str, seq: int) -> int:
    """这一句之后停多久：``base + jitter``（±80ms，由 ``seed`` 派生 ⇒ 可复现）。

    用 ``blake2s`` 而不是 ``random``：同一个任务重跑（改稿 / 二次渲染 / 换台机器）
    必须得到**逐毫秒一致**的时间轴 —— 否则"上一版字幕对得上、这一版对不上"就成了
    一件没法解释的事（与 ``db/lease.py::jitter_ms`` 同一条理由）。
    种子里带上 ``seq`` ⇒ 每句的抖动互不相同，而同一句每次相同。

    ``base_ms`` 为 0（"紧接下一句"）时抖动被夹回 0：停顿是**用户设的**，
    抖动只负责让"设了 200ms"听起来不像节拍器，不该凭空造出 80ms 的静音。
    """
    if base_ms <= 0:
        # "紧接下一句"就是紧接：抖动只负责让**设了的**停顿听起来不像节拍器，
        # 不该凭空造出一段静音。夹在最后（而不是夹 ``base + offset``）是因为
        # 那样在 base 很小时会退化成"只加不减"，节奏反而更整齐。
        return 0
    digest = hashlib.blake2s(f"{seed}:pause:{seq}".encode(), digest_size=8).digest()
    offset = int.from_bytes(digest, "big") % (2 * PAUSE_JITTER_MS + 1) - PAUSE_JITTER_MS
    return max(0, base_ms + offset)


@dataclass(frozen=True, slots=True)
class TimelineSource:
    """拼时间轴要的**一样东西**：这一句的音频、它有多长、后面停多久。

    ``duration_ms`` 必须由调用方量好（:func:`probe_sentence_ms`）—— 本模块不替它猜，
    也不信任 ``script_sentences.tts_duration_ms``：那一列记的是"合成那一刻量到的"，
    而时间轴要描述的是"这次真的拼进去的那份音频"。
    """

    sentence_id: str
    seq: int
    speaker: str
    text: str
    audio: Path
    duration_ms: int
    base_pause_ms: int


@dataclass(frozen=True, slots=True)
class TimelineSentence:
    """时间轴里的一句（:meth:`to_dict` 就是 ``timeline.json`` 里那一行）。

    ``audio`` 与 ``audio_path`` 是同一份文件的两个视角，别合并：
    ``audio`` 是**给人和给下游看**的那一份（相对 ``data/``，能跟着目录搬家），
    ``audio_path`` 是**给 ffmpeg 用**的那一份（绝对路径，拼母带时直接当 ``-i``）。
    """

    sentence_id: str
    seq: int
    speaker: str
    text: str
    audio: str
    audio_path: Path
    start_ms: int
    end_ms: int
    duration_ms: int
    pause_after_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.sentence_id,
            "seq": self.seq,
            "speaker": self.speaker,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "pause_after_ms": self.pause_after_ms,
            "audio": self.audio,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class Timeline:
    """整条时间轴（``total_ms`` 是**成片**时长基准，见模块 docstring）。"""

    task_id: str
    seed: str
    sample_rate: int
    channels: int
    tail_ms: int
    total_ms: int
    voice_master: str
    sentences: tuple[TimelineSentence, ...]

    @property
    def master_ms(self) -> int:
        """母带**应有**的时长 = ``total_ms - tail_ms``（尾部留白不在母带里）。"""
        return self.total_ms - self.tail_ms

    @property
    def spoken_ms(self) -> int:
        """句子本体合计（Σ句实测），不含任何停顿。"""
        return sum(sentence.duration_ms for sentence in self.sentences)

    @property
    def pause_ms(self) -> int:
        """句间停顿合计（含最后一句之后的那一次）。"""
        return sum(sentence.pause_after_ms for sentence in self.sentences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "task_id": self.task_id,
            "seed": self.seed,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "total_ms": self.total_ms,
            "tail_ms": self.tail_ms,
            "voice_master": self.voice_master,
            "sentences": [sentence.to_dict() for sentence in self.sentences],
        }


def build_timeline(
    sources: Sequence[TimelineSource],
    *,
    task_id: str,
    seed: str,
    tail_ms: int,
    data_dir: Path,
    voice_master: Path,
    sample_rate: int = VOICE_MASTER_SAMPLE_RATE,
    channels: int = VOICE_MASTER_CHANNELS,
) -> Timeline:
    """按 ``seq`` 累加出时间轴（纯计算，**不碰盘**）。

    累加规则（§04.2.7 第 3 步）：

    - ``end_ms[i] = start_ms[i] + duration_ms[i]``
    - ``start_ms[i+1] = end_ms[i] + pause_after_ms[i]`` ⇒ 单调、不重叠
    - ``total_ms = end_ms[-1] + pause_after_ms[-1] + tail_ms``

    最后一句之后的停顿照样算进去：它是这一句的**表演参数**（"留一口气"），
    而 ``tail_ms`` 是**成片**的收尾留白（"最后一字别被硬切"），两件事，不互相顶替。

    调用方必须按 ``seq`` 传全（一句话都不许少）—— "少一句"在这里的表现是
    "整条时间轴比母带短"，而它不会报错，只会让字幕从某一句起全体提前。
    所以 ``seq`` 不连续时抛错（宁可现在吵，不要出片之后才发现）。
    """
    ordered = sorted(sources, key=lambda item: item.seq)
    if not ordered:
        raise StudioError(
            "这个任务没有任何句子，时间轴无从算起",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id},
            remediation="先落一版稿件（studio script draft）再配音",
        )
    if tail_ms < 0:
        raise ValueError("tail_ms 不得为负")
    expected = list(range(1, len(ordered) + 1))
    if [item.seq for item in ordered] != expected:
        raise StudioError(
            f"句序不连续（期望 1..{len(ordered)}，实际 {[item.seq for item in ordered]}）",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"task_id": task_id},
            remediation="稿件与句子的对应关系被破坏 ⇒ 重新落一版稿件",
        )

    entries: list[TimelineSentence] = []
    cursor = 0
    for source in ordered:
        if source.duration_ms <= 0:
            raise StudioError(
                f"第 {source.seq} 句的音频量不出时长（{source.duration_ms}ms）",
                code=ErrorCode.TTS_AUDIO_QC_FAILED,
                context={"sentence_id": source.sentence_id, "audio": source.audio.as_posix()},
                remediation="这一句的 WAV 是坏的或空的 ⇒ 删掉它重跑配音池（会重念这一句）",
            )
        pause = pause_after_ms(source.base_pause_ms, seed=seed, seq=source.seq)
        entries.append(
            TimelineSentence(
                sentence_id=source.sentence_id,
                seq=source.seq,
                speaker=source.speaker,
                text=source.text,
                audio=data_relative(source.audio, data_dir),
                audio_path=source.audio,
                start_ms=cursor,
                end_ms=cursor + source.duration_ms,
                duration_ms=source.duration_ms,
                pause_after_ms=pause,
            )
        )
        cursor += source.duration_ms + pause

    return Timeline(
        task_id=task_id,
        seed=seed,
        sample_rate=sample_rate,
        channels=channels,
        tail_ms=tail_ms,
        total_ms=cursor + tail_ms,
        voice_master=data_relative(voice_master, data_dir),
        sentences=tuple(entries),
    )


def has_audio(path: Path) -> bool:
    """盘上有一份**非空**的音频。空文件只可能来自"写到一半断电"，不算数。

    这条判据在三个地方要用（池 worker 的短路、本模块的实测、配音收口时挑哪一份），
    所以定义一次：三处各写一遍，早晚会有一处变成"存在就算"—— 而那会让整段片子
    交给一份半截音频。
    """
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def probe_sentence_ms(path: Path, *, seq: int) -> int:
    """量一句的**实测**时长（毫秒）。量不出来 ⇒ 抛，**不许估**。

    为什么不直接信 ``script_sentences.tts_duration_ms``：那一列记的是"合成那一刻
    量到的"，而时间轴要描述的是"这次真的拼进去的那份音频"。两者不一致只有两种可能
    —— 文件被换过，或者写库时出了错 —— 两种都该以盘为准。
    按字数估时长是更坏的做法：数字念得慢、英文念得快，字幕会从某一句起整体偏移。
    """
    target = Path(path)
    if not has_audio(target):
        raise StudioError(
            f"第 {seq} 句的音频不在盘上：{target}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"seq": seq, "audio": target.as_posix()},
            remediation="这一句的产物被清理掉了（§03.7.5 的 24 小时 GC）⇒ 重跑配音池，它会重念这一句",
        )
    return max(0, probe_media(target).duration_ms)


def _master_filter(timeline: Timeline) -> str:
    """拼接滤镜图：每句统一到目标格式，再用 ``apad`` 把停顿**填进音频**里。

    为什么停顿必须进音频，而不是"在时间轴上记一笔"：字幕与画面都按时间轴对齐，
    观众听到的却是母带 —— 母带里没有那段静音，第 5 句就会比字幕早到 420ms。
    为什么不用每个停顿塞一个 ``anullsrc`` 输入：60 句就是 60 个额外输入与 60 条
    滤镜链，而 ``apad`` 一个参数就够，且它填的正是这一句自己的尾部。

    ``asetpts=PTS-STARTPTS`` 是 ``concat`` 的硬要求：它按各输入的 PTS 顺序拼，
    而 SAPI 出来的 WAV 首帧 PTS 未必是 0。
    """
    layout = "mono" if timeline.channels == 1 else "stereo"
    fmt = f"aformat=sample_fmts=s16:sample_rates={timeline.sample_rate}:channel_layouts={layout}"
    chains: list[str] = []
    for index, sentence in enumerate(timeline.sentences):
        pad = f",apad=pad_dur={sentence.pause_after_ms / 1000:.3f}" if sentence.pause_after_ms > 0 else ""
        chains.append(f"[{index}:a]{fmt},asetpts=PTS-STARTPTS{pad}[a{index}]")
    if len(timeline.sentences) == 1:
        # 单句脚本：``concat`` 在这里没有意义（还可能因为 n=1 报参数错），直接出图。
        return chains[0].replace("[a0]", "[aout]")
    joined = "".join(f"[a{index}]" for index in range(len(timeline.sentences)))
    chains.append(f"{joined}concat=n={len(timeline.sentences)}:v=0:a=1[aout]")
    return ";".join(chains)


def assemble_voice_master(
    timeline: Timeline,
    out_path: Path,
    *,
    timeout: int = MASTER_TIMEOUT_SEC,
) -> Path:
    """把逐句 WAV 拼成 ``voice_master.wav``（一次 ffmpeg 调用，含句间停顿）。

    先写 ``.partial.wav`` 再改名：中途失败留下半个母带，比留下一个**看起来完整**的
    母带更危险 —— 下游（合成 / 字幕）只看"文件在不在、非不非空"。
    """
    if not timeline.sentences:
        raise StudioError(
            "没有可拼接的句子音频",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"task_id": timeline.task_id},
            remediation="确认稿件里有句子（切分后为空说明正文是空的）",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(f"{out_path.stem}.partial{out_path.suffix}")
    if partial.exists():
        partial.unlink()

    argv = [ffmpeg_binary(), "-hide_banner", "-nostats", "-y"]
    for sentence in timeline.sentences:
        argv += ["-i", str(sentence.audio_path)]
    argv += [
        "-filter_complex",
        _master_filter(timeline),
        "-map",
        "[aout]",
        "-ar",
        str(timeline.sample_rate),
        "-ac",
        str(timeline.channels),
        "-c:a",
        "pcm_s16le",
        str(partial),
    ]

    result = run_command(argv, timeout=timeout)
    if not result.ok or not partial.is_file():
        if partial.exists():
            partial.unlink()
        raise StudioError(
            f"人声母带拼接失败（rc={result.returncode}）：{result.tail()}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"task_id": timeline.task_id, "stderr": result.tail()},
            remediation="确认 ffmpeg 可用，且逐句 WAV 都是完整文件",
        )
    partial.replace(out_path)
    return out_path


def measure_master_ms(timeline: Timeline, master: Path) -> int:
    """量一遍母带并与时间轴对账（超线只 ``warn``，见 :data:`MASTER_DRIFT_TOLERANCE_MS`）。

    返回实测毫秒 —— 它要进 ``artifacts.meta_json``：面板据此说得出"这一版母带多长"，
    而不用自己去 ffprobe 一遍。
    """
    measured = probe_media(master).duration_ms
    drift = measured - timeline.master_ms
    if abs(drift) > MASTER_DRIFT_TOLERANCE_MS:
        logger.warning(
            "tts.timeline_drift",
            task_id=timeline.task_id,
            measured_ms=measured,
            expected_ms=timeline.master_ms,
            drift_ms=drift,
        )
    return measured


def write_timeline(timeline: Timeline, path: Path) -> Path:
    """写 ``timeline.json``（原子替换：读者要么看到上一版，要么看到这一版）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.stem}.partial{path.suffix}")
    partial.write_text(
        json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)
    return path
