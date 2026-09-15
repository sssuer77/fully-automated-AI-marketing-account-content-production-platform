"""选题面板 REST 面集成测试（T4.3 验收 · §04.4.5 第 2 行）。

验收八条（todolist T4.3）
-------------------------
① 瀑布流按 ``score DESC, seq``（高分在前，面板不重排）；
② 方向卡片带历史批次下拉 + 每方向选题计数；
③ ``analyze`` ⇒ 方向；``ideate`` ⇒ 选题，**逐方向失败只影响自己**；
④ 长任务**单飞**：已在跑 ⇒ 409 ``TOPIC_BATCH_RUNNING``（不是静默排队）；
⑤ 勾选入队**逐条**建任务，一条失败不影响其余，幂等键 ``topic:<id>``；
⑥ ``draft_now`` 才跑写稿（默认 false ⇒ 入队只是入队）；
⑦ 人工加选题直接入库 + ``audit_ops``，相似**只提示不拦**；
⑧ 热点两种入口（扫盘 / 网页粘贴）都落库，网页粘贴的文件名由**服务端**生成。

为什么只有 LLM 是脚本化的
-------------------------
``build_gateway`` 被换成一个"注入假传输、其余照旧"的包装：schema 目录、提示词库、
预算闸门、熔断、记账全走**生产那套**。用假网关测不出契约漂移（比如 planner 的
输出 schema 改了而提示词没跟上），而那正是这一层最容易腐烂的地方。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.agents.fakes import Reply, ScriptedTransport, persona

from studio.agents import gateway_factory
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.app import deps
from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.app.routers import topics as topics_router
from studio.core.paths import StudioPaths
from studio.core.persona_store import reset_persona_store
from studio.db import migrate
from studio.db.repositories import AuditRepo, DirectionRepo, TopicRepo
from studio.domain.text import count_chars
from studio.services import InputService
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

TOPICS_URL = "/api/v1/topics"
DIRECTIONS_URL = "/api/v1/topics/directions"
ANALYZE_URL = "/api/v1/topics/analyze"
IDEATE_URL = "/api/v1/topics/ideate"
SELECT_URL = "/api/v1/topics/select"
MANUAL_URL = "/api/v1/topics/manual"
HOT_IMPORT_URL = "/api/v1/hot/import"
HOT_SUBMIT_URL = "/api/v1/hot/submit"

#: 一条合法热点行（``标题|热度|平台``）
HOT_LINE = "MC 跑酷新版本|爆|douyin"

#: 成稿正文开头（10 字，**同时命中两个口癖**）
CATCH_LINE = "这不科学，俺寻思也是。"

#: 一句干净的口播（13 字 ≤ 单句上限 28）
LINE = "这不科学，熊大又跑起来了。"

# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ 提示词与 schema 用的是**生产那两份**，数据落 tmp。"""
    return StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppState]:
    # `active_persona()` 走的是进程级单例（按 `STUDIO_HOME` 推断仓库根）。
    # 显式钉住 + 用完复位：别的用例把单例指到 tmp 家目录时，这里不该被带偏。
    monkeypatch.setenv("STUDIO_HOME", str(REPO_ROOT))
    reset_persona_store()
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
    try:
        yield built
    finally:
        built.close()
        reset_persona_store()


@pytest.fixture
def connection(state: AppState) -> sqlite3.Connection:
    return state.connections.get()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


def arm(monkeypatch: pytest.MonkeyPatch, *replies: str) -> ScriptedTransport:
    """把 LLM 传输换成脚本（其余装配照旧：真 schema / 真提示词 / 真预算闸门）。

    原函数刻意取 ``gateway_factory`` 那一份而不是 ``deps.build_gateway``：后者在第一
    次 ``arm`` 之后已经是我们自己换上去的假件，再包一层会把新脚本吃掉、继续用旧的
    传输（一个用例里换两次脚本就会踩到）。
    """
    transport = ScriptedTransport(replies=[Reply(text=item) for item in replies])
    real = gateway_factory.build_gateway

    def patched(**kwargs: Any) -> LlmGateway:
        return real(
            **{
                **kwargs,
                "transport": transport,
                "settings": GatewaySettings(backoff_base_sec=0.0),
                "env": {"STUDIO_LLM_API_KEY": "test-key"},
            }
        )

    monkeypatch.setattr(deps, "build_gateway", patched)
    return transport


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def seed_hot(paths: StudioPaths, connection: sqlite3.Connection, text: str = HOT_LINE) -> str:
    """写一个热点文件并导入一次（幂等 ⇒ ``analyze`` 再导一次不会重复）。"""
    paths.hot_dir.mkdir(parents=True, exist_ok=True)
    (paths.hot_dir / "0913.md").write_text(f"{text}\n", encoding="utf-8")
    InputService(connection, paths=paths).import_hot()
    rows = connection.execute("SELECT id FROM hot_items ORDER BY line_no").fetchall()
    return str(rows[0]["id"])


def planner_reply(hot_id: str | None = None, count: int = 5) -> str:
    """``count`` 个方向（schema 要 5–8 个）：grounded、fit 够、priority 100。

    ``hot_id=None`` ⇒ 只 grounded 在 persona 上（用于"热点已被上一批消费掉"的第二批）。
    """
    grounded = [{"type": "persona", "quote": "账号定位"}]
    if hot_id is not None:
        grounded.append({"type": "hot", "ref_id": hot_id, "quote": "MC 跑酷新版本"})
    return json.dumps(
        {
            "directions": [
                {
                    "title": f"方向{index}",
                    "rationale": f"理由{index}",
                    "grounded_on": grounded,
                    "priority": 100,
                    "risk_flags": [],
                    "fit_score": 8,
                }
                for index in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


#: 5 个方向各 3 条（**必须互不相同**：同一批里重复的会被去重丢掉）
TITLE_SETS: tuple[tuple[str, ...], ...] = (
    ("熊大跑酷翻车现场", "MC冷知识盘点", "跑酷地图推荐"),
    ("MC跑酷7个技巧", "生存模式第一天", "红石机关教程"),
    ("联机对战实录", "极限跳跃挑战", "隐藏关卡探秘"),
    ("萌新入坑指南", "老玩家回归感想", "地图作者访谈"),
    ("手机版操作技巧", "键鼠手感对比", "帧数优化设置"),
)

#: 每方向产出几条（``IdeateBody.per_direction``，schema 允许 1–10）
PER_DIRECTION = 3


def ideator_reply(*titles: str) -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": title,
                    "hook_type": "suspense",
                    "angle": f"{title} 的差异化角度",
                    "exec_feasible": True,
                    "score": 8.0,
                    "reason": "钩子够硬",
                }
                for title in titles
            ]
        },
        ensure_ascii=False,
    )


def body(chars: int = 700) -> str:
    """**恰好** ``chars`` 字的正文（含两个口癖）。"""
    return CATCH_LINE + "熊" * (chars - count_chars(CATCH_LINE))


def director_reply() -> str:
    return json.dumps(
        {
            "hook_3s": "熊大这一跳把我看傻了",
            "segments": [
                {
                    "seq": index,
                    "point": f"第{index}个要点",
                    "visual": f"第{index}段画面建议",
                    "mood": "兴奋",
                    "est_chars": 200,
                }
                for index in range(1, 4)
            ],
            "cta": "点个关注，下期更狠",
            "est_duration_ms": 140_000,
        },
        ensure_ascii=False,
    )


def writer_reply() -> str:
    return json.dumps(
        {
            "title": "MC跑酷最难的一跳",
            "hook": "熊大又整活了",
            "body_md": body(),
            "cta": "点个关注看下集",
            "sentences": [
                {"seq": index, "text": LINE, "speaker": "bigbear" if index % 2 else "littlebear"}
                for index in range(1, 21)
            ],
            "est_duration_ms": 170_000,
            "catchphrases_used": ["这不科学", "俺寻思"],
        },
        ensure_ascii=False,
    )


def declared_events(connection: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    """日志 ``payload`` 里声明了某个事件名的行（Hub 就是靠它扇出 WS 事件的）。"""
    rows = connection.execute(
        "SELECT payload_json FROM system_logs WHERE payload_json LIKE ? ORDER BY id",
        (f'%"{kind}"%',),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def run_planner_and_ideator(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    paths: StudioPaths,
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """跑通 ``analyze`` + ``ideate``，返回方向列表（多条用例的前半段都一样）。

    脚本化回应**按方向逐条给**：``ScriptedTransport`` 用尽后会复用最后一条，
    那样 5 个方向会拿到同一批标题 —— 而被去重丢掉 4 份，池子只剩 3 条。
    """
    hot_id = seed_hot(paths, connection)
    arm(
        monkeypatch,
        planner_reply(hot_id),
        *[ideator_reply(*titles) for titles in TITLE_SETS],
    )

    analyzed = client.post(ANALYZE_URL, json={"import_sources": True})
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["ok"] is True

    directions = client.get(DIRECTIONS_URL).json()["directions"]
    assert len(directions) == len(TITLE_SETS)

    ideated = client.post(IDEATE_URL, json={"per_direction": PER_DIRECTION})
    assert ideated.status_code == 200, ideated.text
    assert ideated.json()["ok"] is True, ideated.text
    return list(directions)


# ══════════════════════════════════════════════════════════════════════
# ① ② ③ 读与生成
# ══════════════════════════════════════════════════════════════════════


def test_analyze_and_ideate_fill_the_pool(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directions = run_planner_and_ideator(client, monkeypatch, paths, connection)

    pool = client.get(TOPICS_URL).json()
    assert pool["status"] == "candidate"
    assert len(pool["topics"]) == len(TITLE_SETS) * PER_DIRECTION
    assert pool["counts"]["candidate"] == len(TITLE_SETS) * PER_DIRECTION
    # ① 高分在前（同一批同分 ⇒ 按 seq 兜底）
    scores = [item["score"] for item in pool["topics"]]
    assert scores == sorted(scores, reverse=True)
    # ② 方向卡片带依据（面板要回答"为什么是这个方向"）
    assert directions[0]["grounded_on"]
    assert directions[0]["rationale"] == "理由1"


def test_directions_keep_history_and_counts(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_planner_and_ideator(client, monkeypatch, paths, connection)

    # 第二批：热点已被上一批消费掉 ⇒ 只 grounded 在 persona 上（不再要求"必须含热点依据"）
    arm(monkeypatch, planner_reply())
    assert client.post(ANALYZE_URL, json={"import_sources": True}).json()["ok"] is True

    latest = client.get(DIRECTIONS_URL).json()
    assert len(latest["batches"]) == 2
    assert latest["batch_id"] == latest["batches"][0]

    # 切回上一批：计数跟着走（"这个方向出了几条、选中几条"）
    older = client.get(DIRECTIONS_URL, params={"batch_id": latest["batches"][1]}).json()
    assert older["batch_id"] == latest["batches"][1]
    counted = {item["topic_count"] for item in older["directions"]}
    assert counted == {PER_DIRECTION}
    assert all(item["selected_count"] == 0 for item in older["directions"])


def test_topics_rejects_an_unknown_status_filter(client: TestClient) -> None:
    """状态过滤有 pattern：写错不是"返回空列表"，而是 422（省得人以为池子空了）。"""
    response = client.get(TOPICS_URL, params={"status": "whatever"})
    assert response.status_code == 422


def test_analyze_is_single_flight(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已在跑 ⇒ 409 + 说清"谁在跑"；**不排队**（排队会让前端挂住且不知道排第几）。"""
    hot_id = seed_hot(paths, connection)
    arm(monkeypatch, planner_reply(hot_id))

    with topics_router._RUN_GUARD.hold("方向分析"):
        blocked = client.post(ANALYZE_URL, json={"import_sources": False})

    assert blocked.status_code == 409
    payload = blocked.json()
    assert payload["code"] == "TOPIC_BATCH_RUNNING"
    assert "方向分析" in payload["message"]
    assert payload["remediation"]

    # 锁放开后照常能跑（守卫不是"一次失败就锁死"）
    assert client.post(ANALYZE_URL, json={"import_sources": False}).status_code == 200


# ══════════════════════════════════════════════════════════════════════
# ④ ⑤ ⑥ 勾选入队
# ══════════════════════════════════════════════════════════════════════


def test_select_enqueues_and_reports_per_item_failure(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]

    response = client.post(SELECT_URL, json={"topic_ids": [topic_id, "tp-does-not-exist"]})
    assert response.status_code == 200, response.text
    body_json = response.json()

    assert body_json["requested"] == 2
    assert [item["topic_id"] for item in body_json["selected"]] == [topic_id]
    assert body_json["selected"][0]["created_task"] is True
    assert body_json["selected"][0]["topic_status"] == "queued"
    # 一条失败不影响其余，且**逐条**带回 code 与补救建议
    assert len(body_json["failed"]) == 1
    assert body_json["failed"][0]["code"] == "TOPIC_NOT_FOUND"
    assert body_json["failed"][0]["remediation"]

    row = TopicRepo(connection).get(topic_id)
    assert row is not None and row.status == "queued" and row.task_id is not None

    audit = AuditRepo(connection).list_recent(limit=10)
    assert any(op.action == "topic.select" and op.actor == "user" for op in audit)
    assert declared_events(connection, "topic.selected")[0]["topic_id"] == topic_id


def test_select_is_idempotent(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """幂等键 ``topic:<id>``：再点一次是**复用**，不是建第二个任务。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]

    first = client.post(SELECT_URL, json={"topic_ids": [topic_id]}).json()["selected"][0]
    second = client.post(SELECT_URL, json={"topic_ids": [topic_id]}).json()["selected"][0]

    assert first["task_id"] == second["task_id"]
    assert second["created_task"] is False
    total = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert total == 1


def test_select_with_draft_now_writes_a_script(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``draft_now=true`` ⇒ 入队后顺手跑写稿（与 CLI 的 ``studio script draft`` 等价）。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]

    # 写稿多两次 LLM 调用（Director + Writer）⇒ 重新脚本化，只留这两条
    arm(monkeypatch, director_reply(), writer_reply())
    response = client.post(SELECT_URL, json={"topic_ids": [topic_id], "draft_now": True})
    assert response.status_code == 200, response.text
    item = response.json()["selected"][0]

    assert response.json()["drafted"] == 1
    assert item["draft"] is not None
    assert item["draft"]["ok"] is True
    assert item["draft"]["sentence_count"] == 20
    assert connection.execute("SELECT COUNT(*) FROM script_sentences").fetchone()[0] == 20


def test_select_body_rejects_unknown_fields(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra=forbid``：`topicIds` 写错要炸，而不是"静默地一条都没进去"。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    response = client.post(SELECT_URL, json={"topicIds": ["tp1"]})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# ⑦ 人工加选题
# ══════════════════════════════════════════════════════════════════════


def test_manual_topic_lands_with_audit_and_similar_hint(
    client: TestClient,
    connection: sqlite3.Connection,
) -> None:
    payload = {"title": "我自己想的一条", "angle": "只讲这一跳", "hook_type": "conflict", "score": 9.0}
    first = client.post(MANUAL_URL, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["similar_to"] == []

    # 同样的标题再加一次：**仍然入库**，只是把"库里有很像的"提示回来（裁定 131）
    second = client.post(MANUAL_URL, json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["similar_to"]
    assert second.json()["warnings"]

    rows = TopicRepo(connection).list_pool(status="candidate", limit=10)
    assert len(rows) == 2
    # 人工加选题挂在固定批次 `manual` 上（懒建一次）
    assert {row.direction_id for row in rows} == {second.json()["direction_id"]}
    assert DirectionRepo(connection).list_batches() == ["manual"]
    assert all(row.exec_feasible for row in rows)

    audit = AuditRepo(connection).list_recent(limit=10)
    assert sum(1 for op in audit if op.action == "topic.manual_add") == 2


def test_manual_topic_rejects_an_empty_title(client: TestClient) -> None:
    assert client.post(MANUAL_URL, json={"title": ""}).status_code == 422
    assert client.post(MANUAL_URL, json={"title": "x", "score": 99}).status_code == 422


# ══════════════════════════════════════════════════════════════════════
# ⑧ 输入源
# ══════════════════════════════════════════════════════════════════════


def test_hot_scan_imports_files(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
) -> None:
    paths.hot_dir.mkdir(parents=True, exist_ok=True)
    (paths.hot_dir / "0913.md").write_text(f"{HOT_LINE}\n这一行只有两列|爆\n", encoding="utf-8")

    response = client.post(HOT_IMPORT_URL, json={"hot": True, "feedback": False})
    assert response.status_code == 200, response.text
    hot = response.json()["hot"]

    assert hot["parsed"] == 1  # 只数**解析成功**的行
    assert hot["inserted"] == 2  # 坏行照入库留痕（`parse_ok=0`），但不参与选题
    assert hot["bad"] == 1
    assert response.json()["feedback"] is None


def test_hot_submit_names_the_file_on_the_server(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
) -> None:
    response = client.post(HOT_SUBMIT_URL, json={"text": f"{HOT_LINE}\nMC 跑酷速通纪录|高|douyin"})
    assert response.status_code == 200, response.text
    assert response.json()["hot"]["inserted"] == 2

    written = sorted(path.name for path in paths.hot_dir.glob("*.md"))
    assert len(written) == 1
    assert written[0].startswith("webui-")
    assert written[0].endswith(".md")


def test_hot_submit_rejects_empty_text(client: TestClient) -> None:
    assert client.post(HOT_SUBMIT_URL, json={"text": ""}).status_code == 422


# ══════════════════════════════════════════════════════════════════════
# WS 扇出
# ══════════════════════════════════════════════════════════════════════


def test_events_are_declared_for_the_ws_fanout(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``direction.batch_ready`` / ``topic.batch_ready`` 必须带路由键。

    这两个事件是"worker 进程把面板叫醒"的唯一手段（跨进程只有"表 → 推送"这条路，
    T1.7 裁定 45）。少了 ``batch_id`` / ``direction_id``，前端就只能整块重拉。
    """
    run_planner_and_ideator(client, monkeypatch, paths, connection)

    ready = declared_events(connection, "direction.batch_ready")
    assert ready and ready[-1]["batch_id"]
    assert len(ready[-1]["directions"]) == len(TITLE_SETS)

    per_direction = declared_events(connection, "topic.batch_ready")
    assert per_direction and per_direction[-1]["direction_id"]
    assert len(per_direction) == len(TITLE_SETS)  # 一个方向一条（`merge_field='direction_id'`）


def test_persona_is_read_from_the_hot_reloadable_store(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``analyze`` 用的是**当前激活人物**（改口吻不需要重启 API）。"""
    transport = arm(monkeypatch, planner_reply())
    assert client.post(ANALYZE_URL, json={"import_sources": False}).status_code == 200

    rendered = "".join(message.content for _, messages in transport.calls for message in messages)
    active = deps.active_persona()
    assert active.name in rendered
    # 口癖来自 `config/persona.yaml`（热重载那一份），而不是测试假件
    assert active.catchphrases[0] in rendered
    assert persona().catchphrases[0] not in rendered
