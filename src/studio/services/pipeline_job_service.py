"""一键出片登记表（T4.14 延伸）—— 把「文案 → 配音 → 渲染出片」变成**可以点、可以看进度**的一件事。

四屏接力解决的是"不手抄任务号"，但仍然要人在四个地方各点一次、每次都等上一步真的结束。
而 :func:`~studio.services.pipeline_service.run_task` 早就能一口气跑完这条链路
（CLI 的 ``studio pipeline run --until completed`` 就是它），只是**没有 REST 面**。
这一层就是那件事的界面化。

与 ``render_job_service`` 的分工（为什么不是同一个服务）
------------------------------------------------------
- ``render_jobs`` 管的是**渲染这一步**：给它文案或任务号，它调 ``produce_video``。
- 这里管的是**整条链路**：从任务当前状态出发，该投配音就投、该拼母带就拼、该渲染就渲染，
  一路推到 ``until``。

两者的形状刻意做成一样（登记表 + 后台线程 + 轮询 + 协作式取消）—— 面板那边的交互是
同一套，运维读日志的姿势也是同一套。差别只在"执行体"。

为什么是**进程内**登记表（与 ``render_job_service`` 同一条）
----------------------------------------------------------
诚实说明：这条链路里的渲染那一步在 ``render_jobs`` 里已经是进程内的了，而"把整条链路接进
队列"需要先有一个 ``pipeline`` 池（现在只有四个池，没有它）。这里沿用最直接的一条路，
**代价写在明处**：

- 进程重启 ⇒ 在跑的任务信息丢失（成片与句子音频都在盘上，重跑一条命令就能接上）；
- 只在本进程内可见（多开一个 API 进程，两边各看各的）。

将来接进队列时，替换的是 :meth:`PipelineJobService._run` 里的执行方式，对外形状不变。

为什么并发上限是 1
------------------
与 ``render_jobs`` 同一条：这条链路会走到渲染，而 ``config/pools.yaml`` 的 render 池就是
``concurrency: 1``。同时开两条 1080×1920 的编码只会让两条都变慢。

**同一个任务不会同时跑两条**：``submit`` 见到该任务已有在跑的 job ⇒ 直接返回那一条
（见那边的注释）。连点两次"开始出片"是很自然的动作，而两条链路同时改一个任务的状态，
轻则互相踩状态迁移，重则两条都渲染一遍。

取消是**协作式**的，而且检查点**比渲染少**
------------------------------------------
进度回调只在**配音的每一句**与**渲染的每一段**被调到（``run_task`` 里那两处）。
所以任务停在"投递配音作业"或"拼母带"那几步时按取消，要等它进到下一个回调点才会真的停。
这一点如实写在 ``note`` 里，否则用户会以为按了没反应 —— 与 ``render_jobs`` 同一条纪律。

配音不依赖常驻 voice 池
-----------------------
``run_task`` 在 ``voicing`` 那一步会**就地借一条 worker** 把这条任务的配音干完
（``_drain_voice_pool``，T2.8 的裁定）。所以这个面板在"没起任何池进程"的情况下也能
从稿子一路做到成片 —— 这正是它作为"第一支 MP4 的入口"的意义。常驻池同时在跑也不会出错：
作业认领靠租约，谁先拿到算谁的。
"""

from __future__ import annotations

import sqlite3
import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.config import OutputsConfig, load_app_config, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.domain.enums import TaskStatus
from studio.services.pipeline_service import SUPPORTED_UNTIL, run_task

__all__ = [
    "MAX_JOBS_KEPT",
    "MAX_LOG_LINES",
    "PipelineJob",
    "PipelineJobService",
    "PipelineRequest",
    "supported_until",
]

logger = get_logger("studio.services.pipeline_jobs")

#: 登记表保留多少条（旧的滚掉；成片本身在盘上，不受影响）
MAX_JOBS_KEPT: Final[int] = 50

#: 每条留多少行日志尾巴（面板上那个滚动框）
MAX_LOG_LINES: Final[int] = 200

#: 任务的两种状态集合，供面板分流显示（与 ``render_job_service`` 同名同义）
_RUNNING: Final[frozenset[str]] = frozenset({"queued", "running"})
_TERMINAL: Final[frozenset[str]] = frozenset({"succeeded", "failed", "canceled"})


class _JobCanceledError(Exception):
    """内部信号：用户按了取消。**不对外抛** —— 由 :meth:`PipelineJobService._run` 翻成状态。"""


@dataclass(frozen=True, slots=True)
class PipelineRequest:
    """一次"一路做到出片"的请求。"""

    task_id: str
    #: 落点（§04.2.9 的 ``--until``）。缺省一路到成片。
    until: TaskStatus = TaskStatus.COMPLETED
    voice: str | None = None
    profile_name: str | None = None
    seed: int | None = None
    subtitle: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "until": self.until.value,
            "voice": self.voice,
            "profile": self.profile_name,
            "seed": self.seed,
            "subtitle": self.subtitle,
        }


@dataclass(frozen=True, slots=True)
class PipelineJob:
    """一条流水线任务的快照（面板轮询的就是它）。"""

    id: str
    task_id: str
    until: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    #: 当前阶段：``starting`` / ``voice`` / ``render`` / ``done``（由进度回调写）
    stage: str = "queued"
    done: int = 0
    total: int = 0
    note: str = ""
    #: 走过的每一步（``PipelineReport.steps`` 的形状，跑完才有内容）
    steps: tuple[dict[str, Any], ...] = ()
    #: 成片路径（``final.mp4``）
    final: str | None = None
    #: QC 结论（``QualityReport`` 的形状）
    quality: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    remediation: str | None = None
    logs: tuple[str, ...] = ()
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.status in _RUNNING

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL

    @property
    def percent(self) -> int:
        """粗粒度百分比。``total<=0`` ⇒ 0（**不猜**：配音与渲染两段的权重不一样）。

        **跑完就是 100%**，不看最后一次回调落在哪个分母上：这条链路的进度回调不是
        连续的（配音按句、渲染按段、投递与拼母带那几步根本不回调），最后停在哪取决于
        它是从哪一步收尾的。进度条停在 37% 而旁边写着"已完成"，是最容易被当成
        "卡住了"的一种显示 —— 而用户此刻正要去点播放。
        """
        if self.status == "succeeded":
            return 100
        if self.total <= 0:
            return 0
        return min(100, max(0, round(self.done * 100 / self.total)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "until": self.until,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "note": self.note,
            "percent": self.percent,
            "steps": list(self.steps),
            "final": self.final,
            "quality": self.quality,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "remediation": self.remediation,
            "logs": list(self.logs),
            "request": self.request,
        }


class PipelineJobService:
    """进程内的流水线登记表 + 一个串行的工作线程。

    线程模型与 :class:`~studio.services.render_job_service.RenderJobService` 一致：
    **一个**常驻工作线程 + 一个待办队列。提交只入队并立刻返回，所以 HTTP 那边永远是
    毫秒级响应。
    """

    def __init__(
        self,
        *,
        paths: StudioPaths,
        outputs: OutputsConfig | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._paths = paths
        self._outputs = outputs
        #: 读任务状态、投配音作业、拼母带、回填 QC 全都要它。
        #:
        #: 是**工厂**不是连接：活跑在工作线程里，而 sqlite3 连接是线程亲和的。
        #: 应用层传 ``state.connections.get``（每线程一条）即可。
        self._connection_factory = connection_factory
        self._lock = threading.Lock()
        self._jobs: dict[str, PipelineJob] = {}
        self._order: deque[str] = deque()
        self._pending: deque[str] = deque()
        self._requests: dict[str, PipelineRequest] = {}
        self._canceled: set[str] = set()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._counter = 0

    # ── 生命周期 ────────────────────────────────────────────────────

    def start(self) -> None:
        """起工作线程（幂等）。由应用 lifespan 调一次。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop, name="studio-pipeline-jobs", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """请求收工（**不等当前任务跑完**：进程都要退了，等它没有意义）。"""
        self._wake.set()

    # ── 读 ──────────────────────────────────────────────────────────

    def get(self, job_id: str) -> PipelineJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> tuple[PipelineJob, ...]:
        """最近的若干条，**新的在前**。"""
        with self._lock:
            ids = list(self._order)[:limit]
            return tuple(self._jobs[job_id] for job_id in ids if job_id in self._jobs)

    def active(self) -> PipelineJob | None:
        """当前在跑 / 排队的那一条（面板顶部那条进度条读它）。"""
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is not None and job.running:
                    return job
            return None

    def active_for(self, task_id: str) -> PipelineJob | None:
        """这个任务当前有没有在跑的（``submit`` 的幂等判据，面板也读它来禁用按钮）。"""
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is not None and job.running and job.task_id == task_id:
                    return job
            return None

    # ── 写 ──────────────────────────────────────────────────────────

    def submit(self, request: PipelineRequest) -> PipelineJob:
        """登记一次"一路做到出片"；**立刻返回**，活由工作线程干。

        **同一个任务已有在跑的 job ⇒ 原样返回那一条**，不新建。连点两次"开始出片"是很自然的
        动作（面板卡顿时尤其如此），而两条链路同时改一个任务的状态，轻则互相踩状态迁移
        （乐观锁会把一条踢成 ``CONCURRENT_MODIFICATION``），重则两条都渲染一遍。
        返回已有那一条之后，面板照样能拿到进度 —— 用户看到的是"它还在跑"，而不是第二条。
        """
        existing = self.active_for(request.task_id)
        if existing is not None:
            return existing
        with self._lock:
            self._counter += 1
            job_id = f"p{self._counter:04d}"
            job = PipelineJob(
                id=job_id,
                task_id=request.task_id,
                until=request.until.value,
                status="queued",
                created_at=now_iso(),
                request=request.to_dict(),
            )
            self._jobs[job_id] = job
            self._order.appendleft(job_id)
            self._pending.append(job_id)
            self._requests[job_id] = request
            self._trim()
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> PipelineJob | None:
        """请求取消。

        **协作式**：排队中的立刻作废；已经在跑的在下一次进度回调处停下。检查点只在
        **配音的每一句**与**渲染的每一段** —— 投递与拼母带那几步之间按取消，要等它进到
        下一个回调点。这一点如实写在 ``note`` 里。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.finished:
                return job
            self._canceled.add(job_id)
            if job.status == "queued":
                updated = replace(
                    job,
                    status="canceled",
                    finished_at=now_iso(),
                    note="已取消（还没开始跑）",
                )
                self._jobs[job_id] = updated
                return updated
        self._wake.set()
        return self.get(job_id)

    # ── 内部 ────────────────────────────────────────────────────────

    def _trim(self) -> None:
        """只留最近 ``MAX_JOBS_KEPT`` 条（**在跑的不删**）。调用方已持锁。"""
        while len(self._order) > MAX_JOBS_KEPT:
            victim = self._order[-1]
            job = self._jobs.get(victim)
            if job is not None and job.running:
                break
            self._order.pop()
            self._jobs.pop(victim, None)
            self._requests.pop(victim, None)
            self._canceled.discard(victim)

    def _loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    job_id = self._pending.popleft() if self._pending else None
                if job_id is None:
                    break
                try:
                    self._run(job_id)
                except Exception:
                    logger.exception("pipeline_job.worker_crashed", job_id=job_id)

    def _update(self, job_id: str, **changes: Any) -> PipelineJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            updated = replace(job, **changes)
            self._jobs[job_id] = updated
            return updated

    def _append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            logs = (*job.logs, line)[-MAX_LOG_LINES:]
            self._jobs[job_id] = replace(job, logs=logs)

    def _is_canceled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._canceled

    def _fail(
        self,
        job_id: str,
        *,
        code: ErrorCode,
        message: str,
        remediation: str | None = None,
    ) -> None:
        self._update(
            job_id,
            status="failed",
            finished_at=now_iso(),
            error_code=str(code),
            error_message=message,
            remediation=remediation,
            note="失败",
        )
        self._append_log(job_id, f"[error] {code} {message}")
        logger.warning("pipeline_job.failed", job_id=job_id, code=str(code))

    def _run(self, job_id: str) -> None:
        """真正跑一次流水线（**唯一**的执行点）。"""
        with self._lock:
            request = self._requests.get(job_id)
        if request is None:
            return

        if self._is_canceled(job_id):
            self._update(job_id, status="canceled", finished_at=now_iso(), note="已取消（开始前）")
            return

        if self._connection_factory is None:
            # 这条链路**必须**读库（任务状态 / 句子 / 时间轴），没有连接工厂就一步都走不了。
            # 与 render_jobs 不同：那边可以直接拿面板上填的文案出片，这里不行。
            self._fail(
                job_id,
                code=ErrorCode.INTERNAL,
                message="这个服务没有数据库连接工厂，读不到任务状态",
                remediation="由 API 进程起这个服务（build_state 会传 state.connections.get）",
            )
            return

        self._update(job_id, status="running", started_at=now_iso(), stage="starting", note="开始")
        self._append_log(job_id, f"[job] 任务 {request.task_id} → {request.until.value}")

        def progress(stage: str, done: int, total: int, note: str) -> None:
            # 进度回调是**唯一的取消检查点**（配音的每一句 / 渲染的每一段）。
            if self._is_canceled(job_id):
                raise _JobCanceledError
            self._update(job_id, stage=stage, done=done, total=total, note=note)
            self._append_log(job_id, f"[{stage}] {done}/{total} {note}")

        connection = self._connection_factory()
        try:
            report = run_task(
                task_id=request.task_id,
                paths=self._paths,
                outputs=self._resolve_outputs(),
                outputs_source=self._paths.config_dir / "outputs.yaml",
                connection=connection,
                until=request.until,
                profile_name=request.profile_name,
                voice=request.voice,
                seed=request.seed,
                subtitle=request.subtitle,
                streaming_render=self._streaming_render(),
                on_progress=progress,
            )
        except _JobCanceledError:
            self._update(
                job_id,
                status="canceled",
                finished_at=now_iso(),
                note="已取消（配音或渲染中途停下；已产出的句子音频与成片可续跑）",
            )
            self._append_log(job_id, "[job] 已取消")
            return
        except StudioError as exc:
            self._fail(job_id, code=exc.code, message=exc.message, remediation=exc.remediation)
            return
        except Exception as exc:
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                error_code=str(ErrorCode.INTERNAL),
                error_message=f"未预期的错误：{exc}",
                remediation="看 data/logs/api.log 的 traceback",
                note="失败（未预期）",
            )
            self._append_log(job_id, f"[error] {traceback.format_exc()[-500:]}")
            logger.exception("pipeline_job.crashed", job_id=job_id)
            return
        finally:
            connection.close()

        payload = report.to_dict()
        final = payload.get("final")
        note = "出片完成" if final else f"已推到 {report.status}（落点 {report.until}）"
        self._update(
            job_id,
            status="succeeded",
            finished_at=now_iso(),
            stage="done",
            note=note,
            steps=tuple(payload.get("steps") or ()),
            final=final if isinstance(final, str) else None,
            quality=payload.get("quality") if isinstance(payload.get("quality"), dict) else None,
        )
        self._append_log(job_id, f"[done] {note}")

    def _resolve_outputs(self) -> OutputsConfig:
        """出片配置：注入的优先，没有就读盘（与 ``RenderJobService`` 同一条）。"""
        if self._outputs is not None:
            return self._outputs
        return load_outputs_config(self._paths.config_dir / "outputs.yaml")

    def _streaming_render(self) -> bool:
        """``app.yaml → pipeline.streaming_render``（C7，一期恒 false）。

        **如实读**，不写死：写死的话，改了 YAML 的人只会看到"改了没生效"，而那种故障最难查。
        值为 true 时 :func:`~studio.services.pipeline_service.run_task` 会带着补救说明抛错 ——
        那正是我们要的（一期以人声实测总时长为唯一基准）。
        """
        return bool(load_app_config(self._paths).pipeline.streaming_render)


def supported_until() -> tuple[str, ...]:
    """``--until`` 允许的落点（面板上的下拉框读它）。**唯一真相**在 ``pipeline_service``。"""
    return tuple(item.value for item in SUPPORTED_UNTIL)
