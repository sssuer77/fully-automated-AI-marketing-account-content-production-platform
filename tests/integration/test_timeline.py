"""端到端：母带 + 时间轴（T2.7 验收 · §04.2.7）。

T2.7 的验收问两件事
------------------
① 时间轴自己站得住：单调、不重叠、``total_ms = Σ句实测 + Σ停顿 + tail``；
② 时间轴与**盘上那条母带**对得上：``ffprobe(voice_master)`` 与 ``total_ms - tail_ms``
   的偏差 ≤30ms。

为什么必须跑真的 ffmpeg
----------------------
"停顿有没有真的进音频"是这一环最容易写错的地方：时间轴上记了 350ms 的停顿、
母带里却没有 —— 两个文件各自看起来都正常，只有把它们放在一起才看得出
"第 3 句比字幕早到 350ms"。单元测试喂的是假音频，答不了这个问题。
所以这里跑真的 ``PoolWorker``（真入队 / 真认领 / 真续租 / 真收尾 + 真 SQLite +
真 ffprobe）与真的 ``settle_voice``（真 ffmpeg 拼母带），只把**引擎**换成
写得出固定时长 WAV 的假件 —— 时长必须是**可控**的，否则"改一句 ⇒ 后面全体挪"
这条断言没法钉死。

第二句故意设 ``pause_after_ms = 0``：它覆盖"不填静音"那条分支
（``apad`` 只在停顿大于 0 时才进滤镜图），而这条分支一旦写错，
表现是"每一句后面都多出一小段静音"，肉眼几乎看不出来。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import wave
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

from studio.core.config import PoolConfig, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import probe_media
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import JobStore
from studio.db.repositories import ArtifactRepo, ScriptRepo, SentenceRepo
from studio.domain import TaskService
from studio.pools.voice_worker import build_voice_handler
from studio.pools.worker_base import PoolWorker, WorkerRunReport
from studio.services.voice_service import VoiceStageReport, settle_voice
from studio.tts.cache import TtsCache
from studio.tts.sentence import synthesize_sentence, write_placeholder
from studio.tts.timeline import MASTER_DRIFT_TOLERANCE_MS

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本机没装的音色不该让"接线写错了"跟着一起红 —— 引擎是假件，音色只是个字符串
VOICE = "验收音色"

#: 假引擎写出来的 WAV 参数（单声道 16bit 22.05kHz，与真 SAPI 的档位一致）
SAMPLE_RATE = 22_050

#: 每句念多久（毫秒）。**700 能被 22050 整除**：重采样到 48k 之后仍是整数个采样点，
#: 于是"母带时长 = Σ句时长 + Σ停顿"是精确等式，30ms 的容差留给 ffprobe 的四舍五入。
SENTENCE_MS = 700

#: 改稿之后这一句念得更久（用于验"后面每一句的 start_ms 都跟着挪"）
LONG_MS = 1_200

#: 任务的随机化种子（``tasks.payload_json.seed``）—— 停顿抖动由它派生
SEED = 42

TEXTS = (
    "第一句台词，说的是跑酷地图。",
    "第二句台词，讲的是开局只有一格方块。",
    "第三句台词，提醒大家点个关注。",
)

#: 每句后面的基础停顿（毫秒）。第二句是 0：紧接下一句。
PAUSES = (200, 0, 350)

EDITED = "第二句被改过了，现在讲的是别的东西，而且更长。"

GLOSSARY_YAML = 'schema_version: "1.0"\nabbreviations: {}\nterms: {}\nunits: {}\n'

#: 空池退避几轮就认定"没活了"（生产里 worker 常驻，本来就该一直等）
MAX_EMPTY_ROUNDS = 5


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("本机没有 ffmpeg（母带靠它拼、时长靠 ffprobe 实测，跳过）")


class CountingEngine:
    """写得出**固定时长** WAV 的假引擎。

    与 T2.6 的假件同一形态：它写的是**真的 WAV**，所以 ``probe_media`` 那一环、
    以及"停顿真的进了母带"这件事都是真验的。``duration_ms`` 可以中途改 ——
    "改稿之后这一句更长了"要靠它造出来。
    """

    name = "counting"
    revision = "test"

    def __init__(self, *, duration_ms: int = SENTENCE_MS) -> None:
        self.calls: list[str] = []
        self.duration_ms = duration_ms

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append(text)
        frames = max(1, round(SAMPLE_RATE * self.duration_ms / 1000))
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * frames)


@dataclass(frozen=True, slots=True)
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一个排好句子的任务。"""

    paths: StudioPaths
    task_id: str
    sentence_ids: tuple[str, ...]

    def wav(self, seq: int) -> Path:
        return self.paths.sentence_wav(self.task_id, seq)


def _stage(tmp_path: Path) -> StudioPaths:
    """临时家目录：真 ``outputs.yaml`` 抄一份（``tail_ms`` 从配置来）+ 一份空词表。

    不 ``load_config``：临时家目录里只有 ``outputs.yaml``，凑齐七份配置只为跑一条
    时间轴不值当（与 ``tests/integration/test_render_pipeline.py`` 同一条取舍）。
    """
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", paths.config_dir / "outputs.yaml")
    paths.glossary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.glossary_file.write_text(GLOSSARY_YAML, encoding="utf-8")
    return paths


def _seed(paths: StudioPaths, texts: Sequence[str] = TEXTS, *, seed: int | None = SEED) -> Rig:
    """迁移 + 建任务（带种子）+ 落稿（逐句停顿）+ 逐句入队。"""
    migrate(paths.db_file)
    setup = connect(paths.db_file)
    try:
        payload = {} if seed is None else {"seed": seed}
        task_id = TaskService(setup).create(title="时间轴验收", payload=payload).id
        saved = ScriptRepo(setup).save_draft(
            task_id=task_id,
            title="标题",
            hook="钩子",
            body_md="正文",
            cta="关注",
            word_count=60,
            est_duration_ms=20_000,
            speaker_ratio={"bigbear": 1.0},
            outline={"hook_3s": "钩子", "segments": []},
            sentences=[
                {
                    "seq": seq,
                    "text_raw": text,
                    "text": text,
                    "speaker": "bigbear",
                    "emotion": "neutral",
                    "pause_after_ms": PAUSES[(seq - 1) % len(PAUSES)],
                }
                for seq, text in enumerate(texts, start=1)
            ],
        )
        store = JobStore(setup)
        for sentence_id in saved.sentence_ids:
            assert (
                store.enqueue(task_id=task_id, pool="voice", unit_type="sentence", unit_ref=sentence_id)
                is not None
            )
    finally:
        setup.close()
    return Rig(paths=paths, task_id=task_id, sentence_ids=saved.sentence_ids)


@contextmanager
def _db(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    """一条**用完就关**的连接（``sqlite3.Connection`` 的 ``with`` 不关连接）。"""
    connection = connect(paths.db_file)
    try:
        yield connection
    finally:
        connection.close()


def _pool_config() -> PoolConfig:
    """voice 池的旋钮（取值照 ``config/pools.yaml``；真正管认领的是库里的 ``pool_settings``）。"""
    return PoolConfig(
        unit_type="sentence",
        concurrency=1,
        poll_ms=50,
        lease_sec=90,
        backoff_base_ms=3000,
        backoff_max_ms=20_000,
        unit_timeout_sec=120,
        max_attempts=3,
        priority=200,
    )


def _run(paths: StudioPaths, engine: CountingEngine, units: int) -> WorkerRunReport:
    """跑一轮真的 ``PoolWorker``，跑满 ``units`` 个单元。"""
    connection = connect(paths.db_file)
    try:
        handler = build_voice_handler(paths=paths, connection=connection, engine=engine, voice=VOICE)
        worker = PoolWorker(
            pool="voice",
            handler=handler,
            pool_config=_pool_config(),
            paths=paths,
            slot=1,
            version="test",
        )
        report = worker.run(max_units=units, max_empty_rounds=MAX_EMPTY_ROUNDS)
    finally:
        connection.close()
    assert report.stop_reason == "max_units", f"池里没有可认领的单元了（{report.stop_reason}）"
    return report


def _settle(rig: Rig) -> VoiceStageReport:
    """跑一次配音收口（真 ffmpeg 拼母带 + 真回写 + 真落盘）。"""
    with _db(rig.paths) as connection:
        return settle_voice(
            paths=rig.paths,
            connection=connection,
            task_id=rig.task_id,
            outputs=load_outputs_config(rig.paths.config_dir / "outputs.yaml"),
        )


def _resynthesize(rig: Rig, engine: CountingEngine, seq: int, text: str) -> None:
    """把某一句**按库里的状态**重新合成一遍（``begin`` → 合成 → ``finish``）。

    为什么这里不走池：池的续传 / 重试是 T2.6 的题目（那边有专门的用例），
    本文件要造的是"这一句的音频变长了、而它仍然是 ``done``"这个**输入状态**。
    直接调与 worker 单元同一个 :func:`synthesize_sentence`，
    比再抄一遍崩溃 + sweeper 回收的机关更短，也更不容易把两件事搅在一起。
    """
    with _db(rig.paths) as connection:
        repo = SentenceRepo(connection)
        row = repo.get(rig.sentence_ids[seq - 1])
        assert row is not None
        assert repo.begin(row.id, engine=engine.name, voice_id=VOICE)
        synthesis = synthesize_sentence(
            text,
            out_path=rig.wav(seq),
            cache=TtsCache(rig.paths.tts_cache_dir),
            engine=engine,
            voice=VOICE,
            speed=row.speed,
            emotion=row.emotion,
        )
        assert repo.finish(
            row.id,
            expected_version=row.version,
            audio_path=synthesis.audio_path.as_posix(),
            duration_ms=synthesis.duration_ms,
            sample_rate=synthesis.sample_rate,
            tts_hash=synthesis.tts_hash,
            engine=synthesis.engine,
            voice_id=synthesis.voice_id,
        )


def _rows(rig: Rig) -> list[tuple[int, int, int]]:
    """库里的 ``(seq, start_ms, end_ms)``（按句序）。"""
    with _db(rig.paths) as connection:
        rows = connection.execute(
            "SELECT seq, start_ms, end_ms FROM script_sentences WHERE task_id = ? ORDER BY seq",
            (rig.task_id,),
        ).fetchall()
    return [(int(row["seq"]), int(row["start_ms"]), int(row["end_ms"])) for row in rows]


# ══════════════════════════════════════════════════════════════════════
# ① 时间轴与盘上的母带对得上
# ══════════════════════════════════════════════════════════════════════


def test_the_master_matches_the_timeline(tmp_path: Path) -> None:
    """★ 验收：单调不重叠、``total_ms = Σ句 + Σ停顿 + tail``、母带与时间轴偏差 ≤30ms。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    engine = CountingEngine()
    _run(rig.paths, engine, len(TEXTS))

    report = _settle(rig)
    timeline = report.timeline

    # ① 时间轴自己站得住
    assert timeline.sentences[0].start_ms == 0
    for previous, current in pairwise(timeline.sentences):
        assert current.start_ms == previous.end_ms + previous.pause_after_ms
        assert current.start_ms >= previous.end_ms
    assert timeline.total_ms == timeline.spoken_ms + timeline.pause_ms + timeline.tail_ms
    assert timeline.spoken_ms == SENTENCE_MS * len(TEXTS)

    # 第二句的停顿是 0 ⇒ 第三句紧接它（"不填静音"那条分支真的没填）
    assert timeline.sentences[1].pause_after_ms == 0
    assert timeline.sentences[2].start_ms == timeline.sentences[1].end_ms

    # ② 与盘上的母带对得上
    master_ms = report.voice_master_ms
    assert master_ms == probe_media(report.voice_master).duration_ms
    assert abs(master_ms - timeline.master_ms) <= MASTER_DRIFT_TOLERANCE_MS
    # ★ 停顿真的进了音频：母带比"Σ句时长"长出来的那一截，就是停顿之和。
    #   少了这条，"时间轴上记了停顿、母带里没有"会一路绿到成片里。
    assert timeline.pause_ms > 0
    assert abs(master_ms - timeline.spoken_ms - timeline.pause_ms) <= MASTER_DRIFT_TOLERANCE_MS

    # 母带的格式是下游编码参数的前提（48k 单声道 PCM）
    info = probe_media(report.voice_master)
    assert (info.sample_rate, info.channels) == (48_000, 1)

    # ③ 回写进了库（面板 / 字幕都读这几列）
    assert _rows(rig) == [
        (sentence.seq, sentence.start_ms, sentence.end_ms) for sentence in timeline.sentences
    ]

    # ④ 时间轴落盘，且与内存里那一份一致
    payload = json.loads(rig.paths.timeline_json(rig.task_id).read_text(encoding="utf-8"))
    assert payload["total_ms"] == timeline.total_ms
    assert payload["seed"] == f"{rig.task_id}:{SEED}"
    assert payload["voice_master"] == (f"work/{rig.task_id}/tts/voice_master.wav"), (
        "清单里的路径要相对 data/（换机器 / 搬目录之后还指得对）"
    )

    # ⑤ 两条产物都进了 artifacts（§03.3.11）
    with _db(rig.paths) as connection:
        recorded = ArtifactRepo(connection, data_dir=rig.paths.data_dir).list_for_task(rig.task_id)
    assert [item.kind for item in recorded] == ["timeline", "voice_master"]
    assert recorded[1].meta is not None and recorded[1].meta["duration_ms"] == master_ms


# ══════════════════════════════════════════════════════════════════════
# ② 改一句 ⇒ 全量重算（陷阱 #26：禁增量拼接）
# ══════════════════════════════════════════════════════════════════════


def test_editing_one_sentence_shifts_every_later_start(tmp_path: Path) -> None:
    """★ 第 2 句变长 ⇒ 第 3 句的 ``start_ms`` **跟着挪**，而不是留着旧值。

    增量拼接在这里的表现是"第 1 句对、第 2 句对、第 3 句起全体提前"——
    它不报错，只让字幕从某一句起对不上口型。
    """
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    engine = CountingEngine()
    _run(rig.paths, engine, len(TEXTS))
    before = _settle(rig)

    # 用户在面板上改了第 2 句，这一句重念之后更长了
    with _db(rig.paths) as connection:
        assert SentenceRepo(connection).update_text(rig.sentence_ids[1], text=EDITED) == 2
    engine.duration_ms = LONG_MS
    _resynthesize(rig, engine, 2, EDITED)

    after = _settle(rig)

    first, second, third = after.timeline.sentences
    # 第 1 句一个字没改 ⇒ 它的窗口不许动
    assert (first.start_ms, first.end_ms) == (
        before.timeline.sentences[0].start_ms,
        before.timeline.sentences[0].end_ms,
    )
    assert second.duration_ms == LONG_MS > before.timeline.sentences[1].duration_ms
    # 抖动只由 (种子, 句序) 决定 ⇒ 改稿不该改变停顿
    assert [item.pause_after_ms for item in after.timeline.sentences] == [
        item.pause_after_ms for item in before.timeline.sentences
    ]
    # ★ 后面每一句都跟着挪：挪的距离正是这一句变长的那一截
    delta = LONG_MS - SENTENCE_MS
    assert third.start_ms - before.timeline.sentences[2].start_ms == delta
    assert after.timeline.total_ms - before.timeline.total_ms == delta
    assert after.timeline.total_ms == after.timeline.spoken_ms + after.timeline.pause_ms + (
        after.timeline.tail_ms
    )
    assert abs(after.voice_master_ms - after.timeline.master_ms) <= MASTER_DRIFT_TOLERANCE_MS
    assert _rows(rig) == [
        (sentence.seq, sentence.start_ms, sentence.end_ms) for sentence in after.timeline.sentences
    ]


def test_the_timeline_is_reproducible_across_runs(tmp_path: Path) -> None:
    """重跑一次得到**逐毫秒一致**的时间轴（抖动由种子派生，不来自时钟 / 随机数）。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    _run(rig.paths, CountingEngine(), len(TEXTS))

    first = _settle(rig)
    second = _settle(rig)

    assert first.timeline.to_dict() == second.timeline.to_dict()
    assert first.timeline.total_ms == second.timeline.total_ms


def test_a_task_without_a_seed_still_gets_a_stable_rhythm(tmp_path: Path) -> None:
    """没给种子的任务也要抖得**可复现**：缺省拿 ``task_id`` 顶上（它是 ULID）。

    少了这条兜底，"没填种子"就会变成"停顿一个都不抖"—— 那样这一类任务的成片
    会带着同一条节奏，把防搬运的初衷做反。
    """
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path), seed=None)
    _run(rig.paths, CountingEngine(), len(TEXTS))

    first = _settle(rig)
    second = _settle(rig)

    assert first.timeline.to_dict() == second.timeline.to_dict()
    payload = json.loads(rig.paths.timeline_json(rig.task_id).read_text(encoding="utf-8"))
    assert payload["seed"] == rig.task_id
    assert first.timeline.pause_ms > 0


# ══════════════════════════════════════════════════════════════════════
# ③ 没定局 / 产物缺失 ⇒ 报错停下（而不是拼半条母带）
# ══════════════════════════════════════════════════════════════════════


def test_a_half_settled_task_refuses_to_build_a_timeline(tmp_path: Path) -> None:
    """只念了两句 ⇒ 不许算时间轴（半条母带配上完整时间轴，字幕会整体错位）。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    engine = CountingEngine()
    _resynthesize(rig, engine, 1, TEXTS[0])
    _resynthesize(rig, engine, 2, TEXTS[1])

    with pytest.raises(StudioError) as caught:
        _settle(rig)

    assert caught.value.code is ErrorCode.STATE_TRANSITION_ILLEGAL
    assert not rig.paths.voice_master(rig.task_id).exists()
    assert not rig.paths.timeline_json(rig.task_id).exists()


def test_a_missing_sentence_audio_is_named_in_the_error(tmp_path: Path) -> None:
    """库说 ``done``、盘上却没有 ⇒ 报**哪一句**（§03.7.5 的 GC 会留下这种状态）。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    _run(rig.paths, CountingEngine(), len(TEXTS))
    rig.wav(2).unlink()

    with pytest.raises(StudioError) as caught:
        _settle(rig)

    assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED
    assert "2" in str(caught.value)


def test_a_degraded_sentence_still_occupies_time(tmp_path: Path) -> None:
    """降级句（``skipped``，一段等长静音）照样进时间轴（§04.3.3 不变量 2）。

    漏掉它，后面每一句的 ``start_ms`` 都会往前错一整句 —— 而画面与字幕都会跟着错。
    """
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))
    engine = CountingEngine()
    _resynthesize(rig, engine, 1, TEXTS[0])
    _resynthesize(rig, engine, 2, TEXTS[1])

    # 第 3 句念不出来 ⇒ 池会写一段等长静音占位并置 skipped（T2.6 的降级路径）
    placeholder = write_placeholder(TEXTS[2], out_path=rig.wav(3))
    with _db(rig.paths) as connection:
        repo = SentenceRepo(connection)
        row = repo.get(rig.sentence_ids[2])
        assert row is not None
        assert repo.skip(
            row.id,
            expected_version=row.version,
            audio_path=rig.wav(3).as_posix(),
            duration_ms=placeholder,
            error="3 次失败后降级：静音占位",
        )

    report = _settle(rig)

    assert report.degraded == 1
    assert len(report.timeline.sentences) == len(TEXTS), "降级句也占时间"
    assert report.timeline.sentences[2].duration_ms == placeholder
    assert report.timeline.total_ms == report.timeline.spoken_ms + report.timeline.pause_ms + (
        report.timeline.tail_ms
    )
    assert abs(report.voice_master_ms - report.timeline.master_ms) <= MASTER_DRIFT_TOLERANCE_MS
