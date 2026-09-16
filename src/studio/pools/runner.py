"""池进程的启动胶水（T1.6）。

:class:`~studio.pools.worker_base.PoolWorker` 只认 :class:`UnitHandler`；
本模块负责把它和**真实世界**接起来：读配置 → 建连接 → 装信号处理 → 起循环。

处理器注册表
------------
四个池的业务处理器随各自任务落地：``draft`` → T4.11 ✅ / ``render`` → T3.7 ✅ /
``voice`` → T2.6 ✅ / ``publish`` → T5.3。已落地的三个在 :data:`HANDLER_MODULES` 里
登记了模块名，启动器据此判"这个池现在能不能拉起来"。
没注册就启动 ⇒ 立刻 ``INTERNAL`` + 明确的 remediation，而不是"起来了但什么都不干"
（后者才是最坏的情况：看着在跑，实际队列永远不消化）。
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, Final

from studio.core.config import AutoConcurrencyConfig, PoolConfig, PoolName, load_config
from studio.core.errors import ConfigError, ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.pools.supervisor import Supervisor, WorkerSpec
from studio.pools.worker_base import PoolWorker, UnitHandler, WorkerRunReport

__all__ = [
    "HANDLERS",
    "HANDLER_MODULES",
    "HANDLER_REMEDIATION",
    "POOL_NAMES",
    "STOP_FLAG_ENV",
    "build_supervisor",
    "build_worker",
    "handler_for",
    "install_signal_handlers",
    "register_handler",
    "restore_signal_handlers",
    "run_pool",
    "stop_flag_from_env",
]

logger = get_logger("studio.pools.runner")

#: 四池名（与 DDL 的 ``jobs.pool`` CHECK / ``config/pools.yaml`` 一致）
POOL_NAMES: Final[tuple[PoolName, ...]] = ("draft", "voice", "render", "publish")

#: 关停标志文件的环境变量名（T1.12 · 裁定 102）：启动器写，worker 读
STOP_FLAG_ENV: Final[str] = "STUDIO_STOP_FLAG"


def stop_flag_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    """从环境变量解析关停标志路径（未设置 / 空串 ⇒ ``None``）。

    空串**不**当成"当前目录下的空文件名"：那会让 ``Path("")`` 变成 ``"."``，
    而 ``".".is_file()`` 恒为 False —— 看起来"配了"，实际永远不生效。
    """
    source = env if env is not None else os.environ
    raw = (source.get(STOP_FLAG_ENV) or "").strip()
    return Path(raw) if raw else None


#: 池 → 单元处理器（各业务任务启动时 ``register_handler`` 注册）
HANDLERS: Final[dict[str, UnitHandler]] = {}

#: 池 → **处理器模块**（"实现已落地"的声明 · T4.11）。
#:
#: 与 :data:`HANDLERS` 的分工要说清，否则很容易问错问题：
#:
#: - ``HANDLERS`` 是**进程内**注册表 —— 只有池 worker 进程自己会填（它 import 处理器
#:   模块再 ``register_handler``）。在 API 进程里问它，答案永远是"没有"。
#: - 而"能不能拉起这个池进程"是**启动器**要回答的问题，启动器在 API 进程里。
#:   它真正想问的是"这个池的处理器**写出来了没有**" ⇒ 用 :func:`importlib.util.find_spec`
#:   问模块在不在，与 ``tts`` 那条 ``server_missing`` 判据同一手法。
#:
#: 少了这张表，T4.11 的守护会陷入一个荒唐的循环：`draft` 进程死了 ⇒ 判死 ⇒
#: 问就绪 ⇒ "handler_missing"（因为 API 进程没注册过）⇒ **拒绝重启** ⇒ 永远起不来。
HANDLER_MODULES: Final[Mapping[str, str]] = {
    "draft": "studio.pools.draft_worker",
    "render": "studio.pools.render_worker",
    "voice": "studio.pools.voice_worker",
}

#: 处理器未落地时的统一 remediation：:func:`handler_for`（进程内取不到）与
#: ``ServiceManager``（启动前判就绪）共用**同一句**。
#:
#: 为什么非要共用：这句话抄成两份时，池的落地任务一改就会只改一处，
#: 于是 WebUI 显示"去等 T3.x"，而代码里 T3.7 早跑完了 —— T3.7 收口时真撞上过。
HANDLER_REMEDIATION: Final[str] = (
    "该池 handler 随业务任务落地：draft→T4.11（已落地）/ render→T3.7（已落地）/ "
    "voice→T2.6（已落地）/ publish→T5.3"
)


def register_handler(pool: str, handler: UnitHandler) -> None:
    """注册池的单元处理器（同池重复注册 ⇒ 后者覆盖，便于测试注入）。"""
    if pool not in POOL_NAMES:
        raise ConfigError(
            f"未知池名：{pool}",
            code=ErrorCode.CONFIG_INVALID,
            context={"pool": pool, "valid": list(POOL_NAMES)},
            remediation="池名只能是 draft / voice / render / publish",
        )
    HANDLERS[pool] = handler


def handler_for(pool: str) -> UnitHandler:
    """取池的处理器；未注册 ⇒ 明确报错（**不**静默起一个空转 worker）。"""
    try:
        return HANDLERS[pool]
    except KeyError as exc:
        raise StudioError(
            f"{pool} 池的单元处理器尚未落地",
            code=ErrorCode.INTERNAL,
            context={"pool": pool, "registered": sorted(HANDLERS)},
            remediation=HANDLER_REMEDIATION,
        ) from exc


def _as_pool_name(pool: str) -> PoolName:
    if pool not in POOL_NAMES:
        raise ConfigError(
            f"未知池名：{pool}",
            code=ErrorCode.CONFIG_INVALID,
            context={"pool": pool, "valid": list(POOL_NAMES)},
            remediation="池名只能是 draft / voice / render / publish",
        )
    return pool


def build_worker(
    *,
    pool: str,
    slot: int = 1,
    handler: UnitHandler | None = None,
    paths: StudioPaths | None = None,
    pool_config: PoolConfig | None = None,
    connection: sqlite3.Connection | None = None,
    stop_flag: Path | None = None,
) -> PoolWorker:
    """组装一个可直接 ``run()`` 的 worker（读配置 + 解析处理器）。"""
    resolved = paths or StudioPaths.from_env()
    config = pool_config
    auto: AutoConcurrencyConfig | None = None
    if config is None:
        loaded = load_config(resolved)
        config = loaded.bundle.pools.pools[_as_pool_name(pool)]
        # 自动降并发的阈值必须来自盘上那份（T4.10 裁定 149）：
        # 默认值与 YAML 出厂值相同，但**只传默认值**会让"改了 YAML 没生效"
        # —— 那正是 0006_seed 顶部点名的经典故障。
        auto = loaded.bundle.pools.auto_concurrency
    return PoolWorker(
        pool=pool,
        handler=handler or handler_for(pool),
        pool_config=config,
        paths=resolved,
        slot=slot,
        connection=connection,
        stop_flag=stop_flag,
        auto_concurrency=auto,
    )


def _default_signals() -> tuple[int, ...]:
    """要接管的信号：``SIGINT``（Ctrl+C）+ ``SIGTERM``；Windows 上再加 ``SIGBREAK``。"""
    candidates = [int(signal.SIGINT), int(signal.SIGTERM)]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        candidates.append(int(sigbreak))
    return tuple(candidates)


def install_signal_handlers(worker: PoolWorker, *, signals: Sequence[int] | None = None) -> dict[int, Any]:
    """把 ``SIGINT``/``SIGTERM`` 接到 ``worker.request_stop()``（draining，不丢进度）。

    :return: 原处理器映射 —— 调用方负责用 :func:`restore_signal_handlers` 还原
        （测试尤其需要：接管 ``SIGINT`` 而不还原会让 pytest 收不到 Ctrl+C）。
    """
    targets = tuple(signals) if signals is not None else _default_signals()
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: FrameType | None) -> None:
        del frame
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.info("worker.signal", signal=name, worker_id=worker.worker_id)
        worker.request_stop(reason=f"signal:{name}")

    for signum in targets:
        try:
            previous[signum] = signal.signal(signum, _handler)
        except (ValueError, OSError):
            continue  # 非主线程 / 平台不支持 ⇒ 跳过（不是错误）
    return previous


def restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    """还原信号处理器（幂等；非主线程调用会安静跳过）。"""
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            continue


def run_pool(
    *,
    pool: str,
    slot: int = 1,
    handler: UnitHandler | None = None,
    max_units: int | None = None,
    max_empty_rounds: int | None = None,
    paths: StudioPaths | None = None,
    stop_flag: Path | None = None,
) -> WorkerRunReport:
    """池进程入口：建目录 → 组装 worker → 接管信号 → 跑到 draining。

    ``stop_flag`` 缺省时从 ``STUDIO_STOP_FLAG`` 环境变量取（T1.12 · 裁定 102）——
    启动器给每个子进程写这个变量，于是"从任何地方发关停"都能落到同一个机制上。
    """
    resolved = paths or StudioPaths.from_env()
    resolved.ensure_runtime_dirs()
    worker = build_worker(
        pool=pool,
        slot=slot,
        handler=handler,
        paths=resolved,
        stop_flag=stop_flag if stop_flag is not None else stop_flag_from_env(),
    )
    previous = install_signal_handlers(worker)
    try:
        return worker.run(max_units=max_units, max_empty_rounds=max_empty_rounds)
    finally:
        restore_signal_handlers(previous)


def build_supervisor(
    *,
    slots: Mapping[str, int] | None = None,
    paths: StudioPaths | None = None,
    python: str | None = None,
    extra_args: Sequence[str] = (),
) -> Supervisor:
    """按四池并发配置构造 supervisor（argv 指向 ``studio pool run``）。"""
    resolved = paths or StudioPaths.from_env()
    loaded = load_config(resolved)
    executable = python or sys.executable
    specs: list[WorkerSpec] = []
    for name in POOL_NAMES:
        want = 1 if slots is None else int(slots.get(name, 1))
        want = max(1, min(want, loaded.bundle.pools.pools[name].concurrency))
        for slot in range(1, want + 1):
            specs.append(
                WorkerSpec(
                    pool=name,
                    slot=slot,
                    argv=(
                        executable,
                        "-m",
                        "studio.cli",
                        "pool",
                        "run",
                        "--pool",
                        name,
                        "--slot",
                        str(slot),
                        *extra_args,
                    ),
                    cwd=resolved.home,
                )
            )
    return Supervisor(specs=tuple(specs), paths=resolved)
