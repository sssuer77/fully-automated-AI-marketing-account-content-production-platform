"""出片任务登记表（T4.6）—— 把「跑一次渲染」变成**可以点、可以看进度、可以查结果**的东西。

为什么需要一个"登记表"而不是直接同步调用
----------------------------------------
一次出片是 10 秒到几分钟的活（配音 + 编码），塞进 HTTP 请求里就是"点一下，转圈两分钟，
然后可能超时断开，而你完全不知道它跑到哪一步了"。所以：请求只负责**登记一个任务**并立刻
返回，真正的活在后台线程里跑，面板轮询进度。

为什么是**进程内**登记表，不是 DB 表 + 队列
------------------------------------------
诚实说明：渲染池（`render`）的单元处理器还没落地（`pools.runner.HANDLERS` 里没有它），
把出片接进队列需要先补 handler + 拉起 render 池进程。那是更大的改动。这里先用最直接
的一条路把"能点出片"做出来，**代价写在明处**：

- 进程重启 ⇒ 在跑的任务信息丢失（成片本身已经在盘上，不受影响）；
- 只在本进程内可见（多开一个 API 进程，两边各看各的）。

将来接进渲染池时，替换的是 :meth:`RenderJobService._run` 里的执行方式，对外形状不变。

为什么并发上限是 1
------------------
`config/pools.yaml` 的 render 池就是 ``concurrency: 1``（与 voice 共享显存 / 编码吃满 CPU）。
这里同样串行：同时开两条 1080×1920 的编码只会让两条都变慢，还会让 ffmpeg 抢内存
（这台机器上 ffmpeg 已经因为内存不足失败过一次）。
"""

from __future__ import annotations

import sqlite3
import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.config import OutputsConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.domain.errors import TaskNotFound
from studio.domain.task_service import TaskService
from studio.services.render_service import (
    ProduceRequest,
    ProduceResult,
    produce_video,
    quality_report,
)
from studio.services.script_service import read_active_script

__all__ = [
    "MAX_JOBS_KEPT",
    "MAX_LOG_LINES",
    "RenderJob",
    "RenderJobService",
    "RenderVideo",
    "list_videos",
]

logger = get_logger("studio.services.render_jobs")

#: 登记表保留多少个任务（旧的滚掉；成片本身在盘上，不受影响）
MAX_JOBS_KEPT: Final[int] = 50

#: 每个任务留多少行日志尾巴（面板上那个滚动框）
MAX_LOG_LINES: Final[int] = 200

#: 任务的两种状态集合，供面板分流显示
_RUNNING: Final[frozenset[str]] = frozenset({"queued", "running"})
_TERMINAL: Final[frozenset[str]] = frozenset({"succeeded", "failed", "canceled"})


class _JobCanceledError(Exception):
    """内部信号：用户按了取消。**不对外抛** —— 由 :meth:`RenderJobService._run` 翻成状态。"""


@dataclass(frozen=True, slots=True)
class RenderVideo:
    """盘上的一支成片（面板列表里的一行）。"""

    name: str
    path: Path
    size_bytes: int
    modified_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": f"/api/v1/render/videos/{self.name}",
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True, slots=True)
class RenderJob:
    """一次出片的全部可见状态（面板轮询的就是它）。"""

    id: str
    task_id: str
    status: str
    stage: str = ""
    done: int = 0
    total: int = 0
    note: str = ""
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    remediation: str | None = None
    result: dict[str, Any] | None = None
    logs: tuple[str, ...] = ()
    #: 请求原文（面板要显示"这条是用什么参数跑的"，也用于"再来一条"）
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.status in _RUNNING

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL

    @property
    def percent(self) -> int:
        """粗粒度百分比。``total<=0`` ⇒ 0（**不猜**：配音与渲染两段的权重不一样）。"""
        if self.total <= 0:
            return 0
        return min(100, max(0, round(self.done * 100 / self.total)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "percent": self.percent,
            "note": self.note,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "remediation": self.remediation,
            "result": self.result,
            "logs": list(self.logs),
            "request": self.request,
        }


def list_videos(paths: StudioPaths) -> tuple[RenderVideo, ...]:
    """盘上的成片，**新的在前**（面板打开就想看到刚出的那支）。

    只认 ``*.mp4``：``data/output/videos/`` 里不该有别的东西，真有也不该当成成片列出来。
    """
    directory = paths.videos_dir
    if not directory.is_dir():
        return ()
    found: list[RenderVideo] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".mp4":
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        found.append(
            RenderVideo(
                name=entry.name,
                path=entry,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
    found.sort(key=lambda item: item.modified_at, reverse=True)
    return tuple(found)


class RenderJobService:
    """进程内的出片任务登记表 + 一个串行的工作线程。

    线程模型：**一个** 常驻工作线程 + 一个队列。提交任务只入队并立刻返回，
    所以 HTTP 那边永远是毫秒级响应。
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
        #: 读生效稿件要用它（"不给文案就用库里的稿"）。为 ``None`` ⇒ 只能用 ``text``。
        #:
        #: 是**工厂**不是连接：活跑在工作线程里，而 sqlite3 连接是线程亲和的。
        #: 传一个连接进来，工作线程要么撞上 "SQLite objects created in a thread can
        #: only be used in that same thread"，要么被迫自己去猜连接怎么造。
        #: 应用层传 ``state.connections.get``（每线程一条）即可。
        self._connection_factory = connection_factory
        self._lock = threading.Lock()
        self._jobs: dict[str, RenderJob] = {}
        self._order: deque[str] = deque()
        self._pending: deque[str] = deque()
        self._requests: dict[str, ProduceRequest] = {}
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
            self._worker = threading.Thread(target=self._loop, name="studio-render-jobs", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """请求收工（**不等当前任务跑完**：进程都要退了，等它没有意义）。"""
        self._wake.set()

    # ── 读 ──────────────────────────────────────────────────────────

    def get(self, job_id: str) -> RenderJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> tuple[RenderJob, ...]:
        """最近的若干条，**新的在前**。"""
        with self._lock:
            ids = list(self._order)[:limit]
            return tuple(self._jobs[job_id] for job_id in ids if job_id in self._jobs)

    def active(self) -> RenderJob | None:
        """当前在跑 / 排队的那一条（面板顶部那条进度条读它）。"""
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is not None and job.running:
                    return job
            return None

    # ── 写 ──────────────────────────────────────────────────────────

    def submit(self, request: ProduceRequest) -> RenderJob:
        """登记一次出片；**立刻返回**，活由工作线程干。"""
        with self._lock:
            self._counter += 1
            job_id = f"r{self._counter:04d}"
            job = RenderJob(
                id=job_id,
                task_id=request.task_id,
                status="queued",
                created_at=now_iso(),
                request=_request_summary(request),
            )
            self._jobs[job_id] = job
            self._order.appendleft(job_id)
            self._pending.append(job_id)
            self._requests[job_id] = request
            self._trim()
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> RenderJob | None:
        """请求取消。

        **协作式**：排队中的立刻作废；已经在跑的在下一次进度回调处停下。
        ffmpeg 一旦跑起来，要等它自己结束 —— 这一点如实写在面板上，
        否则用户会以为按了取消就立刻停了。
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
                    logger.exception("render_job.worker_crashed", job_id=job_id)

    def _update(self, job_id: str, **changes: Any) -> RenderJob | None:
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

    def _run(self, job_id: str) -> None:
        """真正跑一次出片（**唯一**的执行点）。"""
        with self._lock:
            request = self._requests.get(job_id)
        if request is None:
            return

        if self._is_canceled(job_id):
            self._update(job_id, status="canceled", finished_at=now_iso(), note="已取消（开始前）")
            return

        self._update(job_id, status="running", started_at=now_iso(), note="开始")
        self._append_log(job_id, f"[job] 任务 {request.task_id} 开始")

        def progress(stage: str, done: int, total: int, note: str) -> None:
            # 进度回调是**唯一的取消检查点**：它会在这条链路的每一句配音、每一段之间被调到。
            if self._is_canceled(job_id):
                raise _JobCanceledError
            self._update(job_id, stage=stage, done=done, total=total, note=note)
            self._append_log(job_id, f"[{stage}] {done}/{total} {note}")

        try:
            result = self._produce(request, on_progress=progress)
        except _JobCanceledError:
            self._update(
                job_id,
                status="canceled",
                finished_at=now_iso(),
                note="已取消（配音或渲染中途停下；已产出的句子音频可续传）",
            )
            self._append_log(job_id, "[job] 已取消")
            return
        except StudioError as exc:
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                error_code=str(exc.code),
                error_message=exc.message,
                remediation=exc.remediation,
                note="失败",
            )
            self._append_log(job_id, f"[error] {exc.code} {exc.message}")
            logger.warning("render_job.failed", job_id=job_id, code=str(exc.code))
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
            logger.exception("render_job.crashed", job_id=job_id)
            return

        note = "出片完成"
        try:
            note = self._record_quality(result) or note
        except StudioError as exc:
            # QC 回填失败**不翻掉这次出片**：片子已经在盘上、manifest 也在，
            # 丢的只是"库里那一行 QC 结论"。把它记进日志与面板 note，让人看得见。
            self._append_log(job_id, f"[warn] quality_json 回填失败：{exc.message}")
            note = f"出片完成（QC 未回填：{exc.message}）"

        self._update(
            job_id,
            status="succeeded",
            finished_at=now_iso(),
            stage="done",
            note=note,
            result=result.to_dict(),
        )
        self._append_log(job_id, f"[done] {result.final.as_posix()}")

    def _record_quality(self, result: ProduceResult) -> str | None:
        """把 QC 结论回填到 ``tasks.quality_json``；返回一句给面板看的补充说明。

        任务不在库里（面板上直接填文案、没有真实任务那种）⇒ 返回说明，**不报错**：
        "直接填文案出片"本来就是允许脱离任务的，为了回填 QC 而把这条路堵死，
        等于用一个附属功能否掉主功能。
        """
        if self._connection_factory is None:
            return "任务不在库里，QC 结论只写进了 manifest.json"
        report = quality_report(result)
        service = TaskService(self._connection_factory())
        try:
            service.set_quality(result.task_id, report)
        except TaskNotFound:
            return "任务不在库里，QC 结论只写进了 manifest.json"
        return None

    def _produce(
        self, request: ProduceRequest, *, on_progress: Callable[[str, int, int, str], None]
    ) -> ProduceResult:
        """调出片服务。文案缺省时从库里读生效稿件（与 CLI 同一条规则）。"""
        text = request.text
        if not text.strip():
            text = self._script_text(request.task_id)

        return produce_video(
            replace(request, text=text),
            paths=self._paths,
            outputs=self._outputs,
            outputs_source=self._paths.config_dir / "outputs.yaml",
            on_progress=on_progress,
        )

    def _script_text(self, task_id: str) -> str:
        """库里那一版生效稿件的正文（**逐句拼接**，与配音要读的东西一致）。"""
        if self._connection_factory is None:
            raise StudioError(
                f"任务 {task_id} 没有给文案，而这一路又没有数据库连接",
                code=ErrorCode.SCRIPT_NOT_FOUND,
                context={"task_id": task_id},
                remediation="在面板上直接填口播文案（或在 API 进程里带连接工厂起这个服务）",
            )
        payload = read_active_script(self._connection_factory(), task_id)
        if payload is None:
            raise StudioError(
                f"任务 {task_id} 没有生效稿件，也没有给文案",
                code=ErrorCode.SCRIPT_NOT_FOUND,
                context={"task_id": task_id},
                remediation="先在「稿件」面板出一版稿，或在本面板直接填口播文案",
            )
        _script, sentences = payload
        return "".join(row.text for row in sentences)


def _request_summary(request: ProduceRequest) -> dict[str, Any]:
    """请求的可见摘要（**不含全文文案**：它可能很长，面板上显示前 60 字就够）。"""
    text = request.text.strip()
    return {
        "task_id": request.task_id,
        "profile": request.profile_name,
        "voice": request.voice,
        "reuse_voice": request.reuse_voice,
        "seed": request.seed,
        "subtitle": request.subtitle,
        "text_preview": text[:60] + ("…" if len(text) > 60 else ""),
        "text_chars": len(text),
    }
