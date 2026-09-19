"""契约：配音引擎 ABC 与降级决策表（T2.3 · §04.3.2 / §04.3.3）。

为什么值得一条契约测试
----------------------
§04.3.2 要求"实现可替换：CosyVoice 服务 / 备用引擎 / Mock"，而**"可替换"这件事
不会在单元测试里报错**：少一个 ``health()`` 只会让决策表在真机上退化成"重试"
（那正是 T2.3 之前的样子）。所以这里钉住三件事：

1. 三个实现（Mock / 系统语音包 / 常驻服务）都满足 :class:`VoiceEngine`；
2. §04.3.3 的七值枚举、失败类型与两条阈值**逐字**在代码里（改表就改灯）；
3. 两处**故意偏离**规格（同步而非 ``async def``、策略吃错误码字符串而非异常对象）
   有据可查 —— 偏离写在模块 docstring 里，这里验它确实写下来了。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from studio.core.paths import StudioPaths
from studio.tts import engine as engine_module
from studio.tts import fallback as fallback_module
from studio.tts import sentence as sentence_module
from studio.tts.engine import EngineHealth, SentenceEngine, VoiceEngine, rewarm
from studio.tts.fallback import DegradeAction, FailureState, decide
from studio.tts.sentence import CLIP_PEAK_DBFS, SILENCE_RMS_DBFS, SapiEngine
from studio.tts.service_engine import ResidentEngine, ResidentStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DOC = REPO_ROOT / "docs" / "spec" / "04-contracts.md"

#: §04.3.2 的三个生命周期原语（``synthesize`` 是 T2.6 就有的最小缝）
SPEC_LIFECYCLE = ("warmup", "unload", "health")

#: §04.3.3 那张表里出现的失败类型
SPEC_FAILURE_CODES = (
    "TTS_OOM",
    "TTS_TIMEOUT",
    "TTS_SILENT",
    "TTS_CLIP",
    "TTS_TEXT_INVALID",
    "TTS_ENGINE_DOWN",
    "TTS_VOICE_MISSING",
)


def _section(heading: str, *, span: int = 6_000) -> str:
    body = CONTRACTS_DOC.read_text(encoding="utf-8")
    start = body.index(heading)
    return body[start : start + span]


def _paths(tmp_path: Path) -> StudioPaths:
    home = tmp_path / "studio"
    return StudioPaths(home=home, data_dir=home / "data")


def _resident_status() -> ResidentStatus:
    return ResidentStatus(
        base_url="http://127.0.0.1:8788",
        engine="cosyvoice2",
        revision="074ca6dc",
        ready=True,
        device="cuda",
        model_state="ready",
        sample_rate=24_000,
        voices=("littlebear",),
    )


class TestTheSpecIsQuotedVerbatim:
    def test_the_seven_actions_are_in_the_doc(self) -> None:
        body = _section("### 4.3.3")
        for action in DegradeAction:
            assert action.value in body, action.value

    def test_the_doc_lists_the_same_failure_types(self) -> None:
        body = _section("### 4.3.3")
        for code in SPEC_FAILURE_CODES:
            assert code in body, code

    def test_the_qc_thresholds_match_the_doc(self) -> None:
        """表里写的判据是 ``RMS < -50 dBFS`` / ``peak > -0.5 dBFS``。"""
        body = _section("### 4.3.3")
        assert "RMS < -50 dBFS" in body
        assert "peak > -0.5 dBFS" in body
        assert SILENCE_RMS_DBFS == -50.0
        assert CLIP_PEAK_DBFS == -0.5

    def test_the_circuit_thresholds_are_in_the_doc(self) -> None:
        body = _section("### 4.3.3")
        assert "≥3 句" in body
        assert "池暂停 5min" in body


class TestTheAbc:
    def test_the_lifecycle_primitives_are_required(self) -> None:
        for name in SPEC_LIFECYCLE:
            assert callable(getattr(VoiceEngine, name)), name

    def test_the_minimal_seam_is_a_superset(self) -> None:
        assert SentenceEngine in VoiceEngine.__mro__

    def test_the_minimal_seam_is_re_exported(self) -> None:
        """``studio.tts.sentence.SentenceEngine`` 是**同一件东西** —— T2.6 的调用方
        照旧 import 它（定义搬去了 ``engine.py``，但名字不能有两个）。"""
        assert sentence_module.SentenceEngine is SentenceEngine

    def test_voice_registration_is_deliberately_absent(self) -> None:
        """音色的入库判定只有 ``AssetService.ingest`` 一份（裁定 305：服务端
        ``POST|DELETE /voices`` ⇒ 501）。引擎缝上再实现一遍 = 两份判定迟早对不上。
        "这台引擎念得出哪些音色"由 ``health().voices`` 回答 —— 它是**只读**的。"""
        for name in ("register_voice", "list_voices", "unregister_voice"):
            assert not hasattr(VoiceEngine, name), name

    def test_the_sync_deviation_is_recorded(self) -> None:
        """规格写的是 ``async def``，代码是同步的 —— 偏离必须**写在 docstring 里**，
        否则下一个人会以为这是漏了。"""
        doc = engine_module.__doc__ or ""
        assert "async" in doc
        assert not inspect.iscoroutinefunction(VoiceEngine.synthesize)

    def test_the_policy_signature_deviation_is_recorded(self) -> None:
        """规格的 ``FallbackPolicy.decide`` 吃 ``err`` 对象；这里吃**错误码字符串**
        （码有四个来源，不是一个类）。理由同样必须写在 docstring 里。"""
        doc = fallback_module.__doc__ or ""
        assert "错误码字符串" in doc
        parameters = set(inspect.signature(fallback_module.FallbackPolicy.decide).parameters)
        assert parameters == {"self", "code", "state"}


class TestTheImplementations:
    def test_a_mock_satisfies_it(self) -> None:
        class MockEngine:
            name = "mock"
            revision = "test"

            def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
                del text, out_path, voice, rate

            def warmup(self) -> None:
                return None

            def unload(self) -> None:
                return None

            def health(self) -> EngineHealth:
                return EngineHealth(engine=self.name, ready=True)

        assert isinstance(MockEngine(), VoiceEngine)

    def test_the_system_voice_pack_satisfies_it(self) -> None:
        """降级档也必须满足**同一份**契约 —— 决策表不该知道对面是哪台引擎。"""
        assert isinstance(SapiEngine(), VoiceEngine)

    def test_the_resident_service_satisfies_it(self, tmp_path: Path) -> None:
        engine = ResidentEngine(paths=_paths(tmp_path), status=_resident_status())
        assert isinstance(engine, VoiceEngine)

    def test_health_is_usable_when_it_is_only_sleeping(self) -> None:
        """``ready=False`` + ``wakeable=True``（空闲卸载）**算可用** —— 混成一种会
        同时犯两个错（杀一个健康实例 / 悄悄退回系统语音包，陷阱 #168）。"""
        sleeping = EngineHealth(engine="cosyvoice2", ready=False, wakeable=True, model_state="unloaded")
        assert sleeping.usable is True
        broken = EngineHealth(engine="cosyvoice2", ready=False, model_state="error")
        assert broken.usable is False
        ready = EngineHealth(engine="cosyvoice2", ready=True)
        assert ready.usable is True

    def test_health_serializes_for_the_panel(self) -> None:
        payload = EngineHealth(engine="cosyvoice2", ready=True, voices=("littlebear",)).to_dict()
        assert payload["usable"] is True
        assert payload["voices"] == ["littlebear"]


class TestRewarm:
    def test_it_unloads_before_it_warms_up(self) -> None:
        """顺序不能反：``TTS_OOM`` 时"直接载"会在显存已经被自己占着的时候**再炸一次**。"""
        calls: list[str] = []

        class Engine:
            name = "fake"
            revision = "test"

            def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
                del text, out_path, voice, rate

            def warmup(self) -> None:
                calls.append("warmup")

            def unload(self) -> None:
                calls.append("unload")

            def health(self) -> EngineHealth:
                return EngineHealth(engine=self.name, ready=True)

        rewarm(Engine())
        assert calls == ["unload", "warmup"]

    def test_a_failed_unload_does_not_stop_the_reload(self) -> None:
        """卸载失败（服务正忙 / 已经没在跑 / 没有显存可放）**不该阻止重载** ——
        那种时候重载正是要做的事。"""
        calls: list[str] = []

        class Engine:
            name = "fake"
            revision = "test"

            def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
                del text, out_path, voice, rate

            def warmup(self) -> None:
                calls.append("warmup")

            def unload(self) -> None:
                raise RuntimeError("服务正忙")

            def health(self) -> EngineHealth:
                return EngineHealth(engine=self.name, ready=True)

        rewarm(Engine())
        assert calls == ["warmup"]


def test_the_table_agrees_with_the_doc_for_every_row() -> None:
    """把 §04.3.3 的表**当作数据**读一遍：每一行的"失败类型 ⇒ 动作"都要能对上
    代码里的结论。这一条是"文档改了代码没改"的兜底（前几类由
    ``tests/unit/tts/test_fallback.py`` 逐行钉住）。"""
    rows = [
        ("TTS_OOM", 1, DegradeAction.REWARM_ENGINE),
        ("TTS_OOM", 2, DegradeAction.SWITCH_ENGINE),
        ("TTS_TIMEOUT", 1, DegradeAction.SPLIT_AND_MERGE),
        ("TTS_SILENT", 1, DegradeAction.RETRY_SIMPLIFIED),
        ("TTS_CLIP", 1, DegradeAction.RETRY_SIMPLIFIED),
        ("TTS_TEXT_INVALID", 1, DegradeAction.SPLIT_AND_MERGE),
        ("TTS_ENGINE_DOWN", 1, DegradeAction.REWARM_ENGINE),
        ("TTS_VOICE_MISSING", 1, DegradeAction.FAIL_TASK),
    ]
    for code, attempt, expected in rows:
        state = FailureState(attempt=attempt, split_available=True)
        assert decide(code=code, state=state) is expected, f"{code} @ attempt={attempt}"
