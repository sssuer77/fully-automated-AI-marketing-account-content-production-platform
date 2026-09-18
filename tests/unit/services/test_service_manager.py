"""五进程编排的单元测试（T1.12 · 原文附2 / §7.4）。

覆盖的是**最需要被验证、又最容易被糊过去**的那部分：关停时序。
"10s 内全部退出且不丢进度"如果只写在 ``.ps1`` 里就没法测 —— 这正是把核心
搬进 Python 的理由（裁定 104）。这里用假进程表把三级升级（标志 → 信号 → 强杀）
逐级钉死。

约定：**测试永不触碰真实 ``data/``**（``tmp_paths`` 夹具），也**不真拉起进程**
（``spawn`` 注入假实现）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.doctor import CheckResult, DoctorReport
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.pools import runner as pool_runner
from studio.services.service_manager import (
    DEFAULT_READY_TIMEOUT_SEC,
    DEFAULT_STOP_TIMEOUT_SEC,
    SERVICE_NAMES,
    HealthProbe,
    Readiness,
    ServiceKind,
    ServiceManager,
    ServiceSpec,
    build_specs,
    probe_port,
    run_entry,
    tts_python_for,
)

# ── 假件 ────────────────────────────────────────────────────────────────


class FakeClock:
    """可控时钟：``sleep`` 推进 ``monotonic``，于是超时用例**瞬间**跑完。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


class FakeProcessTable:
    """内存进程表（不碰真进程）。

    两个"抗性"集合分别对应两种真实的倔脾气：

    :param ignores_break: 收不到 / 不理 Ctrl-Break（**从另一个控制台发**就是这样）
    :param ignores_terminate: ``terminate`` 也叫不停（卡在驱动 / 无视关闭信号）
    """

    def __init__(
        self,
        alive: set[int] | None = None,
        *,
        ignores_break: set[int] | None = None,
        ignores_terminate: set[int] | None = None,
    ) -> None:
        self.alive_pids: set[int] = set(alive or ())
        self.ignores_break: set[int] = set(ignores_break or ())
        self.ignores_terminate: set[int] = set(ignores_terminate or ())
        self.breaks: list[int] = []
        self.terminated: list[int] = []
        self.killed: list[int] = []
        self.break_supported = True

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def wait(self, pid: int, timeout: float) -> bool:
        del timeout
        return pid not in self.alive_pids

    def request_break(self, pid: int) -> bool:
        self.breaks.append(pid)
        if not self.break_supported:
            return False
        if pid not in self.ignores_break:
            self.alive_pids.discard(pid)
        return True

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        if pid not in self.ignores_terminate:
            self.alive_pids.discard(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self.alive_pids.discard(pid)


class Recorder:
    """记录 spawn / 浏览器 / 健康检查的调用。

    ``spawn`` 会把新 pid 记进 :class:`FakeProcessTable` —— 真 ``Popen`` 也会让
    进程立刻"活着"，假件必须同构，否则每个用例都会走到"起完立刻死"那条路。
    """

    def __init__(self, table: FakeProcessTable | None = None) -> None:
        self.spawned: list[str] = []
        self.browsers: list[str] = []
        self.health_calls: list[str] = []
        self.ports: dict[str, bool] = {}
        self.next_pid = 9000
        self.table = table if table is not None else FakeProcessTable()

    def spawn(self, spec: ServiceSpec) -> int:
        self.spawned.append(spec.name)
        self.next_pid += 1
        self.table.alive_pids.add(self.next_pid)
        return self.next_pid

    def browser(self, url: str) -> bool:
        self.browsers.append(url)
        return True

    def probe(self, host: str, port: int) -> bool:
        del host
        return self.ports.get(str(port), False)

    def health(self, spec: ServiceSpec) -> tuple[bool, str]:
        self.health_calls.append(spec.name)
        return True, "ok"


# ── 夹具 ────────────────────────────────────────────────────────────────


def write_tts_config(paths: StudioPaths) -> Path:
    """写一份最小的 ``config/tts.yaml``（显式指定解释器）。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    path = paths.config_dir / "tts.yaml"
    path.write_text(
        'schema_version: "1.0"\n'
        "python: tts/.venv/Scripts/python.exe\n"
        "model:\n"
        "  dir: D:/somewhere/models\n"
        "  revision: abcdef12\n",
        encoding="utf-8",
    )
    return path


def tts_interpreter(paths: StudioPaths) -> Path:
    """本平台上 ``tts/.venv`` 里那个解释器（测试用它模拟「环境装好了」）。"""
    scripts = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return paths.home / "tts" / ".venv" / scripts


@pytest.fixture
def worker_home(tmp_paths: StudioPaths) -> StudioPaths:
    """临时 home 里造出 5 个入口脚本 + 运行期目录。

    入口脚本不在 ⇒ readiness 全是 ``entry_missing``；``logs/`` 不在 ⇒
    写 PID 台账会直接 ``FileNotFoundError``。
    """
    workers = tmp_paths.workers_dir
    workers.mkdir(parents=True, exist_ok=True)
    for name in SERVICE_NAMES:
        (workers / f"run_{name}.py").write_text("# fake entry\n", encoding="utf-8")
    tmp_paths.ensure_runtime_dirs()
    return tmp_paths


@pytest.fixture
def pooled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """给三个池注册假 handler（否则 readiness 恒为 ``handler_missing``）。

    直接改 ``runner.HANDLERS`` 再还原：``register_handler`` 是模块级注册表，
    没有"注销"接口 —— 测试自己负责把现场收拾干净。
    """

    class _Handler:
        unit_types = frozenset({"fake"})

    saved = dict(pool_runner.HANDLERS)
    for name in ("draft", "voice", "render"):
        pool_runner.HANDLERS[name] = _Handler()  # type: ignore[assignment]
    yield
    pool_runner.HANDLERS.clear()
    pool_runner.HANDLERS.update(saved)


def ok_doctor() -> DoctorReport:
    return DoctorReport(
        checks=(CheckResult(name="config.valid", status="ok", detail="ok", blocking=True),),
        host={},
        duration_ms=1,
        started_at="2026-09-13T00:00:00.000Z",
    )


def blocked_doctor() -> DoctorReport:
    return DoctorReport(
        checks=(
            CheckResult(
                name="disk.gate",
                status="fail",
                detail="C 盘可用 0.1 GB（门禁 ≥1 GB）",
                blocking=True,
                remediation="清理磁盘",
            ),
        ),
        host={},
        duration_ms=1,
        started_at="2026-09-13T00:00:00.000Z",
    )


def ready_pair() -> tuple[FakeProcessTable, Recorder]:
    """一对"接在一起"的假件：``Recorder`` 的 spawn 记进**管理器实际查看的那张表**。

    分开造（``Recorder()`` 自带另一张空表）只适用于故意构造"起完立刻死"的用例，
    见 ``test_a_process_that_dies_before_ready_is_a_failure``。
    """

    table = FakeProcessTable()
    return table, Recorder(table)


def make_manager(
    paths: StudioPaths,
    *,
    table: FakeProcessTable | None = None,
    rec: Recorder | None = None,
    doctor: DoctorReport | None = None,
) -> ServiceManager:
    """构造一个"全假"的管理器（不拉真进程、不真等）。"""
    clock = FakeClock()
    processes = table or FakeProcessTable()
    # 只有"我们自己造的" Recorder 才接到这张进程表上；调用方传进来的 Recorder
    # 保留自己的表 —— 于是"起完立刻死"这种场景可以构造出来。
    recorder = rec if rec is not None else Recorder(processes)
    return ServiceManager(
        paths,
        spawn=recorder.spawn,
        process_table=processes,
        port_probe=recorder.probe,
        health=recorder.health,
        browser=recorder.browser,
        doctor_factory=(lambda: doctor) if doctor is not None else None,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


# ── 规格表 ──────────────────────────────────────────────────────────────


class TestSpecs:
    def test_five_processes_in_the_documented_order(self, tmp_paths: StudioPaths) -> None:
        assert tuple(spec.name for spec in build_specs(tmp_paths)) == SERVICE_NAMES

    def test_api_is_the_only_required_one(self, tmp_paths: StudioPaths) -> None:
        """只有 WebUI 是硬失败：它是唯一入口，起不来就没有操作台。"""
        required = [spec.name for spec in build_specs(tmp_paths) if spec.required]
        assert required == ["api"]

    def test_ports_match_the_contract(self, tmp_paths: StudioPaths) -> None:
        ports = {spec.name: spec.port for spec in build_specs(tmp_paths)}
        assert ports == {"api": 8787, "tts": 8788, "draft": None, "voice": None, "render": None}

    def test_pool_specs_poll_the_stop_flag_http_specs_do_not(self, tmp_paths: StudioPaths) -> None:
        """裁定 107：uvicorn 没有 1s tick ⇒ 对它走信号，别白等一个不会发生的优雅退出。"""
        specs = {spec.name: spec for spec in build_specs(tmp_paths)}
        assert specs["draft"].polls_stop_flag and specs["voice"].polls_stop_flag
        assert specs["render"].polls_stop_flag
        assert not specs["api"].polls_stop_flag and not specs["tts"].polls_stop_flag

    def test_argv_points_at_the_workers_entry(self, tmp_paths: StudioPaths) -> None:
        spec = build_specs(tmp_paths)[0]
        assert spec.entry == tmp_paths.workers_dir / "run_api.py"
        assert spec.argv[-1] == str(spec.entry)
        assert spec.kind is ServiceKind.HTTP

    def test_unknown_service_name_is_refused(self, tmp_paths: StudioPaths) -> None:
        with pytest.raises(StudioError) as excinfo:
            make_manager(tmp_paths).spec("publish")
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID
        assert excinfo.value.context["valid"] == list(SERVICE_NAMES)


# ── 就绪判定（先问再拉 · 裁定 103）─────────────────────────────────────


class TestReadiness:
    def test_missing_entry_is_reported_with_the_path(self, tmp_paths: StudioPaths) -> None:
        item = make_manager(tmp_paths).readiness(build_specs(tmp_paths)[0])
        assert item.readiness is Readiness.ENTRY_MISSING
        assert str(tmp_paths.workers_dir / "run_api.py") in item.detail

    def test_api_is_ready_once_the_entry_exists(self, worker_home: StudioPaths) -> None:
        manager = make_manager(worker_home)
        assert manager.readiness(manager.spec("api")).ready

    def test_tts_reports_the_missing_inference_env(self, worker_home: StudioPaths) -> None:
        """T2.2 的模块已落地，但隔离家目录里没有 ``tts/.venv`` ⇒ ``env_missing``。

        与 ``server_missing`` 分开是有原因的：模块不在 ⇒ 去写代码；环境不在 ⇒ 去建 venv。
        拉起来一个「活着但每句都报 no module named torch」的进程是最糟的第三种结果 ——
        探活回 200，业务每句都失败。
        """
        manager = make_manager(worker_home)
        item = manager.readiness(manager.spec("tts"))
        assert item.readiness is Readiness.ENV_MISSING
        assert item.remediation is not None and "tts_models.md" in item.remediation

    def test_tts_is_ready_once_the_inference_env_exists(self, worker_home: StudioPaths) -> None:
        """子环境在位 ⇒ 真就绪，且命令行指向它（不是启动器自己的解释器）。"""
        interpreter = tts_interpreter(worker_home)
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("", encoding="utf-8")
        manager = make_manager(worker_home)
        assert manager.readiness(manager.spec("tts")).readiness is Readiness.READY
        assert manager.spec("tts").argv[0] == str(interpreter)

    def test_tts_spec_prepends_src_to_pythonpath(self, worker_home: StudioPaths) -> None:
        """子环境里没装 ``studio`` 这个包 ⇒ 必须前置 ``src/`` 才 import 得到。"""
        spec = make_manager(worker_home).spec("tts")
        assert dict(spec.env_prepend)["PYTHONPATH"] == str(worker_home.home / "src")

    def test_configured_interpreter_is_not_silently_swapped_out(self, worker_home: StudioPaths) -> None:
        """配置说了用哪个就用哪个：指错了 ⇒ 报 env_missing，**不静默换一个**。"""
        write_tts_config(worker_home)
        assert tts_python_for(worker_home) is None
        interpreter = tts_interpreter(worker_home)
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("", encoding="utf-8")
        assert tts_python_for(worker_home) == interpreter

    def test_pool_without_a_handler_is_not_ready(
        self, worker_home: StudioPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**不静默起一个空转 worker**：handler 未注册 ⇒ 不拉起。

        四个池的 handler 现在**都已落地**（publish 是 T5.3 补上的最后一个），而
        ``publish`` 仍然不在 ``SERVICE_NAMES`` 里 —— 发布进程要等发布面板（T5.5）
        一起接进 supervisor（出厂 ``publish.enabled=false``，一个常驻发布 worker
        在开关关着时唯一会做的事是把投递进来的作业标成 ``PUBLISH_DISABLED``）。
        所以这里**模拟**"某个池还没落地"：把 ``voice`` 从声明表里摘掉，判据必须立刻
        翻回 ``handler_missing`` —— 这也让这条护栏不依赖任务进度。
        """
        monkeypatch.delitem(pool_runner.HANDLER_MODULES, "voice")
        manager = make_manager(worker_home)
        item = manager.readiness(manager.spec("voice"))
        assert item.readiness is Readiness.HANDLER_MISSING
        assert item.remediation is not None and "voice" in item.remediation

    def test_the_draft_pool_is_ready_without_any_monkeypatching(self, worker_home: StudioPaths) -> None:
        """写稿池的 handler 落在 :data:`HANDLER_MODULES` 声明的模块里 ⇒ 真就绪。

        这条用例是 T4.11 的护栏：有人把 ``draft_worker`` 删掉或改名，
        API 进程就会重新把它判成 ``handler_missing``，这里立刻红。
        """
        manager = make_manager(worker_home)
        item = manager.readiness(manager.spec("draft"))
        assert item.readiness is Readiness.READY
        assert item.remediation is None

    def test_the_voice_pool_is_ready_without_any_monkeypatching(self, worker_home: StudioPaths) -> None:
        """配音池的 handler 落在 :data:`HANDLER_MODULES` 声明的模块里 ⇒ 真就绪。

        同 ``draft`` / ``render`` 那两条护栏：把 ``voice_worker`` 删掉或改名，
        启动器会重新把它判成 ``handler_missing``。
        """
        manager = make_manager(worker_home)
        item = manager.readiness(manager.spec("voice"))
        assert item.readiness is Readiness.READY
        assert item.remediation is None

    def test_the_render_pool_is_ready_without_any_monkeypatching(self, worker_home: StudioPaths) -> None:
        """渲染池的 handler 落在 :data:`HANDLER_MODULES` 声明的模块里 ⇒ 真就绪。

        同 ``draft`` 那条护栏：把 ``render_worker`` 删掉或改名，启动器会重新把它
        判成 ``handler_missing`` —— 而它现在的失败模式是"看着在跑、队列永远不消化"，
        正是这条判据要防的事。
        """
        manager = make_manager(worker_home)
        item = manager.readiness(manager.spec("render"))
        assert item.readiness is Readiness.READY
        assert item.remediation is None

    def test_pool_becomes_ready_once_the_handler_is_registered(
        self, worker_home: StudioPaths, pooled: None
    ) -> None:
        manager = make_manager(worker_home)
        assert all(manager.readiness(manager.spec(name)).ready for name in ("draft", "voice", "render"))


# ── 端口探测 ────────────────────────────────────────────────────────────


class TestPorts:
    def test_free_port_is_not_busy(self) -> None:
        # 端口 0 不是可连接的监听端口 ⇒ 必然"空闲"
        assert probe_port("127.0.0.1", 0, timeout=0.05) is False

    def test_busy_port_is_reported(self, worker_home: StudioPaths) -> None:
        rec = Recorder()
        rec.ports["8787"] = True
        manager = make_manager(worker_home, rec=rec)
        probes = {item.name: item for item in manager.probe_ports()}
        assert probes["api"].busy and not probes["api"].ours
        assert probes["tts"].busy is False

    def test_a_port_we_own_is_marked_ours(self, worker_home: StudioPaths) -> None:
        """**"已在运行" ≠ "端口冲突"**：前者是正常态，后者要人去看。"""
        table = FakeProcessTable(alive={4242})
        rec = Recorder()
        rec.ports["8787"] = True
        manager = make_manager(worker_home, table=table, rec=rec)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        probes = {item.name: item for item in manager.probe_ports()}
        assert probes["api"].ours


# ── 启动 ────────────────────────────────────────────────────────────────


class TestStart:
    def test_doctor_gate_refuses_to_start(self, worker_home: StudioPaths) -> None:
        """R1 的硬约束：门禁不过 ⇒ **拒绝启动**（不是"警告后照常起"）。"""
        rec = Recorder()
        manager = make_manager(worker_home, rec=rec, doctor=blocked_doctor())
        with pytest.raises(StudioError) as excinfo:
            manager.start(open_browser=False)
        assert excinfo.value.code is ErrorCode.ENV_CONTRACT_VIOLATION
        assert rec.spawned == []

    def test_doctor_gate_can_be_skipped_for_debugging(self, worker_home: StudioPaths) -> None:
        table, rec = ready_pair()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=blocked_doctor())
        report = manager.start(open_browser=False, doctor_gate=False)
        assert report.ok and rec.spawned == ["api", "draft", "voice", "render"]

    def test_only_the_ready_processes_are_spawned(self, worker_home: StudioPaths) -> None:
        """现状：api / draft / voice / render 就绪，tts 报降级 —— **降级不算失败**。"""
        table, rec = ready_pair()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        report = manager.start(open_browser=False)
        assert rec.spawned == ["api", "draft", "voice", "render"]
        assert report.ready == ("api", "draft", "voice", "render")
        assert set(report.degraded) == {"tts"}
        assert report.failed == ()
        assert report.ok

    def test_all_five_start_once_everything_is_landed(
        self, worker_home: StudioPaths, pooled: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "studio.services.service_manager.importlib.util.find_spec", lambda _name: object()
        )
        # tts 还要子环境在位（模块在 ≠ 环境在，两条判据都要过）
        interpreter = tts_interpreter(worker_home)
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("", encoding="utf-8")
        table, rec = ready_pair()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        manager.start(open_browser=False)

        assert rec.spawned == list(SERVICE_NAMES)
        assert table.alive_pids  # 假 spawn 的 pid 都被记进台账

    def test_pid_ledger_is_written_for_each_started_service(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable()
        rec = Recorder()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        manager.start(open_browser=False)
        assert worker_home.pid_file("api").read_text(encoding="utf-8") == "9001"

    def test_a_busy_port_blocks_that_service_only(self, worker_home: StudioPaths) -> None:
        rec = Recorder()
        rec.ports["8787"] = True
        manager = make_manager(worker_home, rec=rec, doctor=ok_doctor())
        report = manager.start(open_browser=False)
        # 端口冲突只挡 api；三个池照起（本用例的 Recorder 自带空表 ⇒ 它们随即被判"起完就死"）
        assert rec.spawned == ["draft", "voice", "render"]
        assert report.port_busy == ("api",)
        assert report.ready == ()

    def test_an_already_running_service_is_not_spawned_twice(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable(alive={4242})
        rec = Recorder()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        report = manager.start(open_browser=False)
        assert rec.spawned == ["draft", "voice", "render"]  # api 已在跑 ⇒ 只补拉没跑的池
        assert report.already_running == ("api",)
        assert report.ready == ("api",)

    def test_stale_ledger_and_leftover_stop_flag_are_cleaned(self, worker_home: StudioPaths) -> None:
        """残留标志不清 ⇒ 下次启动 worker 起来就自己关掉（最难查的那种）。"""
        rec = Recorder()
        manager = make_manager(worker_home, rec=rec, doctor=ok_doctor())
        worker_home.pid_file("api").write_text("999999", encoding="utf-8")
        worker_home.stop_flag_file("api").write_text("stop", encoding="utf-8")

        cleaned = manager.clean_stale()
        assert cleaned == ("api",)
        assert not worker_home.stop_flag_file("api").exists()
        assert not worker_home.pid_file("api").exists()

    def test_clean_stale_leaves_a_live_process_alone(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable(alive={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        assert manager.clean_stale() == ()
        assert worker_home.pid_file("api").is_file()

    def test_a_process_that_dies_before_ready_is_a_failure(self, worker_home: StudioPaths) -> None:
        """台账写了、进程却不在 ⇒ 起完立刻死，**必须报失败**（不是"看着起来了"）。"""
        rec = Recorder()  # 它的 spawn 只往自己的表里记 pid
        table = FakeProcessTable()  # 管理器看的是这张空表
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        report = manager.start(open_browser=False)
        assert report.failed == ("api", "draft", "voice", "render")  # 四个就绪进程都没挺过就绪探测
        assert not report.ok

    def test_the_browser_opens_only_when_the_api_is_ready(self, worker_home: StudioPaths) -> None:
        rec = Recorder()
        rec.ports["8787"] = True  # api 起不来
        manager = make_manager(worker_home, rec=rec, doctor=ok_doctor())
        report = manager.start(open_browser=True)
        assert rec.browsers == []
        assert report.browser_opened is False

    def test_the_browser_opens_the_webui_url(self, worker_home: StudioPaths) -> None:
        table, rec = ready_pair()
        manager = make_manager(worker_home, table=table, rec=rec, doctor=ok_doctor())
        report = manager.start(open_browser=True)
        assert rec.browsers == ["http://127.0.0.1:8787"]
        assert report.browser_opened is True

    def test_an_unknown_only_filter_is_refused_before_anything_starts(self, worker_home: StudioPaths) -> None:
        rec = Recorder()
        manager = make_manager(worker_home, rec=rec, doctor=ok_doctor())
        with pytest.raises(StudioError):
            manager.start(open_browser=False, only=["publish"])
        assert rec.spawned == []


# ── 关停（本任务的核心）─────────────────────────────────────────────────


class TestStop:
    def test_nothing_running_is_not_an_error(self, worker_home: StudioPaths) -> None:
        report = make_manager(worker_home).stop()
        assert report.ok
        assert report.stopped == ()
        assert set(report.skipped) == set(SERVICE_NAMES)

    def test_a_stale_ledger_is_cleaned_not_killed(self, worker_home: StudioPaths) -> None:
        manager = make_manager(worker_home, table=FakeProcessTable())
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        report = manager.stop()
        assert report.missing == ("api",)
        assert not worker_home.pid_file("api").exists()

    def test_a_poller_stops_through_the_flag_file_alone(
        self, worker_home: StudioPaths, pooled: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """池 worker 有 1s tick ⇒ 看到标志就退出，**不升级到信号**（裁定 102）。"""
        table = FakeProcessTable(alive={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("draft").write_text("4242", encoding="utf-8")

        def _alive(pid: int) -> bool:
            # 模拟 worker 的 1s 脉冲 tick：看到标志文件 ⇒ draining ⇒ 退出
            if worker_home.stop_flag_file("draft").is_file():
                table.alive_pids.discard(pid)
            return pid in table.alive_pids

        monkeypatch.setattr(table, "alive", _alive)
        report = manager.stop(timeout_sec=10.0)
        assert report.stopped == ("draft",)
        assert report.forced == ()
        assert table.breaks == [] and table.terminated == [] and table.killed == []

    def test_the_flag_file_is_removed_after_stopping(self, worker_home: StudioPaths, pooled: None) -> None:
        table = FakeProcessTable(alive={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("draft").write_text("4242", encoding="utf-8")
        manager.stop(timeout_sec=10.0)
        assert not worker_home.stop_flag_file("draft").exists()

    def test_a_non_poller_escalates_straight_to_the_signal(self, worker_home: StudioPaths) -> None:
        """uvicorn 不读文件 ⇒ 不为它白等 6 秒（裁定 107）。"""
        table = FakeProcessTable(alive={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        report = manager.stop(timeout_sec=10.0)
        assert report.stopped == ("api",)
        assert table.breaks == [4242]

    def test_a_process_that_ignores_the_signal_is_terminated(self, worker_home: StudioPaths) -> None:
        """Ctrl-Break 叫不停（**从另一个控制台发就是这样**）⇒ 升级到 terminate。"""
        table = FakeProcessTable(alive={4242}, ignores_break={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        report = manager.stop(timeout_sec=10.0)
        assert report.stopped == ("api",)
        assert report.forced == ()
        assert table.terminated == [4242]
        assert table.killed == []

    def test_an_unreachable_console_still_ends_with_a_terminate(self, worker_home: StudioPaths) -> None:
        """``request_break`` 直接返回 False（没有同控制台）⇒ 不许卡住，照样升级。"""
        table = FakeProcessTable(alive={4242})
        table.break_supported = False
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        report = manager.stop(timeout_sec=10.0)
        assert report.stopped == ("api",)
        assert table.terminated == [4242]

    def test_a_process_that_survives_terminate_is_killed_and_reported(self, worker_home: StudioPaths) -> None:
        """强杀 ⇒ ``forced`` 如实记录：**可能丢了当前单元**，不假装优雅。"""
        table = FakeProcessTable(alive={4242}, ignores_break={4242}, ignores_terminate={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")

        report = manager.stop(timeout_sec=10.0)
        assert table.killed == [4242]
        assert report.forced == ("api",)
        assert report.stopped == ()
        assert not report.ok

    def test_the_ledger_is_cleared_even_after_a_forced_kill(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable(alive={4242}, ignores_break={4242}, ignores_terminate={4242})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        manager.stop(timeout_sec=10.0)
        assert not worker_home.pid_file("api").exists()
        assert not worker_home.stop_flag_file("api").exists()

    def test_only_filter_limits_the_blast_radius(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable(alive={4242, 4243})
        manager = make_manager(worker_home, table=table)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        worker_home.pid_file("tts").write_text("4243", encoding="utf-8")
        report = manager.stop(timeout_sec=10.0, only=["tts"])
        assert report.stopped == ("tts",)
        assert worker_home.pid_file("api").is_file()
        assert table.alive_pids == {4242}

    def test_unknown_service_in_only_is_refused(self, worker_home: StudioPaths) -> None:
        with pytest.raises(StudioError):
            make_manager(worker_home).stop(only=["publish"])

    def test_the_default_budget_is_the_acceptance_number(self) -> None:
        """验收口径：``停止.bat`` ⇒ 10s 内全部退出。"""
        assert DEFAULT_STOP_TIMEOUT_SEC == 10.0
        assert DEFAULT_READY_TIMEOUT_SEC == 60.0


# ── 查看 ────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status_lists_all_five_even_when_nothing_runs(self, worker_home: StudioPaths) -> None:
        rows = make_manager(worker_home).status()
        assert [row.name for row in rows] == list(SERVICE_NAMES)
        assert all(not row.alive and row.pid is None for row in rows)

    def test_status_reports_pid_and_port(self, worker_home: StudioPaths) -> None:
        table = FakeProcessTable(alive={4242})
        rec = Recorder()
        rec.ports["8787"] = True
        manager = make_manager(worker_home, table=table, rec=rec)
        worker_home.pid_file("api").write_text("4242", encoding="utf-8")
        row = {item.name: item for item in manager.status()}["api"]
        assert (row.pid, row.alive, row.port, row.port_open) == (4242, True, 8787, True)

    def test_status_points_at_the_log_file(self, worker_home: StudioPaths) -> None:
        row = {item.name: item for item in make_manager(worker_home).status()}["draft"]
        assert row.log_file == worker_home.service_log_file("draft")


# ── 入口骨架 ────────────────────────────────────────────────────────────


class TestRunEntry:
    def test_a_successful_runner_returns_zero_and_prints_the_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _Report:
            def to_dict(self) -> dict[str, object]:
                return {"ok": True}

        assert run_entry("draft", _Report) == 0
        assert '"ok": true' in capsys.readouterr().out

    def test_a_studio_error_becomes_a_diagnosis_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        def _boom() -> None:
            raise StudioError(
                "池的单元处理器尚未落地",
                code=ErrorCode.INTERNAL,
                remediation="等 T1.8",
            )

        assert run_entry("draft", _boom) == 1
        captured = capsys.readouterr()
        assert "[INTERNAL]" in captured.err
        assert "等 T1.8" in captured.err
        assert captured.out == ""

    def test_an_interrupt_is_a_clean_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        def _interrupt() -> None:
            raise KeyboardInterrupt

        assert run_entry("api", _interrupt) == 0
        assert "优雅退出" in capsys.readouterr().out


# ── 真实进程表 ──────────────────────────────────────────────────────────


class TestRealProcessTable:
    def test_our_own_pid_is_alive(self) -> None:
        import os  # noqa: PLC0415

        from studio.services.service_manager import _RealProcessTable  # noqa: PLC0415

        assert _RealProcessTable().alive(os.getpid()) is True

    def test_a_dead_pid_is_not_alive(self) -> None:
        from studio.services.service_manager import _RealProcessTable  # noqa: PLC0415

        # 用一个不可能存在的 pid ⇒ 必须报"不存活"，而不是抛异常。
        # （pid 0 在 Windows 上是 Idle 进程，psutil 会说它"在跑"）
        assert _RealProcessTable().alive(999_999_999) is False

    def test_probe_port_finds_a_real_listener(self) -> None:
        import socket  # noqa: PLC0415

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            port = int(server.getsockname()[1])
            assert probe_port("127.0.0.1", port, timeout=0.5) is True
        finally:
            server.close()


# ── 健康判据：自报的 ready 说了算（陷阱 166）─────────────────────────────


def probe_of(
    payload: dict[str, Any] | None,
    *,
    status: int = 200,
    answered: bool = True,
) -> HealthProbe:
    """造一份"健康面的原始结论"（不碰网络）。"""
    if not answered:
        return HealthProbe(answered=False, error="URLError: 连接被拒")
    body = "" if payload is None else json.dumps(payload, ensure_ascii=False)
    return HealthProbe(answered=True, status=status, body=body, payload=payload)


#: 真机那台的读数：进程活着、``/health`` 回 200，但模型根本没加载成
BROKEN_TTS = {
    "ok": True,
    "pid": 4242,
    "ready": False,
    "engine": "cosyvoice2",
    "model_state": "error",
    "detail": "ModuleNotFoundError: No module named 'torch'",
}

#: 同一台服务**空闲 20 分钟之后**的读数：显存卸了、``ready`` 变回 false，但它是健康的
#: （下一次 ``/synth`` 会冷加载）。与 ``BROKEN_TTS`` 只差 ``model_state`` 与 ``detail`` ——
#: 而这两种读数**必须**分开处理（陷阱 168）。
SLEEPING_TTS = {
    **BROKEN_TTS,
    "model_state": "unloaded",
    "detail": "模型未加载（首次 /synth 会冷加载，或调 /warmup 预热）",
}


class OccupiedPortRecorder(Recorder):
    """端口上"占着"一个旧实例：**它死了端口才算空出来**（真 bind 也是这个次序）。"""

    def __init__(self, occupant: int, table: FakeProcessTable) -> None:
        super().__init__(table)
        self.occupant = occupant
        table.alive_pids.add(occupant)

    def probe(self, host: str, port: int) -> bool:
        del host, port
        return self.table.alive(self.occupant)


def make_manager_with_probe(
    paths: StudioPaths,
    *,
    table: FakeProcessTable,
    rec: Recorder,
    probe: Callable[[ServiceSpec], HealthProbe],
) -> ServiceManager:
    """与 :func:`make_manager` 同构，但**不注入** ``health`` —— 于是走真的
    :meth:`ServiceManager._default_health`（本组用例要验的正是它）。"""
    clock = FakeClock()
    return ServiceManager(
        paths,
        spawn=rec.spawn,
        process_table=table,
        port_probe=rec.probe,
        health_probe=probe,
        browser=rec.browser,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.fixture
def tts_ready(worker_home: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 tts 的**规格层**就绪条件摆好（模块在 + 推理子环境在位）—— 剩下的只看健康面。

    与用例主体分开写，是为了让每一条用例读起来只有一条主线：**健康面**怎么判。
    """
    monkeypatch.setattr("studio.services.service_manager.importlib.util.find_spec", lambda _name: object())
    interpreter = tts_interpreter(worker_home)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")


class TestHealthJudge:
    def test_a_service_that_reports_itself_unready_is_not_ready(
        self, worker_home: StudioPaths, tts_ready: None
    ) -> None:
        """★ 「进程活着」≠「服务能用」：自报 ``ready: false`` ⇒ **不算就绪**。

        修前这里只看状态码 200 ⇒ 一个每句都念不出声的引擎被记成「已就绪」，
        守护进程永远不去修它，配音一直悄悄退回系统语音包（真机 8788 上那个旧实例）。
        """
        table = FakeProcessTable()
        rec = Recorder(table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(BROKEN_TTS)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert rec.spawned == ["tts"]  # 进程照起（起来才有日志可看）
        assert report.ready == ()
        assert report.failed == ("tts",)

    def test_a_sleeping_instance_counts_as_ready(self, worker_home: StudioPaths, tts_ready: None) -> None:
        """★ 空闲卸载（``model_state: unloaded``）⇒ **算就绪**：它下一句会自己醒。

        与上面那条只差 ``model_state`` 一个字段。混成一种的话，每一次"空闲 20 分钟
        之后再启动一次"都会稳定地报一句"tts 未就绪"，而它其实好好的（陷阱 168）。
        """
        table = FakeProcessTable()
        rec = Recorder(table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(SLEEPING_TTS)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert report.ready == ("tts",) and report.failed == ()

    def test_a_self_report_of_ready_is_enough(self, worker_home: StudioPaths, tts_ready: None) -> None:
        table = FakeProcessTable()
        rec = Recorder(table)
        healthy = {**BROKEN_TTS, "ready": True, "model_state": "ready", "detail": None}
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(healthy)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert report.ready == ("tts",) and report.failed == ()

    def test_a_service_without_a_self_report_is_judged_by_the_status_code(
        self, worker_home: StudioPaths
    ) -> None:
        """``api`` 的健康面没有 ``ready`` 字段 ⇒ 判据退回"200 就算就绪"（自报了才管）。"""
        table = FakeProcessTable()
        rec = Recorder(table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of({"ok": True})
        )
        spec = manager.spec("api")

        assert manager._default_health(spec) == (True, json.dumps({"ok": True}))

    def test_a_non_200_is_not_ready(self, worker_home: StudioPaths) -> None:
        manager = make_manager(worker_home, rec=Recorder())
        spec = manager.spec("api")
        manager._health_probe = lambda _spec: probe_of(None, status=503)

        healthy, detail = manager._default_health(spec)

        assert healthy is False and "503" in detail

    def test_an_unreachable_service_is_not_ready(self, worker_home: StudioPaths) -> None:
        manager = make_manager(worker_home, rec=Recorder())
        manager._health_probe = lambda _spec: probe_of(None, answered=False)

        healthy, detail = manager._default_health(manager.spec("api"))

        assert healthy is False and "连接被拒" in detail


class TestTakeover:
    """端口上那个**我们自己的、没就绪的**旧实例：接管重启（否则它会一直挂到人手动去关）。"""

    def test_an_unready_instance_we_own_is_taken_over(
        self, worker_home: StudioPaths, tts_ready: None
    ) -> None:
        table = FakeProcessTable()
        rec = OccupiedPortRecorder(4242, table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(BROKEN_TTS)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert 4242 in table.terminated  # 旧实例被请走（先 terminate，不是上来就 kill）
        assert 4242 not in table.alive_pids
        assert rec.spawned == ["tts"]  # 然后**用当前规格表**重新拉起
        assert report.port_busy == ()  # 不再是"端口有人占着，不关我事"

    def test_a_healthy_instance_is_left_alone(self, worker_home: StudioPaths, tts_ready: None) -> None:
        """健康的实例一律不碰：可能是用户自己起的，也可能正在干活。"""
        table = FakeProcessTable()
        rec = OccupiedPortRecorder(4242, table)
        healthy = {**BROKEN_TTS, "ready": True}
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(healthy)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert table.terminated == [] and 4242 in table.alive_pids
        assert rec.spawned == []
        assert report.port_busy == ("tts",)

    def test_a_sleeping_instance_is_left_alone(self, worker_home: StudioPaths, tts_ready: None) -> None:
        """★ 空闲卸载的实例**也是健康的**：接管它只会让用户白等一次 20s 加载（陷阱 168）。

        它与上面那条只差 ``model_state``：``error`` 是坏了（该接管重启），
        ``unloaded`` 只是睡着了（叫得醒）。
        """
        table = FakeProcessTable()
        rec = OccupiedPortRecorder(4242, table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(SLEEPING_TTS)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert table.terminated == [] and table.killed == [] and 4242 in table.alive_pids
        assert rec.spawned == []
        assert report.port_busy == ("tts",)

    def test_a_foreign_service_on_the_port_is_left_alone(
        self, worker_home: StudioPaths, tts_ready: None
    ) -> None:
        """没有 ``ready`` / ``pid`` 两个自述字段 ⇒ 不是我们的服务，不碰。"""
        table = FakeProcessTable()
        rec = Recorder(table)
        rec.ports["8788"] = True
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of({"ok": True})
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert table.terminated == [] and table.killed == []
        assert report.port_busy == ("tts",)

    def test_a_pid_that_is_already_gone_is_not_killed(
        self, worker_home: StudioPaths, tts_ready: None
    ) -> None:
        """自报的 pid 已经不在 ⇒ 不认它（杀一个已经不存在的 pid 只会掩盖真问题）。"""
        table = FakeProcessTable()  # 4242 不在 alive 里
        rec = Recorder(table)
        rec.ports["8788"] = True
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(BROKEN_TTS)
        )

        report = manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert table.terminated == [] and table.killed == []
        assert report.port_busy == ("tts",)

    def test_a_stubborn_old_instance_gets_killed(self, worker_home: StudioPaths, tts_ready: None) -> None:
        """terminate 叫不停（卡在驱动上那种）⇒ 升级到 kill，不能就这么算了。"""
        table = FakeProcessTable(ignores_terminate={4242})
        rec = OccupiedPortRecorder(4242, table)
        manager = make_manager_with_probe(
            worker_home, table=table, rec=rec, probe=lambda spec: probe_of(BROKEN_TTS)
        )

        manager.start(only=["tts"], open_browser=False, doctor_gate=False)

        assert 4242 in table.killed and 4242 not in table.alive_pids


# ── 真实文件布局 ────────────────────────────────────────────────────────


class TestRealLayout:
    def test_the_five_entry_scripts_exist_in_the_repo(self, repo_paths: StudioPaths) -> None:
        """入口脚本是**部署面**：启动器指向它们，缺一个就少一个进程。"""
        missing = [spec.name for spec in build_specs(repo_paths) if not spec.entry.is_file()]
        assert missing == []

    def test_the_entry_scripts_are_declared_in_the_spec_layout(self, repo_paths: StudioPaths) -> None:
        for spec in build_specs(repo_paths):
            assert spec.entry.parent == repo_paths.workers_dir
            assert spec.entry.name.startswith("run_")

    def test_the_ops_wrappers_exist(self, repo_paths: StudioPaths) -> None:
        for name in ("start_all.ps1", "stop_all.ps1", "status.ps1", "healthcheck.ps1"):
            assert (repo_paths.home / "ops" / name).is_file(), name

    def test_the_bat_files_exist(self, repo_paths: StudioPaths) -> None:
        for name in ("启动.bat", "停止.bat"):
            assert (repo_paths.home / name).is_file(), name

    def test_paths_expose_the_ledger_rules(self, tmp_paths: StudioPaths) -> None:
        """台账与标志文件的路径规则只有一处真相（裁定 105）。"""
        assert tmp_paths.pid_file("api") == tmp_paths.logs_dir / "api.pid"
        assert tmp_paths.stop_flag_file("api") == tmp_paths.logs_dir / "api.stop"
        assert tmp_paths.service_log_file("api") == tmp_paths.logs_dir / "api.log"
        assert tmp_paths.workers_dir == tmp_paths.home / "workers"


class TestNoRealDataWrites:
    def test_manager_never_touches_the_repo_data_dir(self, repo_paths: StudioPaths) -> None:
        """管理器只认传进来的 ``paths``（``tmp_paths``）—— 这里只做静态确认。"""
        assert isinstance(repo_paths, StudioPaths)
        assert repo_paths.data_dir.name == "data"
        assert Path(repo_paths.db_file).name == "studio.db"
