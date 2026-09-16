"""``publications`` 的读写（T5.3 · §03.3.15 / §06.5.4）。

为什么单独一个仓储
------------------
与 ``artifacts`` / ``audit_ops`` 同一条：一张表一个写入者。发布记录是
"这条片子发到了哪里、当时发的是什么、出了什么事"的**唯一凭据**（R13/R14），
散着写会让"发了没发"变成一个需要去几个地方对的问题。

幂等：一份内容一个账号只有一行
------------------------------
``idempotency_key = sha256(task_id|platform|account_id)`` 上有 UNIQUE（§03.3.15）。
:meth:`PublicationRepo.create` 命中已有行时**返回已有的那一行**，不报错、也不插第二行：
重投一次发布作业（断点续跑 / 人工重试 / 定时到点）都会走到这一行，
而报错会把一个正常的重复调用变成一条无人处理的异常。

状态机（§06.5.4）
------------------
```text
queued ─▶ uploading ─▶ published
   │          │
   │          ├─▶ failed ──(重试<3)──▶ queued
   │          │      └──(重试≥3)─▶ manual_required
   │          └─▶ manual_required（登录态失效 / 审核不通过 / 选择器失效）
   └─▶ canceled（人工取消）
```

**这张表与 ``tasks`` 无关**：发布失败**不回退任务状态**（任务停在
``completed``）—— 成片仍然有效，可人工下载或换平台重发。本模块**一行 ``tasks``
的 SQL 都没有**，而 ``tests/contract/test_publisher_abc.py`` 把这个"缺席"钉着。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.domain.publish import idempotency_key

__all__ = [
    "CANCELED",
    "FAILED",
    "MANUAL_REQUIRED",
    "PUBLICATION_STATUSES",
    "PUBLISHED",
    "QUEUED",
    "UPLOADING",
    "PublicationRepo",
    "PublicationRow",
]

#: §06.5.4 的六态（与 DDL 的 CHECK 逐字一致）
QUEUED: Final[str] = "queued"
UPLOADING: Final[str] = "uploading"
PUBLISHED: Final[str] = "published"
FAILED: Final[str] = "failed"
MANUAL_REQUIRED: Final[str] = "manual_required"
CANCELED: Final[str] = "canceled"

PUBLICATION_STATUSES: Final[tuple[str, ...]] = (
    QUEUED,
    UPLOADING,
    PUBLISHED,
    FAILED,
    MANUAL_REQUIRED,
    CANCELED,
)

#: 计入"今天发了几条"的状态。与 ``db/queue.py`` 的 ``_RATE_LIMIT_STATUSES``
#: **必须一致**（那边是限频守卫的口径，这里是面板的口径）。
COUNTED_STATUSES: Final[tuple[str, ...]] = (UPLOADING, PUBLISHED)

_COLUMNS: Final[str] = (
    "id, task_id, platform, account_id, profile_key, video_path, video_sha256, cover_path, "
    "title, caption, tags_json, status, dry_run, url, platform_post_id, published_at, "
    "scheduled_at, next_metric_at, attempt_count, max_attempts, error_code, error_message, "
    "evidence_json, metrics_json, metrics_history_json, idempotency_key, created_at, "
    "updated_at, finished_at"
)


@dataclass(frozen=True, slots=True)
class PublicationRow:
    """一行 ``publications``。"""

    id: str
    task_id: str
    platform: str
    account_id: str
    status: str
    video_path: str
    video_sha256: str
    title: str
    caption: str
    tags: tuple[str, ...] = ()
    profile_key: str | None = None
    cover_path: str | None = None
    dry_run: bool = False
    url: str | None = None
    platform_post_id: str | None = None
    published_at: str | None = None
    scheduled_at: str | None = None
    next_metric_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    metrics_history: Sequence[Mapping[str, Any]] = ()
    idempotency_key: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        """终态：``published`` / ``canceled``。

        ``manual_required`` **不是**终态 —— 它在面板上是"等人"。
        """
        return self.status in (PUBLISHED, CANCELED)

    @property
    def needs_human(self) -> bool:
        return self.status == MANUAL_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "status": self.status,
            "title": self.title,
            "caption": self.caption,
            "tags": list(self.tags),
            "profile_key": self.profile_key,
            "cover_path": self.cover_path,
            "video_path": self.video_path,
            "dry_run": self.dry_run,
            "url": self.url,
            "platform_post_id": self.platform_post_id,
            "published_at": self.published_at,
            "scheduled_at": self.scheduled_at,
            "next_metric_at": self.next_metric_at,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PublicationRow:
        return cls(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            platform=str(row["platform"]),
            account_id=str(row["account_id"]),
            status=str(row["status"]),
            video_path=str(row["video_path"]),
            video_sha256=str(row["video_sha256"]),
            title=str(row["title"]),
            caption=str(row["caption"]),
            tags=tuple(_json_list(row["tags_json"])),
            profile_key=_opt(row, "profile_key"),
            cover_path=_opt(row, "cover_path"),
            dry_run=bool(row["dry_run"]),
            url=_opt(row, "url"),
            platform_post_id=_opt(row, "platform_post_id"),
            published_at=_opt(row, "published_at"),
            scheduled_at=_opt(row, "scheduled_at"),
            next_metric_at=_opt(row, "next_metric_at"),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            error_code=_opt(row, "error_code"),
            error_message=_opt(row, "error_message"),
            evidence=_json_map(row["evidence_json"]),
            metrics=_json_map(row["metrics_json"]),
            metrics_history=tuple(_json_list(row["metrics_history_json"])),
            idempotency_key=str(row["idempotency_key"]),
            created_at=_opt(row, "created_at"),
            updated_at=_opt(row, "updated_at"),
            finished_at=_opt(row, "finished_at"),
        )


class PublicationRepo:
    """``publications`` 的唯一写入者与读取入口。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 读 ───────────────────────────────────────────────────────

    def get(self, pub_id: str) -> PublicationRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE id = ?", (pub_id,)
        ).fetchone()
        return None if row is None else PublicationRow.from_row(row)

    def find(self, *, task_id: str, platform: str, account_id: str) -> PublicationRow | None:
        """按**幂等键的三个组成部分**查（与 ``idempotency_key`` 同义，但可读）。"""
        key = idempotency_key(task_id, platform, account_id)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else PublicationRow.from_row(row)

    def list_for_task(self, task_id: str) -> tuple[PublicationRow, ...]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return tuple(PublicationRow.from_row(row) for row in rows)

    def list_by_status(self, status: str | Sequence[str], *, limit: int = 100) -> tuple[PublicationRow, ...]:
        """按状态取（发布面板的七区块都走这一条）。

        新的在前：面板最上面那一条应该是最近发生的那一条。
        """
        wanted = (status,) if isinstance(status, str) else tuple(status)
        _reject_unknown_statuses(wanted)
        placeholders = ",".join("?" for _ in wanted)
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE status IN ({placeholders}) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (*wanted, max(int(limit), 1)),
        ).fetchall()
        return tuple(PublicationRow.from_row(row) for row in rows)

    def manual_queue(self, *, limit: int = 100) -> tuple[PublicationRow, ...]:
        """待人工处理列表（§06.10 不变量 2：**必须在面板上可见**）。"""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE status = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (MANUAL_REQUIRED, max(int(limit), 1)),
        ).fetchall()
        return tuple(PublicationRow.from_row(row) for row in rows)

    def counts(self) -> dict[str, int]:
        """各状态有多少条（面板顶部那一行数字）。没有行的状态补 0。"""
        rows = self._connection.execute(
            "SELECT status, count(*) AS n FROM publications GROUP BY status"
        ).fetchall()
        tally = dict.fromkeys(PUBLICATION_STATUSES, 0)
        for row in rows:
            tally[str(row["status"])] = int(row["n"])
        return tally

    # ── 写 ───────────────────────────────────────────────────────

    def create(
        self,
        *,
        task_id: str,
        platform: str,
        account_id: str,
        video_path: str,
        video_sha256: str,
        title: str,
        caption: str = "",
        tags: Sequence[str] = (),
        cover_path: str | None = None,
        profile_key: str | None = None,
        dry_run: bool = False,
        scheduled_at: str | None = None,
        max_attempts: int = 3,
    ) -> tuple[PublicationRow, bool]:
        """登记一次发布（**幂等**）。

        :return: ``(row, created)`` —— ``created=False`` 说明这份内容在这个账号上
            **已经登记过**（重投 / 断点续跑 / 定时到点都会走到），返回的是
            原来那一行。调用方据此决定"还要不要再往队列里投一次"。
        """
        key = idempotency_key(task_id, platform, account_id)
        existing = self.get_by_key(key)
        if existing is not None:
            return existing, False

        pub_id = new_ulid()
        with transaction(self._connection, immediate=True):
            self._connection.execute(
                """
                INSERT INTO publications(
                    id, task_id, platform, account_id, profile_key, video_path, video_sha256,
                    cover_path, title, caption, tags_json, status, dry_run, scheduled_at,
                    max_attempts, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    pub_id,
                    task_id,
                    platform,
                    account_id,
                    profile_key,
                    video_path,
                    video_sha256,
                    cover_path,
                    title,
                    caption,
                    json.dumps(list(tags), ensure_ascii=False),
                    QUEUED,
                    1 if dry_run else 0,
                    scheduled_at,
                    max(1, int(max_attempts)),
                    key,
                ),
            )
        row = self.get_by_key(key)
        if row is None:
            raise StudioError(
                f"发布记录写不进去：{task_id}/{platform}/{account_id}",
                code=ErrorCode.DB_SCHEMA_DRIFT,
                context={"task_id": task_id, "platform": platform, "account_id": account_id},
                remediation="检查 publications 表与 idempotency_key 的 UNIQUE 索引（0004_publish.sql）",
            )
        return row, row.id == pub_id

    def get_by_key(self, key: str) -> PublicationRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else PublicationRow.from_row(row)

    def mark_uploading(self, pub_id: str) -> PublicationRow:
        """``queued`` ⇒ ``uploading``（第 ② 步开始）。已在往上的不重置。"""
        self._connection.execute(
            "UPDATE publications SET status = ?, error_code = NULL, error_message = NULL "
            "WHERE id = ? AND status IN (?, ?)",
            (UPLOADING, pub_id, QUEUED, UPLOADING),
        )
        return self._require(pub_id)

    def mark_published(
        self,
        pub_id: str,
        *,
        url: str | None = None,
        platform_post_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        next_metric_at: str | None = None,
    ) -> PublicationRow:
        """第 ⑧ 步：真的发出去了。

        ``published_at`` 由**数据库**落（``strftime('now')``）而不是应用层传入：
        限频守卫就是拿这一列数“今天发了几条”的，两个时钟会让那个数字在边界上飘。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, url = ?, platform_post_id = ?,
                   published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                   next_metric_at = ?, evidence_json = ?, error_code = NULL, error_message = NULL,
                   finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE id = ?
            """,
            (
                PUBLISHED,
                url,
                platform_post_id,
                next_metric_at,
                _dump(evidence),
                pub_id,
            ),
        )
        return self._require(pub_id)

    def mark_failed(
        self,
        pub_id: str,
        *,
        error_code: str,
        error_message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> PublicationRow:
        """一次失败（**还会重试**）：``attempt_count += 1``，状态落 ``failed``。

        到没到上限由调用方判（它手上有 ``ctx.attempt`` 与 ``max_attempts``），
        到了就直接调 :meth:`mark_manual_required` —— 不在这里做两步。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, attempt_count = attempt_count + 1,
                   error_code = ?, error_message = ?, evidence_json = ?
             WHERE id = ?
            """,
            (FAILED, error_code, error_message, _dump(evidence), pub_id),
        )
        return self._require(pub_id)

    def mark_manual_required(
        self,
        pub_id: str,
        *,
        error_code: str,
        error_message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> PublicationRow:
        """转人工（§06.10）。

        **不碰 ``tasks``**：成片仍然有效，人可以从渲染面板下载后手工发。
        ``finished_at`` 也不写 —— 它在面板上是"等人"，人接手之后才算完结。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, error_code = ?, error_message = ?, evidence_json = ?
             WHERE id = ?
            """,
            (MANUAL_REQUIRED, error_code, error_message, _dump(evidence), pub_id),
        )
        return self._require(pub_id)

    def cancel(self, pub_id: str, *, reason: str | None = None) -> PublicationRow:
        """人工取消（§06.5.4 的 ``canceled`` 边）。已发出去的**不能**取消。"""
        row = self._require(pub_id)
        if row.status == PUBLISHED:
            raise StudioError(
                f"已发布的记录不能取消：{pub_id}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": pub_id, "status": row.status},
                remediation="平台上已经有了这条作品，只能到平台去删",
            )
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, error_message = COALESCE(?, error_message),
                   finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE id = ?
            """,
            (CANCELED, reason, pub_id),
        )
        return self._require(pub_id)

    def reset_for_retry(self, pub_id: str) -> PublicationRow:
        """人工重试：回 ``queued`` 并把计数归零（"给它一次完整的机会"）。

        与 ``JobStore.requeue_unit`` 同一条口径：不归零的话，一条已经用完
        三次机会的作业重投上去会**立刻再次转人工**。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, attempt_count = 0, error_code = NULL, error_message = NULL,
                   finished_at = NULL
             WHERE id = ?
            """,
            (QUEUED, pub_id),
        )
        return self._require(pub_id)

    # ── 内部 ─────────────────────────────────────────────────────

    def _require(self, pub_id: str) -> PublicationRow:
        row = self.get(pub_id)
        if row is None:
            raise StudioError(
                f"发布记录不存在：{pub_id}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": pub_id},
            )
        return row


def _reject_unknown_statuses(wanted: Sequence[str]) -> None:
    """状态名是**白名单**的：它会被拼进 SQL 占位符，不能靠 DDL 的 CHECK 兜底。"""
    unknown = [status for status in wanted if status not in PUBLICATION_STATUSES]
    if unknown:
        raise StudioError(
            f"未知的发布状态：{unknown}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"unknown": unknown, "valid": list(PUBLICATION_STATUSES)},
        )


def _opt(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _json_map(raw: Any) -> Mapping[str, Any]:
    """JSON 列 → dict。坏 JSON 退成 ``{}`` **不抛**（与 ``db/models.py`` 同一条）。"""
    if raw is None:
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _dump(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False)
