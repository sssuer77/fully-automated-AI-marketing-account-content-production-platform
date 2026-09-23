"""谁在什么时候讲话（T6.5 追加）—— 人物贴图"换图"的唯一依据。

验收四条：

1. **时间轴优先**：它带停顿（``pause_after_ms`` 已经填进母带），cue 是首尾相接的，
   拿 cue 算会越到后面越偏；
2. **相邻区间合并**：同一人连讲两句、中间停 200ms，不合并那张嘴会闪 6 帧；
3. **对不上号就退回 cue，再不行就空**，且空的时候**必有一条 ``note``**；
4. **拿不到就说拿不到** —— 一条依据都没有时返回空计划 + 原因，而不是默默不换。
"""

from __future__ import annotations

from typing import Any

import pytest

from studio.render.speech import (
    MERGE_GAP_MS,
    SpeakingPlan,
    merge_intervals,
    speaking_plan,
)
from studio.render.subtitle import Cue


def _row(speaker: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    """``timeline.json`` 里的一行（只留这个模块要用的三列）。"""
    return {"speaker": speaker, "start_ms": start_ms, "end_ms": end_ms}


def _cue(start_ms: int, end_ms: int) -> Cue:
    return Cue(start_ms=start_ms, end_ms=end_ms, text="词")


# ══════════════════════════════════════════════════════════════════════
# 合并
# ══════════════════════════════════════════════════════════════════════


def test_overlapping_intervals_are_merged() -> None:
    assert merge_intervals([(0, 1000), (500, 1500)]) == ((0, 1500),)


def test_a_short_gap_is_merged() -> None:
    """句间停顿默认 200ms —— 不合并的话，嘴会在停顿里闭上再张开（30fps 下 6 帧闪动）。"""
    assert merge_intervals([(0, 1000), (1000 + MERGE_GAP_MS, 2000)]) == ((0, 2000),)


def test_a_long_gap_is_kept() -> None:
    """停顿长到能看出来，就**不**合并 —— 那时他真的停下来了。"""
    spans = merge_intervals([(0, 1000), (1000 + MERGE_GAP_MS + 1, 2000)])
    assert spans == ((0, 1000), (1001 + MERGE_GAP_MS, 2000))


def test_zero_length_intervals_are_dropped() -> None:
    assert merge_intervals([(1000, 1000), (1000, 900)]) == ()


def test_merge_sorts_before_merging() -> None:
    """时间轴理论上是有序的，但"理论上"不是判据 —— 乱序进来也要并成一段。"""
    assert merge_intervals([(5000, 6000), (0, 1000), (1500, 2000)]) == ((0, 1000), (1500, 2000), (5000, 6000))


# ══════════════════════════════════════════════════════════════════════
# ① 时间轴优先
# ══════════════════════════════════════════════════════════════════════


def test_the_timeline_wins_and_keeps_the_pauses() -> None:
    """时间轴里的区间**带停顿**：第 2 句从 6812 开始，不是从第 1 句结束的 6440。"""
    plan = speaking_plan(
        speakers=("bigbear", "littlebear"),
        timeline_rows=[_row("bigbear", 0, 6440), _row("littlebear", 6812, 8492)],
        cues=[_cue(0, 6440), _cue(6440, 8120)],  # cue 是首尾相接的（少了那 372ms）
    )
    assert plan.source == "timeline"
    assert plan.intervals_for("littlebear") == ((6812, 8492),)


def test_the_timeline_wins_even_when_the_cues_disagree() -> None:
    """★ 两条路都在，用时间轴那条 —— 这条断言就是"越到后面越偏"的防线。"""
    plan = speaking_plan(
        speakers=("bigbear", "bigbear"),
        timeline_rows=[_row("bigbear", 0, 1000), _row("bigbear", 3000, 4000)],
        cues=[_cue(0, 1000), _cue(1000, 2000)],
    )
    assert plan.source == "timeline"
    assert plan.intervals_for("bigbear") == ((0, 1000), (3000, 4000))


def test_intervals_are_grouped_by_speaker() -> None:
    plan = speaking_plan(
        speakers=("bigbear", "littlebear", "bigbear"),
        timeline_rows=[
            _row("bigbear", 0, 1000),
            _row("littlebear", 1000, 2000),
            _row("bigbear", 2000, 3000),
        ],
    )
    assert set(plan.by_speaker) == {"bigbear", "littlebear"}
    assert plan.intervals_for("bigbear") == ((0, 1000), (2000, 3000)), "中间那句是别人的"
    assert plan.intervals_for("littlebear") == ((1000, 2000),)


def test_a_speaker_the_script_does_not_know_is_empty() -> None:
    plan = speaking_plan(speakers=("bigbear",), timeline_rows=[_row("bigbear", 0, 1000)])
    assert plan.intervals_for("nobody") == ()


def test_an_empty_speaker_never_matches_anything() -> None:
    """★ 名字为空是"不知道"，**不是**"某个叫空字符串的人" —— 空名字永远给空区间。"""
    plan = speaking_plan(
        speakers=("", ""),
        timeline_rows=[_row("", 0, 1000), _row("", 2000, 3000)],
    )
    assert plan.source == "none"
    assert plan.intervals_for("") == ()


# ══════════════════════════════════════════════════════════════════════
# ② 退回 cue
# ══════════════════════════════════════════════════════════════════════


def test_cues_are_used_when_the_timeline_is_missing() -> None:
    plan = speaking_plan(speakers=("bigbear",), timeline_rows=None, cues=[_cue(0, 1000)])
    assert plan.source == "cues"
    assert plan.intervals_for("bigbear") == ((0, 1000),)


def test_an_empty_speaker_in_the_timeline_is_filled_in_from_the_request() -> None:
    """★ 真机上出现过"时间轴被出片那一步的兜底写法覆盖成 ``speaker=""``"。

    这时**仍然用时间轴那份时间**（它带停顿），说话人按序号从请求里补 —— 退回 cue 会
    把停顿丢掉，越到后面越偏。
    """
    plan = speaking_plan(
        speakers=("bigbear", "littlebear"),
        timeline_rows=[_row("", 0, 6440), _row("", 6812, 8492)],
        cues=[_cue(0, 6440), _cue(6440, 8120)],
    )
    assert plan.source == "timeline"
    assert plan.intervals_for("littlebear") == ((6812, 8492),)


def test_the_timeline_fills_in_the_speaker_by_position() -> None:
    """时间轴里没有说话人、但请求里带了 ⇒ 按**序号**补上（两者同序，安全）。"""
    plan = speaking_plan(
        speakers=("bigbear", "littlebear"),
        timeline_rows=[_row("", 0, 1000), _row("", 2000, 3000)],
    )
    assert plan.source == "timeline"
    assert plan.intervals_for("littlebear") == ((2000, 3000),)


def test_a_row_count_that_does_not_match_is_not_used() -> None:
    """★ 条数对不上 ⇒ 这份时间轴是上一轮的，**拿它驱动换图比不换更糟**（嘴按别人的节奏开合）。"""
    plan = speaking_plan(
        speakers=("bigbear", "littlebear", "bigbear"),
        timeline_rows=[_row("bigbear", 0, 1000)],
        cues=[_cue(0, 1000), _cue(1000, 2000), _cue(2000, 3000)],
    )
    assert plan.source == "cues", "时间轴作废 ⇒ 退回 cue"
    assert plan.intervals_for("bigbear") == ((0, 1000), (2000, 3000))


def test_a_row_without_usable_timestamps_discards_the_timeline() -> None:
    plan = speaking_plan(
        speakers=("bigbear",),
        timeline_rows=[{"speaker": "bigbear", "start_ms": None, "end_ms": 1000}],
        cues=[_cue(0, 1000)],
    )
    assert plan.source == "cues"


def test_a_cue_count_that_does_not_match_is_not_used() -> None:
    plan = speaking_plan(speakers=("bigbear",), timeline_rows=None, cues=[_cue(0, 1000), _cue(1000, 2000)])
    assert plan.source == "none"


def test_cues_are_matched_by_position() -> None:
    plan = speaking_plan(
        speakers=("bigbear", "littlebear"),
        timeline_rows=None,
        cues=[_cue(0, 1000), _cue(1000, 2000)],
    )
    assert plan.intervals_for("littlebear") == ((1000, 2000),)


# ══════════════════════════════════════════════════════════════════════
# ③ 拿不到就说拿不到
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("speakers", "timeline_rows", "expected"),
    [
        ((), None, "没有拿到逐句说话人"),
        (("bigbear",), None, "没有可用的时间轴"),
        (("",), [_row("", 0, 1000)], "都没有说话人"),
    ],
)
def test_an_empty_plan_always_says_why(
    speakers: tuple[str, ...],
    timeline_rows: list[dict[str, Any]] | None,
    expected: str,
) -> None:
    """★ 一条依据都没有 ⇒ **空计划 + 原因**。默默不换，与"功能没做"在界面上长得一模一样。"""
    plan = speaking_plan(speakers=speakers, timeline_rows=timeline_rows)
    assert plan.source == "none"
    assert plan.by_speaker == {}
    assert expected in (plan.note or "")


def test_a_plan_is_json_ready() -> None:
    plan = speaking_plan(speakers=("bigbear",), timeline_rows=[_row("bigbear", 0, 1000)])
    assert plan.to_dict() == {
        "source": "timeline",
        "note": None,
        "speakers": {"bigbear": [[0, 1000]]},
    }


def test_an_empty_plan_is_json_ready() -> None:
    plan = SpeakingPlan(by_speaker={}, source="none", note="没有依据")
    assert plan.to_dict() == {"source": "none", "note": "没有依据", "speakers": {}}
