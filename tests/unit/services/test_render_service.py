"""出片服务里的两个纯步骤：句级时间轴与字幕事件的来源（T3.5）。

为什么要单独测 `resolve_cues`
-----------------------------
它是"字幕时间从哪来"的**唯一**判断点，而它有三条分支，其中两条只在"复用配音"
时才走到 —— 那正是最容易漏测的地方（正常出片路径根本碰不到）。三条分支里只要有一条
悄悄退化成"按字数估时长"，字幕就会与人声错位，而错位不会报错。
"""

from __future__ import annotations

import json

from studio.core.paths import StudioPaths
from studio.render.subtitle import Cue
from studio.services.render_service import (
    ProduceRequest,
    resolve_cues,
    write_timeline,
)
from studio.tts.synth import VoiceResult

SPEECH = "第一句在这里。第二句在这里。第三句在这里。"


def _voice(paths: StudioPaths, task_id: str = "t1") -> VoiceResult:
    """一条"刚合成完"的配音结果（时长是实测值，不是估的）。"""
    sentences = ("第一句在这里。", "第二句在这里。", "第三句在这里。")
    files = tuple(paths.sentence_wav(task_id, index) for index in range(1, 4))
    for index, path in enumerate(files, start=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF" + bytes(64) + f"s{index}".encode())
    return VoiceResult(
        task_id=task_id,
        engine="sapi",
        voice="Microsoft Huihui Desktop",
        sentences=sentences,
        sentence_files=files,
        sentence_durations_ms=(3200, 2800, 2100),
        voice_master=paths.voice_master(task_id),
        duration_ms=8100,
    )


# ══════════════════════════════════════════════════════════════════════
# resolve_cues
# ══════════════════════════════════════════════════════════════════════


def test_fresh_voice_wins(tmp_paths: StudioPaths) -> None:
    """这一次真的合成了 ⇒ 用它刚量出来的逐句时长（最准）。"""
    cues = resolve_cues(ProduceRequest(task_id="t1", text=SPEECH), paths=tmp_paths, voice=_voice(tmp_paths))
    assert [(c.start_ms, c.end_ms) for c in cues] == [(0, 3200), (3200, 6000), (6000, 8100)]


def test_reused_timeline_is_used_when_voice_is_not_re_synthesized(tmp_paths: StudioPaths) -> None:
    """复用母带 + 上一轮的 timeline.json ⇒ 用记下的时长（同一个母带，数值不变）。"""
    write_timeline(
        paths=tmp_paths,
        task_id="t1",
        cues=resolve_cues(
            ProduceRequest(task_id="t1", text=SPEECH), paths=tmp_paths, voice=_voice(tmp_paths)
        ),
        voice_master=tmp_paths.voice_master("t1"),
        # 兜底这一份的停顿恒为 0（它反推不出句间停顿）⇒ 总时长 = Σ句长 + tail
        tail_ms=0,
    )
    payload = json.loads(tmp_paths.timeline_json("t1").read_text(encoding="utf-8"))
    rows = payload["sentences"]

    cues = resolve_cues(
        ProduceRequest(task_id="t1", text=SPEECH, reuse_voice=True),
        paths=tmp_paths,
        voice=None,
        timeline_sentences=rows,
    )
    assert [c.text for c in cues] == ["第一句在这里。", "第二句在这里。", "第三句在这里。"]
    assert [(c.start_ms, c.end_ms) for c in cues] == [(0, 3200), (3200, 6000), (6000, 8100)]


def test_no_timeline_and_no_voice_means_no_cues(tmp_paths: StudioPaths) -> None:
    """★ 三条来源都拿不到 ⇒ 空元组（**不是**按字数估一个时长）。

    估出来的时长会让字幕与声音错位，而错位比"没有字幕"更糟 —— 观众会以为配音配错了。
    """
    cues = resolve_cues(
        ProduceRequest(task_id="t1", text=SPEECH, reuse_voice=True),
        paths=tmp_paths,
        voice=None,
        timeline_sentences=None,
    )
    assert cues == ()


def test_a_broken_timeline_falls_back_to_no_cues(tmp_paths: StudioPaths) -> None:
    """timeline 里的时长是 0（文件被改坏了）⇒ 宁可不要字幕，也不拿 0 去算时间。"""
    cues = resolve_cues(
        ProduceRequest(task_id="t1", text=SPEECH, reuse_voice=True),
        paths=tmp_paths,
        voice=None,
        timeline_sentences=[{"text": "甲", "duration_ms": 0}, {"text": "乙", "duration_ms": 0}],
    )
    assert cues == ()


def test_empty_text_has_no_cues(tmp_paths: StudioPaths) -> None:
    cues = resolve_cues(
        ProduceRequest(task_id="t1", text="   "), paths=tmp_paths, voice=None, timeline_sentences=None
    )
    assert cues == ()


# ══════════════════════════════════════════════════════════════════════
# write_timeline
# ══════════════════════════════════════════════════════════════════════


def test_timeline_is_written_next_to_the_other_deliverables(tmp_paths: StudioPaths) -> None:
    cues = (Cue(0, 3200, "第一句。"), Cue(3200, 6000, "第二句。"))
    target = write_timeline(
        paths=tmp_paths,
        task_id="t1",
        cues=cues,
        voice_master=tmp_paths.voice_master("t1"),
        tail_ms=600,
    )
    assert target == tmp_paths.timeline_json("t1")
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["total_ms"] == 6600
    assert payload["voice_master"].endswith("voice_master.wav")
    assert [row["seq"] for row in payload["sentences"]] == [1, 2]
    assert [row["start_ms"] for row in payload["sentences"]] == [0, 3200]
    # 二期才有 scenes[]，一期**不写** —— 凭空造一个空数组会让下游以为"场景算出来是空的"
    assert "scenes" not in payload


def test_timeline_keeps_the_sentence_text(tmp_paths: StudioPaths) -> None:
    """正文要留在时间轴里：复用母带时字幕的**文字**只能从这儿来。"""
    cues = (Cue(0, 1000, "留下来。"),)
    write_timeline(
        paths=tmp_paths,
        task_id="t1",
        cues=cues,
        voice_master=tmp_paths.voice_master("t1"),
        tail_ms=600,
    )
    payload = json.loads(tmp_paths.timeline_json("t1").read_text(encoding="utf-8"))
    assert payload["sentences"][0]["text"] == "留下来。"


def test_the_fallback_timeline_has_the_canonical_shape(tmp_paths: StudioPaths) -> None:
    """★ 兜底那一份与配音阶段写的是**同一种文件**（陷阱 #108）。

    以前这里手写一份"少几个字段"的 JSON，于是同一个路径下躺着两种形状，读的人得
    同时容忍两种。现在两个调用点走的是同一个 ``tts.timeline.write_timeline`` ——
    这条断言钉的就是"字段少一个都算改错了地方"。
    """
    write_timeline(
        paths=tmp_paths,
        task_id="t1",
        cues=(Cue(0, 1000, "留下来。"),),
        voice_master=tmp_paths.voice_master("t1"),
        tail_ms=600,
    )
    payload = json.loads(tmp_paths.timeline_json("t1").read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0"
    assert payload["tail_ms"] == 600
    assert payload["seed"]
    assert set(payload) >= {"task_id", "sample_rate", "channels", "total_ms", "voice_master", "sentences"}
    row = payload["sentences"][0]
    assert set(row) >= {"id", "seq", "speaker", "start_ms", "end_ms", "duration_ms"}
    assert set(row) >= {"pause_after_ms", "audio", "text"}
    # 路径是**相对 data/** 的（换机器也能读），不是绝对路径
    assert not row["audio"].startswith("/") and ":" not in row["audio"]
