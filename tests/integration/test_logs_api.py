"""日志 REST 面集成测试（T4.9 验收 · §04.4.6 / §04.5.2）。

验收四条（todolist T4.9）
-------------------------
① **过滤组合生效**：级别 × 任务 × 来源 三者可叠加；
② **搜索**：`message` / `source` 的子串匹配，且 `%` `_` 是**字面量**（否则用户搜 "50%"
   会把整张表捞回来 —— 这是 LIKE 最经典的坑）；
③ **导出 NDJSON**：一行一条、可下载、受同一套过滤约束、能按 `since_id` 翻页；
④ `debug` 默认丢弃（不落库 ⇒ 任何过滤都看不到）。

为什么单独一个文件而不是塞进 `test_ws_replay.py`
-----------------------------------------------
那边测的是**推送**（WS），这边测的是**查询**（REST）。两条通道的失败模式完全不同：
推送丢的是"当下这一刻"，查询丢的是"历史那一段"。混在一个文件里，将来红一条
要先判断是哪种。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.core.proto import AlertCode
from studio.db import migrate
from studio.services.log_service import like_pattern
from studio.ws.hub import HubSettings

LOGS_URL = "/api/v1/logs"
EXPORT_URL = "/api/v1/logs/export"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


@pytest.fixture
def seeded(state: AppState) -> AppState:
    """一批可控日志：3 个来源 × 3 个级别 × 2 个任务。"""
    append = state.logs.append
    append(level="info", source="pipeline", message="task → drafting", task_id="t1")
    append(level="info", source="agent.writer", message="draft ok: 682 字", task_id="t1")
    append(level="warn", source="pool.voice", message="claimed job attempts=2/3", task_id="t1")
    append(level="error", source="render.ffmpeg", message="scene_001 failed rc=1", task_id="t2")
    append(level="info", source="pool.render", message="scene_001 done 6.92s", task_id="t2")
    append(level="fatal", source="system", message="disk_free=D:9.1GB 触发 DISK_LOW", task_id=None)
    append(level="info", source="publish.browser", message="upload douyin 62% (title filled)")
    return state


def _ndjson(response_text: str) -> list[dict[str, Any]]:
    lines = [line for line in response_text.split("\n") if line]
    return [json.loads(line) for line in lines]


# ══════════════════════════════════════════════════════════════════════
# ① 过滤组合
# ══════════════════════════════════════════════════════════════════════


def test_level_filter_is_a_floor_not_an_equality(client: TestClient, seeded: AppState) -> None:
    """`level=warn` ⇒ warn + error + fatal（下限语义，与 WS 的 `min_level` 一致）。"""
    del seeded
    payload = client.get(LOGS_URL, params={"level": "warn"}).json()
    levels = {row["level"] for row in payload["logs"]}
    assert levels == {"warn", "error", "fatal"}


def test_source_filter_matches_exactly(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(LOGS_URL, params={"source": "pool.voice"}).json()
    assert [row["source"] for row in payload["logs"]] == ["pool.voice"]


def test_task_filter_excludes_others(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(LOGS_URL, params={"task_id": "t2"}).json()
    assert {row["task_id"] for row in payload["logs"]} == {"t2"}


def test_filters_compose(client: TestClient, seeded: AppState) -> None:
    """三个过滤叠加 —— 组合生效才是 T4.9 的验收点。"""
    del seeded
    payload = client.get(LOGS_URL, params={"level": "info", "task_id": "t2", "source": "pool.render"}).json()
    rows = payload["logs"]
    assert len(rows) == 1
    assert rows[0]["message"].startswith("scene_001 done")


def test_filters_that_match_nothing_return_empty_not_error(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(LOGS_URL, params={"source": "nope"}).json()
    assert payload["logs"] == []
    # 空页必须**回传请求里的 since_id**，否则前端游标会被清成 None（见 LogPage 文档串）。
    assert payload["next_since_id"] is None


# ══════════════════════════════════════════════════════════════════════
# ② 搜索（含 LIKE 转义）
# ══════════════════════════════════════════════════════════════════════


def test_search_matches_message_substring(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(LOGS_URL, params={"search": "scene_001"}).json()
    assert len(payload["logs"]) == 2


def test_search_also_matches_source(client: TestClient, seeded: AppState) -> None:
    """搜 "render" 要能捞到 render.ffmpeg / pool.render —— 只搜 message 会全漏掉。"""
    del seeded
    payload = client.get(LOGS_URL, params={"search": "render"}).json()
    assert {row["source"] for row in payload["logs"]} == {"render.ffmpeg", "pool.render"}


def test_search_percent_is_literal_not_wildcard(client: TestClient, state: AppState) -> None:
    """`%` 必须当字面量：否则 `search=%` 会命中**每一行**（LIKE 通配符）。"""
    state.logs.append(level="info", source="a", message="命中 50% 的样本")
    state.logs.append(level="info", source="a", message="完全不相干的另一条")

    payload = client.get(LOGS_URL, params={"search": "50%"}).json()
    assert [row["message"] for row in payload["logs"]] == ["命中 50% 的样本"]


def test_search_underscore_is_literal_not_wildcard(client: TestClient, state: AppState) -> None:
    state.logs.append(level="info", source="a", message="voice_master.wav 已生成")
    state.logs.append(level="info", source="a", message="voiceXmaster.wav 不该命中")

    payload = client.get(LOGS_URL, params={"search": "voice_master"}).json()
    assert [row["message"] for row in payload["logs"]] == ["voice_master.wav 已生成"]


def test_like_pattern_escapes_backslash_first() -> None:
    """顺序陷阱：先转义 `\\` 再转义通配符，否则会把刚加的转义符又转一遍。"""
    assert like_pattern("a%b") == "%a\\%b%"
    assert like_pattern("a_b") == "%a\\_b%"
    assert like_pattern("C:\\tmp") == "%C:\\\\tmp%"


# ══════════════════════════════════════════════════════════════════════
# ③ 导出 NDJSON
# ══════════════════════════════════════════════════════════════════════


def test_export_is_ndjson_with_one_row_per_line(client: TestClient, seeded: AppState) -> None:
    del seeded
    response = client.get(EXPORT_URL)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.ndjson"')

    rows = _ndjson(response.text)
    assert len(rows) == 7
    # 每行必须是**独立** JSON —— 这是 NDJSON 的全部意义（可流式消费）。
    ids = [int(row["id"]) for row in rows]
    assert ids == sorted(ids)
    assert set(rows[0]) == {
        "id",
        "ts",
        "level",
        "source",
        "message",
        "task_id",
        "job_id",
        "stage",
        "unit_ref",
        "worker_id",
        "trace_id",
        "duration_ms",
        "seq_in_task",
        "payload",
    }


def test_export_respects_filters(client: TestClient, seeded: AppState) -> None:
    del seeded
    rows = _ndjson(client.get(EXPORT_URL, params={"level": "error", "source": "system"}).text)
    assert [row["level"] for row in rows] == ["fatal"]


def test_export_since_id_pages_forward(client: TestClient, seeded: AppState) -> None:
    """增量导出：从某条之后开始，且**升序**（方便 append 到已有的文件里）。"""
    del seeded
    all_rows = _ndjson(client.get(EXPORT_URL).text)
    cursor = all_rows[2]["id"]
    tail = _ndjson(client.get(EXPORT_URL, params={"since_id": cursor}).text)
    assert [row["id"] for row in tail] == [row["id"] for row in all_rows[3:]]


def test_export_until_id_stops_inclusive(client: TestClient, seeded: AppState) -> None:
    del seeded
    all_rows = _ndjson(client.get(EXPORT_URL).text)
    stop = all_rows[1]["id"]
    rows = _ndjson(client.get(EXPORT_URL, params={"until_id": stop}).text)
    assert [row["id"] for row in rows] == [row["id"] for row in all_rows[:2]]


def test_export_honours_limit(client: TestClient, seeded: AppState) -> None:
    del seeded
    rows = _ndjson(client.get(EXPORT_URL, params={"limit": 3}).text)
    assert len(rows) == 3


def test_export_caps_limit(client: TestClient, seeded: AppState) -> None:
    """超过上限 ⇒ 422，而不是默默按上限截断（静默截断会被当成"导出全了"）。"""
    del seeded
    assert client.get(EXPORT_URL, params={"limit": 200_001}).status_code == 422


def test_export_of_empty_result_is_empty_body(client: TestClient, state: AppState) -> None:
    assert client.get(EXPORT_URL).text == ""


# ══════════════════════════════════════════════════════════════════════
# ④ debug 默认丢弃 + 告警可见
# ══════════════════════════════════════════════════════════════════════


def test_debug_never_reaches_any_query(client: TestClient, state: AppState) -> None:
    """`debug` 不落库 ⇒ 不管怎么查都看不到（这是"默认丢弃"的落地方式）。"""
    assert state.logs.append(level="debug", source="noisy", message="不该出现") is None
    assert client.get(LOGS_URL, params={"level": "debug"}).json()["logs"] == []
    assert client.get(EXPORT_URL, params={"level": "debug"}).text == ""


def test_alert_is_queryable_by_its_code(client: TestClient, state: AppState) -> None:
    """告警行与普通日志同表 ⇒ 面板的"只看告警"可以直接按 `source` / 级别过滤。"""
    state.logs.alert(AlertCode.DISK_LOW, message="disk_free=D:9.1GB", hint="清理 data/tmp")
    payload = client.get(LOGS_URL, params={"level": "warn"}).json()
    rows = payload["logs"]
    assert len(rows) == 1
    assert rows[0]["payload"]["code"] == str(AlertCode.DISK_LOW)
    assert rows[0]["payload"]["hint"] == "清理 data/tmp"
