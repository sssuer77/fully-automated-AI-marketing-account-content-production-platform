r"""发布领域规则（T5.2 · §06.2.2 / §06.5.3）—— 纯函数，零 I/O。

四条不变式，逐条钉住：

| 规则 | 判据 | 反面（会被这条测试抓住的错法） |
| --- | --- | --- |
| 幂等键含账号 | ``sha256(task,platform,account)`` | 漏掉 account ⇒ 同任务发不到第二个账号（UNIQUE 顶掉） |
| 超长只报不改 | ``fit_text().over_by`` | 静默截断 ⇒ 发出去的不是写的那一版，且无人知道 |
| 话题按语法拼 | ``render_tags(syntax=…)`` | 把 ``#`` 写死在代码里 ⇒ 微博的 ``#tag#`` 变成 ``#tag`` |
| 回读比对**说清吞法** | ``compare_readback().reason`` | 只回布尔 ⇒ “emoji 被吞”与“点错框”同一条建议 |
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from studio.domain.publish import (
    CaptionPlan,
    ReadbackDiff,
    TagPlan,
    TextFit,
    build_caption,
    compare_readback,
    fit_text,
    idempotency_key,
    render_tags,
)

# ── 幂等键 ────────────────────────────────────────────────────────────


class TestIdempotencyKey:
    def test_matches_spec_formula(self) -> None:
        """§03.3.15 逐字：``sha256(task_id|platform|account_id)``。"""
        expected = hashlib.sha256(b"01TASK|douyin|acc_main").hexdigest()
        assert idempotency_key("01TASK", "douyin", "acc_main") == expected

    def test_account_changes_the_key(self) -> None:
        """同任务分发到两个账号是**两件不同的事**（§06.2.4）—— 键必须不同。"""
        assert idempotency_key("01TASK", "douyin", "acc_a") != idempotency_key("01TASK", "douyin", "acc_b")

    def test_platform_changes_the_key(self) -> None:
        assert idempotency_key("01TASK", "douyin", "acc") != idempotency_key("01TASK", "kuaishou", "acc")

    def test_task_changes_the_key(self) -> None:
        assert idempotency_key("01A", "douyin", "acc") != idempotency_key("01B", "douyin", "acc")

    def test_key_is_a_hex_digest_not_the_raw_join(self) -> None:
        """返回摘要而不是拼接串：拼接串会把 task_id 与账号名泄进日志行。"""
        key = idempotency_key("01TASK", "douyin", "acc_main")
        assert "01TASK" not in key
        assert "acc_main" not in key
        assert len(key) == 64

    def test_deterministic(self) -> None:
        assert idempotency_key("01TASK", "douyin", "acc") == idempotency_key("01TASK", "douyin", "acc")


# ── 长度 ──────────────────────────────────────────────────────────────


class TestFitText:
    def test_within_limit(self) -> None:
        fit = fit_text("离谱跑酷地图", limit=20)
        assert fit.ok and fit.over_by == 0 and fit.limit == 20

    def test_over_limit_reports_by_how_much(self) -> None:
        """§06.2.2 第 2 条：超长 ⇒ ``warn``。要能报出**超了几个字**。"""
        fit = fit_text("一二三四五六", limit=4)
        assert not fit.ok and fit.over_by == 2

    def test_counts_characters_not_bytes(self) -> None:
        """一个汉字算 1（平台的计数器就是这样），不是 UTF-8 的 3 个字节。"""
        assert fit_text("中文标题", limit=4).ok

    def test_exactly_at_limit_is_ok(self) -> None:
        assert fit_text("一二三四", limit=4).ok

    def test_does_not_modify_the_text(self) -> None:
        """它只**报**，不改。截不截由调用方按平台决定（§06.2.2 第 2 条）。"""
        assert fit_text("一二三四五六", limit=4).text == "一二三四五六"

    def test_empty_is_ok(self) -> None:
        assert fit_text("", limit=0).ok

    def test_is_frozen(self) -> None:
        fit = fit_text("abc", limit=5)
        with pytest.raises(FrozenInstanceError):
            fit.text = "x"  # type: ignore[misc]


# ── 话题 ──────────────────────────────────────────────────────────────


class TestRenderTags:
    def test_douyin_syntax(self) -> None:
        plan = render_tags(["跑酷", "地图"], syntax="#{tag}", max_tags=5)
        assert plan.rendered == ("#跑酷", "#地图")
        assert plan.tags == ("跑酷", "地图")
        assert plan.dropped == ()

    def test_weibo_syntax_wraps_both_sides(self) -> None:
        """微博是 ``#话题#``（§06.2.2 第 3 条）。"""
        plan = render_tags(["跑酷"], syntax="#{tag}#", max_tags=5)
        assert plan.rendered == ("#跑酷#",)

    def test_bilibili_syntax_has_no_hash(self) -> None:
        plan = render_tags(["跑酷"], syntax="{tag}", max_tags=5)
        assert plan.rendered == ("跑酷",)

    def test_strips_existing_hash_prefix(self) -> None:
        """写进来的 ``#`` 会被剥掉再按 syntax 重拼 —— 语法只有一个来源。"""
        plan = render_tags(["#跑酷"], syntax="#{tag}", max_tags=5)
        assert plan.tags == ("跑酷",)
        assert plan.rendered == ("#跑酷",)

    def test_deduplicates_keeping_order(self) -> None:
        """模型偶尔把同一个词写两遍，平台上重复话题会被判成刷标签。"""
        plan = render_tags(["跑酷", "地图", "跑酷"], syntax="#{tag}", max_tags=5)
        assert plan.tags == ("跑酷", "地图")

    def test_blank_entries_are_skipped(self) -> None:
        plan = render_tags(["", "  ", " 跑酷 "], syntax="#{tag}", max_tags=5)
        assert plan.tags == ("跑酷",)

    def test_dropped_is_reported_not_silent(self) -> None:
        """被丢的话题要**报出来**：话题少了是运营看得见的曝光差。"""
        plan = render_tags(["a", "b", "c", "d"], syntax="#{tag}", max_tags=2)
        assert plan.tags == ("a", "b")
        assert plan.dropped == ("#c", "#d")

    def test_max_tags_zero_drops_everything(self) -> None:
        plan = render_tags(["a", "b"], syntax="#{tag}", max_tags=0)
        assert plan.tags == () and plan.dropped == ("#a", "#b")

    def test_empty_input(self) -> None:
        plan = render_tags([], syntax="#{tag}", max_tags=5)
        assert plan == TagPlan(tags=(), rendered=(), dropped=())

    def test_accepts_tuple(self) -> None:
        assert render_tags(("a",), syntax="#{tag}", max_tags=5).rendered == ("#a",)


# ── 文案拼装 ──────────────────────────────────────────────────────────


class TestBuildCaption:
    def _tags(self, *names: str) -> TagPlan:
        return render_tags(list(names), syntax="#{tag}", max_tags=10)

    def test_appends_tags_after_caption(self) -> None:
        plan = build_caption("今天看一张离谱的跑酷地图", self._tags("跑酷", "地图"), caption_max=1000)
        assert plan.text == "今天看一张离谱的跑酷地图 #跑酷 #地图"
        assert plan.tags == ("#跑酷", "#地图")
        assert plan.truncated_by == 0

    def test_drops_tail_tags_before_touching_caption(self) -> None:
        """话题是给算法读的，正文是给人读的：装不下时先摘话题。"""
        plan = build_caption("正文", self._tags("a", "b", "c"), caption_max=10)
        assert plan.text.startswith("正文")
        assert plan.truncated_by == 0
        assert "#c" in plan.dropped_tags

    def test_truncates_caption_only_as_last_resort(self) -> None:
        plan = build_caption("0123456789", self._tags(), caption_max=4)
        assert plan.text == "0123"
        assert plan.truncated_by == 6

    def test_empty_caption_still_renders_tags(self) -> None:
        plan = build_caption("", self._tags("跑酷"), caption_max=100)
        assert plan.text == "#跑酷"

    def test_strips_caption_whitespace(self) -> None:
        plan = build_caption("  正文  ", self._tags(), caption_max=100)
        assert plan.text == "正文"

    def test_merges_pack_dropped_with_caption_dropped(self) -> None:
        pack = render_tags(["a", "b", "c"], syntax="#{tag}", max_tags=2)  # c 被 pack 丢掉
        plan = build_caption("很长的正文占位置", pack, caption_max=6)
        assert "#c" in plan.dropped_tags

    def test_returns_caption_plan_type(self) -> None:
        assert isinstance(build_caption("x", self._tags(), caption_max=10), CaptionPlan)


# ── 回读比对（§06.5.3 第 ⑤ 步）────────────────────────────────────────


class TestCompareReadback:
    def test_identical_matches(self) -> None:
        diff = compare_readback("点个关注", "点个关注")
        assert diff.matched and diff.reason == "ok" and diff.detail == "回读一致"

    def test_empty_input_box_is_its_own_reason(self) -> None:
        """输入框整个空了 ⇒ 多半是选择器点错了框，**不是**编辑器吞字。"""
        assert compare_readback("点个关注", "   ").reason == "empty"

    def test_whitespace_only(self) -> None:
        """编辑器把换行归一了。判定归判定，**放行归放行**（matched 仍为假）。"""
        diff = compare_readback("第一行\n第二行", "第一行 第二行")
        assert diff.reason == "whitespace_only"
        assert not diff.matched

    def test_emoji_stripped(self) -> None:
        diff = compare_readback("点个关注 😀🔥", "点个关注")
        assert diff.reason == "emoji_stripped"

    def test_emoji_stripped_tolerates_trailing_newline(self) -> None:
        """真机上的常见形态：富文本区 ``inner_text`` 尾部带一个换行。

        只按"去掉 emoji 后的原文"比会退化成 ``mismatch``，于是给操作员的建议
        从"去掉 emoji 重发"变成"去查选择器"—— 方向全错（T5.2 真机踩过）。
        """
        assert compare_readback("点个关注 😀🔥", "点个关注 \n").reason == "emoji_stripped"

    def test_real_mismatch(self) -> None:
        assert compare_readback("点个关注", "点个注").reason == "mismatch"

    def test_at_points_at_the_first_difference(self) -> None:
        diff = compare_readback("abcd", "abXd")
        assert diff.at == 2

    def test_at_is_short_string_length_when_one_is_a_prefix(self) -> None:
        assert compare_readback("abcd", "abc").at == 3

    def test_excerpts_are_bounded(self) -> None:
        """摘要**不是**全文：日志里不该出现整篇文案。"""
        long_expected = "甲" * 500
        diff = compare_readback(long_expected, "乙" * 500)
        assert len(diff.expected_excerpt) <= 24
        assert len(diff.actual_excerpt) <= 24

    def test_detail_names_the_reason(self) -> None:
        detail = compare_readback("abcd", "abXd").detail
        # 偏移对外是 **1 基**（"第 3 个字符"），内部下标是 0 基 —— 人读的数是前者。
        assert "mismatch" in detail and "第 3 字符" in detail

    def test_empty_has_no_offset(self) -> None:
        """``empty`` 不给偏移：输入框空了的时候，"第 0 字符起"是句废话。"""
        diff = compare_readback("abc", "")
        assert diff.at is None
        assert diff.detail == "回读不一致（empty）"

    def test_emoji_stripped_keeps_the_offset(self) -> None:
        """emoji 那种**要**偏移：操作员拿它去文案里定位是哪一段。"""
        assert compare_readback("点个关注 😀🔥", "点个关注").at == 4

    def test_returns_readback_diff_type(self) -> None:
        assert isinstance(compare_readback("a", "a"), ReadbackDiff)

    def test_both_empty_matches(self) -> None:
        assert compare_readback("", "").matched

    def test_emoji_only_difference_in_both_directions(self) -> None:
        """方向反过来（平台**加**了 emoji？）也算 emoji 差异，不算真丢字。"""
        assert compare_readback("点个关注", "点个关注 😀").reason == "emoji_stripped"


# ── 类型 ──────────────────────────────────────────────────────────────


def test_public_dataclasses_are_frozen() -> None:
    fit = TextFit(text="x", limit=1, over_by=0)
    with pytest.raises(FrozenInstanceError):
        fit.limit = 2  # type: ignore[misc]
