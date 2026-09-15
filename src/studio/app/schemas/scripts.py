"""稿件面板的响应契约（T4.4 · §04.4.5 第 3 行）。

为什么把"双通道明细"原样透出
----------------------------
``review_scores`` 里 ``rule_detail`` 与 ``llm_detail`` 是两个**独立**通道：
前者是服务端可复算的硬规则，后者是 LLM 的六维度打分。面板只显示总分，
"这稿子为什么是 B 级"就永远说不清 —— 而确认闸要求人在**几十秒内**决定
放行还是退回，说不清理由的分数等于没有分数（原文 §2.2⑦）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel

from studio.db.models import ApprovalRow, ReviewRow, ScriptRow, SentenceRow

__all__ = [
    "ApprovalItemRef",
    "DiffSummaryModel",
    "ScriptBody",
    "ScriptDetail",
    "ScriptDiff",
    "ScriptReview",
    "ScriptSentence",
    "ScriptVersion",
    "ScriptVersionList",
    "SentenceDiffRow",
]


class ScriptSentence(BaseModel):
    """一句待配音文本（``script_sentences`` 的展示字段）。"""

    seq: int
    text: str
    speaker: str
    emotion: str = "neutral"
    pause_after_ms: int = 200


class ScriptReview(BaseModel):
    """一轮审稿结论（一次 ``review_scores`` 行）。"""

    round_no: int
    rule_total: float
    rule_detail: dict[str, Any]
    llm_total: float
    llm_detail: dict[str, Any]
    total: float
    grade: str
    decision: str
    issues: list[Any]
    llm_model: str | None = None
    created_at: str | None = None


class ScriptBody(BaseModel):
    """一版稿子的正文（不含逐句表 —— 那个单独给，面板要分段渲染）。"""

    id: str
    version: int
    is_active: bool
    title: str | None = None
    hook: str | None = None
    body_md: str
    cta: str | None = None
    word_count: int = 0
    est_duration_ms: int | None = None
    target_chars: int = 700
    grade: str | None = None
    score_total: float | None = None
    score_rule: float | None = None
    score_llm: float | None = None
    revision_round: int = 0
    editor_notes: list[Any]
    outline: dict[str, Any]
    speaker_ratio: dict[str, Any]
    llm_model: str | None = None
    prompt_version: str | None = None
    created_at: str | None = None


class ApprovalItemRef(BaseModel):
    """待审记录的**只读**引用（完整契约在 `app/schemas/approvals.py`）。

    这里不直接 import 那个模块：`schemas` 之间横向依赖会让"改一处、炸两处"，
    而面板需要的只是"当前这条待审的 id / 分数 / 轮次"。
    """

    id: str
    task_id: str
    script_id: str | None = None
    status: str
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    requested_at: str | None = None


class ScriptDetail(BaseModel):
    """稿件面板的整页数据（一次请求拿全，避免面板"拼"出不一致的视图）。

    :param reviews: **全部**轮次（按轮次正序）。面板要能并排显示
        "第 1 轮 6.3 分 / 第 2 轮 7.1 分"，否则"改稿有没有用"永远说不清。
    :param approval: 当前待审记录；不在闸里 ⇒ ``None``（面板据此决定是否显示决断条）。
    """

    task_id: str
    task_status: str
    revision_round: int
    script: ScriptBody
    sentences: list[ScriptSentence]
    reviews: list[ScriptReview]
    approval: ApprovalItemRef | None = None


class ScriptVersion(BaseModel):
    """版本列表的一项（"v1 → v2"下拉用）。"""

    version: int
    is_active: bool
    word_count: int = 0
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    sentence_count: int = 0
    created_at: str | None = None


class ScriptVersionList(BaseModel):
    task_id: str
    versions: list[ScriptVersion]


class SentenceDiffRow(BaseModel):
    """两版之间的一句对照。"""

    op: str
    old_seq: int | None = None
    new_seq: int | None = None
    old_text: str | None = None
    new_text: str | None = None


class DiffSummaryModel(BaseModel):
    """改动计数（面板顶部的"改 3 句 / 新增 1 句 / 删 2 句"）。"""

    unchanged: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    total: int = 0


class ScriptDiff(BaseModel):
    """两个版本之间的逐句对照。"""

    task_id: str
    from_version: int
    to_version: int
    changes: list[SentenceDiffRow]
    summary: DiffSummaryModel


def sentence_view(row: SentenceRow) -> ScriptSentence:
    """``SentenceRow`` → 展示模型（**唯一**转换点）。"""
    return ScriptSentence(
        seq=row.seq,
        text=row.text,
        speaker=row.speaker,
        emotion=row.emotion,
        pause_after_ms=row.pause_after_ms,
    )


def review_view(row: ReviewRow) -> ScriptReview:
    """``ReviewRow`` → 展示模型（双通道明细原样透出）。"""
    return ScriptReview(
        round_no=row.round_no,
        rule_total=row.rule_total,
        rule_detail=dict(row.rule_detail),
        llm_total=row.llm_total,
        llm_detail=dict(row.llm_detail),
        total=row.total,
        grade=row.grade,
        decision=row.decision,
        issues=list(row.issues),
        llm_model=row.llm_model,
        created_at=row.created_at,
    )


def script_view(row: ScriptRow) -> ScriptBody:
    """``ScriptRow`` → 正文展示模型。"""
    return ScriptBody(
        id=row.id,
        version=row.version,
        is_active=row.is_active,
        title=row.title,
        hook=row.hook,
        body_md=row.body_md,
        cta=row.cta,
        word_count=row.word_count,
        est_duration_ms=row.est_duration_ms,
        target_chars=row.target_chars,
        grade=row.grade,
        score_total=row.score_total,
        score_rule=row.score_rule,
        score_llm=row.score_llm,
        revision_round=row.revision_round,
        editor_notes=list(row.editor_notes),
        outline=dict(row.outline),
        speaker_ratio=dict(row.speaker_ratio),
        llm_model=row.llm_model,
        prompt_version=row.prompt_version,
        created_at=row.created_at,
    )


def approval_ref(row: ApprovalRow) -> ApprovalItemRef:
    """``ApprovalRow`` → 只读引用（``asdict`` 取字段，避免 schemas 横向 import）。"""
    return ApprovalItemRef(**asdict(row))
