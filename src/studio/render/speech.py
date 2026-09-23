"""谁在什么时候讲话（T6.5 追加）—— 人物贴图"换图"的唯一依据。

这一环回答的问题只有一个
------------------------
"第 3 句到第 5 句是 ``bigbear`` 在讲" ⇒ 那几段时间里，``bigbear`` 那一层贴图换成
讲话图，其余时间换回普通图。除了时间轴，这件事没有第二个来源 —— 所以这里只做
"句子 → 时间区间"的归拢，不碰贴图、不碰 ffmpeg。

为什么不直接用字幕那串 cue
--------------------------
字幕 cue 是**首尾相接**的（``render/subtitle.py::build_cues``：句间停顿不进 cue），
而人声母带里**真的有停顿**（``tts/timeline.py`` 用 ``apad`` 把 ``pause_after_ms``
填进音频）。拿 cue 当讲话区间，第 20 句之后就会偏出好几秒 —— 嘴在没人说话的时候
张着，而且越到后面越离谱。所以优先级是：

1. ``timeline.json`` 的句子行（**有停顿**，且自带 ``speaker``）—— 最准；
2. 拿不到时间轴 / 时间轴里没有说话人 ⇒ 退回 cue（首尾相接）+ 请求里带的逐句
   ``speaker``。它会偏，但至少"哪个人在讲"是对的；
3. 两者都拿不到 ⇒ **空**，并把原因写在 :attr:`SpeakingPlan.note` 里。

第 3 条不能省：换图这事一旦"没有依据"，正确的行为是**如实说没有依据**，
而不是默默不换 —— 后者与"功能没做"在界面上长得一模一样。

为什么相邻区间要合并
--------------------
同一个人连着讲两句、中间停 200ms：不合并的话，那张嘴会在 200ms 里闭上再张开
（30fps 下 6 帧）—— 在成片里是一下**闪动**，而不是"他停了一下"。停顿短到
:data:`MERGE_GAP_MS` 以内就并成一段。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from studio.render.subtitle import Cue

__all__ = [
    "MERGE_GAP_MS",
    "Interval",
    "SpeakingPlan",
    "SpeakingSource",
    "merge_intervals",
    "speaking_plan",
]

#: 讲话区间（毫秒，**闭区间** ``[start, end]``，与 ``TimelineSentence.start_ms/end_ms``
#: 同一口径）。闭区间而不是半开：这里的两个数最终要变成 ffmpeg 的
#: ``between(t, start, end)``，而它本身就是闭区间 —— 换个口径就要在中间做一次
#: "+1/-1" 的换算，那种换算在跨毫秒/秒的边界上最容易错半帧。
Interval = tuple[int, int]

#: 间隔小于这个毫秒数的两段讲话合并成一段（见模块注释"为什么相邻区间要合并"）。
#: 取 200 的理由：句间停顿的默认值就是 200ms（``script_sentences.pause_after_ms``），
#: 加上 ±80ms 抖动后落在 120–280ms —— 把 200 这条线放在这一段的中间，
#: "同一个人连着讲"几乎总能并上，而"两个人你一句我一句"本来就不在同一个 key 里。
MERGE_GAP_MS: Final[int] = 200

#: 讲话区间的来源（进 manifest —— "这条片子的换图是按哪份时间算的"要能查）。
SpeakingSource = Literal["timeline", "cues", "none"]


@dataclass(frozen=True, slots=True)
class _Span:
    """一句口播：谁、从哪到哪。"""

    speaker: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class SpeakingPlan:
    """逐说话人的讲话区间（**合并后**，按说话人分组）。

    ``source`` 是"这些毫秒是从哪份东西里读出来的"，``note`` 是"为什么一个区间都没有"
    —— 两个都要进 manifest：换图没生效时，人第一个要问的就是"它按什么判断的"。
    """

    by_speaker: Mapping[str, tuple[Interval, ...]]
    source: SpeakingSource
    note: str | None = None

    def intervals_for(self, speaker: str) -> tuple[Interval, ...]:
        """某个说话人的讲话区间；没这个人 / 名字为空 ⇒ 空元组（**不抛**）。"""
        if not speaker:
            return ()
        return self.by_speaker.get(speaker, ())

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "note": self.note,
            "speakers": {name: [list(item) for item in spans] for name, spans in self.by_speaker.items()},
        }


def merge_intervals(items: Iterable[Interval]) -> tuple[Interval, ...]:
    """排序 + 合并（重叠、相接、以及间隔 ≤ :data:`MERGE_GAP_MS` 的）。"""
    ordered = sorted((min(a, b), max(a, b)) for a, b in items if b > a)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start - merged[-1][1] <= MERGE_GAP_MS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return tuple(merged)


def _from_timeline(
    rows: Sequence[Mapping[str, Any]],
    speakers: Sequence[str],
) -> tuple[_Span, ...]:
    """时间轴句子行 ⇒ 句级区间（**带停顿**）。

    说话人优先取行里那个（配音阶段写下来的那份最权威）；行里没有就按**序号**取
    请求里带的那一份 —— 两者同源（都来自 ``script_sentences`` 的 ``seq`` 顺序），
    所以按下标对齐是安全的。行里那一份**不覆盖**请求那一份，是因为真机上出现过
    时间轴被出片这一步的兜底写法覆盖成 ``speaker=""`` 的情况（那一份没有停顿、
    也没有说话人）；此时"按序号补上说话人"仍然能救回来。

    条数对不上（换了稿子 / 时间轴是上一轮的）⇒ 返回空：拿一份**对不上号**的时间
    去驱动换图，比不换更糟 —— 嘴会按另一个人的节奏开合。
    """
    if not rows:
        return ()
    if speakers and len(speakers) != len(rows):
        return ()
    spans: list[_Span] = []
    for index, row in enumerate(rows):
        start = row.get("start_ms")
        end = row.get("end_ms")
        if not isinstance(start, int) or not isinstance(end, int):
            return ()
        fallback = speakers[index] if index < len(speakers) else ""
        who = str(row.get("speaker") or fallback)
        if who:
            spans.append(_Span(speaker=who, start_ms=start, end_ms=end))
    return tuple(spans)


def _from_cues(cues: Sequence[Cue], speakers: Sequence[str]) -> tuple[_Span, ...]:
    """字幕 cue ⇒ 句级区间（**首尾相接**，没有停顿；见模块注释的优先级 2）。"""
    if not cues or not speakers or len(speakers) != len(cues):
        return ()
    spans: list[_Span] = []
    for index, cue in enumerate(cues):
        who = speakers[index]
        if who:
            spans.append(_Span(speaker=who, start_ms=cue.start_ms, end_ms=cue.end_ms))
    return tuple(spans)


def _group(spans: Sequence[_Span]) -> dict[str, tuple[Interval, ...]]:
    grouped: dict[str, list[Interval]] = {}
    for span in spans:
        grouped.setdefault(span.speaker, []).append((span.start_ms, span.end_ms))
    return {name: merge_intervals(items) for name, items in grouped.items()}


def speaking_plan(
    *,
    speakers: Sequence[str],
    timeline_rows: Sequence[Mapping[str, Any]] | None = None,
    cues: Sequence[Cue] = (),
) -> SpeakingPlan:
    """这一次渲染的讲话区间（优先级见模块注释）。

    三个参数都是"能拿到就给"：``speakers`` 来自请求（``ProduceRequest.sentence_speakers``，
    与句子同序），``timeline_rows`` 来自盘上的 ``timeline.json``，``cues`` 是这次
    字幕用的那一份。
    """
    from_timeline = _from_timeline(timeline_rows or (), speakers)
    if from_timeline:
        return SpeakingPlan(by_speaker=_group(from_timeline), source="timeline")
    from_cues = _from_cues(cues, speakers)
    if from_cues:
        return SpeakingPlan(by_speaker=_group(from_cues), source="cues")

    if not speakers:
        note = "这次渲染没有拿到逐句说话人（稿件那条路没带 speaker）⇒ 无从判断谁在讲话"
    elif timeline_rows is None:
        note = "没有可用的时间轴（timeline.json 缺失或与这次句子对不上）⇒ 无从判断谁在讲话"
    else:
        note = "时间轴与字幕里都没有说话人 ⇒ 无从判断谁在讲话"
    return SpeakingPlan(by_speaker={}, source="none", note=note)
