"""无人值守守护契约（T4.11 · §04.5.10 / §01.2.1 / §03.7.5）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「守护还活着吗？它守谁、多久一拍？」 ⇒ :class:`WatchdogStatusModel`
   （``enabled`` / ``runnable`` / ``tick_sec`` / ``self_name``）；
② 「它这一轮做了什么？」 ⇒ ``services`` / ``restarted_total`` / ``halted`` /
   ``disk_gate`` —— **自愈次数与停手进程是同一件事的两面**：只报"重启了 3 次"不报
   "voice 已停手"，用户会以为机器还在自愈，实际上它早就放弃了（P4：不许静默）；
③ 「有没有任务掉进人工池？」 ⇒ ``manual_pool``（**T4.11 验收明文要求"人工池可见"**）；
④ 「我现在就想验证它还在干活」 ⇒ :class:`WatchdogTickRequest` /
   :class:`WatchdogTickResponse`（人工触发一轮，留 ``audit_ops``）。

为什么 ``manual_pool`` 不带 ``error_message`` 全文
--------------------------------------------------
它已经在 ``/api/v1/logs`` 与任务详情里各有一份。面板首屏要的是"有几条、卡在哪一步、
什么时候掉的"，点进去再看细节 —— 把长文本塞进总览台的响应里，只会让 5s 轮询的
载荷白白变大。

请求体为什么 ``extra="forbid"``
-------------------------------
与其余面板同一理由（裁定 135 / 155）：把 ``reason`` 写成 ``reasom`` 被静默忽略，
留痕里就会少一句"人为什么按这个按钮"。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "DiskGateModel",
    "ManualPoolTaskModel",
    "TaskSweepModel",
    "WatchdogServiceModel",
    "WatchdogStatusModel",
    "WatchdogTickRequest",
    "WatchdogTickResponse",
]


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读 · 守护状态
# ══════════════════════════════════════════════════════════════════════


class WatchdogServiceModel(BaseModel):
    """一个被守护进程的结论（``ServiceGuard.to_dict``）。

    ``guarded=False`` 有两种含义，靠 ``detail`` 区分：**守护者自己**（``self_name``，
    进程内机制守不了它）与**不在守护名单里的进程**。面板必须把这两种分开显示 ——
    前者是设计使然（由 ``ops/start_all.ps1`` 守护），后者是配置写错了。
    """

    name: str
    guarded: bool
    running: bool
    pid: int | None = None
    restarts: int
    halted: bool
    next_restart_at: str | None = None
    detail: str | None = None


class DiskGateModel(BaseModel):
    """磁盘水位门禁的结论（``DiskGateOutcome.to_dict``）。

    ``low=None`` ⇒ **还没采过资源**（不是"水位正常"）。这两个值在面板上必须分开画：
    把"没数据"画成"正常"，用户会在磁盘爆掉的那一刻才发现门禁从没工作过。
    """

    low: bool | None = None
    enabled: bool
    applied: list[str]
    released: list[str]
    skipped: list[str]
    overridden: list[str]
    detail: str | None = None


class TaskSweepModel(BaseModel):
    """一次任务清扫的结论（``TaskSweep.to_dict``）。"""

    failed: list[str]
    manual_pool: list[str]
    skipped: list[str]


class ManualPoolTaskModel(BaseModel):
    """掉进人工池的一条任务（**T4.11 验收要求"可见"**）。"""

    task_id: str
    title: str
    status: str
    attempt_count: int
    retry_from: str | None = None
    error_code: str | None = None
    stage_detail: str | None = None
    updated_at: str


class WatchdogStatusModel(BaseModel):
    """``GET /api/v1/watchdog`` —— 守护的当前状态（``WatchdogService.snapshot()``）。

    ``manual_pool_after`` 是阈值本身：面板上"为什么这条进了人工池"的答案必须
    和配置在同一个响应里，否则用户要去翻 ``pools.yaml`` 才知道 3 是从哪来的。
    """

    enabled: bool
    runnable: bool
    self_name: str | None = None
    tick_sec: float
    manual_pool_after: int
    last_tick_at: str | None = None
    services: list[WatchdogServiceModel]
    halted: list[str]
    restarted_total: int
    disk_gate: DiskGateModel
    manual_pool: list[ManualPoolTaskModel]
    detail: str | None = None


# ══════════════════════════════════════════════════════════════════════
# 写 · 人工触发一轮
# ══════════════════════════════════════════════════════════════════════


class WatchdogTickRequest(_Body):
    """人工触发一轮守护（``reason`` 进 ``audit_ops``）。"""

    reason: str | None = None


class WatchdogTickResponse(BaseModel):
    """一轮守护的结论（``WatchdogTick.to_dict()`` + ``audit_id`` / ``note``）。

    ``audit_id=None`` ⇒ **留痕没写进去**（库忙 / 表结构漂移），而 ``note`` 仍如实
    汇报这一轮做了什么 —— 不把"没留痕"静默成"没发生"。
    """

    at: str
    enabled: bool
    services: list[WatchdogServiceModel]
    restarted: list[str]
    halted: list[str]
    disk_gate: DiskGateModel
    recovered: list[str]
    sweep: TaskSweepModel
    errors: list[str]
    audit_id: int | None = None
    note: str
