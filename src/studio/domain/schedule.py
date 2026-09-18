"""定时发布的时刻算术（T5.6 · §04.6.5.1 / §06.5.5）。

为什么这一层单独放（而不是塞进 ``scheduler_service``）
----------------------------------------------------
"下一次该在什么时候跑"是一个**纯函数**：给（``schedule_id``、模式、窗口、抖动、now），
出一个 ISO 时刻。它不碰库、不碰队列、不认识 ``PublishConfig`` —— 于是它可以在单测里
被钉死（验收 ①②），而调度循环只负责"读库 -> 问它 -> 建 job -> 写回"。

为什么窗口里要随机取时刻（而不是每天 18:00 准点）
------------------------------------------------
准点发布是**机器特征**（陷阱 #29）：一个账号连着 30 天都在 18:00:00 发出作品，
这件事本身比内容更容易被认出来。随机取时刻 + 抖动让它看起来像人在发。

为什么随机必须是**确定性**的（HMAC 而不是 ``random``）
-----------------------------------------------------
``next_run_at`` 是**落库**的（陷阱 #30），但"重算一次"随时会发生（编辑策略、排下一期）。
如果取时刻用 ``random``，同一天重算一次就得到另一个时刻 —— 表现出来是"我改了下抖动，
今天的发布时刻就跳了 40 分钟"，而且没有任何地方报错。改用 ``HMAC(schedule_id, 日期)``
派生：同一天怎么算都是同一个时刻，换一天自动换一个。

抖动的边界
----------
抖动是"同一天里再挪一点"，而**窗口是硬边界**：``18:00-21:30`` 的计划绝不会因为抖动
跑到 17:45 或 21:44 去（越界就夹到边界）。这一条对 `at_time` 之外的两种模式都成立。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date, datetime, time, timedelta
from typing import Final, Literal

from studio.core.clock import format_iso, local_tz, parse_iso
from studio.core.errors import ErrorCode, StudioError

__all__ = [
    "DEFAULT_JITTER_MIN",
    "DEFAULT_WINDOW",
    "MAX_INTERVAL_HOURS",
    "MAX_JITTER_MIN",
    "MODES",
    "ScheduleMode",
    "format_hhmm",
    "next_run_at",
    "parse_at_time",
    "parse_hhmm",
    "validate_jitter",
    "validate_window",
    "window_moment",
]

ScheduleMode = Literal["at_time", "daily_window", "interval"]

#: 三种模式（与 DDL 的 ``CHECK (mode IN (...))`` 逐字一致）
MODES: Final[tuple[str, ...]] = ("at_time", "daily_window", "interval")

#: Q14 的默认窗口与抖动。**是默认值不是硬编码**：创建计划时不传就落到这里，
#: 传了就按传的来（面板上两项都可编辑）。
DEFAULT_WINDOW: Final[tuple[str, str]] = ("18:00", "21:30")
DEFAULT_JITTER_MIN: Final[int] = 15

#: 与 DDL 的 ``CHECK (jitter_min BETWEEN 0 AND 120)`` 同一个上限。
MAX_JITTER_MIN: Final[int] = 120

#: 间隔模式的上限（30 天）。再长就不叫"定时发布"了，而一个手滑输进去的
#: ``999999`` 会让这个计划这辈子都不再触发 —— 那是最难发现的一类静默失败。
MAX_INTERVAL_HOURS: Final[int] = 24 * 30

_HHMM: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> int:
    """``'18:00'`` -> ``1080``（自午夜起的分钟数）。

    只认 24 小时制的 ``HH:MM``：``'18:0'`` / ``'6:00 PM'`` / ``'25:00'`` 全部拒绝。
    宽松解析在这里的代价是"我写 6:00 想的是早上六点，它当成了 06:00 还是 18:00"——
    这种歧义一旦进了库就再也查不清了。
    """
    match = _HHMM.match(str(value).strip())
    if match is None:
        raise StudioError(
            f"时刻必须写成 24 小时制的 HH:MM，拿到的是 {value!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"value": str(value)},
            remediation="例如 18:00 / 21:30；不要写 18:0、6:00 PM 或 25:00",
        )
    return int(match.group(1)) * 60 + int(match.group(2))


def format_hhmm(minutes: int) -> str:
    """分钟数 -> ``'HH:MM'``（``parse_hhmm`` 的逆）。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_window(window: tuple[str, str] | list[str] | None) -> tuple[int, int]:
    """校验窗口并返回 ``(start_min, end_min)``；非法即抛（**不落库**）。

    ``start < end`` 是硬要求：跨午夜的窗口（``22:00-02:00``）在一期不支持 ——
    "今天还是明天"这个歧义会渗进 `next_run_at`、面板显示与顺延三处，
    而它换来的只是"深夜档"这一种排期。真要深夜档，写两个窗口就行。
    """
    if window is None:
        raise StudioError(
            "窗口模式必须给 window（开始与结束时刻）",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"window": None},
            remediation="例如 window=('18:00','21:30')",
        )
    start_text, end_text = (str(window[0]), str(window[1]))
    start = parse_hhmm(start_text)
    end = parse_hhmm(end_text)
    if start >= end:
        raise StudioError(
            f"窗口的开始必须早于结束：{start_text} -> {end_text}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"window": [start_text, end_text]},
            remediation="写成 18:00 -> 21:30；跨午夜的窗口一期不支持，拆成两个计划即可",
        )
    return start, end


def validate_jitter(value: int) -> int:
    """抖动必须是 ``[0, 120]`` 的整数分钟。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            f"抖动必须是整数分钟，拿到的是 {value!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"jitter_min": str(value)},
            remediation="例如 15（默认）；0 表示不抖动",
        ) from exc
    if not 0 <= minutes <= MAX_JITTER_MIN:
        raise StudioError(
            f"抖动必须在 0-{MAX_JITTER_MIN} 分钟之间，拿到的是 {minutes}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"jitter_min": minutes, "max": MAX_JITTER_MIN},
            remediation="把它改到 0-120；抖动比窗口还大就不再是抖动了",
        )
    return minutes


def parse_at_time(value: str) -> datetime:
    """解析 ``at_time``（**必须带时区**），返回 UTC 时刻。

    不带时区的 ``2026-09-14T19:30:00`` 是**拒绝**的，不是"当成本地时间"：
    同一个字符串在开发机与服务器上会指到两个不同的瞬间，而库里存的是 UTC ——
    排期错了只会表现为"它没在我说的时间发"，排查时没有任何线索指向这里。
    """
    text = str(value).strip()
    if not text:
        raise StudioError(
            "at_time 模式必须给一个时刻",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"at_time": text},
            remediation="例如 2026-09-14T19:30:00+08:00",
        )
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise StudioError(
            f"at_time 不是合法的 ISO-8601 时刻：{text!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"at_time": text},
            remediation="例如 2026-09-14T19:30:00+08:00（要带时区）",
        ) from exc
    if moment.tzinfo is None:
        raise StudioError(
            f"at_time 必须带时区：{text!r}",
            code=ErrorCode.SCHEDULE_INVALID,
            context={"at_time": text},
            remediation="补上 +08:00（或 Z）；不带时区的话同一个字符串在两台机器上是两个瞬间",
        )
    return parse_iso(text)


def _derive(schedule_id: str, day: date, salt: str) -> int:
    """``HMAC(schedule_id, 'salt|日期')`` 的前 64 位（确定性随机的唯一来源）。"""
    message = f"{salt}|{day.isoformat()}".encode()
    digest = hmac.new(schedule_id.encode("utf-8"), message, hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")


def window_moment(
    *,
    schedule_id: str,
    day: date,
    window: tuple[str, str] | list[str],
    jitter_min: int = DEFAULT_JITTER_MIN,
) -> datetime:
    """某个**本地日期**上的发布时刻（确定性：同 schedule + 同一天永远同一个时刻）。

    这是验收 ② 的那一条，也是"重启不漂移"的全部实现：没有任何内存状态，
    库里的 ``next_run_at`` 丢掉之后重算一次，得到的还是同一个时刻。
    """
    start, end = validate_window(window)
    jitter = validate_jitter(jitter_min)
    span = end - start
    base = start + (_derive(schedule_id, day, "base") % span)
    # 抖动为 0 时**不掷第二次骰子**：否则"±0 分钟"会因为取了另一个随机数而挪位。
    offset = (_derive(schedule_id, day, "jitter") % (2 * jitter + 1)) - jitter if jitter else 0
    minute = min(max(base + offset, start), end)
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=local_tz())


def next_run_at(
    *,
    schedule_id: str,
    mode: str,
    now: datetime,
    at_time: str | None = None,
    window: tuple[str, str] | list[str] | None = None,
    interval_hours: int | None = None,
    jitter_min: int = DEFAULT_JITTER_MIN,
    last_run_at: str | None = None,
) -> str | None:
    """下一次该跑的时刻（ISO UTC）；``None`` = 再也不跑了（``at_time`` 已过）。

    三种模式各自的"下一次"
    ----------------------
    - ``daily_window``：今天的窗口内那个时刻；**已经过了就取明天**的（窗口是每日的）；
    - ``interval``：``last_run_at + interval_hours``；从没跑过就相对 ``now`` 算。
      停机错过的那几期**只补一次**（下一次 = 现在），不会攒出一串连发；
    - ``at_time``：就是那个时刻；已经过了 ⇒ ``None``（一次性计划，跑完即停）。
    """
    moment_now = now.astimezone(local_tz())
    if mode == "daily_window":
        chosen = window_moment(
            schedule_id=schedule_id,
            day=moment_now.date(),
            window=window,  # type: ignore[arg-type]
            jitter_min=jitter_min,
        )
        if chosen <= moment_now:
            chosen = window_moment(
                schedule_id=schedule_id,
                day=moment_now.date() + timedelta(days=1),
                window=window,  # type: ignore[arg-type]
                jitter_min=jitter_min,
            )
        return format_iso(chosen)

    if mode == "at_time":
        if at_time is None:
            raise StudioError(
                "at_time 模式必须给 at_time",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"mode": mode},
                remediation="例如 2026-09-14T19:30:00+08:00",
            )
        target = parse_at_time(at_time)
        return None if target <= now else format_iso(target)

    if mode == "interval":
        if interval_hours is None:
            raise StudioError(
                "interval 模式必须给 interval_hours",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"mode": mode},
                remediation="例如 6（每 6 小时一次）",
            )
        hours = int(interval_hours)
        if not 1 <= hours <= MAX_INTERVAL_HOURS:
            raise StudioError(
                f"间隔必须在 1-{MAX_INTERVAL_HOURS} 小时之间，拿到的是 {hours}",
                code=ErrorCode.SCHEDULE_INVALID,
                context={"interval_hours": hours, "max": MAX_INTERVAL_HOURS},
                remediation="把它改到 1-720 小时",
            )
        base = parse_iso(last_run_at) if last_run_at else now
        candidate = base + timedelta(hours=hours)
        candidate = max(now, candidate)
        return format_iso(candidate)

    raise StudioError(
        f"不认识的调度模式：{mode!r}",
        code=ErrorCode.SCHEDULE_INVALID,
        context={"mode": str(mode), "modes": list(MODES)},
        remediation="mode 只能是 at_time / daily_window / interval",
    )
