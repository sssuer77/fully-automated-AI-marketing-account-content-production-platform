"""时长时间轴：纯计算那一部分（T2.7 · §04.2.7）。

这一层只钉**算得对不对**：停顿抖动可复现、累加不重不漏、边界（空稿 / 句序断档 /
零长度音频 / 负 tail）。"母带与时间轴对不对得上"是另一件事 —— 它要跑真的 ffmpeg，
归 ``tests/integration/test_timeline.py``：**算错了这里红，拼错了那里红**。

为什么不给 ``build_timeline`` 喂"已经算好的 start_ms"
----------------------------------------------------
那样测的是"我会不会加法"。这里喂的是**输入侧**的三样东西（句序 / 实测时长 /
基础停顿），断言的是**输出侧**的不变量（单调、不重叠、总和等于三部分之和）——
把公式抄进断言里固然也是抄，但它抄的是契约（§04.2.7 第 3 步），
而不是被测代码的实现。
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.tts.timeline import (
    PAUSE_JITTER_MS,
    Timeline,
    TimelineSource,
    build_timeline,
    has_audio,
    pause_after_ms,
    probe_sentence_ms,
    write_timeline,
)

TASK = "01TESTTASK0000000000000000"
TAIL_MS = 600


def _source(data_dir: Path, seq: int, duration_ms: int, *, pause_ms: int = 200) -> TimelineSource:
    return TimelineSource(
        sentence_id=f"{TASK}-s{seq:03d}",
        seq=seq,
        speaker="bigbear",
        text=f"第{seq}句",
        audio=data_dir / "output" / "voice" / TASK / f"s{seq:03d}.wav",
        duration_ms=duration_ms,
        base_pause_ms=pause_ms,
    )


def _build(
    data_dir: Path,
    durations: tuple[int, ...] = (3200, 2800, 1500),
    *,
    pauses: tuple[int, ...] | None = None,
    tail_ms: int = TAIL_MS,
) -> Timeline:
    """三句的基准时间轴（句序 / 时长 / 停顿都可以逐用例改）。"""
    base = (200,) * len(durations) if pauses is None else pauses
    sources = [
        _source(data_dir, seq, duration_ms, pause_ms=pause)
        for seq, (duration_ms, pause) in enumerate(zip(durations, base, strict=True), start=1)
    ]
    return build_timeline(
        sources,
        task_id=TASK,
        seed=TASK,
        tail_ms=tail_ms,
        data_dir=data_dir,
        voice_master=data_dir / "work" / TASK / "tts" / "voice_master.wav",
    )


# ══════════════════════════════════════════════════════════════════════
# 停顿：可复现 + 有界 + 不凭空造静音
# ══════════════════════════════════════════════════════════════════════


def test_pause_is_reproducible_and_stays_within_the_jitter_band() -> None:
    """★ 同一句每次得到同一个停顿；落在 ±80ms 之内；不同句各不相同。"""
    first = [pause_after_ms(200, seed=TASK, seq=seq) for seq in range(1, 21)]
    assert first == [pause_after_ms(200, seed=TASK, seq=seq) for seq in range(1, 21)]
    assert all(200 - PAUSE_JITTER_MS <= value <= 200 + PAUSE_JITTER_MS for value in first)
    # 每句都停同一个数，整条音轨就带上节拍器的机器特征 —— 抖动必须真的抖起来。
    assert len(set(first)) >= 2


def test_two_tasks_do_not_share_the_same_rhythm() -> None:
    """种子不同 ⇒ 节奏不同（否则"抖动"只是一个全局常量，防搬运的意义就没了）。"""
    left = [pause_after_ms(200, seed="task-a", seq=seq) for seq in (1, 2, 3, 4, 5)]
    right = [pause_after_ms(200, seed="task-b", seq=seq) for seq in (1, 2, 3, 4, 5)]
    assert left != right


def test_a_zero_base_pause_never_invents_silence() -> None:
    """基础停顿是 0（"紧接下一句"）⇒ 抖动被夹回 0，不许凭空多出 80ms。"""
    assert {pause_after_ms(0, seed=TASK, seq=seq) for seq in range(1, 31)} == {0}


# ══════════════════════════════════════════════════════════════════════
# 累加：单调、不重叠、总和 = 三部分之和
# ══════════════════════════════════════════════════════════════════════


def test_the_timeline_accumulates_without_gaps_or_overlaps(tmp_path: Path) -> None:
    """★ 验收：单调不重叠，且 ``total_ms = Σ句时长 + Σ停顿 + tail``。"""
    timeline = _build(tmp_path)
    assert timeline.sentences[0].start_ms == 0
    for previous, current in pairwise(timeline.sentences):
        assert current.start_ms == previous.end_ms + previous.pause_after_ms
        assert current.start_ms >= previous.end_ms
    for sentence in timeline.sentences:
        assert sentence.end_ms == sentence.start_ms + sentence.duration_ms
    assert timeline.total_ms == timeline.spoken_ms + timeline.pause_ms + timeline.tail_ms
    assert timeline.spoken_ms == 3200 + 2800 + 1500
    assert timeline.total_ms == timeline.master_ms + TAIL_MS


def test_the_master_stops_before_the_tail(tmp_path: Path) -> None:
    """``tail_ms`` 是**成片**的收尾留白，不进母带（否则合成那一步会再加一次）。"""
    timeline = _build(tmp_path)
    last = timeline.sentences[-1]
    assert timeline.master_ms == last.end_ms + last.pause_after_ms


def test_a_zero_pause_sentence_keeps_the_next_one_immediately_after(tmp_path: Path) -> None:
    """某一句不设停顿 ⇒ 下一句紧接它（``start_ms`` 正好等于上一句的 ``end_ms``）。"""
    timeline = _build(tmp_path, (1000, 1000), pauses=(0, 0))
    assert timeline.sentences[1].start_ms == timeline.sentences[0].end_ms
    assert timeline.total_ms == 2000 + TAIL_MS


# ══════════════════════════════════════════════════════════════════════
# 落盘的形状（下游按字段名读，改名等于改契约）
# ══════════════════════════════════════════════════════════════════════


def test_the_json_carries_the_contract_fields(tmp_path: Path) -> None:
    """§04.2.7 的字段名逐条钉住；``scenes`` / ``loudness`` 是二期，不该凭空出现。"""
    payload = _build(tmp_path).to_dict()
    assert payload["schema_version"] == "1.0"
    assert payload["task_id"] == TASK
    assert payload["sample_rate"] == 48_000
    assert payload["channels"] == 1
    assert payload["tail_ms"] == TAIL_MS
    assert set(payload["sentences"][0]) == {
        "id",
        "seq",
        "speaker",
        "start_ms",
        "end_ms",
        "duration_ms",
        "pause_after_ms",
        "audio",
        "text",
    }
    assert "scenes" not in payload
    assert "loudness" not in payload


def test_the_paths_inside_the_json_are_relative_to_data(tmp_path: Path) -> None:
    """绝对路径会让这份清单跟着 ``data/`` 一搬家就全指错（§04.2.7 的写法是相对的）。"""
    payload = _build(tmp_path).to_dict()
    assert payload["voice_master"] == f"work/{TASK}/tts/voice_master.wav"
    assert payload["sentences"][0]["audio"] == f"output/voice/{TASK}/s001.wav"


def test_write_timeline_replaces_atomically(tmp_path: Path) -> None:
    """写完不能留下 ``.partial``：读者要么看到上一版，要么看到这一版。"""
    timeline = _build(tmp_path)
    target = tmp_path / "work" / TASK / "timeline.json"
    write_timeline(timeline, target)
    assert json.loads(target.read_text(encoding="utf-8"))["total_ms"] == timeline.total_ms
    assert list(target.parent.glob("*.partial*")) == []


# ══════════════════════════════════════════════════════════════════════
# 边界：宁可现在吵，不要出片之后才发现
# ══════════════════════════════════════════════════════════════════════


def test_an_empty_script_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as caught:
        build_timeline(
            [],
            task_id=TASK,
            seed=TASK,
            tail_ms=TAIL_MS,
            data_dir=tmp_path,
            voice_master=tmp_path / "voice_master.wav",
        )
    assert caught.value.code is ErrorCode.SCRIPT_NOT_FOUND


def test_a_gap_in_the_sequence_is_refused(tmp_path: Path) -> None:
    """句序断档 ⇒ 时间轴会比母带短，而它**不会**报错，只会让字幕从某句起全体提前。"""
    with pytest.raises(StudioError) as caught:
        build_timeline(
            [_source(tmp_path, 1, 1000), _source(tmp_path, 3, 1000)],
            task_id=TASK,
            seed=TASK,
            tail_ms=TAIL_MS,
            data_dir=tmp_path,
            voice_master=tmp_path / "voice_master.wav",
        )
    assert caught.value.code is ErrorCode.STATE_TRANSITION_ILLEGAL


def test_a_zero_length_sentence_is_refused(tmp_path: Path) -> None:
    """量不出时长的句子（坏 WAV）不许进时间轴：它会让这一句的窗口变成 0 毫秒。"""
    with pytest.raises(StudioError) as caught:
        _build(tmp_path, (3200, 0))
    assert caught.value.code is ErrorCode.TTS_AUDIO_QC_FAILED


def test_a_negative_tail_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tail_ms"):
        _build(tmp_path, tail_ms=-1)


def test_has_audio_only_counts_non_empty_files(tmp_path: Path) -> None:
    """空文件只可能来自"写到一半断电" —— 存在不算数（T2.6 起的同一条判据）。"""
    missing = tmp_path / "nope.wav"
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    good = tmp_path / "good.wav"
    good.write_bytes(b"RIFF")
    assert has_audio(missing) is False
    assert has_audio(empty) is False
    assert has_audio(good) is True


def test_probe_refuses_a_missing_sentence_audio(tmp_path: Path) -> None:
    """缺产物要报**哪一句**，而不是一个 ``FileNotFoundError`` 的堆栈。"""
    with pytest.raises(StudioError) as caught:
        probe_sentence_ms(tmp_path / "s007.wav", seq=7)
    assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED
    assert "7" in str(caught.value)
