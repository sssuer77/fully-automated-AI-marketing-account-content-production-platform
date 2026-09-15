"""单句切分（T2.5 · §04.3.6）—— 把一段口播切成"一次合成"的单位。

为什么必须切
------------
CosyVoice 的长文本会**漂移**（R7）：念到后面开始吞字、变调、加速。28 字的上限
不是审美偏好，是"再长就开始不可控"的实测边界。而且切分粒度直接决定**可续传粒度**
（§README：断电重启后从第一个未完成的句子继续）—— 句子越整齐，重跑的成本越低。

两条硬约束
----------
- 每片 ``≤ max_chars``：超过就回到"漂移"那一档；
- 每片 ``≥ min_chars``（默认 4）：一个"好。"单独成片，合出来的音频只有 0.3 秒，
  既白花一次引擎调用，又让时间轴多出一堆碎点。

不破坏语义边界
--------------
切点优先级：句末标点 → 句内停顿标点 → 拉丁词边界。**绝不**在数字或英文单词中间切
（``GPT4`` 不能变成 ``GP`` / ``T4``，``3.14`` 不能变成 ``3.`` / ``14``）。
这条**复用** :func:`studio.domain.text.split_long_sentence` —— 口径只此一处，
否则"写稿时的切分"和"配音时的切分"迟早对不上（写稿说合格、配音那边报超长）。
"""

from __future__ import annotations

from typing import Final

from studio.domain.script import SENTENCE_MAX_CHARS
from studio.domain.text import count_chars, split_long_sentence, split_sentences

__all__ = [
    "MIN_CHARS",
    "SUB_SEQ_MAX_PARTS",
    "seqs_for",
    "split_for_tts",
    "sub_seq",
]

#: 单句字数下限（§04.3.6："每片 ≤ max_chars 且 ≥ 4 字"）
MIN_CHARS: Final[int] = 4

#: ``seq`` 小数编号的上限：2, 2.1 … 2.9 —— 一位小数最多 9 片。
#: 再切下去说明 max_chars 定得太小，那是配置问题，不该悄悄编出 2.10（= 2.1，撞号）。
SUB_SEQ_MAX_PARTS: Final[int] = 9


def split_for_tts(
    text: str,
    *,
    max_chars: int = SENTENCE_MAX_CHARS,
    min_chars: int = MIN_CHARS,
) -> list[str]:
    """切成"一次合成"的片段（§04.3.6）。

    三步：① 按句末标点切句 → ② 超长句强制切分 → ③ 过短的片段与邻居合并。

    :param text: **已归一化**的文本（先 :func:`~studio.tts.text_normalize.normalize`）
    :param max_chars: 单片上限（默认 :data:`~studio.domain.script.SENTENCE_MAX_CHARS`）
    :param min_chars: 单片下限（默认 :data:`MIN_CHARS`）
    :raises ValueError: ``max_chars < min_chars`` 等自相矛盾的参数

    第三步为什么是"合并"而不是"丢掉"：短片段是**有内容**的（"好。"），
    丢掉就等于吞字；合并只是让它和邻居共用一次引擎调用。
    合并不下时（会超 ``max_chars``）宁可留一个短片段 —— 超长比超短贵得多。
    """
    if max_chars < min_chars:
        raise ValueError(f"max_chars({max_chars}) 不得小于 min_chars({min_chars})")
    if min_chars < 1:
        raise ValueError(f"min_chars({min_chars}) 至少为 1")

    stripped = text.strip()
    if not stripped:
        return []

    pieces: list[str] = []
    for sentence in split_sentences(stripped):
        pieces.extend(split_long_sentence(sentence, limit=max_chars))

    speakable = [piece for piece in pieces if count_chars(piece) > 0]
    return _merge_shorts(speakable, max_chars=max_chars, min_chars=min_chars)


def _merge_shorts(pieces: list[str], *, max_chars: int, min_chars: int) -> list[str]:
    """把过短的片段并进邻居（并不下就留着）。"""
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) < min_chars and len(merged[-1]) + len(piece) <= max_chars:
            merged[-1] += piece
        else:
            merged.append(piece)
    # 首片仍太短 ⇒ 并给后一片（"好。今天我们……" 不该让"好。"单独去合成一次）
    if len(merged) >= 2 and len(merged[0]) < min_chars and len(merged[0]) + len(merged[1]) <= max_chars:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def sub_seq(base: int, index: int) -> float:
    """1 句切成多行时的 ``seq`` 小数编号（§04.3.6：2, 2.1, 2.2 …）。

    :param base: 原句的整数序号
    :param index: 片内下标（0 = 首片，沿用整数号）
    :raises ValueError: ``index`` 超出 :data:`SUB_SEQ_MAX_PARTS`（会撞号）
    """
    if index == 0:
        return float(base)
    if not 0 < index < SUB_SEQ_MAX_PARTS:
        raise ValueError(
            f"片内下标 {index} 超出上限：一位小数最多 {SUB_SEQ_MAX_PARTS - 1} 片，"
            f"再多就会编出 {base}.10（= {base}.1，撞号）"
        )
    return float(base) + index / 10


def seqs_for(base: int, count: int) -> list[float]:
    """一次算好整句的 ``seq`` 列表（``count == 1`` ⇒ ``[base]``）。"""
    if count < 1:
        return []
    if count > SUB_SEQ_MAX_PARTS:
        raise ValueError(f"单片数 {count} 超出上限 {SUB_SEQ_MAX_PARTS}（改大 max_chars）")
    return [sub_seq(base, index) for index in range(count)]
