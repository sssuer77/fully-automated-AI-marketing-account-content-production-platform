"""渲染池的单元处理器（``render/final`` · T3.7 收口）。

``render/final`` 单元 = 「一个任务把它的成片渲出来」
--------------------------------------------------
```text
claim(render/final, unit_ref = "final")
   ├─ payload 里没有文案 ⇒ 读库里那一版生效稿件（与 CLI / 面板同一条规则）
   ├─ ProduceRequest → produce_video（配音 → 挑底片 → 合成 → 落 manifest）
   ├─ 进度逐句 / 逐段写进 jobs.result_json（面板轮询读的就是它）
   └─ QC 结论回填 tasks.quality_json；返回一份"这次到底产出了什么"的摘要
```

为什么单元处理器放在 ``pools/`` 而不是 ``services/``
--------------------------------------------------
与 ``draft_worker`` 同一条理由：它实现的是 :class:`~studio.pools.worker_base.UnitHandler`
协议 —— 那是**池**的契约。它只做三件事：把认领到的 id 翻成服务调用、把失败翻成
``StudioError``（好让队列的重试 / 退避 / 死信 / 告警逻辑接手）、把结果压成可留痕的摘要。
业务规则全在 ``services/render_service.py``。

三条纪律
--------
1. **不自己收尾**。``succeed`` / ``fail`` / 退避 / 死信一律由 :class:`PoolWorker` 负责
   （T1.6 裁定）：单元处理器只"产出结果 + 抛异常"。否则每个池都会长出一份自己的重试逻辑。
2. **可重入**。同一条 ``render/final`` 被重投时直接重渲一遍：``produce_video`` 的产物
   走 ``.partial`` + 原子改名，重跑只会覆盖同一批路径，不会留下半成品。
   这里**不**做"产物已存在就跳过"的短路 —— 那会把"上次跑到一半崩了"当成"已经完成"，
   而这两种情况在盘上长得一模一样。
   2026-09-17 起 ``produce_video`` 自己会先比一次 ``composite_hash``（``render/cache.py``，
   §04.2.8.7）：同哈希 ⇒ 复用盘上那一支、不重跑 ffmpeg。那不是上面被否掉的那种短路 ——
   它比的是"这个文件是**这批输入**渲出来的"，而 manifest 只在成片原子改名 + 量完响度
   **之后**才写，比不上一律重渲。
3. **不在单元里开池**。处理器由进程入口建一次（:func:`build_render_handler`），
   单元只拿装配好的东西。

为什么进度写进 ``jobs.result_json``，而不是留在 worker 的内存里
------------------------------------------------------------
读进度的是**另一个进程**（API 进程里的渲染面板）。留在内存里意味着"重启即丢、
多开一个进程就各看各的"，而 §03.4.6 规则 1 把"在 ``jobs`` 之外维护内存队列"直接
列为架构回退 —— "进程内的进度表"正是它的一个变体。
进度写库走 :meth:`~studio.db.queue.JobStore.report_progress`：它带租约守卫，
租约丢了就一个字节都不写（产物可能已经被别人重做）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import connect
from studio.db.queue import JobStore
from studio.domain.enums import UnitType
from studio.domain.errors import TaskNotFound
from studio.domain.task_service import TaskService
from studio.pools.worker_base import UnitContext
from studio.services.asset_service import disabled_assets
from studio.services.log_service import LogService
from studio.services.render_service import (
    ProduceRequest,
    ProduceResult,
    produce_video,
    quality_report,
)
from studio.services.script_service import read_active_script

__all__ = [
    "RENDER_UNIT_TYPES",
    "RenderFinalHandler",
    "RenderFinalOutcome",
    "build_render_handler",
]

logger = get_logger("studio.pools.render")

#: 本处理器认领的单元类型（``jobs.unit_type``）
RENDER_UNIT_TYPES: Final[frozenset[str]] = frozenset({UnitType.FINAL.value})

#: 进度说明写进 ``result_json`` 时截断到多少字符。配音的每一句都会被当成说明写进去，
#: 而一句口播最多 28 字（T2.5 的切分上限）—— 留 80 是给"复用了母带：voice_master.wav"
#: 这类自带说明的进度留余地，同时防止有人把整段文案塞进来。
_NOTE_CHARS: Final[int] = 80

#: 日志来源（``system_logs.source``）—— 面板按它筛出"这条片子渲染时发生了什么"
_LOG_SOURCE: Final[str] = "studio.pools.render"


@dataclass(frozen=True, slots=True)
class RenderFinalOutcome:
    """一次 ``render/final`` 单元的结论（进 ``jobs.result_json``，面板直接读它）。

    字段是**面板要用什么**决定的，不是 ``ProduceResult`` 的镜像：面板只关心
    "哪支片子 / 什么档 / 降没降级 / 实测多响 / 水印贴没贴"。把 ``ProduceResult``
    整个塞进去也能跑，但 ``result_json`` 会跟着内部数据结构的演化一起长胖，
    而它是每一行作业都要存一份的东西。
    """

    task_id: str
    job_id: str
    final: str
    stage: str
    done: int
    total: int
    note: str
    started_at: str
    profile: str
    duration_ms: int
    size_bytes: int
    composite_hash: str
    bg_fill: str
    degraded: bool
    degrade_reason: str | None
    watermark_enabled: bool
    watermark_skipped_reason: str | None
    output_loudness: Mapping[str, Any] | None
    quality_backfilled: bool
    warnings: tuple[str, ...]

    @classmethod
    def from_result(
        cls,
        ctx: UnitContext,
        result: ProduceResult,
        *,
        started_at: str,
        quality_backfilled: bool,
    ) -> RenderFinalOutcome:
        return cls(
            task_id=result.task_id,
            job_id=ctx.job_id,
            final=result.final.as_posix(),
            stage="done",
            done=1,
            total=1,
            note="出片完成" if quality_backfilled else "出片完成（QC 未回填）",
            started_at=started_at,
            profile=result.profile_name,
            duration_ms=result.duration_ms,
            size_bytes=result.size_bytes,
            composite_hash=result.composite_hash,
            bg_fill=result.bg_fill,
            degraded=result.degraded,
            degrade_reason=result.degrade_reason,
            watermark_enabled=result.watermark_enabled,
            watermark_skipped_reason=result.watermark_skipped_reason,
            output_loudness=result.output_loudness.to_dict() if result.output_loudness else None,
            quality_backfilled=quality_backfilled,
            warnings=tuple(result.warnings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "final": self.final,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "note": self.note,
            "started_at": self.started_at,
            "profile": self.profile,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "composite_hash": self.composite_hash,
            "bg_fill": self.bg_fill,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "watermark_enabled": self.watermark_enabled,
            "watermark_skipped_reason": self.watermark_skipped_reason,
            "output_loudness": None if self.output_loudness is None else dict(self.output_loudness),
            "quality_backfilled": self.quality_backfilled,
            "warnings": list(self.warnings),
        }


class RenderFinalHandler:
    """``render/final`` 单元的处理器（**进程内一个实例**，跨单元复用）。

    连接由构造方持有并跨单元存活：进度写入与 QC 回填都挂在它上面，
    而池 worker 是"起一次、跑到关停"的常驻进程。
    """

    unit_types: frozenset[str] = RENDER_UNIT_TYPES

    def __init__(
        self,
        *,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        outputs_source: Path | None = None,
        log: LogService | None = None,
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._store = JobStore(connection)
        self._outputs_source = outputs_source or (paths.config_dir / "outputs.yaml")
        self._log = log

    def run(self, ctx: UnitContext) -> Mapping[str, Any]:
        """跑一次 ``render/final``：文案 ⇒ 配音 ⇒ 合成 ⇒ 成片 + QC 回填。"""
        request = _request_from(ctx.payload, task_id=ctx.task_id)
        if not request.text.strip():
            request = replace(request, text=self._script_text(ctx.task_id))

        started_at = now_iso()
        # 进度**累计**在一份 dict 里、每次整份覆写 `result_json`：进度是"当前状态"，
        # 不是一串增量事件。失败时这份 dict 也正好是"停在哪儿"的现场（见下面的 except）。
        state: dict[str, Any] = {
            "task_id": ctx.task_id,
            "stage": "start",
            "done": 0,
            "total": 0,
            "note": "开始",
            "started_at": started_at,
        }
        self._report(ctx, state)
        self._log_line(ctx, "info", f"render.final 开始（第 {ctx.attempt} 次尝试）")

        def progress(stage: str, done: int, total: int, note: str) -> None:
            # 进度回调是这条链路上**唯一的存活检查点**：它会在每一句配音、每一段之间被调到。
            ctx.check_alive()
            state.update(stage=stage, done=done, total=total, note=_clip(note))
            self._report(ctx, state)

        try:
            result = produce_video(
                request,
                paths=self._paths,
                outputs_source=self._outputs_source,
                on_progress=progress,
                disabled=disabled_assets(self._connection),
            )
        except StudioError as exc:
            # 把 `remediation` 一起写进进度现场：`jobs` 表**没有**这一列，而面板要显示
            # "下一步该怎么办"。只记一行日志、**不吞异常** —— 收尾（重试 / 退避 / 死信 /
            # 告警）是队列的事。
            state.update(
                note=f"失败：{exc.message}",
                error_code=str(exc.code),
                remediation=exc.remediation,
            )
            self._report(ctx, state)
            self._log_line(ctx, "error", f"render.final 失败：{exc.code} {exc.message}")
            raise

        backfilled = self._backfill_quality(ctx.task_id, result)
        outcome = RenderFinalOutcome.from_result(
            ctx, result, started_at=started_at, quality_backfilled=backfilled
        )
        self._log_line(ctx, "info", f"render.final 完成：{result.final.name}")
        return outcome.to_dict()

    # ── 内部 ────────────────────────────────────────────────────────────

    def _report(self, ctx: UnitContext, state: Mapping[str, Any]) -> None:
        """把进度整份写进 ``jobs.result_json``（写不进去只说明租约丢了，**不是错误**）。"""
        self._store.report_progress(job_id=ctx.job_id, worker_id=ctx.worker_id, result=state)

    def _script_text(self, task_id: str) -> str:
        """库里那一版生效稿件的正文（**逐句拼接**，与配音要读的东西一致）。"""
        payload = read_active_script(self._connection, task_id)
        if payload is None:
            raise StudioError(
                f"任务 {task_id} 没有生效稿件，入队的 payload 里也没有文案",
                code=ErrorCode.SCRIPT_NOT_FOUND,
                context={"task_id": task_id, "unit_type": UnitType.FINAL.value},
                remediation="先在「稿件」面板出一版稿，或把文案放进入队 payload 的 text 字段",
            )
        _script, sentences = payload
        return "".join(row.text for row in sentences)

    def _backfill_quality(self, task_id: str, result: ProduceResult) -> bool:
        """把 QC 结论写回 ``tasks.quality_json``；任务不在库里 ⇒ ``False``（**不报错**）。

        为什么"任务不在库里"不算失败：能走到这里说明作业挂着 ``tasks.id`` 的外键，
        任务本来必然存在 —— 它只可能在"入队之后、出片之前"被人删掉（``ON DELETE CASCADE``
        会连着删掉作业，但那一拍与本单元之间仍有窗口）。为了这个窗口把一次**已经
        成功出片**的作业判成失败，等于用附属数据否掉主产物。
        """
        if not self._store_has_task(task_id):
            return False
        try:
            TaskService(self._connection).set_quality(task_id, quality_report(result))
        except TaskNotFound:
            return False
        except StudioError as exc:
            logger.warning("render.quality_backfill_failed", task_id=task_id, code=str(exc.code))
            return False
        return True

    def _store_has_task(self, task_id: str) -> bool:
        row = self._connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row is not None

    def _log_line(self, ctx: UnitContext, level: Severity, message: str) -> None:
        if self._log is None:
            return
        self._log.append(
            level=level,
            source=_LOG_SOURCE,
            message=message,
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            worker_id=ctx.worker_id,
        )


def build_render_handler(
    *,
    paths: StudioPaths,
    log: LogService | None = None,
    connection: sqlite3.Connection | None = None,
) -> RenderFinalHandler:
    """渲染池进程入口的装配（``workers/run_render.py`` 调它）。

    连接**默认自己开一条、故意不关**：它挂着进度写入、QC 回填与日志，而池 worker 是
    "起一次、跑到关停"的常驻进程 —— 单元之间把它关掉，第二条片子就写不了进度了。
    进程退出时由操作系统回收（与 ``workers/run_draft.py`` 同一条）。

    ``log`` 缺省**不是"不打日志"**，而是挂一条落在同一条连接上的 :class:`LogService`：
    面板的"日志尾巴"读的就是 ``system_logs``，而"忘了传 log"这种默认值会让它在
    队列这条路（也就是生产那条路）上**永远是空的** —— 一个只在生产上空的框。
    """
    resolved = connection if connection is not None else connect(paths.db_file)
    return RenderFinalHandler(
        paths=paths,
        connection=resolved,
        log=log if log is not None else LogService(resolved),
    )


def _request_from(payload: Mapping[str, Any], *, task_id: str) -> ProduceRequest:
    """``payload_json`` ⇒ :class:`ProduceRequest`。

    **逐字段显式取**而不是 ``ProduceRequest(**payload)``：payload 是 JSON，
    多一个键就会让构造炸掉，而多出来的键最可能的来源恰恰是"面板加了新字段、
    worker 还没升上去"这种滚动升级 —— 那时该做的是忽略它，不是让整个池停摆。
    类型对不上的键一律退回默认值：宁可这次用默认档渲，也不要抛 ``TypeError``
    把一次能成功的出片变成一条死信。
    """
    text = payload.get("text")
    return ProduceRequest(
        task_id=task_id,
        text=text if isinstance(text, str) else "",
        profile_name=_opt_str(payload, "profile_name"),
        voice=_opt_str(payload, "voice"),
        reuse_voice=payload.get("reuse_voice") is True,
        threads=_opt_int(payload, "threads"),
        seed=_opt_int(payload, "seed"),
        subtitle=_opt_bool(payload, "subtitle"),
    )


def _opt_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value != "" else None


def _opt_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    # ``bool`` 是 ``int`` 的子类：`"threads": true` 会悄悄变成 1 线程，得先挡掉。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _opt_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _clip(note: str) -> str:
    """进度说明截断（``result_json`` 是每行作业都要存一份的东西，不装长文本）。"""
    flat = " ".join(note.split())
    if len(flat) <= _NOTE_CHARS:
        return flat
    return flat[: _NOTE_CHARS - 1] + "…"
