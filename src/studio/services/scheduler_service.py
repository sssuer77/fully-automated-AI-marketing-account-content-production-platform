"""定时发布调度（T5.6 · §04.6.5.1 / §06.5.5）。

调度器只做一件事：**到点建 job**
--------------------------------
它不发布、不认领、不重试 —— 那些全是 §03.4 的队列纪律。这里做的只有
「读表 -> 算下一次 -> 建作业 -> 写回」，于是"调度器挂了会不会把在途的发布弄坏"
这个问题根本不存在：它挂了只是没有新作业被建出来，已经在跑的照跑。

为什么 ``next_run_at`` 在库里而不是内存里
----------------------------------------
陷阱 #30：进程重启即丢计划。内存版的代价很具体 —— 重启一次，"明天 19:12 发"
就没了，而且没有任何地方报错（面板上还显示着旧时刻，因为那一份也是内存）。

三种模式 + 抖动 + 窗口随机
--------------------------
时刻算术全在 :mod:`studio.domain.schedule`（纯函数、可单测），这里只负责把它接到
库与队列上。**定时 != 免限频**（§04.6.5.1「与限频关系」）：到点了也要过
``JobStore.rate_limit_state``，被挡下就记 ``skipped_ratelimit`` 并顺延 ——
那**不是失败**，所以不计 ``fail_streak``。

连续失败要吵醒人
----------------
``error:…`` 连续 5 次 ⇒ 写一条 ``system.alert(SCHEDULE_FAILING)``。少了它，
一个写错平台名的计划会安安静静地每天空转一次，直到有人想起来去看面板。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from studio.core.clock import now_iso, parse_iso, utc_now
from studio.core.config import AccountConfig, PublishConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, AlertCode, EventKind
from studio.db import JobStore
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import PublicationRepo
from studio.db.repositories.schedule_repo import ScheduleRepo, ScheduleRow
from studio.domain.enums import UnitType
from studio.domain.schedule import (
    DEFAULT_JITTER_MIN,
    MAX_INTERVAL_HOURS,
    MODES,
    next_run_at,
    parse_at_time,
    validate_jitter,
    validate_window,
)
from studio.domain.task_service import TaskService
from studio.services.log_service import LogSink
from studio.services.publish_service import enqueue_publications

__all__ = [
    "AUDIT_CREATE",
    "AUDIT_DELETE",
    "AUDIT_DISABLE",
    "AUDIT_UPDATE",
    "FAIL_STREAK_ALERT",
    "LOG_SOURCE",
    "RESULT_OK",
    "RESULT_SKIPPED_DISABLED",
    "RESULT_SKIPPED_DUPLICATE",
    "RESULT_SKIPPED_RATELIMIT",
    "SCHEDULER_BATCH_LIMIT",
    "SCHEDULER_TICK_INTERVAL_SEC",
    "ScheduleFireResult",
    "ScheduleTickReport",
    "SchedulerService",
    "create_schedule",
    "delete_schedule",
    "run_now",
    "update_schedule",
]

logger = get_logger("studio.services.scheduler")

#: 一拍间隔（§04.6.5.1「30s tick」）。窗口精度是**分钟**，30s 一拍已经细一个量级；
#: 再密只是空转（每拍一条索引查询）。
SCHEDULER_TICK_INTERVAL_SEC: Final[float] = 30.0

#: 一拍最多处理几条到点计划（防止积压时一次建出一堆作业）。
SCHEDULER_BATCH_LIMIT: Final[int] = 20

#: 连续失败到这个数 ⇒ ``system.alert``（§04.6.5.1）。
FAIL_STREAK_ALERT: Final[int] = 5

#: 发布池名（与 ``publish_service.PUBLISH_POOL`` 同值）。
PUBLISH_POOL: Final[str] = "publish"

#: 留痕动作名（§06.5.5 点名了前三个；``delete`` 是本项目补的 —— 删掉一个计划
#: 同样"改了别人能看到的东西"，不记就答不上"这个计划是谁删的"）。
AUDIT_CREATE: Final[str] = "schedule.create"
AUDIT_UPDATE: Final[str] = "schedule.update"
AUDIT_DISABLE: Final[str] = "schedule.disable"
AUDIT_DELETE: Final[str] = "schedule.delete"

#: ``last_result`` 的四个非错误取值（与 §04.6.5.1 的表逐字一致）。
RESULT_OK: Final[str] = "ok"
RESULT_SKIPPED_RATELIMIT: Final[str] = "skipped_ratelimit"
RESULT_SKIPPED_DISABLED: Final[str] = "skipped_disabled"
#: 到点了，但这条任务在这个平台上**早就投过**（幂等命中）⇒ 什么都没做。
#: 它**不是失败**：记成 ``error:…`` 的话，一个钉着已发任务的计划会每天"失败"一次，
#: 五天后拉一条告警 —— 而那条告警淹掉的正是真故障（陷阱 182）。
RESULT_SKIPPED_DUPLICATE: Final[str] = "skipped_duplicate"

#: 日志/事件来源（面板按它过滤）。
LOG_SOURCE: Final[str] = "publish.scheduler"

_ERROR: Final[str] = "error:"


@dataclass(frozen=True, slots=True)
class ScheduleFireResult:
    """一次触发的结论（CLI / 面板 / 测试读同一份）。"""

    schedule_id: str
    result: str
    job_ids: tuple[str, ...] = ()
    queued: int = 0
    skipped: tuple[str, ...] = ()
    next_run_at: str | None = None
    task_id: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == RESULT_OK

    @property
    def failed(self) -> bool:
        return self.result.startswith(_ERROR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "result": self.result,
            "job_ids": list(self.job_ids),
            "queued": self.queued,
            "skipped": list(self.skipped),
            "next_run_at": self.next_run_at,
            "task_id": self.task_id,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ScheduleTickReport:
    """一拍的结论（四条清单 + 建出来的作业数）。"""

    fired: tuple[str, ...] = ()
    skipped_ratelimit: tuple[str, ...] = ()
    skipped_disabled: tuple[str, ...] = ()
    skipped_duplicate: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()

    @property
    def due(self) -> int:
        return (
            len(self.fired)
            + len(self.skipped_ratelimit)
            + len(self.skipped_disabled)
            + len(self.skipped_duplicate)
            + len(self.errors)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "due": self.due,
            "fired": list(self.fired),
            "skipped_ratelimit": list(self.skipped_ratelimit),
            "skipped_disabled": list(self.skipped_disabled),
            "skipped_duplicate": list(self.skipped_duplicate),
            "errors": list(self.errors),
            "job_ids": list(self.job_ids),
        }


class SchedulerService:
    """定时发布的一拍（**无状态**：所有状态都在 ``publish_schedules`` 那一行上）。

    :param config: 发布配置（平台 / 账号 / 总开关）。调用方**每拍现读** ——
        写成"构造时读一次"的话，改了 ``config/publish.yaml`` 要重启 API 才生效。
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        config: PublishConfig,
        log: LogSink | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._config = config
        self._log = log
        self._repo = ScheduleRepo(connection)

    # ── 一拍 ────────────────────────────────────────────────────────

    def due(self, *, now: str, limit: int = SCHEDULER_BATCH_LIMIT) -> tuple[ScheduleRow, ...]:
        """到点该跑的计划（走 ``idx_sched_due`` 那条部分索引）。"""
        return self._repo.list_due(now=now, limit=limit)

    def tick(self, *, now: str | None = None, limit: int = SCHEDULER_BATCH_LIMIT) -> ScheduleTickReport:
        """跑一轮（**逐条隔离**：一条炸了不影响其余）。"""
        moment = now or now_iso()
        fired: list[str] = []
        ratelimited: list[str] = []
        disabled: list[str] = []
        duplicated: list[str] = []
        errors: list[str] = []
        jobs: list[str] = []
        for row in self.due(now=moment, limit=limit):
            outcome = self.fire(row, now=moment)
            jobs.extend(outcome.job_ids)
            if outcome.ok:
                fired.append(row.id)
            elif outcome.result == RESULT_SKIPPED_RATELIMIT:
                ratelimited.append(row.id)
            elif outcome.result == RESULT_SKIPPED_DISABLED:
                disabled.append(row.id)
            elif outcome.result == RESULT_SKIPPED_DUPLICATE:
                duplicated.append(row.id)
            else:
                errors.append(row.id)
        report = ScheduleTickReport(
            fired=tuple(fired),
            skipped_ratelimit=tuple(ratelimited),
            skipped_disabled=tuple(disabled),
            skipped_duplicate=tuple(duplicated),
            errors=tuple(errors),
            job_ids=tuple(jobs),
        )
        if report.due:
            # 只在**真有到点的**时留一条汇总：每 30s 写一行"什么都没发生"
            # 会把日志表刷满，而这张表是有保留期的（§03.7.5）。
            logger.info(
                "schedule.tick",
                due=report.due,
                fired=len(report.fired),
                ratelimited=len(report.skipped_ratelimit),
                disabled=len(report.skipped_disabled),
                duplicated=len(report.skipped_duplicate),
                errors=len(report.errors),
            )
        return report

    def fire(self, row: ScheduleRow, *, now: str | None = None) -> ScheduleFireResult:
        """触发一条计划（**不抛**：异常在这里变成 ``error:…`` 与 ``fail_streak``）。"""
        moment = now or now_iso()
        try:
            return self._fire(row, now=moment)
        except Exception as exc:
            return self._on_error(row, exc, now=moment)

    # ── 单条触发 ────────────────────────────────────────────────────

    def _fire(self, row: ScheduleRow, *, now: str) -> ScheduleFireResult:
        if not self._config.enabled:
            # §04.6.5.1「与 publish.enabled 关系」：**空转**（不创建 job）。
            # 仍记 last_result ⇒ 面板上看得见"调度器是活的、只是开关关着"。
            return self._settle(
                row,
                at=now,
                result=RESULT_SKIPPED_DISABLED,
                note="发布总开关是关的（config/publish.yaml -> enabled: false）",
            )

        targets = self._targets(row)
        if not targets:
            raise StudioError(
                f"计划 {row.id} 指向的平台都没有可用账号",
                code=ErrorCode.CONFIG_INVALID,
                context={"schedule_id": row.id, "platforms": list(row.platforms)},
                remediation="给这些平台配上启用的账号，或改这个计划的平台",
            )

        blocked = self._rate_blocked(targets, now=now)
        if blocked is not None:
            return self._settle(
                row,
                at=now,
                result=RESULT_SKIPPED_RATELIMIT,
                defer_to=blocked,
                note="被发布限频挡住（§03.4.4 ⑥）—— 这不是失败，顺延到额度恢复",
            )

        task_id = self._resolve_task(row)
        job_ids: list[str] = []
        skipped: list[str] = []
        duplicates: list[str] = []
        unavailable: list[str] = []
        for platform, account in targets:
            report = enqueue_publications(
                connection=self._connection,
                task_id=task_id,
                config=self._config,
                platforms=(platform,),
                account_id=account.account_id,
            )
            if report.missing:
                raise StudioError(
                    f"任务不存在：{task_id}",
                    code=ErrorCode.TASK_NOT_FOUND,
                    context={"schedule_id": row.id, "task_id": task_id},
                    remediation="这个计划钉着的任务被删了；改掉 task_id 或删掉这个计划",
                )
            skipped.extend(report.skipped)
            if report.queued:
                # 只在**这一拍真的建了作业**时去回读作业号：``queued=0`` 是幂等命中，
                # 那时表里那条是上一拍的，把它算成"这次建的"会让事件骗人。
                job_id = self._latest_job_id(task_id, platform)
                if job_id is not None:
                    job_ids.append(job_id)
            elif report.duplicates:
                duplicates.extend(report.duplicates)
            else:
                # 平台没启用 / 这个平台没有启用的账号 —— 这一类**要人去改配置**。
                unavailable.extend(report.skipped)
        if not job_ids:
            if duplicates and not unavailable:
                return self._settle(
                    row,
                    at=now,
                    result=RESULT_SKIPPED_DUPLICATE,
                    task_id=task_id,
                    skipped=tuple(skipped),
                    note="这条任务在这些平台上早就投过（幂等命中）—— 没有重复发，也不用改什么",
                )
            raise StudioError(
                f"到点了但一个作业都没建出来：{'；'.join(unavailable or skipped) or '没有可投递的平台'}",
                code=ErrorCode.PUBLISH_FAILED,
                context={"schedule_id": row.id, "task_id": task_id, "skipped": list(skipped)},
                remediation="多半是平台没启用、或这个平台没有启用的账号；发布面板的投递区块能看到平台状态",
            )
        return self._settle(
            row,
            at=now,
            result=RESULT_OK,
            job_ids=tuple(job_ids),
            skipped=tuple(skipped),
            task_id=task_id,
        )

    def _targets(self, row: ScheduleRow) -> tuple[tuple[str, AccountConfig], ...]:
        """``(平台, 账号)`` 对 —— 一个平台一个账号（§03.3.10 的发布单元是"一任务一平台一次"）。

        计划里写的 ``account_ids`` 是**筛选**而不是硬绑定：平台下没有它点的那个账号时，
        退回到该平台启用的账号（T5.8 之前一个平台只有一个）。硬绑定会让"账号改名"
        变成"计划永远不再触发"，而那种失败是静默的。
        """
        out: list[tuple[str, AccountConfig]] = []
        for platform in row.platforms:
            accounts = [a for a in self._config.enabled_accounts if a.platform == platform]
            if row.account_ids:
                picked = [a for a in accounts if a.account_id in row.account_ids]
                accounts = picked or accounts
            if not accounts:
                continue
            out.append((platform, accounts[0]))
        return tuple(out)

    def _rate_blocked(self, targets: Sequence[tuple[str, AccountConfig]], *, now: str) -> str | None:
        """限频结论：``None`` = 放行；否则返回**最早能发的时刻**（ISO）。

        多个账号里只要有一个被挡住就整条顺延（不是"发得动的先发"）：
        一次触发要么是一批完整的发布，要么整批顺延 —— 半个批次成功会让
        ``last_result`` 只能写成 ``ok``，而面板上"它到底发出去没有"就再也答不上来了。
        """
        store = JobStore(self._connection)
        latest: str | None = None
        for _platform, account in targets:
            state = store.rate_limit_state(
                account_id=account.account_id,
                daily_limit=self._daily_limit(account),
                min_gap_min=self._min_gap(account),
                now=parse_iso(now),
            )
            if state.allowed:
                continue
            candidate = state.next_allowed_at
            if candidate is None:
                raise StudioError(
                    f"账号 {account.account_id} 被限频，但守卫没给出下一个可用时刻",
                    code=ErrorCode.PUBLISH_RATELIMIT,
                    context={"account_id": account.account_id, "reason": state.reason},
                    remediation="检查 publications 的 published_at 与 pool_settings.rate_limit_json",
                )
            latest = candidate if latest is None else max(latest, candidate)
        return latest

    def _daily_limit(self, account: AccountConfig) -> int:
        """池级为准、账号级兜底（与 ``publish_worker`` 同一口径）。"""
        value = self._pool_rate_limit().get("daily_limit")
        return account.daily_limit if value is None else int(value)

    def _min_gap(self, account: AccountConfig) -> int:
        value = self._pool_rate_limit().get("min_gap_min")
        return account.min_gap_min if value is None else int(value)

    def _pool_rate_limit(self) -> Mapping[str, Any]:
        """``pool_settings.rate_limit_json``；读不出来就退回空表（用账号级配置）。

        读不出来是**可能的**：``build_state`` 在最小家目录里也能起来，那时
        ``pool_settings`` 可能还没有 publish 那一行。退回账号级而不是抛 ——
        限频守卫少一层数据源不该让整个调度器停摆。
        """
        try:
            return JobStore(self._connection).pool_runtime(PUBLISH_POOL).rate_limit
        except Exception as exc:  # pragma: no cover - 只有坏库 / 缺种子会走到
            logger.warning("schedule.pool_runtime_failed", error=str(exc))
            return {}

    def _resolve_task(self, row: ScheduleRow) -> str:
        """这条计划要发哪条任务：钉着的那个，或从"待发布池"挑一条。

        "待发布池"的**一期口径**：最新的、``completed`` 且**一条发布记录都没有**的任务。
        再细的口径（按平台挑、跳过已投过的平台）要等 T5.8 的多账号分发，
        而一个猜出来的口径比一个写清楚的口径更危险 —— 它会悄悄把片子发错。
        """
        if row.task_id:
            return row.task_id
        tasks = TaskService(self._connection)
        publications = PublicationRepo(self._connection)
        for task in tasks.list_completed(limit=50):
            if not publications.list_for_task(task.id):
                return task.id
        raise StudioError(
            "待发布池是空的：没有出片完成但还没发过的任务",
            code=ErrorCode.VALIDATION_FAILED,
            context={"schedule_id": row.id},
            remediation="等一条任务走完出片，或把这个计划钉到某个 task_id 上",
        )

    def _latest_job_id(self, task_id: str, platform: str) -> str | None:
        """刚投出去那条作业的 id（``enqueue_publications`` 只回条数，不回 id）。

        作业 id 是"到点之后到底建了什么"的凭据（WS 事件 ``publish.schedule_fired``
        就带它），所以这里回读一次。调用方保证**只在 ``queued > 0`` 时**调它。
        """
        row = self._connection.execute(
            "SELECT id FROM jobs WHERE task_id = ? AND pool = ? AND unit_type = ? AND unit_ref = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id, PUBLISH_POOL, UnitType.PUBLISH.value, platform),
        ).fetchone()
        return None if row is None else str(row["id"])

    # ── 落库 + 留痕 + 事件 ──────────────────────────────────────────

    def _settle(
        self,
        row: ScheduleRow,
        *,
        at: str,
        result: str,
        defer_to: str | None = None,
        job_ids: tuple[str, ...] = (),
        skipped: tuple[str, ...] = (),
        task_id: str | None = None,
        note: str | None = None,
    ) -> ScheduleFireResult:
        """记结论 + 排下一期 + 发事件（**一次触发的唯一出口**）。

        ``defer_to``（被限频）与"窗口内自然推进"取**较晚**的那个：顺延的意思是
        "额度恢复之前别再试"，而不是"提前到额度恢复的那一刻发"——
        提前发会把一天的额度在几分钟内打光。
        """
        candidate = self._reschedule(row, at=at)
        if defer_to is not None:
            candidate = defer_to if candidate is None else max(candidate, defer_to)
        fresh = self._repo.record_result(
            row.id,
            at=at,
            result=result,
            next_run_at=candidate,
            failed=result.startswith(_ERROR),
        )
        if candidate is None and fresh.enabled:
            # 一次性计划（``at_time``）跑完就停：留着 enabled=1 而 next_run_at=NULL
            # 会让面板显示"启用中"却永远不再触发 —— 那是骗人。
            fresh = self._disable(fresh, reason="一次性计划已执行完（at_time 已过）")
        emit_scheduled(
            self._log,
            fresh,
            message=f"定时计划已排下一期：{fresh.id} -> {fresh.next_run_at or '不再触发'}",
        )
        if job_ids:
            self._emit(
                "info",
                f"定时计划到点：{fresh.id} 建了 {len(job_ids)} 个发布作业",
                event=EventKind.PUBLISH_SCHEDULE_FIRED,
                payload={"schedule_id": fresh.id, "job_ids": list(job_ids), "task_id": task_id},
            )
        elif result != RESULT_OK:
            self._emit(
                "info",
                f"定时计划空转（{result}）：{fresh.id}" + ("" if note is None else f" —— {note}"),
                payload={"schedule_id": fresh.id, "result": result, "note": note},
            )
        return ScheduleFireResult(
            schedule_id=fresh.id,
            result=result,
            job_ids=job_ids,
            queued=len(job_ids),
            skipped=skipped,
            next_run_at=fresh.next_run_at,
            task_id=task_id,
            note=note,
        )

    def _reschedule(self, row: ScheduleRow, *, at: str) -> str | None:
        """下一期（见 :mod:`studio.domain.schedule`）。"""
        return next_run_at(
            schedule_id=row.id,
            mode=row.mode,
            now=parse_iso(at),
            at_time=row.at_time,
            window=row.window,
            interval_hours=row.interval_hours,
            jitter_min=row.jitter_min,
            last_run_at=at,
        )

    def _disable(self, row: ScheduleRow, *, reason: str) -> ScheduleRow:
        """自动停用（跑完的一次性计划）—— 写留痕，不然"它怎么自己停了"查不出来。"""
        fresh = self._repo.set_enabled(row.id, enabled=False, next_run_at=None)
        audit_schedule(
            self._connection,
            action=AUDIT_DISABLE,
            row=fresh,
            actor="system",
            reason=reason,
            before={"enabled": True},
            after={"enabled": False},
            source="auto",
        )
        return fresh

    def _on_error(self, row: ScheduleRow, exc: Exception, *, now: str) -> ScheduleFireResult:
        """失败处置：记 ``error:…`` + ``fail_streak`` 加一；连败到阈值 ⇒ 告警。"""
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        result = f"{_ERROR}{reason}"
        candidate = self._reschedule(row, at=now)
        fresh = self._repo.record_result(row.id, at=now, result=result, next_run_at=candidate, failed=True)
        if candidate is None and fresh.enabled:
            fresh = self._disable(fresh, reason="触发失败且没有下一期（一次性计划）")
        self._emit(
            "error",
            f"定时计划触发失败：{row.id}（连续 {fresh.fail_streak} 次）—— {reason}",
            payload={
                "schedule_id": row.id,
                "fail_streak": fresh.fail_streak,
                "next_run_at": fresh.next_run_at,
                "error": reason,
            },
        )
        if fresh.fail_streak >= FAIL_STREAK_ALERT:
            self._alert(fresh, reason=reason)
        return ScheduleFireResult(
            schedule_id=row.id,
            result=result,
            next_run_at=fresh.next_run_at,
            task_id=row.task_id,
            note=reason,
        )

    def _alert(self, row: ScheduleRow, *, reason: str) -> None:
        """``system.alert``（连续失败 >= 5 次）。

        ``payload.code`` 必须是 ``AlertCode`` 之一，否则 WS 层只当普通日志
        （§04.4.3 的分流），"告警"就变成了"日志里的一行"。
        """
        message = f"定时计划连续 {row.fail_streak} 次触发失败：{row.id}"
        self._emit(
            "error",
            message,
            payload={
                "code": str(AlertCode.SCHEDULE_FAILING),
                "severity": "error",
                "message": message,
                "hint": f"去发布面板看这个计划（{reason}）",
                "schedule_id": row.id,
            },
        )

    def _emit(
        self,
        level: str,
        message: str,
        *,
        event: EventKind | None = None,
        payload: Mapping[str, Any],
    ) -> None:
        emit_log(self._log, level, message, event=event, payload=payload)


# ══════════════════════════════════════════════════════════════════════
# 计划 CRUD（面板的四个写动作 · §04.6.5.1）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """一份**校验过**的计划参数（CRUD 三个入口共用）。"""

    mode: str
    platforms: tuple[str, ...]
    account_ids: tuple[str, ...]
    task_id: str | None = None
    at_time: str | None = None
    window: tuple[str, str] | None = None
    interval_hours: int | None = None
    jitter_min: int = DEFAULT_JITTER_MIN
    enabled: bool = True


def validate_spec(
    spec: Mapping[str, Any], *, config: PublishConfig, connection: sqlite3.Connection
) -> ScheduleSpec:
    """把面板/CLI 给的字典校验成 :class:`ScheduleSpec`；任何一处不合法 ⇒ 抛（**不落库**）。

    校验放在服务层而不是 pydantic 模型里：平台代号与账号名要与 ``config/publish.yaml``
    对得上，而那份配置是**运行期**读的 —— 写进模型就等于把配置快照钉在启动那一刻。
    """
    mode = str(spec.get("mode", "")).strip()
    if mode not in MODES:
        raise StudioError(
            f"不认识的调度模式：{mode!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"mode": mode, "modes": list(MODES)},
            remediation="mode 只能是 at_time / daily_window / interval",
        )

    platforms = tuple(str(item) for item in (spec.get("platforms") or ()))
    if not platforms:
        raise StudioError(
            "至少要选一个平台",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"platforms": list(platforms)},
            remediation="在 config/publish.yaml 的 platforms 里挑一个（douyin / other …）",
        )
    unknown = [item for item in platforms if item not in config.platforms]
    if unknown:
        raise StudioError(
            f"配置里没有这些平台：{'、'.join(unknown)}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"unknown": unknown, "known": sorted(config.platforms)},
            remediation="平台代号要与 config/publish.yaml 的 platforms 键一致",
        )

    account_ids = tuple(str(item) for item in (spec.get("account_ids") or ()))
    if not account_ids:
        raise StudioError(
            "至少要选一个账号",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"account_ids": list(account_ids)},
            remediation="在 config/publish.yaml 的 accounts 里挑一个（演练台是 _rehearsal）",
        )
    known_accounts = {account.account_id for account in config.accounts}
    missing = [item for item in account_ids if item not in known_accounts]
    if missing:
        raise StudioError(
            f"配置里没有这些账号：{'、'.join(missing)}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"unknown": missing, "known": sorted(known_accounts)},
            remediation="账号名要与 config/publish.yaml 的 accounts[].account_id 一致",
        )

    jitter_min = validate_jitter(spec.get("jitter_min", DEFAULT_JITTER_MIN))

    window: tuple[str, str] | None = None
    at_time: str | None = None
    interval_hours: int | None = None
    if mode == "daily_window":
        raw = spec.get("window")
        if not raw or len(raw) != 2:
            raise StudioError(
                "窗口模式必须给 window（两个 HH:MM）",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"window": None if raw is None else list(raw)},
                remediation="例如 ['18:00','21:30']",
            )
        window = (str(raw[0]), str(raw[1]))
        validate_window(window)
    elif mode == "at_time":
        raw_at = spec.get("at_time")
        if not raw_at:
            raise StudioError(
                "at_time 模式必须给 at_time",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"at_time": None},
                remediation="例如 2026-09-14T19:30:00+08:00（要带时区）",
            )
        at_time = str(raw_at)
        parse_at_time(at_time)
    else:
        interval_hours = _validate_interval(spec.get("interval_hours"))

    task_id = None if spec.get("task_id") in (None, "") else str(spec["task_id"])
    if task_id is not None:
        TaskService(connection).get(task_id)

    return ScheduleSpec(
        mode=mode,
        platforms=platforms,
        account_ids=account_ids,
        task_id=task_id,
        at_time=at_time,
        window=window,
        interval_hours=interval_hours,
        jitter_min=jitter_min,
        enabled=bool(spec.get("enabled", True)),
    )


def _validate_interval(raw: Any) -> int:
    if raw is None:
        raise StudioError(
            "interval 模式必须给 interval_hours",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"interval_hours": None},
            remediation="例如 6（每 6 小时一次）",
        )
    try:
        hours = int(raw)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            f"interval_hours 必须是整数，拿到的是 {raw!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"interval_hours": str(raw)},
            remediation="例如 6",
        ) from exc
    if not 1 <= hours <= MAX_INTERVAL_HOURS:
        raise StudioError(
            f"间隔必须在 1-{MAX_INTERVAL_HOURS} 小时之间，拿到的是 {hours}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"interval_hours": hours, "max": MAX_INTERVAL_HOURS},
            remediation="把它改到 1-720 小时",
        )
    return hours


def create_schedule(
    *,
    connection: sqlite3.Connection,
    config: PublishConfig,
    spec: Mapping[str, Any],
    actor: str = "user",
    actor_ref: str | None = None,
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ScheduleRow:
    """建一条计划（``next_run_at`` 当场算好落库 —— 陷阱 #30）。"""
    moment = parse_iso(now) if now is not None else utc_now()
    fields = validate_spec(spec, config=config, connection=connection)
    schedule_id = new_ulid()
    candidate = _plan_next(schedule_id=schedule_id, spec=fields, now=moment)
    if fields.mode == "at_time" and fields.enabled and candidate is None:
        raise _at_time_passed(fields.at_time)
    row = ScheduleRepo(connection).create(
        schedule_id=schedule_id,
        mode=fields.mode,
        platforms=fields.platforms,
        account_ids=fields.account_ids,
        jitter_min=fields.jitter_min,
        task_id=fields.task_id,
        at_time=fields.at_time,
        window=fields.window,
        interval_hours=fields.interval_hours,
        enabled=fields.enabled,
        next_run_at=candidate,
        created_by=actor,
    )
    audit_schedule(
        connection,
        action=AUDIT_CREATE,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason="新建定时计划",
        before=None,
        after=row.to_dict(),
        source=source,
    )
    emit_scheduled(log, row, message=f"定时计划已建：{row.id} -> {row.next_run_at or '不排期'}")
    return row


def update_schedule(
    *,
    connection: sqlite3.Connection,
    config: PublishConfig,
    schedule_id: str,
    patch: Mapping[str, Any],
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ScheduleRow:
    """改一条计划（**整行改写 + 重算 next_run_at 在同一条 UPDATE 里** · 陷阱 #33）。

    ``patch`` 是**部分字段**：只给 ``enabled`` 就是启停，只给 ``window`` 就是改窗口。
    没给的字段沿用库里那一行 —— 让面板每次提交整份表单的话，"我只想停用它"
    会连带把别的字段按面板那一刻的旧值写回去（并发编辑的经典坑）。

    ``patch`` 里**显式给的 ``None`` 会被当成一个值**（不是「没给」）：
    ``{'task_id': None}`` 的意思是「把钉住的任务解掉，回到待发布池」，
    而「没给」是连键都没有。这两件事只能由调用方区分（路由用
    ``model_dump(exclude_unset=True)``），所以这里**不**做 ``None`` 过滤 ——
    过滤掉之后，「解绑」这个动作就永远做不到，而且失败是静默的。
    """
    repo = ScheduleRepo(connection)
    before = repo.require(schedule_id)
    merged: dict[str, Any] = {
        "mode": before.mode,
        "platforms": list(before.platforms),
        "account_ids": list(before.account_ids),
        "task_id": before.task_id,
        "at_time": before.at_time,
        "window": list(before.window) if before.window is not None else None,
        "interval_hours": before.interval_hours,
        "jitter_min": before.jitter_min,
        "enabled": before.enabled,
    }
    merged.update(patch)
    fields = validate_spec(merged, config=config, connection=connection)
    moment = parse_iso(now) if now is not None else utc_now()
    # 模式改了 ⇒ 旧模式的字段要清掉：`at_time` 留着一条昨天的时刻，
    # 面板上"模式=窗口，但还有一个时刻"会让人以为它还会按那个时刻发。
    at_time = fields.at_time if fields.mode == "at_time" else None
    window = fields.window if fields.mode == "daily_window" else None
    interval_hours = fields.interval_hours if fields.mode == "interval" else None
    candidate = next_run_at(
        schedule_id=schedule_id,
        mode=fields.mode,
        now=moment,
        at_time=at_time,
        window=window,
        interval_hours=interval_hours,
        jitter_min=fields.jitter_min,
        last_run_at=before.last_run_at,
    )
    if fields.mode == "at_time" and fields.enabled and candidate is None:
        raise _at_time_passed(at_time)
    if not fields.enabled:
        candidate = None
    row = repo.update(
        schedule_id,
        mode=fields.mode,
        platforms=fields.platforms,
        account_ids=fields.account_ids,
        jitter_min=fields.jitter_min,
        task_id=fields.task_id,
        at_time=at_time,
        window=window,
        interval_hours=interval_hours,
        enabled=fields.enabled,
        next_run_at=candidate,
    )
    action = AUDIT_DISABLE if (before.enabled and not row.enabled) else AUDIT_UPDATE
    audit_schedule(
        connection,
        action=action,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason=reason or ("停用定时计划" if action == AUDIT_DISABLE else "修改定时计划"),
        before=before.to_dict(),
        after=row.to_dict(),
        source=source,
    )
    emit_scheduled(log, row, message=f"定时计划已改：{row.id} -> {row.next_run_at or '不排期'}")
    return row


def delete_schedule(
    *,
    connection: sqlite3.Connection,
    schedule_id: str,
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    source: str = "webui",
) -> bool:
    """删一条计划（不存在 ⇒ ``False``，**不抛** —— 面板连点两下不该报错）。"""
    repo = ScheduleRepo(connection)
    before = repo.get(schedule_id)
    if before is None:
        return False
    removed = repo.delete(schedule_id)
    if removed:
        audit_schedule(
            connection,
            action=AUDIT_DELETE,
            row=before,
            actor=actor,
            actor_ref=actor_ref,
            reason=reason or "删除定时计划",
            before=before.to_dict(),
            after=None,
            source=source,
        )
    return removed


def run_now(
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    config: PublishConfig,
    schedule_id: str,
    log: LogSink | None = None,
    now: str | None = None,
) -> ScheduleFireResult:
    """立刻执行一次（调试用 · §04.6.5.1 的 ``POST .../run_now``）。

    **仍走限频**，也**仍看 ``publish.enabled``**：这一下与到点触发走的是同一条
    :meth:`SchedulerService.fire`。让"人工按一下"绕过开关，等于给 R14 的不可逆防护
    开了一个后门 —— 而那个后门正好长在"我想试试看"这句最常说的话上。
    """
    row = ScheduleRepo(connection).require(schedule_id)
    service = SchedulerService(connection=connection, paths=paths, config=config, log=log)
    return service.fire(row, now=now)


def _plan_next(
    *, schedule_id: str, spec: ScheduleSpec, now: datetime, last_run_at: str | None = None
) -> str | None:
    """算一条计划的 ``next_run_at``；停用的计划一律 ``None``（不排期）。"""
    if not spec.enabled:
        return None
    return next_run_at(
        schedule_id=schedule_id,
        mode=spec.mode,
        now=now,
        at_time=spec.at_time,
        window=spec.window,
        interval_hours=spec.interval_hours,
        jitter_min=spec.jitter_min,
        last_run_at=last_run_at,
    )


def _at_time_passed(at_time: str | None) -> StudioError:
    return StudioError(
        "at_time 已经过去了，这个计划一次都不会触发",
        code=ErrorCode.SCHEDULE_INVALID,
        context={"at_time": at_time, "now": now_iso()},
        remediation="把时刻改到将来，或改用 daily_window 模式",
    )


def audit_schedule(
    connection: sqlite3.Connection,
    *,
    action: str,
    row: ScheduleRow,
    actor: str,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    actor_ref: str | None = None,
    source: str = "webui",
) -> None:
    """写一条计划留痕（§06.5.5：增删改启停全部留痕）。"""
    AuditRepo(connection).record(
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=actor_ref,
        action=action,
        target_type="schedule",
        target_id=row.id,
        task_id=row.task_id,
        before=before,
        after=after,
        result="ok",
        reason=reason,
        source=source,
    )


def emit_scheduled(log: LogSink | None, row: ScheduleRow, *, message: str) -> None:
    """发一条 ``publish.scheduled``（面板据此把新时刻画上去，不必等下一次轮询）。"""
    emit_log(
        log,
        "info",
        message,
        event=EventKind.PUBLISH_SCHEDULED,
        payload={
            "schedule_id": row.id,
            "next_run_at": row.next_run_at,
            "task_id": row.task_id,
            "platforms": list(row.platforms),
        },
    )


def emit_log(
    log: LogSink | None,
    level: str,
    message: str,
    *,
    event: EventKind | None = None,
    payload: Mapping[str, Any],
) -> None:
    """写一条调度日志（``event`` 非空 ⇒ 额外扇出一条 WS 事件 · §04.4.4）。

    没有 ``log`` 出口时退回进程日志：``build_state`` 在最小家目录里也能起来，
    那时 ``LogService`` 可能不在手上 —— 而"调度器做了什么"绝不该因此消失。
    """
    merged: dict[str, Any] = dict(payload)
    if event is not None:
        merged[EVENT_PAYLOAD_KEY] = event.value
    if log is not None:
        log(level=level, source=LOG_SOURCE, message=message, payload=merged)  # type: ignore[arg-type]
        return
    if level == "info":
        logger.info(message, **merged)
    elif level == "warn":
        logger.warning(message, **merged)
    else:
        logger.error(message, **merged)
