"""文件层的两个小工具（T4.7 · 抽自 `persona_store` 与 `outputs_store` 的共同需要）。

为什么值得单独一个模块
----------------------
"这一份文件变了没有"与"这一份文件的指纹是什么"是**热重载型仓库**（`PersonaStore`、
`OutputsStore`，将来还有模板仓库）共用的两个原语。抄第二份的代价很具体：
某天要给"文件被删了"补一种处理，就得记得改两处 —— 而漏掉的那一处会表现为
"人物能热重载、合成配置不能"这种看起来毫无道理的差异（陷阱 #156 的同一条理由）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

__all__ = ["file_sha256", "stat_key", "stream_sha256"]


def file_sha256(path: Path) -> str:
    """文件的 sha256（十六进制）；读不出来 ⇒ 空串。

    读不出来返回空串而不是抛：调用方是**快照的留痕字段**，不是门禁。文件刚被删掉
    的那一瞬间，"指纹为空"是如实回答；为此把一次面板刷新打成 500 才是更坏的结果
    （真正的硬门禁在写入路径与 `doctor`）。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def stat_key(path: Path) -> tuple[int, int] | None:
    """廉价的变更探测键 ``(mtime_ns, size)``；文件不存在 ⇒ ``None``。

    为什么不用 sha256 直接比：热重载要**每次读取都探一次**（面板 5s 一刷、
    worker 每个单元一次），而 sha256 要把整份文件读进内存。`stat` 是一次系统调用。
    代价是"内容变了但 mtime 与 size 都没变"探测不到 —— 实际写作（编辑器 / 我们的
    写入口）一定会改 mtime，而真正的内容比对在 :func:`file_sha256` 那一侧兜底。
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


#: 分块大小（1 MiB）。挑这个数的理由：素材可能是几百 MB 的跑酷视频，
#: ``read_bytes()`` 那种一次性读法在入库时会把内存吃出一个尖峰；而块太小
#: （4 KiB）又会让一次哈希变成几万次系统调用。
HASH_CHUNK_BYTES: Final[int] = 1 << 20


def stream_sha256(path: Path, *, chunk_size: int = HASH_CHUNK_BYTES) -> str:
    """大文件的 sha256（**分块**读，不把整个视频读进内存）。

    与 :func:`file_sha256` 的分工是**失败语义**，不是实现细节：

    - ``file_sha256`` 服务于"快照留痕字段"，读不出来给空串（软失败）；
    - ``stream_sha256`` 服务于"素材入库的指纹"，读不出来**抛** ``OSError`` ——
      入库没有指纹就等于没有幂等键，此时静默写一个空指纹进去，
      下一次重扫会把同一条素材再插一遍（那正是指纹要防的事）。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
