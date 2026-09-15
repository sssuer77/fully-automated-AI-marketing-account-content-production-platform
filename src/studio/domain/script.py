"""写稿领域契约（T1.10 · §04.1.4 / §04.1.5）—— Director 大纲 + Writer 成稿。

本模块**只有纯函数与 pydantic 契约**：不碰库、不碰网络、不碰提示词。
"LLM 说了什么"由 ``agents/director.py`` / ``agents/writer.py`` 负责，
"什么算合格"在这里 —— 于是规则能被单测穷举，而不是埋在 Agent 的分支里。

三道闸门的顺序（**顺序本身就是设计**）
------------------------------------
```
① 结构闸（pydantic + JSON Schema）  段数 / 单段字数 / 时长区间 / 字段名
② 规则闸（本模块 check_*）          字数区间 / 口癖命中 / 禁区 / 单人占比
③ 切分闸（enforce_sentence_limit）  超长句**强制切分**（不重写，省 token）
```
③ 排在②后面：先切分再算字数与占比，校验看到的才是**真正要送去合成的东西**。

为什么 LLM 契约里的 ``text`` 放到 200 字
----------------------------------------
§04.1.5 写的是 ``max_length=28``。但那是**生效文本**的上限；若把它直接钉在
LLM 输出契约上，"模型写了个长句"会变成一次 JSON Schema 修复重试（烧 token，
且可能反复失败），而规格书要求的是"**切分，不重写**"。所以契约层放宽到 200，
28 由 :func:`enforce_sentence_limit` 在服务端强制 —— 落库的
``script_sentences.text`` 必然 ≤28（T1.10 裁定 78）。

为什么时长由字数**算**出来
--------------------------
``WriterOutput.est_duration_ms`` 是模型自报值，只代表"它自己觉得多长"。
落库的 ``scripts.est_duration_ms`` 一律由 :func:`estimate_duration_ms` 从实际字数
重算（≈5 字/秒）—— 与 ADR-005「音频为时长真相」同一取向：**能算的不要信模型**。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.core.config import PersonaConfig
from studio.domain.text import compact_ws, count_chars, split_long_sentence
from studio.domain.topics import TopicSpec

__all__ = [
    "CATCHPHRASE_MIN_HITS",
    "CHARS_PER_SECOND",
    "DURATION_DEFAULT_MS",
    "DURATION_MAX_MS",
    "DURATION_MIN_MS",
    "FORBIDDEN_PREFIX",
    "PAUSE_DEFAULT_MS",
    "REWRITE_LIMIT",
    "SEGMENT_CHARS_MAX",
    "SEGMENT_CHARS_MIN",
    "SEGMENT_MAX",
    "SEGMENT_MIN",
    "SENTENCE_INPUT_MAX_CHARS",
    "SENTENCE_MAX_CHARS",
    "SENTENCE_MIN_COUNT",
    "SPEAKERS",
    "SPEAKER_RATIO_MAX",
    "WORD_COUNT_MAX",
    "WORD_COUNT_MIN",
    "WORD_COUNT_TARGET",
    "DirectorInput",
    "DirectorOutput",
    "OutlineReport",
    "ScriptDraft",
    "ScriptReport",
    "ScriptRules",
    "ScriptSegment",
    "SentenceSpec",
    "Speaker",
    "WriterInput",
    "WriterOutput",
    "build_draft",
    "catchphrase_hits",
    "check_outline",
    "check_script",
    "enforce_sentence_limit",
    "estimate_duration_ms",
    "find_forbidden",
    "rewrite_hint",
    "speaker_ratio",
]

# ── 阈值（默认值 = 规格书原文；persona 可覆盖，见 ScriptRules）────────────
WORD_COUNT_MIN: Final[int] = 600
WORD_COUNT_MAX: Final[int] = 800
WORD_COUNT_TARGET: Final[int] = 700
SEGMENT_MIN: Final[int] = 3
SEGMENT_MAX: Final[int] = 5
SEGMENT_CHARS_MIN: Final[int] = 80
SEGMENT_CHARS_MAX: Final[int] = 350
DURATION_MIN_MS: Final[int] = 60_000
DURATION_MAX_MS: Final[int] = 180_000
DURATION_DEFAULT_MS: Final[int] = 180_000
SENTENCE_MAX_CHARS: Final[int] = 28
SENTENCE_INPUT_MAX_CHARS: Final[int] = 200
SENTENCE_MIN_COUNT: Final[int] = 15
SPEAKER_RATIO_MAX: Final[float] = 0.7
CATCHPHRASE_MIN_HITS: Final[int] = 2
REWRITE_LIMIT: Final[int] = 2
CHARS_PER_SECOND: Final[float] = 5.0
PAUSE_DEFAULT_MS: Final[int] = 200

#: 禁区命中在 ``problems`` 里的前缀（服务层据此判 block，不靠字符串猜）
FORBIDDEN_PREFIX: Final[str] = "forbidden:"

Speaker = Literal["bigbear", "littlebear", "narrator"]
SPEAKERS: Final[tuple[str, ...]] = ("bigbear", "littlebear", "narrator")


class ScriptRules(BaseModel):
    """一次校验真正生效的阈值（**persona 可编辑 ⇒ 阈值不能写死在函数里**）。"""

    model_config = ConfigDict(extra="forbid")

    word_count_min: int = Field(default=WORD_COUNT_MIN, ge=100, le=5000)
    word_count_max: int = Field(default=WORD_COUNT_MAX, ge=100, le=8000)
    duration_max_ms: int = Field(default=DURATION_MAX_MS, ge=10_000, le=1_800_000)
    sentence_max_chars: int = Field(default=SENTENCE_MAX_CHARS, ge=8, le=120)
    catchphrase_min_hits: int = Field(default=CATCHPHRASE_MIN_HITS, ge=0, le=10)
    speaker_ratio_max: float = Field(default=SPEAKER_RATIO_MAX, ge=0.1, le=1.0)

    @classmethod
    def from_persona(cls, persona: PersonaConfig) -> ScriptRules:
        """从人物库取生效阈值（``config/personas/<id>.yaml`` 一改就跟着变）。"""
        return cls(
            word_count_min=persona.target_chars_min,
            word_count_max=persona.target_chars_max,
            duration_max_ms=persona.max_duration_ms,
        )


# ══════════════════════════════════════════════════════════════════════
# Director（§04.1.4）
# ══════════════════════════════════════════════════════════════════════


class ScriptSegment(BaseModel):
    """大纲里的一段（要点 / 画面建议 / 情绪 / 预估字数）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    point: str = Field(max_length=120)
    visual: str = Field(max_length=200)
    mood: str = Field(max_length=40)
    est_chars: int = Field(ge=SEGMENT_CHARS_MIN, le=SEGMENT_CHARS_MAX)


class DirectorInput(BaseModel):
    """Director 的输入（§04.1.4 未定义输入模型，此处按"选题 + 目标时长"补齐）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    target_duration_ms: int = Field(default=DURATION_DEFAULT_MS, ge=DURATION_MIN_MS, le=DURATION_MAX_MS)
    angle: str | None = Field(default=None, max_length=120)


class DirectorOutput(BaseModel):
    """大纲：黄金 3 秒钩子 + 3–5 段 + CTA + 时长预估。"""

    model_config = ConfigDict(extra="forbid")

    hook_3s: str = Field(max_length=80)
    segments: list[ScriptSegment] = Field(min_length=SEGMENT_MIN, max_length=SEGMENT_MAX)
    cta: str = Field(max_length=80)
    est_duration_ms: int = Field(ge=DURATION_MIN_MS, le=DURATION_MAX_MS)


class OutlineReport(BaseModel):
    """大纲校验结果（空 ``problems`` = 合格）。"""

    model_config = ConfigDict(extra="forbid")

    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        return "；".join(self.problems)


# ══════════════════════════════════════════════════════════════════════
# Writer（§04.1.5）
# ══════════════════════════════════════════════════════════════════════


class SentenceSpec(BaseModel):
    """一句口播（**按句配音**的最小单位，也是断点续传的粒度）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=SENTENCE_INPUT_MAX_CHARS)
    speaker: Speaker
    emotion: str = Field(default="neutral", max_length=40)
    pause_after_ms: int = Field(default=PAUSE_DEFAULT_MS, ge=0, le=2000)


class WriterInput(BaseModel):
    """Writer 的输入（``revision_notes`` 非空 ⇒ 本轮是改稿，T1.11 用）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    outline: DirectorOutput
    revision_notes: list[str] = Field(default_factory=list)


class WriterOutput(BaseModel):
    """成稿（``body_md`` 的字数区间**不**在 schema 里卡 —— 见模块 docstring）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=60)
    hook: str = Field(max_length=80)
    body_md: str = Field(min_length=1)
    cta: str = Field(max_length=80)
    sentences: list[SentenceSpec] = Field(min_length=SENTENCE_MIN_COUNT)
    est_duration_ms: int = Field(ge=DURATION_MIN_MS, le=DURATION_MAX_MS)
    catchphrases_used: list[str] = Field(default_factory=list)


class ScriptDraft(BaseModel):
    """落库前的最终产物（**已过强制切分**，句长 ≤ ``sentence_max_chars``）。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    hook: str
    body_md: str
    cta: str
    sentences: list[SentenceSpec]
    est_duration_ms: int
    word_count: int = Field(ge=0)
    speaker_ratio: dict[str, float] = Field(default_factory=dict)
    catchphrases_used: list[str] = Field(default_factory=list)
    outline: DirectorOutput
    warnings: list[str] = Field(default_factory=list)


class ScriptReport(BaseModel):
    """成稿校验结果。"""

    model_config = ConfigDict(extra="forbid")

    word_count: int = 0
    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    forbidden_hits: list[str] = Field(default_factory=list)
    catchphrase_hits: list[str] = Field(default_factory=list)
    split_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def blocked(self) -> bool:
        """禁区命中 ⇒ **直接 block**（不进评分、不重写 —— §04.1.5）。"""
        return bool(self.forbidden_hits)

    def describe(self) -> str:
        return "；".join(self.problems)


# ══════════════════════════════════════════════════════════════════════
# 纯函数
# ══════════════════════════════════════════════════════════════════════


def check_outline(outline: DirectorOutput) -> OutlineReport:
    """大纲规则闸（结构约束已由 pydantic / JSON Schema 保证，这里只查语义）。"""
    problems: list[str] = []
    seqs = [segment.seq for segment in outline.segments]
    if seqs != list(range(1, len(seqs) + 1)):
        problems.append(f"段落 seq 必须从 1 连续递增（当前 {seqs}）")
    total = sum(segment.est_chars for segment in outline.segments)
    if not WORD_COUNT_MIN <= total <= WORD_COUNT_MAX:
        problems.append(f"段落字数合计 {total} 不在 {WORD_COUNT_MIN}–{WORD_COUNT_MAX}")
    if not outline.hook_3s.strip():
        problems.append("hook_3s 不得为空")
    if not outline.cta.strip():
        problems.append("cta 不得为空")
    return OutlineReport(problems=problems)


def catchphrase_hits(text: str, catchphrases: Sequence[str]) -> list[str]:
    """口癖命中（**服务端复核**模型的自报值；忽略空白差异）。"""
    compact = _compact(text)
    hits: list[str] = []
    for phrase in catchphrases:
        key = _compact(phrase)
        if key and key in compact and phrase not in hits:
            hits.append(phrase)
    return hits


def find_forbidden(text: str, terms: Sequence[str]) -> list[str]:
    """禁区词扫描（同样忽略空白差异）。"""
    compact = _compact(text)
    hits: list[str] = []
    for term in terms:
        key = _compact(term)
        if key and key in compact and term not in hits:
            hits.append(term)
    return hits


def speaker_ratio(sentences: Sequence[SentenceSpec]) -> dict[str, float]:
    """各说话人的占比（**按口播字数加权**，不是按句数 —— 10 个短句抵不过 1 段长独白）。"""
    weights: dict[str, int] = {}
    for sentence in sentences:
        weights[sentence.speaker] = weights.get(sentence.speaker, 0) + max(1, count_chars(sentence.text))
    total = sum(weights.values())
    if total == 0:
        return {}
    return {speaker: round(value / total, 4) for speaker, value in weights.items()}


def enforce_sentence_limit(
    sentences: Sequence[SentenceSpec],
    *,
    limit: int = SENTENCE_MAX_CHARS,
) -> tuple[list[SentenceSpec], int]:
    """超长句**强制切分**并重排 ``seq``；返回 ``(新句表, 切分次数)``。

    切分产生的碎片之间 ``pause_after_ms=0``（同一句话的中间不该有句间停顿），
    只有最后一个碎片保留原停顿。
    """
    out: list[SentenceSpec] = []
    splits = 0
    for sentence in sentences:
        pieces = split_long_sentence(sentence.text, limit=limit)
        if not pieces:
            continue
        splits += max(0, len(pieces) - 1)
        for index, piece in enumerate(pieces):
            is_last = index == len(pieces) - 1
            out.append(
                sentence.model_copy(
                    update={
                        "seq": len(out) + 1,
                        "text": piece,
                        "pause_after_ms": sentence.pause_after_ms if is_last else 0,
                    }
                )
            )
    return out, splits


def estimate_duration_ms(word_count: int, *, chars_per_second: float = CHARS_PER_SECOND) -> int:
    """由口播字数估时长（≈5 字/秒 ⇒ 700 字 ≈ 140 秒）。"""
    if word_count <= 0:
        return 0
    return round(word_count / chars_per_second * 1000)


def check_script(
    *,
    body_md: str,
    sentences: Sequence[SentenceSpec],
    catchphrases: Sequence[str],
    forbidden: Sequence[str],
    rules: ScriptRules | None = None,
) -> ScriptReport:
    """成稿规则闸：禁区 / 字数 / 口癖 / 单人占比。"""
    active = rules or ScriptRules()
    word_count = count_chars(body_md)
    problems: list[str] = []
    warnings: list[str] = []

    hits = find_forbidden(body_md, forbidden)
    problems.extend(f"{FORBIDDEN_PREFIX}{term}" for term in hits)

    if not active.word_count_min <= word_count <= active.word_count_max:
        problems.append(f"字数 {word_count} 不在 {active.word_count_min}–{active.word_count_max}")

    used = catchphrase_hits(body_md, catchphrases)
    if len(used) < active.catchphrase_min_hits:
        problems.append(
            f"口癖命中 {len(used)} 个，少于 {active.catchphrase_min_hits} 个（{list(catchphrases)}）"
        )

    ratio = speaker_ratio(sentences)
    if len(ratio) == 1:
        warnings.append(f"single_speaker:{next(iter(ratio))}")
    elif ratio:
        dominant, share = max(ratio.items(), key=lambda item: item[1])
        if share > active.speaker_ratio_max:
            warnings.append(f"speaker_ratio:{dominant}={share:.2f}")

    return ScriptReport(
        word_count=word_count,
        problems=problems,
        warnings=warnings,
        forbidden_hits=hits,
        catchphrase_hits=used,
    )


def build_draft(
    *,
    outline: DirectorOutput,
    output: WriterOutput,
    catchphrases: Sequence[str],
    forbidden: Sequence[str],
    rules: ScriptRules | None = None,
) -> tuple[ScriptDraft, ScriptReport]:
    """把 Writer 的原始输出收敛成**可落库**的 :class:`ScriptDraft` + 校验报告。

    顺序：切分 ⇒ 校验 ⇒ 重算时长/占比。``est_duration_ms`` 一律用**算出来的**值
    （模型自报值只留在 ``WriterOutput`` 里，不落库）。
    """
    active = rules or ScriptRules()
    sentences, splits = enforce_sentence_limit(output.sentences, limit=active.sentence_max_chars)
    report = check_script(
        body_md=output.body_md,
        sentences=sentences,
        catchphrases=catchphrases,
        forbidden=forbidden,
        rules=active,
    )
    report.split_count = splits
    draft = ScriptDraft(
        title=output.title,
        hook=output.hook,
        body_md=output.body_md,
        cta=output.cta,
        sentences=sentences,
        est_duration_ms=estimate_duration_ms(report.word_count),
        word_count=report.word_count,
        speaker_ratio=speaker_ratio(sentences),
        catchphrases_used=report.catchphrase_hits,
        outline=outline,
        warnings=report.warnings,
    )
    return draft, report


def rewrite_hint(report: ScriptReport) -> str:
    """把"哪里不合格"回灌给模型（重写提示；空 = 不用重写）。"""
    if not report.problems:
        return ""
    return "上一版不合格：" + "；".join(report.problems) + "。请重写完整 JSON，不要解释。"


def _compact(text: str) -> str:
    """去掉所有空白（口径在 :func:`studio.domain.text.compact_ws`，此处只是别名）。"""
    return compact_ws(text)
