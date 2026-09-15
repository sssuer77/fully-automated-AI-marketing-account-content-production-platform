"""选题面板的请求 / 响应契约（T4.3 · §04.1.3 / §04.4.5 第 2 行）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① "模型给了哪些方向？" ⇒ :class:`DirectionList`（**一次给全批次列表**，下拉能切历史批次）；
② "哪些选题值得做？" ⇒ :class:`TopicList`（瀑布流 + 按状态计数）；
③ "跑一次要多久、出了什么？" ⇒ :class:`AnalyzeResult` / :class:`IdeateResult`（含逐方向成败）；
④ "我勾的这几条进队了吗？" ⇒ :class:`SelectResult`（**逐条**结果，一条失败不影响其余）。

请求体为什么全部 ``extra="forbid"``
-----------------------------------
与确认闸同一理由（§04.4.5）：勾选入队是"要花 LLM 预算的意图"，
把 ``topic_ids`` 写成 ``topicIds`` 被静默忽略，会让人以为"我已经选了 6 条"，
而实际上一条都没进去。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.db.models import DirectionRow, TopicRow
from studio.services.input_service import ImportReport
from studio.services.topic_service import (
    AnalyzeReport,
    DirectionOutcome,
    IdeateReport,
    ManualTopicOutcome,
)

__all__ = [
    "AnalyzeBody",
    "AnalyzeResult",
    "DirectionCard",
    "DirectionItem",
    "DirectionList",
    "DirectionOutcomeModel",
    "DraftItem",
    "HotImportBody",
    "HotSubmitBody",
    "IdeateBody",
    "IdeateResult",
    "ImportResult",
    "ManualTopicBody",
    "ManualTopicResult",
    "SelectBody",
    "SelectFailure",
    "SelectItem",
    "SelectResult",
    "TopicItem",
    "TopicList",
]

#: 选题池单页上限（瀑布流是"人一条条看"的东西，给太多反而看不完）
MAX_PAGE: int = 500

#: 历史批次下拉的条数上限
MAX_BATCHES: int = 20


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读 · 选题池与方向
# ══════════════════════════════════════════════════════════════════════


class TopicItem(BaseModel):
    """瀑布流的一条选题（``topic_candidates`` 的展示字段）。"""

    id: str
    direction_id: str
    seq: int
    title: str
    angle: str
    hook_type: str | None = None
    exec_feasible: bool = True
    score: float | None = None
    reason: str | None = None
    dedup_hash: str | None = None
    similar_to: list[dict[str, Any]]
    status: str = "candidate"
    task_id: str | None = None
    selected_at: str | None = None
    selected_by: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: TopicRow) -> TopicItem:
        return cls(**asdict(row))


class TopicList(BaseModel):
    """瀑布流 + 按状态计数（面板顶部的"候选 N 条 / 已入队 M 条"）。"""

    status: str
    topics: list[TopicItem]
    counts: dict[str, int]
    limit: int


class DirectionCard(BaseModel):
    """方向卡片（``content_directions`` 的展示字段）。"""

    id: str
    batch_id: str
    seq: int
    title: str
    rationale: str
    grounded_on: list[dict[str, Any]]
    priority: int = 100
    risk_flags: list[str]
    status: str = "open"
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: DirectionRow) -> DirectionCard:
        return cls(
            id=row.id,
            batch_id=row.batch_id,
            seq=row.seq,
            title=row.title,
            rationale=row.rationale,
            grounded_on=list(row.grounded_on),
            priority=row.priority,
            risk_flags=list(row.risk_flags),
            status=row.status,
            created_at=row.created_at,
        )


class DirectionItem(DirectionCard):
    """方向卡片 + 它下面的选题计数（"这个方向出了 4 条，选中 1 条"）。"""

    topic_count: int = 0
    selected_count: int = 0


class DirectionList(BaseModel):
    """一批方向 + 可切换的历史批次（下拉）。

    ``batches`` 给的是**全部**批次（新到旧）：只看"最近一批"会让人没法回看
    "上周那批方向为什么没选"，而那正是复盘选题质量的入口。
    """

    batch_id: str | None = None
    batches: list[str]
    directions: list[DirectionItem]
    counts: dict[str, int]


# ══════════════════════════════════════════════════════════════════════
# 触发 · 分析与生成
# ══════════════════════════════════════════════════════════════════════


class AnalyzeBody(_Body):
    """触发方向分析（Planner）。"""

    import_sources: bool = Field(default=True, description="先扫 data/hot 与 data/feedback")
    batch_id: str | None = Field(default=None, description="指定批次 id（默认自动生成）")


class AnalyzeResult(BaseModel):
    """一次 Planner 运行的结果（``ok=False`` 也返回 200 —— 失败原因在体内）。"""

    ok: bool
    batch_id: str | None = None
    direction_count: int = 0
    directions: list[DirectionCard]
    rule_report: dict[str, Any] | None = None
    hot_imported: int = 0
    hot_bad: int = 0
    feedback_imported: int = 0
    feedback_classified: int = 0
    hot_consumed: int = 0
    archived: list[str]
    warnings: list[str]
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_report(cls, report: AnalyzeReport) -> AnalyzeResult:
        """``AnalyzeReport`` → 响应模型。

        ``to_dict()`` 里的方向**不带** ``batch_id``（那是"这一次运行"的属性，
        逐条重复一遍没意义），而卡片模型要求它 ⇒ 在这里补上，而不是把
        ``batch_id`` 改成可选（可选字段会让"忘了填"变成静默的空字符串）。
        """
        payload = report.to_dict()
        payload["directions"] = [
            DirectionCard.model_validate({**item, "batch_id": report.batch_id or ""})
            for item in payload["directions"]
        ]
        return cls.model_validate(payload)


class IdeateBody(_Body):
    """触发生成选题（Ideator）。"""

    batch_id: str | None = Field(default=None, description="批次 id（默认取最近一批）")
    direction_ids: list[str] | None = Field(default=None, description="只跑指定方向（空=全部）")
    per_direction: int = Field(default=4, ge=1, le=10, description="每方向目标选题数")


class DirectionOutcomeModel(BaseModel):
    """一个方向的成败（**失败只影响自己** · §04.1.3）。"""

    direction_id: str
    title: str
    ok: bool
    inserted: int = 0
    dropped: int = 0
    demoted: int = 0
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_outcome(cls, outcome: DirectionOutcome) -> DirectionOutcomeModel:
        return cls(**outcome.to_dict())


class IdeateResult(BaseModel):
    """一次 Ideator 运行的结果（逐方向）。"""

    ok: bool
    batch_id: str
    direction_count: int = 0
    inserted: int = 0
    dropped: int = 0
    demoted: int = 0
    outcomes: list[DirectionOutcomeModel]

    @classmethod
    def from_report(cls, report: IdeateReport) -> IdeateResult:
        return cls(
            ok=report.ok,
            batch_id=report.batch_id,
            direction_count=len(report.outcomes),
            inserted=report.inserted,
            dropped=report.dropped,
            demoted=report.demoted,
            outcomes=[DirectionOutcomeModel.from_outcome(item) for item in report.outcomes],
        )


# ══════════════════════════════════════════════════════════════════════
# 写 · 勾选入队与人工加选题
# ══════════════════════════════════════════════════════════════════════


class DraftItem(BaseModel):
    """ "入队并立刻写稿"里的一次写稿结果（只在 ``draft_now=true`` 时出现）。"""

    ok: bool
    script_id: str | None = None
    version: int = 0
    title: str = ""
    sentence_count: int = 0
    word_count: int = 0
    est_duration_ms: int = 0
    warnings: list[str]
    error_code: str | None = None
    error_message: str | None = None


class SelectBody(_Body):
    """勾选入队（原文 §7.2"勾选入队"）。

    ``draft_now`` 默认 **false**：入队是"我挑出来了"，跑写稿是另一件事
    （draft 池认领 ⇒ T4.11）。勾上它表示"别等池了，现在就写" —— 那条路径
    与 CLI 的 ``studio script draft`` 等价，只是入口不同（T4.3 裁定 132）。
    """

    topic_ids: list[str] = Field(min_length=1, max_length=200, description="要入队的选题")
    draft_now: bool = Field(default=False, description="入队后立刻跑写稿（长任务）")


class SelectFailure(BaseModel):
    """入队失败的那一条（**不**因为一条失败就回滚其余的）。"""

    topic_id: str
    code: str
    message: str
    remediation: str | None = None


class SelectItem(BaseModel):
    """入队成功的一条（``draft`` 非空 ⇒ 顺手把稿子也写了）。"""

    topic_id: str
    task_id: str
    title: str
    created_task: bool
    task_status: str
    topic_status: str
    selected_by: str
    draft: DraftItem | None = None


class SelectResult(BaseModel):
    """勾选入队的逐条结果。"""

    requested: int
    selected: list[SelectItem]
    failed: list[SelectFailure]
    drafted: int = 0
    draft_failed: int = 0


class ManualTopicBody(_Body):
    """人工加选题（原文 §7.2"人工加选题"）。"""

    title: str = Field(min_length=1, max_length=50, description="选题标题")
    angle: str = Field(default="", max_length=120, description="角度差异化说明")
    hook_type: Literal["conflict", "suspense", "contrast", "number", "other"] | None = None
    score: float | None = Field(default=None, ge=0, le=10, description="自评分（可留空）")
    reason: str | None = Field(default=None, max_length=200, description="评分理由")


class ManualTopicResult(BaseModel):
    """人工加选题的结果（``similar_to`` 非空 ⇒ 库里有很像的，**但仍已入库**）。"""

    topic_id: str
    direction_id: str
    title: str
    hook_type: str | None = None
    score: float | None = None
    similar_to: list[dict[str, Any]]
    warnings: list[str]

    @classmethod
    def from_outcome(cls, outcome: ManualTopicOutcome) -> ManualTopicResult:
        return cls(**outcome.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 输入源 · 热点导入
# ══════════════════════════════════════════════════════════════════════


class HotSubmitBody(_Body):
    """网页端直接粘一批热点（M1 门禁的"网页端输入热点"）。

    ``kind='hot'`` 走 ``标题|热度|平台`` 解析；``kind='feedback'`` 走
    §04.1.7 的双形态兼容解析（自由文本也能兜住）。
    """

    text: str = Field(min_length=1, description="每行一条；热点形如 `标题|热度|平台`")
    kind: Literal["hot", "feedback"] = "hot"


class HotImportBody(_Body):
    """扫盘导入（``data/hot/*.md`` + ``data/feedback/*.md``）。"""

    hot: bool = True
    feedback: bool = True


class ImportResult(BaseModel):
    """一次导入的结果（``issues`` 是**人话**，不是 ``ParseIssue`` 对象）。"""

    kind: str
    files: list[str]
    parsed: int = 0
    bad: int = 0
    inserted: int = 0
    skipped: int = 0
    issues: list[str]

    @classmethod
    def from_report(cls, report: ImportReport) -> ImportResult:
        return cls(
            kind=report.kind,
            files=list(report.files),
            parsed=report.parsed,
            bad=report.bad,
            inserted=report.inserted,
            skipped=report.skipped,
            issues=list(report.warnings),
        )


class HotImportResult(BaseModel):
    """热点 / 反馈导入的结果（没跑的那一类为 ``None``）。"""

    hot: ImportResult | None = None
    feedback: ImportResult | None = None
