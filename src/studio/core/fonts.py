"""系统字体目录（T5.1 从 ``render/subtitle.py`` 下沉）。

为什么住在 ``core/``
--------------------
这份枚举只依赖 ``sys`` / ``os`` / ``pathlib`` —— 它是**平台事实**，不是渲染决策。
原先它长在 ``render/subtitle.py`` 里，于是 ``publish/cover.py`` 为了拿一个字体文件
只能**在函数内** ``from studio.render.subtitle import system_font_dirs``：
那绕过了 ``§02.1`` 的「``publish/`` 不得 import ``render/``」，而"函数内导入"只是
让循环依赖不炸，并没有让分层变干净（T5.1 施工记录）。

两个消费方要的东西**不一样**，但问的是同一个问题：
- 字幕要一个**目录**（libass 自己按家族名挑字体，`msyh.ttc` 的家族名是
  ``Microsoft YaHei``，文件名里一个字母都对不上）；
- 封面要一个**文件**（``drawtext`` 的 ``fontfile=`` 只收文件）。

所以这里只答"系统字体在哪几个目录"，"从里面挑谁"留给各自的调用方。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["system_font_dirs"]


def system_font_dirs() -> tuple[Path, ...]:
    """系统字体目录（Windows / macOS / Linux 各一个约定位置）。

    先把 ``sys.platform`` 存进一个 ``str`` 变量再分支：mypy 会把 ``sys.platform``
    按运行平台收窄成字面量，于是"在 Windows 上给 macOS 分支写代码"会被判成
    不可达语句 —— 而这段代码本来就该在三个平台上都读得通。
    """
    platform: str = sys.platform
    if platform.startswith("win"):
        windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        return (Path(windir) / "Fonts",)
    if platform.startswith("darwin"):
        return (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts")
    return (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local" / "share" / "fonts",
    )
