"""选题池领域契约与算法（T1.9 · §04.1.2 / §04.1.3 / §04.1.7）。

本模块是**纯数据 + 纯算法**：不碰数据库、不碰网络、不碰文件系统（§02.1
``domain/`` 零外部依赖）。所有 I/O 都在 ``db/repositories/`` 与 ``services/``。

为什么去重算法放 domain 而不是服务层
------------------------------------
R15（选题同质化）是**产品级不变式**，不是某次调用的实现细节：
"归一化哈希 + 相似度 ≥0.85 降分"必须能被单测逐条钉死，也必须能在 WebUI 的
"为什么这条被降分"里被解释。纯函数 ⇒ 可 golden 快照、可复算。

与 §04.1.7 / §04.1.2 的三处**增补**（只增不改，同 T1.8 裁定 56 的做法）
---------------------------------------------------------------------
1. ``HotItemSpec.heat_raw``：DDL 的 ``hot_items.heat`` 是 TEXT（原样保留 ``'爆' | '高'``），
   而 §04.1.7 的 ``heat`` 是"可排序的数值视图"（``float | None``）。两者都要 ⇒ 各留一个字段。
2. ``DirectionSpec.fit_score``：§04.1.2 的规则表要求"LLM 自评契合度 < 6 则丢弃"，
   但契约里没有承载它的字段 ⇒ 增补（默认 7 = 放行，手写方向不受影响）。
3. ``PlannerOutput.rule_report``：四条规则的机器化校验结果（由 Agent 回填，
   **不参与 JSON Schema 校验**，LLM 也看不到它）。

情感口径：§04.1.7 用 ``pos/neu/neg`` 简写，DDL 的 CHECK 用 ``positive/neutral/negative``
（§03.3.3）。**落库一律走 DDL 口径**（:data:`SENTIMENT_TO_DB`），
简写只活在领域模型与提示词里 —— 否则运行期才被 CHECK 拒绝（T1.9 裁定 66）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.core.ids import sha256_hex

__all__ = [
    "COMPLAINT_DEMOTION",
    "DB_TO_SENTIMENT",
    "DEDUP_SIMILARITY_PENALTY",
    "DEDUP_SIMILARITY_THRESHOLD",
    "DIRECTION_COUNT_MAX",
    "DIRECTION_COUNT_MIN",
    "MIN_FIT_SCORE",
    "RISK_COMPLAINED_TOPIC",
    "RISK_LOW_GROUNDING",
    "SENTIMENT_TO_DB",
    "SYNONYMS",
    "ClassifiedItem",
    "DedupAction",
    "DedupMatch",
    "DedupResult",
    "DirectionSpec",
    "ExistingTopic",
    "FeedbackBatch",
    "FeedbackBatchItem",
    "FeedbackClassification",
    "FeedbackDigest",
    "FeedbackItemSpec",
    "FeedbackKind",
    "GroundingRef",
    "GroundingType",
    "HookType",
    "HotItemSpec",
    "IdeatorInput",
    "IdeatorOutput",
    "PlannerInput",
    "PlannerOutput",
    "RuleName",
    "RuleReport",
    "RuleViolation",
    "Sentiment",
    "TopicSpec",
    "build_feedback_digest",
    "dedup_topic",
    "hash_title",
    "normalize_title",
    "plan_directions",
    "sentiment_to_kind",
    "similarity",
    "top_phrases",
]

# ══════════════════════════════════════════════════════════════════════
# 取值域（DDL CHECK 的 Python 镜像）
# ══════════════════════════════════════════════════════════════════════

Sentiment = Literal["pos", "neu", "neg", "unknown"]
FeedbackKind = Literal["want", "complaint", "trend", "other"]
HookType = Literal["conflict", "suspense", "contrast", "number", "other"]
GroundingType = Literal["persona", "hot", "feedback"]
RuleName = Literal["positioning", "want", "hot"]

#: 简写 → ``feedback_items.sentiment`` 的 CHECK 取值（**落库口径**，裁定 66）
SENTIMENT_TO_DB: Final[Mapping[str, str]] = {
    "pos": "positive",
    "neu": "neutral",
    "neg": "negative",
    "unknown": "unknown",
}

#: 反向映射（读库 → 领域模型）
DB_TO_SENTIMENT: Final[Mapping[str, str]] = {
    "positive": "pos",
    "neutral": "neu",
    "negative": "neg",
    "unknown": "unknown",
}

#: ``risk_flags`` 里的保留标记（§04.1.2 的规则表逐字引用）
RISK_LOW_GROUNDING: Final[str] = "low_grounding"
RISK_COMPLAINED_TOPIC: Final[str] = "complained_topic"

#: "被吐槽"方向的降权幅度（§04.1.2：``priority += 500``）
COMPLAINT_DEMOTION: Final[int] = 500

#: LLM 自评契合度低于此值 ⇒ 丢弃该方向（§04.1.2 规则表）
MIN_FIT_SCORE: Final[int] = 6

#: 二级去重阈值与降分幅度（§04.1.3）
DEDUP_SIMILARITY_THRESHOLD: Final[float] = 0.85
DEDUP_SIMILARITY_PENALTY: Final[float] = 2.0

#: ``similar_to_json`` 最多留几条命中（够解释就行，不必留全量）
DEDUP_MATCH_LIMIT: Final[int] = 3

#: 方向条数下限（§04.1.2：5–8）
DIRECTION_COUNT_MIN: Final[int] = 5

#: 方向条数上限（§04.1.2：5–8）—— 服务层用它填 ``PlannerInput.count_max``
DIRECTION_COUNT_MAX: Final[int] = 8

# ══════════════════════════════════════════════════════════════════════
# 标题归一化（R15 一级去重的地基）
# ══════════════════════════════════════════════════════════════════════

#: 归一化时保留的字符：CJK 汉字 + 拉丁字母 + 数字
_KEEP = re.compile(r"[0-9a-z\u4e00-\u9fff]+")
_DIGITS = re.compile(r"\d+")

#: 同义替换表（**长词优先**，见 :func:`normalize_title`）。
#: 只收"领域内不区分选题"的词与已知别名；不追求语言学完备 ——
#: 归一化是为了**去重**，宁可略微激进，也不要让同一选题换个说法就混进池子。
SYNONYMS: Final[Mapping[str, str]] = {
    "我的世界": "mc",
    "麦块": "mc",
    "minecraft": "mc",
    "跑酷": "parkour",
    "解说": "",
    "视频": "",
    "教程": "",
}

_SYNONYM_ORDER: Final[tuple[str, ...]] = tuple(
    sorted(SYNONYMS, key=len, reverse=True),
)


def normalize_title(text: str) -> str:
    """标题归一化：NFKC（全角→半角）→ 大小写折叠 → 只留 CJK/字母/数字 → 同义替换。

    ``'我的世界跑酷：5 个必看技巧！'`` 与 ``'MC 跑酷 5 个必看技巧'``
    归一化后都是 ``'mcparkour5个必看技巧'``。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    compact = "".join(_KEEP.findall(folded))
    for word in _SYNONYM_ORDER:
        if word in compact:
            compact = compact.replace(word, SYNONYMS[word])
    return compact


def hash_title(text: str) -> str:
    """一级去重键：归一化后**再把数字折叠成 ``#``**，然后取 sha256。

    为什么数字也要折叠：``'MC跑酷5个技巧'`` 与 ``'MC跑酷7个技巧'`` 是同一个选题模板，
    同一天出两条只会互相分流。数字差异留给二级去重的相似度去表达。
    """
    return sha256_hex(_DIGITS.sub("#", normalize_title(text)))


def similarity(left: str, right: str) -> float:
    """字符级相似度（§04.1.3：``difflib.SequenceMatcher``），保留数字。

    ``autojunk=False`` 是必须的：默认的"垃圾字符"启发式对**中文长串**会误判
    （把高频汉字当噪声剔除），让两条明显不同的标题算出 1.0。
    """
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def top_phrases(texts: Sequence[str], *, top_n: int = 10, min_count: int = 2) -> list[tuple[str, int]]:
    """词频 Top-N（**CJK 二元组**）。

    不引入分词依赖（§1.6.2 没有 jieba）：二元组对"给 Planner 一点提示"这个用途
    足够，且**确定可复算**。停用二元组直接剔除，避免"的话/就是/感觉"霸榜。
    """
    counter: Counter[str] = Counter()
    for text in texts:
        compact = normalize_title(text)
        for index in range(len(compact) - 1):
            gram = compact[index : index + 2]
            if gram in _STOP_GRAMS:
                continue
            counter[gram] += 1
    ranked = [(gram, count) for gram, count in counter.items() if count >= min_count]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:top_n]


#: 停用二元组（高频但无信息量的搭配）
_STOP_GRAMS: Final[frozenset[str]] = frozenset(
    {
        "的话",
        "一个",
        "就是",
        "可以",
        "这个",
        "那个",
        "什么",
        "怎么",
        "还是",
        "因为",
        "所以",
        "但是",
        "如果",
        "已经",
        "没有",
        "不是",
        "我们",
        "你们",
        "他们",
        "自己",
        "现在",
        "时候",
        "感觉",
        "有点",
        "真的",
        "特别",
        "非常",
        "应该",
        "希望",
        "想要",
        "看看",
        "一下",
        "这些",
        "那些",
        "多了",
        "了了",
    }
)

# ══════════════════════════════════════════════════════════════════════
# 输入源契约（§04.1.7）
# ══════════════════════════════════════════════════════════════════════


class HotItemSpec(BaseModel):
    """一行热点（``data/hot/*.md`` 的 ``标题|热度|平台``）。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    heat: float | None = None
    platform: str | None = None
    raw_line: str
    line_no: int = Field(ge=1)
    #: ★增补：热度原文（``'爆' | '高'`` 这类非数值热度不丢）
    heat_raw: str | None = None
    #: 解析是否成功（``False`` ⇒ 入库留痕但不参与选题，T1.9 裁定 67）
    parse_ok: bool = True
    #: 解析失败原因（``parse_ok=False`` 时非空）
    parse_error: str | None = None


class FeedbackItemSpec(BaseModel):
    """一行历史反馈（``data/feedback/*.md``，双形态兼容 · Q4）。"""

    model_config = ConfigDict(extra="forbid")

    platform: str | None = None
    date: str | None = None
    sentiment: Sentiment = "unknown"
    kind: FeedbackKind = "other"
    text: str
    is_auto: bool = False
    raw_line: str
    line_no: int = Field(ge=1)
    #: ★增补：解析警告（"看起来像结构化头但坏了"这类，不中断整批）
    warnings: list[str] = Field(default_factory=list)


class FeedbackDigest(BaseModel):
    """聚合后的反馈摘要（Planner 的输入之一）。"""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    wants: list[FeedbackItemSpec] = Field(default_factory=list)
    complaints: list[FeedbackItemSpec] = Field(default_factory=list)
    trends: list[FeedbackItemSpec] = Field(default_factory=list)
    top_topics: list[tuple[str, int]] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def build_feedback_digest(items: Sequence[FeedbackItemSpec], *, top_n: int = 10) -> FeedbackDigest:
    """按 ``kind`` 分桶 + 词频 Top-N。空输入 ⇒ 空 digest（不是 ``None``）。"""
    wants = [item for item in items if item.kind == "want"]
    complaints = [item for item in items if item.kind == "complaint"]
    trends = [item for item in items if item.kind == "trend"]
    texts = [item.text for item in (*wants, *complaints)]
    return FeedbackDigest(
        total=len(items),
        wants=wants,
        complaints=complaints,
        trends=trends,
        top_topics=top_phrases(texts, top_n=top_n),
    )


# ══════════════════════════════════════════════════════════════════════
# Planner 契约（§04.1.2）
# ══════════════════════════════════════════════════════════════════════


class GroundingRef(BaseModel):
    """一条"依据"（热点 / 反馈 / 定位）。WebUI 展示"为什么是这个方向"。"""

    model_config = ConfigDict(extra="forbid")

    type: GroundingType
    ref_id: str | None = None
    kind: str | None = None  # feedback 用：'want' | 'complaint' | 'trend'
    quote: str | None = None


class DirectionSpec(BaseModel):
    """一个内容方向（5–8 个一批）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=40)
    rationale: str = Field(max_length=200)
    grounded_on: list[GroundingRef] = Field(default_factory=list)
    priority: int = Field(100, ge=0, le=999)
    risk_flags: list[str] = Field(default_factory=list)
    #: ★增补：LLM 自评契合度（0–10）；< 6 ⇒ 丢弃（§04.1.2 规则表）
    fit_score: int = Field(default=7, ge=0, le=10)


class PlannerInput(BaseModel):
    """Planner 的输入（**不直接喂给 LLM**：由 Agent 渲染成提示词）。"""

    model_config = ConfigDict(extra="forbid")

    hot_items: list[HotItemSpec] = Field(default_factory=list)
    feedback_digest: FeedbackDigest | None = None
    count_min: int = DIRECTION_COUNT_MIN
    count_max: int = 8


class RuleViolation(BaseModel):
    """一条规则没被满足。"""

    model_config = ConfigDict(extra="forbid")

    rule: RuleName
    detail: str


class RuleReport(BaseModel):
    """§04.1.2 规则表的机器化校验结果（落 ``system_logs`` 的 payload）。"""

    model_config = ConfigDict(extra="forbid")

    violations: list[RuleViolation] = Field(default_factory=list)
    #: 契合度不足被丢弃的方向标题
    dropped: list[str] = Field(default_factory=list)
    #: 被吐槽 ⇒ ``priority += 500`` 的方向标题
    demoted: list[str] = Field(default_factory=list)
    #: 无热点且无反馈（仅凭 persona 产出）
    low_grounding: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations

    def describe(self) -> str:
        """给"重试提示"用的一句话（回灌给 LLM）。"""
        if self.ok:
            return ""
        return "；".join(f"{item.rule}: {item.detail}" for item in self.violations)


class PlannerOutput(BaseModel):
    """Planner 产出：5–8 个方向。

    ⚠️ 模型这里只要求 ``min_length=1``，**5–8 由 ``schemas/planner_result.schema.json``
    保证**（那是与 LLM 的契约，网关会强校验）。放宽到 1 是为了让"过滤掉跑偏方向后
    不足 5 个"这条**降级路径可表达**：宁可把 4 个方向带着告警交给运营看，
    也不要因为凑不满 5 个就把整批结果丢掉（T1.9 裁定 68）。
    """

    model_config = ConfigDict(extra="forbid")

    directions: list[DirectionSpec] = Field(min_length=1, max_length=8)
    #: ★增补：规则校验结果（由 Agent 回填，**不参与 JSON Schema**）
    rule_report: RuleReport | None = None


def plan_directions(
    directions: Sequence[DirectionSpec],
    *,
    hot_available: bool,
    feedback_available: bool,
    min_fit: int = MIN_FIT_SCORE,
) -> tuple[list[DirectionSpec], RuleReport]:
    """把 LLM 的原始方向过一遍 §04.1.2 的四条规则。

    四条规则各自的处理方式**刻意不同**（这是原文的设计，别"统一"掉）：

    | 规则 | 处理 | 为什么 |
    | --- | --- | --- |
    | 定位跑偏（无 persona 依据 / 契合度 < 6） | **丢弃** | 跑偏的内容再多也没用 |
    | 优先"用户想要" | **不满足 ⇒ 违规**（触发重试） | 这是"应该做"，不是"不能做" |
    | 回避"被吐槽" | **降权 +500**（不丢弃） | 吐槽往往也意味着热度；降权即可 |
    | 紧贴热点 | **不满足 ⇒ 违规**（触发重试） | 同上 |

    ``hot_available`` / ``feedback_available`` 为 ``False`` 时，对应规则**不判违规**
    —— 没有热点却要求"必须含热点依据"是自相矛盾（那是 ``low_grounding`` 场景）。
    """
    kept: list[DirectionSpec] = []
    dropped: list[str] = []
    for direction in directions:
        if direction.fit_score >= min_fit:
            kept.append(direction)
        else:
            dropped.append(direction.title)

    demoted: list[str] = []
    adjusted: list[DirectionSpec] = []
    for direction in kept:
        if RISK_COMPLAINED_TOPIC in direction.risk_flags:
            demoted.append(direction.title)
            adjusted.append(
                direction.model_copy(update={"priority": min(999, direction.priority + COMPLAINT_DEMOTION)})
            )
        else:
            adjusted.append(direction)

    low_grounding = not hot_available and not feedback_available
    if low_grounding:
        adjusted = [
            direction.model_copy(update={"risk_flags": [*direction.risk_flags, RISK_LOW_GROUNDING]})
            for direction in adjusted
        ]

    violations: list[RuleViolation] = []
    missing_persona = [
        item.title for item in adjusted if not any(ref.type == "persona" for ref in item.grounded_on)
    ]
    if missing_persona:
        violations.append(
            RuleViolation(
                rule="positioning",
                detail=f"{len(missing_persona)} 个方向缺 persona 依据：{missing_persona[:3]}",
            )
        )
    if feedback_available and not any(
        ref.type == "feedback" and ref.kind == "want" for item in adjusted for ref in item.grounded_on
    ):
        violations.append(RuleViolation(rule="want", detail="没有任何方向采纳'用户想要'（feedback/want）"))
    if hot_available and not any(ref.type == "hot" for item in adjusted for ref in item.grounded_on):
        violations.append(RuleViolation(rule="hot", detail="没有任何方向引用热点（hot）"))
    if len(adjusted) < DIRECTION_COUNT_MIN:
        violations.append(
            RuleViolation(
                rule="positioning",
                detail=f"过滤后仅剩 {len(adjusted)} 个方向（下限 {DIRECTION_COUNT_MIN}）",
            )
        )

    report = RuleReport(
        violations=violations,
        dropped=dropped,
        demoted=demoted,
        low_grounding=low_grounding,
    )
    return adjusted, report


# ══════════════════════════════════════════════════════════════════════
# Ideator 契约（§04.1.3）
# ══════════════════════════════════════════════════════════════════════


class TopicSpec(BaseModel):
    """一个选题（每方向 4 个）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=50)
    hook_type: HookType
    angle: str = Field(max_length=120)
    exec_feasible: bool = True
    score: float = Field(ge=0, le=10)
    reason: str = Field(max_length=200)


class IdeatorInput(BaseModel):
    """Ideator 的输入（**不直接喂给 LLM**）。"""

    model_config = ConfigDict(extra="forbid")

    direction: DirectionSpec
    per_direction: int = Field(default=4, ge=1, le=10)
    existing_titles: list[str] = Field(default_factory=list)


class IdeatorOutput(BaseModel):
    """Ideator 产出：3–5 个选题（§04.1.3）。"""

    model_config = ConfigDict(extra="forbid")

    topics: list[TopicSpec] = Field(min_length=3, max_length=5)


# ══════════════════════════════════════════════════════════════════════
# 两级去重（R15 · §04.1.3）
# ══════════════════════════════════════════════════════════════════════


class DedupAction(StrEnum):
    """一级命中 ⇒ ``DROP``；二级命中 ⇒ ``DEMOTE``；否则 ``KEEP``。"""

    KEEP = "keep"
    DROP = "drop"
    DEMOTE = "demote"


class ExistingTopic(BaseModel):
    """去重比对的"库里已有的选题"（只要三列，别把整行搬进来）。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str
    dedup_hash: str | None = None


class DedupMatch(BaseModel):
    """一条相似命中（落 ``topic_candidates.similar_to_json``）。"""

    model_config = ConfigDict(extra="forbid")

    target_id: str | None = None
    target_title: str
    similarity: float = Field(ge=0.0, le=1.0)


class DedupResult(BaseModel):
    """一次去重判定（纯函数返回值，可 golden）。"""

    model_config = ConfigDict(extra="forbid")

    action: DedupAction
    dedup_hash: str
    similarity: float = 0.0
    score_delta: float = 0.0
    reason: str = ""
    similar_to: list[DedupMatch] = Field(default_factory=list)


def dedup_topic(
    title: str,
    existing: Iterable[ExistingTopic],
    *,
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,
    penalty: float = DEDUP_SIMILARITY_PENALTY,
) -> DedupResult:
    """两级去重（R15）：

    ① **归一化哈希完全命中** ⇒ ``DROP``：库里已经有同一条，再插一行只是噪声
       （审计信息本来就在库里 —— 按 ``dedup_hash`` 查得到）。
    ② **相似度 ≥ ``threshold``** ⇒ ``DEMOTE``：保留但降 ``penalty`` 分，
       并在 ``similar_to`` 里写清"跟谁像、像到什么程度"。
    ③ 都不命中 ⇒ ``KEEP``。

    相似度用**归一化后的字符级**比较（保留数字）——
    "5 个技巧"和"7 个技巧"在哈希层是同一模板，在相似度层仍然能分辨出"换了个数字"。
    """
    digest = hash_title(title)
    matches: list[DedupMatch] = []
    best = 0.0

    for item in existing:
        item_hash = item.dedup_hash or hash_title(item.title)
        if item_hash == digest:
            return DedupResult(
                action=DedupAction.DROP,
                dedup_hash=digest,
                similarity=1.0,
                reason=f"归一化哈希命中《{item.title}》",
                similar_to=[DedupMatch(target_id=item.id, target_title=item.title, similarity=1.0)],
            )
        ratio = similarity(title, item.title)
        if ratio >= threshold:
            best = max(best, ratio)
            matches.append(DedupMatch(target_id=item.id, target_title=item.title, similarity=round(ratio, 4)))

    if not matches:
        return DedupResult(action=DedupAction.KEEP, dedup_hash=digest, reason="无相似命中")

    matches.sort(key=lambda match: (-match.similarity, match.target_title))
    top = matches[:DEDUP_MATCH_LIMIT]
    return DedupResult(
        action=DedupAction.DEMOTE,
        dedup_hash=digest,
        similarity=round(best, 4),
        score_delta=-penalty,
        reason=f"与《{top[0].target_title}》相似度 {top[0].similarity:.2f} ≥ {threshold}",
        similar_to=top,
    )


# ══════════════════════════════════════════════════════════════════════
# 反馈分类契约（§04.1.7："情感 / 诉求分类由 Planner 阶段批量做一次"）
# ══════════════════════════════════════════════════════════════════════


class FeedbackBatchItem(BaseModel):
    """送去分类的一条反馈（``ref`` 是回抄锚点，用 ``feedback_items.id``）。"""

    model_config = ConfigDict(extra="forbid")

    ref: str
    text: str
    platform: str | None = None


class FeedbackBatch(BaseModel):
    """一次分类的输入（**不直接喂给 LLM**：由 Agent 渲染成提示词）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[FeedbackBatchItem] = Field(default_factory=list)


class ClassifiedItem(BaseModel):
    """一条分类结果（``ref`` 必须原样回抄，服务层据此回填）。"""

    model_config = ConfigDict(extra="forbid")

    ref: str
    sentiment: Sentiment = "unknown"
    wants: list[str] = Field(default_factory=list)
    complaints: list[str] = Field(default_factory=list)


class FeedbackClassification(BaseModel):
    """分类产出（条数与输入**不必相等**：模型可能漏条，服务层按 ``ref`` 对齐）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ClassifiedItem] = Field(default_factory=list)


def sentiment_to_kind(item: ClassifiedItem) -> FeedbackKind:
    """按"想要 / 吐槽"的抽取结果推 ``kind``（Planner 的 ``grounded_on.kind`` 用它）。

    优先级：有 ``wants`` ⇒ ``want``；有 ``complaints`` ⇒ ``complaint``；
    两者都有 ⇒ ``want``（**先满足用户想要的**，与 §04.1.2 规则 2 一致）；
    都没有 ⇒ ``trend``（纯情绪表达，可当趋势参考）。
    """
    if item.wants:
        return "want"
    if item.complaints:
        return "complaint"
    return "trend"
