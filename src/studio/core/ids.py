"""ID 生成（§2.4：``task_id`` 为 ULID，字典序即时间序）。

ULID 相对 UUID4 的两个关键优势：
1. **单调可排序** ⇒ SQLite 主键索引无随机写入放大；
2. **内嵌时间戳** ⇒ 无需额外列即可按时间范围扫描。
"""

from __future__ import annotations

import hashlib
from typing import Final

from ulid import ULID

__all__ = ["new_job_id", "new_request_id", "new_task_id", "new_ulid", "sha256_hex", "stable_key"]

_ULID_LEN: Final[int] = 26


def new_ulid() -> str:
    """生成 26 字符 ULID 字符串。"""
    return str(ULID())


def new_task_id() -> str:
    """任务 ID（主链路，落 ``tasks.id``）。"""
    return new_ulid()


def new_job_id() -> str:
    """队列作业 ID（落 ``jobs.id``）。"""
    return new_ulid()


def new_request_id() -> str:
    """HTTP 请求 ID（中间件注入，落访问日志与 ``system_logs``）。"""
    return new_ulid()


def sha256_hex(*parts: str) -> str:
    """对多段字符串做稳定 sha256（UTF-8，``\\x1f`` 分隔防歧义）。"""
    digest = hashlib.sha256()
    digest.update("\x1f".join(parts).encode("utf-8"))
    return digest.hexdigest()


def stable_key(*parts: str) -> str:
    """幂等键（同 :func:`sha256_hex`，语义化别名）。

    用于 ``jobs`` 的 ``(task_id, pool, unit_type, unit_ref)`` 唯一键
    与发布幂等键 ``sha256(task_id|platform|account_id)``。
    """
    return sha256_hex(*parts)
