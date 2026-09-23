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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from studio.db.models import DirectionRow, TopicOutlineRow, TopicRow
from studio.domain.script import OUTLINE_ARGUMENT_MAX, OUTLINE_TITLE_MAX
from studio.services.input_service import ImportReport
from studio.services.script_service import DraftReviewOutcome, OutlineReport
from studio.services.topic_service import (
    AnalyzeReport,
    ClearTopicsOutcome,
    DirectionDeleteOutcome,
    DirectionEditOutcome,
    DirectionOutcome,
    IdeateReport,
    ManualDirectionOutcome,
    ManualTopicOutcome,
    NewsPullReport,
    TopicDeleteOutcome,
    TopicEditOutcome,
)

__all__ = [
    "AnalyzeBody",
    "AnalyzeResult",
    "ClearTopicsBody",
    "ClearTopicsResult",
    "DirectionCard",
    "DirectionDeleteResult",
    "DirectionEditResult",
    "DirectionItem",
    "DirectionList",
    "DirectionOutcomeModel",
    "DirectionPatchBody",
    "DraftItem",
    "DraftReviewResult",
    "HotImportBody",
    "HotSubmitBody",
    "IdeateBody",
    "IdeateResult",
    "ImportResult",
    "ManualDirectionBody",
    "ManualDirectionResult",
    "ManualTopicBody",
    "ManualTopicResult",
    "OutlineItem",
    "OutlineResult",
    "OutlineSaveBody",
    "OutlineView",
    "SelectBody",
    "SelectFailure",
    "SelectItem",
    "SelectResult",
    "TopicDeleteResult",
    "TopicEditResult",
    "TopicItem",
    "TopicList",
    "TopicPatchBody",
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
# 写 · 人工写方向 / 改方向 / 删方向（左列那一栏）
# ══════════════════════════════════════════════════════════════════════


class ManualDirectionBody(_Body):
    """人工写一个方向（**不经模型**；与「人工加选题」同一手法）。"""

    title: str = Field(min_length=1, max_length=120, description="方向标题")
    rationale: str = Field(default="", max_length=500, description="为什么做这个方向")
    priority: int = Field(default=100, ge=0, le=10_000, description="越小越优先（模型产出的是 100）")
    batch_id: str | None = Field(default=None, description="落进哪个批次（缺省=最近一批）")


class DirectionPatchBody(_Body):
    """改一个方向（**只列你要改的字段**；与 ``TopicPatchBody`` 同一手法）。"""

    title: str | None = Field(default=None, min_length=1, max_length=120, description="方向标题")
    rationale: str | None = Field(default=None, max_length=500, description="为什么做这个方向")
    priority: int | None = Field(default=None, ge=0, le=10_000, description="越小越优先")
    risk_flags: list[str] | None = Field(default=None, max_length=20, description="风险标记")

    @model_validator(mode="after")
    def _require_a_change(self) -> DirectionPatchBody:
        if not self.model_fields_set:
            raise ValueError("至少要给一个要改的字段（title / rationale / priority / risk_flags）")
        return self

    def changes(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields_set}


class ManualDirectionResult(BaseModel):
    """人工写方向的结果（返回**落库之后**那一行）。"""

    direction: DirectionCard

    @classmethod
    def from_outcome(cls, outcome: ManualDirectionOutcome) -> ManualDirectionResult:
        return cls(direction=DirectionCard.from_row(outcome.direction))


class DirectionEditResult(BaseModel):
    """改完之后的那一行 + 改了哪几列（``changed`` 为空 ⇒ 什么都没变）。"""

    direction: DirectionCard
    changed: list[str]

    @classmethod
    def from_outcome(cls, outcome: DirectionEditOutcome) -> DirectionEditResult:
        return cls(direction=DirectionCard.from_row(outcome.direction), changed=list(outcome.changed))


class DirectionDeleteResult(BaseModel):
    """删掉的那个方向 + **被它带走的候选条数**（级联删除要如实报数）。

    ``detached_task_count`` = 被带走的候选里**已经派生过任务**的条数 —— 那些任务不跟着走，
    照跑。不报这一条的话，用户会以为"删了方向 ⇒ 那些活也没了"。
    """

    direction_id: str
    title: str
    deleted: bool
    cascaded_topics: int = 0
    detached_task_count: int = 0

    @classmethod
    def from_outcome(cls, outcome: DirectionDeleteOutcome) -> DirectionDeleteResult:
        return cls(**outcome.to_dict())


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
    per_direction: int = Field(default=4, ge=1, le=20, description="每方向目标选题数")


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


class NewsSkipItem(BaseModel):
    """一条没被留下的新闻（``reason`` 是给人看的一句话）。"""

    title: str
    reason: str


class NewsPullResult(BaseModel):
    """一次「拉今日新闻 ⇒ 评测 ⇒ 留方向」的结果（``ok=False`` 也返回 200 —— 原因在体内）。

    ``ok`` 说的是**评测这一步跑通没有**，不是"留下几条"：跑了但一条都没挑中（``ok=True``
    + ``kept=[]``）与压根没跑起来（``ok=False`` + ``error_code``）是两件事 —— 前者不需要
    用户做任何事，后者要他去配通道。
    """

    ok: bool
    source: str = ""
    fetched: int = 0
    evaluated: int = 0
    batch_id: str | None = None
    kept_count: int = 0
    kept: list[DirectionCard]
    skipped: list[NewsSkipItem]
    warnings: list[str]
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_report(cls, report: NewsPullReport) -> NewsPullResult:
        """``NewsPullReport`` → 响应模型（卡片直接由**落库那一行**造，不经中间快照）。"""
        return cls(
            ok=report.ok,
            source=report.source,
            fetched=report.fetched,
            evaluated=report.evaluated,
            batch_id=report.batch_id,
            kept_count=report.kept_count,
            kept=[DirectionCard.from_row(row) for row in report.kept],
            skipped=[NewsSkipItem(title=item.title, reason=item.reason) for item in report.skipped],
            warnings=list(report.warnings),
            error_code=report.error_code,
            error_message=report.error_message,
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


class DraftReviewResult(BaseModel):
    """「生成完整文案并移交审核」的结果（面板上选中一条候选的那一下）。

    ``reused=true`` 表示库里**已经有生效稿件**，这一次一个 token 都没烧 ——
    面板据此把提示语从"已生成"改成"已有稿件，直接送审"，而不是假装又写了一遍。
    """

    ok: bool
    topic_id: str
    task_id: str
    script_id: str | None = None
    title: str = ""
    sentence_count: int = 0
    word_count: int = 0
    #: 交接完成时任务停在哪（``reviewing`` ⇒ 写稿池接着跑审稿 + 确认闸）
    task_status: str = ""
    reused: bool = False
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_outcome(cls, outcome: DraftReviewOutcome) -> DraftReviewResult:
        return cls.model_validate(outcome.to_dict())


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
# 写 · 改选题 / 删选题
# ══════════════════════════════════════════════════════════════════════


class TopicPatchBody(_Body):
    """改一条选题（**只列你要改的字段**；显式给 ``null`` = 把那一列清空）。

    为什么用 ``model_fields_set`` 判「有没有给」而不是判 ``is not None``：
    ``hook_type=null`` 与「根本没提 hook_type」是两件事 —— 前者是「把钩子标签清掉」，
    后者是「别动它」。Pydantic 已经把这件事记下来了，再引入一个 ``UNSET`` 哨兵只是
    把同一份信息写第二遍。
    """

    title: str | None = Field(default=None, min_length=1, max_length=50, description="选题标题")
    angle: str | None = Field(default=None, max_length=120, description="角度差异化说明")
    hook_type: Literal["conflict", "suspense", "contrast", "number", "other"] | None = None
    score: float | None = Field(default=None, ge=0, le=10, description="自评分（``null`` = 不打分）")
    reason: str | None = Field(default=None, max_length=200, description="评分理由")

    @model_validator(mode="after")
    def _require_a_change(self) -> TopicPatchBody:
        if not self.model_fields_set:
            raise ValueError("至少要给一个要改的字段（title / angle / hook_type / score / reason）")
        return self

    def changes(self) -> dict[str, Any]:
        """只把**显式给过**的字段翻成 ``{列名: 新值}``（没提的字段不进字典）。"""
        return {name: getattr(self, name) for name in self.model_fields_set}


class TopicEditResult(BaseModel):
    """改完之后的那一行 + 改了哪几列 + 去重提示（``changed`` 为空 ⇒ 什么都没变）。"""

    topic: TopicItem
    changed: list[str]
    warnings: list[str]

    @classmethod
    def from_outcome(cls, outcome: TopicEditOutcome) -> TopicEditResult:
        return cls(
            topic=TopicItem.from_row(outcome.topic),
            changed=list(outcome.changed),
            warnings=list(outcome.warnings),
        )


class TopicDeleteResult(BaseModel):
    """删掉的那一条（``deleted=False`` = 服务层到这一步时它已经不在了）。

    ``detached_task_id`` = 这条选题派生过的那条任务号 —— **它不跟着走**。删掉的是想法，
    不是活：那条任务自己带着标题 / 角度 / 钩子，照跑。
    """

    topic_id: str
    title: str
    deleted: bool
    detached_task_id: str | None = None

    @classmethod
    def from_outcome(cls, outcome: TopicDeleteOutcome) -> TopicDeleteResult:
        return cls(**outcome.to_dict())


class ClearTopicsBody(_Body):
    """清空选题面板的请求体（**两级**：先预览，再真删）。

    ``dry_run`` 默认 ``True``：调用方**必须显式**说"我知道会删掉多少，删吧"。
    默认成 ``False`` 的话，一个漏传请求体的客户端（``POST`` 不带 body）就会把
    整个面板清空 —— 而这条链路上"漏传"是常态，不是例外。
    """

    dry_run: bool = Field(default=True, description="true ⇒ 只报会删掉多少，一个字节都不写")


class ClearTopicsResult(BaseModel):
    """一次「清除所有选题」的结果。

    ``dry_run=True`` ⇒ 这是**预览**：两个数说"会删掉多少"，库里一个字节没动。
    ``dry_run=False`` ⇒ 真删了，两个数说"实际删掉多少"（走的是同一个计数路径，
    所以预览与真删报的是同一件事）。

    ``detached_task_count`` = 被清掉的选题里**已经派生过任务**的条数 —— 那些任务不
    跟着走，照跑。不报这一条的话，用户会以为"清了选题 ⇒ 那些活也没了"。
    """

    dry_run: bool
    directions: int = 0
    topics: int = 0
    detached_task_count: int = 0

    @classmethod
    def from_outcome(cls, outcome: ClearTopicsOutcome) -> ClearTopicsResult:
        return cls(**outcome.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 二级产物 · 视频标题 + 核心论点（文案三级流水线的中间一级）
# ══════════════════════════════════════════════════════════════════════


class OutlineItem(BaseModel):
    """二级产物（``topic_outlines`` 的展示字段）。"""

    topic_id: str
    title: str
    core_argument: str
    llm_model: str | None = None
    prompt_version: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: TopicOutlineRow) -> OutlineItem:
        return cls(
            topic_id=row.topic_id,
            title=row.title,
            core_argument=row.core_argument,
            llm_model=row.llm_model,
            prompt_version=row.prompt_version,
            updated_at=row.updated_at,
        )


class OutlineView(BaseModel):
    """一个选题的二级产物（``outline=null`` = 还没定 —— **不是错误**）。

    为什么读接口不 404
    ------------------
    「这一级还没定」是**正常状态**（三级会照旧自由发挥）。用 404 表达它，前端就得把一次
    注定失败的网络往返当成流程的一部分 —— 而那不是错误，只是「还没做」。
    """

    topic_id: str
    outline: OutlineItem | None = None


class OutlineResult(BaseModel):
    """一次二级产物的读写结果（生成 / 定稿 / 清空都返回它）。"""

    ok: bool
    topic_id: str
    title: str = ""
    core_argument: str = ""
    llm_model: str | None = None
    prompt_version: str | None = None
    generated: bool = False
    changed: list[str]
    warnings: list[str]
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_report(cls, report: OutlineReport) -> OutlineResult:
        return cls(**report.to_dict())


class OutlineSaveBody(_Body):
    """手工定稿二级产物（**两个字段一起给**：这一级只有这两样东西）。"""

    title: str = Field(min_length=1, max_length=OUTLINE_TITLE_MAX, description="视频标题")
    core_argument: str = Field(
        min_length=1, max_length=OUTLINE_ARGUMENT_MAX, description="核心论点（一句话）"
    )


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
