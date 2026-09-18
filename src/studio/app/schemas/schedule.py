"""定时发布计划契约（T5.6 · §04.6.5.1）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「我排了哪些计划、下一次各是什么时候？」 ⇒ :class:`ScheduleList`；
② 「新建 / 改一个计划」 ⇒ :class:`PublishScheduleSpec`（建）与
   :class:`PublishSchedulePatch`（改）；
③ 「我刚才那一下的结果是什么？」 ⇒ :class:`ScheduleView` 原样回写；
④ 「现在立刻试一次」 ⇒ :class:`ScheduleRunResponse`。

为什么 ``jitter_min`` **不**在这里加 ``ge=0, le=120``
----------------------------------------------------
规格书 §04.6.5.1 明写「``jitter_min ∈ [0,120]``；非法参数 ⇒ **400** 且不落库」。
约束写在模型里，越界会先被 Pydantic 拦成 **422**，而 422 在本项目里的意思是
"某个表单字段写错了，前端把红字标到那个框上"（见 ``app/errors.py`` 的登记表）。
这个接口的判据是跨字段的（``start < end``、平台代号要在配置里），
所以校验统一放在 :func:`~studio.services.scheduler_service.validate_spec`，
错误码统一是 ``SCHEDULE_INVALID`` ⇒ 400。**一个接口只有一条报错路径**。

``extra="forbid"``
------------------
与其余面板同一理由（裁定 135 / 155）：把 ``windw`` 写成 ``window`` 被静默忽略，
用户看到的是"我改了窗口但它没生效" —— 而库里其实一个字都没变。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.db.repositories.schedule_repo import ScheduleRow

__all__ = [
    "PublishSchedulePatch",
    "PublishScheduleSpec",
    "ScheduleDeleteResponse",
    "ScheduleList",
    "ScheduleRunRequest",
    "ScheduleRunResponse",
    "ScheduleView",
    "schedule_view",
]


class _Body(BaseModel):
    """请求体基类（禁多余字段）。"""

    model_config = ConfigDict(extra="forbid")


ScheduleModeName = Literal["at_time", "daily_window", "interval"]


class PublishScheduleSpec(_Body):
    """新建一条计划（§04.6.5.1 的 ``PublishScheduleSpec``）。

    ``task_id=None`` ⇒ 到点从"待发布池"取一条（出片完成、还没发过的）。
    钉死 ``task_id`` 的用法是"这条片子我要在明天 19 点发"。
    """

    task_id: str | None = None
    platforms: list[str] = Field(min_length=1)
    account_ids: list[str] = Field(min_length=1)
    mode: ScheduleModeName
    at_time: str | None = None
    window: tuple[str, str] | None = None
    interval_hours: int | None = None
    #: 抖动分钟数（默认 15）。范围由服务层校验 ⇒ 越界是 400 而不是 422，见模块注释。
    jitter_min: int = 15
    enabled: bool = True


class PublishSchedulePatch(_Body):
    """改一条计划（**部分字段**：只给 ``enabled`` 就是启停）。

    每个字段都可缺省 —— 缺省 = "别动这一项"。让面板每次提交整份表单的话，
    "我只想停用它"会连带把别的字段按面板那一刻的旧值写回去。
    """

    task_id: str | None = None
    platforms: list[str] | None = None
    account_ids: list[str] | None = None
    mode: ScheduleModeName | None = None
    at_time: str | None = None
    window: tuple[str, str] | None = None
    interval_hours: int | None = None
    jitter_min: int | None = None
    enabled: bool | None = None
    reason: str | None = None


class ScheduleRunRequest(_Body):
    """立刻执行一次（``reason`` 进日志，便于事后对上"我按那一下是为了什么"）。"""

    reason: str | None = None


class ScheduleView(BaseModel):
    """一条计划（``ScheduleRow.to_dict()`` 原样）。"""

    id: str
    task_id: str | None = None
    platforms: list[str]
    account_ids: list[str]
    mode: str
    at_time: str | None = None
    window: list[str] | None = None
    interval_hours: int | None = None
    jitter_min: int
    enabled: bool
    next_run_at: str | None = None
    last_run_at: str | None = None
    run_count: int
    last_result: str | None = None
    fail_streak: int
    created_by: str
    created_at: str | None = None
    updated_at: str | None = None


class ScheduleList(BaseModel):
    """``GET /api/v1/schedules`` —— 全部计划 + 计数。

    ``counts`` 里带 ``failing``（``fail_streak > 0``）：面板要一眼看出
    "有几个计划正在连续失败"，而那一条藏在每条记录的 ``last_result`` 里时，
    没人会一条条点开看。
    """

    items: list[ScheduleView]
    counts: dict[str, int]
    enabled_platforms: list[str]
    tick_sec: float


class ScheduleDeleteResponse(BaseModel):
    """删除的结论（``deleted=False`` ⇒ 这个 id 本来就不在，**不是错误**）。"""

    schedule_id: str
    deleted: bool
    message: str


class ScheduleRunResponse(BaseModel):
    """``POST .../run_now`` 的结论（``ScheduleFireResult.to_dict()``）。"""

    schedule_id: str
    result: str
    job_ids: list[str]
    queued: int
    skipped: list[str]
    next_run_at: str | None = None
    task_id: str | None = None
    note: str | None = None


def schedule_view(row: ScheduleRow) -> ScheduleView:
    """行 -> 视图（走 ``to_dict()`` 再复核，避免两处各写一份字段表）。"""
    data: dict[str, Any] = row.to_dict()
    return ScheduleView.model_validate(data)
