"""稿件版本 diff 单测（T4.4 · §04.4.5）。

重点是**插入导致的偏移不能误报**：这是"逐 seq 对齐"最容易踩的坑 ——
在第 2 句后面插一句，第 3 句起全部显示成"改了"，用户会以为 Editor 把整篇
重写了一遍。块级匹配的意义就在这里，所以它必须有一条**专门的**用例。
"""

from __future__ import annotations

from dataclasses import dataclass

from studio.domain.diff import SentenceChange, diff_sentences, summarize_changes


@dataclass(frozen=True)
class Line:
    """最小实现：diff 只认 ``seq`` + ``text``。"""

    seq: int
    text: str


def lines(*texts: str) -> list[Line]:
    """按位置生成 ``seq`` 从 1 起的逐句表。"""
    return [Line(seq=index + 1, text=text) for index, text in enumerate(texts)]


def ops(changes: list[SentenceChange]) -> list[str]:
    return [change.op for change in changes]


def test_identical_versions_are_all_equal() -> None:
    changes = diff_sentences(lines("甲", "乙", "丙"), lines("甲", "乙", "丙"))
    assert ops(changes) == ["equal", "equal", "equal"]
    summary = summarize_changes(changes)
    assert (summary.unchanged, summary.changed, summary.added, summary.removed) == (3, 0, 0, 0)
    assert summary.total == 0


def test_single_replace_keeps_neighbours_equal() -> None:
    changes = diff_sentences(lines("甲", "乙", "丙"), lines("甲", "乙改", "丙"))
    assert ops(changes) == ["equal", "replace", "equal"]
    middle = changes[1]
    assert (middle.old_text, middle.new_text) == ("乙", "乙改")
    assert (middle.old_seq, middle.new_seq) == (2, 2)


def test_insert_does_not_shift_the_rest_into_replace() -> None:
    """★ 核心用例：中间插一句 ⇒ 只报 1 条 insert，其余仍是 equal。"""
    changes = diff_sentences(lines("甲", "乙", "丙", "丁"), lines("甲", "乙", "新", "丙", "丁"))
    assert ops(changes) == ["equal", "equal", "insert", "equal", "equal"]
    inserted = changes[2]
    assert inserted.old_seq is None
    assert inserted.new_seq == 3
    assert summarize_changes(changes).added == 1


def test_delete_is_reported_on_the_old_side() -> None:
    changes = diff_sentences(lines("甲", "乙", "丙"), lines("甲", "丙"))
    assert ops(changes) == ["equal", "delete", "equal"]
    removed = changes[1]
    assert (removed.old_seq, removed.new_seq) == (2, None)
    assert removed.new_text is None
    assert summarize_changes(changes).removed == 1


def test_replace_block_pairs_by_position_and_spills_the_tail() -> None:
    """一段 3 句换 2 句 ⇒ 2 条 replace + 1 条 delete（不硬凑成替换）。"""
    changes = diff_sentences(lines("甲", "乙", "丙", "丁"), lines("甲", "乙改", "丙改"))
    summary = summarize_changes(changes)
    assert summary.changed == 2
    assert summary.removed == 1
    assert summary.added == 0
    assert ops(changes).count("delete") == 1


def test_replace_block_with_more_new_sentences_emits_inserts() -> None:
    changes = diff_sentences(lines("甲", "乙"), lines("甲", "乙改", "丙新"))
    summary = summarize_changes(changes)
    assert summary.unchanged == 1
    assert (summary.changed, summary.added, summary.removed) == (1, 1, 0)


def test_empty_old_version_is_all_inserts() -> None:
    changes = diff_sentences([], lines("甲", "乙"))
    assert ops(changes) == ["insert", "insert"]
    assert summarize_changes(changes).added == 2


def test_empty_new_version_is_all_deletes() -> None:
    changes = diff_sentences(lines("甲", "乙"), [])
    assert ops(changes) == ["delete", "delete"]
    assert summarize_changes(changes).removed == 2


def test_both_empty_is_empty() -> None:
    changes = diff_sentences([], [])
    assert changes == []
    assert summarize_changes(changes).total == 0


def test_seq_values_are_taken_from_the_rows_not_the_position() -> None:
    """``seq`` 不连续（跳过被删的句子）时，报出的仍是**行上的** seq。"""
    old = [Line(seq=1, text="甲"), Line(seq=4, text="乙")]
    new = [Line(seq=1, text="甲"), Line(seq=9, text="乙改")]
    changes = diff_sentences(old, new)
    assert [(item.old_seq, item.new_seq) for item in changes] == [(1, 1), (4, 9)]


def test_summary_counts_every_op_once() -> None:
    changes = diff_sentences(lines("甲", "乙", "丙"), lines("甲", "乙改", "新", "丙"))
    summary = summarize_changes(changes)
    assert summary.unchanged + summary.changed + summary.added + summary.removed == len(changes)
    assert summary.to_dict()["total"] == summary.total
