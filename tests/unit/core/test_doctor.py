"""启动自检（T1.1 验收核心 · §README.6 / §01.6）。"""

from __future__ import annotations

import json
import os

import pytest

import studio.core.doctor as doctor_module
from studio.core.doctor import (
    REQUIRED_FILTERS,
    CheckResult,
    Doctor,
    DoctorReport,
    _parse_filter_names,
    dumps,
    render_text,
)
from studio.core.errors import ConfigError, DoctorGateError, ErrorCode
from studio.core.paths import StudioPaths

EXPECTED_CHECK_NAMES = {
    "env.contract",
    "config.valid",
    "persona.library",
    "paths.runtime",
    "disk.gate",
    "python.app",
    "python.tts",
    "torch.cuda",
    "ffmpeg.binary",
    "ffmpeg.filters",
    "ffmpeg.nvenc",
    "fonts.available",
    "db.writable",
    "sqlite.version",
    "system.ram",
    "gpu.nvidia",
    "node.version",
    "uv.version",
}

_FILTERS_SAMPLE = """
Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 ... abench            A->A       Benchmark part of a filtergraph.
 ..C ass               V->V       Render ASS subtitles.
 ... amix              N->A       Audio mixing.
 ... overlay_cuda      VV->V      Overlay one video on top of another.
"""


def _report(*checks: CheckResult) -> DoctorReport:
    return DoctorReport(checks=checks, host={}, duration_ms=1, started_at="2026-09-13T00:00:00.000000Z")


def test_parse_filter_names_reads_second_column() -> None:
    names = _parse_filter_names(_FILTERS_SAMPLE)
    assert {"abench", "ass", "amix", "overlay_cuda"} <= names
    assert "Filters:" not in names
    assert "Timeline" not in names


def test_required_filter_list_matches_spec() -> None:
    assert set(REQUIRED_FILTERS) == {
        "ass",
        "subtitles",
        "xfade",
        "loudnorm",
        "sidechaincompress",
        "overlay_cuda",
        "zscale",
        "alimiter",
        "amix",
    }


def test_report_ok_when_only_warnings() -> None:
    report = _report(
        CheckResult("env.contract", "ok", "fine", blocking=True),
        CheckResult("system.ram", "warn", "low", blocking=False),
    )
    assert report.ok
    assert report.blocking_failures == ()
    assert [c.name for c in report.warnings] == ["system.ram"]


def test_report_not_ok_on_blocking_failure() -> None:
    report = _report(
        CheckResult("disk.gate", "fail", "C 盘不足", blocking=True, remediation="清理磁盘"),
        CheckResult("ffmpeg.nvenc", "fail", "无 NVENC", blocking=False),
    )
    assert not report.ok
    assert [c.name for c in report.blocking_failures] == ["disk.gate"]


def test_raise_if_blocked_carries_remediation() -> None:
    report = _report(CheckResult("env.contract", "fail", "缺 3 项", blocking=True))
    with pytest.raises(DoctorGateError) as excinfo:
        report.raise_if_blocked()
    assert "env.contract" in str(excinfo.value)
    assert excinfo.value.context["failed_checks"][0]["name"] == "env.contract"


def test_dumps_is_valid_json() -> None:
    report = _report(CheckResult("env.contract", "ok", "fine", blocking=True))
    payload = json.loads(dumps(report))
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "env.contract"


def test_render_text_lists_every_check() -> None:
    report = _report(
        CheckResult("env.contract", "ok", "fine", blocking=True),
        CheckResult("fonts.available", "warn", "缺项目字体", blocking=False, remediation="放字体"),
    )
    text = render_text(report)
    assert "env.contract" in text
    assert "fonts.available" in text
    assert "放字体" in text


def test_doctor_run_covers_all_checks_on_fake_home(
    tmp_paths: StudioPaths, contract_env: dict[str, str]
) -> None:
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    assert {check.name for check in report.checks} == EXPECTED_CHECK_NAMES
    assert {check.name for check in report.blocking_failures} == {"config.valid", "python.tts"}
    assert json.loads(dumps(report))["ok"] is False


def test_doctor_flags_missing_env_vars(tmp_paths: StudioPaths) -> None:
    report = Doctor(tmp_paths, env={"SystemDrive": "C:"}, skip_heavy=True).run()
    contract = next(check for check in report.checks if check.name == "env.contract")
    assert contract.status == "fail"
    assert contract.blocking
    assert "HF_HOME" in contract.detail


def test_doctor_flags_system_drive_paths(
    tmp_paths: StudioPaths, contract_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    env = dict(contract_env) | {"UV_CACHE_DIR": r"C:\uv-cache"}
    report = Doctor(tmp_paths, env=env, skip_heavy=True).run()
    contract = next(check for check in report.checks if check.name == "env.contract")
    assert contract.status == "fail"
    assert "UV_CACHE_DIR" in contract.detail


def test_doctor_blocks_when_uv_cache_on_system_drive(
    tmp_paths: StudioPaths, contract_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 加固：uv 会静默写 GB 级缓存，落在系统盘必须阻塞启动。"""

    def fake_run(argv: list[str], *, timeout: int = 60) -> tuple[int, str, str]:
        if list(argv[:2]) == ["uv", "--version"]:
            return 0, "uv 0.11.17", ""
        if list(argv[:3]) == ["uv", "cache", "dir"]:
            return 0, "C:\\Users\\me\\.cache\\uv", ""
        return -1, "", "not found"

    monkeypatch.setattr(doctor_module, "_run", fake_run)
    monkeypatch.setenv("SystemDrive", "C:")
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    uv_check = next(check for check in report.checks if check.name == "uv.version")
    assert uv_check.status == "fail"
    assert uv_check.blocking
    assert "uv.version" in {check.name for check in report.blocking_failures}


def test_doctor_skips_heavy_checks_when_asked(tmp_paths: StudioPaths, contract_env: dict[str, str]) -> None:
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["torch.cuda"] == "skip"
    assert statuses["ffmpeg.nvenc"] == "skip"


def test_doctor_reports_config_failure_as_blocking(
    tmp_paths: StudioPaths, contract_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置失败（如局域网无密码 / persona 缺字段）必须**阻塞启动**。"""

    def boom(*args: object, **kwargs: object) -> None:
        raise ConfigError(
            "app.web.allow_lan=true 但未设置 WebUI 密码 ⇒ 拒绝启动",
            code=ErrorCode.AUTH_LAN_WITHOUT_PASSWORD,
            remediation="设置 STUDIO_WEBUI_PASSWORD",
        )

    monkeypatch.setattr(doctor_module, "load_config", boom)
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    check = next(item for item in report.checks if item.name == "config.valid")
    assert check.status == "fail"
    assert check.blocking
    assert check.data["code"] == "AUTH_LAN_WITHOUT_PASSWORD"
    assert "config.valid" in {item.name for item in report.blocking_failures}


def test_doctor_reports_persona_library_health(tmp_paths: StudioPaths, contract_env: dict[str, str]) -> None:
    """人物库体检是**非阻塞**项：库文件坏了不拦启动（激活人物仍可用）。"""
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    check = next(item for item in report.checks if item.name == "persona.library")
    assert check.blocking is False
    assert check.status in {"ok", "warn"}


def test_doctor_flags_broken_library_entry(tmp_paths: StudioPaths, contract_env: dict[str, str]) -> None:
    library = tmp_paths.persona_library_dir
    library.mkdir(parents=True, exist_ok=True)
    (library / "broken.yaml").write_text("id: broken\nname: [\n", encoding="utf-8")
    report = Doctor(tmp_paths, env=contract_env, skip_heavy=True).run()
    check = next(item for item in report.checks if item.name == "persona.library")
    assert check.status in {"ok", "warn"}
    assert "persona.library" not in {item.name for item in report.blocking_failures}


@pytest.mark.slow
def test_doctor_is_green_on_this_machine(repo_paths: StudioPaths) -> None:
    """真机门禁回归：需先 ``. .\\scripts\\env.ps1`` 加载环境变量契约。"""
    if not os.environ.get("STUDIO_DATA_DIR"):
        pytest.skip("未加载 scripts/env.ps1，跳过真机门禁回归")
    report = Doctor(repo_paths, skip_heavy=True).run()
    assert report.ok, [check.to_dict() for check in report.blocking_failures]
    assert next(check for check in report.checks if check.name == "uv.version").status == "ok"
    config_check = next(check for check in report.checks if check.name == "config.valid")
    assert config_check.status == "ok", config_check.detail
