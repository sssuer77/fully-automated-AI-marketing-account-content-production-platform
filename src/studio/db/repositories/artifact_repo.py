"""``artifacts`` 的读写（T2.7 起 · §03.3.11）。

为什么现在才出现这张表的仓储
--------------------------
``artifacts`` 是"这条片子的产物清单"：产出了什么、在哪、多大、指纹是多少。
一期它一直空着，因为**没有代码读它**（面板与 GC 都还没接，GC 的真相是
``config/retention.yaml``）；T2.7 是第一个真正产出"要留痕的产物"的任务
（``timeline.json`` 与 ``voice_master.wav``），顺手把这张表点亮。
先写后读不是问题：清单的价值在于"出事之后还查得到当时产出了什么"。

为什么 ``path`` 一律相对 ``data/``
----------------------------------
列注释就是这么写的（"相对 STUDIO_DATA_DIR"），理由与 ``timeline.json`` 里那一条
一样：清单要能跟着 ``data/`` 搬家。落在 ``data/`` 之外 ⇒ 退回绝对路径而不是抛，
理由见 :func:`studio.core.paths.data_relative`。

为什么是 ``upsert`` 而不是 ``insert``
------------------------------------
同一条产物会被重复产出（重合成 / 重渲染 / 重跑时间轴），而
``UNIQUE(task_id, kind, path)`` 决定了"同一份产物只有一行"。第二次写入要
**刷新事实**（字节数 / 指纹 / 时长），而不是报错、也不是插第二行 ——
那两样都会让清单变成"越跑越乱"，而清单乱了比没有清单更坏。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.core.files import file_sha256
from studio.core.ids import new_ulid
from studio.core.paths import data_relative
from studio.db.engine import transaction

__all__ = [
    "ARTIFACT_COLUMNS",
    "ArtifactRepo",
    "ArtifactRow",
]

#: 读取时用的列清单（一处写全，免得"加了一列忘了带上"）。
ARTIFACT_COLUMNS = (
    "id, task_id, sentence_id, kind, path, bytes, sha256, render_hash, meta_json, ttl_hint, "
    "expires_at, created_at"
)


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """一行 ``artifacts``（面板的"这条片子产出了什么"直接读它）。"""

    id: str
    task_id: str
    kind: str
    path: str
    sentence_id: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    render_hash: str | None = None
    meta: Mapping[str, Any] | None = None
    ttl_hint: str | None = None
    expires_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ArtifactRow:
        raw = row["meta_json"]
        try:
            meta = json.loads(str(raw)) if raw is not None else {}
        except ValueError:
            # 元数据读坏了只说明"这一行的附加信息丢了"，不该让整个清单读不出来
            # （与 T2.6 缓存元数据同一条口径：附加信息坏了就退化成"没有附加信息"）。
            meta = {}
        return cls(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            kind=str(row["kind"]),
            path=str(row["path"]),
            sentence_id=row["sentence_id"],
            size_bytes=row["bytes"],
            sha256=row["sha256"],
            render_hash=row["render_hash"],
            meta=meta if isinstance(meta, dict) else {},
            ttl_hint=row["ttl_hint"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )


class ArtifactRepo:
    """``artifacts`` 的写入与按任务读取。"""

    def __init__(self, connection: sqlite3.Connection, *, data_dir: Path) -> None:
        self._connection = connection
        self._data_dir = data_dir

    def record(
        self,
        *,
        task_id: str,
        kind: str,
        path: Path,
        sentence_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
        ttl_hint: str | None = None,
        render_hash: str | None = None,
    ) -> None:
        """记一条产物（已存在就刷新事实）。

        ``bytes`` / ``sha256`` 在事务**之外**算（R10：事务内禁止任何 I/O）——
        它们要读盘，而这条事务里只有一条 SQL。
        """
        size: int | None = None
        digest: str | None = None
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        if size:
            digest = file_sha256(path) or None

        with transaction(self._connection, immediate=True):
            self._connection.execute(
                "INSERT INTO artifacts "
                "(id, task_id, sentence_id, kind, path, bytes, sha256, render_hash, meta_json, ttl_hint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id, kind, path) DO UPDATE SET "
                "bytes = excluded.bytes, sha256 = excluded.sha256, "
                "render_hash = excluded.render_hash, meta_json = excluded.meta_json, "
                "ttl_hint = excluded.ttl_hint, sentence_id = excluded.sentence_id",
                (
                    new_ulid(),
                    task_id,
                    sentence_id,
                    kind,
                    data_relative(path, self._data_dir),
                    size,
                    digest,
                    render_hash,
                    json.dumps(dict(meta or {}), ensure_ascii=False),
                    ttl_hint,
                ),
            )

    def list_for_task(self, task_id: str, *, kind: str | None = None) -> list[ArtifactRow]:
        """这个任务的产物清单（可按 ``kind`` 收窄），按 ``kind`` + 路径排序。"""
        if kind is None:
            rows = self._connection.execute(
                f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE task_id = ? ORDER BY kind, path",
                (task_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE task_id = ? AND kind = ? "
                "ORDER BY kind, path",
                (task_id, kind),
            ).fetchall()
        return [ArtifactRow.from_row(row) for row in rows]

    def kinds_for_task(self, task_id: str) -> Sequence[str]:
        """这个任务产出过哪些 ``kind``（去重、按字母序）—— 面板画"产物"一栏用。"""
        rows = self._connection.execute(
            "SELECT DISTINCT kind FROM artifacts WHERE task_id = ? ORDER BY kind", (task_id,)
        ).fetchall()
        return [str(row[0]) for row in rows]
