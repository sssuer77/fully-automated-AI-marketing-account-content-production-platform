"""ID 生成（§2.4：``task_id`` 为 ULID，字典序即时间序）。

ULID 相对 UUID4 的两个关键优势：
1. **单调可排序** ⇒ SQLite 主键索引无随机写入放大；
2. **内嵌时间戳** ⇒ 无需额外列即可按时间范围扫描。
"""

from __future__ import annotations

import hashlib
from typing import Final

from ulid import ULID

__all__ = [
    "IDEMPOTENCY_SEP",
    "new_job_id",
    "new_request_id",
    "new_task_id",
    "new_ulid",
    "publication_idempotency_key",
    "sha256_hex",
    "stable_key",
]

#: 发布幂等键的字段分隔符（§03.3.15 逐字：``sha256(task_id|platform|account_id)``）。
#: **不是** :func:`sha256_hex` 的 ``\x1f``：那个分隔符是内部约定，而这个键的形态写在
#: 规格书里、而且已经落到真机的 ``publications.idempotency_key`` 上了 —— 改它等于让
#: 每一条已有记录换个键，重复发布防护当场失效。
IDEMPOTENCY_SEP: Final[str] = "|"

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

    用于 ``jobs`` 的 ``(task_id, pool, unit_type, unit_ref)`` 唯一键。
    发布那条走 :func:`publication_idempotency_key`（分隔符不同，见那边的注释）。
    """
    return sha256_hex(*parts)


def publication_idempotency_key(task_id: str, platform: str, account_id: str) -> str:
    """发布幂等键 ``sha256(task_id|platform|account_id)``（§03.3.15 · §06.2.4）。

    为什么把 ``account_id`` 算进去
    ------------------------------
    同一个任务**分发到两个账号**是两件不同的事（§06.2.4「同任务可安全分发到多账号」），
    键里不含账号就会互相顶掉 —— 而 ``publications.idempotency_key`` 是 UNIQUE 的，
    顶掉的表现是"第二个账号永远发不出去"。

    为什么返回摘要而不是那个 ``|`` 拼接串
    ------------------------------------
    键要进 UNIQUE 索引（长度稳定）**并且**会被写进日志与告警（``|`` 拼接串会把
    ``task_id`` 和账号名一起泄进日志行）。

    为什么住在 ``core/`` 而不是 ``domain/publish.py``
    -----------------------------------------------
    ``publications`` 那一行的**唯一写入者**是 ``db/repositories/publication_repo.py``，
    而分层是 ``core → db → domain``：``db`` 反向 import ``domain`` 会被契约测试
    ``tests/contract/test_no_direct_job_write.py::test_db_layer_does_not_import_domain``
    当场拦下（这条不是洁癖 —— 键的 UNIQUE 约束在 DDL 里，算键的规则就该和它同一层）。
    ``domain.publish.idempotency_key`` 保留为**别名**，契约名不变。
    """
    raw = IDEMPOTENCY_SEP.join((task_id, platform, account_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
