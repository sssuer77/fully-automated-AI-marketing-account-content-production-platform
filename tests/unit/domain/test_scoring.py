"""``domain.scoring``（T1.11 · §04.1.6）—— 评分 / 分级 / 放行 / 改稿越界。

全是纯函数，所以这里不需要库、不需要 Agent：把三件事逐条钉死 ——

1. ``total = 0.3×规则 + 0.7×LLM`` 这条**硬契约**（三条基线是回归锚点，
   权重或取整方式被改动时它们必须第一个红）；
2. "B 级最多改 2 轮"这道**钱闸门**（R16：与"这次改得好不好"无关）；
3. "只改被指出的句子"这条**机检**规则（越界判定按句，不按 diff 行数）。
"""

from __future__ import annotations

from typing import Literal

import pytest

from studio.domain.enums import AutoApprovePolicy, Grade
from studio.domain.scoring import (
    DUP_PENALTY,
    DUP_RATIO_MAX,
    EDIT_RETRIES,
    GRADE_A_MIN,
    GRADE_B_MIN,
    HOOK_MAX_CHARS,
    LLM_DIMENSIONS,
    LLM_WEIGHT,
    REVISION_LIMIT,
    RULE_WEIGHT,
    BannedHit,
    ChannelItem,
    Decision,
    EditReport,
    GateAction,
    LlmChannelDetail,
    ReviewIssue,
    RuleChannelDetail,
    allowed_sentences,
    auto_approved_by,
    check_edit,
    compute_score,
    decision_for,
    evaluate_rule_channel,
    gate_action,
    llm_total_of,
    paragraph_dup_ratio,
    rule_total_of,
    verdict_for,
)
from studio.domain.script import ScriptRules, SentenceSpec, WriterOutput

# ══════════════════════════════════════════════════════════════════════
# 构造器
# ══════════════════════════════════════════════════════════════════════


def channel(score: float) -> ChannelItem:
    return ChannelItem(score=score, comment="测试")


def llm(*scores: float) -> LlmChannelDetail:
    """六维度明细（按 :data:`LLM_DIMENSIONS` 的顺序给分）。"""
    assert len(scores) == len(LLM_DIMENSIONS), "六维度必须给满"
    return LlmChannelDetail(
        hook_opening=channel(scores[0]),
        positioning_fit=channel(scores[1]),
        oral_style=channel(scores[2]),
        emotion_rhythm=channel(scores[3]),
        ending_cta=channel(scores[4]),
        forbidden=channel(scores[5]),
    )


def rule_detail(
    *,
    length: float = 10.0,
    banned: bool = False,
    opening: bool = True,
    ending: bool = True,
    dup: float = 0.0,
) -> RuleChannelDetail:
    """直接拼一份规则明细（不经过 ``evaluate_rule_channel``，便于单独调每一项）。"""
    return RuleChannelDetail(
        length=channel(length),
        banned_hits=[BannedHit(term="脏话", count=1)] if banned else [],
        opening_ok=opening,
        ending_ok=ending,
        paragraph_dup_ratio=dup,
        chars=700,
        est_duration_ms=140_000,
        catchphrases_hit=2,
    )


def rules(*, low: int = 600, high: int = 800) -> ScriptRules:
    """与 persona（``target_chars`` 600–800）一致的生效阈值。"""
    return ScriptRules(word_count_min=low, word_count_max=high)


def issue(target: str, *, severity: Literal["block", "major", "minor"] = "major") -> ReviewIssue:
    return ReviewIssue(
        code="weak_hook",
        severity=severity,
        target=target,
        detail="开场不够抓人",
        suggestion="把结果前置",
    )


def writer(*texts: str, hook: str = "开场", cta: str = "点个关注") -> WriterOutput:
    """一份成稿（句子文本按顺序给）。"""
    return WriterOutput(
        title="标题",
        hook=hook,
        body_md="熊" * 700,
        cta=cta,
        sentences=[
            SentenceSpec(seq=index, text=text, speaker="bigbear") for index, text in enumerate(texts, start=1)
        ],
        est_duration_ms=140_000,
    )


def lines(count: int = 20) -> WriterOutput:
    return writer(*[f"第{index}句" for index in range(1, count + 1)])


def edit_sentence(script: WriterOutput, index: int, text: str) -> WriterOutput:
    """把第 ``index`` 句（1-based）换成 ``text``。"""
    return script.model_copy(
        update={
            "sentences": [
                item.model_copy(update={"text": text}) if item.seq == index else item
                for item in script.sentences
            ]
        }
    )


# ══════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_weights_sum_to_one(self) -> None:
        assert RULE_WEIGHT + LLM_WEIGHT == 1.0

    def test_weights_are_the_documented_ones(self) -> None:
        assert (RULE_WEIGHT, LLM_WEIGHT) == (0.3, 0.7)

    def test_grade_thresholds_are_ordered(self) -> None:
        assert GRADE_B_MIN < GRADE_A_MIN

    def test_grade_thresholds_are_the_documented_ones(self) -> None:
        assert (GRADE_A_MIN, GRADE_B_MIN) == (8.0, 5.0)

    def test_revision_limit_is_two_rounds(self) -> None:
        """原文 §2.2⑥"最多 2 轮"；R16 的钱闸门就是它。"""
        assert REVISION_LIMIT == 2

    def test_edit_retries_is_one(self) -> None:
        assert EDIT_RETRIES == 1

    def test_dup_ratio_contract(self) -> None:
        assert (DUP_RATIO_MAX, DUP_PENALTY) == (0.3, 2.0)

    def test_hook_ceiling(self) -> None:
        assert HOOK_MAX_CHARS == 80

    def test_there_are_exactly_six_dimensions(self) -> None:
        assert LLM_DIMENSIONS == (
            "hook_opening",
            "positioning_fit",
            "oral_style",
            "emotion_rhythm",
            "ending_cta",
            "forbidden",
        )


# ══════════════════════════════════════════════════════════════════════
# 总分与分级
# ══════════════════════════════════════════════════════════════════════


class TestComputeScore:
    @pytest.mark.parametrize(
        ("rule_score", "llm_score", "expected_total", "expected_grade"),
        [
            (8.0, 9.0, 8.7, Grade.A),
            (7.0, 6.0, 6.3, Grade.B),
            (4.0, 4.5, 4.35, Grade.C),
        ],
    )
    def test_baselines(
        self, rule_score: float, llm_score: float, expected_total: float, expected_grade: Grade
    ) -> None:
        """§04.1.6 的实测值 —— 回归锚点。"""
        assert compute_score(rule_score, llm_score) == (expected_total, expected_grade)

    @pytest.mark.parametrize(
        ("total", "expected_grade"),
        [
            (10.0, Grade.A),
            (8.0, Grade.A),
            (7.99, Grade.B),
            (5.0, Grade.B),
            (4.99, Grade.C),
            (0.0, Grade.C),
        ],
    )
    def test_grade_boundaries_are_inclusive_at_the_bottom(self, total: float, expected_grade: Grade) -> None:
        assert compute_score(total, total) == (total, expected_grade)

    def test_total_is_rounded_to_two_decimals(self) -> None:
        total, _ = compute_score(7.0, 6.0)
        assert total == 6.3
        assert round(total, 2) == total


class TestLlmTotal:
    def test_is_the_arithmetic_mean(self) -> None:
        assert llm_total_of(llm(10, 8, 6, 4, 2, 0)) == 5.0

    def test_uniform_scores_pass_through(self) -> None:
        assert llm_total_of(llm(7.5, 7.5, 7.5, 7.5, 7.5, 7.5)) == 7.5

    def test_is_rounded_to_two_decimals(self) -> None:
        assert llm_total_of(llm(9, 9, 9, 9, 9, 8)) == 8.83


# ══════════════════════════════════════════════════════════════════════
# 规则通道
# ══════════════════════════════════════════════════════════════════════


class TestParagraphDupRatio:
    def test_empty_body_is_zero(self) -> None:
        assert paragraph_dup_ratio("") == 0.0

    def test_blank_lines_do_not_count(self) -> None:
        assert paragraph_dup_ratio("熊大\n\n\n熊二") == 0.0

    def test_distinct_paragraphs_are_zero(self) -> None:
        assert paragraph_dup_ratio("熊大\n熊二\n光头强") == 0.0

    def test_one_repeated_paragraph_out_of_three(self) -> None:
        assert paragraph_dup_ratio("熊大\n熊大\n熊二") == round(1 / 3, 4)

    def test_whitespace_is_normalised(self) -> None:
        """ "多打一个空格"不该被算成"这两段不重复"。"""
        assert paragraph_dup_ratio("熊大又跑起来了。\n 熊大又跑起来了。 ") == 0.5

    def test_duplicated_types_over_paragraph_count(self) -> None:
        """口径是"重复出现的**种类数** / 总段数" ⇒ 每种至少 2 段 ⇒ 上限 0.5。"""
        assert paragraph_dup_ratio("a\na\nb\nb") == 0.5


class TestEvaluateRuleChannel:
    def test_length_within_range_scores_ten(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="开场",
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=2,
            forbidden=[],
            rules=rules(),
        )
        assert (detail.length.score, detail.chars) == (10.0, 700)

    @pytest.mark.parametrize(
        ("chars", "expected"),
        [
            (600, 10.0),
            (800, 10.0),
            (500, 5.0),
            (400, 0.0),
            (900, 5.0),
            (1000, 0.0),
        ],
    )
    def test_length_scales_with_the_interval_width(self, chars: int, expected: float) -> None:
        """区间宽 200 ⇒ 每差 20 字扣 1 分（persona 改阈值时曲线跟着缩放）。"""
        detail = evaluate_rule_channel(
            body_md="熊" * chars,
            hook="开场",
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=[],
            rules=rules(),
        )
        assert detail.length.score == expected

    def test_banned_hits_record_the_term_and_its_count(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700 + "脏话脏话",
            hook="开场",
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=["脏话"],
            rules=rules(),
        )
        assert [(item.term, item.count) for item in detail.banned_hits] == [("脏话", 2)]

    def test_clean_body_has_no_banned_hits(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="开场",
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=["脏话"],
            rules=rules(),
        )
        assert detail.banned_hits == []

    def test_blank_hook_and_cta_are_not_ok(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="   ",
            cta="",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=[],
            rules=rules(),
        )
        assert (detail.opening_ok, detail.ending_ok) == (False, False)

    def test_hook_at_the_ceiling_is_still_ok(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="熊" * HOOK_MAX_CHARS,
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=[],
            rules=rules(),
        )
        assert detail.opening_ok is True

    def test_hook_past_the_ceiling_is_not_ok(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="熊" * (HOOK_MAX_CHARS + 1),
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=[],
            rules=rules(),
        )
        assert detail.opening_ok is False

    def test_default_rules_are_used_when_none_is_given(self) -> None:
        detail = evaluate_rule_channel(
            body_md="熊" * 700,
            hook="开场",
            cta="关注",
            est_duration_ms=140_000,
            catchphrases_hit=0,
            forbidden=[],
        )
        assert detail.length.score == 10.0


class TestRuleTotal:
    def test_all_clear_is_ten(self) -> None:
        assert rule_total_of(rule_detail()) == 10.0

    def test_banned_hits_zero_out_that_item(self) -> None:
        assert rule_total_of(rule_detail(banned=True)) == 7.5

    def test_missing_opening_zeroes_that_item(self) -> None:
        assert rule_total_of(rule_detail(opening=False)) == 7.5

    def test_missing_ending_zeroes_that_item(self) -> None:
        assert rule_total_of(rule_detail(ending=False)) == 7.5

    def test_length_item_is_averaged_not_weighted(self) -> None:
        assert rule_total_of(rule_detail(length=5.0)) == 8.75

    def test_dup_at_the_cap_does_not_deduct(self) -> None:
        assert rule_total_of(rule_detail(dup=DUP_RATIO_MAX)) == 10.0

    def test_dup_above_the_cap_deducts_the_penalty(self) -> None:
        assert rule_total_of(rule_detail(dup=DUP_RATIO_MAX + 0.01)) == 10.0 - DUP_PENALTY

    def test_penalty_never_pushes_the_total_below_zero(self) -> None:
        assert (
            rule_total_of(rule_detail(length=0.0, banned=True, opening=False, ending=False, dup=1.0)) == 0.0
        )


# ══════════════════════════════════════════════════════════════════════
# 三值裁决 / 落库决定 / 放行动作
# ══════════════════════════════════════════════════════════════════════


class TestVerdictAndDecision:
    @pytest.mark.parametrize(
        ("grade", "expected"),
        [(Grade.A, "pass"), (Grade.B, "revise"), (Grade.C, "reject")],
    )
    def test_verdict_is_derived_from_the_grade(self, grade: Grade, expected: str) -> None:
        assert verdict_for(grade) == expected

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            (GateAction.AUTO_PASS, Decision.PASS_AUTO),
            (GateAction.EDIT, Decision.NEED_EDIT),
            (GateAction.HUMAN_GATE, Decision.NEED_EDIT),
            (GateAction.DISCARD, Decision.DISCARD),
        ],
    )
    def test_decision_for_maps_actions_onto_the_ddl_values(
        self, action: GateAction, expected: Decision
    ) -> None:
        assert decision_for(action) is expected

    def test_need_edit_is_the_only_value_shared_by_two_actions(self) -> None:
        """裁定 90：``decision`` 是落库事实、``GateAction`` 是动作，两者一对多。"""
        produced = [decision_for(action) for action in GateAction]
        assert len(produced) == 4 and len(set(produced)) == 3

    def test_every_decision_is_a_ddl_value(self) -> None:
        allowed = {"pass_auto", "need_edit", "discard", "human_approved", "human_rejected"}
        assert {item.value for item in Decision} == allowed


class TestGateAction:
    @pytest.mark.parametrize("policy", list(AutoApprovePolicy))
    def test_c_is_always_discarded(self, policy: AutoApprovePolicy) -> None:
        assert gate_action(Grade.C, policy, round_no=1) is GateAction.DISCARD

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            (AutoApprovePolicy.OFF, GateAction.HUMAN_GATE),
            (AutoApprovePolicy.GRADE_A, GateAction.AUTO_PASS),
            (AutoApprovePolicy.GRADE_AB, GateAction.AUTO_PASS),
        ],
    )
    def test_a_grade_auto_passes_unless_the_policy_is_off(
        self, policy: AutoApprovePolicy, expected: GateAction
    ) -> None:
        assert gate_action(Grade.A, policy, round_no=1) is expected

    @pytest.mark.parametrize("round_no", [1, 2])
    def test_b_grade_goes_to_the_editor_within_budget(self, round_no: int) -> None:
        assert gate_action(Grade.B, AutoApprovePolicy.GRADE_A, round_no=round_no) is GateAction.EDIT

    @pytest.mark.parametrize("round_no", [3, 4])
    def test_b_grade_stops_editing_once_the_budget_is_spent(self, round_no: int) -> None:
        """R16：``round_no > REVISION_LIMIT`` ⇒ 不再烧 token 改稿，直接进闸。"""
        assert gate_action(Grade.B, AutoApprovePolicy.GRADE_A, round_no=round_no) is GateAction.HUMAN_GATE

    def test_off_policy_still_edits_before_gating(self) -> None:
        assert gate_action(Grade.B, AutoApprovePolicy.OFF, round_no=1) is GateAction.EDIT
        assert gate_action(Grade.B, AutoApprovePolicy.OFF, round_no=3) is GateAction.HUMAN_GATE

    def test_grade_ab_auto_passes_b_regardless_of_rounds(self) -> None:
        for round_no in (1, 2, 3, 9):
            assert gate_action(Grade.B, AutoApprovePolicy.GRADE_AB, round_no=round_no) is GateAction.AUTO_PASS

    def test_off_policy_never_auto_passes(self) -> None:
        for grade in Grade:
            assert gate_action(grade, AutoApprovePolicy.OFF, round_no=1) is not GateAction.AUTO_PASS


class TestAutoApprovedBy:
    def test_a_is_signed_auto_approve_a(self) -> None:
        assert auto_approved_by(Grade.A, AutoApprovePolicy.GRADE_A) == "auto_approve_A"

    def test_b_is_signed_auto_approve_ab(self) -> None:
        """§04.4.4 不变量 2 只要求 A 级署 ``auto_approve_A``；B 级必须看得出是被捎带的。"""
        assert auto_approved_by(Grade.B, AutoApprovePolicy.GRADE_AB) == "auto_approve_AB"


# ══════════════════════════════════════════════════════════════════════
# 改稿越界
# ══════════════════════════════════════════════════════════════════════


class TestAllowedSentences:
    def test_no_sentences_means_no_allowance(self) -> None:
        assert allowed_sentences([issue("global")], total=0) == set()

    def test_global_covers_everything(self) -> None:
        assert allowed_sentences([issue("global")], total=5) == {1, 2, 3, 4, 5}

    def test_hook_is_the_first_sentence(self) -> None:
        assert allowed_sentences([issue("hook")], total=20) == {1}

    def test_cta_is_the_last_sentence(self) -> None:
        assert allowed_sentences([issue("cta")], total=20) == {20}

    @pytest.mark.parametrize(
        ("index", "expected"),
        [
            (1, {1, 2, 3, 4, 5, 6, 7}),
            (2, {8, 9, 10, 11, 12, 13, 14}),
            (3, {15, 16, 17, 18, 19, 20}),
        ],
    )
    def test_segments_split_the_sentences_by_ceil(self, index: int, expected: set[int]) -> None:
        assert allowed_sentences([issue(f"segment:{index}")], total=20, segments=3) == expected

    def test_segment_beyond_the_outline_grants_nothing(self) -> None:
        assert allowed_sentences([issue("segment:4")], total=20, segments=3) == set()

    def test_segment_zero_is_ignored(self) -> None:
        assert allowed_sentences([issue("segment:0")], total=20, segments=3) == set()

    def test_a_non_numeric_segment_is_ignored(self) -> None:
        assert allowed_sentences([issue("segment:x")], total=20, segments=3) == set()

    def test_an_unknown_target_grants_nothing(self) -> None:
        assert allowed_sentences([issue("paragraph:2")], total=20, segments=3) == set()

    def test_targets_are_unioned(self) -> None:
        assert allowed_sentences([issue("hook"), issue("cta")], total=20) == {1, 20}

    def test_whitespace_around_a_target_is_tolerated(self) -> None:
        assert allowed_sentences([issue(" hook ")], total=20) == {1}


class TestCheckEdit:
    def test_no_issues_is_itself_a_problem(self) -> None:
        """没有授权范围 ⇒ 任何改动都无从谈起，先把这件事说出来。"""
        before = lines()
        assert check_edit(before, before.model_copy(deep=True), []).problems == ["edited:no_issues"]

    def test_an_untouched_script_is_clean(self) -> None:
        before = lines()
        report = check_edit(before, before.model_copy(deep=True), [issue("segment:1")], segments=3)
        assert report.ok and report.changed_sentences == 0

    def test_editing_an_allowed_sentence_is_clean(self) -> None:
        before = lines()
        report = check_edit(before, edit_sentence(before, 7, "改过的第7句"), [issue("segment:1")], segments=3)
        assert report.ok and report.changed_sentences == 1

    def test_editing_a_sentence_outside_the_scope_is_reported(self) -> None:
        before = lines()
        report = check_edit(
            before, edit_sentence(before, 8, "不该动的第8句"), [issue("segment:1")], segments=3
        )
        assert report.problems == ["edited:out_of_scope:第8句"]

    def test_hook_change_needs_a_hook_issue(self) -> None:
        before = lines()
        after = before.model_copy(update={"hook": "新开场"})
        assert check_edit(before, after, [issue("segment:1")], segments=3).problems == [
            "edited:out_of_scope:hook"
        ]
        assert check_edit(before, after, [issue("hook")], segments=3).ok

    def test_cta_change_needs_a_cta_issue(self) -> None:
        before = lines()
        after = before.model_copy(update={"cta": "新结尾"})
        assert check_edit(before, after, [issue("segment:1")], segments=3).problems == [
            "edited:out_of_scope:cta"
        ]
        assert check_edit(before, after, [issue("cta")], segments=3).ok

    def test_global_covers_hook_body_and_cta(self) -> None:
        before = lines()
        after = edit_sentence(
            before.model_copy(update={"hook": "新开场", "cta": "新结尾"}), 20, "改过的第20句"
        )
        assert check_edit(before, after, [issue("global")], segments=3).ok

    def test_a_changed_sentence_count_is_a_warning_not_a_problem(self) -> None:
        before = lines()
        after = before.model_copy(
            update={
                "sentences": [
                    *before.sentences,
                    SentenceSpec(seq=21, text="新加的一句", speaker="bigbear"),
                ]
            }
        )
        report = check_edit(before, after, [issue("global")], segments=3)
        assert report.ok
        assert report.warnings == ["edit:sentence_count:20->21"]

    def test_allowed_targets_are_deduplicated_and_sorted(self) -> None:
        before = lines()
        report = check_edit(
            before,
            before.model_copy(deep=True),
            [issue("segment:3"), issue("hook"), issue("hook")],
            segments=3,
        )
        assert report.allowed_targets == ["hook", "segment:3"]

    def test_describe_joins_the_problems(self) -> None:
        assert EditReport(problems=["a", "b"]).describe() == "a；b"

    def test_an_empty_report_is_ok(self) -> None:
        assert EditReport().ok
