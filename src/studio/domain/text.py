"""口播文本工具（T1.10 · §02.1）：字数 / 分句 / 超长句切分。

"字数"只有一个口径
------------------
§2.2④ 的 "600–800 字" 是**口播字数**，不是 ``len(text)``：

- 标点、空白、emoji 不占口播时长 —— 把它们算进去，会让"看着够长"的稿子
  念出来只有 40 秒；
- 一串拉丁字母 / 数字（``AI`` / ``2026`` / ``GPT4``）念出来是一个词，按 1 字计。

所以 :func:`count_chars` = **CJK 逐字计 1 + 连续 ASCII 字母数字计 1**，其余不计。

⚠️ 与 :data:`studio.domain.script.SENTENCE_MAX_CHARS` 的区别：句长上限（28）走的是
``len(text)`` —— 它与 §04.1.5 的 ``max_length=28``、与字幕断行口径是**同一个数**。
两个口径各有各的用途（``count_chars`` 算总量、``len`` 卡单句），**不要混用**。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "CLOSERS",
    "HARD_ENDINGS",
    "SOFT_BREAKS",
    "compact_ws",
    "count_chars",
    "split_long_sentence",
    "split_sentences",
]

#: 句末标点（切"句"用：一个句子 = 一次 TTS 合成的单位）
HARD_ENDINGS: Final[str] = "。！？!?…；;"

#: 句末收尾符号：跟在句末标点之后仍属同一句（``他说"走吧。"``）
CLOSERS: Final[str] = "”’」』）)》】》"

#: 句内停顿标点（超长句**优先**在这些位置断开，保住语义边界）
SOFT_BREAKS: Final[str] = "，、,：:；"

_CJK: Final[re.Pattern[str]] = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_WORD: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z]+")


def count_chars(text: str) -> int:
    """口播字数：CJK 逐字计 1，连续 ASCII 字母数字计 1，标点/空白不计。"""
    return len(_CJK.findall(text)) + len(_ASCII_WORD.findall(text))


def compact_ws(text: str) -> str:
    """去掉所有空白（``别 急`` 与 ``别急`` 视为同一串）。

    口癖/禁区/段落重复度的判定都走它：模型偶尔多打一个空格，不该被算成
    "没用到口癖"或"这两段不重复"（T1.10 裁定：口径只此一处）。
    """
    return "".join(text.split())


def split_sentences(text: str) -> list[str]:
    """按句末标点切句（**保留**标点与句末引号；换行也断句）。

    切出来的是"一次合成"的单位：喂给 TTS 的句子越整齐，停顿与字幕断行就越好控。
    """
    sentences: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in HARD_ENDINGS:
            buffer.append(char)
            while index + 1 < len(text) and text[index + 1] in CLOSERS:
                index += 1
                buffer.append(text[index])
            sentences.append("".join(buffer).strip())
            buffer = []
        elif char == "\n":
            sentences.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    sentences.append("".join(buffer).strip())
    return [item for item in sentences if item]


def split_long_sentence(text: str, *, limit: int) -> list[str]:
    """超长句**强制切分**（§04.1.5：切分比重写省 token）。

    三步：① 在句内停顿标点处切成小句；② 小句贪心打包到 ≤ ``limit``；
    ③ 单个小句仍超长 ⇒ 硬切，且**不切断拉丁词**（``GPT4`` 不会被劈成 ``GP`` / ``T4``）。
    """
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= limit:
        return [stripped]

    pieces: list[str] = []
    buffer = ""
    for clause in _clauses(stripped):
        for piece in _hard_split(clause, limit):
            if not buffer:
                buffer = piece
            elif len(buffer) + len(piece) <= limit:
                buffer += piece
            else:
                pieces.append(buffer)
                buffer = piece
    if buffer:
        pieces.append(buffer)
    return pieces


def _clauses(text: str) -> list[str]:
    """按句内停顿标点切小句（标点跟着**前**一小句走）。"""
    clauses: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in SOFT_BREAKS:
            clauses.append(buffer)
            buffer = ""
    if buffer:
        clauses.append(buffer)
    return clauses


def _hard_split(clause: str, limit: int) -> list[str]:
    """硬切一个仍然超长的小句（尽量落在拉丁词边界上）。"""
    if len(clause) <= limit:
        return [clause]
    chunks: list[str] = []
    rest = clause
    while len(rest) > limit:
        cut = limit
        if _is_word_char(rest[cut - 1]) and _is_word_char(rest[cut]):
            boundary = cut
            while boundary > limit // 2 and _is_word_char(rest[boundary - 1]):
                boundary -= 1
            if boundary > limit // 2:
                cut = boundary
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _is_word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()
