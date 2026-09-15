"""``api`` 进程入口：WebUI 操作台（T1.12 · §02.2 · 原文附2）。

由 ``studio service start`` 拉起（``ops/start_all.ps1`` → ``启动.bat``），
也可以直接 ``python workers/run_api.py`` 前台跑。子进程的 stdout/stderr 由
启动器重定向到 ``data/logs/api.log``（裁定 106）。

**为什么要有这个文件**：进程入口是"部署面"的一部分 —— 启动器、supervisor、
将来 nssm 注册的服务都指向它，改命令行不该动业务代码。
"""

from __future__ import annotations

from studio.app.main import run_server
from studio.services.service_manager import run_entry

if __name__ == "__main__":
    raise SystemExit(run_entry("api", run_server))
