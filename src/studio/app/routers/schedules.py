"""定时发布计划 REST 面（T5.6 · §04.6.5.1）。

五个端点 = 面板上现在能做的五件事
---------------------------------
列表、新建、修改（含启停）、删除、立刻执行一次。

为什么没有「跑一拍调度器」这个端点
----------------------------------
后台泵每 30s 自己拍一次（``app/recycle.SchedulePump``），而"手动催一下"这件事
对**某一条计划**才有意义 —— 那是 ``run_now``。给整个调度器加一个"现在拍"的按钮，
点下去只会得到"它本来就该在 30s 内自己拍"，而那一拍该做什么完全取决于到点没到点。

为什么 ``run_now`` **不看** ``publish.enabled`` 就放行
-----------------------------------------------------
反过来：它**看**。这一下与到点触发走同一条 ``SchedulerService.fire``，
开关关着时返回 ``result='skipped_disabled'`` 而不是建作业 ——
让"人工按一下"绕过 R14 的不可逆防护，等于给开关开了一个后门，
而那个后门正好长在"我想试试看"这句最常说的话上。

为什么校验统一放在服务层（而不是 Pydantic 约束）
------------------------------------------------
规格书要的是「非法参数 ⇒ **400**」。约束写在模型里会被 Pydantic 拦成 422，
而 422 在本项目里的意思是"某个表单字段写错了"（前端据此标红）。
这个接口的判据是跨字段的（``start < end``、平台代号要在配置里），
所以模型只做形状校验，语义校验全在 ``validate_spec``，错误码统一 400。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from fastapi import Path as PathParam

from studio.app.deps import AppState, publish_config_for
from studio.app.schemas.schedule import (
    PublishSchedulePatch,
    PublishScheduleSpec,
    ScheduleDeleteResponse,
    ScheduleList,
    ScheduleRunRequest,
    ScheduleRunResponse,
    ScheduleView,
    schedule_view,
)
from studio.db.repositories.schedule_repo import ScheduleRepo
from studio.services.publish_service import platform_options
from studio.services.scheduler_service import (
    SCHEDULER_TICK_INTERVAL_SEC,
    create_schedule,
    delete_schedule,
    run_now,
    update_schedule,
)

__all__ = ["router"]

router = APIRouter(tags=["schedules"])

#: 计划 id 的形状（ULID，Crockford base32）。面板只回传服务端给过的 id。
_SCHEDULE_ID = PathParam(pattern=r"^[0-9A-Za-z]{1,64}$")


@router.get("/api/v1/schedules", response_model=ScheduleList)
def list_schedules(request: Request) -> ScheduleList:
    """全部计划 + 计数（**启用的排前面**，同组内按下次触发时刻升序）。"""
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    rows = ScheduleRepo(connection).list_all()
    config = publish_config_for(state)
    items = [schedule_view(row) for row in rows]
    return ScheduleList(
        items=items,
        counts={
            "total": len(items),
            "enabled": sum(1 for item in items if item.enabled),
            "disabled": sum(1 for item in items if not item.enabled),
            "failing": sum(1 for item in items if item.fail_streak > 0),
        },
        # 能选的平台来自**配置**（与投递面板同一份判据）：面板列一份清单
        # 等于把 `config/publish.yaml` 抄第二遍，漏改的那一处表现为
        # "这个平台在定时计划里选不到"，没有人会去报这个 bug。
        enabled_platforms=[opt.code for opt in platform_options(config) if opt.selectable],
        tick_sec=SCHEDULER_TICK_INTERVAL_SEC,
    )


@router.post("/api/v1/schedules", response_model=ScheduleView)
def create(request: Request, body: PublishScheduleSpec) -> ScheduleView:
    """新建一条计划（``next_run_at`` 当场算好落库 · 陷阱 #30）。"""
    state: AppState = request.app.state.studio
    row = create_schedule(
        connection=state.connections.get(),
        config=publish_config_for(state),
        spec=body.model_dump(),
        actor="user",
        source="webui",
        log=state.logs.append,
    )
    return schedule_view(row)


@router.patch("/api/v1/schedules/{schedule_id}", response_model=ScheduleView)
def patch(
    request: Request, schedule_id: Annotated[str, _SCHEDULE_ID], body: PublishSchedulePatch
) -> ScheduleView:
    """改一条计划（部分字段）。

    ``exclude_unset=True`` 是这一层的**关键**：只有请求里真出现过的键才会被写下去。
    少了它，"我只想停用它"会连带把面板那一刻的旧值（可能已经被别人改过）
    一起写回库里 —— 那是并发编辑的经典坑，而表现出来是"我的改动莫名其妙被回滚了"。
    """
    state: AppState = request.app.state.studio
    payload = body.model_dump(exclude_unset=True)
    reason = payload.pop("reason", None)
    row = update_schedule(
        connection=state.connections.get(),
        config=publish_config_for(state),
        schedule_id=schedule_id,
        patch=payload,
        actor="user",
        reason=reason,
        source="webui",
        log=state.logs.append,
    )
    return schedule_view(row)


@router.delete("/api/v1/schedules/{schedule_id}", response_model=ScheduleDeleteResponse)
def remove(request: Request, schedule_id: Annotated[str, _SCHEDULE_ID]) -> ScheduleDeleteResponse:
    """删一条计划（**写留痕**；不存在的 id 回 ``deleted=false`` 而不是 404）。"""
    state: AppState = request.app.state.studio
    removed = delete_schedule(
        connection=state.connections.get(),
        schedule_id=schedule_id,
        actor="user",
        source="webui",
    )
    return ScheduleDeleteResponse(
        schedule_id=schedule_id,
        deleted=removed,
        message="已删除（留痕里记着这一下）" if removed else "这条计划本来就不在（可能是别人已经删了）",
    )


@router.post("/api/v1/schedules/{schedule_id}/run_now", response_model=ScheduleRunResponse)
def run(
    request: Request, schedule_id: Annotated[str, _SCHEDULE_ID], body: ScheduleRunRequest | None = None
) -> ScheduleRunResponse:
    """立刻执行一次（调试用；**仍走限频、仍看开关**）。"""
    state: AppState = request.app.state.studio
    reason = None if body is None else body.reason
    outcome = run_now(
        connection=state.connections.get(),
        paths=state.paths,
        config=publish_config_for(state),
        schedule_id=schedule_id,
        log=state.logs.append,
        now=None,
    )
    result = ScheduleRunResponse.model_validate(outcome.to_dict())
    if reason:
        # `reason` 只进日志（这一下不留痕：它不改计划的参数，只产生一次作业）。
        state.logs.append(
            level="info",
            source="publish.scheduler",
            message=f"人工执行定时计划 {schedule_id}：{reason}",
            payload={"schedule_id": schedule_id, "result": result.result},
        )
    return result
