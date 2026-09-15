"""时间入口（§02.4 · T1.1）。"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from studio.core.clock import (
    ISO_LENGTH,
    epoch_ms,
    file_stamp,
    format_iso,
    local_tz,
    now_iso,
    parse_iso,
    utc_now,
)

_STAMP_PATTERN = re.compile(r"^\d{8}-\d{6}$")


def test_utc_now_is_aware_utc() -> None:
    moment = utc_now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_format_iso_is_zulu_milliseconds() -> None:
    """必须与 DDL 的 ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` 同格式（毫秒）。"""
    text = format_iso(datetime(2026, 9, 13, 6, 30, 22, 123456, tzinfo=UTC))
    assert text == "2026-09-13T06:30:22.123Z"
    assert len(text) == ISO_LENGTH


def test_format_iso_truncates_rather_than_rounds() -> None:
    """截断而非四舍五入：``999_999µs`` 不能进位成下一秒。"""
    text = format_iso(datetime(2026, 9, 13, 6, 30, 22, 999_999, tzinfo=UTC))
    assert text == "2026-09-13T06:30:22.999Z"


def test_format_iso_matches_sqlite_strftime_format() -> None:
    """与 SQLite 自己的 ``strftime`` 输出**逐字同格式**（字典序 = 时间序）。"""
    with sqlite3.connect(":memory:") as conn:
        sql_text = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    assert len(sql_text) == len(now_iso()) == ISO_LENGTH
    assert sql_text.endswith("Z")
    assert abs((parse_iso(now_iso()) - parse_iso(sql_text)).total_seconds()) < 1


def test_timestamps_sort_lexicographically_like_time() -> None:
    """跨来源（Python / SQL）写入的时间戳必须能直接比大小 —— 队列的 ``not_before`` 靠它。"""
    earlier = format_iso(datetime(2026, 9, 13, 6, 30, 22, 123_000, tzinfo=UTC))
    later = format_iso(datetime(2026, 9, 13, 6, 30, 22, 123_456, tzinfo=UTC))
    assert earlier == "2026-09-13T06:30:22.123Z"
    assert later == "2026-09-13T06:30:22.123Z"  # 同一毫秒内截断后相等 ⇒ 不会倒挂
    assert earlier <= later


def test_now_iso_round_trips() -> None:
    parsed = parse_iso(now_iso())
    assert parsed.tzinfo == UTC
    assert abs((utc_now() - parsed).total_seconds()) < 5


def test_parse_iso_accepts_naive_and_offset() -> None:
    assert parse_iso("2026-09-13T06:30:22").tzinfo == UTC
    shifted = parse_iso("2026-09-13T14:30:22+08:00")
    assert shifted.hour == 6


def test_file_stamp_uses_local_time_and_expected_shape() -> None:
    moment = datetime(2026, 9, 13, 6, 30, 22, tzinfo=UTC)
    assert file_stamp(moment) == "20260913-143022"
    assert _STAMP_PATTERN.match(file_stamp())


def test_local_tz_is_shanghai() -> None:
    assert local_tz().key == "Asia/Shanghai"


def test_epoch_ms_is_monotonic_enough() -> None:
    moment = datetime(2026, 9, 13, 6, 30, 22, tzinfo=UTC)
    assert epoch_ms(moment) == 1789281022000
    assert epoch_ms() > epoch_ms(datetime(2020, 1, 1, tzinfo=UTC))
