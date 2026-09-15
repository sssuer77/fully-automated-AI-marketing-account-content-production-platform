"""Worker 生命周期集成测试（T1.6 验收 · §04.5.1 / §03.4）。

三条验收（todolist T1.6）
-------------------------
① ``SIGINT`` ⇒ **跑完当前单元**再退出（不丢进度）
② 心跳超 15s ⇒ 标记 ``dead`` ⇒ supervisor 重启
③ 重启后租约被 sweeper 回收并重新认领

外加：draining 语义、优雅退出摘心跳、租约丢失丢弃产物、协作式超时、
不可重试错误直接死信、``.partial`` 原子提交、空池退避不 busy-loop、
池暂停时不认领、依赖解锁、supervisor 重启上限与 ``stop_all``。

两个测试设计要点
----------------
1. **worker 自己开连接**（``connection=None``）。``sqlite3`` 连接默认线程亲和，
   把 pytest 主线程的连接塞给 worker 线程会直接 ``ProgrammingError``；
   生产也是这个语义（每线程一条连接），所以测试不改这个行为。
2. ``PoolWorker.run()`` 内部用**真实** ``utc_now()``（生产语义，不该被注入），
   所以把 ``T0`` 锚在"半小时前"：``not_before`` 落在过去，字符串比较仍与真实时间一致。
"""

from __future__ import annotations

import signal
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from studio.core.clock import utc_now
from studio.core.config import PoolConfig, UnitType
from studio.core.errors import ConfigError, ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import JobStore, connect
from studio.db.migrate import migrate
from studio.pools.heartbeat import HeartbeatStore
from studio.pools.runner import (
    HANDLERS,
    handler_for,
    install_signal_handlers,
    register_handler,
    restore_signal_handlers,
)
from studio.pools.supervisor import Supervisor, WorkerSpec
from studio.pools.worker_base import (
    PoolWorker,
    UnitAborted,
    UnitContext,
    UnitTimeout,
    commit_partial,
)

T0 = (utc_now() - timedelta(minutes=30)).replace(microsecond=0)

# ══════════════════════════════════════════════════════════════════════
# 夹具与假件
# ══════════════════════════════════════════════════════════════════════


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 队列门面（**主线程专用**）。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    store: JobStore


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    connection.execute("INSERT INTO tasks(id, title) VALUES ('t1', '测试任务')")
    try:
        yield Rig(paths=paths, connection=connection, store=JobStore(connection))
    finally:
        connection.close()


def _open(rig: Rig) -> sqlite3.Connection:
    """在**当前线程**新开一条连接（worker 线程里用；sqlite3 连接是线程亲和的）。"""
    return connect(rig.paths.db_file)


def _pool_config(
    *,
    unit_type: UnitType = "task",
    concurrency: int = 4,
    poll_ms: int = 50,
    lease_sec: int = 180,
    unit_timeout_sec: int = 60,
    max_attempts: int = 3,
    backoff_base_ms: int = 1000,
    backoff_max_ms: int = 5000,
) -> PoolConfig:
    return PoolConfig(
        unit_type=unit_type,
        concurrency=concurrency,
        poll_ms=poll_ms,
        lease_sec=lease_sec,
        backoff_base_ms=backoff_base_ms,
        backoff_max_ms=backoff_max_ms,
        unit_timeout_sec=unit_timeout_sec,
        max_attempts=max_attempts,
        priority=100,
    )


class FakeHandler:
    """可编程假 handler：记录调用、可阻塞、可抛错、可插自定义动作。"""

    def __init__(self, *, unit_types: tuple[str, ...] = ("task",), block: bool = False) -> None:
        self.unit_types = frozenset(unit_types)
        self.calls: list[str] = []
        self.contexts: list[UnitContext] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error: BaseException | None = None
        self.action: Any = None
        if not block:
            self.release.set()

    def run(self, ctx: UnitContext) -> Mapping[str, Any] | None:
        self.calls.append(ctx.unit_ref)
        self.contexts.append(ctx)
        self.entered.set()
        self.release.wait(20.0)
        if self.action is not None:
            self.action(ctx)
        if self.error is not None:
            raise self.error
        return {"unit_ref": ctx.unit_ref, "attempt": ctx.attempt}


class FakeProcess:
    """``subprocess.Popen`` 的结构替身（只实现 supervisor 用到的那几个方法）。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self._exit_code: int | None = None

    def exit(self, code: int = 0) -> None:
        self._exit_code = code

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code or 0


def _enqueue(rig: Rig, unit_ref: str, **kwargs: object) -> str:
    params: dict[str, Any] = {
        "task_id": "t1",
        "pool": "draft",
        "unit_type": "task",
        "unit_ref": unit_ref,
    }
    params.update(kwargs)
    job_id = rig.store.enqueue(**params)
    assert job_id is not None
    return job_id


def _worker(
    rig: Rig,
    handler: FakeHandler,
    *,
    pool: str = "draft",
    worker_id: str | None = None,
    config: PoolConfig | None = None,
    unit_timeout_sec: int | None = None,
) -> PoolWorker:
    """组装 worker —— **不传** ``connection``，让它在自己线程里开连接。"""
    return PoolWorker(
        pool=pool,
        handler=handler,
        pool_config=config or _pool_config(),
        paths=rig.paths,
        worker_id=worker_id,
        heartbeat_interval_sec=0.05,
        pulse_tick_sec=0.05,
        unit_timeout_sec=unit_timeout_sec,
    )


#: 本文件所有“后台线程里跑 worker”的登记表（由 ``_leak_guard`` 兜底收尾）
_RUNNING: list[tuple[PoolWorker, threading.Thread]] = []


@pytest.fixture(autouse=True)
def _leak_guard() -> Iterator[None]:
    """兜底：测试结束时仍在跑的 worker 一律请求停止并等它退干净。

    为什么必须兜底：``run()`` 没收到 ``request_stop()`` 就**永不返回**（这是生产语义，
    不该为测试而改）。漏停一个 worker 就漏一条**永不停歇**的脉冲线程 —— 它会一直
    往已被删除的临时库里写心跳（``no such table``），把后续测试的 stderr 刷成噪音，
    还会掩盖真问题（本文件的 ``test_graceful_exit_forgets_heartbeat`` 就被这样坑过）。
    """
    _RUNNING.clear()
    try:
        yield
    finally:
        for worker, thread in _RUNNING:
            worker.request_stop("test-teardown")
            thread.join(15.0)
            assert not thread.is_alive(), f"worker {worker.worker_id} 未在 15s 内退出"
        _RUNNING.clear()


def _run_in_thread(worker: PoolWorker, **kwargs: Any) -> threading.Thread:
    thread = threading.Thread(target=lambda: worker.run(**kwargs), daemon=True)
    _RUNNING.append((worker, thread))
    thread.start()
    return thread


def _wait_for(predicate: Any, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ══════════════════════════════════════════════════════════════════════
# ① SIGINT ⇒ 跑完当前单元再退出（不丢进度）
# ══════════════════════════════════════════════════════════════════════


def test_draining_finishes_current_unit_then_exits(rig: Rig) -> None:
    """停止请求只置标志：在途单元必须跑完（P2 断点续传的前提）。"""
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler)

    thread = _run_in_thread(worker)
    assert handler.entered.wait(15.0), "worker 没有进入 handler"

    worker.request_stop("signal:SIGINT")
    assert worker.draining is True
    assert thread.is_alive(), "停止请求不该打断在途单元"
    assert worker.status == "draining"

    handler.release.set()
    thread.join(15.0)
    assert not thread.is_alive()

    assert rig.store.get(job_id).status == "succeeded"
    assert len(handler.calls) == 1, "draining 之后不得再认领新单元"


def test_sigint_signal_handler_sets_draining(rig: Rig) -> None:
    """真的把 ``SIGINT`` 接上去 —— 不是只测 ``request_stop()``。"""
    worker = _worker(rig, FakeHandler())
    previous = install_signal_handlers(worker, signals=[int(signal.SIGINT)])
    try:
        assert worker.draining is False
        signal.raise_signal(signal.SIGINT)
        # 读进局部变量再断言：上一行会把 ``worker.draining`` 收窄成 ``Literal[False]``，
        # 紧跟着再写 ``assert worker.draining is True`` 会被 mypy 判成"永远不成立 ⇒ 后续不可达"
        observed = (worker.draining, worker.status)
    finally:
        restore_signal_handlers(previous)
    assert observed == (True, "draining"), "SIGINT 必须把 worker 置成 draining"
    assert signal.getsignal(signal.SIGINT) is previous[int(signal.SIGINT)]


def test_run_returns_report_on_draining(rig: Rig) -> None:
    worker = _worker(rig, FakeHandler())
    worker.request_stop("signal:SIGINT")
    report = worker.run()
    assert report.stop_reason == "signal:SIGINT"
    assert report.units_done == 0
    assert report.worker_id.startswith("draft#1@")


def test_run_rejects_concurrent_run(rig: Rig) -> None:
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler)
    _enqueue(rig, "u1")
    thread = _run_in_thread(worker)
    assert handler.entered.wait(15.0)
    try:
        with pytest.raises(StudioError):
            worker.run()
    finally:
        worker.request_stop("test-teardown")
        handler.release.set()
        thread.join(15.0)


# ══════════════════════════════════════════════════════════════════════
# 心跳：报活 / 摘行 / 脉冲不死
# ══════════════════════════════════════════════════════════════════════


def test_heartbeat_reports_busy_with_current_job(rig: Rig) -> None:
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler, worker_id="draft#1@4242")
    beats = HeartbeatStore(rig.connection)

    thread = _run_in_thread(worker)
    assert handler.entered.wait(15.0)
    assert _wait_for(lambda: beats.read("draft#1@4242") is not None), "没有等到心跳"

    beat = beats.read("draft#1@4242")
    assert beat is not None
    assert beat.status == "busy"
    assert beat.current_job_id == job_id
    assert beat.pool == "draft"
    assert beat.pid is not None
    assert beat.rss_mb is not None and beat.rss_mb > 0

    worker.request_stop("signal:SIGINT")
    handler.release.set()
    thread.join(15.0)
    assert not thread.is_alive()


def test_graceful_exit_forgets_heartbeat(rig: Rig) -> None:
    """优雅退出摘行 ⇒ "行存在 ⇔ 进程应当在跑"，不留会被误判为猝死的行。"""
    _enqueue(rig, "u1")
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler, worker_id="draft#1@4242")
    beats = HeartbeatStore(rig.connection)

    thread = _run_in_thread(worker)
    assert handler.entered.wait(15.0)
    assert _wait_for(lambda: beats.read("draft#1@4242") is not None)

    worker.request_stop("signal:SIGINT")
    handler.release.set()
    thread.join(15.0)
    assert not thread.is_alive(), "优雅退出必须在 15s 内完成"
    assert beats.read("draft#1@4242") is None


def test_pulse_survives_failing_ticks(rig: Rig) -> None:
    """脉冲线程绝不能死：死了就"看着还活着但不再续租"，比直接崩更危险。"""
    _enqueue(rig, "u1")
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler, worker_id="draft#1@4242")

    thread = _run_in_thread(worker)
    assert handler.entered.wait(15.0)

    # 拆掉心跳表 ⇒ 每拍都失败；worker 必须照常跑完（不因脉冲报错而中断）
    rig.connection.execute("DROP TABLE worker_heartbeats")
    time.sleep(0.3)
    assert thread.is_alive()
    assert worker.draining is False

    worker.request_stop("signal:SIGINT")
    handler.release.set()
    thread.join(15.0)
    assert not thread.is_alive()
    assert worker.status in {"idle", "draining"}


# ══════════════════════════════════════════════════════════════════════
# 租约丢失 ⇒ 丢弃产物；单元超时 ⇒ 重排
# ══════════════════════════════════════════════════════════════════════


def test_lease_lost_discards_result(rig: Rig) -> None:
    """租约被回收后 ``renew()`` 必须返回 ``False``；结果不写库（否则覆盖别人的产物）。"""
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler()

    def steal(ctx: UnitContext) -> None:
        own = _open(rig)
        try:
            JobStore(own).reclaim_expired(pool="draft", now=utc_now() + timedelta(days=1))
        finally:
            own.close()
        assert ctx.renew() is False, "租约已被回收，续租必须失败"
        ctx.check_alive()  # 抛 UnitAborted

    handler.action = steal
    report = _worker(rig, handler).run(max_units=1)

    assert report.units_done == 1
    assert report.units_aborted == 1
    job = rig.store.get(job_id)
    assert job.status == "pending", "aborted 的单元不得被标记 succeeded"
    assert job.attempts == 1


def test_check_alive_raises_after_lease_lost(rig: Rig) -> None:
    handler = FakeHandler()
    seen: list[UnitContext] = []

    def probe(ctx: UnitContext) -> None:
        seen.append(ctx)
        ctx._pulse.mark_lease_lost()  # 直接驱动内部状态，避免等脉冲线程
        with pytest.raises(UnitAborted):
            ctx.check_alive()

    handler.action = probe
    _enqueue(rig, "u1")
    _worker(rig, handler).run(max_units=1)
    assert seen


def test_unit_timeout_fails_retryable(rig: Rig) -> None:
    """跑完才发现超时 ⇒ 按 ``UNIT_TIMEOUT`` 失败重排（协作式超时的兜底分支）。"""
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler(block=True)
    worker = _worker(rig, handler, unit_timeout_sec=1)

    def release_late() -> None:
        time.sleep(1.4)
        handler.release.set()

    threading.Thread(target=release_late, daemon=True).start()
    report = worker.run(max_units=1)

    assert report.units_failed == 1
    job = rig.store.get(job_id)
    assert job.status == "pending"
    assert job.error_code == str(ErrorCode.UNIT_TIMEOUT)
    assert job.not_before is not None


def test_handler_raised_unit_timeout_is_retryable(rig: Rig) -> None:
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler()
    handler.error = UnitTimeout("handler 自己发现超时")
    _worker(rig, handler).run(max_units=1)

    job = rig.store.get(job_id)
    assert job.status == "pending"
    assert job.error_code == str(ErrorCode.UNIT_TIMEOUT)


def test_unit_context_exposes_identity(rig: Rig) -> None:
    handler = FakeHandler()
    _enqueue(rig, "u1")
    _worker(rig, handler, worker_id="draft#1@4242").run(max_units=1)

    ctx = handler.contexts[0]
    assert ctx.job_id
    assert ctx.task_id == "t1"
    assert ctx.unit_ref == "u1"
    assert ctx.unit_type == "task"
    assert ctx.pool == "draft"
    assert ctx.worker_id == "draft#1@4242"
    assert ctx.attempt == 1
    assert ctx.remaining_sec > 0
    assert ctx.lease_lost is False
    assert ctx.timed_out is False


# ══════════════════════════════════════════════════════════════════════
# 失败分类：可重试 ⇒ 退避重排；不可重试 ⇒ 直接死信
# ══════════════════════════════════════════════════════════════════════


def test_retryable_studio_error_reschedules(rig: Rig) -> None:
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler()
    handler.error = StudioError("引擎暂时挂了", code=ErrorCode.TTS_ENGINE_UNAVAILABLE)
    report = _worker(rig, handler).run(max_units=1)

    assert report.units_failed == 1
    job = rig.store.get(job_id)
    assert job.status == "pending"
    assert job.error_code == str(ErrorCode.TTS_ENGINE_UNAVAILABLE)
    assert job.not_before is not None


def test_non_retryable_error_goes_dead_immediately(rig: Rig) -> None:
    """缺字体这类错误重试不会变好 ⇒ 第一次失败就死信 + 告警（早报警早能修）。"""
    job_id = _enqueue(rig, "u1", max_attempts=5)
    handler = FakeHandler()
    handler.error = ConfigError("字体缺失", code=ErrorCode.FONT_MISSING)
    report = _worker(rig, handler).run(max_units=1)

    assert report.units_failed == 1
    job = rig.store.get(job_id)
    assert job.status == "dead"
    assert job.error_code == str(ErrorCode.FONT_MISSING)
    assert rig.store.dead_letters(pool="draft")[0].id == job_id


def test_unknown_exception_is_retryable_internal(rig: Rig) -> None:
    """未知异常绝不能让 worker 循环炸掉：记 INTERNAL + 可重试。"""
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler()
    handler.error = ValueError("handler 写崩了")
    report = _worker(rig, handler).run(max_units=1)

    assert report.units_failed == 1
    job = rig.store.get(job_id)
    assert job.status == "pending"
    assert job.error_code == str(ErrorCode.INTERNAL)
    assert "ValueError" in (job.error_message or "")


# ══════════════════════════════════════════════════════════════════════
# 产物：先 .partial 再原子改名（陷阱 #9）
# ══════════════════════════════════════════════════════════════════════


def test_context_work_dir_and_partial_path(rig: Rig) -> None:
    handler = FakeHandler()
    _enqueue(rig, "u1")
    _worker(rig, handler).run(max_units=1)

    ctx = handler.contexts[0]
    work = ctx.work_dir()
    assert work == rig.paths.work_dir_for("t1")
    assert work.is_dir()
    assert ctx.partial_path("voice_master.wav").name == "voice_master.wav.partial"


def test_context_commit_is_atomic(rig: Rig) -> None:
    handler = FakeHandler()

    def write(ctx: UnitContext) -> None:
        partial = ctx.partial_path("out.txt")
        partial.write_text("成品", encoding="utf-8")
        final = ctx.commit(partial, ctx.work_dir() / "out.txt")
        assert final.read_text(encoding="utf-8") == "成品"
        assert not partial.exists()

    handler.action = write
    _enqueue(rig, "u1")
    _worker(rig, handler).run(max_units=1)
    assert (rig.paths.work_dir_for("t1") / "out.txt").is_file()


def test_commit_rejects_missing_partial(tmp_path: Path) -> None:
    with pytest.raises(StudioError) as excinfo:
        commit_partial(tmp_path / "nope.partial", tmp_path / "final.bin")
    assert excinfo.value.code is ErrorCode.INTERNAL


def test_commit_overwrites_existing_final(tmp_path: Path) -> None:
    """重做同一个单元时必须能覆盖旧产物（``Path.replace`` 是无条件替换）。"""
    partial = tmp_path / "a.partial"
    final = tmp_path / "a.bin"
    final.write_text("旧", encoding="utf-8")
    partial.write_text("新", encoding="utf-8")

    commit_partial(partial, final)
    assert final.read_text(encoding="utf-8") == "新"
    assert not partial.exists()


# ══════════════════════════════════════════════════════════════════════
# 循环控制：空池退避 / 池暂停 / 依赖解锁 / 跑满退出
# ══════════════════════════════════════════════════════════════════════


def test_empty_pool_backs_off_without_busy_loop(rig: Rig) -> None:
    """空池必须睡（禁 busy-loop），且连续空转后退出。"""
    worker = _worker(rig, FakeHandler(), config=_pool_config(poll_ms=50))
    started = time.monotonic()
    report = worker.run(max_empty_rounds=3)
    elapsed = time.monotonic() - started

    assert report.stop_reason == "idle"
    assert report.empty_rounds == 3
    assert report.units_done == 0
    assert elapsed >= 0.1, "50ms × 3 轮退避至少要花 150ms"
    assert elapsed < 10.0, "退避不该拖成长睡眠"


def test_max_units_stops_after_n(rig: Rig) -> None:
    for index in range(3):
        _enqueue(rig, f"u{index}")
    handler = FakeHandler()
    report = _worker(rig, handler).run(max_units=2)

    assert report.stop_reason == "max_units"
    assert report.units_done == 2
    assert len(handler.calls) == 2
    assert set(handler.calls) <= {"u0", "u1", "u2"}
    assert len(rig.store.list_jobs(pool="draft", status="succeeded")) == 2


def test_paused_pool_is_not_claimed(rig: Rig) -> None:
    _enqueue(rig, "u1")
    rig.connection.execute("UPDATE pool_settings SET paused = 1 WHERE pool = 'draft'")

    handler = FakeHandler()
    report = _worker(rig, handler).run(max_empty_rounds=2)

    assert report.units_done == 0
    assert handler.calls == []
    assert rig.store.list_jobs(pool="draft", status="pending")[0].attempts == 0


def test_unlock_dependents_runs_after_each_unit(rig: Rig) -> None:
    """上游成功 ⇒ worker 顺手解锁下游（不必等 sweeper 那一轮）。"""
    upstream = _enqueue(rig, "scene", unit_type="scene")
    downstream = _enqueue(rig, "final", unit_type="final", depends_on=[upstream])
    assert rig.store.get(downstream).status == "blocked"

    handler = FakeHandler(unit_types=("scene", "final"))
    _worker(rig, handler, config=_pool_config(unit_type="scene")).run(max_units=1)

    assert rig.store.get(upstream).status == "succeeded"
    assert rig.store.get(downstream).status == "pending"


def test_unit_result_is_persisted(rig: Rig) -> None:
    job_id = _enqueue(rig, "u1")
    handler = FakeHandler()
    _worker(rig, handler).run(max_units=1)

    job = rig.store.get(job_id)
    assert job.result == {"unit_ref": "u1", "attempt": 1}
    assert job.lease_owner is None
    assert job.finished_at is not None


# ══════════════════════════════════════════════════════════════════════
# ②③ 心跳超时 ⇒ dead ⇒ supervisor 重启 ⇒ 租约回收 ⇒ 重新认领
# ══════════════════════════════════════════════════════════════════════


def _supervisor(rig: Rig, spawn: Any, *, specs: tuple[WorkerSpec, ...] | None = None) -> Supervisor:
    return Supervisor(
        specs=specs or (WorkerSpec(pool="draft", slot=1, argv=("python", "-m", "studio.cli")),),
        paths=rig.paths,
        connection_factory=lambda: _open(rig),
        spawn=spawn,
    )


def test_supervisor_starts_and_reports_state(rig: Rig) -> None:
    processes = [FakeProcess(pid=4242)]
    supervisor = _supervisor(rig, lambda spec: processes.pop(0))

    supervisor.start_all()
    state = supervisor.state("draft#1")
    assert state.pid == 4242
    assert state.restarts == 0
    assert state.halted is False
    assert state.started_at is not None


def test_stale_heartbeat_marks_dead_and_restarts(rig: Rig) -> None:
    """② 心跳超 15s ⇒ 标记 ``dead`` ⇒ supervisor 杀掉僵进程并重启。"""
    HeartbeatStore(rig.connection).upsert(
        worker_id="draft#1@4242",
        pool="draft",
        status="busy",
        current_job_id="j1",
        now=T0,
    )
    first, second = FakeProcess(pid=4242), FakeProcess(pid=4243)
    queue = [first, second]
    supervisor = _supervisor(rig, lambda spec: queue.pop(0))
    supervisor.start_all()

    tick = supervisor.poll_once(now=T0 + timedelta(seconds=20))

    assert tick.dead_marked == ("draft#1@4242",)
    assert tick.restarted == ("draft#1",)
    assert supervisor.state("draft#1").pid == 4243
    assert supervisor.state("draft#1").restarts == 1
    assert first.terminated is True, "僵进程必须先被终止"
    assert HeartbeatStore(rig.connection).read("draft#1@4242").status == "dead"  # type: ignore[union-attr]

    row = rig.connection.execute(
        "SELECT * FROM system_logs WHERE payload_json LIKE '%WORKER_DEAD%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["level"] == "error"


def test_supervisor_restarts_exited_process(rig: Rig) -> None:
    first, second = FakeProcess(pid=100), FakeProcess(pid=101)
    queue = [first, second]
    supervisor = _supervisor(rig, lambda spec: queue.pop(0))
    supervisor.start_all()

    first.exit(code=3)
    tick = supervisor.poll_once(now=T0)

    assert tick.exited == ("draft#1",)
    assert tick.restarted == ("draft#1",)
    assert supervisor.state("draft#1").pid == 101
    assert supervisor.state("draft#1").last_exit_code == 3


def _spawner() -> tuple[Any, list[FakeProcess]]:
    """返回 ``(spawn, spawned)``：``spawned[-1]`` 就是"当前在跑的那个进程"。"""
    spawned: list[FakeProcess] = []

    def spawn(spec: WorkerSpec) -> FakeProcess:
        process = FakeProcess(pid=1000 + len(spawned))
        spawned.append(process)
        return process

    return spawn, spawned


def test_supervisor_respects_restart_backoff(rig: Rig) -> None:
    """重启后立刻再崩 ⇒ 必须等退避窗口，不能连着重启（重启风暴）。"""
    spawn, spawned = _spawner()
    spec = WorkerSpec(pool="draft", slot=1, argv=("x",), restart_base_ms=60_000, restart_max_ms=60_000)
    supervisor = _supervisor(rig, spawn, specs=(spec,))
    supervisor.start_all()

    spawned[-1].exit()
    assert supervisor.poll_once(now=T0).restarted == ("draft#1",)
    assert supervisor.state("draft#1").next_restart_at is not None

    spawned[-1].exit()
    assert supervisor.poll_once(now=T0 + timedelta(seconds=1)).restarted == ()
    assert supervisor.poll_once(now=T0 + timedelta(seconds=30)).restarted == ()

    # 窗口过后（60s 基数 + ≤20% 抖动）才允许下一次重启
    assert supervisor.poll_once(now=T0 + timedelta(minutes=5)).restarted == ("draft#1",)


def test_supervisor_halts_after_restart_limit(rig: Rig) -> None:
    """重启次数超限 ⇒ 停止重启并告警（不是无限重试）。"""
    spawn, spawned = _spawner()
    spec = WorkerSpec(pool="draft", slot=1, argv=("x",), restart_base_ms=1, restart_max_ms=1, restart_limit=2)
    supervisor = _supervisor(rig, spawn, specs=(spec,))
    supervisor.start_all()

    moment = T0
    for _ in range(2):
        spawned[-1].exit()
        moment = moment + timedelta(seconds=5)
        assert supervisor.poll_once(now=moment).restarted == ("draft#1",)

    spawned[-1].exit()
    tick = supervisor.poll_once(now=moment + timedelta(seconds=5))

    assert tick.restarted == ()
    assert tick.halted == ("draft#1",)
    assert supervisor.state("draft#1").halted is True

    row = rig.connection.execute(
        "SELECT * FROM system_logs WHERE payload_json LIKE '%WORKER_RESTART_HALTED%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["level"] == "error"


def test_supervisor_stop_all_terminates(rig: Rig) -> None:
    process = FakeProcess(pid=777)
    supervisor = _supervisor(rig, lambda spec: process)
    supervisor.start_all()
    supervisor.stop_all()

    assert process.terminated is True
    assert supervisor.state("draft#1").pid == 777  # 状态保留供事后排查


def test_supervisor_rejects_duplicate_labels(rig: Rig) -> None:
    specs = (
        WorkerSpec(pool="draft", slot=1, argv=("x",)),
        WorkerSpec(pool="draft", slot=1, argv=("y",)),
    )
    with pytest.raises(StudioError):
        _supervisor(rig, lambda spec: FakeProcess(pid=1), specs=specs)


def test_restart_then_lease_reclaimed_and_reclaimed(rig: Rig) -> None:
    """③ 判死 → sweeper 回收租约 → 重启后的 worker 重新认领（attempts 递增）。"""
    job_id = _enqueue(rig, "u1")
    claimed = rig.store.claim(pool="draft", worker_id="draft#1@4242", lease_sec=180, now=T0)
    assert claimed is not None
    HeartbeatStore(rig.connection).upsert(
        worker_id="draft#1@4242", pool="draft", status="busy", current_job_id=job_id, now=T0
    )

    # ① 判死
    assert HeartbeatStore(rig.connection).reap_stale(now=T0 + timedelta(seconds=20)) == ("draft#1@4242",)
    # ② sweeper 回收过期租约
    result = rig.store.reclaim_expired(pool="draft", now=T0 + timedelta(seconds=181))
    assert result.requeued == (job_id,)
    assert rig.store.get(job_id).status == "pending"

    # ③ 重启后的新 worker（新 pid）重新认领并跑完
    handler = FakeHandler()
    report = _worker(rig, handler, worker_id="draft#1@4243").run(max_units=1)

    assert report.units_done == 1
    job = rig.store.get(job_id)
    assert job.status == "succeeded"
    assert job.attempts == 2, "重做算第二次尝试（认领时 +1）"
    assert handler.calls == ["u1"]


# ══════════════════════════════════════════════════════════════════════
# 启动胶水：处理器注册表 / 信号安装
# ══════════════════════════════════════════════════════════════════════


def test_handler_for_reports_unregistered_pool() -> None:
    """没注册就启动 ⇒ 明确报错，而不是"起来了但什么都不干"。"""
    with pytest.raises(StudioError) as excinfo:
        handler_for("render")
    assert excinfo.value.code is ErrorCode.INTERNAL
    assert "T3.x" in (excinfo.value.remediation or "")


def test_register_handler_round_trip() -> None:
    handler = FakeHandler()
    register_handler("voice", handler)
    try:
        assert handler_for("voice") is handler
    finally:
        HANDLERS.pop("voice", None)


def test_register_handler_rejects_unknown_pool() -> None:
    with pytest.raises(StudioError):
        register_handler("encode", FakeHandler())


def test_install_signal_handlers_skips_off_main_thread(rig: Rig) -> None:
    """非主线程装不了信号处理 ⇒ 安静跳过，不是错误（worker 可能在子线程里起）。"""
    worker = _worker(rig, FakeHandler())
    captured: list[dict[int, Any]] = []
    thread = threading.Thread(
        target=lambda: captured.append(install_signal_handlers(worker, signals=[int(signal.SIGINT)]))
    )
    thread.start()
    thread.join(5.0)
    assert captured == [{}]


def test_restore_signal_handlers_is_idempotent(rig: Rig) -> None:
    worker = _worker(rig, FakeHandler())
    previous = install_signal_handlers(worker, signals=[int(signal.SIGINT)])
    restore_signal_handlers(previous)
    restore_signal_handlers(previous)
    assert signal.getsignal(signal.SIGINT) is previous[int(signal.SIGINT)]
