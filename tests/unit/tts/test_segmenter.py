"""单句切分（T2.5 · §04.3.6）—— 长度约束 + 语义边界 + seq 编号。

这份测试守的是**两条会被下游直接吃掉的约束**：
- 超过 ``max_chars`` ⇒ CosyVoice 长文本漂移（R7：吞字 / 变调 / 加速）；
- 低于 ``min_chars`` ⇒ 一次引擎调用换回 0.3 秒音频，还往时间轴里塞碎点。

"不破坏语义边界"在这里落成一条可断言的事实：**拉丁词不会被劈成两半**。
数字那一条由"输入必须已归一化"保证（归一化把 ASCII 数字全换成中文），
见 :func:`test_normalize_removes_ascii_digits_so_numbers_cannot_be_split`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.domain.script import SENTENCE_MAX_CHARS
from studio.domain.text import count_chars
from studio.tts.segmenter import MIN_CHARS, SUB_SEQ_MAX_PARTS, seqs_for, split_for_tts, sub_seq
from studio.tts.text_normalize import load_glossary, normalize

REPO_ROOT = Path(__file__).resolve().parents[3]
GLOSSARY = load_glossary(REPO_ROOT / "prompts" / "shared" / "glossary.yaml")

LONG = (
    "熊大你听我说，这个跑酷地图我玩了整整三天才通关，"
    "结果熊二上来两分钟就掉下去了，你说气人不气人？"
    "不过话说回来，这张图的设计确实有点东西，尤其是最后那个跳跃点，"
    "我建议大家先去练习模式熟悉一下节奏，别像熊二一样直接冲。"
)


# ══════════════════════════════════════════════════════════════════════
# 1. 长度约束
# ══════════════════════════════════════════════════════════════════════


def test_default_limit_is_the_script_limit() -> None:
    """默认上限与写稿口径**同源**：写稿说合格、配音这边不该报超长。"""
    assert SENTENCE_MAX_CHARS == 28
    pieces = split_for_tts(LONG)
    assert pieces
    assert max(len(piece) for piece in pieces) <= SENTENCE_MAX_CHARS


def test_every_piece_within_bounds() -> None:
    pieces = split_for_tts(LONG)
    assert all(MIN_CHARS <= len(piece) <= SENTENCE_MAX_CHARS for piece in pieces)


def test_custom_limits_are_honoured() -> None:
    pieces = split_for_tts(LONG, max_chars=12, min_chars=4)
    assert pieces
    assert max(len(piece) for piece in pieces) <= 12


def test_no_content_is_lost() -> None:
    """切片拼回去 = 原文去掉空白（一个字都不能多、不能少）。"""
    pieces = split_for_tts(LONG)
    assert "".join(pieces) == "".join(LONG.split())


def test_empty_and_blank_input() -> None:
    assert split_for_tts("") == []
    assert split_for_tts("   \n  ") == []


def test_rejects_contradictory_limits() -> None:
    with pytest.raises(ValueError):
        split_for_tts(LONG, max_chars=4, min_chars=8)
    with pytest.raises(ValueError):
        split_for_tts(LONG, min_chars=0)


# ══════════════════════════════════════════════════════════════════════
# 2. 语义边界
# ══════════════════════════════════════════════════════════════════════


def test_never_splits_latin_words() -> None:
    """``CosyVoice`` 被劈成 ``Cosy`` / ``Voice`` 是两个错词的读音。

    这条靠"**整词必须落在同一片里**"来断言：真的被劈开时，
    没有任何一片会含完整的词。
    """
    token = "CosyVoice"
    text = "熊大" * 12 + token + "熊二" * 12 + "。"
    pieces = split_for_tts(text)
    assert len(pieces) > 1, "构造的文本必须真的会触发切分，否则这条断言是空转"
    holders = [piece for piece in pieces if token in piece]
    assert len(holders) == 1


def test_splits_on_sentence_end_before_inner_pause() -> None:
    """句末标点优先于句内停顿：先按 ``。`` 断，再考虑 ``，``。"""
    pieces = split_for_tts("今天先讲跑酷。明天讲生存。")
    assert pieces == ["今天先讲跑酷。", "明天讲生存。"]


def test_merges_short_piece_into_neighbour() -> None:
    """ "好。"只有 2 字 ⇒ 并进后一句，不单独占一次引擎调用。"""
    pieces = split_for_tts("好。今天我们聊聊跑酷地图。")
    assert pieces == ["好。今天我们聊聊跑酷地图。"]
    assert count_chars(pieces[0]) > MIN_CHARS


def test_keeps_short_piece_when_merging_would_overflow() -> None:
    """并进去会超上限 ⇒ 宁可留一个短片段（超长比超短贵得多）。"""
    pieces = split_for_tts("好。" + "熊" * 27, max_chars=28, min_chars=4)
    assert pieces[0] == "好。"
    assert all(len(piece) <= 28 for piece in pieces)


def test_drops_punctuation_only_pieces() -> None:
    """纯标点片段没有可念的内容 ⇒ 丢掉（合成它只会得到一段静音）。"""
    assert split_for_tts("。。。") == []
    assert "。。。" not in split_for_tts("熊大来了。。。熊二也来了。")


def test_normalize_removes_ascii_digits_so_numbers_cannot_be_split() -> None:
    """数字不会被切一半 —— 因为**归一化之后根本没有 ASCII 数字**。

    切分器拿到的必须是 :func:`~studio.tts.text_normalize.normalize` 的输出
    （这是它的输入契约）：``3.14`` 早就变成 ``三点一四``，而 ``_hard_split``
    保护的是拉丁词边界，不会去认 ``.``。
    """
    raw = "圆周率是3.14，记住它。"
    out = normalize(raw, glossary=GLOSSARY)
    assert not any(char.isdigit() for char in out)
    assert "三点一四" in out


# ══════════════════════════════════════════════════════════════════════
# 3. seq 小数编号（§04.3.6：1 句 → 多行时用 2, 2.1, 2.2）
# ══════════════════════════════════════════════════════════════════════


def test_sub_seq_numbering() -> None:
    assert sub_seq(2, 0) == 2.0
    assert sub_seq(2, 1) == 2.1
    assert sub_seq(2, 2) == 2.2


def test_seqs_for_single_piece_keeps_integer() -> None:
    assert seqs_for(7, 1) == [7.0]


def test_seqs_for_multiple_pieces() -> None:
    assert seqs_for(3, 3) == [3.0, 3.1, 3.2]


def test_seqs_for_rejects_overflow() -> None:
    """一位小数最多 9 片；再多就会编出 ``2.10``（= ``2.1``，撞号）。"""
    assert len(seqs_for(2, SUB_SEQ_MAX_PARTS)) == SUB_SEQ_MAX_PARTS
    with pytest.raises(ValueError):
        seqs_for(2, SUB_SEQ_MAX_PARTS + 1)
    with pytest.raises(ValueError):
        sub_seq(2, SUB_SEQ_MAX_PARTS)


def test_seqs_for_empty() -> None:
    assert seqs_for(1, 0) == []


# ══════════════════════════════════════════════════════════════════════
# 4. 端到端：归一化 + 切分（T2.6 的真实调用顺序）
# ══════════════════════════════════════════════════════════════════════


def test_normalize_then_split_end_to_end() -> None:
    raw = (
        "**熊大你听我说**，2026年我玩了3-5个跑酷地图🔥，"
        "其中50%都是MC的，用FFmpeg压了5GB素材。"
        "银行行长说重要的事情要说三遍，因为主角总是最后一个知道。"
    )
    out = normalize(raw, glossary=GLOSSARY)
    pieces = split_for_tts(out)
    assert len(pieces) > 1
    assert all(MIN_CHARS <= len(piece) <= SENTENCE_MAX_CHARS for piece in pieces)
    assert "".join(pieces) == out
    assert "**" not in out and "🔥" not in out
    assert all("**" not in piece for piece in pieces)
