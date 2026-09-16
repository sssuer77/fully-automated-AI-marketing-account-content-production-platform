"""``voice`` 池进程入口：配音（按句合成 + 句级续传）（T1.12 留入口 · **T2.6 接线** · §02.2）。

装配在这里，不在服务层
----------------------
"去哪里拿配置、词表与音色"是**入口**的责任（T1.9 裁定 70 的延伸）：服务层只认
Protocol（可注入假件），入口负责把真实世界接上去。本文件因此只有三步 ——
造处理器 ⇒ 注册 ⇒ 跑池。

**未注册就报错退出**（不静默起一个空转 worker）—— "看着在跑、队列永远不消化"
是最坏的情况；启动器同样**先问再拉**（裁定 103），它问的是
``pools.runner.HANDLER_MODULES`` 里声明的那张表。

为什么这里没有像 ``run_draft`` 那样显式建连接
--------------------------------------------
写稿池必须显式建：它要同时喂 LLM 网关（熔断器 / token 预算都挂在连接上）。
配音池没有这样的共享状态 —— 处理器自己开一条，连进度写入、句级回填与日志一起用
（``build_voice_handler`` 的默认行为），多写一行只会多一处能忘。

音色在**这里**解析（``build_voice_handler`` 的缺省行为）：列音色要起一个
PowerShell（1–2 秒），放在单元里就是"每念一句先卡两秒"。解析不到 ⇒ 进程起不来
并报 ``TTS_ENGINE_UNAVAILABLE`` —— 这比"起来了，但每句都失败"好得多。
"""

from __future__ import annotations

from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.pools import register_handler, run_pool
from studio.pools.voice_worker import build_voice_handler
from studio.services.service_manager import run_entry

logger = get_logger("studio.workers.voice")


def _register(paths: StudioPaths) -> None:
    """装配并注册配音池的单元处理器。"""
    handler = build_voice_handler(paths=paths)
    register_handler("voice", handler)
    logger.info("worker.handler_registered", pool="voice", unit_types=sorted(handler.unit_types))


def _run() -> object:
    _register(StudioPaths.from_env())
    return run_pool(pool="voice", slot=1)


if __name__ == "__main__":
    raise SystemExit(run_entry("voice", _run))
