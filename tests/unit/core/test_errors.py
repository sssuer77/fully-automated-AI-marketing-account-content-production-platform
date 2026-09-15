"""错误码与异常基类（§02.4 / DoD 5 · T1.1）。"""

from __future__ import annotations

import pytest

from studio.core.errors import (
    ConfigError,
    DoctorGateError,
    ErrorCode,
    QueueError,
    RenderError,
    StudioError,
    TtsError,
)


def test_error_codes_are_unique_and_uppercase() -> None:
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values))
    assert all(value == value.upper() for value in values)
    assert all(" " not in value for value in values)


def test_subsystem_prefixes_present() -> None:
    values = {member.value for member in ErrorCode}
    assert {"RENDER_WATERMARK_MISSING", "TTS_OOM", "PUBLISH_LOGIN_EXPIRED", "JOB_DEAD"} <= values


def test_base_error_defaults_to_internal() -> None:
    error = StudioError("boom")
    assert error.code is ErrorCode.INTERNAL
    assert str(error) == "[INTERNAL] boom"
    assert error.to_dict()["type"] == "StudioError"


def test_subclass_default_codes() -> None:
    assert ConfigError("x").code is ErrorCode.CONFIG_INVALID
    assert TtsError("x").code is ErrorCode.TTS_ENGINE_UNAVAILABLE
    assert RenderError("x").code is ErrorCode.RENDER_FAILED
    assert QueueError("x").code is ErrorCode.JOB_LEASE_LOST
    assert DoctorGateError("x").code is ErrorCode.ENV_CONTRACT_VIOLATION


def test_explicit_code_and_context_override() -> None:
    error = RenderError("水印缺失", code=ErrorCode.RENDER_WATERMARK_MISSING, context={"path": "a.png"})
    payload = error.to_dict()
    assert payload["code"] == "RENDER_WATERMARK_MISSING"
    assert payload["context"] == {"path": "a.png"}


def test_remediation_is_carried() -> None:
    error = ConfigError("缺字段", remediation="补 persona.yaml")
    assert error.remediation == "补 persona.yaml"


@pytest.mark.parametrize("error", [StudioError("a"), ConfigError("b"), TtsError("c")])
def test_errors_are_catchable_as_base(error: StudioError) -> None:
    with pytest.raises(StudioError):
        raise error
