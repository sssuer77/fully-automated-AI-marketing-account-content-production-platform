"""降级链（T3.7 · §04.2.8.6）。

:func:`deliver` 的职责只有一条：**正常档失败时换 720P 再试一次**。这一层不跑 ffmpeg ——
把 ``run_composite`` 换成假件，"第一次失败、第二次成功"这种时序才测得准（真跑的话要
先想办法让一次编码真的失败，那既慢又不稳）。

真的端到端验证在 `tests/integration/test_render_pipeline.py` 的黑屏用例里。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from studio.core.config import OutputsConfig
from studio.core.errors import ErrorCode, RenderError
from studio.render import degrade
from studio.render.composite import CompositeRequest, CompositeResult
from studio.render.profiles import FALLBACK_PROFILE_NAME, resolve_profile

from .conftest import write_png  # noqa: F401  （保持与同目录其它用例一致的导入风格）


def _request(outputs: OutputsConfig, tmp_path: Path, **overrides: object) -> CompositeRequest:
    base: dict[str, object] = {
        "profile": resolve_profile(outputs),
        "clip": tmp_path / "clip.mp4",
        "voice": tmp_path / "voice.wav",
        "output": tmp_path / "out.mp4",
        "duration_ms": 12_345,
    }
    base.update(overrides)
    return CompositeRequest(**base)  # type: ignore[arg-type]


def _result(request: CompositeRequest, **overrides: object) -> CompositeResult:
    base: dict[str, object] = {
        "output": request.output,
        "duration_ms": request.duration_ms,
        "size_bytes": 1_024,
        "watermark_applied": False,
        "bgm_applied": False,
        "subtitle_applied": False,
        "bg_fill": "broll",
        "loudness": None,
        "warnings": (),
        "argv": ("ffmpeg", str(request.output)),
    }
    base.update(overrides)
    return CompositeResult(**base)  # type: ignore[arg-type]


class _Script:
    """按脚本依次成功 / 失败的假合成器（记录每一次真的被调用了）。"""

    def __init__(self, *errors: RenderError | None) -> None:
        self._errors = list(errors)
        self.calls: list[CompositeRequest] = []

    def __call__(self, request: CompositeRequest, **_kwargs: object) -> CompositeResult:
        self.calls.append(request)
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        return _result(request)


def _boom(code: ErrorCode = ErrorCode.RENDER_FAILED) -> RenderError:
    return RenderError("编码器炸了", code=code)


def _patch(monkeypatch: pytest.MonkeyPatch, script: _Script) -> _Script:
    monkeypatch.setattr(degrade, "run_composite", script)
    return script


# ══════════════════════════════════════════════════════════════════════
# 正常路径
# ══════════════════════════════════════════════════════════════════════


def test_a_clean_render_is_not_marked_degraded(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _patch(monkeypatch, _Script())
    delivery = degrade.deliver(_request(outputs, tmp_path))
    assert delivery.degraded is False
    assert delivery.degrade_reason is None
    assert delivery.attempts == ()
    assert len(script.calls) == 1


# ══════════════════════════════════════════════════════════════════════
# 720P 保底
# ══════════════════════════════════════════════════════════════════════


def test_a_failed_encode_falls_back_to_720p(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 正常档失败 ⇒ 换保底档重试**一次**，并把这件事留痕。"""
    script = _patch(monkeypatch, _Script(_boom()))
    fallback = resolve_profile(outputs, FALLBACK_PROFILE_NAME)
    fallback_output = tmp_path / "out_720p.mp4"

    delivery = degrade.deliver(
        _request(outputs, tmp_path), fallback_profile=fallback, fallback_output=fallback_output
    )

    assert len(script.calls) == 2
    # 第二次真的换了档、也换了落盘路径
    assert script.calls[1].profile.name == FALLBACK_PROFILE_NAME
    assert script.calls[1].output == fallback_output
    # 其它输入一个字都没动（降级只该降档位）
    assert script.calls[1].duration_ms == script.calls[0].duration_ms
    assert script.calls[1].voice == script.calls[0].voice

    assert delivery.degraded is True
    assert delivery.degrade_reason == degrade.RENDER_720P
    assert delivery.profile.name == FALLBACK_PROFILE_NAME
    assert delivery.attempts == (f"{script.calls[0].profile.name}: RENDER_FAILED",)
    assert any("720P" in item for item in delivery.warnings)


def test_the_timeout_is_also_worth_a_retry(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时也换档重试：保底档更小更快，正是超时最可能救回来的一档。"""
    _patch(monkeypatch, _Script(_boom(ErrorCode.RENDER_TIMEOUT)))
    delivery = degrade.deliver(
        _request(outputs, tmp_path),
        fallback_profile=resolve_profile(outputs, FALLBACK_PROFILE_NAME),
        fallback_output=tmp_path / "out_720p.mp4",
    )
    assert delivery.degrade_reason == degrade.RENDER_720P


def test_both_attempts_failing_raises_the_original_error(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 保底档也失败 ⇒ 抛出去，交给**重试链**（退避 / 死信 / manual_pool）。

    这里不自己再试第三遍：保底档换的是编码器与分辨率，"机器根本没有这个编码器"不会
    因为多试几次而变好 —— 硬撑只会把真正的报错埋到第三层。
    """
    script = _patch(monkeypatch, _Script(_boom(), _boom()))
    with pytest.raises(RenderError):
        degrade.deliver(
            _request(outputs, tmp_path),
            fallback_profile=resolve_profile(outputs, FALLBACK_PROFILE_NAME),
            fallback_output=tmp_path / "out_720p.mp4",
        )
    assert len(script.calls) == 2  # 就两次，没有第三次


def test_an_error_that_a_retry_cannot_fix_is_not_retried(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 只重试合成器自己抛的那两个码 —— "字体缺失"换分辨率也一样失败。

    重试它不只是浪费时间：它会把一个**指向根因**的报错（缺字体）换成第二层那个
    更含糊的报错，排障时先看到的是错的那条。
    """
    script = _patch(monkeypatch, _Script(_boom(ErrorCode.FONT_MISSING)))
    with pytest.raises(RenderError) as excinfo:
        degrade.deliver(
            _request(outputs, tmp_path),
            fallback_profile=resolve_profile(outputs, FALLBACK_PROFILE_NAME),
            fallback_output=tmp_path / "out_720p.mp4",
        )
    assert excinfo.value.code is ErrorCode.FONT_MISSING
    assert len(script.calls) == 1


def test_without_a_fallback_profile_the_error_goes_straight_out(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没给保底档 ⇒ 不做回退（"只想要这一档"的调用方与测试走这条路）。"""
    script = _patch(monkeypatch, _Script(_boom()))
    with pytest.raises(RenderError):
        degrade.deliver(_request(outputs, tmp_path))
    assert len(script.calls) == 1


# ══════════════════════════════════════════════════════════════════════
# 三条轴是独立的
# ══════════════════════════════════════════════════════════════════════


def test_black_fill_does_not_mark_the_delivery_degraded(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 黑屏降级**不**在这里判定：它降的是画面来源（输入），不是档位（编码）。

    这条钉的是"三条轴独立"这个设计 —— 早先把它们写成一条线（正常 → 720P → 黑屏），
    会得到"720P 失败了再黑屏"这种没有意义的时序：黑屏是**素材缺失**，不是编码失败的
    补救。所以 ``degrade_reason`` 由服务层按 ``clip is None`` 单独判。
    """
    script = _patch(monkeypatch, _Script())
    delivery = degrade.deliver(_request(outputs, tmp_path, clip=None))
    assert delivery.degraded is False
    assert delivery.degrade_reason is None
    assert delivery.composite.bg_fill == "broll"  # 假件照原样返回，与降级判定无关
    assert len(script.calls) == 1


def test_the_retry_reuses_every_other_input_untouched(
    outputs: OutputsConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """降级只该换档位与落盘路径 —— 其它字段动一个，就变成"另一支片子"了。"""
    script = _patch(monkeypatch, _Script(_boom()))
    request = _request(outputs, tmp_path, threads=4)
    degrade.deliver(
        request,
        fallback_profile=resolve_profile(outputs, FALLBACK_PROFILE_NAME),
        fallback_output=tmp_path / "out_720p.mp4",
    )
    retried = script.calls[1]
    assert replace(retried, profile=request.profile, output=request.output) == request
