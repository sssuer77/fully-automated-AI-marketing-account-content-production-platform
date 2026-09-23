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

import mimetypes
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from studio.app.deps import (
    AppState,
    handoff_config_for,
    publish_accounts_service_for,
    publish_assist_service_for,
    publish_config_for,
    publish_cover_service_for,
)
from studio.app.schemas.common import clean_reason
from studio.app.schemas.publish import (
    NO_PUBLICATION_HINT,
    ComplianceView,
    HandoffPreview,
    HandoffResponse,
    MemorySinkView,
    MetricsTickView,
    PublicationList,
    PublicationView,
    PublishAccountHealthOutcome,
    PublishAccountOutcome,
    PublishAccountRequest,
    PublishAccountsView,
    PublishActionRequest,
    PublishActionResponse,
    PublishAssistOutcome,
    PublishCoverOutcome,
    PublishCoverRequest,
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
from studio.publish.base import LOGIN_TIMEOUT_SEC
from studio.publish.compliance import compliance_snapshot
from studio.publish.handoff import build_package, handoff_adapter
from studio.publish.memory import sink_memory
from studio.services.publish_metrics_service import PublishMetricsService
from studio.services.publish_service import (
    CoverRequest,
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

#: 封面文件名（与 `render.py::_VIDEO_NAME` 同一条）：**只允许**"一个不含路径分隔符的
#: 文件名"。``../../`` 这类东西在正则这一步就被拒了，而不是靠 ``resolve()`` 之后的
#: 比较兜底 —— 兜底那一步只要有人少写一次，就是一个任意文件读。
_COVER_NAME = re.compile(r"^[^/\\:*?\"<>|]+\.jpg$", re.IGNORECASE)

#: 发布记录 id 的形状（ULID，Crockford base32）。面板只回传服务端给过的 id。
_PUBLICATION_ID = PathParam(pattern=r"^[0-9A-Za-z]{1,64}$")

#: 任务 id 的形状 —— 与渲染面板同一条口径（任务号是**调用方起的名**，
#: ``ui-20260915-120000`` 这类必须能进得来，见 ``routers/voice.py`` 的注释）。
_TASK_ID = PathParam(pattern=r"^[0-9A-Za-z_-]{1,64}$")

#: 账号 id 的形状。比任务号**松**：它是 ``config/publish.yaml`` 里的自由文本
#: （配置模型的约束只有"1–64 字符"），面板要能改任何一个既有的账号 ——
#: 这里收紧成一个更窄的字符集，等于让某些账号在面板上"看得见、改不了"。
#: 只挡掉斜杠：它会让路径段变成两段。
_ACCOUNT_ID = PathParam(pattern=r"^[^/]{1,64}$")


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


# ── 账号区块（T6.4）：面板上直接加号 / 改号 / 停用 / 删号 ──────────────
#
# 为什么这一块是 **PUT + DELETE** 而不是 POST + "整份清单提交"
# ----------------------------------------------------------
# ① ``PUT /accounts/{id}`` 是**幂等替换**：同一个账号存两次，结果一样、第二次连盘都不碰
#    （``changed=false``）。POST 会让人以为"每点一次就多一个号"，而多出来的那个
#    还要人去删。
# ② 删号是独立动作，且**不删登录态**：``data/browser_profile/<id>/`` 是凭据，
#    面板一个字节都不碰（§02.5）。把它藏在"提交整份清单"里，用户根本看不出
#    自己刚做的事有没有连带删掉什么。
#
# 为什么与 ``/publish/platforms`` 分开
# ------------------------------------
# 那一个是"投哪几个平台"（投递用），这里是"我有哪些号、它们现在什么状态"（配置用）。
# 两块各自刷新：改完账号立刻重画这一块，不必等 2 秒一次的看板轮询。


@router.get("/api/v1/publish/accounts", response_model=PublishAccountsView)
def list_accounts(request: Request) -> PublishAccountsView:
    """账号区块首屏：配置里的账号（含停用的）+ 平台清单 + 表单上下限 + 必须说的话。

    **含停用的账号**：停用不是删除（登录态还在、历史发布记录还挂在它名下）。
    藏起来的话，用户会以为"这个号已经没了"，然后去重新加一个同 id 的号。
    """
    state: AppState = request.app.state.studio
    return PublishAccountsView.model_validate(publish_accounts_service_for(state).read())


@router.put("/api/v1/publish/accounts/{account_id}", response_model=PublishAccountOutcome)
def save_account(
    request: Request,
    account_id: Annotated[str, _ACCOUNT_ID],
    body: PublishAccountRequest,
) -> PublishAccountOutcome:
    """新增或改写一个账号（**先校验、后落盘**；没变就不写盘、不留痕）。

    ``account_id`` 只从路径来 —— 请求体里没有这个字段（身份只有一个来源）。
    表单不合法 ⇒ **422** + ``context.errors``（面板把红字标到对应输入框上）；
    与别的账号冲突（``profile_dir`` 重复 / 平台没定义）⇒ **400**：
    那是"这份配置不能变成那样"，不是"你这个框填错了"。

    写盘**按行改写** ``config/publish.yaml``：段外的 ``platforms`` / ``precheck`` /
    注释一个字节都不碰，段内的行尾注释原样保留（见 ``core.config.write_publish_accounts``）。
    """
    state: AppState = request.app.state.studio
    payload = publish_accounts_service_for(state).save(
        account_id,
        body.model_dump(exclude={"reason"}),
        reason=clean_reason(body.reason),
    )
    return PublishAccountOutcome.model_validate(payload)


@router.delete("/api/v1/publish/accounts/{account_id}", response_model=PublishAccountOutcome)
def remove_account(
    request: Request,
    account_id: Annotated[str, _ACCOUNT_ID],
    reason: Annotated[str | None, Query(max_length=500, description="说明（写进留痕）")] = None,
) -> PublishAccountOutcome:
    """从配置里删掉一个账号（**登录态目录不碰**）。

    配置里已经没有这个 id ⇒ **404**（多半是另一个人刚删掉了它）：请求本身没错，
    错的是"你手上这一屏过时了"，面板据此提示刷新，而不是让用户对着一个
    "改不动"的按钮猜原因。

    ``reason`` 走查询串而不是请求体：DELETE 带 body 在代理链路上会被静默丢掉，
    而"这条为什么删了"是三个月后唯一能回答问题的东西。
    """
    state: AppState = request.app.state.studio
    payload = publish_accounts_service_for(state).remove(account_id, reason=clean_reason(reason))
    return PublishAccountOutcome.model_validate(payload)


@router.post(
    "/api/v1/publish/accounts/{account_id}/probe",
    response_model=PublishAccountHealthOutcome,
)
async def probe_account(
    request: Request,
    account_id: Annotated[str, _ACCOUNT_ID],
) -> PublishAccountHealthOutcome:
    """探一眼这个账号的登录态（T6.4 · **无头、几秒钟、不动任何东西**）。

    为什么值得单独一个端点：面板上那一行 ``note`` 说的是"登录过（会话是否仍有效，
    要真发一次才知道）"—— 而这句话对"我现在就想知道能不能发"是不够的。探测把
    那个不确定性收窄成一个当场可得的答案，且**不需要真发一条**（R14 不可逆）。

    探不出来（浏览器起不来 / 平台打不开）⇒ 200 + ``ready=false`` + ``hint``：
    这不是请求失败，是"这次没探到"，面板要把它显示成一句提示而不是一个红叉。
    """
    state: AppState = request.app.state.studio
    payload = await publish_accounts_service_for(state).probe(account_id)
    return PublishAccountHealthOutcome.model_validate(payload)


@router.post(
    "/api/v1/publish/accounts/{account_id}/login",
    response_model=PublishAccountHealthOutcome,
)
async def login_account(
    request: Request,
    account_id: Annotated[str, _ACCOUNT_ID],
    timeout_sec: Annotated[
        float, Query(ge=30, le=600, description="等扫码的上限（秒），默认 180")
    ] = LOGIN_TIMEOUT_SEC,
) -> PublishAccountHealthOutcome:
    """开一个**可见的浏览器窗口**等人扫码（T6.4 · R13：登录只发生在人扫码那一下）。

    窗口出现在**跑着 studio 服务的那台电脑**上 —— 浏览器是它起的，不是手机上会跳出
    什么。人用手机 App 扫窗口里那个码，扫完窗口自己关掉，这里返回探测结论。

    为什么这一个可以跑几分钟而不用后台作业
    ------------------------------------
    它**必须**是人按着看的：窗口关了这件事没有任何地方能替用户确认。所以前端
    按"长动作"处理（按钮转圈 + 文字说明），而不是把它藏进一个看不见的队列 ——
    藏进去之后，"我扫完了没反应"就成了一句没人能回答的话。

    等不到扫码**不是错误**（200 + ``ready=false`` + ``note`` 里写明下一步）；
    真出错（浏览器起不来 / 平台不支持）才抛。
    """
    state: AppState = request.app.state.studio
    payload = await publish_accounts_service_for(state).login(account_id, timeout_sec=timeout_sec)
    return PublishAccountHealthOutcome.model_validate(payload)


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
        account_ids=payload.account_ids,
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


@router.get("/api/v1/publish/covers/{name}")
def get_cover(request: Request, name: str) -> FileResponse:
    """看一张封面（``<img>`` 直接取这个 url）。

    为什么给面板一个能显示的路由、而不是只回一句路径：封面是**给人看的那一格** ——
    "生成了"与"这张能不能用"是两件事，而后者只有眼睛能判。只回路径的话，用户要
    自己去文件管理器里翻 ``data/output/covers/``，而那里堆着这一期的每一张。
    """
    state: AppState = request.app.state.studio
    if not _COVER_NAME.match(name):
        raise StudioError(
            f"不是合法的封面文件名：{name}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"name": name},
            remediation="封面名形如 20260923-101200_<task_id>_cover.jpg（见出封面的回执）",
        )
    path: Path = state.paths.covers_dir / name
    if not path.is_file():
        raise StudioError(
            f"封面不在盘上：{name}",
            code=ErrorCode.PATH_MISSING,
            context={"name": name, "dir": state.paths.covers_dir.as_posix()},
            remediation="重新出一张；文件可能已被媒资回收（studio gc run）清掉",
        )
    guessed, _ = mimetypes.guess_type(name)
    return FileResponse(path, media_type=guessed or "image/jpeg")


@router.post("/api/v1/publish/tasks/{task_id}/cover", response_model=PublishCoverOutcome)
async def make_cover(
    request: Request,
    task_id: Annotated[str, _TASK_ID],
    body: PublishCoverRequest | None = None,
) -> PublishCoverOutcome:
    """给这条任务出一张封面（T5.1 追加 · §06.3）。

    封面 = **成片里抽的一帧** + **合成配置里的贴图**（``stickers`` 里挑一层，居中）
    + **标题**（黄字黑边，样式来自 ``config/outputs.yaml`` 的 ``cover`` 一节）。
    落 ``data/output/covers/``，同时记一条 ``artifacts(kind='cover')`` 与
    ``tasks.context_json.cover_path`` —— 发布器读的就是后者，所以"出过封面"这件事
    在**发布**那一侧立刻生效（§06.3：有封面就带，没有就用平台首帧）。

    为什么 ``ok=False`` 也回 200
    ---------------------------
    封面是**可选装饰**：抽帧失败 ⇒ 退纯色底；连纯色底都失败 ⇒ 无封面发布，这是契约
    写明的合法结局。把它做成 4xx 的话，面板会把一次"正常的降级"画成一条红色故障，
    而用户唯一该做的事（去看出片）与真故障时完全一样。真正的用法错误（任务号不存在）
    才抛 —— 那一条在服务层。

    为什么这条**不**进后台作业
    --------------------------
    与「人工过验证」相反：它不需要人看着，但它要的输入（稿件 / 时间轴 / 成片 / 配置）
    全都在请求这一刻是齐的，跑完就落盘。丢进队列只会让"点了没反应"多一个去处。
    """
    state: AppState = request.app.state.studio
    payload = body or PublishCoverRequest()
    report = await publish_cover_service_for(state).make_cover(
        CoverRequest(task_id=task_id, use_agent=payload.use_agent)
    )
    return PublishCoverOutcome.model_validate(report.to_dict())


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


@router.post(
    "/api/v1/publish/{publication_id}/assist",
    response_model=PublishAssistOutcome,
)
async def assist(
    request: Request,
    publication_id: Annotated[str, _PUBLICATION_ID],
    body: PublishActionRequest | None = None,
) -> PublishAssistOutcome:
    """**人工过验证**：开一个可见窗口，把这条重新发一遍，途中停下来等人输验证码。

    什么时候用它
    ------------
    面板上那条写着 ``PUBLISH_FAILED：平台要求短信验证`` 时 —— 平台在点下发布之后弹一个
    「接收短信验证码」，要人在**浏览器窗口**里点「获取验证码」、再输手机上收到的六位数。
    这时「重试」是没用的：平台问的是"这台机器/这个出口 IP 是不是你本人"，那是个
    **一次性质询**，重试一百次只会弹一百次（真机实测）。

    为什么这一个可以跑十几分钟而不用后台作业
    --------------------------------------
    与「扫码登录」同一条：它**必须**是人按着看的。窗口关掉这件事没有任何地方能替用户
    确认，所以前端按"长动作"处理（转圈 + 一句"去窗口里输码"），而不是把它藏进一个
    看不见的队列 —— 藏进去之后，"我输完了没反应"就成了一句没人能回答的话。

    它做什么 / 不做什么
    ------------------
    做：开**可见**窗口（同一个 profile、同一份登录态）⇒ 上传 ⇒ 填标题文案 ⇒ 点发布
    ⇒ **停在那儿等人**；人过掉验证之后流程自己走完并落库。
    不做：**不点**「获取验证码」、**不读**短信、**不填**码、不存任何凭据（R13）。
    """
    state: AppState = request.app.state.studio
    payload = body or PublishActionRequest()
    outcome = await publish_assist_service_for(state).assist(
        publication_id, reason=payload.reason, source="webui"
    )
    return PublishAssistOutcome(
        publication=publication_view(outcome["publication"]),
        action=str(outcome["action"]),
        message=str(outcome["message"]),
        waited_sec=float(outcome["waited_sec"]),
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
