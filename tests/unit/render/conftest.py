"""渲染子系统单测的共享夹具（T3.2）。

为什么这里有个 conftest
----------------------
"造一张真的 PNG" 这件事在 `test_watermark.py`（解析 PNG）与 `test_profiles.py`
（走通 `--show` 的报告）里都要用。抄两份的直接后果是：某天把夹具改成"带 tRNS 的
调色板图"，只改了其中一份，另一份还在用旧夹具，于是两条用例测的其实是两种输入。

夹具一律**落在 tmp 家目录**里（与 `tests/conftest.py` 的约定一致）：
真实 `templates/` 与 `data/` 一个字节都不碰。
"""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import pytest

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.paths import StudioPaths
from studio.render.watermark import PNG_SIGNATURE

#: 仓库里那份真实配置（只读；复制到临时家目录后再改）
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
REAL_OUTPUTS_YAML: Path = REPO_ROOT / "config" / "outputs.yaml"

#: `config/outputs.yaml` 里写的水印相对路径（相对 STUDIO_HOME）
WATERMARK_REL: str = "templates/douyin_9x16_default/assets/images/watermark.png"


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png_bytes(
    *,
    width: int = 200,
    height: int = 100,
    color_type: int = 6,
    trns: bool = False,
) -> bytes:
    """造一张**真的** PNG（只用 ``zlib`` + ``struct``，不引 Pillow）。

    IDAT 里放的是真数据（zlib 压缩的扫描线），所以这些字节同时也能被 ffmpeg /
    Pillow 读 —— T3.3 / T3.4 的用例可以直接复用同一套夹具，不必再换一套。

    :param color_type: 0 灰度 / 2 真彩 / 3 调色板 / 4 灰度+alpha / 6 真彩+alpha
    :param trns: 是否带 ``tRNS`` 块（调色板与真彩的透明都靠它）
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = b"".join(b"\x00" + bytes(channels * width) for _ in range(height))
    parts = [
        PNG_SIGNATURE,
        _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)),
    ]
    if color_type == 3:
        parts.append(_chunk(b"PLTE", bytes([0, 0, 0, 255, 255, 255])))
    if trns:
        parts.append(_chunk(b"tRNS", b"\x00\x00"))
    parts.append(_chunk(b"IDAT", zlib.compress(raw)))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


def write_png(path: Path, **kwargs: int | bool) -> Path:
    """把 :func:`png_bytes` 写到盘上（父目录自动建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(**kwargs))  # type: ignore[arg-type]
    return path


@pytest.fixture
def outputs_home(tmp_path: Path) -> StudioPaths:
    """临时家目录：`config/outputs.yaml` 从仓库复制一份，可随意改。"""
    home = tmp_path / "studio"
    (home / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_OUTPUTS_YAML, home / "config" / "outputs.yaml")
    return StudioPaths(home=home, data_dir=home / "data")


@pytest.fixture
def outputs(outputs_home: StudioPaths) -> OutputsConfig:
    """那份临时 `outputs.yaml` 解析出来的模型。"""
    return load_outputs_config(outputs_home.config_dir / "outputs.yaml")


@pytest.fixture
def watermark_path(outputs_home: StudioPaths) -> Path:
    """水印 PNG 的**绝对**路径（默认不存在；要用的用例自己 `write_png`）。"""
    return outputs_home.home / WATERMARK_REL
