"""发布操作面 REST（T5.3 · §06.5.4 / §06.10 / §06.12）。

五个端点 = 面板上现在能做的五件事
---------------------------------
投递（``POST /publish/tasks/{id}/enqueue``）、看板（``GET /publish/publications``）、
待人工队列（``GET /publish/queue``，T5.3 验收点名的那一条）、人工处置三连
（``retry`` / ``cancel`` / ``manual-done``）。

**这里没有"点发布"**：真发布是 publish 池 worker 干的，本层只投作业 —— 与"CLI 不
替人点按钮"同一条边界。``publish.enabled=false`` 时投递**照样成功**，作业会在 worker
那一侧带 ``PUBLISH_DISABLED`` **进死信**（R14 的不可逆防护；**不是**转人工 —— 见
``publish_worker._guard_switch`` 与 §04-contracts ① 的开关守卫那条：那一行
``publications`` 根本不会建，所以发布面板上不会出现记录，死信在「四池调度」里看）。
让投递直接报错会得到"面板点不动"，而操作员真正需要看到的是"作业在那儿、它为什么没发"。

为什么"待人工"要一个独立端点
----------------------------
它是**唯一一个要求人做决定的列表**（§06.10）：其余区块是"看"，它是"办"。独立成一个
端点之后，面板可以只轮询它、只给它做红点计数，而不必每次都拉全量记录。

为什么处置动作要回一句 ``message``
----------------------------------
三个动作在库里的落点不一样（``queued`` / ``canceled`` / ``canceled``），而"取消一条
排队中的"与"取消一条已经发出去的"是两件完全不同的事。服务端把"到底改了什么"写成
一句话，比让面板自己拼（然后拼错）可靠。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from studio.app.deps import AppState, handoff_config_for, publish_config_for
from studio.app.schemas.publish import (
    NO_PUBLICATION_HINT,
    ComplianceView,
    HandoffPreview,
    HandoffResponse,
    MemorySinkView,
    MetricsTickView,
    PublicationList,
    PublicationView,
    PublishActionRequest,
    PublishActionResponse,
    PublishEnqueueRequest,
    PublishEnqueueResponse,
    PublishPlatformsView,
    compliance_view,
    handoff_preview,
    platforms_view,
    publication_view,
)
from studio.core.errors import ErrorCode, StudioError
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import PublicationRepo, PublicationRow
from studio.publish.compliance import compliance_snapshot
from studio.publish.handoff import build_package, handoff_adapter
from studio.publish.memory import sink_memory
from studio.services.publish_metrics_service import PublishMetricsService
from studio.services.publish_service import (
    build_board,
    cancel_publication,
    default_platforms,
    enqueue_publications,
    mark_manual_done,
    platform_options,
    retry_publication,
)

__all__ = ["router"]

router = APIRouter(tags=["publish"])

#: 发布记录 id 的形状（ULID，Crockford base32）。面板只回传服务端给过的 id。
_PUBLICATION_ID = PathParam(pattern=r"^[0-9A-Za-z]{1,64}$")

#: 任务 id 的形状 —— 与渲染面板同一条口径（任务号是**调用方起的名**，
#: ``ui-20260915-120000`` 这类必须能进得来，见 ``routers/voice.py`` 的注释）。
_TASK_ID = PathParam(pattern=r"^[0-9A-Za-z_-]{1,64}$")


@router.get("/api/v1/publish/publications", response_model=PublicationList)
def list_publications(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200, description="每个状态最多几条")] = 50,
    task_id: Annotated[str | None, Query(description="只看这条任务")] = None,
) -> PublicationList:
    """发布看板（六状态各若干条 + 全量计数）。

    带上 ``task_id`` ⇒ 只回这条任务的记录，``counts`` 也**只算这条任务的**：
    任务详情页里显示"这条片子发了 1 条、失败 2 次"，而全站计数放在那里会让人以为
    整个系统只有这几条。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    if task_id:
        rows = PublicationRepo(connection).list_for_task(task_id)
        return PublicationList(
            counts=_tally(rows),
            by_status={"all": [publication_view(row) for row in rows[:limit]]},
            manual_required=[publication_view(row) for row in rows if row.needs_human],
            hint=None if rows else NO_PUBLICATION_HINT,
        )

    board = build_board(connection, limit=limit)
    manual = [publication_view(row) for row in board.rows("manual_required")]
    return PublicationList(
        counts=board.counts,
        by_status={
            status: [publication_view(row) for row in rows] for status, rows in board.by_status.items()
        },
        manual_required=manual,
        hint=None if sum(board.counts.values()) else NO_PUBLICATION_HINT,
    )


@router.post("/api/v1/publish/metrics/tick", response_model=MetricsTickView)
async def run_metrics_tick(request: Request) -> MetricsTickView:
    """**立刻**跑一轮数据回收（T5.4 · §06.6）。

    后台本来每 60s 自己拍一次（``app/recycle.py``）；这个端点是给人按的 ——
    "我刚发完，想现在看一眼数据"，或者"上一轮看着没动静，手动催一下"。

    失败**不抛**：一轮里某一条采不到是常态（登录态掉了、平台还没出数），
    它已经体现在返回的 ``deferred`` / ``stopped`` 里。让整个请求 500 会得到
    "点一下就报错"，而操作员真正需要看到的是"哪几条没采到"。
    """
    state: AppState = request.app.state.studio
    service = PublishMetricsService(
        connection=state.connections.get(),
        paths=state.paths,
        config=publish_config_for(state),
        log=state.logs.append,
    )
    report = await service.tick()
    return MetricsTickView(
        collected=list(report.collected),
        deferred=list(report.deferred),
        stopped=list(report.stopped),
        yielded=report.yielded,
        schedule_hours=[int(hour) for hour in publish_config_for(state).metrics_schedule_hours],
        pending=len(service.due()),
    )


@router.post("/api/v1/publish/publications/{publication_id}/collect", response_model=PublicationView)
async def collect_one(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
) -> PublicationView:
    """采**这一条**（不看 ``next_metric_at``，人按的就是"现在采"）。"""
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    config = publish_config_for(state)
    service = PublishMetricsService(
        connection=connection, paths=state.paths, config=config, log=state.logs.append
    )
    row = PublicationRepo(connection).get(publication_id)
    if row is None:
        raise StudioError(
            f"发布记录不存在：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
        )
    await service.collect_one(row)
    fresh = PublicationRepo(connection).get(publication_id)
    return publication_view(fresh if fresh is not None else row)


@router.post("/api/v1/publish/publications/{publication_id}/sink", response_model=MemorySinkView)
async def sink_one(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
) -> MemorySinkView:
    """把这一条的数据**沉淀成记忆**（T5.4 · §06.8：回流 feedback + 汇总 + 降权）。

    与 ``collect`` 分开是 §4.6.2 的两个入口：采数是"读回来"，沉淀是"让下一轮
    选题吃得到"。合并成一个端点之后，"我想再沉淀一次"就得连带再采一次数
    （而那一次采集可能正好撞上平台的限流）。
    """
    state: AppState = request.app.state.studio
    result = await sink_memory(
        publication_id,
        connection=state.connections.get(),
        paths=state.paths,
        config=publish_config_for(state),
    )
    return MemorySinkView(
        feedback_items_created=result.feedback_items_created,
        topics_demoted=result.topics_demoted,
        digest_path=str(result.digest_path),
        planner_consumable=result.planner_consumable,
        comments_seen=result.comments_seen,
        low_engagement=result.low_engagement,
    )


@router.get("/api/v1/publish/queue", response_model=PublicationList)
def manual_queue(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200, description="最多几条")] = 100,
) -> PublicationList:
    """**待人工**队列（§06.10）：自动这条路走完了，等人做决定的那几条。

    每一条都带着 ``error_code`` / ``error_message`` / ``evidence``（截图与 DOM 快照的
    路径）—— 那正是"失败可排查"（R13）要的东西：人要能看着截图决定是重试、是去平台上
    手工发、还是干脆取消。
    """
    state: AppState = request.app.state.studio
    repo = PublicationRepo(state.connections.get())
    rows = repo.manual_queue(limit=limit)
    return PublicationList(
        counts=repo.counts(),
        by_status={"manual_required": [publication_view(row) for row in rows]},
        manual_required=[publication_view(row) for row in rows],
        hint=None if rows else "待人工队列是空的（没有需要你处理的发布）",
    )


@router.get("/api/v1/publish/handoff/{task_id}", response_model=HandoffPreview)
def preview_handoff(
    request: Request,
    task_id: Annotated[str, _TASK_ID],
) -> HandoffPreview:
    """交付包**预览**：会打进去哪几件、缺哪件（**一个字节都不写**）。

    与打包走**同一个** :func:`~studio.publish.handoff.build_package`：分开写的话，
    面板上"七件齐"与真打出来的包里"少两件"会各自成立 —— 那种不一致最难查。
    """
    state: AppState = request.app.state.studio
    config = handoff_config_for(state)
    package = build_package(task_id=task_id, paths=state.paths, connection=state.connections.get())
    return handoff_preview(
        package,
        adapter=config.adapter,
        output_dir=str(state.paths.home / config.output_dir),
        auto=config.enabled,
    )


@router.post("/api/v1/publish/handoff/{task_id}", response_model=HandoffResponse)
def push_handoff(
    request: Request,
    task_id: Annotated[str, _TASK_ID],
    body: PublishActionRequest | None = None,
) -> HandoffResponse:
    """打一个交付包出去（复制到 ``app.yaml → handoff.output_dir`` 下）。

    **不看 ``handoff.enabled``**：那个开关管的是"发布时自动顺手交付一份"，
    而人按下的这一下就是意图本身 —— 拦它只会得到"按钮是坏的"（与 T5.3 裁定 269
    同一条：投递期不看开关，把判断留给真正执行的那一步）。

    请求体复用 :class:`PublishActionRequest`：导出是**人**的动作，留痕里要写清
    "谁、为什么导"，字段与人工处置那三个动作逐字相同（``actor`` / ``actor_ref`` /
    ``reason``），没有第二套形状的必要。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    config = handoff_config_for(state)
    payload = body or PublishActionRequest()
    package = build_package(task_id=task_id, paths=state.paths, connection=connection)
    adapter = handoff_adapter(config.adapter)
    result = adapter.push(package, output_dir=state.paths.home / config.output_dir)

    # 留痕：交付包是**离开我们掌控**的东西（来源登记也跟着它走），所以这一下要记账。
    AuditRepo(connection).record(
        actor=payload.actor,
        actor_ref=payload.actor_ref,
        action="publish.handoff",
        target_type="task",
        target_id=task_id,
        task_id=task_id,
        after={
            "root": result.root.as_posix(),
            "adapter": result.adapter,
            "copied": [entry["kind"] for entry in result.copied],
            "missing": list(result.missing),
            "bytes": result.bytes,
        },
        reason=payload.reason,
        source="webui",
    )
    return HandoffResponse(**result.to_dict())


@router.get("/api/v1/publish/compliance", response_model=ComplianceView)
def compliance(request: Request) -> ComplianceView:
    """R2 来源登记留档（**发布面板与素材库共用这一份**，§06.11）。

    只读：它回答"我现在要发出去的东西，来源登记齐了吗"。缺了**不阻塞发布** ——
    授权范围是人的判断，程序只负责让"缺一份"看得见。
    """
    state: AppState = request.app.state.studio
    snapshot = compliance_snapshot(state.connections.get(), paths=state.paths)
    return compliance_view(snapshot)


@router.get("/api/v1/publish/platforms", response_model=PublishPlatformsView)
def list_platforms(request: Request) -> PublishPlatformsView:
    """投递面板能选哪些平台（T5.10 · **清单来自配置，不是面板自己列的**）。

    面板列一份平台清单 = 把 ``config/publish.yaml`` 抄第二遍：加一个平台要改两处，
    而漏改的那一处表现为"这个平台在面板上不存在" —— 没人会去报这个 bug。

    连"点了会怎样"也一起给（``selectable`` / ``note``）：判据与投递期**同一套**，
    所以不会出现"面板显示点得动、投出去被跳过"。``default_platforms`` 是"一个都不选"
    时后端会投的那几个 —— 面板必须把它显示出来，否则"不选"看起来像"都不发"。
    """
    state: AppState = request.app.state.studio
    config = publish_config_for(state)
    return platforms_view(
        platform_options(config),
        defaults=default_platforms(config),
        publish_enabled=config.enabled,
        dry_run=config.dry_run,
    )


@router.post("/api/v1/publish/tasks/{task_id}/enqueue", response_model=PublishEnqueueResponse)
def enqueue_task(
    request: Request,
    task_id: Annotated[str, _TASK_ID],
    body: PublishEnqueueRequest | None = None,
) -> PublishEnqueueResponse:
    """把这条任务排进发布池（幂等）。

    **不看 ``publish.enabled``**：开关关着的时候投递依然成功，作业会在 worker 那一侧
    带 ``PUBLISH_DISABLED`` **进死信**（不建 ``publications`` 那一行 ⇒ 这一屏上不会出现
    记录，去「四池调度」看死信）。投递期直接拒绝的话，面板上连作业都没有 ——
    而"点了没反应"比"有一条能查的作业"难查得多（见模块注释）。
    """
    state: AppState = request.app.state.studio
    config = publish_config_for(state)
    payload = body or PublishEnqueueRequest()
    report = enqueue_publications(
        connection=state.connections.get(),
        task_id=task_id,
        config=config,
        platforms=payload.platforms,
        account_id=payload.account_id,
        dry_run=payload.dry_run,
        scheduled_at=payload.scheduled_at,
    )
    if report.missing:
        raise StudioError(
            f"任务不存在：{task_id}",
            code=ErrorCode.TASK_NOT_FOUND,
            context={"task_id": task_id},
            remediation="确认任务号；任务被删之后不能再投递发布",
        )
    return PublishEnqueueResponse(**report.to_dict())


@router.post(
    "/api/v1/publish/{publication_id}/retry",
    response_model=PublishActionResponse,
)
def retry(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
    body: PublishActionRequest | None = None,
) -> PublishActionResponse:
    """人工重试：记录回 ``queued`` + 作业重排（两者都做，见服务层注释）。"""
    return _action(
        request,
        publication_id,
        body,
        action="retry",
        run=retry_publication,
        message="已重排：这条会重新走一遍发布流程（计数已归零）",
    )


@router.post(
    "/api/v1/publish/{publication_id}/cancel",
    response_model=PublishActionResponse,
)
def cancel(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
    body: PublishActionRequest | None = None,
) -> PublishActionResponse:
    """人工取消：``canceled`` + 作废还没被认领的作业。"""
    return _action(
        request,
        publication_id,
        body,
        action="cancel",
        run=cancel_publication,
        message="已取消：这条不会再自动发布（已经发出去的取消不了）",
    )


@router.post(
    "/api/v1/publish/{publication_id}/manual-done",
    response_model=PublishActionResponse,
)
def manual_done(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
    body: PublishActionRequest | None = None,
) -> PublishActionResponse:
    """标记已人工处理（**必须写说明**，见服务层注释）。"""
    payload = body or PublishActionRequest()
    if not (payload.reason or "").strip():
        raise StudioError(
            "标记已人工处理必须写一句说明",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
            remediation="在 reason 里写清楚是人工发出去了，还是决定不发（会进 audit_ops）",
        )
    return _action(
        request,
        publication_id,
        payload,
        action="manual_done",
        run=mark_manual_done,
        message="已从待人工队列摘下（留痕里记着你写的说明）",
    )


def _action(
    request: Request,
    publication_id: str,
    body: PublishActionRequest | None,
    *,
    action: str,
    run: Callable[..., PublicationRow],
    message: str,
) -> PublishActionResponse:
    """三个人工处置共用的外壳（差别只在调哪个服务函数 + 回哪句话）。"""
    state: AppState = request.app.state.studio
    payload = body or PublishActionRequest()
    row = run(
        connection=state.connections.get(),
        publication_id=publication_id,
        actor=payload.actor,
        actor_ref=payload.actor_ref,
        reason=payload.reason,
        source="webui",
    )
    return PublishActionResponse(
        publication=publication_view(row),
        action=action,
        message=message,
        job_changed=True,
    )


def _tally(rows: Sequence[PublicationRow]) -> dict[str, int]:
    """按状态计数（任务维度的看板用；全站维度走 ``PublicationRepo.counts``）。"""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts
