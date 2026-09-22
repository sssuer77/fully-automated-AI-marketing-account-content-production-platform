"""选题面板 REST 面（T4.3 · §04.1.3 / §04.4.5 第 2 行）。

这一层的三个职责
----------------
① 把 HTTP 请求翻成服务层调用（``TopicService`` / ``ScriptService``）；
② 把服务层的结果打包成响应（含**逐条**成败，不折叠成一句"部分失败"）；
③ 让 ``StudioError`` 自己冒到应用级 handler（``app/errors.py``）去 —— 业务规则
   （去重、状态机、留痕）一条都不在这里。

长任务为什么要"单飞守卫"
------------------------
``analyze`` / ``ideate`` 是几十秒的 LLM 流水线。面板上的按钮会被连点、会被两个
标签页同时按、会在超时后重试 —— 每一次都真的跑一遍，就是**多烧一份 token 预算**
外加一批互相矛盾的批次（后跑的那批把前一批的选题淹掉）。所以入口处一把进程级
非阻塞锁：已在跑 ⇒ 409 ``TOPIC_BATCH_RUNNING``（**不是**静默排队 —— 前端会挂住，
用户不知道自己排在第几）。

为什么守卫用 ``threading.Lock`` 而不是 ``asyncio.Lock``
-------------------------------------------------------
同步路由跑在 Starlette 线程池里（每次可能不同线程），而 ``asyncio.Lock`` 与事件
循环绑定；跨"线程池 + 事件循环"两种执行环境共用一把锁，``threading.Lock`` 是唯一
不会在换线程/换循环时炸掉的选法。锁**只**在入口处非阻塞地拿一下，拿不到立刻 409。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState, active_persona, script_service_for, topic_service_for
from studio.app.schemas.topics import (
    MAX_BATCHES,
    MAX_PAGE,
    AnalyzeBody,
    AnalyzeResult,
    DirectionCard,
    DirectionDeleteResult,
    DirectionEditResult,
    DirectionItem,
    DirectionList,
    DirectionPatchBody,
    DraftItem,
    DraftReviewResult,
    HotImportBody,
    HotImportResult,
    HotSubmitBody,
    IdeateBody,
    IdeateResult,
    ImportResult,
    ManualDirectionBody,
    ManualDirectionResult,
    ManualTopicBody,
    ManualTopicResult,
    OutlineItem,
    OutlineResult,
    OutlineSaveBody,
    OutlineView,
    SelectBody,
    SelectFailure,
    SelectItem,
    SelectResult,
    TopicDeleteResult,
    TopicEditResult,
    TopicItem,
    TopicList,
    TopicPatchBody,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.paths import StudioPaths
from studio.db.repositories import DirectionRepo, TopicRepo
from studio.services.input_service import InputService
from studio.services.script_service import DraftReport

__all__ = ["router"]

router = APIRouter(tags=["topics"])

#: ``topic_candidates.status`` 的合法取值（与 DDL 的 CHECK 同源）
_STATUS_PATTERN = "^(candidate|selected|queued|rejected|expired)$"

#: 网页端粘贴进来的输入源文件名前缀（**服务端生成**，不接受客户端命名）
_WEB_SUBMIT_PREFIX = "webui"


class _RunGuard:
    """进程级"同一时刻只跑一个长任务"的守卫（见模块 docstring）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    @contextmanager
    def hold(self, label: str) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            running = self._holder or "另一个长任务"
            raise StudioError(
                f"{running}正在运行，请等它跑完",
                code=ErrorCode.TOPIC_BATCH_RUNNING,
                context={"running": running, "requested": label},
                remediation="进度见「实时日志」面板；跑完再点一次，不要重复提交",
            )
        self._holder = label
        try:
            yield
        finally:
            self._holder = None
            self._lock.release()


_RUN_GUARD = _RunGuard()


# ══════════════════════════════════════════════════════════════════════
# 读 · 选题池与方向
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/topics", response_model=TopicList)
def list_topics(
    request: Request,
    status: Annotated[str, Query(pattern=_STATUS_PATTERN, description="默认只看候选")] = "candidate",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
) -> TopicList:
    """选题池瀑布流（``ORDER BY score DESC, seq`` ⇒ 高分在前）。"""
    state: AppState = request.app.state.studio
    repo = TopicRepo(state.connections.get())
    return TopicList(
        status=status,
        topics=[TopicItem.from_row(row) for row in repo.list_pool(status=status, limit=limit)],
        counts=repo.count_by_status(),
        limit=limit,
    )


@router.get("/api/v1/topics/directions", response_model=DirectionList)
def list_directions(
    request: Request,
    batch_id: Annotated[str | None, Query(description="批次 id（缺省=最近一批）")] = None,
) -> DirectionList:
    """方向卡片 + 可切换的历史批次 + 每方向的选题计数。"""
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    directions = DirectionRepo(connection)
    topics = TopicRepo(connection)
    batches = directions.list_batches(limit=MAX_BATCHES)
    resolved = batch_id or (batches[0] if batches else None)
    rows = directions.list_batch(resolved) if resolved else []
    grouped = topics.count_by_direction()
    return DirectionList(
        batch_id=resolved,
        batches=batches,
        directions=[
            DirectionItem(
                **DirectionCard.from_row(row).model_dump(),
                topic_count=sum(grouped.get(row.id, {}).values()),
                selected_count=grouped.get(row.id, {}).get("queued", 0),
            )
            for row in rows
        ],
        counts=topics.count_by_status(),
    )


# ══════════════════════════════════════════════════════════════════════
# 写 · 人工写方向 / 改方向 / 删方向
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/topics/directions", response_model=ManualDirectionResult)
def add_manual_direction(request: Request, body: ManualDirectionBody) -> ManualDirectionResult:
    """人工写一个方向（**不经模型、不烧 token**）。缺省落进最近一批。"""
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).add_manual_direction(
        title=body.title,
        rationale=body.rationale,
        priority=body.priority,
        batch_id=body.batch_id,
    )
    return ManualDirectionResult.from_outcome(outcome)


@router.patch("/api/v1/topics/directions/{direction_id}", response_model=DirectionEditResult)
def patch_direction(request: Request, direction_id: str, body: DirectionPatchBody) -> DirectionEditResult:
    """改一个方向（**只改显式给过的字段**；空 PATCH 在契约层就拦掉了）。"""
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).update_direction(direction_id=direction_id, changes=body.changes())
    return DirectionEditResult.from_outcome(outcome)


@router.delete("/api/v1/topics/directions/{direction_id}", response_model=DirectionDeleteResult)
def delete_direction(request: Request, direction_id: str) -> DirectionDeleteResult:
    """删一个方向，**它下面的候选一起走**（级联）。

    唯一拦下的情形：那个方向下已经有候选派生了任务 —— 那种候选被级联删掉之后，
    它那条任务就再也写不出稿（不是门禁，是断链）。响应里带上 ``cascaded_topics``
    让人看得见这一下删掉了多少条。
    """
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).delete_direction(direction_id=direction_id)
    return DirectionDeleteResult.from_outcome(outcome)


# ══════════════════════════════════════════════════════════════════════
# 触发 · 分析与生成（长任务 · 单飞）
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/topics/analyze", response_model=AnalyzeResult)
async def analyze_topics(request: Request, body: AnalyzeBody | None = None) -> AnalyzeResult:
    """导入输入源 ⇒ 5–8 个内容方向（Planner · §04.1.2）。**长任务**。"""
    state: AppState = request.app.state.studio
    payload = body or AnalyzeBody()
    service = topic_service_for(state)
    with _RUN_GUARD.hold("方向分析"):
        report = await service.run_planner(
            persona=active_persona(),
            batch_id=payload.batch_id,
            import_sources=payload.import_sources,
        )
    return AnalyzeResult.from_report(report)


@router.post("/api/v1/topics/ideate", response_model=IdeateResult)
async def ideate_topics(request: Request, body: IdeateBody | None = None) -> IdeateResult:
    """逐方向产出 3–5 个选题（Ideator · §04.1.3）。**长任务**、方向之间互不影响。"""
    state: AppState = request.app.state.studio
    payload = body or IdeateBody()
    service = topic_service_for(state)
    with _RUN_GUARD.hold("选题生成"):
        report = await service.run_ideator(
            persona=active_persona(),
            batch_id=payload.batch_id,
            direction_ids=payload.direction_ids,
            per_direction=payload.per_direction,
        )
    return IdeateResult.from_report(report)


# ══════════════════════════════════════════════════════════════════════
# 写 · 勾选入队 / 人工加选题
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/topics/select", response_model=SelectResult)
async def select_topics(request: Request, body: SelectBody) -> SelectResult:
    """勾选入队：**逐条**建任务（幂等键 ``topic:<id>``），一条失败不影响其余。

    ``draft_now=true`` ⇒ 建完任务顺手跑一次写稿（Director + Writer），与 CLI 的
    ``studio script draft`` 等价。默认 **false**：入队是"我挑出来了"，跑写稿是
    另一件事（draft 池认领 ⇒ T4.11）。
    """
    state: AppState = request.app.state.studio
    persona = active_persona()
    # 勾选入队本身不调 LLM ⇒ 不装 Agent；只有 draft_now 才需要（裁定 133）
    service = script_service_for(state, with_agents=body.draft_now)
    selected: list[SelectItem] = []
    failed: list[SelectFailure] = []
    drafted = 0
    draft_failed = 0

    for topic_id in body.topic_ids:
        try:
            outcome = service.enqueue(topic_id=topic_id, persona=persona, selected_by="user")
        except StudioError as exc:
            failed.append(
                SelectFailure(
                    topic_id=topic_id,
                    code=str(exc.code),
                    message=exc.message,
                    remediation=exc.remediation,
                )
            )
            continue

        item = SelectItem(**outcome.to_dict())
        if body.draft_now:
            report = await service.draft(topic_id=topic_id, persona=persona, actor="user")
            item.draft = _draft_item(report)
            if report.ok:
                drafted += 1
            else:
                draft_failed += 1
        selected.append(item)

    return SelectResult(
        requested=len(body.topic_ids),
        selected=selected,
        failed=failed,
        drafted=drafted,
        draft_failed=draft_failed,
    )


@router.post("/api/v1/topics/manual", response_model=ManualTopicResult)
def add_manual_topic(request: Request, body: ManualTopicBody) -> ManualTopicResult:
    """人工加选题：**直接入库** + ``audit_ops``（相似只提示、不拦）。"""
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).add_manual_topic(
        title=body.title,
        angle=body.angle,
        hook_type=body.hook_type,
        score=body.score,
        reason=body.reason,
    )
    return ManualTopicResult.from_outcome(outcome)


@router.patch("/api/v1/topics/{topic_id}", response_model=TopicEditResult)
def patch_topic(request: Request, topic_id: str, body: TopicPatchBody) -> TopicEditResult:
    """改一条选题（**只改显式给过的字段**；改标题会重算去重指纹）。

    空 PATCH 在契约层就拦掉了（``TopicPatchBody`` 要求"至少给一个字段"）；而
    "给了但跟原来一样"由服务层判 —— 那种情况回当前行、``changed`` 为空、不留痕，
    不是错误（与 ``assets`` 的 PATCH 同一取舍）。
    """
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).update_topic(topic_id=topic_id, changes=body.changes())
    return TopicEditResult.from_outcome(outcome)


@router.delete("/api/v1/topics/{topic_id}", response_model=TopicDeleteResult)
def delete_topic(request: Request, topic_id: str) -> TopicDeleteResult:
    """删一条选题（**已经派生过任务的那条不给删** —— 删了那条任务就再也写不出稿）。"""
    state: AppState = request.app.state.studio
    outcome = topic_service_for(state).delete_topic(topic_id=topic_id)
    return TopicDeleteResult.from_outcome(outcome)


# ══════════════════════════════════════════════════════════════════════
# 三级产物 · 生成完整文案并移交审核
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/topics/{topic_id}/draft-review", response_model=DraftReviewResult)
async def draft_topic_for_review(request: Request, topic_id: str) -> DraftReviewResult:
    """选中一条候选 ⇒ 生成完整文案 ⇒ 移交审核（**长任务**：Director + Writer）。

    与 ``select`` 的 ``draft_now`` 差在哪：那一个是"批量勾选，顺手写稿"（结果停在
    ``drafting``，交给写稿池），这一个是**单条候选的下一步**（写完之后推到
    ``reviewing``，写稿池接着跑评分 + 确认闸）。面板上这两个按钮挨着，但语义不同，
    所以不合并。

    共用单飞守卫：它和 ``analyze`` / ``ideate`` / ``outline`` 一样是"点下去等一会儿"
    的长任务，同时跑只会让日志与预算互相打架。
    """
    state: AppState = request.app.state.studio
    service = script_service_for(state)
    with _RUN_GUARD.hold("写稿并送审"):
        outcome = await service.draft_and_review(topic_id=topic_id, persona=active_persona())
    return DraftReviewResult.from_outcome(outcome)


# ══════════════════════════════════════════════════════════════════════
# 二级产物 · 视频标题 + 核心论点
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/topics/{topic_id}/outline", response_model=OutlineView)
def get_topic_outline(request: Request, topic_id: str) -> OutlineView:
    """读一个选题的二级产物（没有 ⇒ ``outline=null``，**不是 404**）。"""
    state: AppState = request.app.state.studio
    row = script_service_for(state, with_agents=False).get_outline(topic_id)
    return OutlineView(topic_id=topic_id, outline=None if row is None else OutlineItem.from_row(row))


@router.post("/api/v1/topics/{topic_id}/outline", response_model=OutlineResult)
async def generate_topic_outline(request: Request, topic_id: str) -> OutlineResult:
    """让模型给这条选题定标题与核心论点（**长任务**：一次 LLM）。

    与 ``analyze`` / ``ideate`` 共用同一把单飞守卫：三者都是「点下去等一会儿」的长任务，
    同时跑只会让日志与预算互相打架。
    """
    state: AppState = request.app.state.studio
    service = script_service_for(state)
    with _RUN_GUARD.hold("标题与论点生成"):
        report = await service.outline(topic_id=topic_id, persona=active_persona())
    return OutlineResult.from_report(report)


@router.put("/api/v1/topics/{topic_id}/outline", response_model=OutlineResult)
def save_topic_outline(request: Request, topic_id: str, body: OutlineSaveBody) -> OutlineResult:
    """手工定稿二级产物（**一次 LLM 都不调**：没配 Key 也能用）。"""
    state: AppState = request.app.state.studio
    report = script_service_for(state, with_agents=False).save_outline(
        topic_id=topic_id, title=body.title, core_argument=body.core_argument
    )
    return OutlineResult.from_report(report)


@router.delete("/api/v1/topics/{topic_id}/outline", response_model=OutlineResult)
def clear_topic_outline(request: Request, topic_id: str) -> OutlineResult:
    """清空二级产物（**幂等**）。清掉之后三级退回「按选题自由发挥」。"""
    state: AppState = request.app.state.studio
    report = script_service_for(state, with_agents=False).clear_outline(topic_id=topic_id)
    return OutlineResult.from_report(report)


# ══════════════════════════════════════════════════════════════════════
# 输入源 · 热点导入
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/hot/import", response_model=HotImportResult)
def import_sources(request: Request, body: HotImportBody | None = None) -> HotImportResult:
    """扫盘导入 ``data/hot/*.md`` 与 ``data/feedback/*.md``（幂等，坏行照入库留痕）。"""
    state: AppState = request.app.state.studio
    payload = body or HotImportBody()
    service = _input_service(state)
    return HotImportResult(
        hot=ImportResult.from_report(service.import_hot()) if payload.hot else None,
        feedback=ImportResult.from_report(service.import_feedback()) if payload.feedback else None,
    )


@router.post("/api/v1/hot/submit", response_model=HotImportResult)
def submit_sources(request: Request, body: HotSubmitBody) -> HotImportResult:
    """网页端直接粘一批输入源 ⇒ 落成 ``data/hot/webui-<ulid>.md`` ⇒ 立刻导入。

    文件名**服务端生成**（``webui-<ulid>.md``），不接受客户端命名：客户端给名字就
    意味着要校验路径穿越、非法字符、覆盖已有文件 —— 而这三件事没有一件对用户有价值
    （他关心的是"这段热点进去了没有"）。
    """
    state: AppState = request.app.state.studio
    paths: StudioPaths = state.paths
    text = body.text if body.text.endswith("\n") else f"{body.text}\n"
    target_dir = paths.hot_dir if body.kind == "hot" else paths.feedback_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{_WEB_SUBMIT_PREFIX}-{new_ulid()}.md").write_text(text, encoding="utf-8", newline="\n")

    service = _input_service(state)
    report = service.import_hot() if body.kind == "hot" else service.import_feedback()
    result = ImportResult.from_report(report)
    return HotImportResult(
        hot=result if body.kind == "hot" else None,
        feedback=result if body.kind == "feedback" else None,
    )


def _input_service(state: AppState) -> InputService:
    """按**当前线程**的连接造一个导入服务（与 ``review_service_for`` 同一手法）。"""
    return InputService(state.connections.get(), paths=state.paths, log=state.logs.append)


def _draft_item(report: DraftReport) -> DraftItem:
    """``DraftReport`` → :class:`DraftItem`（"顺手写稿"那一条的结果）。"""
    payload = report.to_dict()
    return DraftItem(
        ok=report.ok,
        script_id=report.script_id,
        version=int(report.version or 0),
        title=report.title,
        sentence_count=report.sentence_count,
        word_count=report.word_count,
        est_duration_ms=report.est_duration_ms,
        warnings=list(report.warnings),
        error_code=None if payload.get("error_code") is None else str(payload["error_code"]),
        error_message=None if payload.get("error_message") is None else str(payload["error_message"]),
    )
