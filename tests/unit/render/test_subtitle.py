"""字幕生成：断行 / 安全区 / 字体 / ASS 编码（T3.5 · §04.2.6）。

这一层的用例**不跑 ffmpeg**：wrap_lines / build_ass 是纯函数，直接断言它们产出的
字符串，比"渲一遍再截图看"快几个数量级。真正烧进画面那一遍由
`tests/integration/test_render_pipeline.py` 的端到端用例覆盖。

为什么把"断行"测得这么细
------------------------
断行是这个模块唯一有**真实分支**的地方：标点优先、词不可分、行数上限三者在打架。
它在成片里的表现是"某一行溢出画面"或"一个数字被拆成两半"，两者都不会报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.config import SubtitleConfig, load_outputs_config
from studio.core.errors import ErrorCode, RenderError
from studio.render.subtitle import (
    ASS_LINE_BREAK,
    Cue,
    assign_speaker_styles,
    build_ass,
    build_cues,
    escape_ass_text,
    format_ass_time,
    plan_subtitle,
    resolve_font_dir,
    wrap_lines,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
REAL_OUTPUTS_YAML: Path = REPO_ROOT / "config" / "outputs.yaml"


@pytest.fixture
def subtitle() -> SubtitleConfig:
    """仓库那份 `outputs.yaml` 里的字幕配置（测试跟着真实配置走，不另抄一份）。"""
    return load_outputs_config(REAL_OUTPUTS_YAML).subtitle


@pytest.fixture
def fonts_dir(tmp_path: Path) -> Path:
    """一个**有**字体的模板目录（内容是什么不重要，只看扩展名）。"""
    target = tmp_path / "templates" / "tpl" / "assets" / "fonts"
    target.mkdir(parents=True)
    (target / "fake.ttf").write_bytes(b"\x00\x01\x00\x00")
    return tmp_path / "templates"


# ══════════════════════════════════════════════════════════════════════
# 断行
# ══════════════════════════════════════════════════════════════════════


def test_short_line_is_not_wrapped() -> None:
    assert wrap_lines("开局只有一格方块", max_chars_per_line=13, max_lines=2) == ["开局只有一格方块"]


def test_break_prefers_punctuation() -> None:
    """优先在标点处断 —— 标点**留在上一行**，不会跑到行首。"""
    lines = wrap_lines("今天我们来看一张特别离谱的跑酷地图。", max_chars_per_line=13, max_lines=2)
    assert lines == ["今天我们来看一张特别离谱的", "跑酷地图。"]
    assert all(not line.startswith(("，", "。", "！", "？")) for line in lines)


def test_never_breaks_inside_a_number_or_latin_word() -> None:
    """★ 不在数字/英文单词中间断行（§04.2.6 的硬要求）。

    判据是"每个不可分单元都完整地落在某一行里"，而不是"总字数对得上" ——
    后者在"切开了又重新拼起来"时同样成立，测不出这个 bug。
    """
    source = "iPhone15 Pro Max 售价 8999 元，贵得离谱。"
    lines = wrap_lines(source, max_chars_per_line=13, max_lines=2)
    # 一个字都没丢（空格被行首行尾的 strip 吃掉，所以只比非空格字符）
    assert "".join(lines).replace(" ", "") == source.replace(" ", "")
    for unit in ("iPhone15", "Pro", "Max", "8999"):
        assert any(unit in line for line in lines), (unit, lines)


def test_a_single_word_longer_than_the_line_stays_whole() -> None:
    """一个词比一整行还长 ⇒ 让它独占一行溢出，**不切开**它。"""
    lines = wrap_lines(
        "这是一个超长英文词 abcdefghijklmnopqrstuvwxyz 收尾", max_chars_per_line=8, max_lines=2
    )
    assert any("abcdefghijklmnopqrstuvwxyz" in line for line in lines), lines


def test_capacity_widens_so_that_nothing_is_dropped() -> None:
    """没有标点的长句：把每行容量抬到"刚好排成 max_lines 行"，而不是丢字。"""
    text = "这是一句完全没有标点的超长台词它一直写下去写下去写下去写下去写下去"
    lines = wrap_lines(text, max_chars_per_line=13, max_lines=2)
    assert len(lines) == 2
    assert "".join(lines) == text


def test_empty_text_has_no_lines() -> None:
    assert wrap_lines("   ", max_chars_per_line=13, max_lines=2) == []


# ══════════════════════════════════════════════════════════════════════
# 时间轴 → 事件
# ══════════════════════════════════════════════════════════════════════


def test_cues_are_contiguous_and_use_measured_durations() -> None:
    """句与句首尾相接；时长**只**用实测值（不按字数估）。"""
    cues = build_cues(["甲", "乙"], [3200, 2800])
    assert [(c.start_ms, c.end_ms) for c in cues] == [(0, 3200), (3200, 6000)]


def test_mismatched_lengths_are_an_error() -> None:
    with pytest.raises(RenderError) as caught:
        build_cues(["甲", "乙"], [1000])
    assert caught.value.code is ErrorCode.RENDER_FAILED


def test_speaker_styles_follow_first_appearance() -> None:
    assert assign_speaker_styles(["b", "a", "b"]) == {"b": "SpeakerA", "a": "SpeakerB"}
    # 第三个说话人退回 Main（调色板只有两个强调色）
    assert assign_speaker_styles(["a", "b", "c"])["c"] == "Main"


def test_speakers_map_to_styles_on_cues() -> None:
    cues = build_cues(["甲", "乙"], [1000, 1000], speakers=["bigbear", "littlebear"])
    assert [cue.style for cue in cues] == ["SpeakerA", "SpeakerB"]


# ══════════════════════════════════════════════════════════════════════
# ASS 文本
# ══════════════════════════════════════════════════════════════════════


def test_ass_time_is_centiseconds() -> None:
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(3_723_450) == "1:02:03.45"


def test_escape_protects_braces_and_backslashes() -> None:
    """花括号是样式覆盖指令：不转义的话 `{` 之后的内容会被 libass 吃掉。"""
    assert escape_ass_text("{an8}") == r"\{an8\}"
    assert escape_ass_text(r"a\b") == r"a\\b"


def test_ass_declares_canvas_and_styles(subtitle: SubtitleConfig) -> None:
    body = build_ass([Cue(0, 1000, "你好")], config=subtitle, canvas=(1080, 1920), title="t1")
    assert "PlayResX: 1080" in body
    assert "PlayResY: 1920" in body
    # WrapStyle 2 = 只认显式换行（否则 libass 会把排好的两行再折一次）
    assert "WrapStyle: 2" in body
    for style in ("Main", "SpeakerA", "SpeakerB"):
        assert f"Style: {style}," in body


def _margin_v_of(config: SubtitleConfig) -> int:
    body = build_ass([Cue(0, 1000, "你好")], config=config, canvas=(1080, 1920), title="t1")
    main = next(line for line in body.splitlines() if line.startswith("Style: Main,"))
    return int(main.split(",")[21])


def test_margin_v_respects_the_safe_area(subtitle: SubtitleConfig) -> None:
    """★ MarginV ≥ safe_area.bottom（§04.2.6 的安全区要求）。"""
    assert _margin_v_of(subtitle) >= subtitle.safe_area.bottom

    # 配置里写小了不会出事 —— 会被抬到安全区下沿
    small = subtitle.model_copy(update={"margin_bottom": 10})
    assert _margin_v_of(small) == subtitle.safe_area.bottom == small.margin_v


def test_margin_v_is_one_number_shared_with_the_panel(subtitle: SubtitleConfig) -> None:
    """★ 写进 ASS 的那个数与面板上的「实际距底」是**同一个口径**（裁定 399）。

    以前这一屏只读，理由正是"两个数取 max，写了未必生效"。现在生效值由
    ``SubtitleConfig.margin_v`` 一处算出来、跟着响应下发给面板 —— 于是"往上挪"与
    "往下挪"都做得到，而且面板说的数与成片渲的数是同一个。
    """
    # 往上挪：调大「距底」立刻生效（它比安全区大，max 取它）。
    up = subtitle.model_copy(update={"margin_bottom": 640})
    assert _margin_v_of(up) == 640 == up.margin_v

    # 往下挪：把「底部安全区」一起调小才动得了（面板上那句提示说的就是这件事）。
    down = subtitle.model_copy(
        update={
            "margin_bottom": 200,
            "safe_area": subtitle.safe_area.model_copy(update={"bottom": 200}),
        }
    )
    assert _margin_v_of(down) == 200 == down.margin_v


def test_dialogue_uses_the_escape_sequence_for_line_breaks(subtitle: SubtitleConfig) -> None:
    body = build_ass(
        [Cue(0, 3000, "今天我们来看一张特别离谱的跑酷地图。")],
        config=subtitle,
        canvas=(1080, 1920),
        title="t1",
    )
    dialogue = next(line for line in body.splitlines() if line.startswith("Dialogue:"))
    assert ASS_LINE_BREAK in dialogue
    assert dialogue.count(ASS_LINE_BREAK) == 1  # 两句 ⇒ 一个换行


def test_zero_length_cues_are_dropped(subtitle: SubtitleConfig) -> None:
    """零长度事件在 libass 里就是不显示 —— 与其在成片里找"少了一句"，不如生成时丢掉。"""
    body = build_ass(
        [Cue(0, 0, "不显示"), Cue(0, 1000, "显示")],
        config=subtitle,
        canvas=(1080, 1920),
        title="t1",
    )
    assert body.count("Dialogue:") == 1
    assert "显示" in body


def test_ass_is_utf8_without_bom_and_lf_only(subtitle: SubtitleConfig, tmp_path: Path) -> None:
    """★ UTF-8 无 BOM + LF（§04.2.6；BOM 会让某些 libass 直接不显示字幕）。"""
    target = tmp_path / "sub.ass"
    plan = plan_subtitle(
        subtitle,
        build_cues(["你好"], [1000]),
        ass_path=target,
        templates_dir=tmp_path / "templates",
        canvas=(1080, 1920),
        title="t1",
    )
    assert plan.enabled, plan.skipped_reason
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


# ══════════════════════════════════════════════════════════════════════
# 字体
# ══════════════════════════════════════════════════════════════════════


def test_bundled_fonts_win_over_system_fonts(fonts_dir: Path) -> None:
    resolved = resolve_font_dir(fonts_dir)
    assert resolved.source == "assets"
    assert resolved.note is None


def test_a_fonts_dir_with_only_gitkeep_does_not_count(tmp_path: Path) -> None:
    """只有 `.gitkeep` 的目录算"没有字体" —— 否则会在模板里画出一排豆腐块。"""
    empty = tmp_path / "templates" / "tpl" / "assets" / "fonts"
    empty.mkdir(parents=True)
    (empty / ".gitkeep").write_text("", encoding="utf-8")
    resolved = resolve_font_dir(tmp_path / "templates")
    assert resolved.source == "system"
    assert resolved.note


def test_missing_font_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 两边都找不到 ⇒ 抛 FONT_MISSING（不是画豆腐块）。"""
    monkeypatch.setattr("studio.render.subtitle.system_font_dirs", lambda: (tmp_path / "nope",))
    with pytest.raises(RenderError) as caught:
        resolve_font_dir(tmp_path / "templates")
    assert caught.value.code is ErrorCode.FONT_MISSING


def test_missing_font_degrades_to_no_subtitle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 但**不阻塞出片**：字体缺失 ⇒ 跳过字幕 + 记原因（与水印同一条口径）。"""
    monkeypatch.setattr("studio.render.subtitle.system_font_dirs", lambda: (tmp_path / "nope",))
    plan = plan_subtitle(
        SubtitleConfig(),
        build_cues(["你好"], [1000]),
        ass_path=tmp_path / "sub.ass",
        templates_dir=tmp_path / "templates",
        canvas=(1080, 1920),
        title="t1",
    )
    assert plan.enabled is False
    assert plan.ass_path is None
    assert plan.skipped_reason and "字体" in plan.skipped_reason
    assert not (tmp_path / "sub.ass").exists()


def test_disabled_subtitle_writes_nothing(subtitle: SubtitleConfig, tmp_path: Path) -> None:
    """★ 关掉开关仍能出片（可选性验证）：这一层只是不产出 ASS，不抛错。"""
    plan = plan_subtitle(
        subtitle.model_copy(update={"enabled": False}),
        build_cues(["你好"], [1000]),
        ass_path=tmp_path / "sub.ass",
        templates_dir=tmp_path / "templates",
        canvas=(1080, 1920),
        title="t1",
    )
    assert plan.enabled is False
    assert plan.ass_path is None
    assert not (tmp_path / "sub.ass").exists()


def test_no_cues_degrades_to_no_subtitle(subtitle: SubtitleConfig, tmp_path: Path) -> None:
    """没有句级时间轴（复用母带且找不到逐句音频）⇒ 跳过，不按字数估一个。"""
    plan = plan_subtitle(
        subtitle,
        (),
        ass_path=tmp_path / "sub.ass",
        templates_dir=tmp_path / "templates",
        canvas=(1080, 1920),
        title="t1",
    )
    assert plan.enabled is False
    assert plan.skipped_reason and "时间轴" in plan.skipped_reason
