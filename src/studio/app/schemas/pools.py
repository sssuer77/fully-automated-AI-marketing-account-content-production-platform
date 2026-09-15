"""四池调度控制台契约（T4.10 · §04.4.5 / §03.4.4）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「四个池各自什么配置、现在什么状态？」 ⇒ :class:`PoolConsoleModel`
   （**配置值与运行值并排**：``pools.yaml`` 的出厂值 vs ``pool_settings`` 的当前值）；
② 「把并发调一下」 ⇒ :class:`ConcurrencyRequest` / :class:`ConcurrencyResponse`；
③ 「这几条死信再跑一次」 ⇒ :class:`RequeueRequest` / :class:`RequeueResponse`；
④ 「机器是不是在偷偷降我的并发？」 ⇒ ``consecutive_oom`` / ``oom_threshold`` /
   ``auto_degrade_enabled`` 三件套摆在卡片上 —— 少了它们，「并发怎么突然变 1 了」
   就成了一条查不完的悬案。

请求体为什么全部 ``extra="forbid"``
-----------------------------------
与总览台、确认闸、选题面板同一理由：把 ``concurrency`` 写成 ``concurreny`` 被静默
忽略，会让人以为「我已经调上去了」，而池还按旧值认领 —— 在并发旋钮上，
「以为生效了」的代价是 OOM。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from studio.core.config import POOL_CONCURRENCY_MAX, POOL_CONCURRENCY_MIN, PoolName

__all__ = [
    "MAX_CONCURRENCY_ANY",
    "MAX_REQUEUE_BATCH",
    "ConcurrencyRequest",
    "ConcurrencyResponse",
    "DeadLetterModel",
    "PoolConsoleModel",
    "PoolsResponse",
    "RequeueFailureModel",
    "RequeueRequest",
    "RequeueResponse",
]

#: 请求体能表达的**物理**上限（与 DDL ``CHECK (concurrency BETWEEN 0 AND 8)`` 同一条线）。
#: 按池的**工程**上限（voice ≤ 3）由服务层兜底 —— 那里给的是一条带 remediation 的
#: 业务错误（「8GB 显存跑 4 路 TTS 必然 OOM」），比一句「字段超范围」有用得多。
MAX_CONCURRENCY_ANY: Final[int] = max(POOL_CONCURRENCY_MAX.values())

#: 一次最多重投多少条死信。重投是「要人一条条看」的动作，不是批量清库：
#: 上限给得宽松，但**必须有**（一次点出 500 条重投，等于把池当成垃圾场）。
MAX_REQUEUE_BATCH: Final[int] = 50


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读 · 控制台
# ══════════════════════════════════════════════════════════════════════


class DeadLetterModel(BaseModel):
    """一条死信（``jobs.status = 'dead'``）。"""

    job_id: str
    task_id: str
    unit_type: str
    unit_ref: str
    attempts: int
    max_attempts: int
    error_code: str | None = None
    error_message: str | None = None
    finished_at: str | None = None


class PoolConsoleModel(BaseModel):
    """一个池在控制台上的全部信息（配置 + 运行值 + 状态 + 死信）。

    ``error`` 有值 ⇒ 除 ``pool`` 外都不可信（池参数缺失 / 库结构漂移）。
    **不折叠成 500**：三个池好好的、一个池读不出来，面板要能把好的三个画出来。
    """

    pool: str
    unit_type: str | None = None
    priority: int | None = None
    poll_ms: int | None = None
    unit_timeout_sec: int | None = None
    config_concurrency: int | None = None
    concurrency: int
    concurrency_min: int
    concurrency_max: int
    at_concurrency_ceiling: bool
    paused: bool
    paused_at: str | None = None
    paused_by: str | None = None
    lease_sec: int
    max_attempts: int
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    canceled: int
    running: int
    oldest_pending_age_sec: int | None = None
    backlog: int
    consecutive_oom: int
    oom_threshold: int
    auto_degrade_enabled: bool
    dead_letters: list[DeadLetterModel]
    error: str | None = None


class PoolsResponse(BaseModel):
    """``GET /api/v1/pools`` —— 一次拿全（四个池 + 自动降级开关 + 配置出处）。"""

    generated_at: str
    pools: list[PoolConsoleModel]
    auto_degrade_enabled: bool
    oom_threshold: int
    config_path: str
    config_error: str | None = None


# ══════════════════════════════════════════════════════════════════════
# 写 · 并发旋钮
# ══════════════════════════════════════════════════════════════════════


class ConcurrencyRequest(_Body):
    """调一个池的并发（``concurrency`` 是**目标值**，不是增量）。"""

    pool: PoolName
    concurrency: int = Field(ge=POOL_CONCURRENCY_MIN, le=MAX_CONCURRENCY_ANY)
    reason: str | None = None


class ConcurrencyResponse(BaseModel):
    """并发调整的结论（``note`` 直接给人看：「在途 N 个跑完才收敛」）。"""

    pool: str
    concurrency: int
    previous: int
    changed: bool
    running: int
    pending: int
    paused: bool
    note: str


# ══════════════════════════════════════════════════════════════════════
# 写 · 死信重投
# ══════════════════════════════════════════════════════════════════════


class RequeueRequest(_Body):
    """批量重投死信（逐条独立，部分失败不回滚）。"""

    job_ids: list[str] = Field(min_length=1, max_length=MAX_REQUEUE_BATCH)
    reason: str | None = None


class RequeueFailureModel(BaseModel):
    """重投失败的一条（带 ``code`` + ``remediation``，前端能照着重试）。"""

    job_id: str
    code: str
    message: str
    remediation: str | None = None


class RequeueResponse(BaseModel):
    """一次死信重投的结论（``requeued`` 与 ``failed`` 之和恒等于 ``requested``）。"""

    requested: int
    requeued: list[str]
    failed: list[RequeueFailureModel]
