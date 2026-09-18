"""契约：WS 协议与规格书逐字一致（T1.7 · §04.4）。

为什么值得一条静态检查
----------------------
WS 协议是**跨进程、跨语言**的接口：服务端在 Python，客户端在 TypeScript。任何一处
漂移（通道少一个、事件名换个写法、`seq` 语义变了）都不会在单元测试里报错，只会让
前端"少一个面板的数据"或者"永远在 resync"。所以这里的做法是**直接解析规格书**，
把"文档 vs 代码"的差异变成红灯。

同时锁住三条分层与安全不变式：
1. `ws/` 层**只读**（不出现任何 INSERT/UPDATE/DELETE）—— 这是"先落库再广播"的结构保证；
2. `app/` 层不直接写 `system_logs`（必须走 `LogService`）；
3. `system.alert` 的码表锁死为 §04.5.2 的 8 个值，且告警永不合并/永不丢弃。
"""

from __future__ import annotations

import re
from pathlib import Path

from studio.core.proto import AlertCode
from studio.ws.backpressure import DROP_ORDER
from studio.ws.protocol import (
    CHANNELS,
    COALESCE_WINDOW_SEC,
    KIND_POLICY,
    REPLAY_LIMIT,
    RING_CAPACITY,
    SNAPSHOT_LOGS,
    Channel,
    Envelope,
    EventKind,
    FrameType,
    policy_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "studio"
CONTRACTS_DOC = REPO_ROOT / "docs" / "spec" / "04-contracts.md"


def _spec() -> str:
    return CONTRACTS_DOC.read_text(encoding="utf-8")


# ── 通道 ────────────────────────────────────────────────────────────


def test_channels_match_spec_literal() -> None:
    """§04.4.1 的 `Channel = Literal[...]` 与代码里的枚举逐字一致。"""
    block = _spec().split("Channel = Literal[", 1)[1].split("]", 1)[0]
    documented = tuple(re.findall(r'"([a-z]+)"', block))
    assert documented == CHANNELS, f"规格书 {documented} ≠ 代码 {CHANNELS}"


def test_channel_count_is_eight() -> None:
    assert len(Channel) == 8


# ── 信封 ────────────────────────────────────────────────────────────


def test_envelope_fields_match_spec() -> None:
    assert set(Envelope.model_fields) == {"v", "type", "channel", "seq", "ts", "data"}


def test_frame_types_match_spec() -> None:
    line = next(item for item in _spec().splitlines() if "type: Literal[" in item and '"pong"' in item)
    parsed = {item.strip().strip('"') for item in line.split("[", 1)[1].split("]", 1)[0].split(",")}
    assert parsed == {item.value for item in FrameType}


# ── 事件表 ──────────────────────────────────────────────────────────


def _documented_kinds() -> set[str]:
    kinds: set[str] = set()
    for line in _spec().splitlines():
        match = re.match(r"^\|\s*[a-z]+\s*\|\s*`([a-z_]+(?:\.[a-z_]+)?)`\s*\|", line)
        if match and match.group(1) != "data.kind":
            kinds.add(match.group(1))
    return kinds


def test_event_kinds_match_spec_table() -> None:
    """§04.4.3 事件表里列的 `data.kind` 与 `EventKind` 完全一致（不多不少）。"""
    documented = _documented_kinds()
    assert documented, "没从 §04.4.3 解析到任何事件（表格格式变了？）"
    assert documented == {kind.value for kind in EventKind}


def test_every_kind_is_registered_in_policy_table() -> None:
    assert {kind.value for kind in EventKind} == {kind.value for kind in KIND_POLICY}


# ── 告警 ────────────────────────────────────────────────────────────


def test_alert_codes_match_spec() -> None:
    line = next(line for line in _spec().splitlines() if "system.alert.code" in line and "`DISK_LOW`" in line)
    documented = set(re.findall(r"`([A-Z_]+)`", line))
    assert documented == {code.value for code in AlertCode}
    assert len(AlertCode) == 9


def test_alert_is_never_merged_never_dropped_never_rate_limited() -> None:
    policy = policy_for(EventKind.SYSTEM_ALERT.value)
    assert policy.never_merge and policy.never_drop and policy.rate_hz is None
    assert policy.channel is Channel.SYSTEM


def test_drop_order_matches_spec() -> None:
    """§04.4.6："满则丢弃 `debug` → `info` → 合并同类"。"""
    assert DROP_ORDER == ("debug", "info")


# ── 背压与补发阈值 ──────────────────────────────────────────────────


def test_coalesce_window_matches_spec() -> None:
    match = re.search(r"合并窗口（coalescer）\s*\|\s*(\d+)\s*ms", _spec())
    assert match is not None
    assert int(match.group(1)) / 1000.0 == COALESCE_WINDOW_SEC


def test_ring_capacity_matches_spec() -> None:
    match = re.search(r"环形缓冲\s*\|\s*(\d+)\s*条/连接", _spec())
    assert match is not None
    assert int(match.group(1)) == RING_CAPACITY


def test_replay_limits_match_spec() -> None:
    limit = re.search(r"since_id`?\s*上限\s*(\d+)\s*条", _spec())
    snapshot = re.search(r"最近\s*(\d+)\s*条", _spec())
    assert limit is not None and snapshot is not None
    assert int(limit.group(1)) == REPLAY_LIMIT
    assert int(snapshot.group(1)) == SNAPSHOT_LOGS


def test_rate_limits_match_spec() -> None:
    line = next(line for line in _spec().splitlines() if "单任务限流" in line)
    assert "2 Hz" in line and "5 Hz" in line
    assert policy_for(EventKind.TASK_UPDATED.value).rate_hz == 2.0
    assert policy_for(EventKind.SENTENCE_UPDATED.value).rate_hz == 5.0


# ── 分层不变式 ──────────────────────────────────────────────────────


_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+\w+", re.IGNORECASE),
    re.compile(r"UPDATE\s+(?:OR\s+\w+\s+)?\w+\s+SET", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+\w+", re.IGNORECASE),
)


def test_ws_layer_never_writes_any_table() -> None:
    """`ws/` 只读 ⇒ "先落库再广播"是结构保证，而不是纪律（陷阱 #13）。"""
    offenders: list[str] = []
    for path in sorted((SRC_ROOT / "ws").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in _WRITE_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}: {match.group(0)}")
    assert not offenders, "ws 层出现了写库语句：\n" + "\n".join(offenders)


def test_app_layer_does_not_write_system_logs_directly() -> None:
    """应用层写日志必须走 `LogService.append()`（否则不会叫醒 Hub ⇒ 前端收不到）。"""
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in (SRC_ROOT / "app").rglob("*.py")
        if "INTO system_logs" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"app 层直接写 system_logs：{offenders}"


def test_log_service_is_the_only_services_writer() -> None:
    offenders = [
        path.name
        for path in (SRC_ROOT / "services").rglob("*.py")
        if path.name != "log_service.py" and "INTO system_logs" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"services 层越权写 system_logs：{offenders}"


def test_allowlist_is_not_vacuous() -> None:
    """反向断言：白名单文件被删/改名后，上面两条会变成永远通过。"""
    text = (SRC_ROOT / "services" / "log_service.py").read_text(encoding="utf-8")
    assert "INSERT INTO system_logs" in text
    assert "class LogService" in text
    hub = (SRC_ROOT / "ws" / "hub.py").read_text(encoding="utf-8")
    assert "SELECT" not in hub, "Hub 不该自己写 SQL（读日志走 LogReader 协议）"


def test_hub_does_not_import_db() -> None:
    """分层：`ws` 只依赖 `core` / `services`，不直接碰 `db`。"""
    offenders = [
        path.name
        for path in (SRC_ROOT / "ws").rglob("*.py")
        if re.search(r"^from studio\.db|^import studio\.db", path.read_text(encoding="utf-8"), re.M)
    ]
    assert not offenders, f"ws 层直接依赖 db：{offenders}"


def test_app_wires_log_service_into_hub() -> None:
    """`LogService` 必须挂上 `Hub.wake`，否则"落库后立刻推送"退化成"等下一拍"。"""
    text = (SRC_ROOT / "app" / "deps.py").read_text(encoding="utf-8")
    assert "logs.attach_waker(hub.wake)" in text
