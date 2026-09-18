"""制片台进程编排（T1.12 · 原文附2 / §7.4 / ADR-002）。

六个进程
--------
| 进程 | 端口 | 就绪判据 | 未就绪时 |
| --- | --- | --- | --- |
| ``api`` | 8787 | ``GET /api/v1/health`` 回 200 且 ``ok=true`` | **硬失败**（WebUI 是唯一入口） |
| ``tts`` | 8788 | ``GET /health`` 回 200 | **降级**：其余照常启动（§1.7） |
| ``draft`` / ``voice`` / ``render`` / ``publish`` | — | 池 handler 已注册 + 进程活着 | **降级**：不拉起 |

``publish`` 是最后一个进来的（T5.5 面板落地之后）：在那之前它是唯一「面板能看、
进程起不来」的池 —— 发布作业投进去没人消化。出厂 ``publish.enabled=false`` 时它
照常活着，只是把作业如实标成 ``PUBLISH_DISABLED``（进死信），这是**要的**行为：
开关一开就立刻能发，不必先重启服务。

为什么核心是 Python 而不是 PowerShell（裁定 104）
--------------------------------------------------
关停时序（标志 → 信号 → 强杀 + 超时 + 升级）是本任务**最需要被验证**的东西：
"10s 内全部退出且不丢进度"如果只写在 ``.ps1`` 里，既测不了也改不动。
``ops/*.ps1`` 于是退化成薄壳（dot-source env + 调本模块），T4.12 的"总览台启停"
直接 import 同一份实现 —— 不必再写第三遍，也不会两份实现各自漂移。

关停为什么靠标志文件而不是信号（裁定 102）
------------------------------------------
``GenerateConsoleCtrlEvent`` 只能作用于**同控制台**的进程组。``停止.bat`` 是
另起一个控制台跑的进程 ⇒ 从那里发的 Ctrl-Break 会**静默失效**（API 返回 TRUE
但目标收不到），最后只能 ``terminate()`` 硬杀 ⇒ 丢进度。所以主通道是
``data/logs/<name>.stop`` 标志文件（worker 在 1s 脉冲 tick 里看到就 draining），
与"谁在哪个控制台发命令"无关；信号与强杀只作为**升级手段**，且强杀会被如实
记进 :attr:`StopReport.forced` —— 不假装优雅。

未就绪的进程不拉起（裁定 103）
-------------------------------
``pools.runner.handler_for()`` 故意在 handler 未注册时报错（"不静默起一个空转
worker"）。管理器的职责是**先问再拉**：报 ``degraded`` 并照常启动其余进程，
而不是为了让验收数字好看而放行一个永远不消化队列的进程。

进程独立性（§7.4 / ADR-002）
----------------------------
子进程用 ``CREATE_NEW_PROCESS_GROUP`` 拉起：新进程组默认**屏蔽 Ctrl+C**，
所以启动窗口被关掉、启动器退出、用户在启动窗口按 Ctrl+C 都不会带走 worker。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from studio.core.config import load_config, load_tts_config
from studio.core.doctor import Doctor, DoctorReport
from studio.core.entry import run_entry
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.pools.runner import HANDLER_MODULES, HANDLER_REMEDIATION, STOP_FLAG_ENV, handler_for
from studio.tts.service_engine import wakeable_health

__all__ = [
    "DEFAULT_READY_TIMEOUT_SEC",
    "DEFAULT_STOP_TIMEOUT_SEC",
    "SERVICE_NAMES",
    "HealthProbe",
    "PortProbe",
    "Readiness",
    "ServiceKind",
    "ServiceManager",
    "ServiceReadiness",
    "ServiceSpec",
    "ServiceStatus",
    "StartReport",
    "StopReport",
    "build_specs",
    "default_manager",
    "probe_health",
    "probe_port",
    "run_entry",
]

logger = get_logger("studio.services.service_manager")

#: 六个进程（原文附2：任务队列 + CosyVoice 服务 + Web 服务 ⇒ 一期细化为 5 个；
#: T5.5 发布面板落地后把 ``publish`` 补成第 6 个 —— 在那之前它是唯一「面板能看、
#: 进程起不来」的池）。
SERVICE_NAMES: Final[tuple[str, ...]] = ("api", "tts", "draft", "voice", "render", "publish")

#: 启动后等就绪的总预算（验收：60s 内 5 进程 ready）
DEFAULT_READY_TIMEOUT_SEC: Final[float] = 60.0

#: 关停宽限期（验收：10s 内全部退出）
DEFAULT_STOP_TIMEOUT_SEC: Final[float] = 10.0

#: 池进程的"活着"观察窗：起完立刻死掉的 worker 不算 started
POOL_GRACE_SEC: Final[float] = 3.0

#: 接管一个未就绪的旧实例时，给它几次优雅退出的机会（秒）
TAKEOVER_GRACE_SEC: Final[float] = 3.0

#: 接管后等端口空出来的上限（秒）
TAKEOVER_FREE_SEC: Final[float] = 5.0


class ServiceKind(StrEnum):
    """服务的就绪判据种类。"""

    HTTP = "http"  # 端口 + /health
    POOL = "pool"  # handler 已注册 + 进程活着


class Readiness(StrEnum):
    """服务能不能被拉起（**先问再拉**）。"""

    READY = "ready"
    ENTRY_MISSING = "entry_missing"  # workers/run_*.py 不存在
    HANDLER_MISSING = "handler_missing"  # 池 handler 未注册
    SERVER_MISSING = "server_missing"  # tts 常驻服务模块未落地
    ENV_MISSING = "env_missing"  # tts 推理子环境（tts/.venv）不存在


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """一个待编排的服务（**不可变**：命令行是契约的一部分）。"""

    name: str
    kind: ServiceKind
    entry: Path
    argv: tuple[str, ...]
    port: int | None = None
    health_path: str | None = None
    pool: str | None = None
    required: bool = False
    #: 这个进程会不会**自己轮询**关停标志文件？
    #:
    #: 池 worker 有 1s 脉冲 tick（心跳/续租那条），看一眼标志是顺手的事 ⇒ ``True``；
    #: HTTP 面的进程（uvicorn）没有 tick ⇒ ``False``，对它直接走信号/强杀，
    #: 免得白等一个"永远不会发生"的优雅退出（T1.12 裁定 107）。
    polls_stop_flag: bool = False
    #: 这个服务**必须**用哪个解释器起（``None`` ⇒ 用当前解释器）。
    #:
    #: ``tts`` 是唯一一个填它的：torch 2.4.0+cu121 与 cosyvoice 只装得进
    #: Python 3.11 的 ``tts/.venv``，而启动器自己是主 venv（3.12）的解释器。
    #: 用错解释器的现象是「进程起来了，但每句都报 no module named torch」——
    #: 看上去像代码坏了，其实是解释器错了。
    python: Path | None = None
    #: 拉起时**前置**到子进程环境变量的键值对。
    #:
    #: 用来把仓库 ``src/`` 塞进 ``PYTHONPATH``：``tts`` 子环境里没装 ``studio``
    #: 这个包（``tts/pyproject.toml`` 写着 ``package = false``），不前置就 import 不到。
    env_prepend: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "entry": str(self.entry),
            "argv": list(self.argv),
            "port": self.port,
            "health_path": self.health_path,
            "pool": self.pool,
            "required": self.required,
            "polls_stop_flag": self.polls_stop_flag,
            "python": str(self.python) if self.python is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ServiceReadiness:
    """就绪判定结论（含**可直接展示的**修复提示）。"""

    name: str
    readiness: Readiness
    detail: str
    remediation: str | None = None

    @property
    def ready(self) -> bool:
        return self.readiness is Readiness.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "readiness": str(self.readiness),
            "ready": self.ready,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class PortProbe:
    """端口占用探测结论。"""

    name: str
    port: int
    busy: bool
    #: 占用者是不是**我们自己的**进程（PID 台账里的那个）—— "已在运行" ≠ "端口冲突"
    ours: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "port": self.port, "busy": self.busy, "ours": self.ours}


@dataclass(frozen=True, slots=True)
class StartReport:
    """一次启动的完整结论。"""

    started: tuple[str, ...] = ()
    ready: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    already_running: tuple[str, ...] = ()
    port_busy: tuple[str, ...] = ()
    readiness: tuple[ServiceReadiness, ...] = ()
    url: str = ""
    browser_opened: bool = False
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        """**硬失败**才算不 ok：降级不算（P4 无人值守优先，§1.7）。"""
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started": list(self.started),
            "ready": list(self.ready),
            "degraded": list(self.degraded),
            "failed": list(self.failed),
            "already_running": list(self.already_running),
            "port_busy": list(self.port_busy),
            "readiness": [item.to_dict() for item in self.readiness],
            "url": self.url,
            "browser_opened": self.browser_opened,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class StopReport:
    """一次关停的完整结论。"""

    stopped: tuple[str, ...] = ()  # 优雅退出（标志 / 信号）
    forced: tuple[str, ...] = ()  # 强杀 ⇒ **可能丢当前单元**，如实记录
    missing: tuple[str, ...] = ()  # 台账有、进程已不在（陈旧台账）
    skipped: tuple[str, ...] = ()  # 没有台账
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.forced

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stopped": list(self.stopped),
            "forced": list(self.forced),
            "missing": list(self.missing),
            "skipped": list(self.skipped),
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """一个服务的当前状态。"""

    name: str
    pid: int | None
    alive: bool
    port: int | None
    port_open: bool
    log_file: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "alive": self.alive,
            "port": self.port,
            "port_open": self.port_open,
            "log_file": str(self.log_file),
        }


# ── 注入点（生产用真实现，测试注入假件）────────────────────────────────


class ProcessTable(Protocol):
    """进程表操作（测试注入假实现 ⇒ 关停时序**可测**）。"""

    def alive(self, pid: int) -> bool: ...

    def wait(self, pid: int, timeout: float) -> bool:
        """等进程退出；返回是否已退出。"""

    def request_break(self, pid: int) -> bool:
        """尝试发 Ctrl-Break（同控制台才有效）；返回"这条通道可用吗"。"""

    def terminate(self, pid: int) -> None: ...

    def kill(self, pid: int) -> None: ...


class _RealProcessTable:
    """真进程表（psutil 判活；Windows 用 ``GenerateConsoleCtrlEvent``）。"""

    def alive(self, pid: int) -> bool:
        import psutil  # noqa: PLC0415

        try:
            process = psutil.Process(pid)
        except psutil.Error:
            return False
        try:
            return bool(process.is_running()) and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def wait(self, pid: int, timeout: float) -> bool:
        import psutil  # noqa: PLC0415

        try:
            psutil.Process(pid).wait(timeout=max(0.0, timeout))
        except psutil.TimeoutExpired:
            return False
        except psutil.Error:
            return True  # 进程已不在 ⇒ 视为已退出
        return True

    def request_break(self, pid: int) -> bool:
        if os.name != "nt":
            return False
        try:
            import ctypes  # noqa: PLC0415

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (OSError, AttributeError):
            return False
        # CTRL_BREAK_EVENT = 1；新进程组默认屏蔽 CTRL_C 但接受 CTRL_BREAK
        return bool(kernel32.GenerateConsoleCtrlEvent(1, int(pid)))

    def terminate(self, pid: int) -> None:
        import psutil  # noqa: PLC0415

        try:
            psutil.Process(pid).terminate()
        except psutil.Error:
            return

    def kill(self, pid: int) -> None:
        import psutil  # noqa: PLC0415

        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            return


def probe_port(host: str, port: int, *, timeout: float = 0.25) -> bool:
    """TCP 连得上就算占用（不做协议握手 —— 启动前探测要快且无副作用）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _json_object(body: str) -> dict[str, Any] | None:
    """响应体是 JSON 对象 ⇒ 解析结果；不是 ⇒ ``None``（对面不是 JSON 不该算错误）。"""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _self_report_reason(payload: Mapping[str, Any]) -> str:
    """服务自报「为什么没就绪」：``detail`` 优先，其次模型状态与设备。

    这一句话要一路带到 ``data/logs/*.log`` 与总览台上：真机上它是
    ``ModuleNotFoundError: No module named torch`` —— 没有它，人就只能看到
    「端口占用」这种把原因藏起来的话。
    """
    for key in ("detail", "model_state", "device"):
        value = payload.get(key)
        if value:
            return str(value)
    return "ready=false"


def _self_report(probe: HealthProbe) -> tuple[bool, str] | None:
    """服务**自述**的那部分判据；没自述 ⇒ ``None``（交给状态码那一支）。

    单拎出来是因为 :meth:`ServiceManager._default_health` 已经把"没答上 / 不是 200"
    两条先排掉了，剩下的分岔（``ok`` / ``ready`` / 可唤醒）挤在一起会让那个函数
    超出分支上限 —— 而它真正要说的话只有一句：**自报了才管**。
    """
    payload = probe.payload
    if payload is None:
        if '"ok": false' in probe.body.replace(" ", "").lower():
            return False, "health 报 ok=false"
        return None
    if payload.get("ok") is False:
        return False, "health 报 ok=false"
    if payload.get("ready") is False:
        if wakeable_health(payload):
            return True, f"自报未就绪但可唤醒：{_self_report_reason(payload)}"
        return False, f"自报未就绪：{_self_report_reason(payload)}"
    return None


@dataclass(frozen=True, slots=True)
class HealthProbe:
    """一次 HTTP 健康面探测的**原始**结论（判据由调用方自己定）。"""

    #: 对面答话了吗（连不上 / 不是 HTTP ⇒ ``False``）
    answered: bool
    status: int | None = None
    body: str = ""
    payload: dict[str, Any] | None = None
    error: str | None = None


def probe_health(host: str, port: int | None, path: str | None, *, timeout: float = 1.5) -> HealthProbe:
    """问一次 HTTP 健康面（**唯一一处 urlopen**；"算不算就绪"由调用方判）。

    两个"没答上"要分开记：``HTTPError`` 是**答了**（只是 4xx/5xx，比如服务还在
    启动中），``URLError``/``OSError`` 才是**连不上**。前者重试有意义，后者要先
    看进程在不在 —— 混成一种，排障时第一步就分岔。
    """
    if port is None or path is None:
        return HealthProbe(answered=True, status=None, body="no_http")
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # 本机回环
            body = response.read(4096).decode("utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也算"答了"，只是没就绪
        return HealthProbe(answered=True, status=int(exc.code), body="")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return HealthProbe(answered=False, error=f"{type(exc).__name__}: {exc}")
    return HealthProbe(answered=True, status=status, body=body, payload=_json_object(body))


# ── 规格表 ──────────────────────────────────────────────────────────────


def tts_python_for(paths: StudioPaths) -> Path | None:
    """推理子环境的解释器（``config/tts.yaml: python``；缺了返回 ``None``）。

    ``None`` 不是「没有解释器」，而是「**推理环境没装好**」—— 调用方据此把
    ``tts`` 标成 :attr:`Readiness.ENV_MISSING` 并给出「去建 venv」的提示，
    而不是拉起来一个每句都报 ``no module named torch`` 的进程。

    显式配置了 ``python`` 但那个文件不在 ⇒ **不退回**默认位置：配置说了用哪个，
    就只用那个。静默换一个解释器的后果是「环境看起来对，跑起来全不对」。
    """
    configured: Path | None = None
    try:
        configured = load_tts_config(paths).python
    except StudioError:  # 配置读不出来 ⇒ 退回默认位置，让 doctor 去报那一件事
        configured = None
    if configured is not None:
        candidate = configured if configured.is_absolute() else paths.home / configured
        return candidate if candidate.is_file() else None
    default = paths.home / "tts" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return default if default.is_file() else None


def build_specs(paths: StudioPaths, *, python: str | None = None) -> tuple[ServiceSpec, ...]:
    """构造六进程规格表（命令行 = ``<python> workers/run_<name>.py``）。

    用**脚本路径**而不是 ``python -m studio.cli``：worker 进程的日志第一行就能
    看出"是哪个入口"，排障时少一次推理。
    """
    executable = python or sys.executable
    workers = paths.workers_dir
    tts_python = tts_python_for(paths) if python is None else None

    def entry(name: str) -> tuple[Path, tuple[str, ...]]:
        script = workers / f"run_{name}.py"
        return script, (executable, str(script))

    api_entry, api_argv = entry("api")
    tts_entry, tts_argv = entry("tts")
    draft_entry, draft_argv = entry("draft")
    voice_entry, voice_argv = entry("voice")
    render_entry, render_argv = entry("render")
    publish_entry, publish_argv = entry("publish")
    if tts_python is not None:
        tts_argv = (str(tts_python), str(tts_entry))

    return (
        ServiceSpec(
            name="api",
            kind=ServiceKind.HTTP,
            entry=api_entry,
            argv=api_argv,
            port=8787,
            health_path="/api/v1/health",
            required=True,
        ),
        ServiceSpec(
            name="tts",
            kind=ServiceKind.HTTP,
            entry=tts_entry,
            argv=tts_argv,
            port=8788,
            health_path="/health",
            python=tts_python,
            env_prepend=(("PYTHONPATH", str(paths.home / "src")),),
        ),
        ServiceSpec(
            name="draft",
            kind=ServiceKind.POOL,
            entry=draft_entry,
            argv=draft_argv,
            pool="draft",
            polls_stop_flag=True,
        ),
        ServiceSpec(
            name="voice",
            kind=ServiceKind.POOL,
            entry=voice_entry,
            argv=voice_argv,
            pool="voice",
            polls_stop_flag=True,
        ),
        ServiceSpec(
            name="render",
            kind=ServiceKind.POOL,
            entry=render_entry,
            argv=render_argv,
            pool="render",
            polls_stop_flag=True,
        ),
        ServiceSpec(
            name="publish",
            kind=ServiceKind.POOL,
            entry=publish_entry,
            argv=publish_argv,
            pool="publish",
            polls_stop_flag=True,
        ),
    )


# ── 管理器 ──────────────────────────────────────────────────────────────


class ServiceManager:
    """六个进程的启动 / 关停 / 查看（§7.4 · 原文附2 + T5.5 补 publish）。

    :param paths: 路径契约
    :param env: 传给子进程的环境变量快照（默认 ``os.environ``）
    :param spawn: 拉起进程的注入点（默认 ``subprocess.Popen``）
    :param process_table: 进程表（默认 psutil 实现）
    :param port_probe: 端口探测（默认 TCP connect）
    :param health: HTTP 健康检查（默认 urllib）
    :param health_probe: 健康面的**原始**探测（默认 urllib；给测试注入假件用）
    :param browser: 打开浏览器（默认 ``webbrowser.open``）
    :param doctor_factory: doctor 门禁的构造器（测试注入假件，不跑真自检）
    """

    def __init__(
        self,
        paths: StudioPaths,
        *,
        env: Mapping[str, str] | None = None,
        python: str | None = None,
        spawn: Callable[[ServiceSpec], int] | None = None,
        process_table: ProcessTable | None = None,
        port_probe: Callable[[str, int], bool] | None = None,
        health: Callable[[ServiceSpec], tuple[bool, str]] | None = None,
        health_probe: Callable[[ServiceSpec], HealthProbe] | None = None,
        browser: Callable[[str], bool] | None = None,
        doctor_factory: Callable[[], DoctorReport] | None = None,
        host: str = "127.0.0.1",
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._paths = paths
        self._env: dict[str, str] = dict(env if env is not None else os.environ)
        self._host = host
        self._specs = build_specs(paths, python=python)
        self._by_name = {spec.name: spec for spec in self._specs}
        self._spawn = spawn or self._default_spawn
        self._processes: ProcessTable = process_table or _RealProcessTable()
        self._probe: Callable[[str, int], bool] = port_probe or probe_port
        self._health = health or self._default_health
        self._health_probe = health_probe or (lambda spec: probe_health(host, spec.port, spec.health_path))
        self._browser = browser or webbrowser.open
        self._doctor_factory = doctor_factory
        self._sleep = sleep
        self._monotonic = monotonic

    # ── 只读 ────────────────────────────────────────────────────────────
    @property
    def specs(self) -> tuple[ServiceSpec, ...]:
        return self._specs

    def spec(self, name: str) -> ServiceSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise StudioError(
                f"未知服务：{name}",
                code=ErrorCode.CONFIG_INVALID,
                context={"service": name, "valid": list(SERVICE_NAMES)},
                remediation="服务名只能是 api / tts / draft / voice / render",
            ) from exc

    @property
    def url(self) -> str:
        api = self._by_name["api"]
        return f"http://{self._host}:{api.port}"

    # ── 就绪判定 ────────────────────────────────────────────────────────
    def readiness(self, spec: ServiceSpec) -> ServiceReadiness:
        """能不能拉起这个服务（**先问再拉**，裁定 103）。"""
        if not spec.entry.is_file():
            return ServiceReadiness(
                name=spec.name,
                readiness=Readiness.ENTRY_MISSING,
                detail=f"进程入口不存在：{spec.entry}",
                remediation="确认 workers/ 下的入口脚本在位（§02.2）",
            )
        if spec.kind is ServiceKind.POOL:
            assert spec.pool is not None  # 规格表构造时就保证了
            if not self._handler_available(spec.pool):
                return ServiceReadiness(
                    name=spec.name,
                    readiness=Readiness.HANDLER_MISSING,
                    detail=f"{spec.pool} 池的单元处理器尚未落地",
                    remediation=HANDLER_REMEDIATION,
                )
        if spec.name == "tts" and importlib.util.find_spec("studio.tts.server") is None:
            return ServiceReadiness(
                name=spec.name,
                readiness=Readiness.SERVER_MISSING,
                detail="常驻推理服务模块尚未落地（studio.tts.server）",
                remediation="T2.2 落地后自动就绪；在此之前其余进程照常启动（降级模式 §1.7）",
            )
        if spec.name == "tts" and spec.python is None:
            # 模块在、环境不在：拉起来只会得到一个「每句都报 no module named torch」的
            # 进程 —— 那比「没起来」更难查（进程是活的，探活还回 200）。
            return ServiceReadiness(
                name=spec.name,
                readiness=Readiness.ENV_MISSING,
                detail="推理子环境不存在（tts/.venv）",
                remediation=(
                    "按 docs/runbook/tts_models.md 第二节建 tts/.venv 并装依赖；"
                    "config/tts.yaml 的 python 指错了也会走到这里"
                ),
            )
        return ServiceReadiness(name=spec.name, readiness=Readiness.READY, detail="就绪")

    def readiness_all(self) -> tuple[ServiceReadiness, ...]:
        return tuple(self.readiness(spec) for spec in self._specs)

    @staticmethod
    def _handler_available(pool: str) -> bool:
        """这个池的单元处理器**能不能跑起来**（T4.11）。

        两级判据，顺序不能反：

        1. 本进程注册过（``HANDLERS``）⇒ 立刻能跑（内嵌/测试场景）；
        2. 否则看 :data:`~studio.pools.runner.HANDLER_MODULES` 里声明的模块**在不在**
           —— 启动器要拉的是**另一个进程**，那边会自己 ``register_handler``。
           用 ``find_spec`` 而不是 import：判据要便宜且无副作用，与 ``tts`` 那条
           ``server_missing`` 判据同一手法。
        """
        try:
            handler_for(pool)
        except StudioError:
            module = HANDLER_MODULES.get(pool)
            return module is not None and importlib.util.find_spec(module) is not None
        return True

    # ── 端口 ────────────────────────────────────────────────────────────
    def probe_ports(self) -> tuple[PortProbe, ...]:
        """启动前的端口占用探测（端口冲突要**说清**，不能静默失败）。"""
        found: list[PortProbe] = []
        for spec in self._specs:
            if spec.port is None:
                continue
            busy = self._probe(self._host, spec.port)
            ours = busy and self._running_pid(spec.name) is not None
            found.append(PortProbe(name=spec.name, port=spec.port, busy=busy, ours=ours))
        return tuple(found)

    # ── 台账 ────────────────────────────────────────────────────────────
    def _read_pid(self, name: str) -> int | None:
        path = self._paths.pid_file(name)
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw.isdigit():
            return None
        return int(raw)

    def _running_pid(self, name: str) -> int | None:
        pid = self._read_pid(name)
        if pid is None or not self._processes.alive(pid):
            return None
        return pid

    def _write_pid(self, name: str, pid: int) -> None:
        path = self._paths.pid_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}", encoding="utf-8")

    def _clear(self, name: str) -> None:
        for path in (self._paths.pid_file(name), self._paths.stop_flag_file(name)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("service.ledger_cleanup_failed", service=name, path=str(path), error=str(exc))

    def clean_stale(self) -> tuple[str, ...]:
        """清理陈旧台账与**上一轮残留的关停标志**。

        残留标志必须清：不清的话下一次启动会立刻看到它 ⇒ worker 起来就自己关掉，
        现象是"启动了但什么都没干"（最难查的那种）。
        """
        cleaned: list[str] = []
        for spec in self._specs:
            pid_file = self._paths.pid_file(spec.name)
            flag = self._paths.stop_flag_file(spec.name)
            had = pid_file.exists() or flag.exists()
            if pid_file.exists() and self._running_pid(spec.name) is not None:
                continue  # 真在跑 ⇒ 别动它
            self._clear(spec.name)
            if had:
                cleaned.append(spec.name)
        return tuple(cleaned)

    # ── 拉起 ────────────────────────────────────────────────────────────
    def _default_spawn(self, spec: ServiceSpec) -> int:
        """拉起子进程并把 stdout/stderr 追加到 ``data/logs/<name>.log``（裁定 106）。"""
        log_path = self._paths.service_log_file(spec.name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(self._env)
        for key, value in spec.env_prepend:
            # 前置而不是覆盖：用户自己设的 PYTHONPATH 里可能有别的东西，
            # 直接盖掉等于悄悄改了他给别的工具准备的环境。
            existing = env.get(key)
            env[key] = f"{value}{os.pathsep}{existing}" if existing else value
        env[STOP_FLAG_ENV] = str(self._paths.stop_flag_file(spec.name))
        env["PYTHONUNBUFFERED"] = "1"
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        with log_path.open("ab") as sink:
            process = subprocess.Popen(  # argv 是自建常量，不经 shell
                list(spec.argv),
                cwd=str(self._paths.home),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        return int(process.pid)

    def _default_health(self, spec: ServiceSpec) -> tuple[bool, str]:
        """HTTP 就绪探针：200 + ``ok`` 不为 False + **服务自报的 ``ready``**。

        为什么要看自报的 ``ready``（陷阱 166）
        --------------------------------------
        「进程活着」与「服务能用」是**两件事**：tts 起得来、``/health`` 也回 200，
        可模型没加载成时它自报 ``ready: false``（真机那台是
        ``ModuleNotFoundError: No module named 'torch'``）。只看状态码，一个
        **每句都念不出声**的引擎就会被记成「已就绪」：守护进程永远不去修它，
        配音一直悄悄退回系统语音包，而每一处日志都写着成功。

        没有 ``ready`` 字段的服务（``api`` 那条）不受影响 —— 判据是「自报了才管」。

        两种"没就绪"（陷阱 168）
        ------------------------
        ``ready: false`` 有两种语义：①**坏了**（子环境里没有 torch ⇒ 该接管重启）；
        ②**只是睡着了**（模型空闲卸载，``model_state: unloaded``）。后者叫得醒 ——
        下一句 ``/synth`` 会自己把它读回来 —— 所以这里算就绪。混成一种的话，
        "空闲 20 分钟后再启动一次"会稳定地报一句"tts 未就绪"，而它其实好好的。
        """
        probe = self._health_probe(spec)
        if not probe.answered:
            return False, probe.error or "没有应答"
        if probe.status is not None and probe.status != 200:
            return False, f"HTTP {probe.status}"
        verdict = _self_report(probe)
        if verdict is not None:
            return verdict
        return True, probe.body[:200]

    def _takeover_unhealthy(self, spec: ServiceSpec) -> bool:
        """端口上占着的是**我们自己的、没就绪的**实例 ⇒ 接管：杀掉，本轮用当前规格表重启。

        为什么不能只报一句 ``port_busy`` 就收工
        --------------------------------------
        真机 8788 上跑的是 T2.2 落地**之前**起的旧实例：它用主解释器起，
        ``import torch`` 当场失败 ⇒ ``ready: false`` ⇒ 配音每句都退回系统语音包。
        而守护进程每几分钟看到一次「端口有人占着，不关我事」就收工，这个状态于是能
        一直挂到人手动去关 —— 用户看到的是「配音怎么一直不是那个音色」。

        四条判据缺一不可（**宁可不动手，也不能杀错**）
        ----------------------------------------------
        ① 对面答的是**我们的**健康面（同时自报 ``ready`` 与 ``pid`` 两个字段）；
        ② 自报的 pid 真的活着；
        ③ 它自报**没就绪** —— 健康的实例一律不碰（可能是用户自己起的，也可能正在干活）；
        ④ 它**不是"睡着了"**：空闲卸载（``model_state: unloaded``）是健康状态，
           杀它只会让用户白等一次 20s 加载，而且加载期间面板会退回系统语音包（陷阱 168）。

        :return: 接管成功（端口已空出来）⇒ ``True``；否则 ``False``，调用方照旧记 port_busy。
        """
        probe = self._health_probe(spec)
        payload = probe.payload
        if payload is None or payload.get("ready") is not False or "engine" not in payload:
            # 要么不是我们的服务（没有自述字段），要么它自报是**就绪的** ⇒ 不碰
            return False
        if wakeable_health(payload):
            logger.info(
                "service.port_busy_wakeable",
                service=spec.name,
                port=spec.port,
                reason=_self_report_reason(payload),
            )
            return False
        pid = payload.get("pid")
        if not isinstance(pid, int) or not self._processes.alive(pid):
            # 认得出它坏了、却认不出它是谁（T2.2 之前的版本不自报 pid）⇒ 至少把原因喊出来。
            # 这正是真机上那一台：只回一句 port_busy，人会以为"端口冲突"，去改配置。
            logger.warning(
                "service.port_busy_unready",
                service=spec.name,
                port=spec.port,
                reason=_self_report_reason(payload),
                hint="端口上那个实例自报未就绪、又不肯说自己是谁：停一次服务再启动，就会用当前规格表重起",
            )
            return False
        logger.warning(
            "service.takeover",
            service=spec.name,
            port=spec.port,
            pid=pid,
            reason=_self_report_reason(payload),
        )
        self._processes.terminate(pid)
        if not self._processes.wait(pid, TAKEOVER_GRACE_SEC):
            self._processes.kill(pid)
            self._processes.wait(pid, TAKEOVER_GRACE_SEC)
        if not self._await_port_free(spec):
            logger.warning("service.takeover_port_stuck", service=spec.name, port=spec.port)
            return False
        return True

    def _await_port_free(self, spec: ServiceSpec, *, timeout_sec: float = TAKEOVER_FREE_SEC) -> bool:
        """等端口真的空出来：杀完到 bind 之间有一段窗口，抢跑会直接 bind 失败。"""
        assert spec.port is not None  # 只有带端口的规格会走到这里
        deadline = self._monotonic() + timeout_sec
        while self._probe(self._host, spec.port):
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.1)
        return True

    def start(
        self,
        *,
        open_browser: bool = True,
        only: Sequence[str] | None = None,
        doctor_gate: bool = True,
        ready_timeout_sec: float = DEFAULT_READY_TIMEOUT_SEC,
        poll_sec: float = 0.25,
    ) -> StartReport:
        """拉起服务并等就绪。

        :param only: 只拉起这些服务（排障用；默认全部）
        :param doctor_gate: 启动前跑 ``studio doctor``，任何阻塞项 ⇒ **拒绝启动**
        :raises DoctorGateError: 门禁未过（R1 的硬约束：磁盘/环境变量/journal_mode）
        """
        began = self._monotonic()
        self._paths.ensure_runtime_dirs()

        if doctor_gate:
            gate = self._run_doctor()
            gate.raise_if_blocked()  # 阻塞项 ⇒ 抛 DoctorGateError（拒绝启动）

        wanted = set(only) if only is not None else set(SERVICE_NAMES)
        for name in wanted:
            self.spec(name)  # 早失败：名字打错不该走到一半才发现

        self.clean_stale()
        readiness = tuple(item for item in self.readiness_all() if item.name in wanted)
        ready_map = {item.name: item for item in readiness}

        started: list[str] = []
        already: list[str] = []
        busy: list[str] = []
        degraded: list[str] = []
        failed: list[str] = []

        for spec in self._specs:
            if spec.name not in wanted:
                continue
            item = ready_map[spec.name]
            if not item.ready:
                degraded.append(spec.name)
                logger.warning(
                    "service.not_ready",
                    service=spec.name,
                    readiness=str(item.readiness),
                    detail=item.detail,
                )
                continue
            if self._running_pid(spec.name) is not None:
                already.append(spec.name)
                continue
            # 端口有人占着 ⇒ 先问一句"是不是**我们自己**那个没就绪的旧实例"：
            # 是就接管（见 _takeover_unhealthy），不是才记 port_busy 收工。
            if (
                spec.port is not None
                and self._probe(self._host, spec.port)
                and not self._takeover_unhealthy(spec)
            ):
                busy.append(spec.name)
                logger.warning("service.port_busy", service=spec.name, port=spec.port)
                continue
            try:
                pid = self._spawn(spec)
            except OSError as exc:
                failed.append(spec.name)
                logger.exception("service.spawn_failed", service=spec.name, error=str(exc))
                continue
            self._write_pid(spec.name, pid)
            started.append(spec.name)
            logger.info("service.started", service=spec.name, pid=pid)

        ready, unhealthy = self._await_ready(
            [self._by_name[name] for name in [*started, *already]],
            timeout_sec=ready_timeout_sec,
            poll_sec=poll_sec,
        )
        failed.extend(unhealthy)
        ready = [name for name in ready if name not in unhealthy]

        opened = False
        if open_browser and "api" in ready:
            try:
                opened = bool(self._browser(self.url))
            except Exception as exc:  # 打开浏览器失败不是启动失败
                logger.warning("service.browser_failed", url=self.url, error=str(exc))

        report = StartReport(
            started=tuple(started),
            ready=tuple(ready),
            degraded=tuple(degraded),
            failed=tuple(failed),
            already_running=tuple(already),
            port_busy=tuple(busy),
            readiness=readiness,
            url=self.url,
            browser_opened=opened,
            elapsed_ms=int((self._monotonic() - began) * 1000),
        )
        logger.info("service.start_done", **report.to_dict())
        return report

    def _run_doctor(self) -> DoctorReport:
        if self._doctor_factory is not None:
            return self._doctor_factory()
        return Doctor(self._paths, env=self._env).run()

    def _await_ready(
        self,
        specs: Sequence[ServiceSpec],
        *,
        timeout_sec: float,
        poll_sec: float,
    ) -> tuple[list[str], list[str]]:
        """等就绪：HTTP 服务等 ``/health``；池服务只看**别立刻死**。

        :return: ``(ready, unhealthy)`` —— ``unhealthy`` 是超时/夭折的
        """
        deadline = self._monotonic() + max(0.0, timeout_sec)
        pending = list(specs)
        ready: list[str] = []
        unhealthy: list[str] = []
        pool_deadline: dict[str, float] = {}

        while pending:
            still: list[ServiceSpec] = []
            for spec in pending:
                pid = self._running_pid(spec.name)
                if pid is None:
                    unhealthy.append(spec.name)
                    logger.error("service.died_before_ready", service=spec.name)
                    continue
                if spec.kind is ServiceKind.POOL:
                    # 池进程没有 HTTP 面 ⇒ 用"活过观察窗"代替握手（T4.11 才有真探活）
                    due = pool_deadline.setdefault(spec.name, self._monotonic() + POOL_GRACE_SEC)
                    if self._monotonic() >= due:
                        ready.append(spec.name)
                    else:
                        still.append(spec)
                    continue
                healthy, detail = self._health(spec)
                if healthy:
                    ready.append(spec.name)
                    logger.info("service.ready", service=spec.name, detail=detail)
                else:
                    still.append(spec)
            pending = still
            if not pending:
                break
            if self._monotonic() >= deadline:
                for spec in pending:
                    unhealthy.append(spec.name)
                    logger.error("service.ready_timeout", service=spec.name)
                break
            self._sleep(poll_sec)
        return ready, unhealthy

    # ── 关停 ────────────────────────────────────────────────────────────
    def stop(
        self,
        *,
        timeout_sec: float = DEFAULT_STOP_TIMEOUT_SEC,
        only: Sequence[str] | None = None,
    ) -> StopReport:
        """优雅关停（标志 → Ctrl-Break → terminate → kill）。

        每一级都留时间给 worker 跑完当前单元；只有**跑不完**才升级，
        并且升级结果如实记进 :attr:`StopReport.forced`。
        """
        began = self._monotonic()
        wanted = set(only) if only is not None else set(SERVICE_NAMES)
        for name in wanted:
            self.spec(name)

        targets: list[tuple[str, int]] = []
        missing: list[str] = []
        skipped: list[str] = []
        for spec in self._specs:
            if spec.name not in wanted:
                continue
            pid = self._read_pid(spec.name)
            if pid is None:
                skipped.append(spec.name)
                continue
            if not self._processes.alive(pid):
                missing.append(spec.name)
                self._clear(spec.name)
                continue
            targets.append((spec.name, pid))

        if not targets:
            return StopReport(missing=tuple(missing), skipped=tuple(skipped), elapsed_ms=0)

        # 1) 主通道：标志文件（与"命令从哪个控制台发出"无关）
        for name, _pid in targets:
            flag = self._paths.stop_flag_file(name)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("stop", encoding="utf-8")
        logger.info("service.stop_requested", services=[name for name, _ in targets])

        budget = max(0.0, timeout_sec)
        stopped: list[str] = []
        pending: list[tuple[str, int]] = list(targets)

        # 1) 主通道：标志文件 —— **只等会轮询它的进程**（裁定 107）
        pollers = [(name, pid) for name, pid in pending if self._by_name[name].polls_stop_flag]
        if pollers:
            still = self._wait_gone(pollers, budget=budget * 0.6)
            stopped.extend(name for name, _pid in pollers if name not in still)
            pending = [(name, pid) for name, pid in pending if name in still]

        # 2) 升级：Ctrl-Break（同控制台才有效，尽力而为）
        if pending:
            signalled = [name for name, pid in pending if self._processes.request_break(pid)]
            if signalled:
                logger.warning("service.stop_escalated_break", services=signalled)
            still = self._wait_gone(pending, budget=max(0.5, budget * 0.3))
            stopped.extend(name for name, _pid in pending if name not in still)
            pending = [(name, pid) for name, pid in pending if name in still]

        # 3) 升级：terminate ⇒ kill（**强制**，如实记录 —— 不假装优雅）
        forced: list[str] = []
        if pending:
            for _name, pid in pending:
                self._processes.terminate(pid)
            still = self._wait_gone(pending, budget=max(1.0, budget * 0.2))
            left = [(name, pid) for name, pid in pending if name in still]
            for _name, pid in left:
                self._processes.kill(pid)
            self._wait_gone(left, budget=1.0)
            forced = [name for name, _pid in left]
            stopped.extend(name for name, _pid in pending if name not in forced)
            if forced:
                logger.error("service.stop_forced", services=forced)

        for name, _pid in targets:
            self._clear(name)
        return StopReport(
            stopped=tuple(stopped),
            forced=tuple(forced),
            missing=tuple(missing),
            skipped=tuple(skipped),
            elapsed_ms=int((self._monotonic() - began) * 1000),
        )

    def _wait_gone(self, targets: Sequence[tuple[str, int]], *, budget: float) -> set[str]:
        """等这些进程退出；返回**仍未退出**的名字集合。"""
        if not targets:
            return set()
        deadline = self._monotonic() + max(0.0, budget)
        remaining = {name for name, _pid in targets}
        while remaining:
            for name, pid in targets:
                if name in remaining and not self._processes.alive(pid):
                    remaining.discard(name)
            if not remaining or self._monotonic() >= deadline:
                break
            self._sleep(min(0.2, max(0.02, budget / 20.0)))
        return remaining

    # ── 查看 ────────────────────────────────────────────────────────────
    def status(self) -> tuple[ServiceStatus, ...]:
        """台账 + 存活 + 端口（``studio service status``）。"""
        rows: list[ServiceStatus] = []
        for spec in self._specs:
            pid = self._read_pid(spec.name)
            rows.append(
                ServiceStatus(
                    name=spec.name,
                    pid=pid,
                    alive=bool(pid is not None and self._processes.alive(pid)),
                    port=spec.port,
                    port_open=bool(spec.port is not None and self._probe(self._host, spec.port)),
                    log_file=self._paths.service_log_file(spec.name),
                )
            )
        return tuple(rows)


def default_manager(paths: StudioPaths | None = None, **kwargs: Any) -> ServiceManager:
    """按配置构造管理器（端口从 ``config/app.yaml`` 取，与 API 绑的端口同源）。"""
    resolved = paths or StudioPaths.from_env()
    try:
        loaded = load_config(resolved)
    except StudioError:
        return ServiceManager(resolved, **kwargs)
    web = loaded.bundle.app.web
    return ServiceManager(resolved, host=web.host, **kwargs)
