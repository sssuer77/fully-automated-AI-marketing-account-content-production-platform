"""``publish`` 池进程入口：发布到平台（T1.12 留入口 · **T5.3 接线** · §02.2）。

装配在这里，不在服务层
----------------------
"去哪里拿配置、用哪个账号、开不开演练"是**入口**的责任（T1.9 裁定 70 的延伸）：
服务层只认数据，入口负责把真实世界接上去。本文件因此只有三步 ——
造处理器 ⇒ 注册 ⇒ 跑池。

**未注册就报错退出**（不静默起一个空转 worker）—— "看着在跑、队列永远不消化"
是最坏的情况；启动器同样**先问再拉**（裁定 103），它问的是
``pools.runner.HANDLER_MODULES`` 里声明的那张表。

为什么这里没有像 ``run_draft`` 那样显式建连接
--------------------------------------------
写稿池必须显式建：它要同时喂 LLM 网关（熔断器 / token 预算都挂在连接上）。
发布池没有这样的共享状态 —— 处理器自己开一条，连进度写入、``publications``
与留痕一起用（``build_publish_handler`` 的默认行为），多写一行只会多一处能忘。

配置在**这里**读（``build_publish_handler`` 的缺省行为）
--------------------------------------------------------
平台表 / 账号表 / 限频 / 演练开关都是"这次进程启动时定的环境事实"。放进单元里就是
"每发一条先读八份 yaml"，而其中任何一份写错都会在**第一次发布**时才炸 ——
那时作业已经在跑发布流程了。启动期报错比运行期报错便宜得多。

**headless 恒为真**：常驻 worker 不开窗口（发布池 `concurrency=1`，一个可见窗口
会把操作员的桌面占住）。要看界面就人工跑 ``studio publish dry-run --headed``。
"""

from __future__ import annotations

from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.pools import register_handler, run_pool
from studio.pools.publish_worker import build_publish_handler
from studio.services.service_manager import run_entry

logger = get_logger("studio.workers.publish")


def _register(paths: StudioPaths) -> None:
    """装配并注册发布池的单元处理器。"""
    handler = build_publish_handler(paths=paths)
    register_handler("publish", handler)
    logger.info("worker.handler_registered", pool="publish", unit_types=sorted(handler.unit_types))


def _run() -> object:
    _register(StudioPaths.from_env())
    return run_pool(pool="publish", slot=1)


if __name__ == "__main__":
    raise SystemExit(run_entry("publish", _run))
