"""字幕 golden：一整份 ASS 逐字节比对（T3.5 · §04.2.6）。

为什么值得一个 golden
---------------------
字幕文件里绝大多数内容是**格式**（样式表列顺序、时间码位数、换行符、BOM），
而格式错了的后果全都一样：libass 静默不显示、或者显示成一排豆腐块。逐字段断言
写不完这些，逐字节比对一行就够。

golden 挂了怎么办
-----------------
**先看差异，再决定改哪边**。差异是"我改了生成器" ⇒ 用
``STUDIO_UPDATE_GOLDEN=1 pytest tests/golden/test_ass.py`` 重刷；
差异是"我没改生成器但输出变了" ⇒ 那是一个 bug，别刷。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from studio.core.config import SubtitleConfig
from studio.render.subtitle import Cue, build_ass

GOLDEN_DIR: Path = Path(__file__).resolve().parent / "ass"

#: 固定画布 / 配置，让 golden 与 `config/outputs.yaml` 的调整解耦 ——
#: 这份 golden 要钉的是**格式**，不是"当前字号是多少"。
CANVAS: tuple[int, int] = (1080, 1920)


def _config() -> SubtitleConfig:
    return SubtitleConfig(
        font_name="Microsoft YaHei",
        font_size=64,
        outline=4,
        shadow=2,
        margin_bottom=420,
        max_chars_per_line=13,
        max_lines=2,
    )


#: 三句覆盖三种情况：标点断行 / 英文数字不可分 / 无标点长句（容量放宽）
CUES: tuple[Cue, ...] = (
    Cue(0, 3200, "今天我们来看一张特别离谱的跑酷地图。"),
    Cue(3200, 6100, "iPhone15 Pro Max 售价 8999 元，贵得离谱。", style="SpeakerA"),
    Cue(6100, 9000, "开局只有一格方块脚下就是虚空我试了三次全都掉下去了", style="SpeakerB"),
)


def test_ass_matches_the_golden_file() -> None:
    """★ 整份 ASS 逐字节比对（含 BOM / 行尾 / 时间码位数）。"""
    body = build_ass(CUES, config=_config(), canvas=CANVAS, title="golden-task")
    golden = GOLDEN_DIR / "subtitle.ass"

    if os.environ.get("STUDIO_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(body, encoding="utf-8", newline="\n")
        pytest.skip("已重刷 golden（STUDIO_UPDATE_GOLDEN=1）")

    assert golden.is_file(), f"缺少 golden 文件：{golden}（用 STUDIO_UPDATE_GOLDEN=1 生成）"
    assert body == golden.read_text(encoding="utf-8")


def test_golden_file_has_no_bom_and_no_crlf() -> None:
    """★ golden 自己也得满足 §04.2.6 的编码要求（否则它钉住的是一个错的格式）。"""
    raw = (GOLDEN_DIR / "subtitle.ass").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
