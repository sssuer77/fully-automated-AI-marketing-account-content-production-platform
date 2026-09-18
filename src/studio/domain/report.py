"""报告周期的时刻算术与置信度（T5.7 · §03.3.20 / §04.6.5.2 / §06.7）。

为什么与 ``domain/schedule.py`` 分家
----------------------------------
发布计划问的是「今天几点发」（窗口 / 抖动 / 间隔），报告计划问的是
「哪一天的九点出报告」（周几 / 每月几号）。两者只有 ``HH:MM`` 的解析是共用的
（:func:`~studio.domain.schedule.parse_hhmm`），硬塞进一个模块只会让
"改发布窗口"与"改报告周期"这两件事在同一个文件里互相绊。

报告区间由 ``lookback_days`` 一个字段决定
----------------------------------------
DDL 只给了 ``lookback_days``，而 §6.7.5 的文字写的是"周报生成上周 / 月报生成上月"。
两套口径**必须选一个**：自然月边界要靠日历算术，而 ``lookback_days`` 是
**用户可编辑**的那个字段 —— 用户把它改成 14 之后，"面板显示 14 天区间"与
"库里其实按自然月算"就分家了，而这种不一致只会表现为"报告里的数字对不上"。
所以：**区间 = 触发日的前一天往前推 ``lookback_days`` 天**，默认
daily=1 / weekly=7 / monthly=31（月报用 31 天而不是自然月，理由同上）。

置信度是硬规则
--------------
``n < 10`` ⇒ ``low``（**必须**在 UI 上标"样本不足"）；``10 ≤ n < 30`` ⇒ ``medium``；
``n ≥ 30`` ⇒ ``high``。这条线由 §06.7.3 定死，**不**允许各处自己算 ——
"面板说 high、导出的 md 说 low"是同一份数据两个结论，最难查的一类不一致。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Literal

from studio.core.clock import format_iso, local_tz
from studio.core.errors import ErrorCode, StudioError
from studio.domain.schedule import format_hhmm, parse_hhmm

__all__ = [
    "CONFIDENCE_HIGH_MIN",
    "CONFIDENCE_MEDIUM_MIN",
    "DEFAULT_AT_TIME",
    "DEFAULT_INCLUDE",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_TZ",
    "INCLUDE_KINDS",
    "MAX_LOOKBACK_DAYS",
    "PERIODS",
    "SUPPORTED_TZ",
    "ReportPeriod",
    "confidence_for",
    "next_run_at",
    "period_bounds",
    "validate_at_time",
    "validate_include",
    "validate_lookback",
    "validate_period_fields",
    "validate_tz",
]

ReportPeriod = Literal["daily", "weekly", "monthly"]

#: 三种周期（与 DDL 的 ``CHECK (period IN (...))`` 逐字一致）
PERIODS: Final[tuple[str, ...]] = ("daily", "weekly", "monthly")

#: 默认生成时刻（§6.7.5：周报周一 09:00）
DEFAULT_AT_TIME: Final[str] = "09:00"

#: 全站唯一时区（``core/clock.py`` 的 ``_LOCAL_TZ``）。一期**只认这一个** ——
#: 允许填别的时区会立刻产生"计划按纽约算、面板按上海显示"的静默偏差。
DEFAULT_TZ: Final[str] = "Asia/Shanghai"
SUPPORTED_TZ: Final[tuple[str, ...]] = ("Asia/Shanghai",)

#: 回看天数默认值（见模块注释：区间由这一个字段决定）
DEFAULT_LOOKBACK_DAYS: Final[Mapping[str, int]] = {"daily": 1, "weekly": 7, "monthly": 31}

#: 与 DDL 的 ``CHECK (lookback_days BETWEEN 1 AND 366)`` 同一个上限
MAX_LOOKBACK_DAYS: Final[int] = 366

#: 报告可含的区块（与 DDL 的 ``include_json`` 默认值逐字一致）
INCLUDE_KINDS: Final[tuple[str, ...]] = ("publish", "metrics", "topics", "quality", "cost", "errors")
DEFAULT_INCLUDE: Final[tuple[str, ...]] = ("publish", "metrics", "topics", "quality", "cost")

#: 置信度分档（§06.7.3 的硬规则，**一处定义**）
CONFIDENCE_MEDIUM_MIN: Final[int] = 10
CONFIDENCE_HIGH_MIN: Final[int] = 30


def confidence_for(samples: int) -> str:
    """样本量 -> ``low`` / ``medium`` / ``high``（§06.7.3）。"""
    count = max(int(samples), 0)
    if count >= CONFIDENCE_HIGH_MIN:
        return "high"
    if count >= CONFIDENCE_MEDIUM_MIN:
        return "medium"
    return "low"


def period_bounds(period: str, *, now: datetime, lookback_days: int | None = None) -> tuple[str, str]:
    """报告覆盖的**本地日期**区间 ``(start_date, end_date)``（含首尾）。

    区间右端是**触发日的前一天**：周一一早生成的报告不该把"今天"算进去 ——
    今天才刚开始，把它算进来会让同一份报告在 00:05 与 23:55 生成出两个数字。
    """
    days = validate_lookback(
        lookback_days if lookback_days is not None else DEFAULT_LOOKBACK_DAYS.get(period, 7)
    )
    end = now.astimezone(local_tz()).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return (start.isoformat(), end.isoformat())


def next_run_at(
    *,
    period: str,
    now: datetime,
    weekday: int | None = None,
    day_of_month: int | None = None,
    at_time: str = DEFAULT_AT_TIME,
) -> str | None:
    """下一次该生成报告的时刻（ISO UTC）；``None`` = 算不出来（见下）。

    从 ``now`` 起**逐日往后找第一个满足条件的本地日期**，再拼上 ``at_time``。
    逐日扫的上限是 400 天：``monthly`` 的 ``day_of_month ≤ 28``（DDL 的 CHECK），
    所以 62 天内必然命中；400 只是"宁可多扫也不写一个可能返回 None 的循环"。

    **不补跑**：停机三天后回来，日报不会一口气补三份 —— 它等下一个 ``at_time``。
    报告与发布不同：发布错过一次是真的少发一条作品，报告错过一次只是少一份汇总，
    而"重启后突然生成三份报告"会让人以为系统在乱跑。
    """
    moment = now.astimezone(local_tz())
    minutes = parse_hhmm(validate_at_time(at_time))
    hour, minute = divmod(minutes, 60)
    wanted_weekday, wanted_day = validate_period_fields(period, weekday=weekday, day_of_month=day_of_month)
    for offset in range(0, 400):
        day = moment.date() + timedelta(days=offset)
        if not _matches(period, day, weekday=wanted_weekday, day_of_month=wanted_day):
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=local_tz())
        if candidate > moment:
            return format_iso(candidate)
    return None


def _matches(period: str, day: date, *, weekday: int | None, day_of_month: int | None) -> bool:
    if period == "daily":
        return True
    if period == "weekly":
        return weekday is not None and day.weekday() == weekday
    if period == "monthly":
        return day_of_month is not None and day.day == day_of_month
    return False


def validate_period_fields(
    period: str, *, weekday: Any = None, day_of_month: Any = None
) -> tuple[int | None, int | None]:
    """校验周期与它的两个附属字段（与 DDL 的两条 CHECK 双保险）。

    ``weekly`` 必须有 ``weekday``；``monthly`` 必须有 ``day_of_month``；
    ``daily`` 两者都必须是 ``None``（留着一条"每月 1 日"在日报上，
    面板会显示一个永远不生效的设置 —— 用户会以为改它有用来回试）。
    """
    if period not in PERIODS:
        raise StudioError(
            f"不认识的报告周期：{period!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"period": str(period), "periods": list(PERIODS)},
            remediation="period 只能是 daily / weekly / monthly",
        )
    if period == "weekly":
        if weekday is None:
            raise StudioError(
                "weekly 报告必须给 weekday（0=周一）",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"period": period, "weekday": None},
                remediation="例如 weekday=0 表示每周一",
            )
        value = int(weekday)
        if not 0 <= value <= 6:
            raise StudioError(
                f"weekday 必须在 0-6 之间（0=周一），拿到的是 {value}",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"weekday": value},
                remediation="0=周一 … 6=周日（Python 的 weekday 口径）",
            )
        return (value, None)
    if period == "monthly":
        if day_of_month is None:
            raise StudioError(
                "monthly 报告必须给 day_of_month（1-28）",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"period": period, "day_of_month": None},
                remediation="例如 day_of_month=1 表示每月 1 日",
            )
        value = int(day_of_month)
        if not 1 <= value <= 28:
            raise StudioError(
                f"day_of_month 必须在 1-28 之间，拿到的是 {value}",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"day_of_month": value},
                remediation="上限是 28：29/30/31 在某些月份不存在，那会让计划整月不触发",
            )
        return (None, value)
    return (None, None)


def validate_at_time(value: Any) -> str:
    """``'09:00'`` 原样返回；非法 ⇒ 抛（**不落库**）。

    复用 :func:`~studio.domain.schedule.parse_hhmm` 的措辞（它已经把
    「不要写 18:0 / 6:00 PM / 25:00」写清楚了），但把错误码换成
    ``REPORT_INVALID``：这个接口报的是**报告周期**的参数错，回一个
    ``SCHEDULE_INVALID`` 会让排障的人去翻发布计划那张表。
    """
    text = str(value).strip()
    try:
        minutes = parse_hhmm(text)
    except StudioError as exc:
        raise StudioError(
            exc.message,
            code=ErrorCode.REPORT_INVALID,
            context=dict(exc.context),
            remediation=exc.remediation,
        ) from exc
    return format_hhmm(minutes)


def validate_tz(value: Any) -> str:
    """一期只认 ``Asia/Shanghai``（见 :data:`SUPPORTED_TZ`）。"""
    text = str(value).strip()
    if text not in SUPPORTED_TZ:
        raise StudioError(
            f"时区只支持 {'/'.join(SUPPORTED_TZ)}，拿到的是 {text!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"tz": text, "supported": list(SUPPORTED_TZ)},
            remediation="全站时间口径统一在上海时区；要别的时区得先改 core/clock.py 的展示口径",
        )
    return text


def validate_lookback(value: Any) -> int:
    """回看天数（1-366）。"""
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            f"lookback_days 必须是整数，拿到的是 {value!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"lookback_days": str(value)},
            remediation="例如 7（回看 7 天）",
        ) from exc
    if not 1 <= days <= MAX_LOOKBACK_DAYS:
        raise StudioError(
            f"lookback_days 必须在 1-{MAX_LOOKBACK_DAYS} 之间，拿到的是 {days}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"lookback_days": days, "max": MAX_LOOKBACK_DAYS},
            remediation="报告最多回看一年",
        )
    return days


def validate_include(value: Sequence[Any] | None) -> tuple[str, ...]:
    """报告区块清单；空清单 ⇒ 默认那五项（**不是**"什么都不生成"）。"""
    if value is None:
        return DEFAULT_INCLUDE
    items = tuple(str(item) for item in value)
    if not items:
        return DEFAULT_INCLUDE
    unknown = [item for item in items if item not in INCLUDE_KINDS]
    if unknown:
        raise StudioError(
            f"不认识的报告区块：{'、'.join(unknown)}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"include": list(items), "allowed": list(INCLUDE_KINDS)},
            remediation=f"可选项：{'、'.join(INCLUDE_KINDS)}",
        )
    return tuple(dict.fromkeys(items))
