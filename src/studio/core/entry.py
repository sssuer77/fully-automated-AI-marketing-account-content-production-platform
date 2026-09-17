"""``workers/run_<name>.py`` 的公共骨架：跑 → 出错就说清 → 退出码（T1.12）。

为什么住在 ``core`` 而不是 ``services``（T2.3 收尾）
--------------------------------------------------
``tts`` 是**唯一**跑在另一个 venv 里的进程（``tts/.venv``，只装推理那点依赖，
见 ``config/tts.yaml: python``）。它的入口 ``workers/run_tts.py`` 原先从
``studio.services.service_manager`` 取这个函数 —— 而 ``studio.services`` 的
``__init__`` 会把整个服务层（``input_service`` → ``ulid`` → …）一并拖进来，
于是真机上 tts 一启动就是 ``ModuleNotFoundError: No module named 'ulid'``：
**进程根本起不来，日志里却像"推理环境坏了"**（真机实测 2026-09-18）。

这个函数只依赖 ``core``（错误码 / 日志 / json / sys），所以它本来就属于 ``core``：
子环境的入口不该因为"借一个十行的骨架"而被拖进整层业务代码。
``studio.services.service_manager`` 仍然原样 re-export 它，老调用点不受影响。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from studio.core.errors import StudioError
from studio.core.logging import get_logger

__all__ = ["run_entry"]

logger = get_logger("studio.core.entry")


def run_entry(name: str, runner: Callable[[], Any]) -> int:
    """``workers/run_<name>.py`` 的公共骨架：跑 → 出错就说清 → 退出码。

    退出码是**契约**：``studio service start`` 靠它判断"这个进程是不是起来就死了"。
    失败一律落 ``stderr`` 且带错误码与修复提示 —— 子进程的 stdout/stderr 会进
    ``data/logs/<name>.log``（裁定 106），排障时第一眼就能看到原因。
    """
    try:
        result = runner()
    except StudioError as exc:
        # StudioError 是**预期内的诊断**（带 code + remediation），堆栈只会淹没原因。
        logger.error(  # noqa: TRY400 -- 见上
            "service.entry_failed", service=name, code=str(exc.code), error=exc.message
        )
        print(f"[{name}] 启动失败 [{exc.code}] {exc.message}", file=sys.stderr, flush=True)
        if exc.remediation:
            print(f"[{name}] 修复：{exc.remediation}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:  # 前台 Ctrl+C：优雅退出，不算失败
        print(f"[{name}] 收到中断，已优雅退出", flush=True)
        return 0
    as_dict = getattr(result, "to_dict", None)
    if callable(as_dict):
        print(f"[{name}] {json.dumps(as_dict(), ensure_ascii=False)}", flush=True)
    return 0
