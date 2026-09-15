"""Worker 心跳与存活判定（T1.6 · §04.5.1）。

职责边界（别混）
----------------
- **写** ``worker_heartbeats``：本模块是**唯一**写入方（契约测试锁死）。
- **判活**：``last_seen_at`` 超 15s ⇒ ``dead``（§04.5.1）。
- **不管 job 租约**：那是 ``db/lease.py`` + ``db/queue.py`` 的 ``renew()``。
  两者**职责不同** —— 心跳报"进程还活着"，租约保"这个 job 还归我"。
  一个进程活着但租约被 sweeper 回收是完全可能的（比如卡在长 I/O 上没续租）。

不变式：**行存在 ⇔ 该进程应当在跑**
------------------------------------
优雅退出时 worker **删掉**自己那行（:meth:`HeartbeatStore.forget`），
所以"行还在但 15s 没动静"就一定是猝死 ⇒ 判死 + 告警，不会误报。

关于 ``WORKER_DEAD`` 为什么不是 ``system.alert``
----------------------------------------------
§04.5.2 把 ``system.alert.code`` 锁死为 8 个值（T1.5 施工裁定 31），
所以 worker 猝死落 ``system_logs`` 的 ``error`` 行 + ``payload_json.code='WORKER_DEAD'``；
T1.7 的 WS 层按"不在 8 值内 ⇒ 走 ``log.append``（level=error）"处理，不升格为 alert。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import psutil

from studio.core.clock import format_iso, parse_iso, utc_now
from studio.core.logging import get_logger
from studio.db import WorkerHeartbeat

__all__ = [
    "HEARTBEAT_INTERVAL_SEC",
    "HEARTBEAT_TIMEOUT_SEC",
    "RSS_GROWTH_RATIO",
    "RSS_MIN_GROWTH_MB",
    "RSS_STRIKES",
    "STALE_RETENTION_SEC",
    "WORKER_DEAD_CODE",
    "WORKER_STATUSES",
    "HeartbeatStore",
    "ProcessStats",
    "RssLeakWatch",
    "WorkerIdentity",
]

logger = get_logger("studio.pools.heartbeat")

#: 心跳周期（§04.5.1）：worker 每 5s upsert 一次
HEARTBEAT_INTERVAL_SEC: Final[float] = 5.0

#: 判死阈值 = 3 个心跳周期（容忍连丢两拍，不误杀慢进程）
HEARTBEAT_TIMEOUT_SEC: Final[float] = 15.0

#: ``worker_heartbeats.status`` 合法取值 —— 必须与 DDL 的 CHECK **逐字一致**
WORKER_STATUSES: Final[frozenset[str]] = frozenset({"idle", "busy", "draining", "dead"})

#: 猝死日志的 ``payload_json.code``（**不是** ``system.alert.code``，见模块 docstring）
WORKER_DEAD_CODE: Final[str] = "WORKER_DEAD"

#: 死心跳保留时长（由 T4.12 的 ``gc_service`` 调用 :meth:`HeartbeatStore.purge`）
STALE_RETENTION_SEC: Final[int] = 24 * 3600

#: RSS 泄漏判定：相对基线涨 ≥50% **且** 绝对涨 ≥256MB，连续 3 拍 ⇒ 告警一次
RSS_GROWTH_RATIO: Final[float] = 0.50
RSS_MIN_GROWTH_MB: Final[int] = 256
RSS_STRIKES: Final[int] = 3

_MB: Final[int] = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """``<pool>#<slot>@<pid>``（§04.5.1 的 ``worker_id`` 格式）。"""

    pool: str
    slot: int
    pid: int

    @property
    def worker_id(self) -> str:
        return f"{self.pool}#{self.slot}@{self.pid}"

    @classmethod
    def parse(cls, worker_id: str) -> WorkerIdentity:
        """反向解析；格式不对 ⇒ ``ValueError``（不静默给默认值）。"""
        head, _, pid_text = worker_id.rpartition("@")
        pool, _, slot_text = head.partition("#")
        if not pool or not slot_text or not pid_text:
            raise ValueError(f"worker_id 形如 <pool>#<slot>@<pid>：{worker_id!r}")
        try:
            slot = int(slot_text)
            pid = int(pid_text)
        except ValueError as exc:
            raise ValueError(f"worker_id 的 slot / pid 必须是整数：{worker_id!r}") from exc
        return cls(pool=pool, slot=slot, pid=pid)

    @classmethod
    def current(cls, pool: str, slot: int = 1, *, pid: int | None = None) -> WorkerIdentity:
        """本进程的身份（``pid=None`` ⇒ 取当前进程）。"""
        return cls(pool=pool, slot=slot, pid=os.getpid() if pid is None else pid)


@dataclass(frozen=True, slots=True)
class ProcessStats:
    """进程资源画像（RSS 用于识别泄漏；采样失败一律给 0，绝不抛）。"""

    rss_mb: int = 0
    cpu_percent: float = 0.0

    @classmethod
    def sample(cls, pid: int | None = None) -> ProcessStats:
        try:
            process = psutil.Process(os.getpid() if pid is None else pid)
            with process.oneshot():
                rss = int(process.memory_info().rss)
                cpu = float(process.cpu_percent(interval=None))
        except (psutil.Error, OSError, ValueError):
            # ValueError：psutil 对非法 pid 直接抛（不是 psutil.Error）；同样必须吞掉
            return cls()
        return cls(rss_mb=rss // _MB, cpu_percent=round(cpu, 1))


@dataclass(slots=True)
class RssLeakWatch:
    """RSS 持续增长（worker 泄漏）判定：低水位基线 + 连续超阈计数。

    为什么用"低水位基线"而不是"启动时快照"：正常 worker 的内存是**阶梯式**上升的
    （每次加载模型/滤镜上一次台阶），拿启动值当基线会天天误报；取运行期最低点作基线，
    再要求"连续 3 拍都在基线 +50% 以上"，才既灵敏又不吵。
    """

    growth_ratio: float = RSS_GROWTH_RATIO
    min_growth_mb: int = RSS_MIN_GROWTH_MB
    strikes_to_warn: int = RSS_STRIKES
    baseline_mb: int | None = None
    strikes: int = 0

    def observe(self, rss_mb: int) -> bool:
        """喂一个采样；``True`` ⇒ 这一拍该告警（并自动抬高基线，防每 5s 刷屏）。"""
        if rss_mb <= 0:
            return False
        if self.baseline_mb is None or rss_mb < self.baseline_mb:
            self.baseline_mb = rss_mb
            self.strikes = 0
            return False
        threshold = max(
            int(self.baseline_mb * (1.0 + self.growth_ratio)),
            self.baseline_mb + self.min_growth_mb,
        )
        if rss_mb < threshold:
            self.strikes = 0
            return False
        self.strikes += 1
        if self.strikes < self.strikes_to_warn:
            return False
        self.strikes = 0
        self.baseline_mb = rss_mb
        return True


class HeartbeatStore:
    """``worker_heartbeats`` 的读写面（**唯一写入方**）。

    全部方法都是单语句 ⇒ 依赖 autocommit 的原子性，不额外开事务
    （R10 纪律：事务只留给"必须一起成功"的多语句场景）。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 写 ──────────────────────────────────────────────────────────────

    def upsert(
        self,
        *,
        worker_id: str,
        pool: str,
        status: str,
        current_job_id: str | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        gpu_mem_mb: int | None = None,
        cpu_percent: float | None = None,
        rss_mb: int | None = None,
        started_at: str | None = None,
        version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """写一拍心跳。

        ``started_at`` 用 ``COALESCE(旧值, 新值)``：进程重启后同一个 ``worker_id``
        （同 pid 复用）也不该把"最早启动时间"改晚 —— 那是判断重启频率的依据。
        """
        if status not in WORKER_STATUSES:
            raise ValueError(f"非法 worker 状态 {status!r}；合法值：{sorted(WORKER_STATUSES)}")
        moment = now or utc_now()
        self._connection.execute(
            """
            INSERT INTO worker_heartbeats(worker_id, pool, pid, hostname, status, current_job_id,
                                          last_seen_at, gpu_mem_mb, cpu_percent, rss_mb,
                                          started_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
              pool           = excluded.pool,
              pid            = excluded.pid,
              hostname       = excluded.hostname,
              status         = excluded.status,
              current_job_id = excluded.current_job_id,
              last_seen_at   = excluded.last_seen_at,
              gpu_mem_mb     = excluded.gpu_mem_mb,
              cpu_percent    = excluded.cpu_percent,
              rss_mb         = excluded.rss_mb,
              started_at     = COALESCE(worker_heartbeats.started_at, excluded.started_at),
              version        = excluded.version
            """,
            (
                worker_id,
                pool,
                pid,
                hostname if hostname is not None else socket.gethostname(),
                status,
                current_job_id,
                format_iso(moment),
                gpu_mem_mb,
                cpu_percent,
                rss_mb,
                started_at,
                version,
            ),
        )

    def forget(self, worker_id: str) -> bool:
        """优雅退出时摘掉自己那行（**不变式：行存在 ⇔ 进程应当在跑**）。"""
        cursor = self._connection.execute("DELETE FROM worker_heartbeats WHERE worker_id = ?", (worker_id,))
        return cursor.rowcount == 1

    def mark_dead(self, *, worker_id: str, now: datetime | None = None) -> bool:
        """标死但**保留** ``last_seen_at``（那是最后一次真实接触的时间，不能被覆盖）。

        ``current_job_id`` 同样保留：排查"它死在哪一步"就靠这个字段。
        """
        del now  # 语义上不需要写时间戳；留参数是为了与其它方法签名一致
        cursor = self._connection.execute(
            "UPDATE worker_heartbeats SET status = 'dead' WHERE worker_id = ? AND status <> 'dead'",
            (worker_id,),
        )
        return cursor.rowcount == 1

    def purge(self, *, now: datetime | None = None, keep_sec: int = STALE_RETENTION_SEC) -> int:
        """清掉久远的 ``dead`` 行（T4.12 的 GC 调用；活行永不删）。"""
        moment = now or utc_now()
        cutoff = format_iso(moment - timedelta(seconds=keep_sec))
        cursor = self._connection.execute(
            "DELETE FROM worker_heartbeats WHERE status = 'dead' AND last_seen_at < ?", (cutoff,)
        )
        return int(cursor.rowcount)

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, worker_id: str) -> WorkerHeartbeat | None:
        row = self._connection.execute(
            "SELECT * FROM worker_heartbeats WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        return None if row is None else WorkerHeartbeat.from_row(row)

    def list_workers(self, *, pool: str | None = None) -> tuple[WorkerHeartbeat, ...]:
        sql = "SELECT * FROM worker_heartbeats"
        params: list[Any] = []
        if pool is not None:
            sql += " WHERE pool = ?"
            params.append(pool)
        sql += " ORDER BY pool ASC, worker_id ASC"
        return tuple(WorkerHeartbeat.from_row(row) for row in self._connection.execute(sql, params))

    def stale(
        self,
        *,
        now: datetime | None = None,
        timeout_sec: float = HEARTBEAT_TIMEOUT_SEC,
        pool: str | None = None,
    ) -> tuple[WorkerHeartbeat, ...]:
        """超时未报心跳的**活**worker（``dead`` 的不重复算）。"""
        moment = now or utc_now()
        cutoff = format_iso(moment - timedelta(seconds=timeout_sec))
        sql = "SELECT * FROM worker_heartbeats WHERE status <> 'dead' AND last_seen_at < ?"
        params: list[Any] = [cutoff]
        if pool is not None:
            sql += " AND pool = ?"
            params.append(pool)
        sql += " ORDER BY last_seen_at ASC"
        return tuple(WorkerHeartbeat.from_row(row) for row in self._connection.execute(sql, params))

    # ── 判死 ────────────────────────────────────────────────────────────

    def reap_stale(
        self,
        *,
        now: datetime | None = None,
        timeout_sec: float = HEARTBEAT_TIMEOUT_SEC,
        pool: str | None = None,
    ) -> tuple[str, ...]:
        """判死一批超时 worker 并留痕（supervisor 每轮调用，§04.5.1）。

        :return: 本轮**新**判死的 ``worker_id``（已经是 ``dead`` 的不重复返回 ⇒ 幂等）
        """
        moment = now or utc_now()
        reaped: list[str] = []
        for beat in self.stale(now=moment, timeout_sec=timeout_sec, pool=pool):
            if not self.mark_dead(worker_id=beat.worker_id):
                continue
            reaped.append(beat.worker_id)
            self._log_dead(beat, moment, timeout_sec=timeout_sec)
        if reaped:
            logger.warning("worker.reaped", pool=pool or "all", workers=list(reaped))
        return tuple(reaped)

    def _log_dead(self, beat: WorkerHeartbeat, moment: datetime, *, timeout_sec: float) -> None:
        silent = (moment - parse_iso(beat.last_seen_at)).total_seconds()
        message = f"worker 心跳超时被判死：{beat.worker_id}（{silent:.0f}s 无心跳，阈值 {timeout_sec:.0f}s）"
        payload = {
            "code": WORKER_DEAD_CODE,
            "severity": "error",
            "message": message,
            "hint": "supervisor 会重启该 worker；其 job 租约由 sweeper 回收后重排（双保险）",
            "worker_id": beat.worker_id,
            "pool": beat.pool,
            "last_seen_at": beat.last_seen_at,
            "silent_sec": round(silent, 3),
            "current_job_id": beat.current_job_id,
        }
        self._connection.execute(
            """
            INSERT INTO system_logs(level, source, job_id, stage, message, payload_json, worker_id)
            VALUES ('error', ?, ?, ?, ?, ?, ?)
            """,
            (
                f"pool.{beat.pool}",
                beat.current_job_id,
                beat.pool,
                message,
                json.dumps(payload, ensure_ascii=False),
                beat.worker_id,
            ),
        )
