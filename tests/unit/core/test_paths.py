"""路径契约与命名约定（§02.2 / §2.4 · T1.1）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from studio.core.paths import DEFAULT_DATA_DIRNAME, StudioPaths, is_on_system_drive

#: 仓库根：这一层要读**真实**的 ``scripts/env.ps1``（它是部署面的一部分，没有 Python 替身）。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 素材根目录 —— 它们下面**一级**就是某一条具体素材（音色的目录名就是它的 id，§3.1）。
#: 环境闸门只许建到这些根目录**本身**为止。
_ASSET_ROOTS = ("voice_src",)


def _env_gate_dirs() -> list[str]:
    """``scripts/env.ps1`` 里 ``$StudioEnvDirs`` 那串预建目录（``data\\`` 之后的那一段）。

    反斜杠统一成 ``/``：这一层判的是**层级**，不是分隔符。
    """
    text = (REPO_ROOT / "scripts" / "env.ps1").read_text(encoding="utf-8")
    return [
        match.group("rel").replace("\\", "/")
        for match in re.finditer(r"Join-Path \$StudioDataDir '(?P<rel>[^']+)'", text)
    ]


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


def test_env_gate_pre_creates_container_dirs_but_never_a_concrete_asset_dir() -> None:
    """环境闸门建**骨架**，不建**素材**（陷阱 219 的根因）。

    ``data/voice_src/<音色 id>/`` 的目录名**就是那条素材的 id**。环境闸门一 dot-source
    就把它建出来，等于凭空造出一条"盘上有、库里没有"的孤儿 —— 面板上是一条永远删不掉
    的警告，而且**每次 dot-source 都长回来**：手工删是白删，「清掉不合格的孤儿」也白清。
    它当初连的还是《熊出没》占位音色的名字，而音色 id 与展现名是解耦的（R2）—— 用户换成
    自录音色之后，那两个目录就成了两条**永远在报的假警报**。

    所以这条断言不是"文案整洁"，而是「这条提示必须能消失」。
    """
    dirs = _env_gate_dirs()
    assert "voice_src" in dirs, "父目录要在：全新克隆得有个地方放参考音"
    nested = [item for item in dirs if "/" in item and item.split("/", 1)[0] in _ASSET_ROOTS]
    assert nested == []


def test_is_on_system_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    assert is_on_system_drive(r"C:\Windows\Temp")
    assert not is_on_system_drive(r"D:\ai_models")
