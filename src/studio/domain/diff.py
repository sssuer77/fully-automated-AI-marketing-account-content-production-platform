"""稿件版本对比（T4.4 · §04.4.5 稿件面板的"v1 → v2 diff"）。

为什么不是"逐 ``seq`` 对齐"
---------------------------
改稿不是"第 N 句改几个字"：Editor 会合并短句、拆开长句、整段删掉。按 ``seq``
硬对齐的话，只要中间插了一句，**后面全部**会显示成"改了"—— 而真实改动可能只有
一处。所以走 :mod:`difflib` 的块级匹配：它先找最长公共子序列，再把剩下的区段
判成 equal / replace / insert / delete，插入导致的整体偏移因此不会污染判定。

块内为什么按位置配对
--------------------
``replace`` 块内"哪句对哪句"没有客观答案（两侧都被改过）。按位置配对是
**可预测**的：用户看到的是"第 3 句 → 第 3 句"，而不是"算法觉得这两句最像"。
多余的一侧如实标成 ``insert`` / ``delete``，不硬凑成替换。

纯函数
------
不读库、不碰文件系统（读库在服务层）。这样"两版稿子差在哪"可以脱离数据库单测，
也才能被 WebUI 与 CLI 共用同一份口径。
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

__all__ = [
    "CHANGE_OPS",
    "DiffSummary",
    "SentenceChange",
    "SentenceLike",
    "diff_sentences",
    "summarize_changes",
]

#: 一次改动块的四种形态（与 :mod:`difflib` 的 opcode 同名，只多一个 ``replace``）
ChangeOp = Literal["equal", "replace", "insert", "delete"]

CHANGE_OPS: Final[tuple[ChangeOp, ...]] = ("equal", "replace", "insert", "delete")


class SentenceLike(Protocol):
    """diff 只需要「序号 + 文本」（``script_sentences`` 行与领域模型都满足）。

    两个成员都声明成**只读属性**：``ScriptRow`` / ``SentenceRow`` 是 frozen
    dataclass，字段只读。声明成可变属性（``seq: int``）会让协议要求实现者可写，
    于是 frozen 的行**结构上不匹配** —— 而它显然满足「有 seq 和 text」。
    """

    @property
    def seq(self) -> int: ...

    @property
    def text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SentenceChange:
    """一句的对照结果（``insert`` / ``delete`` 的另一侧为 ``None``）。"""

    op: ChangeOp
    old_seq: int | None
    new_seq: int | None
    old_text: str | None
    new_text: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "op": self.op,
            "old_seq": self.old_seq,
            "new_seq": self.new_seq,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """改动计数（面板顶部的"改 3 句 / 新增 1 句 / 删 2 句"）。"""

    unchanged: int
    changed: int
    added: int
    removed: int

    @property
    def total(self) -> int:
        """有变化的句子总数（``unchanged`` 不计）。"""
        return self.changed + self.added + self.removed

    def to_dict(self) -> dict[str, int]:
        return {
            "unchanged": self.unchanged,
            "changed": self.changed,
            "added": self.added,
            "removed": self.removed,
            "total": self.total,
        }


def diff_sentences(
    old: Sequence[SentenceLike],
    new: Sequence[SentenceLike],
) -> list[SentenceChange]:
    """两版逐句表 → 对照序列（按新稿顺序排列，删除句挂在它原来的位置）。

    ``old`` / ``new`` 都假定已按 ``seq`` 升序（仓储的读取顺序）。
    """
    old_items = list(old)
    new_items = list(new)
    matcher = difflib.SequenceMatcher(
        None, [item.text for item in old_items], [item.text for item in new_items], autojunk=False
    )
    changes: list[SentenceChange] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            changes.extend(
                SentenceChange(
                    op="equal",
                    old_seq=old_items[i1 + offset].seq,
                    new_seq=new_items[j1 + offset].seq,
                    old_text=old_items[i1 + offset].text,
                    new_text=new_items[j1 + offset].text,
                )
                for offset in range(i2 - i1)
            )
            continue
        if tag == "delete":
            changes.extend(
                SentenceChange(op="delete", old_seq=row.seq, new_seq=None, old_text=row.text, new_text=None)
                for row in old_items[i1:i2]
            )
            continue
        if tag == "insert":
            changes.extend(
                SentenceChange(op="insert", old_seq=None, new_seq=row.seq, old_text=None, new_text=row.text)
                for row in new_items[j1:j2]
            )
            continue
        changes.extend(_pair_replace(old_items[i1:i2], new_items[j1:j2]))
    return changes


def summarize_changes(changes: Sequence[SentenceChange]) -> DiffSummary:
    """数一遍四种改动（``total`` 是派生属性，不参与计数）。"""
    counts = dict.fromkeys(CHANGE_OPS, 0)
    for change in changes:
        counts[change.op] += 1
    return DiffSummary(
        unchanged=counts["equal"],
        changed=counts["replace"],
        added=counts["insert"],
        removed=counts["delete"],
    )


def _pair_replace(
    old_block: Sequence[SentenceLike],
    new_block: Sequence[SentenceLike],
) -> list[SentenceChange]:
    """``replace`` 块内按位置配对，多余的一侧降级成 ``delete`` / ``insert``。"""
    paired = min(len(old_block), len(new_block))
    changes = [
        SentenceChange(
            op="replace",
            old_seq=old_block[offset].seq,
            new_seq=new_block[offset].seq,
            old_text=old_block[offset].text,
            new_text=new_block[offset].text,
        )
        for offset in range(paired)
    ]
    changes.extend(
        SentenceChange(op="delete", old_seq=row.seq, new_seq=None, old_text=row.text, new_text=None)
        for row in old_block[paired:]
    )
    changes.extend(
        SentenceChange(op="insert", old_seq=None, new_seq=row.seq, old_text=None, new_text=row.text)
        for row in new_block[paired:]
    )
    return changes
