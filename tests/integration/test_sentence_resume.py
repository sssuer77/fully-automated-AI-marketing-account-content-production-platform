"""端到端：句级续传与句级缓存（T2.6 验收 · §03.3.7 / §04.3.6）。

T2.6 的四条验收问的是同一件事 —— **"重跑一遍"到底重念了几句**：

① 杀进程重启后已完成句的引擎调用次数为 **0**（硬断言）；
② 第 3 句注入失败 ⇒ **只有它**重试；
③ 编辑某句 ⇒ **只有它**失效重合成；
④ 缓存命中率 ≥ 30%。

为什么单元测试答不了这四条
--------------------------
单元测试把"进程"与"队列"都换成了假件；而这四条恰恰是**库里的状态 / 盘上的缓存 /
队列的重投**三者对不上时才出错 —— 那种错的表现是"面板显示全部完成，成片里却有
几句没声音"，全链路一声不吭。所以这里跑**真的 PoolWorker**：真入队、真认领、
真续租、真收尾、真 SQLite、真的缓存目录、真的 ffprobe 实测时长（``tts_duration_ms``
是时间轴（T2.7）的唯一来源，"探针读出来的数有没有被原样写进库"必须真验一次）。

只把**引擎**换成数得清调用次数的假件 —— "重念了几句"这件事必须有账可查。
假引擎写的是**真的 WAV**（stdlib ``wave``），所以 ``probe_media`` 那一环仍然是真的。

"进程被杀"是怎么造的
--------------------
不 mock worker，也不手搓执行上下文：让第一轮 worker **跑完单元但不 ack**
（``JobStore.succeed`` 返回 True 而不落库），作业于是停在 ``claimed``。
这正是进程在 ``finish`` 之后、``succeed`` 之前被杀留下的状态；随后由**真的
sweeper**（``reclaim_expired``）把它退回待办。认领、续租、进度、收尾的判断
全都真跑过一遍，只有"写 succeeded 这一行"被省掉。

池参数在 ``_seed`` 里被调成"崩溃后一秒内可重启"（租约 0 秒、退避 1 毫秒）：
``pool_settings`` 本来就是池参数的唯一真相（WebUI 改的就是这几列），
改它比 mock 时钟诚实。

环境不满足（没 ffmpeg）⇒ skip，不是 fail。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
import wave
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import PoolConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import JobStore
from studio.db.repositories import ScriptRepo, SentenceRepo
from studio.domain import TaskService
from studio.pools.voice_worker import build_voice_handler
from studio.pools.worker_base import PoolWorker, WorkerRunReport
from studio.tts.cache import TtsCache

pytestmark = pytest.mark.e2e

#: 本机没装的音色不该让"池接线写错了"跟着一起红 —— 引擎是假件，音色只是个字符串
VOICE = "验收音色"

#: 池租约（秒）。调到下限只为让"崩溃后 sweeper 能回收"在测试里一秒内发生 ——
#: 生产是 90 秒（``config/pools.yaml``），那是给人留的排障时间。
LEASE_SEC = 1

#: 假引擎写出来的 WAV 参数（单声道 16bit 22.05kHz，与真 SAPI 的档位一致）
SAMPLE_RATE = 22_050
SENTENCE_MS = 700

TEXTS = (
    "第一句台词，说的是跑酷地图。",
    "第二句台词，讲的是开局只有一格方块。",
    "第三句台词，提醒大家点个关注。",
)

EDITED = "第二句被改过了，现在讲的是别的东西。"

#: 句序的**中文**写法。送进引擎的是归一化之后的文本，ASCII 数字会被念成中文
#: （``3`` ⇒ ``三``），拿带阿拉伯数字的标记去认句子永远认不到 —— 故障注入
#: 会静默失效，而"注入失败"在测试里表现为"这个用例什么都没验"。
ORDINALS = ("第一句", "第二句", "第三句")

#: 空池退避几轮就认定"没活了"。生产里 worker 是常驻的、本来就该一直等；
#: 测试里等下去只会挂住 —— 让它**响亮地失败**。
MAX_EMPTY_ROUNDS = 5

GLOSSARY_YAML = 'schema_version: "1.0"\nabbreviations: {}\nterms: {}\nunits: {}\n'


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("本机没有 ffmpeg（时长靠 ffprobe 实测，跳过）")


class CountingEngine:
    """数得清调用次数的假引擎。**它写的是真的 WAV**，所以 ffprobe 那一环是真的。

    ``fail_marker`` 命中 ⇒ 抛 :class:`StudioError`：注入"这一句念不出来"。
    按**子串**判定而不是整串相等 —— 送进引擎的是归一化之后的文本（T2.5），
    标点会被动，拿原文比会得到一个永远不触发的"故障注入"。
    """

    name = "counting"
    revision = "test"

    def __init__(self, *, fail_marker: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_marker = fail_marker

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append(text)
        if self.fail_marker is not None and self.fail_marker in text:
            raise StudioError("注入的合成失败", code=ErrorCode.TTS_SENTENCE_FAILED)
        frames = max(1, round(SAMPLE_RATE * SENTENCE_MS / 1000))
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
    """一个带词表的临时家目录（测试**不碰**真实 ``data/``）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    paths.glossary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.glossary_file.write_text(GLOSSARY_YAML, encoding="utf-8")
    return paths


def _seed(paths: StudioPaths, texts: Sequence[str]) -> Rig:
    """迁移 + 建任务 + 落稿 + 逐句入队；返回这套运行时。"""
    migrate(paths.db_file)
    setup = connect(paths.db_file)
    try:
        task_id = TaskService(setup).create(title="句级续传验收").id
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
                    "pause_after_ms": 200,
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
        # 池参数：并发 3（§03.4.4 的 voice 上限）、租约 1 秒、退避 1 毫秒。
        # 只为一件事：让"崩溃 ⇒ sweeper 回收 ⇒ 重启后重认领"在一秒内发生，
        # 而不是干等 90 秒的租约过期。库是池参数的唯一真相（WebUI 改的就是这几列），
        # 所以这里改它，而不是去 mock 时钟。
        setup.execute(
            "UPDATE pool_settings SET concurrency = 3, lease_sec = ?, "
            "backoff_base_ms = 1, backoff_max_ms = 1 WHERE pool = 'voice'",
            (LEASE_SEC,),
        )
    finally:
        setup.close()
    return Rig(paths=paths, task_id=task_id, sentence_ids=saved.sentence_ids)


@contextmanager
def _db(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    """一条**用完就关**的连接。

    不用 ``with connect(...)``：``sqlite3.Connection`` 的上下文管理器只管事务提交，
    **不关连接** —— 在 Windows 上那会让临时目录删不干净，也会让下一条连接拿到
    别人还开着的 WAL。
    """
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


@contextmanager
def _worker(paths: StudioPaths, engine: CountingEngine) -> Iterator[PoolWorker]:
    """一个**真的** voice 池 worker（连接用完就关，Windows 上临时目录才删得掉）。"""
    connection = connect(paths.db_file)
    try:
        handler = build_voice_handler(paths=paths, connection=connection, engine=engine, voice=VOICE)
        yield PoolWorker(
            pool="voice",
            handler=handler,
            pool_config=_pool_config(),
            paths=paths,
            slot=1,
            version="test",
        )
    finally:
        connection.close()


def _run(paths: StudioPaths, engine: CountingEngine, units: int) -> WorkerRunReport:
    """跑一轮真的 PoolWorker，跑满 ``units`` 个单元。"""
    with _worker(paths, engine) as worker:
        report = worker.run(max_units=units, max_empty_rounds=MAX_EMPTY_ROUNDS)
    assert report.stop_reason == "max_units", f"池里没有可认领的单元了（{report.stop_reason}）"
    return report


def _no_ack(_self: JobStore, **_kwargs: Any) -> bool:
    """收尾的假件：返回"成功"但**不写库** ⇒ 作业停在 ``claimed``（= 进程被杀）。"""
    return True


def _crash(
    paths: StudioPaths, engine: CountingEngine, monkeypatch: pytest.MonkeyPatch, units: int
) -> WorkerRunReport:
    """跑一轮，但让 worker 在 ack 之前"死掉"（作业停在 ``claimed``）。"""
    real = JobStore.succeed
    monkeypatch.setattr(JobStore, "succeed", _no_ack)
    try:
        return _run(paths, engine, units)
    finally:
        monkeypatch.setattr(JobStore, "succeed", real)


def _reclaim_until_pending(rig: Rig) -> None:
    """等 sweeper 真能把这三条作业收回去（租约 ``LEASE_SEC`` 秒）。

    **轮询而不是死等**：``_Pulse`` 每 1 秒会给**在途单元**续一次租，所以
    "跑完就 sleep 一个固定时长"在慢机器上会撞上那次续租 —— 表现是偶发红灯，
    而它看起来像"续传逻辑坏了"。轮询到状态真的变了为止，最多 10 秒。
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _reclaim(rig.paths)
        if "claimed" not in _job_statuses(rig):
            return
        time.sleep(0.1)
    raise AssertionError("租约一直没被回收：后面的断言会变成假绿")


def _reclaim(paths: StudioPaths) -> tuple[str, ...]:
    """真的 sweeper：过期租约 ⇒ 作业退回 ``pending``（§03.4.3 ③）。"""
    with _db(paths) as connection:
        return JobStore(connection).reclaim_expired(pool="voice").requeued


def _job_ids(rig: Rig) -> list[str]:
    """该任务三条 ``voice/sentence`` 作业的 id（按入队顺序）。"""
    with _db(rig.paths) as connection:
        rows = connection.execute(
            "SELECT id FROM jobs WHERE task_id = ? AND pool = 'voice' ORDER BY created_at, id",
            (rig.task_id,),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _calls(engine: CountingEngine) -> dict[str, int]:
    """按句子归类引擎调用（归一化会动标点，所以按子串数，不按整串相等）。"""
    return {name: sum(1 for text in engine.calls if name in text) for name in ORDINALS}


def _job_statuses(rig: Rig) -> list[str]:
    with _db(rig.paths) as connection:
        rows = connection.execute(
            "SELECT status FROM jobs WHERE task_id = ? AND pool = 'voice' ORDER BY created_at, id",
            (rig.task_id,),
        ).fetchall()
    return [str(row["status"]) for row in rows]


def _drop_sentence_wavs(rig: Rig, count: int) -> None:
    """§03.7.5：任务完成后 24 小时删句子 WAV，只留缓存副本。"""
    for seq in range(1, count + 1):
        rig.wav(seq).unlink()


# ══════════════════════════════════════════════════════════════════════
# ① 重启之后，已完成句一次引擎调用都不发生
# ══════════════════════════════════════════════════════════════════════


def test_a_restart_does_not_re_synthesize_finished_sentences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 验收 ①：杀进程重启后已完成句的引擎调用次数为 **0**。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path), TEXTS)

    first = CountingEngine()
    crashed = _crash(rig.paths, first, monkeypatch, len(TEXTS))
    assert (crashed.units_done, crashed.units_failed) == (3, 0)
    assert _calls(first) == dict.fromkeys(ORDINALS, 1)
    assert _job_statuses(rig) == ["claimed"] * 3, "worker 没 ack ⇒ 作业停在 claimed（进程被杀的现场）"

    _drop_sentence_wavs(rig, len(TEXTS))
    _reclaim_until_pending(rig)
    assert set(_job_statuses(rig)) == {"pending"}, "三条作业都该被 sweeper 退回待办"

    # 重启：新进程、新引擎计数器、新连接
    second = CountingEngine()
    resumed = _run(rig.paths, second, len(TEXTS))

    assert second.calls == [], "已完成句一次引擎调用都不该发生（§03.3.7 续传语义 2）"
    assert (resumed.units_done, resumed.units_failed) == (3, 0)
    for seq in range(1, len(TEXTS) + 1):
        assert rig.wav(seq).is_file(), "产物被 GC 删掉了 ⇒ 得从缓存拷回来"
    with _db(rig.paths) as connection:
        store = JobStore(connection)
        results = [store.get(job_id).result for job_id in _job_ids(rig)]
    assert all(item["status"] == "done" for item in results)
    assert all(item["cache_hit"] is True for item in results), "这一轮该是缓存命中，不是重念"


# ══════════════════════════════════════════════════════════════════════
# ② 失败只重试那一句
# ══════════════════════════════════════════════════════════════════════


def test_only_the_failing_sentence_is_retried(tmp_path: Path) -> None:
    """★ 验收 ②：第 3 句注入失败 ⇒ 只有它重试；前两句**不再被念一遍**。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path), TEXTS)

    engine = CountingEngine(fail_marker=ORDINALS[2])
    first = _run(rig.paths, engine, len(TEXTS))
    assert first.units_failed == 1
    assert _calls(engine) == dict.fromkeys(ORDINALS, 1)

    # 环境好了（或换了个音色）：重投那一句
    engine.fail_marker = None
    second = _run(rig.paths, engine, 1)

    assert (second.units_done, second.units_failed) == (1, 0)
    assert _calls(engine) == {"第一句": 1, "第二句": 1, "第三句": 2}, "只有第 3 句被重念"
    with _db(rig.paths) as connection:
        repo = SentenceRepo(connection)
        rows = [repo.get(sentence_id) for sentence_id in rig.sentence_ids]
    assert [row.tts_status for row in rows if row is not None] == ["done", "done", "done"]
    assert rows[2] is not None and rows[2].tts_attempts == 1, "失败记在**它自己**头上"
    assert [row.tts_attempts for row in rows[:2] if row is not None] == [0, 0]


def test_a_hopeless_sentence_degrades_to_silence_instead_of_blocking(tmp_path: Path) -> None:
    """§04.3.3 不变量 3：连挂 3 次 ⇒ 静音占位 + 字幕保留 + **作业成功**（不卡死）。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path), TEXTS)

    engine = CountingEngine(fail_marker=ORDINALS[2])
    _run(rig.paths, engine, len(TEXTS))
    _run(rig.paths, engine, 1)
    last = _run(rig.paths, engine, 1)

    assert (last.units_done, last.units_failed) == (1, 0), "降级是**成功**的一种，不是失败"
    with _db(rig.paths) as connection:
        row = SentenceRepo(connection).get(rig.sentence_ids[2])
    assert row is not None
    assert row.tts_status == "skipped"
    assert row.tts_attempts == 3
    assert row.tts_error is not None and "静音占位" in row.tts_error
    placeholder = rig.wav(3)
    assert placeholder.is_file() and placeholder.stat().st_size > 0
    assert row.tts_duration_ms == _wav_ms(placeholder), "占位时长是 ffprobe **实测**值"
    assert _job_statuses(rig)[2] == "succeeded"


def _wav_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return round(handle.getnframes() * 1000 / handle.getframerate())


# ══════════════════════════════════════════════════════════════════════
# ③ 编辑某句只失效那一句
# ══════════════════════════════════════════════════════════════════════


def test_editing_one_sentence_only_invalidates_that_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 验收 ③：改一句 ⇒ 只有它重合成；其余句**连产物都不该被重写**。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path), TEXTS)

    first = CountingEngine()
    _crash(rig.paths, first, monkeypatch, len(TEXTS))
    before = {seq: rig.wav(seq).stat().st_mtime_ns for seq in (1, 2, 3)}

    # 用户在面板上改了第 2 句（§03.3.7 续传语义 3）
    with _db(rig.paths) as connection:
        assert SentenceRepo(connection).update_text(rig.sentence_ids[1], text=EDITED) == 2
    _reclaim_until_pending(rig)

    second = CountingEngine()
    resumed = _run(rig.paths, second, len(TEXTS))

    assert resumed.units_done == 3
    assert _calls(second) == {"第一句": 0, "第二句": 1, "第三句": 0}, "只有被改的那一句重合成"
    after = {seq: rig.wav(seq).stat().st_mtime_ns for seq in (1, 2, 3)}
    assert (after[1], after[3]) == (before[1], before[3]), "其余句的产物不该被动过"
    assert after[2] != before[2]
    with _db(rig.paths) as connection:
        row = SentenceRepo(connection).get(rig.sentence_ids[1])
    assert row is not None and row.tts_status == "done" and row.version == 2


# ══════════════════════════════════════════════════════════════════════
# ④ 复用率高的稿子，缓存命中率 ≥ 30%
# ══════════════════════════════════════════════════════════════════════


def test_repeated_lines_are_served_from_the_cache(tmp_path: Path) -> None:
    """★ 验收 ④：套话反复出现 ⇒ 命中率 ≥ 30%（§04.3.6 的 P2 性能前提）。"""
    _require_ffmpeg()
    open_line = "关注我，每天更新。"
    texts = (
        open_line,
        "今天我们来看一张跑酷地图。",
        open_line,
        "开局只有一格方块。",
        open_line,
        "记得点赞收藏。",
    )
    rig = _seed(_stage(tmp_path), texts)

    engine = CountingEngine()
    report = _run(rig.paths, engine, len(texts))

    assert (report.units_done, report.units_failed) == (6, 0)
    assert len(engine.calls) == len(set(texts)) == 4, "重复的那 3 句只该念一遍"
    hit_rate = 1 - len(engine.calls) / len(texts)
    assert hit_rate >= 0.30, f"缓存命中率 {hit_rate:.0%} < 30%"
    stats = TtsCache(rig.paths.tts_cache_dir).stats()
    assert stats.files == 4, "缓存里只该有 4 份音频（内容寻址）"
    assert stats.hits >= 2
