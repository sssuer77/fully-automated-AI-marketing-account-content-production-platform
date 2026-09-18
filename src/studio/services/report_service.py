"""数据报告与决策闭环（T5.7 · §04.6.5.2 / §06.7）。

报告是**结论**，不是又一份原始数据
--------------------------------
§06.6 的数据回收产出的是"这条作品多少播放"；报告产出的是"数字型钩子比悬念型高 23%，
所以下轮多写数字型"。前者是原料，后者是决定。两者的表、节奏、有没有人参与都不同
（§6.7.1 那张对照表）。

**不调 LLM**
-----------
聚合与归因是确定性问题：同一份数据跑两次必须得到同一个结论，而 LLM 做不到这一点。
代价是 `statement` 只能由模板渲染（"X 的播放中位数高于 Y 23%"），换来的是
可复现、零成本、能单测。真要自然语言总结，走 §04.1 的 Agent 通道（可选开关）。

一次 SQL 取行，其余在 Python 里算
---------------------------------
SQLite 没有 ``percentile``：用窗口函数硬算中位数会变成一段没人敢改的 SQL，而
"中位数"正是 §6.7.2 点名要的那一项（均值会被长尾拉偏）。窗口内的记录是几十到几百条，
取回内存再分组既好读又能单测。**这不是"没用 SQL"** —— 取数仍然只有一条 SQL，
分组口径写在 :func:`_group_stats` 里，一眼能看全。

样本不足必须说出来
------------------
``n < 10`` 的建议一律 ``confidence='low'`` 并在 ``statement`` 里带"样本不足"，
面板再强制显示一次（§6.7.3 的三条安全阀之二）。报告最贵的错误不是算错数，
而是**拿三条作品的差异去改生产策略**。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

from studio.core.clock import format_iso, local_tz, now_iso, parse_iso, utc_now
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.persona_store import PersonaStore
from studio.core.proto import EVENT_PAYLOAD_KEY, AlertCode, EventKind
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.report_repo import (
    ReportRepo,
    ReportRow,
    ReportScheduleRepo,
    ReportScheduleRow,
)
from studio.domain.report import (
    DEFAULT_AT_TIME,
    DEFAULT_INCLUDE,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_TZ,
    PERIODS,
    confidence_for,
    next_run_at,
    period_bounds,
    validate_at_time,
    validate_include,
    validate_lookback,
    validate_period_fields,
    validate_tz,
)
from studio.services.log_service import LogSink
from studio.services.persona_service import PersonaService

__all__ = [
    "AUDIT_APPLY",
    "AUDIT_GENERATE",
    "FAIL_STREAK_ALERT",
    "LOG_SOURCE",
    "REPORT_BATCH_LIMIT",
    "RESULT_OK",
    "RESULT_SKIPPED_DISABLED",
    "RESULT_SKIPPED_NO_DATA",
    "Insight",
    "ReportBundle",
    "ReportScheduleSpec",
    "ReportSchedulerService",
    "ReportTickReport",
    "apply_insight",
    "build_report",
    "create_schedule",
    "delete_schedule",
    "generate_report",
    "report_markdown",
    "run_now",
    "update_schedule",
]

logger = get_logger("studio.services.report")

#: 报告池的日志来源（与 ``publish.scheduler`` 并列，面板按 source 过滤）
LOG_SOURCE: Final[str] = "report.scheduler"

#: 一拍最多生成几份（防积压时一次算一堆）
REPORT_BATCH_LIMIT: Final[int] = 10

#: 连续失败到这个数 ⇒ ``system.alert``（§03.3.20：≥3，**不阻断生产**）
FAIL_STREAK_ALERT: Final[int] = 3

#: ``last_result`` 的取值（与 §03.3.20 的注释一致）
RESULT_OK: Final[str] = "ok"
RESULT_SKIPPED_DISABLED: Final[str] = "skipped_disabled"
RESULT_SKIPPED_NO_DATA: Final[str] = "skipped_no_data"

#: 留痕动作名（§06.7.5 / §3.3.20 点名了前两个）
AUDIT_GENERATE: Final[str] = "report.generate"
AUDIT_APPLY: Final[str] = "report.apply_insight"
AUDIT_SCHEDULE_CREATE: Final[str] = "report_schedule.create"
AUDIT_SCHEDULE_UPDATE: Final[str] = "report_schedule.update"
AUDIT_SCHEDULE_DELETE: Final[str] = "report_schedule.delete"

#: 报告落盘目录（``data/output/reports/``；**永久保留**，纳入备份 —— §6.7.5）
REPORT_DIRNAME: Final[str] = "reports"

#: 归因出建议的最小相对差（10%）。低于这个数就只是噪声，写进报告等于教人瞎改。
MIN_DELTA_PCT: Final[float] = 10.0

#: 七维归因的维度名（与 §04.6.5.2 的表逐字一致，面板按这个顺序画）
DIMENSIONS: Final[tuple[str, ...]] = (
    "by_hook_type",
    "by_hour",
    "by_platform",
    "by_duration",
    "by_grade",
)

_INSIGHT_KIND_BY_DIMENSION: Final[Mapping[str, str]] = {
    "by_hook_type": "topic",
    "by_hour": "timing",
    "by_platform": "platform",
    "by_grade": "quality",
    "by_duration": "quality",
}

_ACTION_BY_KIND: Final[Mapping[str, str]] = {
    "topic": "下轮选题把这个钩子类型的占比提上来（改的是选题偏好，不是某一篇稿子）",
    "timing": "把发布时段往这个时段靠（定时计划的窗口改到这一段）",
    "platform": "同一内容优先发这个平台（多账号启用后可按账号再拆）",
    "quality": "按这个档位的稿件标准写（评分口径见 §04.1.6）",
    "cost": "把它当基线；下一期报告对比它有没有上升",
}

#: 时长分档（秒）—— 归因④。分档边界是**业务口径**，不是随便切的：
#: 短视频的完播率在 30s / 60s 两个坎上变化最明显。
_DURATION_BUCKETS: Final[tuple[tuple[str, int, int], ...]] = (
    ("≤30s", 0, 30_000),
    ("30-60s", 30_000, 60_000),
    (">60s", 60_000, 1 << 62),
)


@dataclass(frozen=True, slots=True)
class Insight:
    """一条决策建议（§04.6.5.2 的 ``Insight``）。

    ``evidence`` 必须能追溯到聚合值：报告里的每个结论都要能回答"凭什么"。
    """

    kind: str
    statement: str
    evidence: dict[str, Any]
    confidence: str
    suggested_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "statement": self.statement,
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class ReportBundle:
    """一份算好但**还没落库**的报告（纯函数产出，便于单测直接断言）。"""

    period: str
    start_date: str
    end_date: str
    task_count: int
    publish_count: int
    total_views: int | None
    total_likes: int | None
    total_comments: int | None
    total_shares: int | None
    avg_views: float | None
    median_views: float | None
    llm_cost_usd: float | None
    tts_skip_ratio: float | None
    data: dict[str, Any] = field(default_factory=dict)
    insights: tuple[Insight, ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)
    summary_md: str = ""

    @property
    def has_data(self) -> bool:
        """窗口里有没有东西可写（没有 ⇒ ``skipped_no_data``，**不是失败**）。"""
        return self.publish_count > 0 or self.task_count > 0


# ══════════════════════════════════════════════════════════════════════
# 聚合（给窗口，出一份结论 —— 不碰库以外的任何东西）
# ══════════════════════════════════════════════════════════════════════


def build_report(
    connection: sqlite3.Connection,
    *,
    period: str,
    start_date: str,
    end_date: str,
    include: Sequence[str] | None = None,
) -> ReportBundle:
    """算一份报告（**不落库、不写盘**：落库与留痕在 :func:`generate_report`）。

    这样分开是为了可测：验收①②③里的"周报含 summary_md + data_json + insights"
    与"``n<10`` ⇒ confidence=low"都可以直接对返回值断言，不必先造一张报告表。
    """
    if period not in PERIODS:
        raise StudioError(
            f"不认识的报告周期：{period!r}",
            code=ErrorCode.REPORT_INVALID,
            context={"period": str(period), "periods": list(PERIODS)},
            remediation="period 只能是 daily / weekly / monthly",
        )
    wanted = validate_include(include if include is not None else DEFAULT_INCLUDE)
    start_utc, end_utc = _window(start_date, end_date)
    rows = _publication_rows(connection, start=start_utc, end=end_utc)
    task_count = _task_count(connection, start=start_utc, end=end_utc)
    metrics_rows = [row for row in rows if row["views"] is not None]

    views = [row["views"] for row in metrics_rows if row["views"] is not None]
    likes = [row["likes"] for row in metrics_rows if row["likes"] is not None]
    comments = [row["comments"] for row in metrics_rows if row["comments"] is not None]
    shares = [row["shares"] for row in metrics_rows if row["shares"] is not None]
    data: dict[str, Any] = {
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "include": list(wanted),
        "totals": {
            "task_count": task_count,
            "publish_count": len(rows),
            "with_metrics": len(metrics_rows),
            "views": _sum(views),
            "likes": _sum(likes),
            "comments": _sum(comments),
            "shares": _sum(shares),
        },
    }
    if "topics" in wanted:
        data["by_hook_type"] = _group_stats(metrics_rows, lambda row: row["hook_type"] or "未标钩子")
    if "metrics" in wanted:
        data["by_hour"] = _group_stats(
            metrics_rows,
            lambda row: None if row["hour"] is None else f"{int(row['hour']):02d}:00",
        )
        data["by_platform"] = _group_stats(metrics_rows, lambda row: row["platform"])
        data["by_duration"] = _group_stats(
            metrics_rows,
            lambda row: None if row["duration_ms"] is None else _duration_bucket(int(row["duration_ms"])),
        )
    if "quality" in wanted:
        data["by_grade"] = _group_stats(metrics_rows, lambda row: row["grade"] or "未评级")
        data["tts"] = _tts_stats(connection, start=start_utc, end=end_utc)
    if "cost" in wanted:
        data["cost"] = _cost_stats(connection, start=start_utc, end=end_utc, samples=len(rows))
    if "errors" in wanted:
        data["errors"] = _error_stats(connection, start=start_utc, end=end_utc)

    coverage = {
        "published": len(rows),
        "with_metrics": len(metrics_rows),
        "window": {"start": start_date, "end": end_date},
        "tz": DEFAULT_TZ,
    }
    bundle = ReportBundle(
        period=period,
        start_date=start_date,
        end_date=end_date,
        task_count=task_count,
        publish_count=len(rows),
        total_views=_sum(views),
        total_likes=_sum(likes),
        total_comments=_sum(comments),
        total_shares=_sum(shares),
        avg_views=None if not views else round(statistics.fmean(views), 1),
        median_views=None if not views else round(statistics.median(views), 1),
        llm_cost_usd=None if "cost" not in wanted else (data.get("cost") or {}).get("total_usd"),
        tts_skip_ratio=None if "quality" not in wanted else (data.get("tts") or {}).get("ratio"),
        data=data,
        coverage=coverage,
    )
    return replace(bundle, insights=_build_insights(data, samples=len(rows)))


def _window(start_date: str, end_date: str) -> tuple[str, str]:
    """本地日期区间 -> **UTC** 的 ``[start, end)``（与 ``publications.published_at`` 同格式）。

    本地日边界必须换算成 UTC 再比较（陷阱 #75 的同一手法）：``published_at`` 存的是
    UTC，直接拿本地日期字符串去比，会让 UTC+8 的 08:00 之前发布的记录落到前一天。
    """
    first = _parse_day(start_date)
    last = _parse_day(end_date)
    if last < first:
        raise StudioError(
            f"报告区间反了：{start_date} ~ {end_date}",
            code=ErrorCode.REPORT_INVALID,
            context={"start": start_date, "end": end_date},
            remediation="start 要早于（或等于）end",
        )
    start_local = datetime.combine(first, time.min, tzinfo=local_tz())
    end_local = datetime.combine(last + timedelta(days=1), time.min, tzinfo=local_tz())
    return (format_iso(start_local.astimezone(UTC)), format_iso(end_local.astimezone(UTC)))


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise StudioError(
            f"日期必须是 YYYY-MM-DD，拿到的是 {text!r}",
            code=ErrorCode.REPORT_INVALID,
            context={"value": str(text)},
            remediation="例如 2026-09-07",
        ) from exc


def _publication_rows(connection: sqlite3.Connection, *, start: str, end: str) -> list[dict[str, Any]]:
    """窗口内的发布记录 + 它挂着的任务属性（**一条 SQL 取全**，见模块注释）。"""
    rows = connection.execute(
        """
        SELECT p.id AS publication_id, p.task_id, p.platform, p.account_id, p.published_at,
               p.metrics_json, t.grade, t.score_total, t.payload_json, tc.hook_type
          FROM publications p
          JOIN tasks t ON t.id = p.task_id
          LEFT JOIN topic_candidates tc ON tc.id = t.source_topic_id
         WHERE p.status = 'published'
           AND p.published_at IS NOT NULL
           AND p.published_at >= ? AND p.published_at < ?
         ORDER BY p.published_at
        """,
        (start, end),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        metrics = _json_map(row["metrics_json"])
        payload = _json_map(row["payload_json"])
        published_at = str(row["published_at"])
        out.append(
            {
                "publication_id": str(row["publication_id"]),
                "task_id": str(row["task_id"]),
                "platform": str(row["platform"]),
                "account_id": str(row["account_id"]),
                "published_at": published_at,
                "hour": _local_hour(published_at),
                "views": _metric_int(metrics, "views"),
                "likes": _metric_int(metrics, "likes"),
                "comments": _metric_int(metrics, "comments"),
                "shares": _metric_int(metrics, "shares"),
                "completion_rate": _metric_float(metrics, "completion_rate"),
                "grade": None if row["grade"] is None else str(row["grade"]),
                "score_total": None if row["score_total"] is None else float(row["score_total"]),
                "hook_type": None if row["hook_type"] is None else str(row["hook_type"]),
                "duration_ms": _payload_int(payload, "target_duration_ms"),
            }
        )
    return out


def _local_hour(published_at: str) -> int | None:
    """发布时刻的**本地小时**（时段归因问的是"几点发"，不是 UTC 几点）。"""
    try:
        return parse_iso(published_at).astimezone(local_tz()).hour
    except ValueError:
        return None


def _group_stats(
    rows: Sequence[Mapping[str, Any]], key_fn: Callable[[Mapping[str, Any]], str | None]
) -> list[dict[str, Any]]:
    """按 ``key_fn`` 分组，逐组算 n / 播放均值 / 播放中位数 / 点赞均值 / 完播均值。

    ``views_median`` 是**主判据**（§6.7.2：均值会被长尾拉偏）：一条爆款能把
    一个组的均值抬到另一个组的十倍，而中位数回答的是"典型的一条怎么样"。
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        buckets.setdefault(str(key), []).append(row)
    out: list[dict[str, Any]] = []
    for key, items in buckets.items():
        views = [int(item["views"]) for item in items if item.get("views") is not None]
        likes = [int(item["likes"]) for item in items if item.get("likes") is not None]
        completions = [
            float(item["completion_rate"]) for item in items if item.get("completion_rate") is not None
        ]
        out.append(
            {
                "key": key,
                "n": len(items),
                "views_avg": None if not views else round(statistics.fmean(views), 1),
                "views_median": None if not views else round(statistics.median(views), 1),
                "likes_avg": None if not likes else round(statistics.fmean(likes), 1),
                "completion_avg": None if not completions else round(statistics.fmean(completions), 4),
            }
        )
    out.sort(key=lambda item: (-int(item["n"]), str(item["key"])))
    return out


def _duration_bucket(duration_ms: int) -> str:
    for label, low, high in _DURATION_BUCKETS:
        if low <= duration_ms < high:
            return label
    return _DURATION_BUCKETS[-1][0]


def _tts_stats(connection: sqlite3.Connection, *, start: str, end: str) -> dict[str, Any]:
    """TTS 质量（归因⑥）：窗口内任务的句子里有多少是 ``skipped``。

    ``skipped`` 是"这句没合成"（音色缺失 / 空文本 / 人工跳过）。比例高说明
    成片里有一段是**静音或原声**，而它与完播率的关系正是这一维要回答的问题。
    """
    row = connection.execute(
        """
        SELECT count(*) AS total,
               sum(CASE WHEN s.tts_status = 'skipped' THEN 1 ELSE 0 END) AS skipped
          FROM script_sentences s
          JOIN tasks t ON t.id = s.task_id
         WHERE t.finished_at >= ? AND t.finished_at < ?
        """,
        (start, end),
    ).fetchone()
    total = 0 if row is None else int(row["total"] or 0)
    skipped = 0 if row is None else int(row["skipped"] or 0)
    return {
        "sentences": total,
        "skipped": skipped,
        "ratio": None if total == 0 else round(skipped / total, 4),
    }


def _cost_stats(connection: sqlite3.Connection, *, start: str, end: str, samples: int) -> dict[str, Any]:
    """成本（归因⑦）：``llm_calls`` 汇总 + 单位成本（元/条成片）。

    ``per_video_usd`` 的分母是**窗口内的发布条数**而不是 LLM 调用次数：
    "一条成片要花多少钱"才是能拿去做决定的那个数。
    """
    head = connection.execute(
        "SELECT sum(cost_usd) AS total, count(*) AS calls FROM llm_calls"
        " WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()
    total = 0.0 if head is None or head["total"] is None else float(head["total"])
    calls = 0 if head is None else int(head["calls"] or 0)
    by_agent = [
        {
            "agent": str(row["agent"]),
            "cost_usd": round(0.0 if row["cost"] is None else float(row["cost"]), 4),
            "calls": int(row["calls"] or 0),
        }
        for row in connection.execute(
            "SELECT agent, sum(cost_usd) AS cost, count(*) AS calls FROM llm_calls"
            " WHERE created_at >= ? AND created_at < ? GROUP BY agent ORDER BY cost DESC",
            (start, end),
        )
    ]
    return {
        "total_usd": round(total, 4),
        "calls": calls,
        "per_video_usd": None if samples <= 0 else round(total / samples, 4),
        "by_agent": by_agent,
    }


def _error_stats(connection: sqlite3.Connection, *, start: str, end: str) -> dict[str, Any]:
    """异常（§6.7.2 最后一行）：失败任务 / 死信 / 待人工堆积。"""
    return {
        "failed_tasks": _scalar(
            connection,
            "SELECT count(*) FROM tasks WHERE status = 'failed' AND finished_at >= ? AND finished_at < ?",
            (start, end),
        ),
        "failed_publications": _scalar(
            connection,
            "SELECT count(*) FROM publications WHERE status = 'failed'"
            " AND created_at >= ? AND created_at < ?",
            (start, end),
        ),
        "manual_required": _scalar(
            connection,
            "SELECT count(*) FROM publications WHERE status = 'manual_required'"
            " AND created_at >= ? AND created_at < ?",
            (start, end),
        ),
        "dead_jobs": _scalar(
            connection,
            "SELECT count(*) FROM jobs WHERE status = 'dead' AND finished_at >= ? AND finished_at < ?",
            (start, end),
        ),
    }


def _scalar(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = connection.execute(sql, params).fetchone()
    return 0 if row is None else int(row[0] or 0)


def _task_count(connection: sqlite3.Connection, *, start: str, end: str) -> int:
    """窗口内**出片**的任务数（``completed`` / ``published``，与 StatsRepo 同一口径）。"""
    return _scalar(
        connection,
        "SELECT count(*) FROM tasks WHERE status IN ('completed','published')"
        " AND finished_at >= ? AND finished_at < ?",
        (start, end),
    )


def _sum(values: Sequence[int]) -> int | None:
    """求和；**一个数都没有时给 ``None`` 而不是 0**。

    0 是"确实没人看"，``None`` 是"我们没采到数"。合成 0 会让报告写
    "本期播放 0"，而真实情况可能是采集还没跑 —— 这两件事的处置完全相反。
    """
    return None if not values else int(sum(values))


def _json_map(raw: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _metric_int(metrics: Mapping[str, Any], key: str) -> int | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _metric_float(metrics: Mapping[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _payload_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ══════════════════════════════════════════════════════════════════════
# 决策建议（insights）—— 报告的价值就在这一层
# ══════════════════════════════════════════════════════════════════════

_DIMENSION_LABEL: Final[Mapping[str, str]] = {
    "by_hook_type": "选题类型",
    "by_hour": "发布时段",
    "by_platform": "平台",
    "by_duration": "时长",
    "by_grade": "稿件评分",
}

_PERIOD_LABEL: Final[Mapping[str, str]] = {"daily": "日报", "weekly": "周报", "monthly": "月报"}

#: 主判据的名字（进 ``statement`` 与 ``evidence``，**只有这一处写**）
_METRIC_LABEL: Final[str] = "播放中位数"


def _build_insights(data: Mapping[str, Any], *, samples: int) -> tuple[Insight, ...]:
    """按七维里能对比的那几维出建议（**没对比就不出**，不硬凑）。"""
    out: list[Insight] = []
    for dimension in DIMENSIONS:
        groups = data.get(dimension)
        if not isinstance(groups, list):
            continue
        insight = _dimension_insight(dimension, groups, samples=samples)
        if insight is not None:
            out.append(insight)
    cost = data.get("cost")
    if isinstance(cost, dict):
        insight = _cost_insight(cost, samples=samples)
        if insight is not None:
            out.append(insight)
    return tuple(out)


def _dimension_insight(
    dimension: str, groups: Sequence[Mapping[str, Any]], *, samples: int
) -> Insight | None:
    """一个维度里"最好 vs 最差"的对比。

    出建议的三个门槛（缺一条就不出）：
    ① 至少两组**有播放中位数**的（只有一组时"最好"就是"唯一"）；
    ② 最差那组的中位数 > 0（从 0 起步谈不上"高百分之多少"，除零会直接炸）；
    ③ 相对差 ≥ :data:`MIN_DELTA_PCT`（低于它的差异是噪声，写进报告等于教人瞎改）。
    """
    usable = [item for item in groups if item.get("views_median") is not None and int(item.get("n") or 0) > 0]
    if len(usable) < 2:
        return None
    best = max(usable, key=lambda item: float(item["views_median"]))
    worst = min(usable, key=lambda item: float(item["views_median"]))
    if str(best["key"]) == str(worst["key"]):
        return None
    floor = float(worst["views_median"])
    if floor <= 0:
        return None
    delta = (float(best["views_median"]) - floor) / floor * 100.0
    if delta < MIN_DELTA_PCT:
        return None
    n = min(int(best["n"]), int(worst["n"]))
    confidence = confidence_for(n)
    kind = _INSIGHT_KIND_BY_DIMENSION[dimension]
    statement = (
        f"{_DIMENSION_LABEL[dimension]}：{best['key']} 的{_METRIC_LABEL}高于 {worst['key']} {delta:.0f}%"
    )
    if confidence == "low":
        statement = f"（样本不足，仅供参考）{statement}"
    return Insight(
        kind=kind,
        statement=statement,
        evidence={
            "dimension": dimension,
            "metric": "views_median",
            "n": n,
            "a": str(best["key"]),
            "a_value": float(best["views_median"]),
            "b": str(worst["key"]),
            "b_value": floor,
            "delta_pct": round(delta, 1),
            "samples": samples,
        },
        confidence=confidence,
        suggested_action=_ACTION_BY_KIND[kind],
    )


def _cost_insight(cost: Mapping[str, Any], *, samples: int) -> Insight | None:
    """成本建议：这是一条**事实陈述**（本期花了多少），不是对比。"""
    total = cost.get("total_usd")
    if not total or samples <= 0:
        return None
    per_video = cost.get("per_video_usd")
    return Insight(
        kind="cost",
        statement=(
            f"本期 {samples} 条发布的 LLM 成本合计 {total} 美元"
            + (f"，单条 {per_video} 美元" if per_video is not None else "")
        ),
        evidence={
            "metric": "llm_cost_usd",
            "n": samples,
            "total_usd": total,
            "per_video_usd": per_video,
            "calls": cost.get("calls"),
        },
        confidence=confidence_for(samples),
        suggested_action=_ACTION_BY_KIND["cost"],
    )


# ══════════════════════════════════════════════════════════════════════
# summary_md（人类可读的那一份 —— 面板直接渲染、导出也用它）
# ══════════════════════════════════════════════════════════════════════


def report_markdown(bundle: ReportBundle, *, generated_at: str, trigger: str) -> str:
    """把一份算好的报告渲染成 Markdown。

    刻意用表格而不是自然语言段落：数字对不上的时候，表格能一眼看出是哪一格错了，
    而"本期表现稳中有升"这种句子只能靠人去猜它指的是哪几个数。
    """
    coverage = bundle.coverage or {}
    lines: list[str] = [
        f"# {_PERIOD_LABEL.get(bundle.period, bundle.period)}（{bundle.start_date} ~ {bundle.end_date}）",
        "",
        f"> 生成于 {generated_at}（{trigger}）｜发布 {bundle.publish_count} 条，"
        f"其中有读数 {coverage.get('with_metrics', 0)} 条",
        "",
        "## 一、产量概览",
        "",
        *_table(
            ("指标", "值"),
            (
                ("出片任务", str(bundle.task_count)),
                ("发布条数", str(bundle.publish_count)),
            ),
        ),
        "",
        "## 二、效果指标",
        "",
        *_table(
            ("指标", "总量", "均值", "中位数"),
            (
                ("播放", _num(bundle.total_views), _num(bundle.avg_views), _num(bundle.median_views)),
                ("点赞", _num(bundle.total_likes), "", ""),
                ("评论", _num(bundle.total_comments), "", ""),
                ("分享", _num(bundle.total_shares), "", ""),
            ),
        ),
        "",
    ]
    data = bundle.data
    for dimension in DIMENSIONS:
        groups = data.get(dimension)
        if not isinstance(groups, list) or not groups:
            continue
        lines.extend(_dimension_section(_DIMENSION_LABEL[dimension], groups))
    tts = data.get("tts")
    if isinstance(tts, dict):
        lines.extend(
            [
                "## 六、TTS 质量",
                "",
                *_table(
                    ("句子总数", "未合成（skipped）", "比例"),
                    (
                        (
                            str(tts.get("sentences", 0)),
                            str(tts.get("skipped", 0)),
                            _ratio(tts.get("ratio")),
                        ),
                    ),
                ),
                "",
            ]
        )
    cost = data.get("cost")
    if isinstance(cost, dict):
        lines.extend(
            [
                "## 七、成本",
                "",
                *_table(
                    ("LLM 调用", "合计（美元）", "单条发布（美元）"),
                    (
                        (
                            str(cost.get("calls", 0)),
                            _num(cost.get("total_usd")),
                            _num(cost.get("per_video_usd")),
                        ),
                    ),
                ),
                "",
            ]
        )
        by_agent = cost.get("by_agent")
        if isinstance(by_agent, list) and by_agent:
            lines.extend(
                _table(
                    ("Agent", "调用次数", "成本（美元）"),
                    tuple(
                        (str(item.get("agent")), str(item.get("calls")), _num(item.get("cost_usd")))
                        for item in by_agent
                        if isinstance(item, dict)
                    ),
                )
            )
            lines.append("")
    errors = data.get("errors")
    if isinstance(errors, dict):
        lines.extend(
            [
                "## 八、异常",
                "",
                *_table(
                    ("失败任务", "失败发布", "待人工", "死信作业"),
                    (
                        (
                            str(errors.get("failed_tasks", 0)),
                            str(errors.get("failed_publications", 0)),
                            str(errors.get("manual_required", 0)),
                            str(errors.get("dead_jobs", 0)),
                        ),
                    ),
                ),
                "",
            ]
        )
    lines.extend(["## 决策建议", ""])
    if not bundle.insights:
        lines.append("本期没有可对比的差异（样本太少，或各分组之间没有明显差别）—— **这不是故障**。")
    else:
        for index, insight in enumerate(bundle.insights, start=1):
            flag = "⚠️ 样本不足 · " if insight.confidence == "low" else ""
            lines.append(f"{index}. {flag}{insight.statement}")
            lines.append(f"   - 建议：{insight.suggested_action}")
            lines.append(f"   - 置信度：{insight.confidence}（n={insight.evidence.get('n')}）")
    lines.append("")
    lines.append("> 建议**不会自动生效**：要人去面板点「采纳」才会写进人物偏好（§6.7.4 的安全阀一）。")
    return "\n".join(lines)


def _dimension_section(label: str, groups: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        f"## {label}归因",
        "",
        *_table(
            (label, "条数", "播放中位数", "播放均值", "点赞均值", "完播均值"),
            tuple(
                (
                    str(item.get("key")),
                    str(item.get("n")),
                    _num(item.get("views_median")),
                    _num(item.get("views_avg")),
                    _num(item.get("likes_avg")),
                    _ratio(item.get("completion_avg")),
                )
                for item in groups
            ),
        ),
        "",
    ]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def _num(value: Any) -> str:
    """数字上屏；``None`` 显示为 ``—``（**不是 0**，见 :func:`_sum`）。"""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return str(value)


def _ratio(value: Any) -> str:
    """比值上屏（``0.423`` -> ``42.3%``）；``None`` ⇒ ``—``。"""
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


# ══════════════════════════════════════════════════════════════════════
# 生成 / 采纳 / 导出
# ══════════════════════════════════════════════════════════════════════

#: ``style_hint`` 里最多保留几条「数据结论」（见 :func:`_merge_style_hint`）
STYLE_HINT_LIMIT: Final[int] = 5

#: 结论行的前缀（**用它认旧行**，改这个前缀会让历史结论被当成普通文案留着）
_INSIGHT_PREFIX: Final[str] = "【数据结论"


def generate_report(
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    period: str,
    start_date: str | None = None,
    end_date: str | None = None,
    include: Sequence[str] | None = None,
    trigger: str = "manual",
    skip_when_empty: bool = False,
    actor: str = "user",
    actor_ref: str | None = None,
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ReportRow:
    """生成一份报告：算 -> 落盘 -> 落库 -> 留痕 -> 发事件。

    ``skip_when_empty``（调度器传 ``True``）：窗口里一条数据都没有时**不生成行**。
    一份"本期什么都没有"的报告会污染列表（周报列表里一半是空报告），而
    "这周没数据"这件事在调度器的 ``last_result`` 上已经看得见。人工点生成时
    不传它 —— 人明确要一份，就给一份。

    幂等：同 ``(period, start_date, end_date)`` 已存在 ⇒ **直接返回那一行**，
    不插入、不重写文件。判据与 DDL 的 ``UNIQUE`` 逐字一致，重复触发时
    得到的是"同一份报告"，而不是一条 IntegrityError。
    """
    moment = parse_iso(now) if now is not None else utc_now()
    repo = ReportRepo(connection)
    if start_date is None or end_date is None:
        auto_start, auto_end = period_bounds(period, now=moment, lookback_days=None)
        start_date = start_date or auto_start
        end_date = end_date or auto_end
    existing = repo.find_period(period=period, start_date=start_date, end_date=end_date)
    if existing is not None:
        _emit(
            log,
            "info",
            f"这个周期的报告已经有了：{period} {start_date}~{end_date} ⇒ 直接用那一份（{existing.id}）",
            payload={
                "report_id": existing.id,
                "period": period,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        return existing
    bundle = build_report(
        connection, period=period, start_date=start_date, end_date=end_date, include=include
    )
    if skip_when_empty and not bundle.has_data:
        raise _NoDataError(f"{period} {start_date}~{end_date} 里没有任何发布或出片记录")
    generated_at = format_iso(moment)
    summary_md = report_markdown(bundle, generated_at=generated_at, trigger=trigger)
    path = _write_report_file(paths, bundle, summary_md)
    row = repo.create(
        period=period,
        start_date=start_date,
        end_date=end_date,
        trigger=trigger,
        task_count=bundle.task_count,
        publish_count=bundle.publish_count,
        total_views=bundle.total_views,
        total_likes=bundle.total_likes,
        total_comments=bundle.total_comments,
        total_shares=bundle.total_shares,
        avg_views=bundle.avg_views,
        median_views=bundle.median_views,
        llm_cost_usd=bundle.llm_cost_usd,
        tts_skip_ratio=bundle.tts_skip_ratio,
        summary_md=summary_md,
        data=bundle.data,
        insights=[item.to_dict() for item in bundle.insights],
        artifacts=[str(path)],
        coverage=bundle.coverage,
    )
    AuditRepo(connection).record(
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=actor_ref,
        action=AUDIT_GENERATE,
        target_type="report",
        target_id=row.id,
        before=None,
        after={"period": period, "start": start_date, "end": end_date, "insights": len(row.insights)},
        result="ok",
        reason=f"{trigger} 生成{_PERIOD_LABEL.get(period, period)}",
        source=source,
    )
    _emit(
        log,
        "info",
        f"报告已生成：{row.id}（{period} {start_date}~{end_date}，{len(row.insights)} 条建议）",
        event=EventKind.REPORT_GENERATED,
        payload={
            "report_id": row.id,
            "period": period,
            "start": start_date,
            "end": end_date,
            "insights_count": len(row.insights),
        },
    )
    return row


class _NoDataError(Exception):
    """窗口里没有数据（**内部信号**：调度器据此记 ``skipped_no_data``）。"""


def _write_report_file(paths: StudioPaths, bundle: ReportBundle, summary_md: str) -> Path:
    """把 ``summary_md`` 落盘（``data/output/reports/``；**永久保留** · §6.7.5）。"""
    directory = paths.output_dir / REPORT_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{bundle.period}_{bundle.start_date}_{bundle.end_date}.md"
        path.write_text(summary_md, encoding="utf-8", newline=chr(10))
    except OSError as exc:
        raise StudioError(
            f"报告文件写不下去：{exc}",
            code=ErrorCode.CONFIG_INVALID,
            context={"dir": str(directory), "error": str(exc)},
            remediation="看这个目录的权限与磁盘剩余空间；报告文件是交付物的一部分，不能只留在库里",
        ) from exc
    return path


def apply_insight(
    *,
    connection: sqlite3.Connection,
    persona: PersonaStore,
    report_id: str,
    index: int,
    reason: str | None = None,
    actor: str = "user",
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ReportRow:
    """采纳第 ``index`` 条建议 ⇒ 写进人物偏好（**唯一正门**：``PersonaStore``）。

    为什么落在 ``persona.style_hint`` 而不是 ``content_directions.priority``
    ------------------------------------------------------------------
    ``content_directions`` 是**历史批次**："当时为什么这么做"的证据。改它的权重
    等于篡改历史，而且下一轮 Planner 会重新生成一个批次 —— 改上一批的 priority
    对下一轮**没有任何影响**，表现出来就是"我点了采纳，但选题一点没变"。
    那是最坏的一类假闭环：面板说生效了，链路里一个字都没变。

    ``style_hint`` 则是所有 Agent（含 Planner）**每次都会读到**的公共输入
    （见 ``agents/base.py`` 的 ``persona_block``），改它才是真的影响下一轮。

    幂等：已经采纳过的建议**再点一次什么都不做**（往 ``style_hint`` 里重复追加
    同一句话会让提示词越滚越长，而且看不出重复）。
    """
    moment = parse_iso(now) if now is not None else utc_now()
    repo = ReportRepo(connection)
    row = repo.require(report_id)
    insights = row.insights
    if index < 0 or index >= len(insights):
        raise StudioError(
            f"这份报告里没有第 {index} 条建议（共 {len(insights)} 条）",
            code=ErrorCode.REPORT_INVALID,
            context={"report_id": report_id, "index": index, "count": len(insights)},
            remediation="刷新报告详情；建议列表会随新报告变化",
        )
    target = dict(insights[index])
    if target.get("applied"):
        _emit(
            log,
            "info",
            f"这条建议早就采纳过了（{report_id}#{index}）—— 没有重复写人物偏好",
            payload={"report_id": report_id, "index": index},
        )
        return row
    statement = str(target.get("statement") or "")
    confidence = str(target.get("confidence") or "low")
    stamp = moment.astimezone(local_tz()).strftime("%Y-%m-%d")
    line = f"{_INSIGHT_PREFIX} {stamp}】{statement}（置信度 {confidence}）"
    current = _current_style_hint(persona)
    merged, dropped = _merge_style_hint(current, line)
    if merged != current:
        PersonaService(persona, audit=AuditRepo(connection)).update(
            changes={"style_hint": merged},
            reason=reason or f"采纳报告建议（{report_id}#{index}）",
            actor=actor,
            source=source,
        )
    target["applied"] = True
    target["applied_at"] = format_iso(moment)
    target["applied_by"] = actor
    updated = [*(insights[:index]), target, *(insights[index + 1 :])]
    row = repo.mark_applied(report_id, insights=updated)
    AuditRepo(connection).record(
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=None,
        action=AUDIT_APPLY,
        target_type="report",
        target_id=report_id,
        before={"style_hint": current},
        after={"style_hint": merged, "insight_index": index, "dropped": dropped},
        result="ok",
        reason=reason or f"采纳第 {index} 条建议：{statement[:120]}",
        source=source,
    )
    _emit(
        log,
        "info",
        f"已采纳报告建议（{report_id}#{index}）⇒ 写进人物偏好，下一轮 Planner 就会读到",
        payload={"report_id": report_id, "index": index, "dropped": dropped},
    )
    return row


def _current_style_hint(persona: PersonaStore) -> str:
    try:
        return persona.current().config.style_hint or ""
    except StudioError as exc:
        raise StudioError(
            f"读不到当前人物，没法写偏好：{exc.message}",
            code=ErrorCode.CONFIG_INVALID,
            context={"error": exc.message},
            remediation="先去人物库把激活人物修好（面板的「人物库」页能看是哪一条坏了）",
        ) from exc


def _merge_style_hint(existing: str, line: str, *, limit: int = STYLE_HINT_LIMIT) -> tuple[str, int]:
    """把一条结论并进 ``style_hint`` ⇒ ``(新文本, 丢掉的旧结论条数)``。

    只保留**最新的 ``limit`` 条**结论行：``style_hint`` 会进每一次 LLM 调用，
    无限追加的代价是提示词越来越长、越来越贵，而三个月前的结论早就被新数据推翻了。
    非结论行（用户自己写的风格要求）**一条都不丢**。
    """
    lines = [item for item in existing.splitlines() if item.strip()]
    kept: list[str] = []
    conclusions: list[str] = []
    for item in lines:
        if item.strip().startswith(_INSIGHT_PREFIX):
            conclusions.append(item)
        else:
            kept.append(item)
    conclusions.append(line)
    dropped = max(len(conclusions) - max(int(limit), 1), 0)
    conclusions = conclusions[dropped:]
    return "\n".join([*kept, *conclusions]), dropped


def export_report(*, connection: sqlite3.Connection, report_id: str, fmt: str = "md") -> tuple[str, str, str]:
    """导出报告 ⇒ ``(文件名, 正文, media_type)``（§04.6.5.2 的 ``?format=md|csv``）。"""
    row = ReportRepo(connection).require(report_id)
    base = f"{row.period}_{row.start_date}_{row.end_date}"
    if fmt == "md":
        return (f"{base}.md", row.summary_md, "text/markdown; charset=utf-8")
    if fmt == "csv":
        return (f"{base}.csv", _report_csv(row), "text/csv; charset=utf-8")
    raise StudioError(
        f"不支持的导出格式：{fmt!r}",
        code=ErrorCode.REPORT_INVALID,
        context={"format": str(fmt), "allowed": ["md", "csv"]},
        remediation="format 只能是 md 或 csv",
    )


def _report_csv(row: ReportRow) -> str:
    """把各分组展平成 CSV（维度 / 分组 / 条数 / 各项均值）。

    导出的口径与 ``data_json`` **同一份数据**：另算一遍必然有一天对不上，
    而"面板和 CSV 不一样"是最难解释的一类问题。
    """
    header = "dimension,key,n,views_median,views_avg,likes_avg,completion_avg"
    lines = [header]
    for dimension in DIMENSIONS:
        groups = row.data.get(dimension)
        if not isinstance(groups, list):
            continue
        for item in groups:
            if not isinstance(item, dict):
                continue
            lines.append(
                ",".join(
                    [
                        dimension,
                        _csv_cell(item.get("key")),
                        str(item.get("n", "")),
                        _csv_cell(item.get("views_median")),
                        _csv_cell(item.get("views_avg")),
                        _csv_cell(item.get("likes_avg")),
                        _csv_cell(item.get("completion_avg")),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(char in text for char in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


# ══════════════════════════════════════════════════════════════════════
# 周期调度（与发布计划共用 30s 那一拍 · §3.3.20）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ReportFireResult:
    """触发一条报告周期的结论。"""

    schedule_id: str
    result: str
    report_id: str | None = None
    next_run_at: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == RESULT_OK

    @property
    def failed(self) -> bool:
        return self.result.startswith("error:")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "result": self.result,
            "report_id": self.report_id,
            "next_run_at": self.next_run_at,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ReportTickReport:
    """一拍报告调度的汇总。"""

    #: 这一拍**补排期**的周期（``next_run_at`` 原本是 NULL，见 :meth:`_schedule_pending`）
    scheduled: tuple[str, ...] = ()
    generated: tuple[str, ...] = ()
    skipped_no_data: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def due(self) -> int:
        return len(self.generated) + len(self.skipped_no_data) + len(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled": list(self.scheduled),
            "due": self.due,
            "generated": list(self.generated),
            "skipped_no_data": list(self.skipped_no_data),
            "errors": list(self.errors),
        }


class ReportSchedulerService:
    """报告周期的一拍（**无状态**：状态全在 ``report_schedules`` 那一行上）。

    与 ``SchedulerService`` 分开而不是塞进它：那个服务认识 ``PublishConfig``、
    限频、发布池，而报告生成**完全不碰这些**（它只读库、写文件）。
    合成一个的代价是"报告生成要 import 发布那一整套" —— 而它们唯一的共同点是
    "都由 30s 那一拍驱动"，那是 ``app/recycle.py`` 的事。
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        log: LogSink | None = None,
    ) -> None:
        self._connection = connection
        self._paths = paths
        self._log = log
        self._repo = ReportScheduleRepo(connection)

    # ── 一拍 ────────────────────────────────────────────────────────

    def due(self, *, now: str, limit: int = REPORT_BATCH_LIMIT) -> tuple[ReportScheduleRow, ...]:
        """到点该生成的周期（走 ``idx_rsched_due``；``next_run_at IS NULL`` 取不到）。"""
        return self._repo.list_due(now=now, limit=limit)

    def tick(self, *, now: str | None = None, limit: int = REPORT_BATCH_LIMIT) -> ReportTickReport:
        """跑一轮（**逐条隔离**：一条炸了不影响其余）。"""
        moment = now or now_iso()
        scheduled = self._schedule_pending(now=moment, limit=limit)
        generated: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for row in self.due(now=moment, limit=limit):
            outcome = self.fire(row, now=moment)
            if outcome.ok:
                generated.append(row.id)
            elif outcome.result == RESULT_SKIPPED_NO_DATA:
                skipped.append(row.id)
            else:
                errors.append(row.id)
        report = ReportTickReport(
            scheduled=scheduled,
            generated=tuple(generated),
            skipped_no_data=tuple(skipped),
            errors=tuple(errors),
        )
        if report.due:
            # 只在真有到点的时留汇总（与发布调度同一条：每 30s 一行会把日志刷满）
            logger.info(
                "report.tick",
                due=report.due,
                generated=len(report.generated),
                skipped_no_data=len(report.skipped_no_data),
                errors=len(report.errors),
            )
        return report

    def _schedule_pending(self, *, now: str, limit: int) -> tuple[str, ...]:
        """给「启用但还没排期」的周期补上 ``next_run_at``（**陷阱 178**）。

        ``0006_seed.sql`` 里三条内置计划的 ``next_run_at`` 是 NULL，而
        ``next_run_at <= now`` 对 NULL 永远为假 —— 只写那个条件的话，
        出厂自带的周报与月报**一次都不会触发**，而且没有任何地方会报错
        （面板上那条计划看起来完全正常，只是永远不动）。

        补的是「下一期」而不是「立刻跑一次」：报告是定期产物，重启后突然
        生成一份上周的报告没有意义（下一拍就到点了）。
        """
        out: list[str] = []
        for row in self._repo.list_unscheduled(limit=limit):
            candidate = self._next(row, at=now)
            if candidate is None:
                self._disable(row, reason="排不出下一期（周期参数跑不出将来）")
                continue
            fresh = self._repo.set_enabled(row.id, enabled=True, next_run_at=candidate)
            out.append(fresh.id)
            emit_schedule(self._log, fresh, message=f"报告周期补排期：{fresh.id} -> {fresh.next_run_at}")
        return tuple(out)

    def fire(
        self, row: ReportScheduleRow, *, now: str | None = None, trigger: str = "scheduled"
    ) -> ReportFireResult:
        """触发一条周期（**不抛**：异常在这里变成 ``error:…`` 与 ``fail_streak``）。"""
        moment = now or now_iso()
        try:
            return self._fire(row, now=moment, trigger=trigger)
        except _NoDataError as exc:
            return self._settle(
                row,
                at=moment,
                result=RESULT_SKIPPED_NO_DATA,
                note=f"{exc} —— 没有数据不是故障，下一期照排",
            )
        except Exception as exc:
            return self._on_error(row, exc, now=moment)

    # ── 单条触发 ────────────────────────────────────────────────────

    def _fire(self, row: ReportScheduleRow, *, now: str, trigger: str) -> ReportFireResult:
        if not row.enabled:
            return self._settle(
                row,
                at=now,
                result=RESULT_SKIPPED_DISABLED,
                note="这条周期是停用的（正常不该被取到；说明有人在跑到点查询之后把它停了）",
            )
        moment = parse_iso(now)
        start_date, end_date = period_bounds(row.period, now=moment, lookback_days=row.lookback_days)
        report = generate_report(
            connection=self._connection,
            paths=self._paths,
            period=row.period,
            start_date=start_date,
            end_date=end_date,
            include=row.include,
            trigger=trigger,
            skip_when_empty=True,
            actor="system",
            source="auto",
            log=self._log,
            now=now,
        )
        return self._settle(
            row,
            at=now,
            result=RESULT_OK,
            report_id=report.id,
            note=f"{row.period} {start_date}~{end_date}",
        )

    def _settle(
        self,
        row: ReportScheduleRow,
        *,
        at: str,
        result: str,
        report_id: str | None = None,
        note: str | None = None,
    ) -> ReportFireResult:
        """记结论 + 排下一期 + 发事件（**一次触发的唯一出口**）。"""
        candidate = self._next(row, at=at)
        fresh = self._repo.record_result(
            row.id,
            at=at,
            result=result,
            next_run_at=candidate,
            report_id=report_id,
            failed=result.startswith("error:"),
        )
        if candidate is None and fresh.enabled:
            # 排不出下一期 ⇒ 留着 enabled=1 会让面板显示"启用中"却永远不再触发（骗人）。
            fresh = self._disable(fresh, reason="排不出下一期（周期参数已经跑不出将来）")
        emit_schedule(
            self._log,
            fresh,
            message=f"报告周期已排下一期：{fresh.id} -> {fresh.next_run_at or '不再触发'}",
        )
        if result != RESULT_OK:
            _emit(
                self._log,
                "info",
                f"报告周期空转（{result}）：{fresh.id}" + ("" if note is None else f" —— {note}"),
                payload={"schedule_id": fresh.id, "result": result, "note": note},
            )
        return ReportFireResult(
            schedule_id=fresh.id,
            result=result,
            report_id=report_id,
            next_run_at=fresh.next_run_at,
            note=note,
        )

    def _next(self, row: ReportScheduleRow, *, at: str) -> str | None:
        return next_run_at(
            period=row.period,
            now=parse_iso(at),
            weekday=row.weekday,
            day_of_month=row.day_of_month,
            at_time=row.at_time,
        )

    def _disable(self, row: ReportScheduleRow, *, reason: str) -> ReportScheduleRow:
        """自动停用（排不出下一期时）—— 写留痕，不然"它怎么自己停了"查不出来。"""
        fresh = self._repo.set_enabled(row.id, enabled=False, next_run_at=None)
        AuditRepo(self._connection).record(
            actor="system",
            action=AUDIT_SCHEDULE_UPDATE,
            target_type="schedule",
            target_id=fresh.id,
            before={"enabled": True},
            after={"enabled": False},
            result="ok",
            reason=reason,
            source="auto",
        )
        return fresh

    def _on_error(self, row: ReportScheduleRow, exc: Exception, *, now: str) -> ReportFireResult:
        """失败处置：记 ``error:…`` + ``fail_streak`` 加一；连败到阈值 ⇒ 告警。"""
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        result = f"error:{reason}"
        candidate = self._next(row, at=now)
        fresh = self._repo.record_result(row.id, at=now, result=result, next_run_at=candidate, failed=True)
        if candidate is None and fresh.enabled:
            fresh = self._disable(fresh, reason="触发失败且没有下一期")
        _emit(
            self._log,
            "error",
            f"报告周期触发失败：{row.id}（连续 {fresh.fail_streak} 次）—— {reason}",
            payload={
                "schedule_id": row.id,
                "fail_streak": fresh.fail_streak,
                "next_run_at": fresh.next_run_at,
                "error": reason,
            },
        )
        if fresh.fail_streak >= FAIL_STREAK_ALERT:
            message = f"报告周期连续 {fresh.fail_streak} 次触发失败：{fresh.id}"
            _emit(
                self._log,
                "error",
                message,
                payload={
                    "code": str(AlertCode.REPORT_FAILING),
                    "severity": "warn",
                    "message": message,
                    "hint": f"去发布面板的「报告」区块看这条周期（{reason}）",
                    "schedule_id": fresh.id,
                },
            )
        return ReportFireResult(schedule_id=row.id, result=result, next_run_at=fresh.next_run_at, note=reason)


# ══════════════════════════════════════════════════════════════════════
# 周期 CRUD（面板的四个写动作 · §04.6.5.2）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ReportScheduleSpec:
    """一份**校验过**的报告周期参数（CRUD 三个入口共用）。"""

    period: str
    weekday: int | None
    day_of_month: int | None
    at_time: str
    tz: str
    lookback_days: int
    include: tuple[str, ...]
    enabled: bool
    is_builtin: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "weekday": self.weekday,
            "day_of_month": self.day_of_month,
            "at_time": self.at_time,
            "tz": self.tz,
            "lookback_days": self.lookback_days,
            "include": list(self.include),
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
        }


def validate_spec(
    spec: Mapping[str, Any], *, existing: ReportScheduleRow | None = None
) -> ReportScheduleSpec:
    """校验并补齐一份周期参数（**部分字段**：没给的沿用 ``existing``）。

    ``lookback_days`` 的缺省跟着 **period 走**（daily 1 / weekly 7 / monthly 31）：
    用户把周报改成日报却没动回看天数时，留着 7 天会生成一份"日报" —— 名字变了、
    内容没变，而面板上两个字段都是"我设过的值"。
    """
    merged: dict[str, Any] = {
        "period": existing.period if existing is not None else None,
        "weekday": existing.weekday if existing is not None else None,
        "day_of_month": existing.day_of_month if existing is not None else None,
        "at_time": existing.at_time if existing is not None else DEFAULT_AT_TIME,
        "tz": existing.tz if existing is not None else DEFAULT_TZ,
        "lookback_days": existing.lookback_days if existing is not None else None,
        "include": list(existing.include) if existing is not None else None,
        "enabled": existing.enabled if existing is not None else True,
        "is_builtin": existing.is_builtin if existing is not None else False,
    }
    merged.update(dict(spec))
    period = merged.get("period")
    if period is None:
        raise StudioError(
            "新建报告周期必须给 period",
            code=ErrorCode.REPORT_INVALID,
            context={"period": None, "periods": list(PERIODS)},
            remediation="period 只能是 daily / weekly / monthly",
        )
    weekday, day_of_month = validate_period_fields(
        str(period), weekday=merged.get("weekday"), day_of_month=merged.get("day_of_month")
    )
    lookback_raw = merged.get("lookback_days")
    if lookback_raw is None:
        lookback_raw = DEFAULT_LOOKBACK_DAYS.get(str(period), 7)
    return ReportScheduleSpec(
        period=str(period),
        weekday=weekday,
        day_of_month=day_of_month,
        at_time=validate_at_time(merged.get("at_time")),
        tz=validate_tz(merged.get("tz")),
        lookback_days=validate_lookback(lookback_raw),
        include=validate_include(merged.get("include")),
        enabled=bool(merged.get("enabled", True)),
        is_builtin=bool(merged.get("is_builtin", False)),
    )


def create_schedule(
    *,
    connection: sqlite3.Connection,
    spec: Mapping[str, Any],
    actor: str = "user",
    actor_ref: str | None = None,
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ReportScheduleRow:
    """建一条周期（``next_run_at`` 当场算好落库 —— 陷阱 #30）。"""
    moment = parse_iso(now) if now is not None else utc_now()
    fields = validate_spec(spec)
    repo = ReportScheduleRepo(connection)
    _reject_duplicate_period(repo, period=fields.period, enabled=fields.enabled)
    schedule_id = new_ulid()
    candidate = _plan_next(schedule_id=schedule_id, fields=fields, now=moment)
    row = repo.create(
        schedule_id=schedule_id,
        period=fields.period,
        weekday=fields.weekday,
        day_of_month=fields.day_of_month,
        at_time=fields.at_time,
        tz=fields.tz,
        lookback_days=fields.lookback_days,
        include=fields.include,
        enabled=fields.enabled,
        is_builtin=fields.is_builtin,
        next_run_at=candidate,
        created_by=actor,
    )
    _audit(
        connection,
        action=AUDIT_SCHEDULE_CREATE,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason="新建报告周期",
        before=None,
        after=row.to_dict(),
        source=source,
    )
    emit_schedule(log, row, message=f"报告周期已建：{row.id} -> {row.next_run_at or '不排期'}")
    return row


def update_schedule(
    *,
    connection: sqlite3.Connection,
    schedule_id: str,
    patch: Mapping[str, Any],
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    source: str = "webui",
    log: LogSink | None = None,
    now: str | None = None,
) -> ReportScheduleRow:
    """改一条周期（**整行改写 + 重算 next_run_at 在同一条 UPDATE 里** · 陷阱 #33）。"""
    repo = ReportScheduleRepo(connection)
    before = repo.require(schedule_id)
    fields = validate_spec(patch, existing=before)
    _reject_duplicate_period(repo, period=fields.period, enabled=fields.enabled, exclude=schedule_id)
    moment = parse_iso(now) if now is not None else utc_now()
    candidate = _plan_next(schedule_id=schedule_id, fields=fields, now=moment)
    row = repo.update(
        schedule_id,
        period=fields.period,
        weekday=fields.weekday,
        day_of_month=fields.day_of_month,
        at_time=fields.at_time,
        tz=fields.tz,
        lookback_days=fields.lookback_days,
        include=fields.include,
        enabled=fields.enabled,
        next_run_at=candidate,
    )
    _audit(
        connection,
        action=AUDIT_SCHEDULE_UPDATE,
        row=row,
        actor=actor,
        actor_ref=actor_ref,
        reason=reason or "编辑报告周期",
        before=before.to_dict(),
        after=row.to_dict(),
        source=source,
    )
    emit_schedule(log, row, message=f"报告周期已改：{row.id} -> {row.next_run_at or '不排期'}")
    return row


def delete_schedule(
    *,
    connection: sqlite3.Connection,
    schedule_id: str,
    actor: str = "user",
    actor_ref: str | None = None,
    reason: str | None = None,
    source: str = "webui",
) -> bool:
    """删一条周期。

    ``is_builtin=1`` 的**不能删**（§3.3.20）：那是出厂就有的周报/月报，
    删掉之后"默认那两条去哪了"没人答得上来。要它别跑就**停用** ——
    停用是一条留痕的动作，删除不是。
    """
    repo = ReportScheduleRepo(connection)
    before = repo.get(schedule_id)
    if before is None:
        return False
    if before.is_builtin:
        raise StudioError(
            f"「{before.period}」是内置周期，不能删，只能停用",
            code=ErrorCode.REPORT_INVALID,
            context={"schedule_id": schedule_id, "period": before.period},
            remediation="把它停用（enabled=false）—— 停用会留痕，删除不会",
        )
    removed = repo.delete(schedule_id)
    if removed:
        _audit(
            connection,
            action=AUDIT_SCHEDULE_DELETE,
            row=before,
            actor=actor,
            actor_ref=actor_ref,
            reason=reason or "删除报告周期",
            before=before.to_dict(),
            after=None,
            source=source,
        )
    return removed


def run_now(
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    schedule_id: str,
    log: LogSink | None = None,
    now: str | None = None,
) -> ReportFireResult:
    """立刻生成一次（``trigger='manual'`` · §04.6.5.2 的 ``POST .../run_now``）。"""
    row = ReportScheduleRepo(connection).require(schedule_id)
    service = ReportSchedulerService(connection=connection, paths=paths, log=log)
    return service.fire(row, now=now, trigger="manual")


def _reject_duplicate_period(
    repo: ReportScheduleRepo, *, period: str, enabled: bool, exclude: str | None = None
) -> None:
    """同周期最多 1 个启用（部分唯一索引 ``idx_rsched_period`` 的友好版）。

    索引是最后一道防线（它会直接抛 ``IntegrityError``）；这里先查一次是为了
    给出**能照着做**的错误 —— "UNIQUE constraint failed: report_schedules.period"
    对用户来说等于没说。
    """
    if not enabled:
        return
    for row in repo.list_all():
        if row.enabled and row.period == period and row.id != exclude:
            raise StudioError(
                f"「{period}」已经有一条启用中的周期了（{row.id}）",
                code=ErrorCode.REPORT_INVALID,
                context={"period": period, "existing": row.id},
                remediation="先把它停用，或者改这条的周期；同一个周期只允许一条启用",
            )


def _plan_next(*, schedule_id: str, fields: ReportScheduleSpec, now: datetime) -> str | None:
    """算一条周期的 ``next_run_at``；停用的计划一律 ``None``（不排期）。"""
    if not fields.enabled:
        return None
    return next_run_at(
        period=fields.period,
        now=now,
        weekday=fields.weekday,
        day_of_month=fields.day_of_month,
        at_time=fields.at_time,
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    action: str,
    row: ReportScheduleRow,
    actor: str,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    actor_ref: str | None = None,
    source: str = "webui",
) -> None:
    AuditRepo(connection).record(
        actor=actor if actor in {"user", "system", "auto", "worker"} else "user",
        actor_ref=actor_ref,
        action=action,
        target_type="schedule",
        target_id=row.id,
        before=before,
        after=after,
        result="ok",
        reason=reason,
        source=source,
    )


def emit_schedule(log: LogSink | None, row: ReportScheduleRow, *, message: str) -> None:
    """发一条 ``report.schedule_updated``（面板据此把新时刻画上去）。"""
    _emit(
        log,
        "info",
        message,
        event=EventKind.REPORT_SCHEDULE_UPDATED,
        payload={
            "schedule_id": row.id,
            "period": row.period,
            "next_run_at": row.next_run_at,
            "enabled": row.enabled,
        },
    )


def _emit(
    log: LogSink | None,
    level: str,
    message: str,
    *,
    event: EventKind | None = None,
    payload: Mapping[str, Any],
) -> None:
    """写一条报告日志（``event`` 非空 ⇒ 额外扇出一条 WS 事件 · §04.4.4）。

    没有 ``log`` 出口时退回进程日志：``build_state`` 在最小家目录里也能起来，
    那时 ``LogService`` 可能不在手上 —— 而"报告做了什么"绝不该因此消失。
    """
    merged: dict[str, Any] = dict(payload)
    if event is not None:
        merged[EVENT_PAYLOAD_KEY] = event.value
    if log is not None:
        log(level=level, source=LOG_SOURCE, message=message, payload=merged)  # type: ignore[arg-type]
        return
    if level == "info":
        logger.info(message, **merged)
    elif level == "warn":
        logger.warning(message, **merged)
    else:
        logger.error(message, **merged)
