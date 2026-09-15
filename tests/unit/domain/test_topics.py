"""选题池领域契约与算法（T1.9 · §04.1.2 / §04.1.3 / §04.1.7）。

这里钉的是**产品级不变式**，不是某次调用的实现细节：

- 归一化哈希（R15 一级去重）必须"同一模板换数字也算同一条"；
- 相似度（二级去重）必须"换个说法还能认出来"；
- 四条规则各自的处理方式**刻意不同**（丢弃 / 违规 / 降权），不能被"统一"掉。

全部是纯函数 ⇒ 可 golden、可复算、不需要任何 I/O。
"""

from __future__ import annotations

import pytest

from studio.domain.topics import (
    COMPLAINT_DEMOTION,
    DEDUP_MATCH_LIMIT,
    DEDUP_SIMILARITY_PENALTY,
    DEDUP_SIMILARITY_THRESHOLD,
    DIRECTION_COUNT_MIN,
    MIN_FIT_SCORE,
    RISK_COMPLAINED_TOPIC,
    RISK_LOW_GROUNDING,
    ClassifiedItem,
    DedupAction,
    DirectionSpec,
    ExistingTopic,
    FeedbackItemSpec,
    GroundingRef,
    build_feedback_digest,
    dedup_topic,
    hash_title,
    normalize_title,
    plan_directions,
    sentiment_to_kind,
    similarity,
    top_phrases,
)

# ══════════════════════════════════════════════════════════════════════
# 构造器
# ══════════════════════════════════════════════════════════════════════


def _direction(
    title: str,
    *,
    fit: int = 8,
    priority: int = 100,
    refs: list[GroundingRef] | None = None,
    risks: list[str] | None = None,
) -> DirectionSpec:
    return DirectionSpec(
        title=title,
        rationale=f"{title} 的理由",
        grounded_on=refs if refs is not None else [GroundingRef(type="persona", quote="账号定位")],
        priority=priority,
        risk_flags=list(risks or []),
        fit_score=fit,
    )


def _five(
    *,
    refs: list[GroundingRef] | None = None,
    risks: list[str] | None = None,
) -> list[DirectionSpec]:
    """五个方向 —— 够 ``DIRECTION_COUNT_MIN``，从而把"条数不足"这条违规隔离掉。"""
    return [_direction(f"方向{i}", refs=refs, risks=risks) for i in range(1, 6)]


def _hot_ref() -> GroundingRef:
    return GroundingRef(type="hot", ref_id="hot-1", quote="MC 跑酷新版本")


def _want_ref() -> GroundingRef:
    return GroundingRef(type="feedback", ref_id="fb-1", kind="want", quote="想看跑酷")


# ══════════════════════════════════════════════════════════════════════
# 归一化与哈希（R15 一级去重的地基）
# ══════════════════════════════════════════════════════════════════════


class TestNormalize:
    def test_full_width_and_case_are_folded(self) -> None:
        assert normalize_title("ＭＣ 跑酷") == "mcparkour"

    def test_punctuation_and_spaces_are_stripped(self) -> None:
        assert normalize_title("MC跑酷：5 个必看技巧！") == "mcparkour5个必看技巧"

    def test_synonyms_replace_longest_first(self) -> None:
        assert normalize_title("我的世界生存") == "mc生存"

    def test_symbol_only_title_normalizes_to_empty(self) -> None:
        assert normalize_title("！！！？？？") == ""

    def test_hash_folds_digits(self) -> None:
        """同一个模板换个数字 ⇒ 一级去重就该命中（数字差异留给二级相似度表达）。"""
        assert hash_title("MC跑酷5个技巧") == hash_title("我的世界跑酷7个技巧")

    def test_hash_keeps_genuinely_different_topics_apart(self) -> None:
        assert hash_title("MC跑酷5个技巧") != hash_title("MC跑酷地图推荐")


class TestSimilarity:
    def test_identical_after_normalization(self) -> None:
        assert similarity("MC跑酷5个技巧", "我的世界 跑酷 5 个技巧！") == pytest.approx(1.0)

    def test_empty_side_is_zero(self) -> None:
        assert similarity("！！！", "MC跑酷") == 0.0

    def test_near_duplicate_reaches_threshold(self) -> None:
        assert similarity("MC跑酷必看技巧合集", "MC跑酷必看技巧大全") >= DEDUP_SIMILARITY_THRESHOLD

    def test_unrelated_stays_low(self) -> None:
        assert similarity("MC跑酷必看技巧", "熊出没大电影") < 0.5


class TestTopPhrases:
    def test_counts_cjk_bigrams(self) -> None:
        ranked = dict(top_phrases(["MC跑酷太难了", "MC跑酷真好玩", "MC跑酷第一"], min_count=2))
        assert ranked["mc"] == 3

    def test_stop_grams_are_excluded(self) -> None:
        grams = {gram for gram, _ in top_phrases(["这个东西真的太好了"] * 2, top_n=50, min_count=2)}
        assert "个东" in grams
        assert "真的" not in grams

    def test_rare_grams_are_filtered_by_min_count(self) -> None:
        assert top_phrases(["独一无二的标题"], min_count=2) == []


# ══════════════════════════════════════════════════════════════════════
# 两级去重（§04.1.3）
# ══════════════════════════════════════════════════════════════════════


class TestDedupTopic:
    def test_hash_hit_drops(self) -> None:
        result = dedup_topic("我的世界跑酷7个技巧", [ExistingTopic(id="t1", title="MC跑酷5个技巧")])
        assert result.action is DedupAction.DROP
        assert result.dedup_hash == hash_title("MC跑酷5个技巧")
        assert result.similar_to[0].target_id == "t1"

    def test_similar_title_demotes_with_penalty(self) -> None:
        existing = [ExistingTopic(id="t1", title="MC跑酷必看技巧合集")]
        result = dedup_topic("MC跑酷必看技巧大全", existing)
        assert result.action is DedupAction.DEMOTE
        assert result.score_delta == -DEDUP_SIMILARITY_PENALTY
        assert result.similarity >= DEDUP_SIMILARITY_THRESHOLD
        assert result.similar_to[0].target_title == "MC跑酷必看技巧合集"

    def test_unrelated_keeps(self) -> None:
        result = dedup_topic("熊出没大电影", [ExistingTopic(id="t1", title="MC跑酷必看技巧")])
        assert result.action is DedupAction.KEEP
        assert result.score_delta == 0.0
        assert result.similar_to == []

    def test_matches_are_capped_and_sorted_by_similarity(self) -> None:
        tails = ["合集", "大全", "盘点", "推荐", "总结"]
        existing = [ExistingTopic(id=f"t{i}", title=f"MC跑酷必看技巧{tails[i - 1]}") for i in range(1, 6)]
        result = dedup_topic("MC跑酷必看技巧汇总", existing)
        assert result.action is DedupAction.DEMOTE
        assert len(result.similar_to) <= DEDUP_MATCH_LIMIT
        ratios = [match.similarity for match in result.similar_to]
        assert ratios == sorted(ratios, reverse=True)

    def test_stored_hash_is_reused_instead_of_recomputed(self) -> None:
        """库里的 ``dedup_hash`` 是**权威**：它可能来自旧版本的归一化规则。"""
        result = dedup_topic(
            "完全不同的标题", [ExistingTopic(id="t1", title="x", dedup_hash=hash_title("完全不同的标题"))]
        )
        assert result.action is DedupAction.DROP


# ══════════════════════════════════════════════════════════════════════
# Planner 四条规则（§04.1.2）
# ══════════════════════════════════════════════════════════════════════


class TestPlanDirections:
    def test_low_fit_is_dropped(self) -> None:
        kept, report = plan_directions(
            [_direction("跑偏的方向", fit=MIN_FIT_SCORE - 1), _direction("靠谱的方向")],
            hot_available=False,
            feedback_available=False,
        )
        assert [item.title for item in kept] == ["靠谱的方向"]
        assert report.dropped == ["跑偏的方向"]

    def test_complained_topic_is_demoted_not_dropped(self) -> None:
        kept, report = plan_directions(
            [_direction("被吐槽过的方向", priority=10, risks=[RISK_COMPLAINED_TOPIC])],
            hot_available=False,
            feedback_available=False,
        )
        assert [item.title for item in kept] == ["被吐槽过的方向"]
        assert kept[0].priority == 10 + COMPLAINT_DEMOTION
        assert report.demoted == ["被吐槽过的方向"]

    def test_priority_is_capped_at_999(self) -> None:
        kept, _ = plan_directions(
            [_direction("被吐槽过的方向", priority=900, risks=[RISK_COMPLAINED_TOPIC])],
            hot_available=False,
            feedback_available=False,
        )
        assert kept[0].priority == 999

    def test_low_grounding_marks_every_direction(self) -> None:
        kept, report = plan_directions(
            [_direction("仅凭人设")], hot_available=False, feedback_available=False
        )
        assert report.low_grounding is True
        assert kept[0].risk_flags == [RISK_LOW_GROUNDING]

    def test_missing_persona_grounding_is_a_violation(self) -> None:
        _, report = plan_directions(_five(refs=[]), hot_available=False, feedback_available=False)
        assert [item.rule for item in report.violations] == ["positioning"]

    def test_hot_rule_is_skipped_when_no_hot_is_available(self) -> None:
        _, report = plan_directions(_five(), hot_available=False, feedback_available=False)
        assert report.violations == []

    def test_hot_rule_fires_when_hot_is_available_but_unused(self) -> None:
        _, report = plan_directions(_five(), hot_available=True, feedback_available=False)
        assert [item.rule for item in report.violations] == ["hot"]

    def test_want_rule_fires_when_feedback_is_available_but_unused(self) -> None:
        refs = [GroundingRef(type="persona"), _hot_ref()]
        _, report = plan_directions(_five(refs=refs), hot_available=True, feedback_available=True)
        assert [item.rule for item in report.violations] == ["want"]

    def test_all_four_rules_pass(self) -> None:
        directions = [
            _direction(f"方向{i}", refs=[GroundingRef(type="persona"), _hot_ref(), _want_ref()])
            for i in range(5)
        ]
        kept, report = plan_directions(directions, hot_available=True, feedback_available=True)
        assert report.ok is True
        assert report.violations == []
        assert len(kept) == 5

    def test_too_few_directions_is_a_violation(self) -> None:
        directions = [_direction(f"方向{i}") for i in range(DIRECTION_COUNT_MIN - 1)]
        _, report = plan_directions(directions, hot_available=False, feedback_available=False)
        assert [item.rule for item in report.violations] == ["positioning"]
        assert "过滤后仅剩" in report.violations[0].detail

    def test_report_describe_is_empty_when_ok(self) -> None:
        refs = [GroundingRef(type="persona"), _hot_ref()]
        _, report = plan_directions(_five(refs=refs), hot_available=True, feedback_available=False)
        assert report.ok is True
        assert report.describe() == ""


# ══════════════════════════════════════════════════════════════════════
# 反馈摘要与分类
# ══════════════════════════════════════════════════════════════════════


class TestFeedbackDigest:
    def test_buckets_by_kind(self) -> None:
        items = [
            FeedbackItemSpec(kind="want", text="想看跑酷合集", raw_line="a", line_no=1),
            FeedbackItemSpec(kind="complaint", text="太吵了", raw_line="b", line_no=2),
            FeedbackItemSpec(kind="trend", text="最近都在刷这个", raw_line="c", line_no=3),
        ]
        digest = build_feedback_digest(items)
        assert digest.total == 3
        assert [item.text for item in digest.wants] == ["想看跑酷合集"]
        assert [item.text for item in digest.complaints] == ["太吵了"]
        assert [item.text for item in digest.trends] == ["最近都在刷这个"]

    def test_empty_input_is_empty_digest_not_none(self) -> None:
        digest = build_feedback_digest([])
        assert digest.is_empty is True
        assert digest.total == 0
        assert digest.top_topics == []

    def test_top_topics_comes_from_wants_and_complaints_only(self) -> None:
        """趋势条目**不参与**词频：4 条文本里只有 2 条 want ⇒ 计数是 4 而不是 8。"""
        items = [
            FeedbackItemSpec(kind="want", text="合集合集", raw_line="a", line_no=1),
            FeedbackItemSpec(kind="want", text="合集合集", raw_line="b", line_no=2),
            FeedbackItemSpec(kind="trend", text="合集合集", raw_line="c", line_no=3),
            FeedbackItemSpec(kind="trend", text="合集合集", raw_line="d", line_no=4),
        ]
        digest = build_feedback_digest(items)
        assert ("合集", 4) in digest.top_topics


class TestSentimentToKind:
    def test_want_wins_over_complaint(self) -> None:
        assert sentiment_to_kind(ClassifiedItem(ref="r", wants=["a"], complaints=["b"])) == "want"

    def test_complaint_only(self) -> None:
        assert sentiment_to_kind(ClassifiedItem(ref="r", complaints=["b"])) == "complaint"

    def test_pure_emotion_becomes_trend(self) -> None:
        assert sentiment_to_kind(ClassifiedItem(ref="r", sentiment="pos")) == "trend"
