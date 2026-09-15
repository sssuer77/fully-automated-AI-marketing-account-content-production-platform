"""WS 协议契约的单元测试（T1.7 · §04.4）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from studio.core.errors import ErrorCode
from studio.ws.protocol import (
    CHANNELS,
    DEFAULT_MIN_LEVEL,
    KIND_POLICY,
    WS_PROTOCOL_VERSION,
    Channel,
    Envelope,
    EventKind,
    FrameType,
    WsSubscriptionError,
    level_at_least,
    parse_subscription,
    policy_for,
    rate_bucket,
)

# ── 通道 ────────────────────────────────────────────────────────────


def test_channel_count_is_eight() -> None:
    assert len(CHANNELS) == 8
    assert CHANNELS == ("tasks", "logs", "pools", "metrics", "system", "control", "topics", "publish")


def test_parse_defaults_to_all_channels_and_info_level() -> None:
    subscription = parse_subscription({})
    assert subscription.channels == frozenset(CHANNELS)
    assert subscription.min_level == DEFAULT_MIN_LEVEL
    assert subscription.since_id is None
    assert subscription.task_ids is None
    assert subscription.wants_logs is True


def test_parse_channel_subset_and_filters() -> None:
    subscription = parse_subscription(
        {"channels": "logs,system", "task_ids": "t1, t2", "min_level": "warn", "since_id": "42"}
    )
    assert subscription.channels == {"logs", "system"}
    assert subscription.task_ids == frozenset({"t1", "t2"})
    assert subscription.min_level == "warn"
    assert subscription.since_id == 42
    assert subscription.accepts_task("t1") is True
    assert subscription.accepts_task("t9") is False
    assert subscription.accepts_task(None) is True


def test_parse_rejects_unknown_channel() -> None:
    with pytest.raises(WsSubscriptionError) as excinfo:
        parse_subscription({"channels": "logs,telemetry"})
    assert excinfo.value.code is ErrorCode.WS_SUBSCRIPTION_INVALID
    assert "telemetry" in excinfo.value.message


@pytest.mark.parametrize("bad", ["verbose", "WARN", "warning"])
def test_parse_rejects_bad_min_level(bad: str) -> None:
    with pytest.raises(WsSubscriptionError):
        parse_subscription({"min_level": bad})


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_parse_rejects_bad_since_id(bad: str) -> None:
    with pytest.raises(WsSubscriptionError):
        parse_subscription({"since_id": bad})


def test_level_at_least_is_monotonic() -> None:
    assert level_at_least("error", "info") is True
    assert level_at_least("info", "info") is True
    assert level_at_least("debug", "info") is False
    assert level_at_least("unknown-level", "warn") is False  # 未知按 info 计


# ── 信封 ────────────────────────────────────────────────────────────


def test_envelope_round_trip() -> None:
    envelope = Envelope(
        type=FrameType.EVENT,
        channel=Channel.TASKS,
        seq=7,
        ts="2026-09-13T00:00:00.000Z",
        data={"kind": EventKind.TASK_UPDATED.value, "id": "t1"},
    )
    assert envelope.v == WS_PROTOCOL_VERSION
    assert '"seq":7' in envelope.to_json()


def test_envelope_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            {"type": FrameType.EVENT, "channel": Channel.TASKS, "seq": 1, "ts": "t", "data": {}, "extra": 1}
        )


def test_envelope_seq_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Envelope(type=FrameType.EVENT, channel=Channel.TASKS, seq=0, ts="t", data={})


def test_envelope_is_frozen() -> None:
    envelope = Envelope(type=FrameType.EVENT, channel=Channel.LOGS, seq=1, ts="t", data={})
    with pytest.raises(ValidationError):
        envelope.seq = 2


# ── 事件策略表 ──────────────────────────────────────────────────────


def test_every_event_kind_has_a_policy() -> None:
    missing = [kind.value for kind in EventKind if kind not in KIND_POLICY]
    assert missing == [], f"事件未登记推送策略：{missing}"


def test_policy_channels_are_valid() -> None:
    assert {policy.channel.value for policy in KIND_POLICY.values()} <= set(CHANNELS)


def test_alert_policy_is_never_merge_never_drop_never_rate_limited() -> None:
    policy = policy_for(EventKind.SYSTEM_ALERT.value)
    assert policy.never_merge is True
    assert policy.never_drop is True
    assert policy.rate_hz is None
    assert policy.channel is Channel.SYSTEM


def test_rate_limited_kinds_match_spec() -> None:
    assert policy_for(EventKind.TASK_UPDATED.value).rate_hz == 2.0
    assert policy_for(EventKind.TASK_PROGRESS_DETAIL.value).rate_hz == 2.0
    assert policy_for(EventKind.SENTENCE_UPDATED.value).rate_hz == 5.0
    assert policy_for(EventKind.LOG_APPENDED.value).rate_hz == 2.0
    assert policy_for(EventKind.POOL_STATS.value).rate_hz == 1.0
    assert policy_for(EventKind.METRICS_TICK.value).rate_hz == 0.2
    assert policy_for(EventKind.TASK_COMPLETED.value).rate_hz is None


def test_unknown_kind_falls_back_without_dropping() -> None:
    policy = policy_for("brand.new_kind")
    assert policy.rate_hz is None
    assert policy.never_drop is False


def test_rate_bucket_is_per_entity() -> None:
    first = rate_bucket(EventKind.TASK_UPDATED.value, {"id": "t1"})
    second = rate_bucket(EventKind.TASK_UPDATED.value, {"id": "t2"})
    assert first != second
    assert first == rate_bucket(EventKind.TASK_UPDATED.value, {"id": "t1"})
    # 不限流的事件没有桶
    assert rate_bucket(EventKind.TASK_COMPLETED.value, {"id": "t1"}) == ""
