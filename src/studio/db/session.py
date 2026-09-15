"""按线程取连接（T1.7 · §02.2 / R10）。

为什么必须有这一层
------------------
`sqlite3` 的连接是**线程亲和**的（`check_same_thread=True`），而应用进程里至少有三类
线程会碰数据库：

1. **事件循环线程**（uvicorn）：Hub 的 tail、async 路由；
2. **线程池**（Starlette 把同步 `def` 路由丢进 `anyio` 线程池）：REST 查询；
3. **测试**：`TestClient` 会在自己的 portal 线程里跑事件循环。

"一个进程一条连接"在 1 与 2 之间就会直接 `ProgrammingError`。所以口径统一为
**每线程一条**（T1.5 裁定 27 / T1.6 裁定 41），本类就是这个口径的唯一实现。

写并发呢？
----------
连接是 autocommit（`isolation_level=None`），跨连接靠 SQLite 自身的写锁 + WAL 协调；
本项目**单进程单写者**（§03.7.2 规则 2），不需要额外的写队列。真要串行化写，
那是 `write_queue.py` 的事（R10 的后续落点），不是本模块。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from studio.core.logging import get_logger
from studio.db.engine import connect

__all__ = ["ThreadLocalConnections"]

logger = get_logger("studio.db.session")


class ThreadLocalConnections:
    """连接工厂：同一线程复用一条，不同线程各一条。"""

    def __init__(self, db_path: Path | str, *, read_only: bool = False, apply: bool = True) -> None:
        self._path = Path(db_path)
        self._read_only = read_only
        self._apply = apply
        self._local = threading.local()
        self._lock = threading.Lock()
        self._opened: list[sqlite3.Connection] = []

    @property
    def db_path(self) -> Path:
        return self._path

    def get(self) -> sqlite3.Connection:
        """取本线程的连接（没有就建一条）。"""
        existing: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        connection = connect(self._path, read_only=self._read_only, apply=self._apply)
        self._local.connection = connection
        with self._lock:
            self._opened.append(connection)
        logger.debug("db.connection_opened", db=str(self._path), thread=threading.current_thread().name)
        return connection

    def close_all(self) -> int:
        """尽力关闭（**只关得动本线程建的**；其余随进程退出释放）。

        跨线程 `close()` 会被 `sqlite3` 拒绝（同样是线程亲和检查），所以这里吞掉
        `sqlite3.Error` —— 关不掉不是错误，是预期。
        """
        with self._lock:
            connections = list(self._opened)
            self._opened.clear()
        closed = 0
        for connection in connections:
            try:
                connection.close()
                closed += 1
            except sqlite3.Error:
                continue
        return closed

    @property
    def opened(self) -> int:
        """已建连接数（诊断 / 测试用）。"""
        return len(self._opened)
