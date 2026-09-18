"""`system_logs` 的唯一应用层写入口 + 只读查询（T1.7 · §04.5.2 / §04.4.6）。

顺序铁律：**先落库，再广播**（陷阱 #13）
----------------------------------------
本模块的 `append()` **只**做两件事：写 `system_logs` → 叫醒 Hub。
它**不**负责"怎么发"—— 那是 Hub 的事，而且 Hub 是从**表里**按 `id > cursor` 捞的。
于是"先落库再广播"不再是需要每个调用点自觉遵守的纪律，而是结构上做不到违反。

为什么不做"写完直接推送"
------------------------
worker 是**独立进程**，它写的 `pool.*` / `tts.*` / `render.*` 日志没有 IPC 通道能推给
API 进程里的 Hub。让 Hub 以表为真相源，跨进程日志自动到前端，且天然满足"落库优先"。

告警怎么写
----------
`alert()` 沿用 `db.queue.JobStore._alert()` 的载荷约定：
`payload_json = {code, severity, message, hint}` ⇒ Hub 依此识别为 `system.alert`。
`code` 必须是 §04.5.2 的 `AlertCode` 之一；`WORKER_DEAD` 这类**不在枚举内**的码
走普通 `append(level="error", payload={"code": ...})` ⇒ 只会是 `log.appended`（裁定 39）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from studio.core.clock import format_iso, utc_now
from studio.core.errors import ErrorCode, StudioError
from studio.core.proto import ALERT_SEVERITY, AlertCode, Severity

__all__ = [
    "DB_MIN_LEVEL",
    "LIKE_ESCAPE",
    "LogService",
    "LogSink",
    "SystemLog",
    "is_alert_code",
    "like_pattern",
]

#: `config/logging.yaml: database.min_level` —— 低于此级别不落库（防日志表膨胀）
DB_MIN_LEVEL: Final[str] = "info"

#: 级别序（与 `ws/protocol.SEVERITY_ORDER` 同源；这里只为"是否落库"服务）
_LEVELS: Final[Mapping[str, int]] = {"debug": 10, "info": 20, "warn": 30, "error": 40, "fatal": 50}

#: `LIKE ... ESCAPE` 的转义符（T4.9 搜索）。
#: 选反斜杠是因为它**不在**任何日志文本的常见字符集里 —— 用户搜 `50%` 时
#: 那颗 `%` 必须当字面量，否则会变成通配符，把整张表都捞回来。
LIKE_ESCAPE: Final[str] = "\\"


def like_pattern(text: str) -> str:
    """把用户输入变成 `LIKE` 的**字面量**子串模式（`%x%`，转义 `\\` `%` `_`）。

    顺序要紧：**先转义反斜杠**再转义通配符，否则会把刚加上的转义符又转一遍。
    """
    escaped = text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, LIKE_ESCAPE + wildcard)
    return f"%{escaped}%"


class LogSink(Protocol):
    """日志出口的结构类型（``LogService.append`` 满足它）。

    为什么和 ``agents/gateway.py`` 里那份长得一样却要写两遍
    ------------------------------------------------------
    ``agents/`` 在 ``services/`` **之下**（§02.1），它不能 import 本模块 ——
    否则 Worker 里跑一个 Agent 就要把整条 API 依赖链拉起来。两份协议形状相同，
    ``LogService.append`` 同时满足两者，注入点因此可以互换。
    """

    def __call__(
        self,
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any: ...


def is_alert_code(code: str | None) -> bool:
    """`payload_json.code` 是否是 §04.5.2 的告警码之一。"""
    if not code:
        return False
    return code in {item.value for item in AlertCode}


@dataclass(frozen=True, slots=True)
class SystemLog:
    """`system_logs` 一行（WS 推送 / REST 分页 / 日志面板共用）。"""

    id: int
    ts: str
    level: str
    source: str
    message: str
    task_id: str | None = None
    job_id: str | None = None
    stage: str | None = None
    unit_ref: str | None = None
    worker_id: str | None = None
    trace_id: str | None = None
    duration_ms: int | None = None
    seq_in_task: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SystemLog:
        raw = row["payload_json"]
        payload: Mapping[str, Any] = {}
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_unparsable": raw}
            if isinstance(parsed, dict):
                payload = parsed
        return cls(
            id=int(row["id"]),
            ts=str(row["ts"]),
            level=str(row["level"]),
            source=str(row["source"]),
            message=str(row["message"]),
            task_id=row["task_id"],
            job_id=row["job_id"],
            stage=row["stage"],
            unit_ref=row["unit_ref"],
            worker_id=row["worker_id"],
            trace_id=row["trace_id"],
            duration_ms=row["duration_ms"],
            seq_in_task=row["seq_in_task"],
            payload=payload,
        )

    # ── 分类 ────────────────────────────────────────────────────────
    @property
    def alert_code(self) -> str | None:
        """`payload_json.code`（非告警 ⇒ `None`）。"""
        code = self.payload.get("code")
        return code if isinstance(code, str) else None

    @property
    def is_alert(self) -> bool:
        """是否升格为 `system.alert`（§04.5.2 的告警码枚举）。"""
        return is_alert_code(self.alert_code)

    @property
    def hint(self) -> str | None:
        hint = self.payload.get("hint")
        return hint if isinstance(hint, str) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "stage": self.stage,
            "unit_ref": self.unit_ref,
            "worker_id": self.worker_id,
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "seq_in_task": self.seq_in_task,
            "payload": dict(self.payload),
        }


class LogService:
    """`system_logs` 的门面（写入 + 查询）。

    :param connection: 一条连接，**或**"按线程取连接"的工厂（`ThreadLocalConnections.get`）。
        应用进程里事件循环线程与线程池都要用库，而 `sqlite3` 连接线程亲和
        （T1.5 裁定 27 / T1.6 裁定 41），所以生产一律传工厂；测试传单条连接即可。
    :param waker: 落库后叫醒 Hub（`Hub.wake`）；`None` ⇒ 不叫醒（测试 / 一次性脚本）
    :param db_min_level: 低于此级别不落库（`logging.yaml: database.min_level`）
    """

    def __init__(
        self,
        connection: sqlite3.Connection | Callable[[], sqlite3.Connection],
        *,
        waker: Callable[[], None] | None = None,
        db_min_level: str = DB_MIN_LEVEL,
    ) -> None:
        self._provider: Callable[[], sqlite3.Connection] = (
            (lambda: connection) if isinstance(connection, sqlite3.Connection) else connection
        )
        self._waker = waker
        self._db_min_level = db_min_level

    @property
    def _connection(self) -> sqlite3.Connection:
        """当前线程的连接（工厂每次调用都只做一次字典查找，不建连接）。"""
        return self._provider()

    def attach_waker(self, waker: Callable[[], None] | None) -> None:
        """后挂叫醒回调（`app/deps.py` 里 Hub 建好之后才拿得到 `hub.wake`）。"""
        self._waker = waker

    # ── 写 ──────────────────────────────────────────────────────────
    def append(
        self,
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        job_id: str | None = None,
        stage: str | None = None,
        unit_ref: str | None = None,
        payload: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
        trace_id: str | None = None,
        duration_ms: int | None = None,
        seq_in_task: int | None = None,
        now: datetime | None = None,
    ) -> SystemLog | None:
        """落库一条日志并叫醒 Hub；低于 `db_min_level` ⇒ 返回 `None`（不落库）。

        返回值是**已落库**的行（带 `id`）—— 调用方可以拿它做后续断言，
        但**不要**拿它去推送（推送一律由 Hub 从表里读）。
        """
        if _LEVELS.get(level, 20) < _LEVELS.get(self._db_min_level, 20):
            return None
        moment = now or utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO system_logs(ts, level, source, task_id, job_id, stage, unit_ref,
                                    message, payload_json, worker_id, trace_id, duration_ms,
                                    seq_in_task)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                format_iso(moment),
                level,
                source,
                task_id,
                job_id,
                stage,
                unit_ref,
                message,
                None if payload is None else json.dumps(dict(payload), ensure_ascii=False),
                worker_id,
                trace_id,
                duration_ms,
                seq_in_task,
            ),
        )
        log_id = int(cursor.lastrowid or 0)
        # 连接是 autocommit（`isolation_level=None`，§03.7.2）⇒ 此处已对其他进程可见，
        # 这正是 Hub 的 tail 能立刻捞到这一行的前提。
        row = self.get(log_id)
        if self._waker is not None:
            self._waker()
        return row

    def alert(
        self,
        code: AlertCode,
        *,
        message: str,
        hint: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
        pool: str | None = None,
        now: datetime | None = None,
    ) -> SystemLog | None:
        """写一条 `system.alert`（载荷约定与 `JobStore._alert` 完全一致）。"""
        severity = ALERT_SEVERITY[code]
        payload = {"code": str(code), "severity": severity, "message": message, "hint": hint}
        return self.append(
            level=severity,
            source=f"pool.{pool}" if pool else "system.alert",
            message=message,
            task_id=task_id,
            job_id=job_id,
            stage=pool,
            unit_ref=str(code),
            payload=payload,
            now=now,
        )

    # ── 读（Hub / REST 共用）────────────────────────────────────────
    def get(self, log_id: int) -> SystemLog | None:
        row = self._connection.execute("SELECT * FROM system_logs WHERE id = ?", (log_id,)).fetchone()
        return None if row is None else SystemLog.from_row(row)

    def latest_id(self) -> int:
        """当前最大 `id`（Hub 的 tail 起点：只推"启动之后"的新行）。"""
        row = self._connection.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM system_logs").fetchone()
        return int(row["latest"]) if row is not None else 0

    def since(
        self,
        log_id: int,
        *,
        limit: int,
        min_level: str | None = None,
        task_id: str | None = None,
        source: str | None = None,
        search: str | None = None,
    ) -> tuple[SystemLog, ...]:
        """`id > log_id` 的增量（按 id 升序；断线重连补发用）。"""
        sql, params = self._filtered(
            "id > ?", [log_id], min_level=min_level, task_id=task_id, source=source, search=search
        )
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        return tuple(SystemLog.from_row(row) for row in self._connection.execute(sql, params))

    def recent(
        self,
        *,
        limit: int,
        min_level: str | None = None,
        task_id: str | None = None,
        source: str | None = None,
        search: str | None = None,
        until_id: int | None = None,
    ) -> tuple[SystemLog, ...]:
        """最近 `limit` 条（按 id 升序返回，前端可直接渲染）。

        :param until_id: 窗口上界（含）。导出与“往前翻历史”都靠它把窗口钉在某个时刻之前
            —— 少了它，“导出到这条为止”就只能由调用方自己截断，而“截断”与“查询”
            的差别恰恰在分页边界上露出来（漏一条或重一条）。
        """
        sql, params = self._filtered(
            "1 = 1" if until_id is None else "id <= ?",
            [] if until_id is None else [until_id],
            min_level=min_level,
            task_id=task_id,
            source=source,
            search=search,
        )
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = list(self._connection.execute(sql, params))
        rows.reverse()
        return tuple(SystemLog.from_row(row) for row in rows)

    @staticmethod
    def _filtered(
        where: str,
        params: list[Any],
        *,
        min_level: str | None,
        task_id: str | None,
        source: str | None = None,
        search: str | None = None,
    ) -> tuple[str, list[Any]]:
        sql = f"SELECT * FROM system_logs WHERE {where}"
        if min_level is not None:
            allowed = [name for name, rank in _LEVELS.items() if rank >= _LEVELS.get(min_level, 20)]
            placeholders = ", ".join("?" for _ in allowed)
            sql += f" AND level IN ({placeholders})"
            params.extend(sorted(allowed, key=lambda name: _LEVELS[name]))
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        if search:
            # `source` 也一起搜：用户打 "render" 时想看到的是 render.ffmpeg 那一族，
            # 只搜 message 会把它们全漏掉。
            sql += f" AND (message LIKE ? ESCAPE '{LIKE_ESCAPE}'"
            sql += f" OR source LIKE ? ESCAPE '{LIKE_ESCAPE}')"
            pattern = like_pattern(search)
            params.extend([pattern, pattern])
        return sql, params

    # ── 兜底：表不存在等结构性错误 ⇒ 明确报错，不静默返回空 ──────────
    def require_table(self) -> None:
        try:
            self.latest_id()
        except sqlite3.OperationalError as exc:
            raise StudioError(
                "system_logs 表不可用（先跑 `studio db migrate`）",
                code=ErrorCode.DB_SCHEMA_DRIFT,
                context={"error": str(exc)},
            ) from exc


def ndjson_lines(rows: Sequence[SystemLog]) -> str:
    """把日志行渲染成 NDJSON（T4.9 的导出复用；此处只做纯函数）。"""
    return "".join(json.dumps(row.to_dict(), ensure_ascii=False) + "\n" for row in rows)
