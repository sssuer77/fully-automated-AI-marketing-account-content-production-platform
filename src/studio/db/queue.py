"""四池队列内核 ``JobStore``（T1.5 · §03.4）——**唯一允许写 ``jobs`` 的模块**。

三条机制撑起全部并发正确性
--------------------------
1. **单语句原子认领**（§03.4.2）：``UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING``
   —— 子查询与更新在同一语句同一事务内求值，不存在"两个 worker 都读到同一行再都去改"的窗口。
   **禁止** ``SELECT`` + 应用层判断 + ``UPDATE`` 的认领写法（§03.4.6 规则 2）。
2. **租约**：认领即写 ``lease_expires_at``；worker 每 ``lease/3`` 续租；崩溃 ⇒ 租约自然过期 ⇒
   sweeper 回收（退避重排 or 死信）。
3. **幂等键** ``(task_id, pool, unit_type, unit_ref)``：重复入队不产生第二条。

时间来源（T1.5 施工裁定 26）
----------------------------
队列里所有时间戳都由 :mod:`studio.core.clock` 在 Python 侧算好、作为绑定参数传入，
**不用** SQL 的 ``strftime('now')``。理由：① 单测可冻结时间（``now=`` 参数），
不用 monkeypatch 全局时钟；② 与 DDL 默认值同为毫秒格式，字典序即时间序；
③ 同一方法内多处取时间必然一致。

分层
----
本模块只依赖 ``core`` / ``db.engine`` / ``db.lease``，**不导入** ``domain`` ——
池名与单元类型一律以 ``str`` 透传，合法性由 DDL 的 ``CHECK`` 兜底
（"DDL 是唯一真相"，不在 Python 里再抄一份枚举）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from studio.core.clock import format_iso, local_tz, parse_iso, utc_now
from studio.core.config import POOL_CONCURRENCY_MIN, AutoConcurrencyConfig, concurrency_bounds
from studio.core.errors import ErrorCode, QueueError, StudioError
from studio.core.ids import new_job_id
from studio.core.logging import get_logger
from studio.core.proto import ALERT_SEVERITY, AlertCode
from studio.db.engine import transaction
from studio.db.lease import lease_expires_at, not_before_at

__all__ = [
    "AUTODEGRADE_CODES",
    "AUTO_ACTOR",
    "ConcurrencyChange",
    "Job",
    "JobOutcome",
    "JobStore",
    "PoolRuntime",
    "PoolStats",
    "RateLimitState",
    "ReclaimResult",
    "WorkerHeartbeat",
]

logger = get_logger("studio.db.queue")

#: 自动降并发的操作者署名（进 ``pool_settings.updated_by`` / ``audit_ops.actor_ref``）。
#: 与 ``user`` 分开是刚需：复盘时要能一眼看出"这个并发是机器降的，不是人调的"。
AUTO_ACTOR: Final[str] = "auto_autodegrade"

#: 触发"自动降并发"的错误码（T4.10 · §01.4.4）。
#: 现在只有 TTS 的显存不足；render 的 CUDA OOM 若将来有独立错误码，加进这个元组即可
#: —— 判据写成元组而不是 `if code == "TTS_OOM"`，是为了让"哪些失败算容量问题"
#: 有一个可枚举的落点，而不是散在条件表达式里。
AUTODEGRADE_CODES: Final[tuple[str, ...]] = (ErrorCode.TTS_OOM.value,)

#: 认领单元的状态（``jobs.status``）—— 只有这些取值能落库（DDL CHECK 兜底）
CLAIMABLE_STATUS: Final[str] = "pending"
CLAIMED_STATUS: Final[str] = "claimed"
SUCCEEDED_STATUS: Final[str] = "succeeded"
BLOCKED_STATUS: Final[str] = "blocked"
DEAD_STATUS: Final[str] = "dead"

#: 租约过期回收时写入的 ``error_code``
LEASE_EXPIRED_CODE: Final[str] = "LEASE_EXPIRED"

#: 发布限频：这些状态计入"今日已用额度"（§3.3.15）
_RATE_LIMIT_STATUSES: Final[tuple[str, ...]] = ("uploading", "published")


# ── 数据结构 ────────────────────────────────────────────────────────────


def _job_pool(connection: sqlite3.Connection, job_id: str) -> str | None:
    """作业属于哪个池（``succeed()`` 归零计数器用；作业已被删 ⇒ ``None``）。"""
    row = connection.execute("SELECT pool FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else str(row["pool"])


def _opt(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(str(raw))


@dataclass(frozen=True, slots=True)
class Job:
    """``jobs`` 行的强类型视图。"""

    id: str
    task_id: str
    pool: str
    unit_type: str
    unit_ref: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    depends_on: tuple[str, ...]
    payload: dict[str, Any]
    result: dict[str, Any]
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    not_before: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Job:
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            pool=row["pool"],
            unit_type=row["unit_type"],
            unit_ref=row["unit_ref"],
            status=row["status"],
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            depends_on=tuple(_loads(_opt(row, "depends_on_json"), [])),
            payload=_loads(_opt(row, "payload_json"), {}),
            result=_loads(_opt(row, "result_json"), {}),
            lease_owner=_opt(row, "lease_owner"),
            lease_expires_at=_opt(row, "lease_expires_at"),
            heartbeat_at=_opt(row, "heartbeat_at"),
            not_before=_opt(row, "not_before"),
            error_code=_opt(row, "error_code"),
            error_message=_opt(row, "error_message"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=_opt(row, "finished_at"),
        )


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """``worker_heartbeats`` 行（T1.6 写入，这里只读）。"""

    worker_id: str
    pool: str
    status: str
    pid: int | None
    current_job_id: str | None
    last_seen_at: str
    gpu_mem_mb: int | None
    rss_mb: int | None
    started_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkerHeartbeat:
        return cls(
            worker_id=row["worker_id"],
            pool=row["pool"],
            status=row["status"],
            pid=_opt(row, "pid"),
            current_job_id=_opt(row, "current_job_id"),
            last_seen_at=row["last_seen_at"],
            gpu_mem_mb=_opt(row, "gpu_mem_mb"),
            rss_mb=_opt(row, "rss_mb"),
            started_at=_opt(row, "started_at"),
        )


@dataclass(frozen=True, slots=True)
class PoolRuntime:
    """``pool_settings`` 的一行：池的运行时旋钮（§03.3.17）。"""

    pool: str
    concurrency: int
    paused: bool
    lease_sec: int
    max_attempts: int
    backoff_base_ms: int
    backoff_max_ms: int
    rate_limit: dict[str, Any] = field(default_factory=dict)
    #: **本次**暂停的时间与操作者（恢复时清空）。
    #: 为什么不留"最后一次暂停"：列名是 `paused_at`，留一个"并不在暂停"的时间戳
    #: 只会让人误判；"谁在什么时候暂停过"本来就在 `audit_ops` 里永久保留
    #: （T4.2 裁定 138）。
    paused_at: str | None = None
    paused_by: str | None = None
    #: 连续 OOM 次数（T4.10 自动降并发的**唯一**状态 · 0007 迁移）。
    #: 一条成功即归零；达到阈值的那一次才降并发并告警。
    consecutive_oom: int = 0


@dataclass(frozen=True, slots=True)
class ConcurrencyChange:
    """一次并发旋钮的结果（``changed=False`` ⇒ 值本来就是这样，**没写留痕**）。"""

    pool: str
    concurrency: int
    previous: int
    changed: bool
    running: int
    paused: bool
    consecutive_oom: int = 0


@dataclass(frozen=True, slots=True)
class PoolStats:
    """``pool.stats`` 事件的数据体（§04.5.2）。"""

    pool: str
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    canceled: int
    concurrency: int
    running: int
    paused: bool
    oldest_pending_age_sec: int | None
    workers: tuple[WorkerHeartbeat, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "pending": self.pending,
            "blocked": self.blocked,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "dead": self.dead,
            "canceled": self.canceled,
            "concurrency": self.concurrency,
            "running": self.running,
            "paused": self.paused,
            "oldest_pending_age_sec": self.oldest_pending_age_sec,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "pool": w.pool,
                    "status": w.status,
                    "current_job_id": w.current_job_id,
                    "gpu_mem_mb": w.gpu_mem_mb,
                }
                for w in self.workers
            ],
        }


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """``fail()`` 的结论：退避重排 or 死信。"""

    job_id: str
    pool: str
    status: str
    attempts: int
    max_attempts: int
    not_before: str | None = None
    error_code: str | None = None

    @property
    def is_dead(self) -> bool:
        return self.status == DEAD_STATUS


@dataclass(frozen=True, slots=True)
class ReclaimResult:
    """sweeper 一轮回收的结果。"""

    requeued: tuple[str, ...] = ()
    dead: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RateLimitState:
    """``publish`` 池的限频结论（§03.4.4 ⑥ · §06.10）。

    限频触顶**不是失败**：调用方应把 ``not_before`` 顺延到 ``next_allowed_at``，
    job 保持 ``pending``，不计 ``attempts``（§06.10 "限频触顶"一行）。
    """

    allowed: bool
    used_today: int
    daily_limit: int
    next_allowed_at: str | None = None
    reason: str | None = None


# ── JobStore ────────────────────────────────────────────────────────────


class JobStore:
    """四池共用的队列门面（§03.4.1）。

    所有方法都是**同步**的（T1.5 施工裁定 26）：``db`` 层全同步（与 ``TaskService``
    一致），单次调用 < 5ms，远低于 R10 的 20ms 事务预算；T1.6 的 worker 在自己的
    事件循环里直接调用即可，需要并发时用 ``asyncio.to_thread`` + 每线程独立连接。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        auto_concurrency: AutoConcurrencyConfig | None = None,
    ) -> None:
        """``auto_concurrency`` 来自 ``config/pools.yaml``（T4.10 自动降并发）。

        为什么是构造参数而不是在 ``fail()`` 里现读 YAML
        ----------------------------------------------
        ``fail()`` 整个跑在 ``BEGIN IMMEDIATE`` 事务里，而 §03.4.6 规则 3 明令
        "禁止在事务内做 I/O" —— 一次 YAML 读盘足够把写锁多持几毫秒，正是
        ``database is locked`` 的成因。配置在构造时注入，事务里只剩算术。

        默认值取 :class:`AutoConcurrencyConfig` 的字段默认（``enabled=True`` /
        ``oom_threshold=2``），与 ``pools.yaml`` 的出厂值一致；生产路径
        （``pools/runner.build_worker``）**显式**传盘上那份，改 YAML 即生效。
        """
        self._connection = connection
        self._auto = auto_concurrency or AutoConcurrencyConfig()

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job:
        row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise QueueError(
                f"作业不存在：{job_id}",
                code=ErrorCode.JOB_NOT_FOUND,
                context={"job_id": job_id},
                remediation="确认 job_id；已 GC 的作业可从 data/backups/ 还原",
            )
        return Job.from_row(row)

    def list_jobs(
        self,
        *,
        pool: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Job, ...]:
        """按池 / 状态 / 任务过滤（WebUI 与测试用；**永远带 LIMIT**，§03.4.6 规则 4）。"""
        if limit <= 0:
            raise ValueError(f"limit 必须为正：{limit}")
        sql = "SELECT * FROM jobs WHERE 1 = 1"
        params: list[Any] = []
        if pool is not None:
            sql += " AND pool = ?"
            params.append(pool)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY priority ASC, created_at ASC LIMIT ?"
        params.append(limit)
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(Job.from_row(row) for row in rows)

    def pool_runtime(self, pool: str) -> PoolRuntime:
        """读 ``pool_settings``（worker 认领前调用，可自行缓存 2s，§03.4.4）。"""
        return self._runtime(pool)

    def running_count(self, pool: str) -> int:
        """在跑单元数（``status='claimed'``）。"""
        return int(
            self._connection.execute(
                "SELECT count(*) FROM jobs WHERE pool = ? AND status = ?",
                (pool, CLAIMED_STATUS),
            ).fetchone()[0]
        )

    def stats(self, *, pool: str, now: datetime | None = None) -> PoolStats:
        """池统计（``pool.stats`` 事件的数据源）。"""
        moment = now or utc_now()
        rows = self._connection.execute(
            "SELECT status, count(*) AS n FROM jobs WHERE pool = ? GROUP BY status", (pool,)
        ).fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        runtime = self._runtime(pool)
        oldest = self._connection.execute(
            "SELECT min(created_at) AS first_seen FROM jobs WHERE pool = ? AND status = ?",
            (pool, CLAIMABLE_STATUS),
        ).fetchone()["first_seen"]
        age = None if oldest is None else max(0, int((moment - parse_iso(oldest)).total_seconds()))
        workers = self._connection.execute(
            "SELECT * FROM worker_heartbeats WHERE pool = ? ORDER BY worker_id", (pool,)
        ).fetchall()
        return PoolStats(
            pool=pool,
            pending=counts.get("pending", 0),
            blocked=counts.get("blocked", 0),
            claimed=counts.get("claimed", 0),
            succeeded=counts.get("succeeded", 0),
            failed=counts.get("failed", 0),
            dead=counts.get("dead", 0),
            canceled=counts.get("canceled", 0),
            concurrency=runtime.concurrency,
            running=counts.get("claimed", 0),
            paused=runtime.paused,
            oldest_pending_age_sec=age,
            workers=tuple(WorkerHeartbeat.from_row(row) for row in workers),
        )

    def dead_letters(self, *, pool: str, limit: int = 100) -> tuple[Job, ...]:
        """死信列表（WebUI 死信区 / 一键重投入口）。"""
        return self.list_jobs(pool=pool, status=DEAD_STATUS, limit=limit)

    # ── 入队 ────────────────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        task_id: str,
        pool: str,
        unit_type: str,
        unit_ref: str,
        priority: int = 100,
        depends_on: Sequence[str] | None = None,
        payload: Mapping[str, Any] | None = None,
        max_attempts: int | None = None,
        job_id: str | None = None,
    ) -> str | None:
        """幂等入队：命中 ``(task_id, pool, unit_type, unit_ref)`` ⇒ **返回 ``None`` 且不重复插入**。

        幂等是刚需：§3.4.5 的"完成后自动触发下游入队"可能被多处触发（worker 的
        ack 路径 + 恢复路径 + 人工重投），重复调用必须安全。

        ``depends_on`` 非空且上游**尚未全部 succeeded** ⇒ 初始状态 ``blocked``；
        上游已全部成功（如 ``render/final`` 依赖已完成的场景）⇒ 直接 ``pending``，
        不必等下一轮 ``unlock_dependents``。
        """
        deps = list(depends_on or [])
        deps_json = json.dumps(deps, ensure_ascii=False)
        payload_json = json.dumps(dict(payload or {}), ensure_ascii=False)
        new_id = job_id or new_job_id()

        with transaction(self._connection, immediate=True):
            limit = max_attempts or self._runtime(pool).max_attempts
            row = self._connection.execute(
                """
                INSERT INTO jobs(id, task_id, pool, unit_type, unit_ref, priority,
                                 depends_on_json, payload_json, max_attempts, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CASE
                          WHEN ? = '[]' THEN 'pending'
                          WHEN NOT EXISTS (
                                 SELECT 1 FROM json_each(?) dep
                                  WHERE dep.value NOT IN (SELECT id FROM jobs WHERE status = 'succeeded')
                               ) THEN 'pending'
                          ELSE 'blocked'
                        END)
                ON CONFLICT (task_id, pool, unit_type, unit_ref) DO NOTHING
                RETURNING id
                """,
                (
                    new_id,
                    task_id,
                    pool,
                    unit_type,
                    unit_ref,
                    priority,
                    deps_json,
                    payload_json,
                    limit,
                    deps_json,
                    deps_json,
                ),
            ).fetchone()
        return None if row is None else str(row["id"])

    # ── 认领 / 续租 / 收尾 ──────────────────────────────────────────────

    def claim(
        self,
        *,
        pool: str,
        worker_id: str,
        lease_sec: int | None = None,
        now: datetime | None = None,
    ) -> Job | None:
        """单语句原子认领；``None`` = 当前无可认领单元（**空转不是错误**）。

        认领前**顺带**守卫池的 ``paused`` / ``concurrency``（§03.4.4）——
        放在这里是为了"只有一个地方能忘"：worker 侧那 2s 缓存只是省一次读，
        真正的正确性由本方法保证。
        """
        if not worker_id.strip():
            raise ValueError("worker_id 不能为空：租约必须能追责到持有者")
        moment = now or utc_now()
        stamp = format_iso(moment)

        with transaction(self._connection, immediate=True):
            runtime = self._runtime(pool)
            if runtime.paused:
                return None
            if self.running_count(pool) >= runtime.concurrency:
                return None
            effective_lease = lease_sec if lease_sec is not None else runtime.lease_sec
            row = self._connection.execute(
                """
                UPDATE jobs
                   SET status            = 'claimed',
                       lease_owner       = ?,
                       lease_expires_at  = ?,
                       heartbeat_at      = ?,
                       attempts          = attempts + 1
                 WHERE id = (
                   SELECT id FROM jobs
                    WHERE pool = ?
                      AND status = 'pending'
                      AND (not_before IS NULL OR not_before <= ?)
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1)
                RETURNING *
                """,
                (
                    worker_id,
                    lease_expires_at(effective_lease, now=moment),
                    stamp,
                    pool,
                    stamp,
                ),
            ).fetchone()
        return None if row is None else Job.from_row(row)

    def renew(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_sec: int,
        now: datetime | None = None,
    ) -> bool:
        """续租。``False`` ⇒ **租约已丢，worker 必须立即中止并丢弃产物**。"""
        moment = now or utc_now()
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = ?,
                       heartbeat_at     = ?
                 WHERE id = ? AND lease_owner = ? AND status = 'claimed'
                """,
                (
                    lease_expires_at(lease_sec, now=moment),
                    format_iso(moment),
                    job_id,
                    worker_id,
                ),
            )
        return cursor.rowcount == 1

    def succeed(
        self,
        *,
        job_id: str,
        worker_id: str,
        result: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """标记成功。``False`` ⇒ 租约已丢（产物可能已被别人重做，不要覆盖）。"""
        moment = now or utc_now()
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                """
                UPDATE jobs
                   SET status           = 'succeeded',
                       result_json      = ?,
                       finished_at      = ?,
                       lease_owner      = NULL,
                       lease_expires_at = NULL,
                       heartbeat_at     = NULL,
                       not_before       = NULL,
                       error_code       = NULL,
                       error_message    = NULL,
                       error_trace      = NULL
                 WHERE id = ? AND lease_owner = ? AND status = 'claimed'
                """,
                (
                    json.dumps(dict(result or {}), ensure_ascii=False),
                    format_iso(moment),
                    job_id,
                    worker_id,
                ),
            )
            if cursor.rowcount == 1:
                # 一条成功就打断"连续 OOM"（T4.10）：池重新证明了自己装得下当前并发。
                # 与作业状态同事务 —— 分开写会出现"作业成功了但计数器还是 3"的中间态。
                self._reset_oom_streak(_job_pool(self._connection, job_id))
        return cursor.rowcount == 1

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retryable: bool = True,
        error_trace: str | None = None,
        now: datetime | None = None,
    ) -> JobOutcome:
        """失败收尾：``retryable`` 且 ``attempts < max_attempts`` ⇒ 退避重排；否则 ⇒ **死信**。

        死信**强制告警**（``system.alert(JOB_DEAD)``，§03.4.3）—— 静默丢弃是
        DoD 5"禁止静默失败"的直接违反。
        """
        moment = now or utc_now()
        with transaction(self._connection, immediate=True):
            row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise QueueError(
                    f"作业不存在：{job_id}",
                    code=ErrorCode.JOB_NOT_FOUND,
                    context={"job_id": job_id},
                )
            job = Job.from_row(row)
            if job.status != CLAIMED_STATUS or job.lease_owner != worker_id:
                raise QueueError(
                    f"作业 {job_id} 的租约不在 {worker_id} 手上（当前：{job.lease_owner}）",
                    code=ErrorCode.JOB_LEASE_LOST,
                    context={
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "status": job.status,
                        "lease_owner": job.lease_owner,
                    },
                    remediation="不要写入结果：租约已丢，该单元会被 sweeper 回收后重做",
                )

            will_die = (not retryable) or job.attempts >= job.max_attempts
            if will_die:
                self._connection.execute(
                    """
                    UPDATE jobs
                       SET status           = 'dead',
                           error_code       = ?,
                           error_message    = ?,
                           error_trace      = ?,
                           finished_at      = ?,
                           lease_owner      = NULL,
                           lease_expires_at = NULL,
                           heartbeat_at     = NULL,
                           not_before       = NULL
                     WHERE id = ? AND status = 'claimed'
                    """,
                    (error_code, error_message, error_trace, format_iso(moment), job_id),
                )
                self._alert(
                    AlertCode.JOB_DEAD,
                    pool=job.pool,
                    task_id=job.task_id,
                    job_id=job_id,
                    message=f"作业进入死信：{job.pool}/{job.unit_type} {job.unit_ref}",
                    hint=f"{error_code}：{error_message}；在 WebUI 死信区可一键重投",
                )
                outcome = JobOutcome(
                    job_id=job_id,
                    pool=job.pool,
                    status=DEAD_STATUS,
                    attempts=job.attempts,
                    max_attempts=job.max_attempts,
                    error_code=error_code,
                )
            else:
                runtime = self._runtime(job.pool)
                resume_at = not_before_at(
                    job.attempts,
                    base_ms=runtime.backoff_base_ms,
                    max_ms=runtime.backoff_max_ms,
                    seed=f"{job.id}:{job.attempts}",
                    now=moment,
                )
                self._connection.execute(
                    """
                    UPDATE jobs
                       SET status           = 'pending',
                           not_before       = ?,
                           error_code       = ?,
                           error_message    = ?,
                           error_trace      = ?,
                           lease_owner      = NULL,
                           lease_expires_at = NULL,
                           heartbeat_at     = NULL
                     WHERE id = ? AND status = 'claimed'
                    """,
                    (resume_at, error_code, error_message, error_trace, job_id),
                )
                outcome = JobOutcome(
                    job_id=job_id,
                    pool=job.pool,
                    status=CLAIMABLE_STATUS,
                    attempts=job.attempts,
                    max_attempts=job.max_attempts,
                    not_before=resume_at,
                    error_code=error_code,
                )
            if error_code in AUTODEGRADE_CODES:
                # 与作业状态**同事务**：分开写会出现"作业重排了但计数器没加"的窗口，
                # 而窗口期恰好是连续 OOM 正在发生的时候（T4.10 裁定 146）。
                self._maybe_autodegrade(job.pool, error_code, moment)
        logger.info(
            "queue.job_failed",
            job_id=job_id,
            pool=outcome.pool,
            status=outcome.status,
            attempts=outcome.attempts,
            error_code=error_code,
        )
        return outcome

    # ── sweeper ─────────────────────────────────────────────────────────

    def reclaim_expired(
        self,
        *,
        pool: str | None = None,
        now: datetime | None = None,
    ) -> ReclaimResult:
        """回收过期租约（sweeper 周期调用，§03.4.3 ③）。

        ``attempts >= max_attempts`` ⇒ 死信；否则回 ``pending`` 并叠加指数退避 + 抖动。

        实现说明：退避时长**按池配置**算，而一条 SQL 里没法按行取不同的 base/cap，
        所以这里先读出过期行、再逐行带守卫 UPDATE（整轮在一个 ``BEGIN IMMEDIATE`` 内）。
        §3.4.3 的 SQL 注释本身就写明"``backoff_ms`` 由应用层算"，与此一致；
        §03.4.6 规则 2 禁的是**认领**路径的 SELECT-then-UPDATE，sweeper 不在其列。
        """
        moment = now or utc_now()
        stamp = format_iso(moment)
        requeued: list[str] = []
        dead: list[str] = []

        with transaction(self._connection, immediate=True):
            sql = (
                "SELECT id, pool, task_id, unit_type, unit_ref, attempts, max_attempts "
                "FROM jobs WHERE status = 'claimed' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?)"
            )
            params: list[Any] = [stamp]
            if pool is not None:
                sql += " AND pool = ?"
                params.append(pool)
            rows = self._connection.execute(sql, params).fetchall()

            runtimes: dict[str, PoolRuntime] = {}
            for row in rows:
                job_pool = str(row["pool"])
                if job_pool not in runtimes:
                    runtimes[job_pool] = self._runtime(job_pool)
                runtime = runtimes[job_pool]
                if int(row["attempts"]) >= int(row["max_attempts"]):
                    self._connection.execute(
                        """
                        UPDATE jobs
                           SET status           = 'dead',
                               error_code       = ?,
                               error_message    = ?,
                               finished_at      = ?,
                               lease_owner      = NULL,
                               lease_expires_at = NULL,
                               heartbeat_at     = NULL,
                               not_before       = NULL
                         WHERE id = ? AND status = 'claimed'
                        """,
                        (
                            LEASE_EXPIRED_CODE,
                            f"租约过期且已达最大尝试次数（{row['attempts']}/{row['max_attempts']}）",
                            stamp,
                            row["id"],
                        ),
                    )
                    dead.append(str(row["id"]))
                    self._alert(
                        AlertCode.JOB_DEAD,
                        pool=job_pool,
                        task_id=str(row["task_id"]),
                        job_id=str(row["id"]),
                        message=f"作业租约过期进入死信：{job_pool}/{row['unit_type']} {row['unit_ref']}",
                        hint="worker 可能已崩溃；在 WebUI 死信区可一键重投",
                    )
                else:
                    resume_at = not_before_at(
                        int(row["attempts"]),
                        base_ms=runtime.backoff_base_ms,
                        max_ms=runtime.backoff_max_ms,
                        seed=f"{row['id']}:{row['attempts']}",
                        now=moment,
                    )
                    self._connection.execute(
                        """
                        UPDATE jobs
                           SET status           = 'pending',
                               not_before       = ?,
                               error_code       = ?,
                               error_message    = ?,
                               lease_owner      = NULL,
                               lease_expires_at = NULL,
                               heartbeat_at     = NULL
                         WHERE id = ? AND status = 'claimed'
                        """,
                        (
                            resume_at,
                            LEASE_EXPIRED_CODE,
                            "租约过期（worker 崩溃或卡死）",
                            row["id"],
                        ),
                    )
                    requeued.append(str(row["id"]))

        if requeued or dead:
            logger.info(
                "queue.reclaimed",
                pool=pool or "all",
                requeued=len(requeued),
                dead=len(dead),
            )
        return ReclaimResult(requeued=tuple(requeued), dead=tuple(dead))

    def unlock_dependents(self, *, pool: str | None = None) -> tuple[str, ...]:
        """依赖解锁：上游全部 ``succeeded`` ⇒ 下游 ``blocked`` 转 ``pending``（§03.4.3 ④）。

        多级依赖靠**反复调用**自然收敛：一轮解锁第 1 级，下一轮解锁第 2 级
        （sweeper 每轮都会调，所以"2 级"只是两次 tick 的事，不需要递归 SQL）。
        """
        with transaction(self._connection, immediate=True):
            rows = self._connection.execute(
                """
                UPDATE jobs SET status = 'pending'
                 WHERE status = 'blocked'
                   AND (? IS NULL OR pool = ?)
                   AND NOT EXISTS (
                         SELECT 1 FROM json_each(jobs.depends_on_json) dep
                          WHERE dep.value NOT IN (SELECT id FROM jobs WHERE status = 'succeeded'))
                RETURNING id
                """,
                (pool, pool),
            ).fetchall()
        return tuple(sorted(str(row["id"]) for row in rows))

    # ── 池控制 · 并发旋钮（T4.10 · §03.4.4）─────────────────────────────

    def set_concurrency(
        self,
        *,
        pool: str,
        concurrency: int,
        actor: str = "user",
        actor_ref: str | None = None,
        source: str = "webui",
        reason: str | None = None,
        action: str = "pool.set_concurrency",
        now: datetime | None = None,
    ) -> ConcurrencyChange:
        """改池并发（写 ``pool_settings`` + ``audit_ops``，**生效无需重启**）。

        三条不变量（§03.4.4）：

        1. **下调不杀在途 worker**：新值只影响 ``claim()`` 的守卫
           （``running_count(pool) >= concurrency`` ⇒ 不认领），所以在跑的几个
           会自然跑完，池**收敛**到新值 —— 不是"立刻掐掉"。
        2. **必须留痕**（``pool.set_concurrency``，带 before/after）。
        3. **幂等**：值没变 ⇒ ``changed=False``，不再写一条"又设成 3 了"。

        :param action: 审计动作名。默认 `pool.set_concurrency`（人/面板调的）；
            T4.11 的自动回升传 `pool.autorecover` —— 那是自动回升**唯一**的冷却锚点：
            最近一次并发变更"是谁定的"决定了还能不能再涨（见 `watchdog_service`）。

        上限按池取 :func:`~studio.core.config.concurrency_bounds`（voice ≤ 3）：
        REST 层的 ``le=8`` 只挡住"物理上越界"，"工程上限"在这里兜底 ——
        两处都写一遍是有意的，前者给的是字段级 422，后者给的是带 remediation
        的业务错误（"8GB 显存跑 4 路 TTS 必然 OOM"）。
        """
        low, high = concurrency_bounds(pool)
        if not low <= concurrency <= high:
            raise QueueError(
                f"{pool} 池并发只能是 {low}–{high}，收到 {concurrency}",
                code=ErrorCode.POOL_CONCURRENCY_LIMIT,
                context={"pool": pool, "concurrency": concurrency, "min": low, "max": high},
                remediation=(
                    "voice 池上限 3 是 8GB 显存下的工程值（§01.4.4）；"
                    "确需更高请改 config/pools.yaml 并重跑 doctor"
                ),
            )
        moment = now or utc_now()
        with transaction(self._connection, immediate=True):
            runtime = self._runtime(pool)
            running = self.running_count(pool)
            if runtime.concurrency == concurrency:
                return ConcurrencyChange(
                    pool=pool,
                    concurrency=concurrency,
                    previous=runtime.concurrency,
                    changed=False,
                    running=running,
                    paused=runtime.paused,
                    consecutive_oom=runtime.consecutive_oom,
                )
            self._connection.execute(
                """
                UPDATE pool_settings
                   SET concurrency = ?, updated_at = ?, updated_by = ?
                 WHERE pool = ?
                """,
                (concurrency, format_iso(moment), actor, pool),
            )
            self._audit(
                actor=actor,
                actor_ref=actor_ref,
                action=action,
                target_type="pool",
                target_id=pool,
                before={"concurrency": runtime.concurrency, "running": running},
                after={"concurrency": concurrency},
                reason=reason or f"WebUI 调并发 {runtime.concurrency} → {concurrency}",
                source=source,
                at=moment,
            )
        logger.info(
            "queue.pool_concurrency_set",
            pool=pool,
            previous=runtime.concurrency,
            concurrency=concurrency,
            running=running,
        )
        return ConcurrencyChange(
            pool=pool,
            concurrency=concurrency,
            previous=runtime.concurrency,
            changed=True,
            running=running,
            paused=runtime.paused,
            consecutive_oom=runtime.consecutive_oom,
        )

    # ── 死信重投 ────────────────────────────────────────────────────────

    def requeue_dead(
        self,
        *,
        job_id: str,
        actor: str = "user",
        actor_ref: str | None = None,
        source: str = "webui",
        reason: str | None = None,
    ) -> None:
        """一键重投死信（§03.4.3"可一键重投"）—— 重置 ``attempts`` 并**写 ``audit_ops``**。"""
        with transaction(self._connection, immediate=True):
            row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise QueueError(
                    f"作业不存在：{job_id}",
                    code=ErrorCode.JOB_NOT_FOUND,
                    context={"job_id": job_id},
                )
            job = Job.from_row(row)
            if job.status != DEAD_STATUS:
                raise QueueError(
                    f"只有死信可以重投：{job_id} 当前是 {job.status}",
                    code=ErrorCode.JOB_DEAD,
                    context={"job_id": job_id, "status": job.status},
                    remediation="活着的作业不用重投；要重做请等它自己退避重试或先取消",
                )
            self._connection.execute(
                """
                UPDATE jobs
                   SET status           = 'pending',
                       attempts         = 0,
                       not_before       = NULL,
                       error_code       = NULL,
                       error_message    = NULL,
                       error_trace      = NULL,
                       finished_at      = NULL,
                       lease_owner      = NULL,
                       lease_expires_at = NULL,
                       heartbeat_at     = NULL
                 WHERE id = ? AND status = 'dead'
                """,
                (job_id,),
            )
            self._audit(
                actor=actor,
                actor_ref=actor_ref,
                action="job.requeue",
                target_type="job",
                target_id=job_id,
                task_id=job.task_id,
                before={
                    "status": DEAD_STATUS,
                    "attempts": job.attempts,
                    "error_code": job.error_code,
                },
                after={"status": CLAIMABLE_STATUS, "attempts": 0},
                reason=reason,
                source=source,
            )

    # ── publish 池限频守卫 ──────────────────────────────────────────────

    def rate_limit_state(
        self,
        *,
        account_id: str,
        daily_limit: int,
        min_gap_min: int,
        now: datetime | None = None,
    ) -> RateLimitState:
        """发布限频守卫（§03.4.4 ⑥ · §3.3.15）：``≤daily_limit 条/天/账号`` + 间隔 ``≥min_gap_min``。

        **按本地日**统计（``rate_limit_json.window = 'local_day'``）：DB 里存的是 UTC，
        所以先把时间戳偏移到本地时区再取日期部分 —— 直接用 ``substr(utc,1,10)``
        会把北京时间 08:00 前发布的算进前一天。
        """
        moment = now or utc_now()
        offset_delta = moment.astimezone(local_tz()).utcoffset() or timedelta()
        offset = f"{int(offset_delta.total_seconds() // 60):+d} minutes"
        local_today = moment.astimezone(local_tz()).strftime("%Y-%m-%d")
        placeholders = ",".join("?" for _ in _RATE_LIMIT_STATUSES)

        used_today = int(
            self._connection.execute(
                f"""
                SELECT count(*) FROM publications
                 WHERE account_id = ?
                   AND status IN ({placeholders})
                   AND substr(datetime(COALESCE(published_at, created_at), ?), 1, 10) = ?
                """,
                (account_id, *_RATE_LIMIT_STATUSES, offset, local_today),
            ).fetchone()[0]
        )

        if used_today >= daily_limit:
            next_local_midnight = (moment.astimezone(local_tz()) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return RateLimitState(
                allowed=False,
                used_today=used_today,
                daily_limit=daily_limit,
                next_allowed_at=format_iso(next_local_midnight.astimezone(UTC)),
                reason="daily_limit",
            )

        last = self._connection.execute(
            f"""
            SELECT max(COALESCE(published_at, created_at)) AS last_at
              FROM publications
             WHERE account_id = ? AND status IN ({placeholders})
            """,
            (account_id, *_RATE_LIMIT_STATUSES),
        ).fetchone()["last_at"]
        if last is not None and min_gap_min > 0:
            earliest = parse_iso(str(last)) + timedelta(minutes=min_gap_min)
            if earliest > moment:
                return RateLimitState(
                    allowed=False,
                    used_today=used_today,
                    daily_limit=daily_limit,
                    next_allowed_at=format_iso(earliest),
                    reason="min_gap",
                )

        return RateLimitState(allowed=True, used_today=used_today, daily_limit=daily_limit)

    # ── 内部：池参数 / 告警 / 审计 ──────────────────────────────────────

    def _runtime(self, pool: str) -> PoolRuntime:
        row = self._connection.execute("SELECT * FROM pool_settings WHERE pool = ?", (pool,)).fetchone()
        if row is None:
            raise StudioError(
                f"池参数缺失：{pool}",
                code=ErrorCode.DB_SCHEMA_DRIFT,
                context={"pool": pool},
                remediation="`pool_settings` 应有 draft/voice/render/publish 四行（0006_seed.sql）",
            )
        return PoolRuntime(
            pool=str(row["pool"]),
            concurrency=int(row["concurrency"]),
            paused=bool(row["paused"]),
            lease_sec=int(row["lease_sec"]),
            max_attempts=int(row["max_attempts"]),
            backoff_base_ms=int(row["backoff_base_ms"]),
            backoff_max_ms=int(row["backoff_max_ms"]),
            rate_limit=_loads(_opt(row, "rate_limit_json"), {}),
            paused_at=_opt(row, "paused_at"),
            paused_by=_opt(row, "paused_by"),
            consecutive_oom=int(row["consecutive_oom"]),
        )

    # ── 内部：自动降并发（T4.10 · §01.4.4）──────────────────────────────

    def _bump_oom_streak(self, pool: str) -> int:
        """``consecutive_oom`` +1 并回读（同一事务内，读到的就是刚写进去的值）。"""
        self._connection.execute(
            "UPDATE pool_settings SET consecutive_oom = consecutive_oom + 1 WHERE pool = ?",
            (pool,),
        )
        row = self._connection.execute(
            "SELECT consecutive_oom FROM pool_settings WHERE pool = ?", (pool,)
        ).fetchone()
        return 0 if row is None else int(row["consecutive_oom"])

    def _reset_oom_streak(self, pool: str | None) -> None:
        """一条成功 ⇒ 连续 OOM 归零（``<> 0`` 守卫：不写没意义的行）。"""
        if pool is None:
            return
        self._connection.execute(
            "UPDATE pool_settings SET consecutive_oom = 0 WHERE pool = ? AND consecutive_oom <> 0",
            (pool,),
        )

    def _maybe_autodegrade(self, pool: str, error_code: str, moment: datetime) -> None:
        """连续 OOM 达阈值 ⇒ 并发 −1 + ``POOL_AUTODEGRADED`` 告警。

        为什么判据是 ``== threshold`` 而不是 ``>=``
        ------------------------------------------
        ``>=`` 会在**每一次**后续 OOM 上重复告警、重复 −1（阈值 2 时连来 5 次
        OOM ⇒ 告警 4 次、并发降到下限）。``==`` 是"刚好越过线"的那一次；
        计数器由 ``succeed()`` 归零 ⇒ 下一次越线必然发生在"池又成功过一次"之后。

        为什么降不动（已在 :data:`POOL_CONCURRENCY_MIN`）也要告警
        ---------------------------------------------------------
        "降不下去"如果静默，故障就变成"面板上并发一直是 1，任务一直 OOM"——
        人只会以为任务本身有问题。告警里明说"已在并发下限"，把矛头指向显存。
        """
        if not self._auto.enabled:
            return
        streak = self._bump_oom_streak(pool)
        threshold = int(self._auto.oom_threshold)
        if streak != threshold:
            return

        runtime = self._runtime(pool)
        at_floor = runtime.concurrency <= POOL_CONCURRENCY_MIN
        target = runtime.concurrency if at_floor else runtime.concurrency - 1
        if not at_floor:
            self._connection.execute(
                """
                UPDATE pool_settings
                   SET concurrency = ?, updated_at = ?, updated_by = ?
                 WHERE pool = ?
                """,
                (target, format_iso(moment), AUTO_ACTOR, pool),
            )
        self._alert(
            AlertCode.POOL_AUTODEGRADED,
            pool=pool,
            message=(
                f"{pool} 池连续 {streak} 次 {error_code} ⇒ 并发 "
                + ("已在下限，保持不变" if at_floor else f"{runtime.concurrency} → {target}")
            ),
            hint=(
                f"阈值 {threshold} 来自 config/pools.yaml → auto_concurrency.oom_threshold；"
                + (
                    "并发已是下限，请先清显存 / 降分辨率再往上调"
                    if at_floor
                    else "在「四池调度」面板可调回（需人工确认，避免升降抖动）"
                )
            ),
        )
        self._audit(
            actor="auto",
            actor_ref=AUTO_ACTOR,
            action="pool.autodegrade",
            target_type="pool",
            target_id=pool,
            before={"concurrency": runtime.concurrency, "consecutive_oom": streak},
            after={"concurrency": target, "threshold": threshold, "error_code": error_code},
            reason=f"连续 {streak} 次 {error_code} 触发自动降并发",
            source="worker",
            at=moment,
        )
        logger.warning(
            "queue.pool_autodegraded",
            pool=pool,
            previous=runtime.concurrency,
            concurrency=target,
            streak=streak,
            error_code=error_code,
        )

    def _alert(
        self,
        code: AlertCode,
        *,
        message: str,
        hint: str | None = None,
        pool: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """写 ``system_logs`` 的告警行（WS 层按 ``payload_json.code`` 识别为 ``system.alert``）。

        与 job 状态变更**同事务**落库 ⇒ 不会出现"状态变了但没告警"或反之。
        """
        severity = ALERT_SEVERITY[code]
        payload = {"code": str(code), "severity": severity, "message": message, "hint": hint}
        self._connection.execute(
            """
            INSERT INTO system_logs(level, source, task_id, job_id, stage, unit_ref,
                                    message, payload_json, worker_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                severity,
                f"pool.{pool}" if pool else "system.alert",
                task_id,
                job_id,
                pool,
                code.value,
                message,
                json.dumps(payload, ensure_ascii=False),
                None,
            ),
        )

    def _audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        actor_ref: str | None = None,
        task_id: str | None = None,
        reason: str | None = None,
        source: str = "webui",
        at: datetime | None = None,
    ) -> None:
        """写一条留痕。``at`` 缺省交给 DDL 的 ``strftime('now')``。

        调用方**显式**给了时刻（``now=`` / 判死那一拍的 ``moment``）时必须一路带进来：
        同一次变更在 ``pool_settings.updated_at`` 与 ``audit_ops.at`` 上留两个不同的
        时间戳，读留痕的人（T4.11 的并发回升冷却锚点 `_concurrency_anchor`）就会按
        "另一个时钟"算冷却 —— 测试无法把那一刻摆到想要的相对位置上。
        """
        self._connection.execute(
            """
            INSERT INTO audit_ops(at, actor, actor_ref, action, target_type, target_id, task_id,
                                  before_json, after_json, result, reason, source)
            VALUES (COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')), ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?)
            """,
            (
                None if at is None else format_iso(at),
                actor,
                actor_ref,
                action,
                target_type,
                target_id,
                task_id,
                json.dumps(dict(before), ensure_ascii=False),
                json.dumps(dict(after), ensure_ascii=False),
                reason,
                source,
            ),
        )
