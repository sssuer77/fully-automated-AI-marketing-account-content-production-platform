"""路径契约与命名约定（§02.2 / §2.4 · T1.1）。"""

from __future__ import annotations

import pytest

from studio.core.paths import DEFAULT_DATA_DIRNAME, StudioPaths, is_on_system_drive


def test_from_env_prefers_explicit_values(contract_env: dict[str, str]) -> None:
    paths = StudioPaths.from_env(contract_env)
    assert paths.home == paths.home.resolve()
    assert paths.data_dir.name == DEFAULT_DATA_DIRNAME


def test_from_env_infers_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_HOME", raising=False)
    monkeypatch.delenv("STUDIO_DATA_DIR", raising=False)
    paths = StudioPaths.from_env({})
    assert (paths.home / "pyproject.toml").is_file()
    assert paths.data_dir == paths.home / DEFAULT_DATA_DIRNAME


def test_sentence_wav_is_zero_padded(tmp_paths: StudioPaths) -> None:
    assert tmp_paths.sentence_wav("01J9Z", 7).name == "s007.wav"
    assert tmp_paths.sentence_wav("01J9Z", 1234).name == "s1234.wav"
    assert tmp_paths.sentence_wav("01J9Z", 7).parent.name == "01J9Z"


def test_final_video_naming_matches_convention(tmp_paths: StudioPaths) -> None:
    path = tmp_paths.final_video("01J9Z3K7", stamp="20260913-143022")
    assert path.name == "20260913-143022_01J9Z3K7_final.mp4"
    assert path.parent == tmp_paths.videos_dir


def test_degraded_video_gets_720p_suffix(tmp_paths: StudioPaths) -> None:
    path = tmp_paths.final_video("01J9Z", degraded=True, stamp="20260913-143022")
    assert path.name.endswith("_final_720p.mp4")


def test_cover_naming(tmp_paths: StudioPaths) -> None:
    path = tmp_paths.cover_image("01J9Z", stamp="20260913-143022")
    assert path.name == "20260913-143022_01J9Z_cover.jpg"


def test_work_paths_are_task_scoped(tmp_paths: StudioPaths) -> None:
    work = tmp_paths.work_dir_for("01J9Z")
    assert work.parent == tmp_paths.work_dir
    assert tmp_paths.graphs_dir_for("01J9Z") == work / "graphs"
    assert tmp_paths.voice_master("01J9Z") == work / "tts" / "voice_master.wav"
    assert tmp_paths.script_json("01J9Z") == work / "script.json"


def test_ensure_runtime_dirs_is_idempotent(tmp_paths: StudioPaths) -> None:
    created_first = tmp_paths.ensure_runtime_dirs()
    created_second = tmp_paths.ensure_runtime_dirs()
    assert created_first
    assert created_second == []
    assert all(directory.is_dir() for directory in tmp_paths.runtime_dirs())


def test_is_on_system_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    assert is_on_system_drive(r"C:\Windows\Temp")
    assert not is_on_system_drive(r"D:\ai_models")
