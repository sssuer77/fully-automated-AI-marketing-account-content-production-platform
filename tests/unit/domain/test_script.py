"""写稿领域契约（T1.10 · §04.1.4 / §04.1.5）。

这里穷举的是"什么算合格"：三道闸门（结构 / 规则 / 切分）全部用纯函数表达，
所以每条规则都能被一个用例钉住 —— 不需要跑 LLM，也不需要数据库。
"""

from __future__ import annotations

import pytest

from studio.domain.script import (
    CATCHPHRASE_MIN_HITS,
    ENDING_QUESTION_PROBLEM,
    FORBIDDEN_PREFIX,
    SEGMENT_CHARS_MAX,
    SEGMENT_CHARS_MIN,
    SENTENCE_MAX_CHARS,
    DirectorOutput,
    ScriptRules,
    ScriptSegment,
    SentenceSpec,
    WriterOutput,
    audience_speaker_labels,
    build_draft,
    catchphrase_hits,
    check_outline,
    check_outline_title,
    check_script,
    ending_asks_audience,
    enforce_sentence_limit,
    estimate_duration_ms,
    find_forbidden,
    rewrite_hint,
    speaker_label_problems,
    speaker_ratio,
    strip_speaker_labels,
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
# 说话人标签（观众可见字段里不许出现"谁在说"）
# ══════════════════════════════════════════════════════════════════════


class TestAudienceSpeakerLabels:
    """判据是**形态**（短标签 + 冒号），不是名字表 —— 名字只活在 persona 的自由文本里。"""

    def test_the_real_machine_title_is_caught(self) -> None:
        """真机那条（2026-09-23）：二级标题把一级标题前面挂了个「熊大：」。"""
        title = "老人登记遗体捐献被拒收，熊大：凉的不是他一个人的心"
        assert audience_speaker_labels(title) == ["熊大："]

    def test_a_normal_colon_title_is_not_flagged(self) -> None:
        """一级标题本来就用冒号（`…被拒收：制度别凉了心`）—— 不能把正文当标签。"""
        assert audience_speaker_labels("退休捐献被拒收：制度别凉了心") == []

    def test_an_enumeration_colon_is_not_flagged(self) -> None:
        """顿号枚举后面接冒号也不是标签（`0蔗糖、0添加糖、无糖：…`）。"""
        assert audience_speaker_labels("0蔗糖、0添加糖、无糖：三行字能把人绕晕") == []

    def test_a_label_at_the_very_start_is_caught(self) -> None:
        assert audience_speaker_labels("熊二：这不科学") == ["熊二："]

    def test_an_english_label_is_caught(self) -> None:
        assert audience_speaker_labels("littlebear: that is not science")

    def test_the_problem_names_the_label(self) -> None:
        problems = speaker_label_problems("标题", "熊大：凉的不是他一个人的心")
        assert len(problems) == 1 and "熊大：" in problems[0]

    def test_clean_fields_have_no_problem(self) -> None:
        assert speaker_label_problems("配料表越短越干净？", "开场第一句", "看具体写了啥。") == []


class TestStripSpeakerLabels:
    def test_line_leading_labels_are_stripped_and_counted(self) -> None:
        body = "熊大：这回你结论反了。\n熊二：啥假信条？\n熊大：行数少，不等于干净。"
        stripped, count = strip_speaker_labels(body)
        assert count == 3
        assert stripped == "这回你结论反了。\n啥假信条？\n行数少，不等于干净。"

    def test_a_clean_body_is_untouched(self) -> None:
        stripped, count = strip_speaker_labels("行数少，不等于干净。\n看具体写了啥。")
        assert count == 0 and stripped == "行数少，不等于干净。\n看具体写了啥。"

    def test_a_colon_inside_a_line_is_content_not_a_label(self) -> None:
        """行中间的"说："是正文 —— 删了就是改内容（只认行首）。"""
        body = "他后来说：便宜的东西代价都在别处。"
        stripped, count = strip_speaker_labels(body)
        assert count == 0 and stripped == body

    def test_the_trailing_newline_survives(self) -> None:
        stripped, count = strip_speaker_labels("熊大：开场。\n")
        assert (stripped, count) == ("开场。\n", 1)


class TestCheckOutlineTitle:
    def test_a_label_in_the_title_is_rejected(self) -> None:
        report = check_outline_title("老人登记遗体捐献被拒收，熊大：凉的不是他一个人的心")
        assert not report.ok

    def test_a_clean_title_passes(self) -> None:
        assert check_outline_title("退休捐献被拒收：制度别凉了心").ok


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

    def test_cta_question_is_rejected(self) -> None:
        """结尾向观众提问 ⇒ 大纲层就拦下（Writer 不会继承一个问句收尾）。"""
        outline = _outline()
        outline.cta = "评论区打俩字：牛肉还是五仁？"
        report = check_outline(outline)
        assert not report.ok
        assert ENDING_QUESTION_PROBLEM in report.problems

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

    def test_ending_question_is_a_problem(self) -> None:
        """结尾提问**不改字数** ⇒ 只能靠这条判据拦（用户：不要在文稿最后给观众抛出问题）。"""
        report = check_script(
            body_md="这不科学，俺寻思也是。" + _body(680),
            sentences=_sentences(),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
            cta="想看下回俺们还拆啥，点个关注别走丢？",
        )
        assert not report.ok
        assert ENDING_QUESTION_PROBLEM in report.problems

    def test_a_speaker_label_in_the_title_is_a_problem(self) -> None:
        report = check_script(
            body_md="这不科学，俺寻思也是。" + _body(680),
            sentences=_sentences(),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
            title="老人登记遗体捐献被拒收，熊大：凉的不是他一个人的心",
        )
        assert not report.ok
        assert any("说话人标签" in problem for problem in report.problems)

    def test_a_clean_title_and_hook_are_not_a_problem(self) -> None:
        report = check_script(
            body_md="这不科学，俺寻思也是。" + _body(680),
            sentences=_sentences(),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
            title="退休捐献被拒收：制度别凉了心",
            hook="老人登记遗体捐献，转头被拒收。",
        )
        assert report.ok

    def test_a_flat_ending_is_not_a_problem(self) -> None:
        report = check_script(
            body_md="这不科学，俺寻思也是。" + _body(680),
            sentences=_sentences(),
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
            cta="便宜的东西，代价都在别处。",
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

    def test_line_leading_labels_never_reach_the_draft(self) -> None:
        """真机两条稿子每行都写着 `熊大：` ⇒ 落库的 body_md 与逐句表里都不该再有它。

        连带后果是**字数**：标签混在里面会把口播字数撑起来（真机 726 里 120 个是标签）。
        """
        output = _output(word_count=600)
        labelled = output.model_copy(
            update={
                "body_md": "\n".join("熊大：" + line for line in output.body_md.splitlines()),
                "sentences": [
                    item.model_copy(update={"text": "熊二：" + item.text}) for item in output.sentences
                ],
            }
        )
        draft, report = build_draft(
            outline=_outline(),
            output=labelled,
            catchphrases=CATCHPHRASES,
            forbidden=FORBIDDEN,
        )
        assert "熊大：" not in draft.body_md
        assert all(not item.text.startswith("熊二：") for item in draft.sentences)
        assert draft.word_count == 600
        assert report.speaker_labels_stripped == len(labelled.sentences) + 1
        assert any(item.startswith("speaker_label_stripped:") for item in draft.warnings)

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


# ══════════════════════════════════════════════════════════════════════
# 结尾不得向观众提问
# ══════════════════════════════════════════════════════════════════════


class TestEndingAsksAudience:
    """用户口径：**不要在文稿最后给观众抛出问题** —— 结尾的职责是把观点钉死。

    判据落在 ``cta`` 上，而不是"最后一句口播"：``cta`` 就是这条片子的落点，
    它同时会当作发布文案 —— 在这里提问，等于把问题留在片尾又留在简介里。
    """

    def test_question_mark_at_the_end_is_caught(self) -> None:
        assert ending_asks_audience("评论区打俩字：牛肉还是五仁？")

    def test_half_width_question_mark_is_caught(self) -> None:
        assert ending_asks_audience("想看下回拆啥?")

    def test_a_question_in_the_middle_is_caught_too(self) -> None:
        """前半句提问、后半句求关注 —— 一样是把问题踢回给观众。"""
        assert ending_asks_audience("这钱谁掏？关注俺们接着算。")

    def test_a_flat_ending_passes(self) -> None:
        assert ending_asks_audience("便宜的东西，代价都在别处。") is None

    def test_blank_passes(self) -> None:
        """空 ``cta`` 由"不得为空"那条管，不在这里重复报错。"""
        assert ending_asks_audience("") is None
