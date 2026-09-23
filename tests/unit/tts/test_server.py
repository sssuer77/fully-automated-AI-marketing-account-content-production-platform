"""``tts.server``（T2.2）—— 常驻推理服务的判定层与 HTTP 面。

这些用例**不需要 GPU**：推理后端是注入的假件，验的是服务自己那些容易写反的规矩 ——
① "进程活着"与"模型已加载"必须分开报（混成一个字段，探活说就绪、首句却要等 10 秒）；
② 排队超限要**在进线程池之前**就拒掉（先排再拒的话，429 已经没有意义了）；
③ ``out_path`` 的边界是 ``data/``，绝对路径与 ``..`` 一律拒（§04.3.5 路径穿越防护）；
④ 超时的合成**不会停**，所以 ``/health`` 必须如实标 stuck，而不是假装没事。
"""

from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from studio.core.config import TtsServerConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import probe_media
from studio.core.paths import StudioPaths
from studio.tts.cosyvoice import CosyVoiceBackend, EngineState, LoadResult, SynthResult
from studio.tts.server import (
    VOICE_PROMPT_GAP_MS,
    VOICE_PROMPT_MAX_MS,
    SynthRequest,
    TtsService,
    VoiceRegistry,
    build_prompt,
    create_app,
    resolve_out_path,
)


class FakeBackend:
    """数得出调用次数、可以变慢/变炸的假后端（真后端要 GPU，这里要的是判定）。"""

    revision = "testrev"
    fp16 = True

    def __init__(self, *, delay_sec: float = 0.0, error: StudioError | None = None) -> None:
        self.delay_sec = delay_sec
        self.error = error
        self.calls: list[tuple[str, Path, str]] = []
        self._state = EngineState.UNLOADED
        self._load_result: LoadResult | None = None

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return None

    @property
    def load_result(self) -> LoadResult | None:
        return self._load_result

    @property
    def sample_rate(self) -> int:
        return 24_000

    @property
    def device(self) -> str:
        return "cuda"

    def load(self) -> LoadResult:
        if self._load_result is None:
            self._load_result = LoadResult(
                load_ms=11,
                device="cuda",
                revision=self.revision,
                sample_rate=24_000,
                fp16=True,
                vram_mb=2400,
            )
        self._state = EngineState.READY
        return self._load_result

    def unload(self) -> int:
        freed = 2400 if self._state is EngineState.READY else 0
        self._state = EngineState.UNLOADED
        self._load_result = None
        return freed

    def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        ref_wav: Path,
        ref_text: str,
        speed: float = 1.0,
    ) -> SynthResult:
        if self.delay_sec:
            time.sleep(self.delay_sec)
        if self.error is not None:
            raise self.error
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF0000")
        self.calls.append((text, out_path, ref_wav.name))
        return SynthResult(duration_ms=1000, sample_rate=24_000, synth_ms=900, rtf=0.9, segments=1)


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """隔离家目录 + 两个音色（一个可用、一个缺 ref.txt）。"""
    home = tmp_path / "studio"
    data = home / "data"
    good = data / "voice_src" / "bigbear"
    good.mkdir(parents=True)
    (good / "ref_01.wav").write_bytes(b"RIFF")
    (good / "ref.txt").write_text("这是第一段参考音里逐字说的话。", encoding="utf-8")
    (good / "profile.json").write_text(
        '{"id": "bigbear", "origin": "recorded", "note": "自录"}', encoding="utf-8"
    )
    broken = data / "voice_src" / "littlebear"
    broken.mkdir(parents=True)
    (broken / "ref_01.wav").write_bytes(b"RIFF")
    return StudioPaths(home=home, data_dir=data)


def make_service(
    paths: StudioPaths,
    *,
    backend: FakeBackend | None = None,
    server: TtsServerConfig | None = None,
    clock: list[float] | None = None,
    warmup_text: str = "预热。",
) -> TtsService:
    ticks = clock if clock is not None else [0.0]
    return TtsService(
        # FakeBackend 是鸭子类型的假件（只实现这一层真正调到的几个方法）⇒ 显式 cast
        cast(CosyVoiceBackend, backend or FakeBackend()),
        paths=paths,
        server=server
        or TtsServerConfig(concurrency=1, queue_max=2, idle_unload_min=1, request_timeout_sec=5),
        warmup_text=warmup_text,
        monotonic=lambda: ticks[0],
    )


# ── 探活 ────────────────────────────────────────────────────────────────


class TestHealth:
    def test_process_alive_is_not_the_same_as_model_ready(self, paths: StudioPaths) -> None:
        """``ok`` 与 ``ready`` 分开：进程活着 ≠ 模型在显存里。"""
        health = make_service(paths).health()
        assert health.ok is True
        assert health.ready is False
        assert health.model_state == "unloaded"
        assert health.detail is not None and "冷加载" in health.detail

    def test_warmup_flips_ready_and_reports_load_ms(self, paths: StudioPaths) -> None:
        service = make_service(paths)
        result = service.warmup()
        assert result.ready is True
        assert result.load_ms == 11
        assert result.synth_ms is not None  # 预热**真念了一句**，不是只加载
        assert service.health().ready is True
        assert service.health().detail is None

    def test_warmup_without_any_usable_voice_still_loads(self, paths: StudioPaths) -> None:
        """没有可用参考音 ⇒ 只加载不试念，**如实**说跳过了什么（不假装成功）。"""
        (paths.voice_src_dir / "bigbear" / "ref.txt").unlink()
        result = make_service(paths).warmup()
        assert result.ready is True
        assert result.synth_ms is None


# ── 音色 ────────────────────────────────────────────────────────────────


class TestVoices:
    def test_lists_what_this_engine_can_actually_clone(self, paths: StudioPaths) -> None:
        voices = {item.id: item for item in make_service(paths).voices().voices}
        assert voices["bigbear"].usable is True
        assert voices["bigbear"].origin == "recorded"
        assert voices["littlebear"].usable is False
        assert "ref.txt" in voices["littlebear"].detail

    def test_unknown_voice_says_what_is_available(self, paths: StudioPaths) -> None:
        with pytest.raises(StudioError) as caught:
            make_service(paths).synth(SynthRequest(text="喂", voice_id="nope", task_id="t1", seq=1))
        assert caught.value.code is ErrorCode.TTS_VOICE_MISSING
        assert caught.value.context["available"] == ["bigbear"]

    def test_registry_is_empty_when_the_dir_is_missing(self, tmp_path: Path) -> None:
        assert VoiceRegistry(tmp_path / "nope").list() == []


# ── 参考音 ⇒ 一段 prompt（裁定 377）────────────────────────────────────


def _voice_paths(tmp_path: Path, *, segments: int, lines: int) -> StudioPaths:
    """造一个音色目录：``segments`` 段 wav + ``lines`` 行 ref.txt。"""
    home = tmp_path / "studio"
    data = home / "data"
    root = data / "voice_src" / "bigbear"
    root.mkdir(parents=True)
    for index in range(1, segments + 1):
        (root / f"ref_{index:02d}.wav").write_bytes(b"RIFF")
    (root / "ref.txt").write_text(
        "\n".join(f"第{index}句" for index in range(1, lines + 1)), encoding="utf-8"
    )
    return StudioPaths(home=home, data_dir=data)


def _recorder(calls: list[list[str]]) -> Any:
    """假的 ``run_command``：只记下 argv，不真跑 ffmpeg。"""

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        target = Path(argv[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF")
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="", tail=lambda limit=400: "")

    return run


class TestPromptBuild:
    """参考音怎么变成**一段** prompt —— 引擎一次只吃一段，所以拼是我们的事。

    这里验的是"多段真的参与"（裁定 377）：改之前 ``resolve`` 只取 ``wavs[0]``，
    第 2、3 段**从来没进过引擎** —— 用户给的参考音越多，白存的越多。
    """

    def test_one_segment_is_used_as_is_without_copying(self, tmp_path: Path) -> None:
        """单段**不复制、不过 ffmpeg**：那是改之前的行为，零开销。"""
        paths = _voice_paths(tmp_path, segments=1, lines=1)
        ref = VoiceRegistry(paths.voice_src_dir).resolve("bigbear")
        target = paths.tmp_dir / "tts_prompt" / "bigbear.wav"

        prompt = build_prompt(ref, target)

        assert prompt.wav == ref.ref_wavs[0]
        assert prompt.text == "第1句"
        assert prompt.used == ("ref_01.wav",)
        assert prompt.dropped == ()
        assert not target.exists()  # 一个字节都没写

    def test_every_segment_goes_into_the_prompt(self, tmp_path: Path) -> None:
        paths = _voice_paths(tmp_path, segments=3, lines=3)
        ref = VoiceRegistry(paths.voice_src_dir).resolve("bigbear")
        calls: list[list[str]] = []
        target = paths.tmp_dir / "tts_prompt" / "bigbear.wav"

        prompt = build_prompt(ref, target, duration_ms=lambda _path: 3_000, runner=_recorder(calls))

        assert prompt.used == ("ref_01.wav", "ref_02.wav", "ref_03.wav")
        assert prompt.dropped == ()
        assert prompt.text == "第1句。第2句。第3句"
        assert prompt.wav == target
        assert len(calls) == 1
        # 三路输入 + concat：少一路就是少一段原声
        assert calls[0].count("-i") == 3
        assert "concat=n=3:v=0:a=1[out]" in calls[0][calls[0].index("-filter_complex") + 1]

    def test_segments_that_do_not_fit_are_dropped_and_reported(self, tmp_path: Path) -> None:
        """超过 prompt 上限的段**整段丢掉**，而且**说出来** —— 不截半句。

        截音频就得同时截文本（两者必须逐字对应），而"截到第几个字"没有可靠答案。
        """
        paths = _voice_paths(tmp_path, segments=3, lines=3)
        ref = VoiceRegistry(paths.voice_src_dir).resolve("bigbear")
        target = paths.tmp_dir / "tts_prompt" / "bigbear.wav"

        prompt = build_prompt(ref, target, duration_ms=lambda _path: 20_000, runner=_recorder([]))

        assert prompt.used == ("ref_01.wav",)
        assert prompt.dropped == ("ref_02.wav", "ref_03.wav")
        assert prompt.text == "第1句"

    def test_the_cap_leaves_room_under_the_engine_hard_limit(self) -> None:
        """上限必须**低于**引擎的 30 秒硬断言，还要容得下一段静音。

        引擎那条是 ``assert speech.shape[1] / 16000 <= 30`` —— 超了是**当场抛异常**，
        不是降级。留 1 秒是给重采样取整的余量。
        """
        assert VOICE_PROMPT_MAX_MS < 30_000
        assert VOICE_PROMPT_MAX_MS + VOICE_PROMPT_GAP_MS < 30_000

    def test_a_segment_without_its_text_is_not_usable(self, tmp_path: Path) -> None:
        """没有对应逐字文本的段进不了 prompt —— 文本与音频必须一一对应。"""
        paths = _voice_paths(tmp_path, segments=3, lines=2)
        ref = VoiceRegistry(paths.voice_src_dir).resolve("bigbear")
        assert len(ref.ref_wavs) == 2
        assert len(ref.ref_texts) == 2


class TestPromptBuildForReal:
    """真 ffmpeg 拼一次 —— 滤镜图写错了，假件永远发现不了。"""

    def test_real_concat_is_as_long_as_the_pieces_plus_the_gaps(self, tmp_path: Path) -> None:
        paths = _voice_paths(tmp_path, segments=2, lines=2)
        root = paths.voice_src_dir / "bigbear"
        for name in ("ref_01.wav", "ref_02.wav"):
            _write_tone(root / name, seconds=1.0)
        ref = VoiceRegistry(paths.voice_src_dir).resolve("bigbear")

        prompt = build_prompt(ref, paths.tmp_dir / "tts_prompt" / "bigbear.wav")

        info = probe_media(prompt.wav)
        assert info.duration_ms == pytest.approx(2_000 + VOICE_PROMPT_GAP_MS, abs=120)
        assert info.sample_rate == 24_000
        assert info.channels == 1


def _write_tone(path: Path, *, seconds: float, rate: int = 24_000) -> None:
    """写一段真的 PCM wav（方波）—— 拼接那一步必须喂真的能解码的东西。"""
    frames = int(seconds * rate)
    peak = int(0.5 * 32767)
    period = max(2, rate // 200)
    half = period // 2
    high = peak.to_bytes(2, "little", signed=True) * half
    low = (-peak).to_bytes(2, "little", signed=True) * (period - half)
    body = ((high + low) * (frames // period + 1))[: frames * 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(body)


# ── 落盘边界 ────────────────────────────────────────────────────────────


class TestOutPath:
    def test_derives_the_path_from_task_and_seq(self, paths: StudioPaths) -> None:
        target = resolve_out_path(SynthRequest(text="喂", voice_id="bigbear", task_id="t1", seq=3), paths)
        assert target == paths.tts_work_dir_for("t1") / "s003.wav"

    @pytest.mark.parametrize("bad", ["../evil.wav", "C:/evil.wav", "/etc/passwd"])
    def test_rejects_anything_outside_data(self, paths: StudioPaths, bad: str) -> None:
        with pytest.raises(StudioError) as caught:
            resolve_out_path(SynthRequest(text="喂", voice_id="bigbear", out_path=bad), paths)
        assert caught.value.code is ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS

    def test_requires_out_path_or_task_and_seq(self, paths: StudioPaths) -> None:
        with pytest.raises(StudioError) as caught:
            resolve_out_path(SynthRequest(text="喂", voice_id="bigbear"), paths)
        assert caught.value.code is ErrorCode.VALIDATION_FAILED

    def test_synth_writes_where_it_says_it_writes(self, paths: StudioPaths) -> None:
        response = make_service(paths).synth(SynthRequest(text="喂", voice_id="bigbear", task_id="t1", seq=1))
        assert response.out_path == "work/t1/tts/s001.wav"
        assert (paths.data_dir / response.out_path).is_file()


# ── 背压与超时 ──────────────────────────────────────────────────────────


class TestBackpressure:
    def test_queue_overflow_is_429_not_a_silent_wait(self, paths: StudioPaths) -> None:
        service = make_service(paths, backend=FakeBackend(delay_sec=0.4))
        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def hit() -> None:
            barrier.wait()
            try:
                service.synth(SynthRequest(text="喂", voice_id="bigbear", task_id="t1", seq=1))
                outcomes.append("ok")
            except StudioError as exc:
                outcomes.append(str(exc.code))

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert outcomes.count(str(ErrorCode.TTS_BUSY)) >= 1

    def test_timeout_marks_the_service_stuck_instead_of_pretending(self, paths: StudioPaths) -> None:
        service = make_service(
            paths,
            backend=FakeBackend(delay_sec=1.5),
            server=TtsServerConfig(concurrency=1, queue_max=4, request_timeout_sec=1),
        )
        with pytest.raises(StudioError) as caught:
            service.synth(SynthRequest(text="喂", voice_id="bigbear", task_id="t1", seq=1))
        assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED
        health = service.health()
        assert health.ready is False
        assert health.detail is not None and "超时" in health.detail
        service.shutdown()

    def test_engine_errors_propagate_with_their_own_code(self, paths: StudioPaths) -> None:
        boom = StudioError("显存没了", code=ErrorCode.TTS_OOM)
        service = make_service(paths, backend=FakeBackend(error=boom))
        with pytest.raises(StudioError) as caught:
            service.synth(SynthRequest(text="喂", voice_id="bigbear", task_id="t1", seq=1))
        assert caught.value.code is ErrorCode.TTS_OOM


# ── 空闲卸载 ────────────────────────────────────────────────────────────


class TestIdleUnload:
    def test_unloads_only_after_the_idle_window(self, paths: StudioPaths) -> None:
        clock = [0.0]
        service = make_service(paths, clock=clock, server=TtsServerConfig(idle_unload_min=20))
        service.warmup()
        clock[0] = 19 * 60
        assert service.unload_if_idle() == 0  # 还没到
        clock[0] = 20 * 60 + 1
        assert service.unload_if_idle() == 2400  # 到了 ⇒ 释放
        assert service.health().model_state == "unloaded"

    def test_idle_check_is_idempotent(self, paths: StudioPaths) -> None:
        clock = [0.0]
        service = make_service(paths, clock=clock, server=TtsServerConfig(idle_unload_min=1))
        clock[0] = 3600
        assert service.unload_if_idle() == 0  # 本来就没加载


# ── HTTP 面 ─────────────────────────────────────────────────────────────


class TestHttp:
    @pytest.fixture
    def client(self, paths: StudioPaths) -> Any:
        service = make_service(paths)
        with TestClient(create_app(service)) as client:
            yield client
        service.shutdown()

    def test_health_matches_the_acceptance_shape(self, client: Any) -> None:
        payload = client.get("/health").json()
        assert payload["ok"] is True
        assert payload["device"] == "cuda"
        assert payload["model_state"] == "unloaded"
        # 自报 pid：编排器靠它认出「8788 上那个进程是我的哪一个实例」——
        # 端口被一个坏掉的旧实例占着时，光看端口占用查不出是谁（陷阱 166）
        assert payload["pid"] == os.getpid()

    def test_warmup_then_synth_over_http(self, client: Any) -> None:
        assert client.post("/warmup", json={}).json()["ready"] is True
        body = {"text": "喂", "voice_id": "bigbear", "out_path": "work/t1/tts/s001.wav"}
        payload = client.post("/synth", json=body).json()
        assert payload["out_path"] == "work/t1/tts/s001.wav"
        assert payload["engine"] == "cosyvoice2"
        # 用上了哪几段要在响应里说清楚（裁定 377）：单段音色就是它自己那一段
        assert payload["ref_wavs"] == ["ref_01.wav"]
        assert payload["dropped_refs"] == []

    def test_unload_reports_freed_vram(self, client: Any) -> None:
        client.post("/warmup", json={})
        assert client.post("/unload", json={}).json()["freed_mb"] == 2400

    def test_error_envelope_carries_code_and_remediation(self, client: Any) -> None:
        body = {"text": "喂", "voice_id": "nope", "task_id": "t1", "seq": 1}
        response = client.post("/synth", json=body)
        assert response.status_code == 422
        payload = response.json()
        assert payload["code"] == "TTS_VOICE_MISSING"
        assert payload["remediation"]

    def test_registration_is_not_here_and_says_where_it_is(self, client: Any) -> None:
        """**501 而不是假装成功**：注册在 T2.4 的入库路径上（裁定 305）。"""
        assert client.post("/voices").status_code == 501
        assert client.delete("/voices/bigbear").status_code == 501
        assert "ingest_voice_src" in client.post("/voices").json()["remediation"]

    def test_unknown_field_is_rejected(self, client: Any) -> None:
        body = {"text": "喂", "voice_id": "bigbear", "outpath": "x.wav", "seq": 1}
        assert client.post("/synth", json=body).status_code == 422
