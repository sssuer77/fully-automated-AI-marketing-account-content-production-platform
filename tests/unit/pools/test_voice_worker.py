"""配音池单元处理器（``voice/sentence``）的单元测试（T2.6）。

测什么、不测什么
----------------
"真的用 SAPI 念出一句、真的拼出母带"由 ``tests/integration/test_sentence_resume.py``
端到端覆盖。这里只测**单元处理器自己的几件事** —— 每一件出错都会**静默走偏**：

① 已经 ``done`` 的句子必须**一次引擎调用都不发生**（§03.3.7 续传语义 2）；
② 失败要记账（``tts_attempts + 1``）并把异常抛给队列 —— 收尾是队列的事；
③ 到线（3 次）必须**降级**：写静音占位、落 ``skipped``、**返回成功**（字幕模式）；
④ 稿件在合成期间被改过 ⇒ 丢弃结果，且**不**把这次失败记到新稿子头上；
⑤ 进度写进 ``jobs.result_json``，且是**整个任务**的逐句进度（不是 1/1）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import wave
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from studio.core.clock import utc_now
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import JobStore
from studio.db.repositories import ScriptRepo, SentenceRepo
from studio.domain import TaskService
from studio.pools import voice_worker
from studio.pools.heartbeat import WorkerIdentity
from studio.pools.runner import HANDLER_MODULES
from studio.pools.voice_worker import (
    DEGRADE_AFTER_ATTEMPTS,
    VOICE_UNIT_TYPES,
    VoiceSentenceHandler,
    build_voice_handler,
)
from studio.pools.worker_base import UnitContext, _Pulse
from studio.services.log_service import LogService
from studio.tts import sentence as sentence_module
from studio.tts.cache import TtsCache
from studio.tts.sentence import SapiEngine
from studio.tts.service_engine import ResidentEngine, ResidentStatus

WORKER_ID = "voice#1@4242"
VOICE = "Fake Voice"
SAMPLE_RATE = 22_050

GLOSSARY_YAML = 'schema_version: "1.0"\nabbreviations: {}\nterms: {}\nunits: {}\n'


# ── 夹具 ────────────────────────────────────────────────────────────────


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一个写好句子的任务。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    store: JobStore
    repo: SentenceRepo
    task_id: str
    sentence_ids: tuple[str, ...]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    glossary = paths.glossary_file
    glossary.parent.mkdir(parents=True, exist_ok=True)
    glossary.write_text(GLOSSARY_YAML, encoding="utf-8")
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    task_id = TaskService(connection).create(title="跑酷合集").id
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook="钩子",
        body_md="正文",
        cta="关注",
        word_count=60,
        est_duration_ms=12_000,
        speaker_ratio={"bigbear": 1.0},
        outline={"hook_3s": "钩子", "segments": []},
        sentences=[
            {
                "seq": index,
                "text_raw": f"第{index}句台词",
                "text": f"第{index}句台词",
                "speaker": "bigbear",
                "emotion": "neutral",
                "pause_after_ms": 200,
            }
            for index in range(1, 4)
        ],
    )
    try:
        yield Rig(
            paths=paths,
            connection=connection,
            store=JobStore(connection),
            repo=SentenceRepo(connection),
            task_id=task_id,
            sentence_ids=saved.sentence_ids,
        )
    finally:
        connection.close()


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


@pytest.fixture(autouse=True)
def fake_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """占位静音走假实现（真 ffmpeg 那条路由集成测试覆盖）。"""

    def write(text: str, *, out_path: Path, duration_ms: int | None = None, **_kwargs: object) -> int:
        frames = 24_000  # 0.5 秒 @48kHz
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48_000)
            handle.writeframes(b"\x00\x00" * frames)
        return 500

    monkeypatch.setattr(voice_worker, "write_placeholder", write)


class FakeEngine:
    """数得清调用次数的假引擎（"引擎调用为 0"这类断言全靠它）。"""

    name = "fake"
    revision = "test"

    def __init__(self, *, duration_ms: int = 900, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None, int]] = []
        self._duration_ms = duration_ms
        self._fail = fail
        #: 合成**期间**要发生的事（测竞态用：用户在引擎正念这一句时改了稿）
        self.on_call: Callable[[], None] | None = None

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append((text, voice, rate))
        if self.on_call is not None:
            self.on_call()
        if self._fail:
            raise StudioError("引擎炸了", code=ErrorCode.TTS_SENTENCE_FAILED)
        frames = max(1, round(SAMPLE_RATE * self._duration_ms / 1000))
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * frames)


def _handler(rig: Rig, engine: FakeEngine | None = None) -> VoiceSentenceHandler:
    return VoiceSentenceHandler(
        paths=rig.paths,
        connection=rig.connection,
        cache=TtsCache(rig.paths.tts_cache_dir),
        engine=engine if engine is not None else FakeEngine(),
        voice=VOICE,
        log=None,
    )


def _claimed(rig: Rig, sentence_id: str, payload: Mapping[str, Any] | None = None) -> tuple[UnitContext, str]:
    """入队一条 ``voice/sentence`` 并认领它，返回（执行上下文, job_id）。

    真的走一遍 ``enqueue`` + ``claim``：处理器读的是 ``ctx.payload`` / ``ctx.job_id``
    / ``ctx.worker_id``，这三样只有真作业才有。
    """
    job_id = rig.store.enqueue(
        task_id=rig.task_id,
        pool="voice",
        unit_type="sentence",
        unit_ref=sentence_id,
        payload=dict(payload or {}),
    )
    assert job_id is not None
    job = rig.store.claim(pool="voice", worker_id=WORKER_ID)
    assert job is not None and job.id == job_id
    pulse = _Pulse(
        identity=WorkerIdentity(pool="voice", slot=1, pid=4242),
        connection_factory=lambda: rig.connection,
        version=None,
    )
    return (
        UnitContext(
            job=job,
            pool="voice",
            worker_id=WORKER_ID,
            paths=rig.paths,
            timeout_sec=60,
            pulse=pulse,
            renew=lambda: True,
        ),
        job_id,
    )


def _claim_again(rig: Rig) -> UnitContext:
    """模拟"进程被杀 ⇒ sweeper 回收租约 ⇒ 换个 worker 重新认领**同一行作业**"。

    幂等键挡着"再入队一条同样的单元"（§3.4.5：同 ``(task_id, pool, unit_type,
    unit_ref)`` 只允许一行），所以"重跑"在库里长得就是**同一行作业被重认领**。
    认领时间取未来：回收会给作业叠退避（``not_before``），不跳过去就认不到。
    """
    rig.store.reclaim_expired(pool="voice", now=utc_now() + timedelta(seconds=300))
    job = rig.store.claim(pool="voice", worker_id=WORKER_ID, now=utc_now() + timedelta(seconds=600))
    assert job is not None
    pulse = _Pulse(
        identity=WorkerIdentity(pool="voice", slot=1, pid=4242),
        connection_factory=lambda: rig.connection,
        version=None,
    )
    return UnitContext(
        job=job,
        pool="voice",
        worker_id=WORKER_ID,
        paths=rig.paths,
        timeout_sec=60,
        pulse=pulse,
        renew=lambda: True,
    )


def _fail_job(rig: Rig, job_id: str) -> None:
    """模拟队列的失败收尾（退避重排）—— 处理器**不自己收尾**，这一步是 worker 的事。"""
    rig.store.fail(
        job_id=job_id,
        worker_id=WORKER_ID,
        error_code=str(ErrorCode.TTS_SENTENCE_FAILED),
        error_message="boom",
    )


# ══════════════════════════════════════════════════════════════════════
# 注册表与装配
# ══════════════════════════════════════════════════════════════════════


def test_handler_claims_the_sentence_unit_type() -> None:
    assert frozenset({"sentence"}) == VOICE_UNIT_TYPES
    assert VoiceSentenceHandler.unit_types == VOICE_UNIT_TYPES


def test_the_voice_pool_module_is_declared_for_the_launcher() -> None:
    """启动器（裁定 103）**先问再拉**：它问的就是这张表。"""
    assert HANDLER_MODULES["voice"] == "studio.pools.voice_worker"
    assert importlib.util.find_spec(HANDLER_MODULES["voice"]) is not None


def test_build_resolves_the_voice_at_assembly_time(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """音色在**装配期**解析一次：放进单元里就是"每念一句先卡两秒"。"""
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: VOICE)
    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)
    assert handler._voice == VOICE
    assert isinstance(handler._cache, TtsCache)
    assert handler._log is not None, "忘了挂日志 ⇒ 面板的日志框在生产上永远是空的"


def test_build_raises_when_no_voice_is_installed(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个音色都没有 ⇒ **启动就报**，而不是"起来了，然后每句都失败"。"""
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: None)
    with pytest.raises(StudioError) as excinfo:
        build_voice_handler(paths=rig.paths, connection=rig.connection)
    assert excinfo.value.code is ErrorCode.TTS_ENGINE_UNAVAILABLE


def test_sapi_engine_is_the_default_seam() -> None:
    """SAPI 仍是**兜底**那台引擎（常驻服务不可用时全靠它，成片照样有人声）。"""
    assert SapiEngine().name == "sapi"


def test_the_resident_service_wins_when_it_is_up(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ T2.3 薄片：常驻服务起着 ⇒ 配音池用它，不再是系统语音包。

    这一条就是「文案 → 配音 → 渲染」里那个"配音"换嗓子了没有的判据：引擎不是
    SAPI，音色是参考音的名字（``bigbear``），而不是系统语音包的名字。
    """
    found = ResidentStatus(
        base_url="http://127.0.0.1:8788",
        engine="cosyvoice2",
        revision="074ca6dc",
        ready=True,
        device="cuda",
        model_state="ready",
        sample_rate=24_000,
        voices=("bigbear", "littlebear"),
    )
    monkeypatch.setattr(voice_worker, "active_resident", lambda paths, **kwargs: found)

    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)

    assert isinstance(handler._engine, ResidentEngine)
    assert handler._engine.name == "cosyvoice2"
    assert handler._voice == "bigbear", "作业 payload 没带 voice 时要用服务报的第一个可克隆音色"


def test_sapi_stays_the_fallback_when_the_service_is_down(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务没起 ⇒ 退回 SAPI，**不是**报错退出：整条产线不能因为配音降级就停
    （§1.7；成片有人声优先于"用上模型"）。"""
    monkeypatch.setattr(voice_worker, "active_resident", lambda paths, **kwargs: None)
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: VOICE)

    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)

    assert isinstance(handler._engine, SapiEngine)
    assert handler._voice == VOICE


def test_an_injected_engine_is_never_probed_around(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """注入的引擎是"就用这一个"的明确指令 —— 探测只会把它顶掉。"""

    def boom(paths: object, **kwargs: object) -> None:
        raise AssertionError("注入了引擎还去探服务")

    monkeypatch.setattr(voice_worker, "active_resident", boom)
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: VOICE)
    fake = FakeEngine()

    handler = build_voice_handler(paths=rig.paths, connection=rig.connection, engine=fake)

    assert handler._engine is fake


# ── 引擎选择器："每一次取用时判定"（真机坑 2026-09-18）────────────────────────


def _resident(voices: tuple[str, ...] = ("bigbear", "littlebear")) -> ResidentStatus:
    return ResidentStatus(
        base_url="http://127.0.0.1:8788",
        engine="cosyvoice2",
        revision="074ca6dc",
        ready=True,
        device="cuda",
        model_state="ready",
        sample_rate=24_000,
        voices=voices,
    )


class FakeResidentEngine(FakeEngine):
    """常驻引擎的替身：名字得跟服务自报的一致（断言读的就是它）。"""

    name = "cosyvoice2"


def _fake_resident_engine(*, paths: object, status: object) -> FakeResidentEngine:
    """把 `ResidentEngine` 换成替身（真实现要连 HTTP，单测不该碰网络）。"""
    return FakeResidentEngine()


@dataclass
class RecordingLog:
    """记下每一条 ``_log_line`` 的假日志（真实现要一条连接，这里只要"说了什么"）。"""

    lines: list[tuple[str, str]]

    def __init__(self) -> None:
        self.lines = []

    def append(self, *, level: str, message: str, **_kwargs: object) -> None:
        self.lines.append((level, message))


def _upgrading_pool(
    monkeypatch: pytest.MonkeyPatch, *, resident_voices: tuple[str, ...] = ("bigbear", "littlebear")
) -> dict[str, bool]:
    """搭一个"装配期服务没起、之后才就绪"的池子 —— 真机启动时序就长这样。

    返回的是那个开关：置 ``True`` 就当"模型加载完了"。
    """
    state = {"up": False}

    def probe(paths: object, **kwargs: object) -> ResidentStatus | None:
        return _resident(resident_voices) if state["up"] else None

    monkeypatch.setattr(voice_worker, "active_resident", probe)
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: VOICE)
    monkeypatch.setattr(voice_worker, "list_voices_cached", lambda: (VOICE, "Microsoft Huihui Desktop"))
    monkeypatch.setattr(voice_worker, "ResidentEngine", _fake_resident_engine)
    return state


def test_the_picker_upgrades_once_the_model_has_finished_loading(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 真机坑：装配期服务没起（模型要加载 22s），第一句执行时已经就绪 ⇒ **用常驻引擎**。

    "装配期定一次"会把整条链路钉在 SAPI 上，而且**一直钉到 voice 进程重启**：
    面板上写着 ``bigbear``（它问的时候服务已经就绪），池子却把 ``bigbear`` 交给 SAPI ——
    ``SelectVoice`` 抛 ⇒ 每句失败 3 次 ⇒ 成片没人声，而库里写着"换音色成功"。
    """
    state = _upgrading_pool(monkeypatch)
    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)

    assert isinstance(handler._engine, SapiEngine), "装配期确实还没起"

    state["up"] = True
    engine, voice, speakable = handler._current_engine()

    assert isinstance(engine, FakeResidentEngine)
    assert voice == "bigbear"
    assert speakable == ("bigbear", "littlebear")


def test_the_picker_never_downgrades_mid_task(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """拿到常驻引擎就固定用它：中途降回 SAPI 会让同一支片子一半 CosyVoice、
    一半系统音色 —— 听起来只是"有几句话怪"，比如实失败难查得多。
    """
    state = _upgrading_pool(monkeypatch)
    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)

    state["up"] = True
    assert isinstance(handler._current_engine()[0], FakeResidentEngine)

    state["up"] = False
    assert isinstance(handler._current_engine()[0], FakeResidentEngine), "不能回头降级"


def test_a_sleeping_resident_service_still_wins(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 真机坑（2026-09-18）：空闲卸载（``model_state: unloaded``）**不是**不可用。

    池子若把它当"服务不可用"，成片里就换成系统语音包念 —— 而面板、日志、库里
    全都写着 ``cosyvoice2``。真正会付出的代价只是**第一句多等一次冷加载**。
    """
    sleeping = ResidentStatus(
        base_url="http://127.0.0.1:8788",
        engine="cosyvoice2",
        revision="074ca6dc",
        ready=False,
        device="cuda",
        model_state="unloaded",
        sample_rate=24_000,
        voices=("bigbear", "littlebear"),
    )
    monkeypatch.setattr(voice_worker, "active_resident", lambda paths, **kwargs: sleeping)
    monkeypatch.setattr(voice_worker, "pick_voice", lambda: VOICE)
    monkeypatch.setattr(voice_worker, "ResidentEngine", _fake_resident_engine)

    handler = build_voice_handler(paths=rig.paths, connection=rig.connection)

    assert isinstance(handler._engine, FakeResidentEngine), "睡着了也要用常驻引擎（它叫得醒）"
    assert handler._voice == "bigbear"


def test_the_sapi_fallback_voice_is_resolved_only_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """列音色要起一次 PowerShell（1–2 秒）—— 进程生命周期内只该解析一次。"""
    calls: list[str] = []
    _upgrading_pool(monkeypatch)

    def resolve() -> str:
        calls.append(VOICE)
        return VOICE

    monkeypatch.setattr(voice_worker, "pick_voice", resolve)

    picker = voice_worker._EnginePicker(rig.paths)
    assert picker()[1] == VOICE
    assert picker()[1] == VOICE
    assert calls == [VOICE]


def test_a_voice_this_engine_cannot_speak_falls_back(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """payload 里的音色是**面板算的时候**能念的，而引擎可能在那之后换了。

    硬交给现在的引擎只会每句失败 3 次、降级成静音 —— 成片没人声，而库里写着"换音色成功"。
    """
    state = _upgrading_pool(monkeypatch)
    log = RecordingLog()
    handler = build_voice_handler(paths=rig.paths, connection=rig.connection, log=cast(LogService, log))
    state["up"] = True

    sentence_id = rig.sentence_ids[0]
    ctx, _job_id = _claimed(rig, sentence_id, {"voice": "Microsoft Huihui Desktop"})
    handler.run(ctx)

    assert handler._engine.name == "cosyvoice2"
    row = rig.repo.get(sentence_id)
    assert row is not None
    assert row.tts_status == "done", "不能因为音色对不上就失败"
    assert row.tts_engine == "cosyvoice2"
    assert row.tts_voice_id == "bigbear", "应该改用当前引擎的兜底音色"

    levels = [level for level, _ in log.lines]
    assert "warn" in levels, "改了音色就得说一声，否则只能靠听"


def test_an_injected_engine_does_not_second_guess_the_voice(rig: Rig) -> None:
    """注入引擎（测试 / 演练）时不判音色："就用这一个"是明确指令。"""
    fake = FakeEngine()
    handler = _handler(rig, fake)

    assert handler._current_engine() == (fake, VOICE, None)


# ══════════════════════════════════════════════════════════════════════
# ① 合成与回填
# ══════════════════════════════════════════════════════════════════════


def test_pending_sentence_is_synthesized_and_written_back(rig: Rig) -> None:
    engine = FakeEngine(duration_ms=900)
    handler = _handler(rig, engine)
    ctx, job_id = _claimed(rig, rig.sentence_ids[0])

    result = handler.run(ctx)

    assert len(engine.calls) == 1
    assert result["status"] == "done"
    assert result["cache_hit"] is False
    row = rig.repo.get(rig.sentence_ids[0])
    assert row is not None
    assert row.tts_status == "done"
    assert row.tts_duration_ms == 900
    assert row.tts_hash == result["tts_hash"]
    assert row.tts_engine == "fake"
    assert row.tts_voice_id == VOICE
    assert row.tts_audio_path == rig.paths.sentence_wav(rig.task_id, 1).as_posix()
    assert rig.store.get(job_id).status == "claimed", "收尾是队列的事，不是处理器的"


def test_payload_voice_overrides_the_assembled_one(rig: Rig) -> None:
    engine = FakeEngine()
    handler = _handler(rig, engine)
    ctx, _ = _claimed(rig, rig.sentence_ids[0], {"voice": "另一个音色"})

    handler.run(ctx)

    assert engine.calls[0][1] == "另一个音色"
    row = rig.repo.get(rig.sentence_ids[0])
    assert row is not None and row.tts_voice_id == "另一个音色"


def test_speed_reaches_the_engine_as_a_sapi_rate(rig: Rig) -> None:
    engine = FakeEngine()
    handler = _handler(rig, engine)
    ctx, _ = _claimed(rig, rig.sentence_ids[0])

    handler.run(ctx)

    assert engine.calls[0][2] == 0, "speed=1.0（DB 默认）⇒ SAPI 的 0 档"


# ══════════════════════════════════════════════════════════════════════
# ② 已经念过的句子：0 次引擎调用
# ══════════════════════════════════════════════════════════════════════


def test_done_sentence_is_restored_from_cache_without_an_engine_call(rig: Rig) -> None:
    """★ 验收硬断言：重启之后已完成句的引擎调用次数必须是 0。"""
    first = _handler(rig, FakeEngine())
    ctx, _ = _claimed(rig, rig.sentence_ids[0])
    first.run(ctx)
    # 产物被清掉（§03.7.5：任务完成后 24 小时删句子 WAV），缓存还在
    rig.paths.sentence_wav(rig.task_id, 1).unlink()

    engine = FakeEngine()
    second = _handler(rig, engine)
    result = second.run(_claim_again(rig))

    assert engine.calls == [], "已经 done 且缓存命中 ⇒ 一次引擎调用都不该发生"
    assert result["cache_hit"] is True
    assert rig.paths.sentence_wav(rig.task_id, 1).is_file(), "音频要从缓存找回来"


def test_done_sentence_whose_artifact_is_on_disk_is_not_re_synthesized(rig: Rig) -> None:
    """产物还在盘上 ⇒ 连缓存都不用查：重跑一条刚做完的任务不该重念任何一句。"""
    first = _handler(rig, FakeEngine())
    ctx, _ = _claimed(rig, rig.sentence_ids[0])
    result = first.run(ctx)
    TtsCache(rig.paths.tts_cache_dir).path_for(result["tts_hash"]).unlink()

    engine = FakeEngine()
    second = _handler(rig, engine)
    fresh = second.run(_claim_again(rig))

    assert engine.calls == []
    assert fresh["status"] == "done"
    assert fresh["cache_hit"] is False, "没走缓存：产物本来就在盘上"


def test_done_sentence_is_re_synthesized_when_the_cache_was_pruned(rig: Rig) -> None:
    """产物与缓存**都没了**（24 小时 GC + LRU 淘汰）⇒ 不能"假装命中"：得重新念出来。

    否则交出去的是一份指向空气的 ``tts_audio_path`` —— 成片里那一句没声音，
    而且没有任何地方会报错。这一条是"静默走偏"里最贵的一种。
    """
    first = _handler(rig, FakeEngine())
    ctx, _ = _claimed(rig, rig.sentence_ids[0])
    result = first.run(ctx)
    rig.paths.sentence_wav(rig.task_id, 1).unlink()
    TtsCache(rig.paths.tts_cache_dir).path_for(result["tts_hash"]).unlink()

    engine = FakeEngine()
    second = _handler(rig, engine)
    fresh = second.run(_claim_again(rig))

    assert len(engine.calls) == 1
    assert fresh["status"] == "done"
    assert fresh["cache_hit"] is False
    assert rig.paths.sentence_wav(rig.task_id, 1).is_file(), "重念之后音频得回来"
    row = rig.repo.get(rig.sentence_ids[0])
    assert row is not None
    assert row.tts_status == "done"
    assert row.tts_hash == fresh["tts_hash"]


def test_a_truncated_artifact_is_not_treated_as_done(rig: Rig) -> None:
    """0 字节的产物只可能来自"写到一半断电"：当成完成就是把一段残缺音频交给成片。"""
    first = _handler(rig, FakeEngine())
    ctx, _ = _claimed(rig, rig.sentence_ids[0])
    result = first.run(ctx)
    target = rig.paths.sentence_wav(rig.task_id, 1)
    target.write_bytes(b"")
    TtsCache(rig.paths.tts_cache_dir).path_for(result["tts_hash"]).unlink()

    engine = FakeEngine()
    fresh = _handler(rig, engine).run(_claim_again(rig))

    assert len(engine.calls) == 1
    assert fresh["status"] == "done"
    assert target.stat().st_size > 0


def test_skipped_sentence_is_not_re_synthesized(rig: Rig) -> None:
    """降级是**人工可查的结论**，重投一次作业不该把它偷偷变回"再念一遍"。"""
    rig.repo.skip(
        rig.sentence_ids[0],
        expected_version=1,
        audio_path=rig.paths.sentence_wav(rig.task_id, 1).as_posix(),
        duration_ms=500,
        error="连续 3 次失败 ⇒ 静音占位",
    )
    engine = FakeEngine()
    handler = _handler(rig, engine)
    ctx, _ = _claimed(rig, rig.sentence_ids[0])

    result = handler.run(ctx)

    assert engine.calls == []
    assert result["status"] == "skipped"
    assert result["degraded"] is True


# ══════════════════════════════════════════════════════════════════════
# ③ 失败与降级
# ══════════════════════════════════════════════════════════════════════


def test_failure_counts_an_attempt_and_re_raises(rig: Rig) -> None:
    handler = _handler(rig, FakeEngine(fail=True))
    ctx, job_id = _claimed(rig, rig.sentence_ids[0])

    with pytest.raises(StudioError):
        handler.run(ctx)

    row = rig.repo.get(rig.sentence_ids[0])
    assert row is not None
    assert (row.tts_status, row.tts_attempts) == ("failed", 1)
    assert row.tts_error is not None and "引擎炸了" in row.tts_error
    assert rig.store.get(job_id).status == "claimed", "处理器不自己收尾"


def test_third_failure_degrades_to_silence_and_succeeds(rig: Rig) -> None:
    """★ 到线降级：写静音占位 + 落 ``skipped`` + **返回成功**（字幕模式继续往下走）。"""
    handler = _handler(rig, FakeEngine(fail=True))
    sentence_id = rig.sentence_ids[0]
    ctx, job_id = _claimed(rig, sentence_id)
    for expected in (1, 2):
        with pytest.raises(StudioError):
            handler.run(ctx)
        row = rig.repo.get(sentence_id)
        assert row is not None and row.tts_attempts == expected
        _fail_job(rig, job_id)  # 队列退避重排，下一轮换个租约重认领
        ctx = _claim_again(rig)

    result = handler.run(ctx)

    assert result["status"] == "skipped"
    assert result["degraded"] is True
    assert result["duration_ms"] == 500, "占位静音的时长是实测值，不是估算值"
    assert result["degrade_reason"] and "3 次" in result["degrade_reason"]
    row = rig.repo.get(sentence_id)
    assert row is not None
    assert row.tts_status == "skipped"
    assert row.tts_audio_path == rig.paths.sentence_wav(rig.task_id, 1).as_posix()
    assert rig.paths.sentence_wav(rig.task_id, 1).is_file()
    assert rig.store.get(job_id).status == "claimed"


def test_degrade_attempt_line_matches_the_spec() -> None:
    """§04.3.3 不变量 3 写死的是 3 —— 改它等于改产品行为，得有人看见。"""
    assert DEGRADE_AFTER_ATTEMPTS == 3


def test_version_conflict_discards_the_result_without_counting_an_attempt(rig: Rig) -> None:
    """★ 稿件在合成期间被改过 ⇒ 丢弃结果，且**不**把这次失败记到新稿子头上。"""
    engine = FakeEngine()
    handler = _handler(rig, engine)
    ctx, _ = _claimed(rig, rig.sentence_ids[0])

    # 引擎正在念这一句的时候，用户在面板上把它改了
    def edit_the_line() -> None:
        rig.repo.update_text(rig.sentence_ids[0], text="改过的台词")

    engine.on_call = edit_the_line

    with pytest.raises(StudioError) as excinfo:
        handler.run(ctx)

    assert excinfo.value.code is ErrorCode.STATE_VERSION_CONFLICT
    row = rig.repo.get(rig.sentence_ids[0])
    assert row is not None
    assert row.tts_status == "pending", "结果被丢弃 ⇒ 这一句仍是待办"
    assert row.tts_attempts == 0, "过期结果不该记账（否则改一句就离降级更近一步）"
    assert row.tts_hash is None


def test_missing_sentence_row_is_reported(rig: Rig) -> None:
    handler = _handler(rig)
    job_id = rig.store.enqueue(task_id=rig.task_id, pool="voice", unit_type="sentence", unit_ref="nope")
    assert job_id is not None
    job = rig.store.claim(pool="voice", worker_id=WORKER_ID)
    assert job is not None
    pulse = _Pulse(
        identity=WorkerIdentity(pool="voice", slot=1, pid=4242),
        connection_factory=lambda: rig.connection,
        version=None,
    )
    ctx = UnitContext(
        job=job,
        pool="voice",
        worker_id=WORKER_ID,
        paths=rig.paths,
        timeout_sec=60,
        pulse=pulse,
        renew=lambda: True,
    )

    with pytest.raises(StudioError) as excinfo:
        handler.run(ctx)

    assert excinfo.value.code is ErrorCode.SCRIPT_NOT_FOUND


# ══════════════════════════════════════════════════════════════════════
# ④ 进度
# ══════════════════════════════════════════════════════════════════════


def test_progress_is_written_into_result_json(rig: Rig) -> None:
    """进度落在 ``jobs.result_json``：读它的是**另一个进程**（面板）。"""
    handler = _handler(rig)
    ctx, job_id = _claimed(rig, rig.sentence_ids[0])

    result = handler.run(ctx)

    stored = rig.store.get(job_id).result
    assert stored["stage"] == "done"
    assert stored["sentence_id"] == rig.sentence_ids[0]
    assert stored["seq"] == 1
    assert stored["note"]
    assert (stored["done"], stored["total"]) == (1, 3)
    assert result["progress"]["total"] == 3
    assert result["progress"]["ratio"] == pytest.approx(1 / 3, abs=1e-4), "进度是四舍五入过的"


def test_progress_counts_the_whole_task_not_just_this_sentence(rig: Rig) -> None:
    """面板要的是"这条片子念到哪儿了"；每句都报 1/1 等于没信息。"""
    rig.repo.skip(
        rig.sentence_ids[1],
        expected_version=1,
        audio_path="p.wav",
        duration_ms=500,
        error="降级",
    )
    handler = _handler(rig)
    ctx, job_id = _claimed(rig, rig.sentence_ids[0])

    handler.run(ctx)

    stored = rig.store.get(job_id).result
    assert stored["done"] == 2, "第 2 句已降级 + 第 1 句刚念完"
    assert stored["total"] == 3


def test_progress_keeps_the_failure_scene(rig: Rig) -> None:
    """失败时那份 dict 就是"停在哪儿"的现场（含 ``remediation``：``jobs`` 没这一列）。"""
    handler = _handler(rig, FakeEngine(fail=True))
    ctx, job_id = _claimed(rig, rig.sentence_ids[0])

    with pytest.raises(StudioError):
        handler.run(ctx)

    stored = rig.store.get(job_id).result
    assert "失败" in stored["note"]
    assert stored["error_code"] == str(ErrorCode.TTS_SENTENCE_FAILED)
