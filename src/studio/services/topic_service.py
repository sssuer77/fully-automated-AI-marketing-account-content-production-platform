"""选题流水线（T1.9 · §04.1.2 / §04.1.3 / §04.1.7）—— 输入源 → 方向 → 选题。

一次 ``run_planner`` 的链路
--------------------------
```
data/hot/*.md ─┐
               ├─ InputService.import_*（解析 + 幂等入库；坏行照入库留痕）
data/feedback/*.md ─┘
        │
        ▼  FeedbackClassifierAgent（批量）── 失败 ⇒ 关键词规则兜底
   feedback_items 回填 sentiment / wants / complaints
        │
        ▼  build_feedback_digest（机器算词频，不额外调 LLM）
   PlannerAgent（5–8 方向 + 四条规则机器化校验）
        │
        ▼
   content_directions（batch_id 一次运行一个）
   + hot_items 标记消费（used_by_direction_id = 引用它的方向）
   + 已消费的热点文件归档 data/hot/archive/
```

一次 ``run_ideator`` 的链路
--------------------------
```
content_directions(batch_id) ── 逐方向（**串行、互不影响**）
        │
        ▼  IdeatorAgent（一个方向 → 3–5 个选题）
        │
        ▼  dedup_topic（① 归一化哈希命中 ⇒ 丢弃不入库
        │                ② 相似度 ≥ 0.85 ⇒ 降分 + 写 similar_to）
        ▼
   topic_candidates + 方向 status 回写（selected / dropped）
```

为什么"每方向独立"落在服务层
----------------------------
§04.1.3 要求"单个方向失败**不**影响其他方向"。做法是**每方向一个 try/except +
一次独立事务**：某一方向 LLM 超时 / 返回坏 JSON 时，其余方向照常产出，
失败的只记 :class:`DirectionOutcome` 与一条 ``warn`` 日志（DoD 5：不静默失败）。

**T1.9 裁定 71（改）**：``topic_batch`` 队列单元（§03.3.9，``unit_ref = batch_id``）
**推迟到 T1.12** 接入 draft 池 —— 此刻还没有能认领它的 worker，提前入队只会留下
永远 ``pending`` 的作业（比"没有作业"更难排查）。服务层的 ``run_planner`` /
``run_ideator`` 已经是**批次键控**的，届时 job handler 直接调用即可，属纯增量接线。

为什么 Agent 用 Protocol 而不是具体类
------------------------------------
``TopicService`` 是"把 I/O、Agent、仓储缝在一起"的那一层，测试要能在**没有网关、
没有提示词库、没有网络**的前提下跑通全链路（尤其是"某方向失败"这种分支）。
Protocol 让假件只需实现 ``run``；生产侧 ``PlannerAgent`` / ``IdeatorAgent``
形状一致，直接传即可。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

from studio.agents.base import AgentContext, AgentResult
from studio.core.config import PersonaConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, EventKind, Severity
from studio.db.models import DirectionRow, FeedbackItemRow, HotItemRow, TopicRow
from studio.db.repositories import AuditRepo, DirectionRepo, FeedbackItemRepo, HotItemRepo, TopicRepo
from studio.domain.topics import (
    DB_TO_SENTIMENT,
    DIRECTION_COUNT_MAX,
    DIRECTION_COUNT_MIN,
    SENTIMENT_TO_DB,
    ClassifiedItem,
    DedupAction,
    DedupResult,
    DirectionSpec,
    ExistingTopic,
    FeedbackBatch,
    FeedbackBatchItem,
    FeedbackClassification,
    FeedbackItemSpec,
    HookType,
    HotItemSpec,
    IdeatorInput,
    IdeatorOutput,
    PlannerInput,
    PlannerOutput,
    RuleReport,
    Sentiment,
    TopicSpec,
    build_feedback_digest,
    dedup_topic,
    sentiment_to_kind,
)
from studio.services.input_service import ImportReport, InputService
from studio.services.log_service import LogSink

__all__ = [
    "DEDUP_POOL_LIMIT",
    "DIGEST_TOP_N",
    "EXISTING_TITLE_LIMIT",
    "FEEDBACK_BATCH_SIZE",
    "FEEDBACK_LIMIT",
    "HOT_LIMIT",
    "MANUAL_BATCH_ID",
    "MANUAL_DIRECTION_TITLE",
    "TOPICS_PER_DIRECTION",
    "AnalyzeReport",
    "ClassifierLike",
    "DirectionDeleteOutcome",
    "DirectionEditOutcome",
    "DirectionOutcome",
    "IdeateReport",
    "IdeatorLike",
    "ManualDirectionOutcome",
    "ManualTopicOutcome",
    "PlannerLike",
    "TopicDeleteOutcome",
    "TopicEditOutcome",
    "TopicService",
    "classify_by_keywords",
    "db_sentiment",
    "direction_spec_from_row",
    "topic_spec_from_row",
]

#: ``hook_type`` 的合法取值（与 §04.1.3 的 ``HookType`` 逐字一致）
_HOOK_TYPES: Final[frozenset[str]] = frozenset({"conflict", "suspense", "contrast", "number", "other"})

#: 人工加选题挂靠的**固定批次**（T4.3 裁定 130）
#:
#: ``topic_candidates.direction_id`` 是 NOT NULL + 外键，人工加的选题也得有个方向。
#: 为它现造一个一次性批次会让"人工加的选题散落在 N 个批次里"，面板与统计都没法看；
#: 固定一个批次 ⇒ 一条 SQL 就能把"人加的都捞出来"，也让模型产出的方向保持纯净。
MANUAL_BATCH_ID: Final[str] = "manual"

#: 人工加选题的方向标题（面板展示用）
MANUAL_DIRECTION_TITLE: Final[str] = "人工加选题"

#: structlog 的保留键：``_emit`` 的兜底分支要把 payload 展开成 kwargs，这些键会撞车
_LOG_RESERVED: Final[frozenset[str]] = frozenset({"event", "level", "logger", "message", "timestamp"})

logger = get_logger("studio.topics")

#: 一次 Planner 读取的**未消费**热点上限
HOT_LIMIT: Final[int] = 200
#: 一次 Planner 读取的最近反馈条数上限
FEEDBACK_LIMIT: Final[int] = 200
#: 两级去重比对的"库里已有选题"条数上限
DEDUP_POOL_LIMIT: Final[int] = 500
#: 每次分类调用塞给模型的反馈条数（塞太多 ⇒ JSON 契约更容易崩，得不偿失）
FEEDBACK_BATCH_SIZE: Final[int] = 40
#: 喂给 Ideator 的"已有标题"条数上限（全量塞进去只会烧 token）
EXISTING_TITLE_LIMIT: Final[int] = 60
#: 每方向目标选题数（§04.1.3：5–8 方向 × 4 = 20–32 个）
TOPICS_PER_DIRECTION: Final[int] = 4
#: 反馈摘要保留的高频词条数
DIGEST_TOP_N: Final[int] = 10

#: 关键词兜底表（**只做粗判**：宁可漏判成 ``trend``，也不要把"想要"写成"吐槽"）
_WANT_MARKERS: Final[tuple[str, ...]] = (
    "想看",
    "求",
    "希望",
    "能不能",
    "可以出",
    "多来点",
    "多更",
    "下一期",
    "再来",
    "建议",
)
_COMPLAINT_MARKERS: Final[tuple[str, ...]] = (
    "太吵",
    "太水",
    "标题党",
    "无聊",
    "广告",
    "太长",
    "太短",
    "废话",
    "重复",
    "不好看",
)
_POSITIVE_MARKERS: Final[tuple[str, ...]] = ("好看", "有意思", "笑死", "支持", "喜欢", "爱了", "真牛")
_NEGATIVE_MARKERS: Final[tuple[str, ...]] = ("难看", "失望", "取关", "差评", "不行", "别发了")
#: 小句切分（中英文标点都算：反馈是人手敲的，标点不可能统一）
_CLAUSE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[。！？!?；;\n]+")
#: 命中片段截断长度（``wants_json`` 是给人看的，不是存档）
_QUOTE_LIMIT: Final[int] = 40
#: ``topic_candidates.reason`` 的落库截断长度（DDL 无约束，但别让面板撑爆）
_REASON_LIMIT: Final[int] = 200


# ══════════════════════════════════════════════════════════════════════
# Agent 结构契约（测试可注入假件）
# ══════════════════════════════════════════════════════════════════════


class PlannerLike(Protocol):
    """方向分析 Agent 的结构类型（``PlannerAgent`` 满足它）。"""

    async def run(self, ctx: AgentContext, payload: PlannerInput) -> AgentResult[PlannerOutput]: ...


class IdeatorLike(Protocol):
    """选题 Agent 的结构类型（``IdeatorAgent`` 满足它）。"""

    async def run(self, ctx: AgentContext, payload: IdeatorInput) -> AgentResult[IdeatorOutput]: ...


class ClassifierLike(Protocol):
    """反馈分类 Agent 的结构类型（``FeedbackClassifierAgent`` 满足它）。"""

    async def run(self, ctx: AgentContext, payload: FeedbackBatch) -> AgentResult[FeedbackClassification]: ...


# ══════════════════════════════════════════════════════════════════════
# 关键词兜底分类（分类 Agent 失败时的降级路径）
# ══════════════════════════════════════════════════════════════════════


def classify_by_keywords(text: str) -> tuple[Sentiment, list[str], list[str]]:
    """零 LLM 的粗分类 ⇒ ``(sentiment, wants, complaints)``。

    它替代不了 LLM 的语义判断，但保证"分类挂了 ⇒ 反馈摘要不会整个空掉"：
    Planner 依旧拿得到"有人在要什么 / 有人在骂什么"，而不是收到一个空 digest
    然后按 ``low_grounding`` 硬写（那是把降级放大成质量事故）。
    """
    wants = _match_clauses(text, _WANT_MARKERS)
    complaints = _match_clauses(text, _COMPLAINT_MARKERS)
    positive = any(marker in text for marker in _POSITIVE_MARKERS)
    negative = any(marker in text for marker in _NEGATIVE_MARKERS)
    if negative and not positive:
        sentiment: Sentiment = "neg"
    elif positive and not negative:
        sentiment = "pos"
    elif complaints and not wants:
        sentiment = "neg"
    elif wants and not complaints:
        sentiment = "pos"
    else:
        sentiment = "neu"
    return sentiment, wants, complaints


def _match_clauses(text: str, markers: Sequence[str]) -> list[str]:
    """命中小句（截断 + 按原文顺序去重）。"""
    found: list[str] = []
    for clause in _CLAUSE_SPLIT.split(text):
        stripped = clause.strip()
        if not stripped or not any(marker in stripped for marker in markers):
            continue
        quote = stripped[:_QUOTE_LIMIT]
        if quote not in found:
            found.append(quote)
    return found


def db_sentiment(raw: str | None) -> Sentiment:
    """``feedback_items.sentiment``（DDL 口径）→ 领域简写（认不出的值一律 ``unknown``）。

    领域层只认 ``pos/neu/neg/unknown``（§04.1.7），落库只认 ``positive/…``（§03.3.3），
    两个口径的换算**只在这一处**（裁定 66）。
    """
    mapped = DB_TO_SENTIMENT.get(raw or "")
    return cast("Sentiment", mapped if mapped is not None else "unknown")


def _fallback_item(ref: str, text: str) -> ClassifiedItem:
    sentiment, wants, complaints = classify_by_keywords(text)
    return ClassifiedItem(ref=ref, sentiment=sentiment, wants=wants, complaints=complaints)


# ══════════════════════════════════════════════════════════════════════
# 报告对象（CLI / API / WS 共用同一份）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DirectionOutcome:
    """一个方向的选题结果（失败**只影响自己**）。"""

    direction_id: str
    title: str
    ok: bool
    inserted: int = 0
    dropped: int = 0
    demoted: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction_id": self.direction_id,
            "title": self.title,
            "ok": self.ok,
            "inserted": self.inserted,
            "dropped": self.dropped,
            "demoted": self.demoted,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class AnalyzeReport:
    """一次 Planner 运行的结果。"""

    ok: bool
    batch_id: str | None = None
    directions: list[DirectionRow] = field(default_factory=list)
    rule_report: RuleReport | None = None
    hot_imported: int = 0
    hot_bad: int = 0
    feedback_imported: int = 0
    feedback_classified: int = 0
    hot_consumed: int = 0
    archived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    @property
    def direction_count(self) -> int:
        return len(self.directions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "batch_id": self.batch_id,
            "direction_count": self.direction_count,
            "directions": [
                {
                    "id": row.id,
                    "seq": row.seq,
                    "title": row.title,
                    "rationale": row.rationale,
                    "priority": row.priority,
                    "risk_flags": row.risk_flags,
                    "grounded_on": row.grounded_on,
                }
                for row in self.directions
            ],
            "rule_report": None if self.rule_report is None else self.rule_report.model_dump(mode="json"),
            "hot_imported": self.hot_imported,
            "hot_bad": self.hot_bad,
            "feedback_imported": self.feedback_imported,
            "feedback_classified": self.feedback_classified,
            "hot_consumed": self.hot_consumed,
            "archived": list(self.archived),
            "warnings": list(self.warnings),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class ManualTopicOutcome:
    """一次"人工加选题"的结果（``similar_to`` 非空 ⇒ 库里有很像的，**但仍入库**）。"""

    topic_id: str
    direction_id: str
    title: str
    hook_type: str | None = None
    score: float | None = None
    similar_to: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "direction_id": self.direction_id,
            "title": self.title,
            "hook_type": self.hook_type,
            "score": self.score,
            "similar_to": list(self.similar_to),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class TopicEditOutcome:
    """一次「改选题」的结果（``changed`` 为空 ⇒ 一个字节都没改）。"""

    topic: TopicRow
    changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"changed": list(self.changed), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class TopicDeleteOutcome:
    """一次「删选题」的结果（``deleted=False`` = 服务层到这一行时它已经没了）。"""

    topic_id: str
    title: str
    deleted: bool

    def to_dict(self) -> dict[str, Any]:
        return {"topic_id": self.topic_id, "title": self.title, "deleted": self.deleted}


@dataclass(frozen=True, slots=True)
class ManualDirectionOutcome:
    """一次「人工写方向」的结果。"""

    direction: DirectionRow

    def to_dict(self) -> dict[str, Any]:
        return {"direction": _direction_snapshot(self.direction)}


@dataclass(frozen=True, slots=True)
class DirectionEditOutcome:
    """一次「改方向」的结果（``changed`` 为空 ⇒ 一个字节都没改）。"""

    direction: DirectionRow
    changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"changed": list(self.changed), "direction": _direction_snapshot(self.direction)}


@dataclass(frozen=True, slots=True)
class DirectionDeleteOutcome:
    """一次「删方向」的结果（``cascaded_topics`` = 跟着走的候选条数）。"""

    direction_id: str
    title: str
    deleted: bool
    cascaded_topics: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction_id": self.direction_id,
            "title": self.title,
            "deleted": self.deleted,
            "cascaded_topics": self.cascaded_topics,
        }


@dataclass(frozen=True, slots=True)
class IdeateReport:
    """一次 Ideator 运行的结果（逐方向）。"""

    batch_id: str
    outcomes: list[DirectionOutcome] = field(default_factory=list)
    inserted: int = 0
    dropped: int = 0
    demoted: int = 0

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def failed(self) -> list[DirectionOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "batch_id": self.batch_id,
            "direction_count": len(self.outcomes),
            "inserted": self.inserted,
            "dropped": self.dropped,
            "demoted": self.demoted,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


# ══════════════════════════════════════════════════════════════════════
# 行 ⇄ 领域模型（``db`` 不认识领域模型，翻译只能在这里）
# ══════════════════════════════════════════════════════════════════════


def direction_spec_from_row(row: DirectionRow) -> DirectionSpec:
    """``content_directions`` 行 → :class:`DirectionSpec`。

    ``fit_score`` 在 DDL 里**没有对应列**，这里取默认 7（放行）：落库的方向
    都是**已经通过**契合度过滤的，重新给它们 7 分与事实一致（T1.9 裁定 73）。
    """
    return DirectionSpec.model_validate(
        {
            "title": row.title,
            "rationale": row.rationale,
            "grounded_on": list(row.grounded_on),
            "priority": row.priority,
            "risk_flags": list(row.risk_flags),
        }
    )


def topic_spec_from_row(row: TopicRow) -> TopicSpec:
    """``topic_candidates`` 行 → :class:`TopicSpec`（写稿阶段的输入契约）。

    ``hook_type`` 在库里是**可空** TEXT（人工加选题时可以不填），而领域契约是
    枚举字面量。对不上的值一律落到 ``"other"`` —— 让"没填钩子类型"变成一条
    可用的选题，而不是让整条链路在这里炸掉（T1.10 裁定 80）。
    """
    hook = row.hook_type if row.hook_type in _HOOK_TYPES else "other"
    return TopicSpec(
        title=row.title,
        hook_type=cast("HookType", hook),
        angle=row.angle,
        exec_feasible=row.exec_feasible,
        score=10.0 if row.score is None else min(10.0, max(0.0, float(row.score))),
        reason=row.reason or "（无评分理由）",
    )


def _hot_spec(row: HotItemRow) -> HotItemSpec:
    return HotItemSpec(
        title=row.title,
        heat=_heat_number(row.heat),
        platform=row.platform,
        raw_line=row.raw_line,
        line_no=row.line_no or 1,
        heat_raw=row.heat,
        parse_ok=row.parse_ok,
        parse_error=None if row.parse_ok else "入库时解析失败（原文已保留）",
    )


def _heat_number(raw: str | None) -> float | None:
    """``heat`` 列是 TEXT（可存 ``'爆' | '高'``）⇒ 能转数值才给数值视图。"""
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _direction_payload(direction: DirectionSpec) -> dict[str, Any]:
    """``DirectionSpec`` → ``content_directions`` 的纯数据（仓储只认这个）。"""
    return {
        "title": direction.title,
        "rationale": direction.rationale,
        "grounded_on": [ref.model_dump(mode="json") for ref in direction.grounded_on],
        "priority": direction.priority,
        "risk_flags": list(direction.risk_flags),
    }


def _topic_payload(*, direction_id: str, topic: TopicSpec, seq: int, dedup: DedupResult) -> dict[str, Any]:
    """``TopicSpec`` + 去重判定 → ``topic_candidates`` 的纯数据。"""
    score = max(0.0, min(10.0, round(topic.score + dedup.score_delta, 2)))
    reason = topic.reason if not dedup.similar_to else f"{topic.reason}；去重：{dedup.reason}"
    return {
        "direction_id": direction_id,
        "seq": seq,
        "title": topic.title,
        "hook_type": topic.hook_type,
        "angle": topic.angle,
        "exec_feasible": topic.exec_feasible,
        "score": score,
        "reason": reason[:_REASON_LIMIT],
        "dedup_hash": dedup.dedup_hash,
        "similar_to": [match.model_dump(mode="json") for match in dedup.similar_to],
        "status": "candidate",
    }


#: 文本比对的最短长度。太短的引文（"好" / "沙发"）会在任何一条反馈里命中 ——
#: 那会把人工录入的依据误标成自动回流，而"这条依据是谁给的"正是这个字段的全部意义。
MIN_QUOTE_MATCH: Final[int] = 6


def _auto_texts(rows: Sequence[FeedbackItemRow]) -> set[str]:
    """自动回流反馈的原文（§06.8 ① 落进 ``feedback_items`` 的那些）。"""
    return {row.content.strip() for row in rows if row.is_auto and row.content.strip()}


def _mark_auto_refs(directions: Sequence[DirectionSpec], auto_texts: set[str]) -> int:
    """引用了自动回流反馈的方向 ⇒ 把那一条 ``grounded_on`` 标 ``source="auto"``（§06.8）。

    为什么是**文本比对**而不是让模型自己声明：模型分不清"这条反馈是人写的还是系统
    回流的"（喂给它的摘要里两类混在一起），而它一旦猜错，面板上的"依据"就会
    把一条自动数据画成人工结论。判据在我们手里（``is_auto`` 就在库那一行上），
    交给模型只是把一件确定的事变成一次抽奖。
    """
    marked = 0
    for direction in directions:
        for ref in direction.grounded_on:
            if ref.type != "feedback":
                continue
            quote = (ref.quote or "").strip()
            if len(quote) < MIN_QUOTE_MATCH:
                continue
            if any(quote in text or text in quote for text in auto_texts):
                ref.source = "auto"
                marked += 1
    return marked


def _digest_specs(
    rows: Sequence[FeedbackItemRow], classified: Mapping[str, ClassifiedItem]
) -> list[FeedbackItemSpec]:
    """DB 行 + 分类结果 → ``FeedbackItemSpec``（``build_feedback_digest`` 的输入）。

    ``line_no`` 用**本次读取的序号**填：``feedback_items`` 表刻意没有 ``line_no``
    列（幂等键是 ``(source_file, content)``，见 §03.3.3），而该字段只是展示用的
    排序位，填序号不撒谎。
    """
    specs: list[FeedbackItemSpec] = []
    for index, row in enumerate(rows, start=1):
        item = classified.get(row.id)
        if item is None:
            continue
        specs.append(
            FeedbackItemSpec(
                platform=row.platform,
                date=row.occurred_on,
                sentiment=item.sentiment,
                kind=sentiment_to_kind(item),
                text=row.content,
                is_auto=row.is_auto,
                raw_line=row.content,
                line_no=index,
            )
        )
    return specs


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class TopicService:
    """选题流水线的唯一编排入口（§04.1.2 / §04.1.3 / §04.1.7）。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        planner: PlannerLike,
        ideator: IdeatorLike,
        classifier: ClassifierLike | None = None,
        paths: StudioPaths | None = None,
        log: LogSink | None = None,
        input_service: InputService | None = None,
        include_auto_feedback: bool = True,
    ) -> None:
        self._planner = planner
        self._ideator = ideator
        self._classifier = classifier
        self._paths = paths or StudioPaths.from_env()
        self._log = log
        self._input = input_service or InputService(connection, paths=self._paths, log=log)
        #: 发布回流自动写入的反馈要不要喂给 Planner（§06.8 安全阀 ①）。
        self._include_auto = include_auto_feedback
        self._hot = HotItemRepo(connection)
        self._feedback = FeedbackItemRepo(connection)
        self._directions = DirectionRepo(connection)
        self._topics = TopicRepo(connection)
        self._audit = AuditRepo(connection)

    # ── ① 方向分析 ──────────────────────────────────────────────────
    async def run_planner(
        self,
        *,
        persona: PersonaConfig,
        trace_id: str | None = None,
        batch_id: str | None = None,
        import_sources: bool = True,
    ) -> AnalyzeReport:
        """导入输入 → 分类反馈 → Planner → 落方向 → 消费热点 → 归档热点文件。"""
        trace = trace_id or new_ulid()
        hot_report = self._input.import_hot() if import_sources else None
        feedback_report = self._input.import_feedback() if import_sources else None
        warnings = _import_warnings(hot_report, feedback_report)

        hot_rows = self._hot.list_unconsumed(limit=HOT_LIMIT)
        feedback_rows = self._feedback.list_recent(limit=FEEDBACK_LIMIT)
        classified = await self._classify_feedback(
            persona=persona, rows=feedback_rows, trace_id=trace, warnings=warnings
        )
        auto_texts = _auto_texts(feedback_rows)
        specs = _digest_specs(feedback_rows, classified)
        if not self._include_auto and auto_texts:
            specs = [item for item in specs if not item.is_auto]
            warnings.append(
                f"llm.yaml 关掉了 planner.include_auto_feedback："
                f"{len(auto_texts)} 条自动回流反馈没喂给 Planner（数据仍在库里）"
            )
        digest = build_feedback_digest(specs, top_n=DIGEST_TOP_N)

        result = await self._planner.run(
            AgentContext(persona=persona, trace_id=trace),
            PlannerInput(
                hot_items=[_hot_spec(row) for row in hot_rows],
                feedback_digest=digest,
                count_min=DIRECTION_COUNT_MIN,
                count_max=DIRECTION_COUNT_MAX,
            ),
        )
        hot_imported = 0 if hot_report is None else hot_report.inserted
        hot_bad = 0 if hot_report is None else hot_report.bad
        feedback_imported = 0 if feedback_report is None else feedback_report.inserted
        if not result.ok or result.data is None:
            self._emit(
                "error",
                f"方向分析失败：{result.error_message or result.error_code}",
                payload={"trace_id": trace, "error_code": result.error_code},
            )
            return AnalyzeReport(
                ok=False,
                warnings=warnings,
                error_code=result.error_code or ErrorCode.TOPIC_DIRECTION_EMPTY,
                error_message=result.error_message or "Planner 未返回可用方向",
                hot_imported=hot_imported,
                hot_bad=hot_bad,
                feedback_imported=feedback_imported,
                feedback_classified=len(classified),
            )

        warnings.extend(result.warnings)
        directions = result.data.directions
        if not directions:
            return AnalyzeReport(
                ok=False,
                warnings=warnings,
                error_code=ErrorCode.TOPIC_DIRECTION_EMPTY,
                error_message="四条规则过滤后没有任何方向（契合度全部不足）",
                hot_imported=hot_imported,
                hot_bad=hot_bad,
                feedback_imported=feedback_imported,
                feedback_classified=len(classified),
            )

        # §06.8 闭环：把"这条依据来自发布回流"确定性地标回 grounded_on。
        auto_refs = _mark_auto_refs(directions, auto_texts)

        batch = batch_id or DirectionRepo.new_batch_id()
        ids = self._directions.insert_batch(
            batch_id=batch,
            directions=[_direction_payload(item) for item in directions],
            llm_model=result.model or None,
            prompt_version=result.prompt_version or None,
        )
        rows = self._directions.list_batch(batch)
        consumed = self._consume_hot(hot_rows=hot_rows, directions=directions, direction_ids=ids)
        archived = self._archive_consumed()
        self._emit(
            "info",
            f"选题批次 {batch}：{len(rows)} 个方向（热点消费 {consumed} 条，归档 {len(archived)} 个文件）",
            payload={
                "batch_id": batch,
                "direction_count": len(rows),
                # §04.4.3 的载荷契约：面板要能**只凭这一条事件**把方向卡片画出来
                # （否则还得再发一次 REST 请求，而"事件到了但列表还是空的"正是
                # 面板最容易被骂的那种闪烁）。
                "directions": [
                    {
                        "id": row.id,
                        "seq": row.seq,
                        "title": row.title,
                        "rationale": row.rationale,
                        "priority": row.priority,
                        "risk_flags": list(row.risk_flags),
                    }
                    for row in rows
                ],
                "hot_consumed": consumed,
                "archived": archived,
                "auto_refs": auto_refs,
                "warnings": warnings[:10],
            },
            event_kind=EventKind.DIRECTION_BATCH_READY,
        )
        return AnalyzeReport(
            ok=True,
            batch_id=batch,
            directions=rows,
            rule_report=result.data.rule_report,
            hot_consumed=consumed,
            archived=archived,
            warnings=warnings,
            hot_imported=hot_imported,
            hot_bad=hot_bad,
            feedback_imported=feedback_imported,
            feedback_classified=len(classified),
        )

    # ── ② 选题生成 ──────────────────────────────────────────────────
    async def run_ideator(
        self,
        *,
        persona: PersonaConfig,
        batch_id: str | None = None,
        direction_ids: Sequence[str] | None = None,
        per_direction: int = TOPICS_PER_DIRECTION,
        trace_id: str | None = None,
    ) -> IdeateReport:
        """逐方向产出选题（**一个方向失败不影响其他方向**）。"""
        trace = trace_id or new_ulid()
        batch = batch_id or self._directions.latest_batch_id()
        if batch is None:
            raise StudioError(
                "还没有任何选题批次（先跑 `studio topics analyze`）",
                code=ErrorCode.TOPIC_BATCH_NOT_FOUND,
                context={"batch_id": batch_id},
                remediation="执行 `studio topics analyze` 生成方向后再选题",
            )
        rows = self._directions.list_batch(batch)
        if direction_ids is not None:
            wanted = set(direction_ids)
            rows = [row for row in rows if row.id in wanted]
        if not rows:
            raise StudioError(
                f"批次 {batch} 没有可选题的方向",
                code=ErrorCode.TOPIC_DIRECTION_EMPTY,
                context={"batch_id": batch, "direction_ids": list(direction_ids or ())},
                remediation="换一个 batch_id，或重新跑 `studio topics analyze`",
            )

        pool = [
            ExistingTopic(id=row.id, title=row.title, dedup_hash=row.dedup_hash)
            for row in self._topics.list_for_dedup(limit=DEDUP_POOL_LIMIT)
        ]
        outcomes: list[DirectionOutcome] = []
        for row in rows:
            outcomes.append(
                await self._ideate_one(
                    persona=persona, row=row, pool=pool, per_direction=per_direction, trace_id=trace
                )
            )

        report = IdeateReport(
            batch_id=batch,
            outcomes=outcomes,
            inserted=sum(item.inserted for item in outcomes),
            dropped=sum(item.dropped for item in outcomes),
            demoted=sum(item.demoted for item in outcomes),
        )
        self._emit(
            "info" if report.ok else "warn",
            f"选题批次 {batch}：新增 {report.inserted} 个选题"
            f"（丢弃重复 {report.dropped}，降分 {report.demoted}，失败方向 {len(report.failed)}）",
            payload=report.to_dict(),
        )
        return report

    # ── 内部 · 反馈分类 ─────────────────────────────────────────────
    async def _classify_feedback(
        self,
        *,
        persona: PersonaConfig,
        rows: Sequence[FeedbackItemRow],
        trace_id: str,
        warnings: list[str],
    ) -> dict[str, ClassifiedItem]:
        """批量分类 + 回填 ``feedback_items``。

        **已分类的不再烧钱**：``sentiment IS NOT NULL`` 的行直接复用库里结果，
        只把 ``sentiment IS NULL`` 的送去模型（或关键词兜底）。
        """
        classified: dict[str, ClassifiedItem] = {}
        pending: list[FeedbackItemRow] = []
        for row in rows:
            if row.sentiment is None:
                pending.append(row)
                continue
            classified[row.id] = ClassifiedItem(
                ref=row.id,
                sentiment=db_sentiment(row.sentiment),
                wants=list(row.wants),
                complaints=list(row.complaints),
            )
        if not pending:
            return classified

        if self._classifier is None:
            warnings.append("feedback_classified_by_keywords")
            for row in pending:
                classified[row.id] = self._apply_fallback(row)
            return classified

        ctx = AgentContext(persona=persona, trace_id=trace_id)
        for start in range(0, len(pending), FEEDBACK_BATCH_SIZE):
            chunk = pending[start : start + FEEDBACK_BATCH_SIZE]
            batch = FeedbackBatch(
                items=[
                    FeedbackBatchItem(ref=row.id, text=row.content, platform=row.platform) for row in chunk
                ]
            )
            result = await self._classifier.run(ctx, batch)
            if not result.ok or result.data is None:
                warnings.append(f"feedback_classify_failed:{result.error_code or 'unknown'}")
                for row in chunk:
                    classified[row.id] = self._apply_fallback(row)
                continue
            by_ref = {item.ref: item for item in result.data.items}
            for row in chunk:
                item = by_ref.get(row.id)
                if item is None:
                    # ``ref`` 对不上 ⇒ **丢弃而不是猜**：猜错会把"想看的"写成"吐槽的"
                    warnings.append(f"feedback_ref_missing:{row.id}")
                    classified[row.id] = self._apply_fallback(row)
                    continue
                self._feedback.apply_classification(
                    item_id=row.id,
                    sentiment=SENTIMENT_TO_DB.get(item.sentiment, "unknown"),
                    wants=item.wants,
                    complaints=item.complaints,
                )
                classified[row.id] = item
        return classified

    def _apply_fallback(self, row: FeedbackItemRow) -> ClassifiedItem:
        """关键词兜底 + 回填（下次运行就不必再兜底一次）。"""
        item = _fallback_item(row.id, row.content)
        self._feedback.apply_classification(
            item_id=row.id,
            sentiment=SENTIMENT_TO_DB.get(item.sentiment, "unknown"),
            wants=item.wants,
            complaints=item.complaints,
        )
        return item

    # ── 内部 · 单方向选题 ───────────────────────────────────────────
    async def _ideate_one(
        self,
        *,
        persona: PersonaConfig,
        row: DirectionRow,
        pool: list[ExistingTopic],
        per_direction: int,
        trace_id: str,
    ) -> DirectionOutcome:
        """一个方向：调用 → 两级去重 → 入库 → 回写方向状态。

        ``pool`` 是**共享且就地追加**的：A 方向刚产出的选题，B 方向立刻能看见，
        否则"同一批里两个方向撞车"只能靠事后去重，而那时两行都已经写进去了。
        """
        try:
            result = await self._ideator.run(
                AgentContext(persona=persona, trace_id=trace_id),
                IdeatorInput(
                    direction=direction_spec_from_row(row),
                    per_direction=per_direction,
                    existing_titles=[item.title for item in pool[:EXISTING_TITLE_LIMIT]],
                ),
            )
        except StudioError as exc:
            return self._failed(row, code=str(exc.code), message=str(exc))
        # 兜住一切：某个方向的 bug 不该让整批选题归零（§04.1.3「单个方向失败不影响其他方向」）
        except Exception as exc:
            logger.exception("ideator_crashed", direction_id=row.id)
            return self._failed(row, code=str(ErrorCode.TOPIC_DIRECTION_EMPTY), message=repr(exc))

        if not result.ok or result.data is None:
            return self._failed(
                row,
                code=result.error_code or str(ErrorCode.TOPIC_DIRECTION_EMPTY),
                message=result.error_message or "Ideator 未返回选题",
            )

        payloads: list[dict[str, Any]] = []
        kept: list[TopicSpec] = []
        dropped = 0
        demoted = 0
        for seq, topic in enumerate(result.data.topics, start=1):
            dedup = dedup_topic(topic.title, pool)
            if dedup.action is DedupAction.DROP:
                dropped += 1
                self._emit(
                    "info",
                    f"选题《{topic.title}》命中库内重复，已丢弃",
                    payload={"direction_id": row.id, "reason": dedup.reason},
                )
                continue
            if dedup.action is DedupAction.DEMOTE:
                demoted += 1
            payloads.append(_topic_payload(direction_id=row.id, topic=topic, seq=seq, dedup=dedup))
            kept.append(topic)
            pool.append(ExistingTopic(id=None, title=topic.title, dedup_hash=dedup.dedup_hash))

        ids = self._topics.insert_many(payloads)
        self._directions.set_status(direction_id=row.id, status="selected" if ids else "dropped")
        if ids:
            # §04.4.3 的载荷契约（``merge_field='direction_id'`` ⇒ **一方向一条**）：
            # 方向之间互不影响，事件也就该各自到达 —— 一条"整批"事件会让面板在
            # 某个方向失败时无法区分"这个方向没产出"与"整批还没跑完"。
            self._emit(
                "info",
                f"方向《{row.title}》产出 {len(ids)} 个选题",
                payload={
                    "direction_id": row.id,
                    "topics": [
                        {
                            "id": topic_id,
                            "title": topic.title,
                            "hook_type": topic.hook_type,
                            "score": topic.score,
                            "reason": topic.reason,
                        }
                        for topic_id, topic in zip(ids, kept, strict=True)
                    ],
                },
                event_kind=EventKind.TOPIC_BATCH_READY,
            )
        return DirectionOutcome(
            direction_id=row.id,
            title=row.title,
            ok=True,
            inserted=len(ids),
            dropped=dropped,
            demoted=demoted,
        )

    def _failed(self, row: DirectionRow, *, code: str, message: str) -> DirectionOutcome:
        self._emit(
            "warn",
            f"方向《{row.title}》选题失败：{message}",
            payload={"direction_id": row.id, "error_code": code},
        )
        return DirectionOutcome(
            direction_id=row.id, title=row.title, ok=False, error_code=code, error_message=message
        )

    # ── 内部 · 热点消费与归档 ───────────────────────────────────────
    def _consume_hot(
        self,
        *,
        hot_rows: Sequence[HotItemRow],
        directions: Sequence[DirectionSpec],
        direction_ids: Sequence[str],
    ) -> int:
        """标记热点消费：**被引用的记方向，没被引用的只记时间**（裁定 72）。

        消费的是"本轮喂给 Planner 的全部热点"，不是"被采纳的那几条"：
        热点是一次性消耗品，留着不消费只会让它们反复占用下一轮的提示词预算。
        """
        if not hot_rows:
            return 0
        owner: dict[str, str] = {}
        for direction, direction_id in zip(directions, direction_ids, strict=True):
            for ref in direction.grounded_on:
                if ref.type == "hot" and ref.ref_id:
                    owner.setdefault(ref.ref_id, direction_id)

        grouped: dict[str | None, list[str]] = {}
        for row in hot_rows:
            grouped.setdefault(owner.get(row.id), []).append(row.id)
        return sum(
            self._hot.mark_consumed(item_ids=item_ids, direction_id=direction_id)
            for direction_id, item_ids in grouped.items()
        )

    def _archive_consumed(self) -> list[str]:
        """把**已全部消费**的热点文件移入 ``data/hot/archive/``。

        判据来自库（``list_consumed_sources``）而不是本轮的导入报告：
        ``--no-import`` 那次运行同样该把上一轮消费完的文件收走。
        """
        return self._input.archive_hot(self._hot.list_consumed_sources())

    # ── 人工加选题 ──────────────────────────────────────────────────
    def add_manual_topic(
        self,
        *,
        title: str,
        angle: str,
        hook_type: str | None = None,
        score: float | None = None,
        reason: str | None = None,
        actor: str = "user",
    ) -> ManualTopicOutcome:
        """人工加一条选题（**直接入库** + ``audit_ops``）。

        与 Ideator 的两处刻意不同（T4.3 裁定 131）：

        1. **去重只提示、不拦**：模型产出的重复是噪音（白烧 token），人加的重复是
           **明确意图**（"我就是要做这条，哪怕跟上次像"）。命中相似度只写进
           ``similar_to_json`` + 回一条 ``warnings``，让人自己判断。
        2. **``exec_feasible`` 恒为真**：那一位是模型对"3 分钟内能不能做完"的估计；
           人加了它却没有可估的输入 —— 编一个值比留空更糟。
        """
        cleaned = title.strip()
        if not cleaned:
            raise StudioError(
                "选题标题不能为空",
                code=ErrorCode.TOPIC_SELECT_INVALID,
                context={"title": title},
                remediation="填一个不超过 50 字的标题",
            )
        normalized = hook_type if hook_type in _HOOK_TYPES else None
        pool = [
            ExistingTopic(id=row.id, title=row.title, dedup_hash=row.dedup_hash)
            for row in self._topics.list_for_dedup(limit=DEDUP_POOL_LIMIT)
        ]
        dedup = dedup_topic(cleaned, pool)
        warnings: list[str] = []
        similar = [item.model_dump(mode="json") for item in dedup.similar_to]
        if similar:
            warnings.append(
                f"与库内 {len(similar)} 条选题相似（最高 {dedup.similarity:.2f}）："
                + "、".join(str(item.get("target_title", "?")) for item in similar)
            )

        direction_id = self._ensure_manual_direction()
        ids = self._topics.insert_many(
            [
                {
                    "direction_id": direction_id,
                    "seq": 1,
                    "title": cleaned,
                    "hook_type": normalized,
                    "angle": angle.strip() or "人工指定",
                    "exec_feasible": True,
                    "score": score,
                    "reason": reason or "人工加选题",
                    "dedup_hash": dedup.dedup_hash,
                    "similar_to": similar,
                    "status": "candidate",
                }
            ]
        )
        topic_id = ids[0]
        self._audit.record(
            actor=actor,
            action="topic.manual_add",
            target_type="topic",
            target_id=topic_id,
            after={
                "title": cleaned,
                "angle": angle,
                "hook_type": normalized,
                "score": score,
                "similarity": dedup.similarity,
            },
            reason=reason or "人工加选题",
            source="webui",
        )
        self._emit(
            "warn" if similar else "info",
            f"人工加选题《{cleaned}》已入库" + (f"（{warnings[0]}）" if warnings else ""),
            payload={"topic_id": topic_id, "direction_id": direction_id, "similar_to": similar},
        )
        return ManualTopicOutcome(
            topic_id=topic_id,
            direction_id=direction_id,
            title=cleaned,
            hook_type=normalized,
            score=score,
            similar_to=similar,
            warnings=warnings,
        )

    def _ensure_manual_direction(self) -> str:
        """人工加选题挂靠的方向（**懒建一次**，之后一直复用）。

                按**标题**找，而不是取批次里的第一条：``manual`` 批次里除了它，还可能
        躺着人自己写的方向（:meth:`add_manual_direction` 在没有别的批次时会落到这里）——
                取第一条会把"人工加选题"挂到人写的那个方向下面去。
        """
        for row in self._directions.list_batch(MANUAL_BATCH_ID):
            if row.title == MANUAL_DIRECTION_TITLE:
                return row.id
        row = self._directions.insert_manual(
            batch_id=MANUAL_BATCH_ID,
            title=MANUAL_DIRECTION_TITLE,
            rationale="人在 WebUI 上直接加的选题（不经模型）",
            priority=1,
            status="selected",
        )
        return row.id

    def update_topic(
        self,
        *,
        topic_id: str,
        changes: Mapping[str, Any],
        actor: str = "user",
    ) -> TopicEditOutcome:
        """改一条选题（``changes`` 的键 = 要改的列，值 ``None`` = 置空）。

        三件事刻意放在**服务层**而不是路由层：

        1. **空 PATCH 不假装改了一次**（与 ``assets`` 的 PATCH 同一取舍）：回当前行、
           ``changed`` 为空、**不写留痕** —— 一条什么都没改的审计行只会稀释审计。
        2. **改了标题就重算去重指纹**：``dedup_hash`` 是 R15 的判据，标题改了却留着旧
           指纹，等于让「这条跟谁像」从此说谎，后续同名选题会一路漏进池子。顺带把
           ``similar_to_json`` 也按新标题刷新一遍（**排除自己** —— 自己跟自己永远 100% 像）。
        3. **只在真改了才写 ``audit_ops``**，``before``/``after`` 逐列都记全。

        改状态**不走这里**：``status`` 有它自己的入口（``set_status``）与语义，混进来
        只会让「改标题」顺手把一条已入队的选题变回候选。
        """
        row = self._topics.get(topic_id)
        if row is None:
            raise StudioError(
                f"选题不存在：{topic_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"topic_id": topic_id},
                remediation="刷新选题池 —— 可能已经被别的标签页删掉了",
            )
        updates: dict[str, Any] = {}
        warnings: list[str] = []
        for column, raw in changes.items():
            # 各列的值类型不同（str / float / None）⇒ 显式 Any，别让第一支决定整条链的类型
            value: Any
            if column == "title":
                value = _clean_title(raw, topic_id=topic_id)
            elif column == "angle":
                value = str(raw).strip() or "人工指定"
            elif column == "hook_type":
                value = raw if raw in _HOOK_TYPES else None
            elif column == "score":
                value = _clean_score(raw, topic_id=topic_id)
            elif column == "reason":
                value = None if raw is None else (str(raw).strip() or None)
            else:
                raise ValueError(f"不可改写的列：{column}")
            if getattr(row, column) != value:
                updates[column] = value
        if "title" in updates:
            dedup_hash, similar = self._refresh_dedup(topic_id, str(updates["title"]))
            updates["dedup_hash"] = dedup_hash
            updates["similar_to"] = similar
            if similar:
                top = float(similar[0].get("similarity") or 0.0)
                names = "、".join(str(item.get("target_title", "?")) for item in similar)
                warnings.append(f"与库内 {len(similar)} 条选题相似（最高 {top:.2f}）：{names}")
        if not updates:
            return TopicEditOutcome(topic=row, changed=[], warnings=[])
        updated = self._topics.patch(topic_id=topic_id, changes=updates)
        if updated is None:  # pragma: no cover - get 与 patch 之间被删掉的窗口
            raise StudioError(
                f"选题不存在：{topic_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"topic_id": topic_id},
                remediation="刷新选题池",
            )
        # ``changed`` 报的是**调用方点名要改的列**：``dedup_hash`` / ``similar_to``
        # 是改标题带出来的副作用，把它们混进「改了哪几列」里只会让人看不懂面板在说什么。
        changed = sorted(changes)
        self._audit.record(
            actor=actor,
            action="topic.updated",
            target_type="topic",
            target_id=topic_id,
            before=_edit_snapshot(row),
            after=_edit_snapshot(updated),
            reason="WebUI 改选题",
            source="webui",
        )
        self._emit(
            "warn" if warnings else "info",
            f"选题《{updated.title}》已更新（{'、'.join(changed)}）",
            payload={"topic_id": topic_id, "changed": changed},
        )
        return TopicEditOutcome(topic=updated, changed=changed, warnings=warnings)

    def delete_topic(self, *, topic_id: str, actor: str = "user") -> TopicDeleteOutcome:
        """硬删一条选题（**派生过任务的不给删**）。

        为什么只拦「有任务」这一种：任务是通过 ``topic_id`` 反查选题的
        （``ScriptService.draft`` 先 ``topics.get(topic_id)``），选题没了那条任务就
        再也写不出稿 —— 这不是门禁，是**删掉之后会立刻断链**的那一种。
        其余状态（``rejected`` / ``expired`` / 老的 ``candidate``）删掉不牵连任何东西。
        """
        row = self._topics.get(topic_id)
        if row is None:
            raise StudioError(
                f"选题不存在：{topic_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"topic_id": topic_id},
                remediation="刷新选题池 —— 可能已经被别的标签页删掉了",
            )
        if row.task_id:
            raise StudioError(
                f"《{row.title}》已经派生过任务，不能直接删",
                code=ErrorCode.TOPIC_SELECT_INVALID,
                context={"topic_id": topic_id, "task_id": row.task_id},
                remediation=f"先处理任务 {row.task_id}（删掉它或让它跑完），再回来删这条选题",
            )
        deleted = self._topics.delete(topic_id)
        self._audit.record(
            actor=actor,
            action="topic.deleted",
            target_type="topic",
            target_id=topic_id,
            before=_edit_snapshot(row),
            reason="WebUI 删除选题",
            source="webui",
        )
        self._emit(
            "info",
            f"选题《{row.title}》已删除",
            payload={"topic_id": topic_id, "direction_id": row.direction_id},
        )
        return TopicDeleteOutcome(topic_id=topic_id, title=row.title, deleted=deleted)

    # ── 方向 · 人工写 / 改 / 删（T4.3 追加）──────────────────────────
    def add_manual_direction(
        self,
        *,
        title: str,
        rationale: str,
        priority: int = 100,
        batch_id: str | None = None,
        actor: str = "user",
    ) -> ManualDirectionOutcome:
        """人工写一个方向（**不经模型**）。

        落进**当前正在看的那个批次**，而不是另起一个「手工批次」：方向这一列是
        "这一批要做什么"，人写的与模型产的混在一起才看得见全貌；单独开一个批次，
        人写完那一刻它会顶到"最近一批"上，把模型那批整个盖掉 —— 而再跑一次
        ``analyze`` 又会反过来把人写的盖掉。``batch_id`` 缺省取最近一批；一条批次
        都没有（全新库）时才落到 :data:`MANUAL_BATCH_ID`。
        """
        cleaned_title = _clean_direction_title(title, direction_id=None)
        cleaned_rationale = _clean_direction_rationale(rationale)
        batch = batch_id or self._directions.latest_batch_id() or MANUAL_BATCH_ID
        row = self._directions.insert_manual(
            batch_id=batch,
            title=cleaned_title,
            rationale=cleaned_rationale,
            priority=_clean_priority(priority, direction_id=None),
        )
        self._audit.record(
            actor=actor,
            action="direction.created",
            target_type="direction",
            target_id=row.id,
            after=_direction_snapshot(row),
            reason="人工写方向",
            source="webui",
        )
        self._emit(
            "info",
            f"人工方向《{row.title}》已入库（批次 {batch}）",
            payload={"direction_id": row.id, "batch_id": batch},
        )
        return ManualDirectionOutcome(direction=row)

    def update_direction(
        self,
        *,
        direction_id: str,
        changes: Mapping[str, Any],
        actor: str = "user",
    ) -> DirectionEditOutcome:
        """改一个方向（``changes`` 的键 = 要改的列）。

        与 :meth:`update_topic` 同一取舍：**空 PATCH 不假装改了一次**（回当前行、
        ``changed`` 为空、不写留痕），只有真改了才留痕。``batch_id`` / ``seq`` /
        ``status`` 不可改 —— 前两个是"这一批怎么排的"，后者有自己的语义。
        """
        row = self._directions.get(direction_id)
        if row is None:
            raise StudioError(
                f"方向不存在：{direction_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"direction_id": direction_id},
                remediation="刷新选题面板 —— 可能已经被别的标签页删掉了",
            )
        updates: dict[str, Any] = {}
        for column, raw in changes.items():
            if column == "title":
                value: Any = _clean_direction_title(raw, direction_id=direction_id)
            elif column == "rationale":
                value = _clean_direction_rationale(raw)
            elif column == "priority":
                value = _clean_priority(raw, direction_id=direction_id)
            elif column == "risk_flags":
                value = [str(item) for item in raw]
            else:
                raise ValueError(f"不可改写的列：{column}")
            if getattr(row, column) != value:
                updates[column] = value
        if not updates:
            return DirectionEditOutcome(direction=row, changed=[])
        updated = self._directions.patch(direction_id=direction_id, changes=updates)
        if updated is None:  # pragma: no cover - get 与 patch 之间被删掉的窗口
            raise StudioError(
                f"方向不存在：{direction_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"direction_id": direction_id},
                remediation="刷新选题面板",
            )
        self._audit.record(
            actor=actor,
            action="direction.updated",
            target_type="direction",
            target_id=direction_id,
            before=_direction_snapshot(row),
            after=_direction_snapshot(updated),
            reason="WebUI 改方向",
            source="webui",
        )
        self._emit(
            "info",
            f"方向《{updated.title}》已更新（{'、'.join(sorted(changes))}）",
            payload={"direction_id": direction_id, "changed": sorted(changes)},
        )
        return DirectionEditOutcome(direction=updated, changed=sorted(changes))

    def delete_direction(self, *, direction_id: str, actor: str = "user") -> DirectionDeleteOutcome:
        """删一个方向，**它下面的候选一起走**（``ON DELETE CASCADE``）。

        唯一拦下的情形与 :meth:`delete_topic` 同源：**已经有候选派生了任务**。
        那种候选被级联删掉之后，它那条任务就再也写不出稿（``draft`` 要按 topic_id
        反查选题）—— 这不是门禁，是"删掉之后立刻断链"。所以这里先把那几条点出来，
        让人自己决定先处理哪一条。
        """
        row = self._directions.get(direction_id)
        if row is None:
            raise StudioError(
                f"方向不存在：{direction_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"direction_id": direction_id},
                remediation="刷新选题面板 —— 可能已经被别的标签页删掉了",
            )
        attached = [item for item in self._topics.list_by_direction(direction_id) if item.task_id is not None]
        if attached:
            raise StudioError(
                f"方向《{row.title}》下有 {len(attached)} 条候选已经派生了任务，不能级联删除",
                code=ErrorCode.TOPIC_SELECT_INVALID,
                context={
                    "direction_id": direction_id,
                    "topics": [{"topic_id": item.id, "title": item.title} for item in attached],
                    "task_ids": [item.task_id for item in attached],
                },
                remediation="先处理这几条任务（删掉它或让它跑完），再回来删这个方向",
            )
        cascaded = self._directions.delete(direction_id)
        self._audit.record(
            actor=actor,
            action="direction.deleted",
            target_type="direction",
            target_id=direction_id,
            before=_direction_snapshot(row),
            after={"cascaded_topics": cascaded},
            reason="WebUI 删除方向（候选一并删除）",
            source="webui",
        )
        self._emit(
            "info",
            f"方向《{row.title}》已删除（一并删掉 {cascaded} 条候选）",
            payload={"direction_id": direction_id, "cascaded_topics": cascaded},
        )
        return DirectionDeleteOutcome(
            direction_id=direction_id, title=row.title, deleted=True, cascaded_topics=cascaded
        )

    def _refresh_dedup(self, topic_id: str, title: str) -> tuple[str, list[dict[str, Any]]]:
        """按新标题重算 ``dedup_hash`` + 相似清单（**排除自己**）。

        排除自己不是洁癖：不排除的话每一次改标题都会给这一行留下一条
        「跟自己 100% 像」的记录，面板上从此挂着一条永远消不掉的黄字。
        """
        pool = [
            ExistingTopic(id=item.id, title=item.title, dedup_hash=item.dedup_hash)
            for item in self._topics.list_for_dedup(limit=DEDUP_POOL_LIMIT)
            if item.id != topic_id
        ]
        dedup = dedup_topic(title, pool)
        return dedup.dedup_hash, [item.model_dump(mode="json") for item in dedup.similar_to]

    # ── 内部 · 日志出口 ─────────────────────────────────────────────
    def _emit(
        self,
        level: Severity,
        message: str,
        *,
        payload: Mapping[str, Any] | None = None,
        event_kind: EventKind | None = None,
    ) -> None:
        """写一条 ``topics.pipeline`` 日志；``event_kind`` 非空 ⇒ 额外扇出一条 WS 事件。

        与 ``ReviewService._emit`` 同一手法（T4.4 裁定 126）：事件名落在
        ``payload[EVENT_PAYLOAD_KEY]`` 上，Hub 读到就多扇一条，跨进程不需要第二套 IPC。
        """
        merged = dict(payload or {})
        if event_kind is not None:
            merged[EVENT_PAYLOAD_KEY] = event_kind.value
        if self._log is not None:
            self._log(level=level, source="topics.pipeline", message=message, payload=merged)
            return
        # 兜底走 structlog：payload 是自由字典，保留键展开成 kwargs 会直接 TypeError
        extra = {key: value for key, value in merged.items() if key not in _LOG_RESERVED}
        if level in {"warn", "error", "fatal"}:
            logger.warning(message, source="topics.pipeline", **extra)
        else:
            logger.info(message, source="topics.pipeline", **extra)


def _clean_direction_title(value: Any, *, direction_id: str | None) -> str:
    """方向标题去空白 + 非空校验（空标题 = 把这一列改成了没有内容的东西）。"""
    cleaned = str(value).strip()
    if cleaned:
        return cleaned
    raise StudioError(
        "方向标题不能为空",
        code=ErrorCode.TOPIC_SELECT_INVALID,
        context={"direction_id": direction_id},
        remediation="填一个不超过 120 字的标题；想撤掉这个方向请用「删除」",
    )


def _clean_direction_rationale(value: Any) -> str:
    """方向理由去空白（**允许留空**：人写方向时常常只想先占个位置）。"""
    return str(value).strip()


def _clean_priority(value: Any, *, direction_id: str | None) -> int:
    """优先级取整（越小越优先，与 ``content_directions.priority`` 同一口径）。"""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            f"优先级必须是整数：{value!r}",
            code=ErrorCode.TOPIC_SELECT_INVALID,
            context={"direction_id": direction_id, "priority": value},
            remediation="给一个整数（越小越优先，模型产出的是 100）",
        ) from exc


def _direction_snapshot(row: DirectionRow) -> dict[str, Any]:
    """留痕里那几列（``before``/``after`` 用同一份形状，方便逐列比对）。"""
    return {
        "title": row.title,
        "rationale": row.rationale,
        "priority": row.priority,
        "risk_flags": list(row.risk_flags),
        "batch_id": row.batch_id,
        "seq": row.seq,
    }


def _clean_title(value: Any, *, topic_id: str) -> str:
    """标题去空白 + 非空校验（空标题 = 把这条选题改成了没有内容的东西）。"""
    cleaned = str(value).strip()
    if cleaned:
        return cleaned
    raise StudioError(
        "选题标题不能为空",
        code=ErrorCode.TOPIC_SELECT_INVALID,
        context={"topic_id": topic_id},
        remediation="填一个不超过 50 字的标题；想撤掉这条选题请用「删除」",
    )


def _clean_score(value: Any, *, topic_id: str) -> float | None:
    """自评分夹在 0–10（``None`` = 不打分，与人工加选题同一区间）。"""
    if value is None:
        return None
    score = float(value)
    if 0.0 <= score <= 10.0:
        return score
    raise StudioError(
        f"自评分必须在 0–10 之间：{score}",
        code=ErrorCode.TOPIC_SELECT_INVALID,
        context={"topic_id": topic_id, "score": score},
        remediation="给 0 到 10 之间的一个数，或留空表示不打分",
    )


def _edit_snapshot(row: TopicRow) -> dict[str, Any]:
    """留痕里那几列（``before``/``after`` 用同一份形状，方便逐列比对）。"""
    return {
        "title": row.title,
        "angle": row.angle,
        "hook_type": row.hook_type,
        "score": row.score,
        "reason": row.reason,
        "status": row.status,
    }


def _import_warnings(*reports: ImportReport | None) -> list[str]:
    """导入阶段的问题转成告警（**不中断**：坏行照入库，见裁定 67）。"""
    warnings: list[str] = []
    for report in reports:
        if report is None:
            continue
        if report.issues:
            warnings.append(f"{report.kind}_parse_issues:{len(report.issues)}")
        if report.bad:
            warnings.append(f"{report.kind}_bad_lines:{report.bad}")
    return warnings
