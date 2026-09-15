"""``draft`` 池进程入口：写稿 + 审稿（T1.12 留入口 · **T4.11 接线** · §02.2）。

装配在这里，不在服务层
----------------------
"去哪里拿配置与提示词"是**入口**的责任（T1.9 裁定 70 的延伸）：服务层只认 Protocol
（可注入假件），入口负责把真实世界接上去。本文件因此只有三步 ——
建连接 ⇒ 造处理器 ⇒ 注册 ⇒ 跑池。

**未注册就报错退出**（不静默起一个空转 worker）—— "看着在跑、队列永远不消化"
是最坏的情况；启动器同样**先问再拉**（裁定 103），它问的是
``pools.runner.HANDLER_MODULES`` 里声明的那张表。
"""

from __future__ import annotations

from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.pools import register_handler, run_pool
from studio.pools.draft_worker import build_draft_handler
from studio.services.log_service import LogService
from studio.services.service_manager import run_entry

logger = get_logger("studio.workers.draft")


def _register(paths: StudioPaths) -> None:
    """装配并注册写稿池的单元处理器。

    这条连接**故意不关**：`LogService` 与 LLM 网关（熔断器 / token 预算 / 记账）
    都挂在它上面，而池 worker 是"起一次、跑到关停"的常驻进程 —— 单元之间把它关掉
    等于第二篇稿子就写不出来了。进程退出时由操作系统回收（与 `LogService` 在
    `build_state` 里的用法一致）。
    """
    connection = connect(paths.db_file)
    handler = build_draft_handler(paths=paths, log=LogService(connection))
    register_handler("draft", handler)
    logger.info("worker.handler_registered", pool="draft", unit_types=sorted(handler.unit_types))


def _run() -> object:
    _register(StudioPaths.from_env())
    return run_pool(pool="draft", slot=1)


if __name__ == "__main__":
    raise SystemExit(run_entry("draft", _run))
