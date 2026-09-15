"""时间唯一入口（全站禁用裸 ``datetime.now()``）。

约定
----
- **存储**：DB 内一律 UTC ISO-8601 **毫秒**精度（``YYYY-MM-DDTHH:MM:SS.mmmZ``）。
  必须与 DDL 的 ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` **逐字同格式** ——
  两种精度混写会让 SQL 里的字符串比较出现亚毫秒倒挂（T1.5 施工裁定 25）。
- **展示**：前端按 ``Asia/Shanghai`` 本地化（§02.4）。
- **命名**：文件名时间戳 ``yyyymmdd-HHMMSS``（§2.4 成片命名约定）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

__all__ = [
    "ISO_LENGTH",
    "epoch_ms",
    "file_stamp",
    "format_iso",
    "local_tz",
    "now_iso",
    "parse_iso",
    "utc_now",
]

_ISO_MS = "%Y-%m-%dT%H:%M:%S"

#: ``format_iso`` 输出的固定长度（``2026-09-13T06:30:22.123Z``）
ISO_LENGTH = 24
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def local_tz() -> ZoneInfo:
    """展示用本地时区（``Asia/Shanghai``，进程内单例）。"""
    return _LOCAL_TZ


def format_iso(dt: datetime) -> str:
    """序列化为 UTC ISO-8601 字符串（DB / API 唯一格式，**毫秒**）。

    毫秒不是"够用就行"：DDL 的默认值是 ``strftime('%f')``（= 毫秒），
    而队列的 ``not_before`` / ``lease_expires_at`` 要与 SQL 里的 ``now`` 比大小。
    若 Python 侧写微秒，``...22.123456Z`` 与 ``...22.123Z`` 的字典序会**倒挂**
    （``'Z' > '2'``），比较结果就不可信了。
    """
    moment = dt.astimezone(UTC)
    return f"{moment.strftime(_ISO_MS)}.{moment.microsecond // 1000:03d}Z"


def now_iso() -> str:
    """当前时间的 ISO 字符串。"""
    return format_iso(utc_now())


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601 字符串（兼容带 / 不带 ``Z``）。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def epoch_ms(dt: datetime | None = None) -> int:
    """毫秒级 Unix 时间戳（WS 推送 / 前端排序用）。"""
    return int((dt or utc_now()).timestamp() * 1000)


def file_stamp(dt: datetime | None = None) -> str:
    """文件名时间戳 ``yyyymmdd-HHMMSS``（§2.4：成片 / 封面命名）。"""
    return (dt or utc_now()).astimezone(_LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
