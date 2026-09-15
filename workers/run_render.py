"""``render`` 池进程入口：渲染（跑酷循环裁长 + 水印 + 混音）（T1.12 · §02.2）。

真正的单元处理器（``pools.runner.register_handler``）随业务任务落地
（draft→T1.8 队列消费 / voice→T2.6 / render→T3.x）。**未注册时直接报错退出**，
不静默起一个空转 worker —— "看着在跑、队列永远不消化"是最坏的情况。
启动器同样**先问再拉**（裁定 103）。
"""

from __future__ import annotations

from studio.pools import run_pool
from studio.services.service_manager import run_entry


def _run() -> object:
    return run_pool(pool="render", slot=1)


if __name__ == "__main__":
    raise SystemExit(run_entry("render", _run))
