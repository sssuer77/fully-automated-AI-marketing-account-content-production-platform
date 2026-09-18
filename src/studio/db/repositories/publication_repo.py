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

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid, publication_idempotency_key
from studio.db.engine import transaction

__all__ = [
    "CANCELED",
    "FAILED",
    "MANUAL_REQUIRED",
    "METRICS_HISTORY_LIMIT",
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

#: ``metrics_history_json`` 的**截断长度**（见 :meth:`PublicationRepo.record_metrics`）。
METRICS_HISTORY_LIMIT: Final[int] = 48

_COLUMNS: Final[str] = (
    "id, task_id, platform, account_id, profile_key, video_path, video_sha256, cover_path, "
    "title, caption, tags_json, status, dry_run, url, platform_post_id, published_at, "
    "scheduled_at, next_metric_at, metric_attempts, attempt_count, max_attempts, error_code, error_message, "
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
    #: 当前时点**失败**了几次（0007 的 `consecutive_oom` 同款计数器 · T5.4）。
    metric_attempts: int = 0
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
            "metric_attempts": self.metric_attempts,
            "metrics": dict(self.metrics),
            "metrics_history": [dict(item) for item in self.metrics_history],
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
            metric_attempts=int(row["metric_attempts"]),
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
        key = publication_idempotency_key(task_id, platform, account_id)
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
        key = publication_idempotency_key(task_id, platform, account_id)
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

    def mark_dry_run(self, pub_id: str, *, evidence: Mapping[str, Any] | None = None) -> PublicationRow:
        """演练收工：回 ``queued`` + 留取证，**不碰** ``attempt_count``（§06.5.3 第 ⑥ 步）。

        为什么演练的落点是 ``queued`` 而不是 ``published``
        -------------------------------------------------
        ``publications`` 那一行是"这份内容在这个账号上处于什么状态"的**唯一凭据**。
        把演练写成 ``published`` 等于在库里记下一条**从没发生过的发布**：面板上多一条
        点不开的作品，而限频守卫正是按 ``status IN ('uploading','published')`` 数
        "今天发了几条"的 —— 演练会把自己的额度占掉。

        ``WHERE status <> 'published'``：一条真发出去过的记录不会被后一次演练抹掉。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, evidence_json = ?, error_code = NULL, error_message = NULL
             WHERE id = ? AND status <> ?
            """,
            (QUEUED, _dump(evidence), pub_id, PUBLISHED),
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

        ``attempt_count`` 照样 +1：这条路是 :meth:`mark_failed` 的**替代**而不是补充
        （见那边的注释"到了就直接调 mark_manual_required"），所以那一次失败的账要记在
        这里。漏掉它，面板上会出现"试了 2 次"配着"重试 3 次仍失败"的结论 —— 两个数字
        说的是同一件事，对不上就没人信第二个。
        """
        self._connection.execute(
            """
            UPDATE publications
               SET status = ?, attempt_count = attempt_count + 1,
                   error_code = ?, error_message = ?, evidence_json = ?
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

    # ── 数据回收（T5.4 · §06.6）──────────────────────────────────

    def record_metrics(
        self,
        pub_id: str,
        metrics: Mapping[str, Any],
        *,
        next_metric_at: str | None,
        history_limit: int = METRICS_HISTORY_LIMIT,
    ) -> PublicationRow:
        """落一次成功读数：``metrics_json``（最新）+ 追加 ``metrics_history_json``。

        为什么"最新"与"历史"两列都要写
        ------------------------------
        面板要一眼看到"现在多少播放"（读 ``metrics_json``），趋势图要一串点
        （读 ``metrics_history_json``）。只留历史 ⇒ 每次读最新都要 ``json_extract``
        最后一项；只留最新 ⇒ 趋势图没有数据源（§06.6 的落库那一行明写两列）。

        为什么历史要**截断**（而不是无限追加）
        ------------------------------------
        四个时点（T+1h/6h/24h/72h）× 每条作品 = 4 个点，48 条已经足够覆盖
        "重试顺延导致的重复采集"（最多 3 次/时点）。不截断的话，一条被反复
        重试的记录会让这一行的 JSON 无限长，而它每次读取都要整段反序列化。

        ``metric_attempts`` 归 0：一次成功读数就是"这个时点办完了"，
        下一个时点重新计数（见 0010 迁移的注释）。
        """
        row = self._require(pub_id)
        history = [dict(item) for item in row.metrics_history]
        history.append(_history_entry(metrics))
        if history_limit > 0:
            history = history[-history_limit:]
        self._connection.execute(
            """
            UPDATE publications
               SET metrics_json = ?, metrics_history_json = ?,
                   next_metric_at = ?, metric_attempts = 0
             WHERE id = ?
            """,
            (json.dumps(dict(metrics), ensure_ascii=False), _dump_list(history), next_metric_at, pub_id),
        )
        return self._require(pub_id)

    def defer_metrics(self, pub_id: str, *, next_metric_at: str | None) -> PublicationRow:
        """落一次失败：计数 +1，并把下一个时点顺延（``None`` ⇒ 停止采集这一条）。

        **不写 ``error_code`` / ``error_message``**：那两列说的是"这条作品发出去
        这件事出了什么事"，而数据回收失败时作品是**好好发着**的。把采集失败写进
        去，面板上的红色错误会指向一次并不存在的发布故障。
        """
        self._connection.execute(
            "UPDATE publications SET metric_attempts = metric_attempts + 1, next_metric_at = ? WHERE id = ?",
            (next_metric_at, pub_id),
        )
        return self._require(pub_id)

    def list_due_metrics(self, *, now: str | None = None, limit: int = 20) -> tuple[PublicationRow, ...]:
        """到点该采数的记录（``status='published'`` 且 ``next_metric_at <= now``）。

        排序按 ``next_metric_at`` 升序：一个积压了很久的时点该**先**被采，
        否则"晚了 3 天的 T+1h"会排在"刚到点的 T+72h"后面，趋势图上的点会乱序。

        为什么不用 ``next_metric_at IS NOT NULL`` 之外的过滤
        --------------------------------------------------
        ``dry_run`` 的演练记录**不在**这个集合里 —— 它们的 ``status`` 是 ``queued``，
        没有 ``platform_post_id``，去平台上采不到任何东西。
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM publications "
            "WHERE status = ? AND next_metric_at IS NOT NULL AND next_metric_at <= ? "
            "ORDER BY next_metric_at LIMIT ?",
            (PUBLISHED, now or now_iso(), limit),
        ).fetchall()
        return tuple(PublicationRow.from_row(row) for row in rows)

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


def _dump_list(value: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps([dict(item) for item in value], ensure_ascii=False)


def _history_entry(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """最新读数 ⇒ 时间序列的一项（``{at, views, likes, comments, shares}`` · 0004 的列注释）。

    为什么**不原样**把 ``metrics_json`` 追加进去：那一份还带 ``source``（"这个数是谁
    给的"），而它是**当前**口径 —— 将来接了平台开放接口，历史里会出现"前半段浏览器读的、
    后半段 API 拿的"，那是好事，但趋势图的每个点只需要"什么时候、多少"。
    """
    return {
        "at": metrics.get("collected_at") or now_iso(),
        "views": metrics.get("views"),
        "likes": metrics.get("likes"),
        "comments": metrics.get("comments"),
        "shares": metrics.get("shares"),
    }
