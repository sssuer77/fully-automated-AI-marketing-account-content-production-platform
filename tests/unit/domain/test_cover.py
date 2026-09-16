"""封面排版纯函数（T5.1 · §04.1.8 / §06.3）。

验收四条（`todolist.md` T5.1）：**换行（≤2 行 × ≤10 字）/ 高亮分段 / 字号收缩 / 安全区**，
外加一条"截断必须记账"（§06.3：仍溢出 ⇒ 截断 + warn）。

这里一个 ffmpeg 都不跑 —— 排版是纯计算，把它的正确性绑在一次子进程调用上，
只会让失败原因分不清是"算错了"还是"机器上没有字体"。
"""

from __future__ import annotations

import pytest

from studio.domain.cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    HOOK_FRAME_OFFSET_MS,
    SAFE_BOTTOM,
    SAFE_LEFT,
    SAFE_RIGHT,
    SAFE_TOP,
    SUB_MAX_CHARS,
    TITLE_FONT_MIN,
    TITLE_FONT_SIZE,
    TITLE_MAX_CHARS,
    TITLE_MAX_LINES,
    CoverInput,
    CoverOutput,
    cover_block_top,
    cover_plan_payload,
    default_frame_at_ms,
    fit_font_size,
    line_height,
    rule_cover_output,
    split_runs,
    strip_trailing_punctuation,
    wrap_cover_text,
)

SAFE_WIDTH = SAFE_RIGHT - SAFE_LEFT


# ── 换行 ──────────────────────────────────────────────────────────────


class TestWrapCoverText:
    def test_short_text_stays_on_one_line(self) -> None:
        wrapped = wrap_cover_text("离谱跑酷地图", max_lines=TITLE_MAX_LINES, max_chars=TITLE_MAX_CHARS)
        assert wrapped.lines == ("离谱跑酷地图",)
        assert wrapped.truncated is False
        assert wrapped.dropped_chars == 0

    def test_breaks_at_punctuation_preferentially(self) -> None:
        """10 字以内断得下时，断在标点之后 —— 中文里"逗号换行"比"硬断"好看得多。"""
        wrapped = wrap_cover_text(
            "开局一格方块，脚下就是虚空",
            max_lines=TITLE_MAX_LINES,
            max_chars=TITLE_MAX_CHARS,
        )
        assert wrapped.lines == ("开局一格方块，", "脚下就是虚空")
        assert wrapped.truncated is False

    def test_no_punctuation_falls_back_to_hard_cut(self) -> None:
        """没有标点可断时按字数硬切 —— 且**每一行都不超限**。"""
        wrapped = wrap_cover_text(
            "开局只有一格方块脚下就是虚空",
            max_lines=TITLE_MAX_LINES,
            max_chars=TITLE_MAX_CHARS,
        )
        assert wrapped.lines == ("开局只有一格方块脚下", "就是虚空")
        assert all(len(line) <= TITLE_MAX_CHARS for line in wrapped.lines)

    def test_punctuation_never_starts_a_line(self) -> None:
        """断点前移一格，也不让逗号落在下一行开头。"""
        wrapped = wrap_cover_text(
            "这是一段很长的文案，测试",
            max_lines=TITLE_MAX_LINES,
            max_chars=6,
        )
        for line in wrapped.lines:
            assert not line.startswith("，")

    def test_overflow_is_truncated_and_accounted(self) -> None:
        """超出 2 行 ⇒ 截断，并如实记账（不能静默删字）。"""
        text = "一二三四五六七八九十" * 3  # 30 字 ⇒ 3 行
        wrapped = wrap_cover_text(text, max_lines=TITLE_MAX_LINES, max_chars=TITLE_MAX_CHARS)
        assert len(wrapped.lines) == TITLE_MAX_LINES
        assert wrapped.truncated is True
        assert wrapped.dropped_chars == 10

    def test_empty_text_yields_no_lines(self) -> None:
        wrapped = wrap_cover_text("   ", max_lines=TITLE_MAX_LINES, max_chars=TITLE_MAX_CHARS)
        assert wrapped.lines == ()
        assert wrapped.truncated is False

    def test_text_property_joins_back(self) -> None:
        wrapped = wrap_cover_text("开局一格方块，脚下就是虚空", max_lines=2, max_chars=10)
        assert wrapped.text == "开局一格方块，脚下就是虚空"


# ── 高亮分段 ──────────────────────────────────────────────────────────


class TestSplitRuns:
    def test_no_highlight_is_a_single_plain_run(self) -> None:
        runs = split_runs("离谱跑酷地图", [])
        assert len(runs) == 1
        assert runs[0].highlight is False
        assert runs[0].text == "离谱跑酷地图"

    def test_highlight_in_the_middle_splits_three_ways(self) -> None:
        runs = split_runs("离谱跑酷地图", ["跑酷"])
        assert [(run.text, run.highlight) for run in runs] == [
            ("离谱", False),
            ("跑酷", True),
            ("地图", False),
        ]

    def test_only_the_first_occurrence_is_highlighted(self) -> None:
        """高亮是**子串且最早匹配**：一行里同一个词出现两次，只涂第一处。

        为什么不去找全部：高亮词的用途是"把最关键的词标出来"，而一行里同一个词
        出现两遍本身就说明这一行有问题（该由换行解决，不是靠涂两处颜色）。
        """
        runs = split_runs("跑酷地图跑酷", ["跑酷"])
        assert [(r.text, r.highlight) for r in runs] == [("跑酷", True), ("地图跑酷", False)]

    def test_word_not_on_this_line_is_skipped_silently(self) -> None:
        """模型给的高亮词没落到这一行是**常态**（换行把它切到另一行了），不该报错。"""
        runs = split_runs("开局一格方块", ["跑酷"])
        assert len(runs) == 1
        assert runs[0].highlight is False

    def test_overlapping_words_do_not_duplicate_text(self) -> None:
        runs = split_runs("离谱跑酷地图", ["离谱跑酷", "跑酷"])
        assert "".join(run.text for run in runs) == "离谱跑酷地图"
        assert [run.highlight for run in runs] == [True, False]

    def test_empty_word_is_ignored(self) -> None:
        runs = split_runs("离谱跑酷地图", ["", "跑酷"])
        assert "".join(run.text for run in runs) == "离谱跑酷地图"


# ── 字号收缩 ──────────────────────────────────────────────────────────


class TestFitFontSize:
    def test_fits_keeps_the_base_size(self) -> None:
        assert fit_font_size(96, measured_width=500, safe_width=928) == 96

    def test_overflow_scales_proportionally(self) -> None:
        """超宽 20% ⇒ 字号收 20%（线性一次算出来，不逐档试 —— 每档一次 ffmpeg 太贵）。"""
        assert fit_font_size(96, measured_width=1160, safe_width=928) == 76

    def test_scaling_stops_at_the_minimum(self) -> None:
        """§06.3：最小 60pt —— 再小就没人看得清了（哪怕算出来该是 48）。"""
        assert fit_font_size(96, measured_width=1856, safe_width=928) == TITLE_FONT_MIN
        assert fit_font_size(96, measured_width=9280, safe_width=928) == TITLE_FONT_MIN

    def test_zero_measurement_keeps_the_base_size(self) -> None:
        """量不出来 ⇒ 按原字号画（宁可可能溢出，也不能因为一次量不出来就丢掉封面）。"""
        assert fit_font_size(96, measured_width=0, safe_width=928) == 96

    def test_never_exceeds_the_base(self) -> None:
        assert fit_font_size(96, measured_width=10, safe_width=928) == 96


# ── 安全区 ────────────────────────────────────────────────────────────


class TestSafeArea:
    def test_block_sits_above_the_bottom_safe_line(self) -> None:
        height = 2 * line_height(TITLE_FONT_SIZE)
        top = cover_block_top(height)
        assert top + height <= SAFE_BOTTOM

    def test_block_never_crosses_the_top_safe_line(self) -> None:
        """文字块高到压过安全区上沿时，**压回来**（宁可下边被切，也不进平台 UI）。"""
        assert cover_block_top(COVER_HEIGHT) == SAFE_TOP

    def test_safe_area_is_inside_the_canvas(self) -> None:
        assert 0 < SAFE_TOP < SAFE_BOTTOM < COVER_HEIGHT
        assert 0 < SAFE_LEFT < SAFE_RIGHT < COVER_WIDTH

    def test_line_height_exceeds_the_font_size(self) -> None:
        """行距必须大于字号，否则两行会贴住（中文字面比字号高）。"""
        assert line_height(96) > 96


# ── 抽帧点 ────────────────────────────────────────────────────────────


class TestDefaultFrameAtMs:
    def test_offset_is_applied(self) -> None:
        assert default_frame_at_ms(0) == HOOK_FRAME_OFFSET_MS
        assert default_frame_at_ms(1200) == 1200 + HOOK_FRAME_OFFSET_MS

    def test_negative_start_is_clamped(self) -> None:
        """``-ss`` 收到负数会直接报错 ⇒ 整张封面降级成纯色底。钳在 0。"""
        assert default_frame_at_ms(-5000) == 0


# ── 规则兜底文案 ──────────────────────────────────────────────────────


def _input(**overrides: object) -> CoverInput:
    data: dict[str, object] = {
        "task_id": "01TASK",
        "title": "离谱跑酷地图",
        "hook": "今天我们来看一张特别离谱的跑酷地图",
        "cta": "点个关注",
        "hook_start_ms": 0,
        "duration_ms": 17_482,
    }
    data.update(overrides)
    return CoverInput.model_validate(data)


class TestRuleCoverOutput:
    def test_title_wins_over_hook(self) -> None:
        """标题本来就是按"一句话概括这一期"写的，钩子更像口播第一句。"""
        output = rule_cover_output(_input(), frame_at_ms=500)
        assert output.title_text == "离谱跑酷地图"
        assert output.sub_text == "点个关注"

    def test_falls_back_to_hook_when_title_missing(self) -> None:
        output = rule_cover_output(_input(title=""), frame_at_ms=500)
        assert output.title_text.startswith("今天我们来看一张")

    def test_identical_sub_is_dropped(self) -> None:
        """同一句话写两遍是噪音 —— 次文案留空。"""
        output = rule_cover_output(_input(title="离谱跑酷地图", cta="离谱跑酷地图"), frame_at_ms=500)
        assert output.sub_text is None

    def test_title_is_clipped_to_the_contract_length(self) -> None:
        output = rule_cover_output(_input(title="一" * 40), frame_at_ms=500)
        assert len(output.title_text) == 20

    def test_no_highlight_in_the_rule_path(self) -> None:
        """高亮是"模型认为这个词最关键"的表达，规则挑的词没有这个判断。"""
        assert rule_cover_output(_input(), frame_at_ms=500).highlight_words == []

    def test_empty_everything_still_yields_a_valid_title(self) -> None:
        """全部为空也要产出一条**过得了 schema** 的文案（``min_length=1``）。"""
        output = rule_cover_output(_input(title="", hook="", cta=""), frame_at_ms=500)
        assert output.title_text
        assert output.banned_checked is False

    def test_trailing_punctuation_is_stripped(self) -> None:
        output = rule_cover_output(_input(title="离谱跑酷地图。", cta="点个关注！"), frame_at_ms=500)
        assert output.title_text == "离谱跑酷地图"
        assert output.sub_text == "点个关注"


class TestStripTrailingPunctuation:
    @pytest.mark.parametrize("text", ["离谱跑酷地图。", "离谱跑酷地图！", "离谱跑酷地图…", "离谱跑酷地图，"])
    def test_strips(self, text: str) -> None:
        assert strip_trailing_punctuation(text) == "离谱跑酷地图"

    def test_keeps_inner_punctuation(self) -> None:
        assert strip_trailing_punctuation("开局一格方块，脚下就是虚空") == "开局一格方块，脚下就是虚空"


# ── 留痕 ──────────────────────────────────────────────────────────────


class TestCoverPlanPayload:
    def test_answers_why_it_looks_like_this(self) -> None:
        """plan 要能回答"字号被缩过吗、文案被截过吗" —— 只写最终图像答不了。"""
        output = CoverOutput(title_text="离谱跑酷地图", sub_text="点个关注", frame_at_ms=500)
        title = wrap_cover_text(output.title_text, max_lines=TITLE_MAX_LINES, max_chars=TITLE_MAX_CHARS)
        plan = cover_plan_payload(
            output=output,
            title=title,
            sub=wrap_cover_text("点个关注", max_lines=1, max_chars=SUB_MAX_CHARS),
            title_size=96,
            sub_size=52,
        )
        assert plan["title_lines"] == ["离谱跑酷地图"]
        assert plan["title_font_size"] == 96
        assert plan["title_truncated"] is False
        assert plan["canvas"] == f"{COVER_WIDTH}x{COVER_HEIGHT}"
