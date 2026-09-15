"""口播文本工具（T1.10 · ``domain/text.py``）。

"字数"与"句长"是两个口径 —— 这里把两条都钉死，免得日后有人"顺手统一"。
"""

from __future__ import annotations

import pytest

from studio.domain.text import count_chars, split_long_sentence, split_sentences


class TestCountChars:
    def test_empty_is_zero(self) -> None:
        assert count_chars("") == 0

    def test_cjk_counts_one_each(self) -> None:
        assert count_chars("熊大熊二") == 4

    def test_ascii_run_counts_one(self) -> None:
        assert count_chars("hello world") == 2

    def test_punctuation_and_space_are_free(self) -> None:
        assert count_chars("熊大，熊二！ ") == 4

    def test_mixed_line(self) -> None:
        # AI(1) + 跑(1) + 酷(1) + 2026(1)
        assert count_chars("AI跑酷 2026！") == 4

    def test_emoji_is_free(self) -> None:
        assert count_chars("跑酷🎉") == 2

    def test_full_body_sample(self) -> None:
        assert count_chars("这不科学！" * 10) == 40


class TestSplitSentences:
    def test_splits_on_hard_endings(self) -> None:
        assert split_sentences("熊大说走吧。然后呢？真的！") == ["熊大说走吧。", "然后呢？", "真的！"]

    def test_keeps_closing_quote_with_previous_sentence(self) -> None:
        assert split_sentences("他说“走吧。”然后走了。") == ["他说“走吧。”", "然后走了。"]

    def test_newline_also_breaks(self) -> None:
        assert split_sentences("第一行\n第二行") == ["第一行", "第二行"]

    def test_blank_input_yields_nothing(self) -> None:
        assert split_sentences("\n\n  \n") == []

    def test_trailing_fragment_without_punctuation_is_kept(self) -> None:
        assert split_sentences("没写完的一句话") == ["没写完的一句话"]

    def test_soft_break_does_not_split(self) -> None:
        assert split_sentences("熊大说，别急。") == ["熊大说，别急。"]


class TestSplitLongSentence:
    def test_short_sentence_is_returned_as_is(self) -> None:
        assert split_long_sentence("很短的一句", limit=28) == ["很短的一句"]

    def test_blank_returns_empty(self) -> None:
        assert split_long_sentence("   ", limit=28) == []

    def test_every_piece_within_limit(self) -> None:
        text = "熊大说这不科学，" * 8
        pieces = split_long_sentence(text, limit=28)
        assert len(pieces) > 1
        assert all(len(piece) <= 28 for piece in pieces)

    def test_prefers_soft_breaks(self) -> None:
        pieces = split_long_sentence("熊大说这不科学，熊二说俺寻思也是，观众都笑了。" * 2, limit=28)
        # 每一片都该以标点收尾（在停顿标点处断开，而不是硬切）
        assert all(piece.endswith(("，", "。")) for piece in pieces[:-1])

    def test_hard_split_does_not_break_latin_words(self) -> None:
        text = "supercalifragilisticexpialidocious" * 2
        pieces = split_long_sentence(text, limit=28)
        assert all(len(piece) <= 28 for piece in pieces)
        assert "".join(pieces) == text
        assert all(piece.isascii() and piece.isalpha() for piece in pieces)

    def test_concatenation_is_lossless(self) -> None:
        text = "熊大熊二在MC里跑酷，" * 6
        assert "".join(split_long_sentence(text, limit=28)) == text

    @pytest.mark.parametrize("limit", [8, 12, 28, 40])
    def test_limit_is_respected_for_any_bound(self, limit: int) -> None:
        pieces = split_long_sentence("一句话" * 30, limit=limit)
        assert all(len(piece) <= limit for piece in pieces)
