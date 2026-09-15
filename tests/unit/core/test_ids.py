"""ID 生成与幂等键（§2.4 · T1.1）。"""

from __future__ import annotations

import re

from studio.core.ids import new_job_id, new_request_id, new_task_id, new_ulid, sha256_hex, stable_key

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_ulid_shape_and_sortability() -> None:
    first = new_ulid()
    second = new_ulid()
    assert _ULID_PATTERN.match(first)
    assert _ULID_PATTERN.match(second)
    assert first <= second


def test_id_helpers_share_shape() -> None:
    for factory in (new_task_id, new_job_id, new_request_id):
        assert _ULID_PATTERN.match(factory())


def test_ulids_are_unique() -> None:
    assert len({new_ulid() for _ in range(500)}) == 500


def test_sha256_hex_is_stable_and_order_sensitive() -> None:
    assert sha256_hex("a", "b") == sha256_hex("a", "b")
    assert sha256_hex("a", "b") != sha256_hex("b", "a")
    assert len(sha256_hex("a")) == 64


def test_stable_key_separator_prevents_collision() -> None:
    assert stable_key("ab", "c") != stable_key("a", "bc")
    assert stable_key("01J9Z", "publish", "douyin", "acc-1") == stable_key(
        "01J9Z", "publish", "douyin", "acc-1"
    )
