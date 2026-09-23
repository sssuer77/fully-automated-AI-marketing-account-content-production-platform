"""双通道评分与分级（T1.11 · §04.1.6 / §04.4.4）。

本模块是**纯函数 + 纯契约**：不碰数据库、不碰网络、不碰时钟（§02.1 ``domain/``
零外部依赖）。评分公式的唯一实现就在这里，别处不许再写一份。

两个通道，两种可信度
--------------------
| 通道 | 谁算 | 权重 | 为什么 |
| --- | --- | --- | --- |
| 规则 | **服务端**（本模块） | 0.3 | 字数/禁区/开场/结尾都是**可复算的事实** |
| LLM | 模型 | 0.7 | 钩子抓不抓人、口不口语化、节奏对不对 —— 只能靠判断 |

把规则通道交给模型，等于把可验证的事实变成不可验证的；把定级交给模型，
等于让考生自己判卷。

**LLM 不参与定级**：模型只给六维度分数与问题清单，``total`` / ``grade`` / ``decision``
一律由 :func:`compute_score` / :func:`gate_action` 算出来（裁定 89）。让模型自己报总分
等于让考生自己判卷 —— 而且总分一旦由模型给，"0.3×规则+0.7×LLM"这条硬契约就没人守了。

``need_edit`` 的语义（裁定 90）
-------------------------------
DDL 把 ``review_scores.decision`` 的取值锁死为 5 个（``pass_auto`` / ``need_edit`` /
``discard`` / ``human_approved`` / ``human_rejected``），**没有**"等人工"这一项。
所以 ``need_edit`` 的含义是"**未自动放行**"：到底去改稿还是直接进确认闸，由
:func:`gate_action` 决定。``AutoApprovePolicy.OFF`` 下 A 级也走这条路（A 级不需要改稿，
直接进闸），因此 ``need_edit`` **不等于**"一定要改稿"。
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.domain.enums import AutoApprovePolicy, Grade
from studio.domain.script import (
    DirectorOutput,
    ScriptRules,
    WriterOutput,
    ending_asks_audience,
    find_forbidden,
)
from studio.domain.text import compact_ws, count_chars
from studio.domain.topics import TopicSpec

__all__ = [
    "DUP_PENALTY",
    "DUP_RATIO_MAX",
    "EDIT_RETRIES",
    "GRADE_A_MIN",
    "GRADE_B_MIN",
    "LLM_WEIGHT",
    "REVISION_LIMIT",
    "RULE_WEIGHT",
    "BannedHit",
    "ChannelItem",
    "Decision",
    "EditReport",
    "EditorInput",
    "EditorOutput",
    "GateAction",
    "LlmChannelDetail",
    "ReviewIssue",
    "ReviewOutput",
    "ReviewerInput",
    "ReviewerOutput",
    "RuleChannelDetail",
    "allowed_sentences",
    "auto_approved_by",
    "check_edit",
    "compute_score",
    "decision_for",
    "evaluate_rule_channel",
    "gate_action",
    "llm_total_of",
    "paragraph_dup_ratio",
    "rule_total_of",
    "verdict_for",
]

# ── 权重与阈值（§04.1.6 硬契约）─────────────────────────────────────────
RULE_WEIGHT: Final[float] = 0.3
LLM_WEIGHT: Final[float] = 0.7
GRADE_A_MIN: Final[float] = 8.0
GRADE_B_MIN: Final[float] = 5.0

#: 改稿轮次硬上限（原文 §2.2⑥"最多 2 轮"；R16：防无限循环烧 token）
REVISION_LIMIT: Final[int] = 2

#: 段落重复度上限：超过它按 :data:`DUP_PENALTY` 扣规则分
DUP_RATIO_MAX: Final[float] = 0.3
DUP_PENALTY: Final[float] = 2.0

#: Editor 越界改动后的纠正重试次数（轮次预算另算，见 :data:`REVISION_LIMIT`）
EDIT_RETRIES: Final[int] = 1

#: 开场钩子的字数上限（§04.1.6 "钩子存在且 ≤80 字"）
HOOK_MAX_CHARS: Final[int] = 80

#: 六维度字段名（顺序即 ``LlmChannelDetail`` 的字段顺序，UI 与提示词都按它排）
LLM_DIMENSIONS: Final[tuple[str, ...]] = (
    "hook_opening",
    "positioning_fit",
    "oral_style",
    "emotion_rhythm",
    "ending_cta",
    "forbidden",
)


class Decision(StrEnum):
    """``review_scores.decision``（DDL CHECK 的 Python 镜像）。"""

    PASS_AUTO = "pass_auto"
    NEED_EDIT = "need_edit"
    DISCARD = "discard"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"


class GateAction(StrEnum):
    """ "这一稿接下来去哪儿"（服务层据此迁移状态）。

    与 :class:`Decision` 分开：``Decision`` 是**落库的事实**，``GateAction`` 是
    **动作**。两者一对多（``NEED_EDIT`` 既可能是改稿也可能是进闸），混用一个枚举
    会让"改了几轮"这件事在库里看不出来。
    """

    AUTO_PASS = "auto_pass"  # ⇒ queued_voice
    EDIT = "edit"  # ⇒ editing（≤2 轮）
    HUMAN_GATE = "human_gate"  # ⇒ awaiting_approval
    DISCARD = "discard"  # ⇒ discarded


# ══════════════════════════════════════════════════════════════════════
# 契约
# ══════════════════════════════════════════════════════════════════════


class ChannelItem(BaseModel):
    """一个评分项：分数 + 人类可读的理由（WebUI 直接展示）。"""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=10)
    comment: str = Field(min_length=1, max_length=200)


class BannedHit(BaseModel):
    """禁区词命中（``count`` 是出现次数 —— "出现 1 次"与"刷了 8 次"不是一回事）。"""

    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=1)


class RuleChannelDetail(BaseModel):
    """通道一：规则明细（**服务端算出来的**，§04.1.6 原文四项 + 上下文）。"""

    model_config = ConfigDict(extra="forbid")

    length: ChannelItem
    banned_hits: list[BannedHit] = Field(default_factory=list)
    opening_ok: bool
    ending_ok: bool
    paragraph_dup_ratio: float = Field(ge=0, le=1)
    chars: int = Field(ge=0)
    est_duration_ms: int = Field(ge=0)
    catchphrases_hit: int = Field(ge=0)


class LlmChannelDetail(BaseModel):
    """通道二：LLM 六维度（§04.1.6 原文六项，各 0–10 分 + 评语）。"""

    model_config = ConfigDict(extra="forbid")

    hook_opening: ChannelItem  # 开场钩子
    positioning_fit: ChannelItem  # 定位契合
    oral_style: ChannelItem  # 口语化
    emotion_rhythm: ChannelItem  # 情绪节奏
    ending_cta: ChannelItem  # 结尾引导
    forbidden: ChannelItem  # 禁区

    def scores(self) -> list[float]:
        """六项分数（顺序同 :data:`LLM_DIMENSIONS`）。"""
        return [getattr(self, name).score for name in LLM_DIMENSIONS]


class ReviewIssue(BaseModel):
    """审稿指出的一个问题（★ 也是 Editor 的改动授权范围，§04.1.6）。

    ``target`` 是**机器可读**的落点（``title`` / ``hook`` / ``segment:3`` / ``cta`` / ``global``）：
    Editor 的"只改被指出的问题"要能机检，靠的就是它 —— 自由文本没法机检。
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=40)
    severity: Literal["block", "major", "minor"]
    target: str = Field(min_length=1, max_length=40)
    detail: str = Field(min_length=1, max_length=200)
    suggestion: str = Field(min_length=1, max_length=200)


class ReviewerInput(BaseModel):
    """审稿输入（稿件全文 + 大纲 + **服务端已算好的规则明细**）。

    规则明细由服务层算好再传进来，Agent 不再算第二遍：``chars`` / ``catchphrases_hit``
    / ``dup_ratio`` 这些是"事实"，模型打分时该看到它们（否则它会去猜字数），
    但**不该由它产出**（见模块 docstring）。

    ``round_no`` **只保下界、不设上界**（裁定 100）：人工在确认闸退回后
    ``revision_round`` 会继续加，审稿轮次自然超过 :data:`REVISION_LIMIT + 1`。
    轮次闸门由 :func:`gate_action` 把守（``> REVISION_LIMIT`` ⇒ 不再改稿、直接进闸），
    输入模型不该再当第二道闸 —— 那会把"人工退回后重审"这条正常路径直接炸掉。
    """

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    script: WriterOutput
    outline: DirectorOutput
    rule_detail: RuleChannelDetail
    round_no: int = Field(default=1, ge=1)


class ReviewerOutput(BaseModel):
    """★ LLM 的产出（``schemas/review_result.schema.json``）。

    **只有这两样**：六维度明细 + 问题清单。``rule_total`` / ``total`` / ``grade``
    / ``verdict`` 都不在这里 —— 它们是服务端算出来的（见模块 docstring）。
    """

    model_config = ConfigDict(extra="forbid")

    llm_detail: LlmChannelDetail
    issues: list[ReviewIssue] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    """一次审稿的完整结论（落 ``review_scores`` 的就是它）。"""

    model_config = ConfigDict(extra="forbid")

    rule_total: float = Field(ge=0, le=10)
    rule_detail: RuleChannelDetail
    llm_total: float = Field(ge=0, le=10)
    llm_detail: LlmChannelDetail
    issues: list[ReviewIssue] = Field(default_factory=list)
    total: float = Field(ge=0, le=10)
    grade: Grade
    decision: Decision
    round_no: int = Field(ge=1)

    @property
    def verdict(self) -> Literal["pass", "revise", "reject"]:
        """给提示词/UI 的三值口径（由等级派生，不接受模型自报）。"""
        return verdict_for(self.grade)


class EditorInput(BaseModel):
    """改稿输入（``issues`` 是**唯一**的改动授权范围）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    script: WriterOutput
    outline: DirectorOutput
    issues: list[ReviewIssue] = Field(default_factory=list)
    round_no: int = Field(default=1, ge=1, le=REVISION_LIMIT)


class EditorOutput(BaseModel):
    """改稿产出：改后的完整稿件 + 逐条对应的改动说明。"""

    model_config = ConfigDict(extra="forbid")

    script: WriterOutput
    changes: list[str] = Field(default_factory=list)


class EditReport(BaseModel):
    """改稿的越界检查结论（"只改被指出的问题"是**可机检**的）。"""

    model_config = ConfigDict(extra="forbid")

    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    changed_sentences: int = 0
    allowed_targets: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        return "；".join(self.problems)


# ══════════════════════════════════════════════════════════════════════
# 评分
# ══════════════════════════════════════════════════════════════════════


def compute_score(rule_total: float, llm_total: float) -> tuple[float, Grade]:
    """``total = round(0.3×rule + 0.7×llm, 2)`` ⇒ ``(total, grade)``。

    基线（§04.1.6 已实测）：``8.0/9.0 ⇒ 8.7 → A``、``7.0/6.0 ⇒ 6.3 → B``、
    ``4.0/4.5 ⇒ 4.35 → C``。
    """
    total = round(RULE_WEIGHT * rule_total + LLM_WEIGHT * llm_total, 2)
    if total >= GRADE_A_MIN:
        grade = Grade.A
    elif total >= GRADE_B_MIN:
        grade = Grade.B
    else:
        grade = Grade.C
    return total, grade


def llm_total_of(detail: LlmChannelDetail) -> float:
    """六维度取算术平均（原文没有给维度权重 ⇒ 不擅自加权）。"""
    scores = detail.scores()
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def paragraph_dup_ratio(body_md: str) -> float:
    """段落重复度 = 重复出现的段落数 / 总段落数（空稿 ⇒ 0）。

    先按行切段、去掉空白差异再比 —— ``"熊大又跑起来了。"`` 与 ``"熊大又跑起来了。 "``
    是同一个段落，按原文比会让"多打一个空格"变成不重复。
    """
    paragraphs = [compact_ws(line) for line in body_md.splitlines()]
    kept = [item for item in paragraphs if item]
    if not kept:
        return 0.0
    counts = Counter(kept)
    duplicated = sum(1 for text, count in counts.items() if count > 1)
    return round(duplicated / len(kept), 4)


def _length_score(chars: int, *, low: int, high: int) -> float:
    """字数得分：区间内 10 分，越界按"差了几倍区间宽"线性扣到 0。

    用区间宽度做分母（而不是拍一个固定扣分），是为了让阈值可编辑：persona 把
    600–800 改成 300–500 时，扣分曲线跟着缩放，不需要再调一个魔法数。
    """
    if low <= chars <= high:
        return 10.0
    span = high - low
    if span <= 0:
        return 0.0
    distance = low - chars if chars < low else chars - high
    return round(max(0.0, 10.0 - 10.0 * distance / span), 2)


def evaluate_rule_channel(
    *,
    body_md: str,
    hook: str,
    cta: str,
    est_duration_ms: int,
    catchphrases_hit: int,
    forbidden: list[str],
    rules: ScriptRules | None = None,
) -> RuleChannelDetail:
    """算通道一的明细（**四项**：长度 / 禁区 / 开场 / 结尾 + 重复度扣分）。

    ``catchphrases_hit`` 由调用方传入（它已经由 :func:`check_script` 算过一次）——
    这里再扫一遍等于同一件事有两个实现。
    """
    active = rules or ScriptRules()
    chars = count_chars(body_md)
    length = _length_score(chars, low=active.word_count_min, high=active.word_count_max)
    compact = compact_ws(body_md)
    hits = [
        BannedHit(term=term, count=max(1, compact.count(compact_ws(term))))
        for term in find_forbidden(body_md, forbidden)
    ]
    opening_ok = bool(hook.strip()) and count_chars(hook) <= HOOK_MAX_CHARS
    # 结尾的判据是**两件事**：写了，而且**没有向观众提问**（裁定 394 后半）。
    # 以前只看“非空”，于是“评论区打俩字：牛肉还是五仁？”这种结尾照样满分 ——
    # 而用户要的恰恰相反：结尾的职责是把观点钉死。
    ending_ok = bool(cta.strip()) and ending_asks_audience(cta) is None
    return RuleChannelDetail(
        length=ChannelItem(
            score=length,
            comment=f"{chars} 字（要求 {active.word_count_min}–{active.word_count_max}）",
        ),
        banned_hits=hits,
        opening_ok=opening_ok,
        ending_ok=ending_ok,
        paragraph_dup_ratio=paragraph_dup_ratio(body_md),
        chars=chars,
        est_duration_ms=est_duration_ms,
        catchphrases_hit=catchphrases_hit,
    )


def rule_total_of(detail: RuleChannelDetail) -> float:
    """四项等权平均，再按段落重复度扣分（§04.1.6 ">0.3 扣分"）。"""
    items = [
        detail.length.score,
        0.0 if detail.banned_hits else 10.0,
        10.0 if detail.opening_ok else 0.0,
        10.0 if detail.ending_ok else 0.0,
    ]
    base = sum(items) / len(items)
    penalty = DUP_PENALTY if detail.paragraph_dup_ratio > DUP_RATIO_MAX else 0.0
    return round(max(0.0, base - penalty), 2)


def verdict_for(grade: Grade) -> Literal["pass", "revise", "reject"]:
    """等级 → 三值裁决（A=pass / B=revise / C=reject）。"""
    if grade is Grade.A:
        return "pass"
    return "revise" if grade is Grade.B else "reject"


def decision_for(action: GateAction) -> Decision:
    """动作 → 落库的 ``decision``（见模块 docstring 对 ``need_edit`` 的说明）。"""
    if action is GateAction.AUTO_PASS:
        return Decision.PASS_AUTO
    if action is GateAction.DISCARD:
        return Decision.DISCARD
    return Decision.NEED_EDIT


def gate_action(
    grade: Grade,
    policy: AutoApprovePolicy,
    *,
    round_no: int,
) -> GateAction:
    """按等级 + 放行策略 + 已改轮次，决定下一步动作。

    | 等级 | ``off`` | ``grade_a``（默认） | ``grade_ab`` |
    | --- | --- | --- | --- |
    | A | 进闸 | **自动放行** | **自动放行** |
    | B | 改稿 ⇒ 进闸 | 改稿 ⇒ 进闸 | **自动放行** |
    | C | 废弃 | 废弃 | 废弃 |

    ``round_no`` 超过 :data:`REVISION_LIMIT` ⇒ B 级**不再改稿**，直接进闸：
    这是 R16（改稿烧 token）的硬闸门，与"模型这次改得好不好"无关。
    """
    if grade is Grade.C:
        return GateAction.DISCARD
    if grade is Grade.A:
        return GateAction.AUTO_PASS if policy is not AutoApprovePolicy.OFF else GateAction.HUMAN_GATE
    if policy is AutoApprovePolicy.GRADE_AB:
        return GateAction.AUTO_PASS
    return GateAction.EDIT if round_no <= REVISION_LIMIT else GateAction.HUMAN_GATE


def auto_approved_by(grade: Grade, policy: AutoApprovePolicy) -> str:
    """自动放行时写进 ``tasks.approved_by`` / ``approvals.decided_by`` 的署名。

    A 级署 ``auto_approve_A``（§04.4.4 不变量 2 明确要求），B 级只能是"全自动"
    模式下被放行的 ⇒ 署 ``auto_approve_AB``：复盘时要能一眼看出"这一稿是 A 还是
    被全自动模式捎带的 B"。
    """
    return "auto_approve_A" if grade is Grade.A else "auto_approve_AB"


# ══════════════════════════════════════════════════════════════════════
# 改稿越界检查
# ══════════════════════════════════════════════════════════════════════


def check_edit(
    original: WriterOutput,
    edited: WriterOutput,
    issues: list[ReviewIssue],
    *,
    segments: int = 1,
) -> EditReport:
    """ "只改被指出的问题"的机检（§04.1.6 约束 1）。

    判据是**句子级**的：``issues[].target`` 里的 ``segment:N`` 映射到第 N 段覆盖的
    句子区间，``title`` / ``hook`` / ``cta`` / ``global`` 各自对应标题、开场句、
    收尾句、全篇。没被指到的句子逐字变了 ⇒ ``edited:out_of_scope:第N句``。

    标题与开场、结尾同样受这一条约束（2026-09-23 补）：标题原先**完全没被检**，
    于是改稿 Agent 可以顺手换掉一个没被指到的标题，而面板上看不出发生过什么。

    为什么按句而不是 diff 行数：句长本来就 ≤28 字，diff 行数会被"多断一句"这种无关
    变化搅乱；按句比才能精确说清"第 7 句不该动"。
    """
    allowed = allowed_sentences(issues, total=len(original.sentences), segments=segments)
    problems: list[str] = []
    warnings: list[str] = []
    changed = 0

    if not issues:
        problems.append("edited:no_issues")
    if len(edited.sentences) != len(original.sentences):
        # 句数变了 ⇒ 逐句对不齐，只报总账（硬凑索引会给出错误的行号）
        warnings.append(f"edit:sentence_count:{len(original.sentences)}->{len(edited.sentences)}")
    for index, (before, after) in enumerate(zip(original.sentences, edited.sentences, strict=False), start=1):
        if before.text == after.text:
            continue
        changed += 1
        if index not in allowed:
            problems.append(f"edited:out_of_scope:第{index}句")
    if original.hook != edited.hook and not _covers(issues, "hook", segments, len(original.sentences)):
        problems.append("edited:out_of_scope:hook")
    if original.cta != edited.cta and not _covers(issues, "cta", segments, len(original.sentences)):
        problems.append("edited:out_of_scope:cta")
    if original.title != edited.title and not _covers(issues, "title", segments, len(original.sentences)):
        problems.append("edited:out_of_scope:title")

    return EditReport(
        problems=problems,
        warnings=warnings,
        changed_sentences=changed,
        allowed_targets=sorted({issue.target for issue in issues}),
    )


def allowed_sentences(issues: list[ReviewIssue], *, total: int, segments: int = 1) -> set[int]:
    """``issues[].target`` → 允许改动的句子序号（1-based）。

    段到句的映射用 ``ceil`` 均分：这是**服务端自己的近似**（真实段落边界要看
    ``body_md`` 的换行），刻意取"宽" —— 多放过一句，好过冤枉一次合规改稿。
    ``global`` 表示全篇授权。
    """
    if total <= 0:
        return set()
    if any(issue.target.strip() == "global" for issue in issues):
        return set(range(1, total + 1))
    allowed: set[int] = set()
    for issue in issues:
        target = issue.target.strip()
        if target == "hook":
            allowed.add(1)
        elif target == "cta":
            allowed.add(total)
        elif target.startswith("segment:"):
            index = _segment_index(target)
            if index is not None:
                allowed.update(_segment_range(index, total=total, segments=max(1, segments)))
    return allowed


def _covers(issues: list[ReviewIssue], target: str, segments: int, total: int) -> bool:
    """``issues`` 是否授权改 ``target``（``title`` / ``hook`` / ``cta``；``global`` 通吃）。"""
    if any(issue.target.strip() == "global" for issue in issues):
        return True
    return any(issue.target.strip() == target for issue in issues)


def _segment_index(target: str) -> int | None:
    _, _, raw = target.partition(":")
    return int(raw) if raw.isdigit() and int(raw) >= 1 else None


def _segment_range(index: int, *, total: int, segments: int) -> set[int]:
    """第 ``index`` 段覆盖的句子序号（``ceil`` 均分；超出段数 ⇒ 空集）。"""
    if index > segments:
        return set()
    per = -(-total // segments)  # ceil
    start = (index - 1) * per + 1
    if start > total:
        return set()
    return set(range(start, min(start + per - 1, total) + 1))
