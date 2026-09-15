"""写稿领域契约（T1.10 · §04.1.4 / §04.1.5）。

这里穷举的是"什么算合格"：三道闸门（结构 / 规则 / 切分）全部用纯函数表达，
所以每条规则都能被一个用例钉住 —— 不需要跑 LLM，也不需要数据库。
"""

from __future__ import annotations

import pytest

from studio.domain.script import (
    CATCHPHRASE_MIN_HITS,
    FORBIDDEN_PREFIX,
    SEGMENT_CHARS_MAX,
    SEGMENT_CHARS_MIN,
    SENTENCE_MAX_CHARS,
    DirectorOutput,
    ScriptRules,
    ScriptSegment,
    SentenceSpec,
    WriterOutput,
    build_draft,
    catchphrase_hits,
    check_outline,
    check_script,
    enforce_sentence_limit,
    estimate_duration_ms,
    find_forbidden,
    rewrite_hint,
    speaker_ratio,
)
from tests.unit.agents.fakes import persona

CATCHPHRASES = ["这不科学", "俺寻思"]
FORBIDDEN = ["脏话", "政治"]


def _segments(count: int = 3, chars: int = 200) -> list[ScriptSegment]:
    return [
        ScriptSegment(seq=index, point=f"要点{index}", visual=f"画面{index}", mood="兴奋", est_chars=chars)
        for index in range(1, count + 1)
    ]


def _outline(*, count: int = 3, chars: int = 200) -> DirectorOutput:
    return DirectorOutput(
        hook_3s="熊大又整活了",
        segments=_segments(count, chars),
        cta="点个关注看下集",
        est_duration_ms=140_000,
    )


def _sentence(seq: int, text: str, speaker: str = "bigbear") -> SentenceSpec:
    return SentenceSpec(seq=seq, text=text, speaker=speaker)  # type: ignore[arg-type]


def _sentences(
    count: int = 30, text: str = "这不科学，熊大又跑起来了。", speaker: str = "bigbear"
) -> list[SentenceSpec]:
    return [_sentence(index, text, speaker) for index in range(1, count + 1)]


def _body(word_count: int) -> str:
    """造一份恰好 ``word_count`` 个口播字的稿子（用不含口癖/禁区的填充字）。"""
    return "熊" * word_count


def _output(*, word_count: int = 700, sentences: list[SentenceSpec] | None = None) -> WriterOutput:
    return WriterOutput(
        title="标题",
        hook="开场",
        body_md=_body(word_count),
        cta="关注",
        sentences=sentences if sentences is not None else _sentences(),
        est_duration_ms=140_000,
    )


# ══════════════════════════════════════════════════════════════════════
# 结构闸：大纲
# ══════════════════════════════════════════════════════════════════════


class TestCheckOutline:
    def test_clean_outline_passes(self) -> None:
        assert check_outline(_outline()).ok

    def test_seq_must_be_contiguous_from_one(self) -> None:
        outline = _outline()
        outline.segments[1].seq = 5
        report = check_outline(outline)
        assert not report.ok
        assert any("seq" in problem for problem in report.problems)

    def test_total_chars_below_range_is_rejected(self) -> None:
        report = check_outline(_outline(count=3, chars=SEGMENT_CHARS_MIN))
        assert not report.ok
        assert any("合计" in problem for problem in report.problems)

    def test_total_chars_above_range_is_rejected(self) -> None:
        report = check_outline(_outline(count=3, chars=SEGMENT_CHARS_MAX))
        assert not report.ok

    def test_blank_hook_is_rejected(self) -> None:
        outline = _outline()
        outline.hook_3s = "   "
        assert not check_outline(outline).ok

    def test_describe_joins_problems(self) -> None:
        outline = _outline()
        outline.cta = "  "
        assert "cta" in check_outline(outline).describe()

    def test_segment_count_is_enforced_by_the_model(self) -> None:
        with pytest.raises(ValueError):
            _outline(count=2)

    def test_segment_chars_are_enforced_by_the_model(self) -> None:
        with pytest.raises(ValueError):
            ScriptSegment(seq=1, point="p", visual="v", mood="m", est_chars=SEGMENT_CHARS_MIN - 1)


# ══════════════════════════════════════════════════════════════════════
# 规则闸：口癖 / 禁区 / 占比
# ══════════════════════════════════════════════════════════════════════


class TestCatchphrasesAndForbidden:
    def test_hits_are_reported_in_persona_order(self) -> None:
        assert catchphrase_hits("俺寻思这不科学", CATCHPHRASES) == CATCHPHRASES

    def test_hits_ignore_whitespace(self) -> None:
        assert catchphrase_hits("这 不 科 学 啊", ["这不科学"]) == ["这不科学"]

    def test_no_hits(self) -> None:
        assert catchphrase_hits("今天天气不错", CATCHPHRASES) == []

    def test_forbidden_scan(self) -> None:
        assert find_forbidden("说点脏话", FORBIDDEN) == ["脏话"]

    def test_forbidden_scan_is_empty_when_clean(self) -> None:
        assert find_forbidden("干净内容", FORBIDDEN) == []


class TestSpeakerRatio:
    def test_weighted_by_chars_not_by_count(self) -> None:
        sentences = [
            _sentence(1, "熊" * 100, "bigbear"),
            _sentence(2, "熊二", "littlebear"),
        ]
        ratio = speaker_ratio(sentences)
        assert ratio["bigbear"] > 0.9
        assert ratio["littlebear"] < 0.1

    def test_empty_yields_empty(self) -> None:
        assert speaker_ratio([]) == {}

    def test_ratio_sums_to_one(self) -> None:
        ratio = speaker_ratio(_sentences(10))
        assert pytest.approx(sum(ratio.values()), abs=1e-3) == 1.0


# ══════════════════════════════════════════════════════════════════════
# 切分闸
# ══════════════════════════════════════════════════════════════════════


class TestEnforceSentenceLimit:
    def test_short_sentences_pass_through(self) -> None:
        sentences, splits = enforce_sentence_limit(_sentences(3))
        assert splits == 0
        assert [item.seq for item in sentences] == [1, 2, 3]

    def test_long_sentence_is_split_and_renumbered(self) -> None:
        long_text = "熊大说这不科学，" * 5
        sentences, splits = enforce_sentence_limit([_sentence(1, long_text)])
        assert splits >= 1
        assert all(len(item.text) <= SENTENCE_MAX_CHARS for item in sentences)
        assert [item.seq for item in sentences] == list(range(1, len(sentences) + 1))

    def test_middle_fragments_have_no_pause(self) -> None:
        long_text = "熊大说这不科学，" * 5
        sentences, _ = enforce_sentence_limit([_sentence(1, long_text)])
        assert sentences[0].pause_after_ms == 0
        assert sentences[-1].pause_after_ms == 200

    def test_empty_sentence_is_dropped(self) -> None:
        sentences, _ = enforce_sentence_limit([_sentence(1, "   ")])
        assert sentences == []


class TestEstimateDuration:
    def test_zero_for_empty(self) -> None:
        assert estimate_duration_ms(0) == 0

    def test_five_chars_per_second(self) -> None:
        assert estimate_duration_ms(700) == 140_000

    def test_custom_rate(self) -> None:
        assert estimate_duration_ms(100, chars_per_second=10.0) == 10_000


# ══════════════════════════════════════════════════════════════════════
# 规则闸：成稿
# ══════════════════════════════════════════════════════════════════════


class TestCheckScript:
    def test_clean_script_passes(self) -> None:
        body = "这不科学，俺寻思也是。" + _body(680)
        report = check_script(
            body_md=body, sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert report.ok
        assert report.catchphrase_hits == CATCHPHRASES

    def test_forbidden_hit_blocks(self) -> None:
        body = "说点脏话" + _body(690)
        report = check_script(
            body_md=body, sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert report.blocked
        assert any(problem.startswith(FORBIDDEN_PREFIX) for problem in report.problems)

    def test_word_count_below_range_is_a_problem(self) -> None:
        report = check_script(
            body_md=_body(300), sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert any("字数" in problem for problem in report.problems)

    def test_word_count_above_range_is_a_problem(self) -> None:
        report = check_script(
            body_md=_body(1200), sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert any("字数" in problem for problem in report.problems)

    def test_catchphrase_shortfall_is_a_problem(self) -> None:
        body = "这不科学。" + _body(690)
        report = check_script(
            body_md=body, sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert any("口癖" in problem for problem in report.problems)

    def test_single_speaker_is_a_warning_not_a_problem(self) -> None:
        body = "这不科学，俺寻思也是。" + _body(680)
        report = check_script(
            body_md=body, sentences=_sentences(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert report.ok
        assert any(item.startswith("single_speaker:") for item in report.warnings)

    def test_dominant_speaker_warning(self) -> None:
        body = "这不科学，俺寻思也是。" + _body(680)
        sentences = [_sentence(1, "熊" * 100, "bigbear"), _sentence(2, "熊二", "littlebear")]
        report = check_script(
            body_md=body, sentences=sentences, catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert any(item.startswith("speaker_ratio:") for item in report.warnings)

    def test_rules_override_thresholds(self) -> None:
        rules = ScriptRules(word_count_min=100, word_count_max=200, catchphrase_min_hits=0)
        report = check_script(
            body_md=_body(150),
            sentences=_sentences(),
            catchphrases=[],
            forbidden=[],
            rules=rules,
        )
        assert report.ok


class TestScriptRules:
    def test_defaults_match_the_spec(self) -> None:
        rules = ScriptRules()
        assert (rules.word_count_min, rules.word_count_max) == (600, 800)
        assert rules.catchphrase_min_hits == CATCHPHRASE_MIN_HITS
        assert rules.sentence_max_chars == SENTENCE_MAX_CHARS

    def test_from_persona_takes_the_editable_window(self) -> None:
        custom = persona().model_copy(update={"target_chars_min": 500, "target_chars_max": 900})
        rules = ScriptRules.from_persona(custom)
        assert (rules.word_count_min, rules.word_count_max) == (500, 900)


# ══════════════════════════════════════════════════════════════════════
# 汇总：build_draft
# ══════════════════════════════════════════════════════════════════════


class TestBuildDraft:
    def test_duration_comes_from_word_count_not_from_the_model(self) -> None:
        draft, _ = build_draft(
            outline=_outline(),
            output=_output(word_count=700),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
        )
        assert draft.word_count == 700
        assert draft.est_duration_ms == 140_000  # 而不是模型自报的 140_000 之外的值

    def test_split_count_is_recorded(self) -> None:
        long_one = _sentence(15, "熊大说这不科学，" * 5)
        output = _output(sentences=[*_sentences(14), long_one])
        draft, report = build_draft(
            outline=_outline(), output=output, catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert report.split_count >= 1
        assert all(len(item.text) <= SENTENCE_MAX_CHARS for item in draft.sentences)

    def test_warnings_are_carried_onto_the_draft(self) -> None:
        draft, _ = build_draft(
            outline=_outline(),
            output=_output(sentences=_sentences(speaker="bigbear")),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
        )
        assert any(item.startswith("single_speaker:") for item in draft.warnings)

    def test_outline_is_kept_on_the_draft(self) -> None:
        outline = _outline()
        draft, _ = build_draft(
            outline=outline, output=_output(), catchphrases=CATCHPHRASES, forbidden=FORBIDDEN
        )
        assert draft.outline is outline


class TestRewriteHint:
    def test_empty_when_clean(self) -> None:
        report = check_script(
            body_md="这不科学，俺寻思也是。" + _body(680),
            sentences=_sentences(),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
        )
        assert rewrite_hint(report) == ""

    def test_mentions_the_problem(self) -> None:
        report = check_script(body_md=_body(100), sentences=_sentences(), catchphrases=[], forbidden=[])
        hint = rewrite_hint(report)
        assert "字数" in hint and hint.startswith("上一版不合格")
