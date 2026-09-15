"""环境变量重定向契约（§README.6 · T1.1）。"""

from __future__ import annotations

import pytest

from studio.core.errors import ConfigError, ErrorCode
from studio.core.settings import (
    DISK_GATE_FREE_C_BYTES,
    DISK_GATE_FREE_D_BYTES,
    REQUIRED_PATH_VARS,
    inspect_env_contract,
    load_env_settings,
)


def test_contract_lists_exactly_ten_path_vars() -> None:
    assert len(REQUIRED_PATH_VARS) == 11
    assert "TEMP" in REQUIRED_PATH_VARS and "TMP" in REQUIRED_PATH_VARS


def test_disk_gate_thresholds_match_spec() -> None:
    assert DISK_GATE_FREE_C_BYTES == 1024**3
    assert DISK_GATE_FREE_D_BYTES == 15 * 1024**3


def test_inspect_reports_all_ok(contract_env: dict[str, str]) -> None:
    report = inspect_env_contract(contract_env)
    assert report.ok
    assert report.missing == ()
    assert report.wrong_drive == ()
    assert {status for status, _ in report.items.values()} == {"ok"}


@pytest.mark.parametrize("variable", ["HF_HOME", "UV_CACHE_DIR", "TEMP"])
def test_inspect_flags_missing_variable(contract_env: dict[str, str], variable: str) -> None:
    env = {k: v for k, v in contract_env.items() if k != variable}
    report = inspect_env_contract(env)
    assert not report.ok
    assert report.missing == (variable,)
    assert report.items[variable] == ("missing", "")


def test_inspect_flags_system_drive(contract_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    env = dict(contract_env)
    env["TEMP"] = r"C:\Windows\Temp"
    env["TMP"] = r"C:\Windows\Temp"
    report = inspect_env_contract(env)
    assert not report.ok
    assert set(report.wrong_drive) == {"TEMP", "TMP"}
    assert report.items["HF_HOME"][0] == "ok"


def test_load_raises_on_missing(contract_env: dict[str, str]) -> None:
    env = {k: v for k, v in contract_env.items() if k != "PIP_CACHE_DIR"}
    with pytest.raises(ConfigError) as excinfo:
        load_env_settings(env)
    assert excinfo.value.code is ErrorCode.ENV_MISSING
    assert excinfo.value.context["missing"] == ["PIP_CACHE_DIR"]


def test_load_raises_on_system_drive(contract_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    env = dict(contract_env)
    env["MODELSCOPE_CACHE"] = r"C:\modelscope"
    with pytest.raises(ConfigError) as excinfo:
        load_env_settings(env)
    assert excinfo.value.code is ErrorCode.ENV_NOT_ON_D_DRIVE
    assert "modelscope_cache" in excinfo.value.context["violations"]


def test_load_succeeds_and_secrets_optional(contract_env: dict[str, str]) -> None:
    settings = load_env_settings(contract_env)
    assert settings.system_drive_violations() == []
    assert not settings.has_llm_key()


def test_load_reads_llm_key(contract_env: dict[str, str]) -> None:
    env = dict(contract_env) | {"STUDIO_LLM_API_KEY": "sk-test"}
    settings = load_env_settings(env)
    assert settings.has_llm_key()
    assert "sk-test" not in repr(settings)


def test_load_rejects_relative_path(contract_env: dict[str, str]) -> None:
    env = dict(contract_env) | {"TORCH_HOME": "relative\\path"}
    with pytest.raises(ConfigError) as excinfo:
        load_env_settings(env)
    assert excinfo.value.code is ErrorCode.ENV_CONTRACT_VIOLATION
