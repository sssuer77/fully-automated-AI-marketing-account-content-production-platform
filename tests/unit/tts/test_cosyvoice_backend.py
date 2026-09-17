"""``tts.cosyvoice``（T2.2）—— 推理后端的边界行为。

这些用例在**主 venv**（Python 3.12、没有 torch）里跑，这本身就是一条断言：
后端模块必须能在没有 GPU 的机器上被 import —— 服务管理器的就绪判据、``/health``
的形状、这一整个文件都依赖它。所以 torch / cosyvoice 只在 :meth:`load` 里进来，
而下面的用例正好把"进不来的时候报什么"钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.tts.cosyvoice import CosyVoiceBackend, EngineState, looks_like_oom


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """一个"看起来像权重目录"的空目录（真权重 5 GB，测试不碰它）。"""
    root = tmp_path / "model"
    root.mkdir()
    (root / "cosyvoice2.yaml").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def backend(model_dir: Path) -> CosyVoiceBackend:
    return CosyVoiceBackend(model_dir, revision="testrev")


class TestModuleBoundary:
    def test_importing_the_backend_does_not_pull_torch(self) -> None:
        """★ 模块级只 import 标准库 —— 没有 GPU 的机器也要 import 得动。"""
        assert "torch" not in sys.modules

    def test_load_without_the_inference_venv_says_so(self, backend: CosyVoiceBackend) -> None:
        """主 venv 里没有 torch ⇒ 报**推理依赖缺失**，并把修复指向 tts/.venv。"""
        with pytest.raises(StudioError) as caught:
            backend.load()
        assert caught.value.code is ErrorCode.TTS_ENGINE_UNAVAILABLE
        assert "推理依赖缺失" in caught.value.message
        assert "tts/.venv" in (caught.value.remediation or "")
        assert backend.state is EngineState.ERROR


class TestPathGuards:
    def test_missing_model_dir_points_at_the_runbook(self, tmp_path: Path) -> None:
        backend = CosyVoiceBackend(tmp_path / "nope", revision="testrev")
        with pytest.raises(StudioError) as caught:
            backend.load()
        assert caught.value.code is ErrorCode.TTS_ENGINE_UNAVAILABLE
        assert "tts_models.md" in (caught.value.remediation or "")

    @pytest.mark.parametrize("field", ["source_dir", "matcha_dir"])
    def test_missing_source_or_matcha_dir_is_its_own_message(
        self, model_dir: Path, tmp_path: Path, field: str
    ) -> None:
        """Matcha-TTS 缺了报的是 ``no module named matcha``（陷阱 160）——
        所以配置期就要把它单独指出来，别让它伪装成"权重坏了"。"""
        # 另一个**必须真的存在**：两个都缺的话先报的永远是 source_dir，
        # 这条用例就验不到 matcha_dir 那一支了。
        source = tmp_path / "CosyVoice"
        matcha = tmp_path / "Matcha-TTS"
        source.mkdir()
        matcha.mkdir()
        # 显式标注：不标的话 mypy 推出 dict[str, Path]，`**kwargs` 展开到 bool / int 参数上会报错
        kwargs: dict[str, Any] = {"source_dir": source, "matcha_dir": matcha}
        kwargs[field] = tmp_path / f"missing-{field}"
        backend = CosyVoiceBackend(model_dir, revision="testrev", **kwargs)
        with pytest.raises(StudioError) as caught:
            backend.load()
        assert caught.value.context["field"] == field


class TestSysPath:
    def test_both_segments_go_in_and_stay_idempotent(self, model_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "CosyVoice"
        matcha = tmp_path / "Matcha-TTS"
        backend = CosyVoiceBackend(model_dir, revision="testrev", source_dir=source, matcha_dir=matcha)
        backend._extend_sys_path()
        backend._extend_sys_path()
        assert str(source) in sys.path
        assert str(matcha) in sys.path
        assert sys.path.count(str(source)) == 1


class TestUnload:
    def test_unload_before_load_is_a_noop(self, backend: CosyVoiceBackend) -> None:
        assert backend.unload() == 0
        assert backend.state is EngineState.UNLOADED

    def test_device_is_answered_without_loading(self, backend: CosyVoiceBackend) -> None:
        """探活不该要求先把模型读进来。"""
        assert backend.device == "unavailable"  # 主 venv 没有 torch


class TestSynthesizeGuards:
    def test_missing_reference_audio_fails_before_touching_the_engine(
        self, backend: CosyVoiceBackend, tmp_path: Path
    ) -> None:
        """参考音不在 ⇒ 报 ``TTS_VOICE_MISSING``，而且**不用加载模型**（真机上省 10 秒）。"""
        with pytest.raises(StudioError) as caught:
            backend.synthesize(
                "喂",
                tmp_path / "out.wav",
                ref_wav=tmp_path / "missing.wav",
                ref_text="随便",
            )
        assert caught.value.code is ErrorCode.TTS_VOICE_MISSING
        assert backend.state is EngineState.UNLOADED

    def test_empty_reference_text_is_rejected(self, backend: CosyVoiceBackend, tmp_path: Path) -> None:
        ref = tmp_path / "ref_01.wav"
        ref.write_bytes(b"RIFF")
        with pytest.raises(StudioError) as caught:
            backend.synthesize("喂", tmp_path / "out.wav", ref_wav=ref, ref_text="   ")
        assert caught.value.code is ErrorCode.TTS_VOICE_MISSING


class TestOomDetection:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"),
            RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"),
            type("OutOfMemoryError", (RuntimeError,), {})("boom"),
        ],
    )
    def test_recognizes_oom(self, exc: BaseException) -> None:
        assert looks_like_oom(exc) is True

    @pytest.mark.parametrize("exc", [RuntimeError("shape mismatch"), ValueError("bad input")])
    def test_does_not_swallow_other_failures(self, exc: BaseException) -> None:
        assert looks_like_oom(exc) is False
