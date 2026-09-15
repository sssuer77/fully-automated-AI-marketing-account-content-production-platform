"""pytest 全局夹具。

约定：**测试永不触碰真实 ``data/``**。需要落盘的用例一律用 ``tmp_paths``，
从而保证 CI / 本地跑测试不会污染生产库与媒资目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.paths import StudioPaths

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: 10 项路径变量的合法取值（指向临时目录，避免依赖真实 D 盘）
_ENV_TEMPLATE: dict[str, str] = {
    "STUDIO_HOME": "{home}",
    "STUDIO_DATA_DIR": "{home}\\data",
    "HF_HOME": "{home}\\hf",
    "MODELSCOPE_CACHE": "{home}\\modelscope",
    "TORCH_HOME": "{home}\\torch",
    "UV_CACHE_DIR": "{home}\\uv",
    "PIP_CACHE_DIR": "{home}\\pip",
    "PLAYWRIGHT_BROWSERS_PATH": "{home}\\playwright",
    "NPM_CONFIG_CACHE": "{home}\\npm",
    "TMP": "{home}\\tmp",
    "TEMP": "{home}\\tmp",
}


@pytest.fixture
def repo_paths() -> StudioPaths:
    """仓库真实路径（只读用途；禁止在测试中写 ``data/``）。"""
    return StudioPaths(home=REPO_ROOT, data_dir=REPO_ROOT / "data")


@pytest.fixture
def tmp_paths(tmp_path: Path) -> StudioPaths:
    """隔离路径契约（可写）。"""
    home = tmp_path / "studio"
    home.mkdir(parents=True, exist_ok=True)
    return StudioPaths(home=home, data_dir=home / "data")


@pytest.fixture
def contract_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """构造一份**合法**的环境变量契约快照。

    把 ``SystemDrive`` 指向一个不存在的盘符，使临时目录天然"不在系统盘"，
    从而让环境变量门禁用例与真实机器配置解耦。
    """
    home = str(tmp_path / "studio").replace("/", "\\")
    env = {name: value.format(home=home) for name, value in _ENV_TEMPLATE.items()}
    monkeypatch.setenv("SystemDrive", "Q:")
    return env
