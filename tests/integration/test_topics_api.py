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
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.agents.fakes import Reply, ScriptedTransport, persona

from studio.agents import gateway_factory
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.agents.llm_client import ChatMessage, LlmCall, LlmResponse
from studio.app import deps
from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.app.routers import topics as topics_router
from studio.app.watchdog import WatchdogPump
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
    # `home` 指向仓库根 ⇒ `workers/` 真的存在 ⇒ `WatchdogPump.runnable` 为真，
    # `lifespan` 会起跳守护；守护一旦发现 `draft` 没就绪，就**真的 Popen 一个
    # `workers/run_draft.py`**，而且以 3.5s 的节奏反复重启（每轮两个进程：venv
    # 转发器 + 真解释器）。每个孤儿都攥着本用例 tmp 里那份 `data/logs/draft.log`，
    # pytest 收尾删临时目录时就是 `WinError 32`，而报错挂在**后面几百个用例**的
    # setup 上 —— 看上去像是它们坏了。本文件测的是选题 REST 面，守护起不起跳与
    # 它无关，所以把「能不能起跳」钉死为否。
    monkeypatch.setattr(WatchdogPump, "runnable", property(lambda self: False))
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


def arm(
    monkeypatch: pytest.MonkeyPatch,
    *replies: str,
    probe: Callable[[], None] | None = None,
) -> ScriptedTransport:
    """把 LLM 传输换成脚本（其余装配照旧：真 schema / 真提示词 / 真预算闸门）。

    原函数刻意取 ``gateway_factory`` 那一份而不是 ``deps.build_gateway``：后者在第一
    次 ``arm`` 之后已经是我们自己换上去的假件，再包一层会把新脚本吃掉、继续用旧的
    传输（一个用例里换两次脚本就会踩到）。

    ``probe=`` 给「每次 LLM 调用**之前**」挂一个钩子（探针在**外面**套一层，
    返回的仍是那条脚本传输）—— 用来断言「某一刻库里是什么样」。
    """
    transport = ScriptedTransport(replies=[Reply(text=item) for item in replies])
    wrapped: Any = transport if probe is None else _ProbingTransport(probe, transport)
    real = gateway_factory.build_gateway

    def patched(**kwargs: Any) -> LlmGateway:
        return real(
            **{
                **kwargs,
                "transport": wrapped,
                "settings": GatewaySettings(backoff_base_sec=0.0),
                "env": {"STUDIO_LLM_API_KEY": "test-key"},
            }
        )

    monkeypatch.setattr(deps, "build_gateway", patched)
    return transport


class _ProbingTransport:
    """``ScriptedTransport`` 外面套一层探针：每次调用**之前**先跑一下 ``probe``。

    为什么不是子类：``ScriptedTransport`` 是 ``slots=True`` 的 dataclass，
    塞不进额外属性。
    """

    def __init__(self, probe: Callable[[], None], inner: ScriptedTransport) -> None:
        self._probe = probe
        self._inner = inner

    async def chat(self, call: LlmCall, messages: Sequence[ChatMessage]) -> LlmResponse:
        self._probe()
        return await self._inner.chat(call, messages)

    async def aclose(self) -> None:
        await self._inner.aclose()


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


# ══════════════════════════════════════════════════════════════════════
# ⑨ 改选题 / 删选题
# ══════════════════════════════════════════════════════════════════════


def _manual_topic(client: TestClient, title: str = "我自己想的一条") -> str:
    """人工加一条选题并返回它的 id（⑨ 这一组都要先有一条能改的东西）。"""
    response = client.post(MANUAL_URL, json={"title": title, "angle": "只讲这一跳"})
    assert response.status_code == 200, response.text
    return str(response.json()["topic_id"])


def test_patch_topic_rewrites_and_keeps_dedup_honest(
    client: TestClient,
    connection: sqlite3.Connection,
) -> None:
    """改标题 ⇒ 去重指纹跟着改；同样的值再改一次 ⇒ 不假装改了一次。"""
    topic_id = _manual_topic(client, "改之前")
    before = TopicRepo(connection).get(topic_id)
    assert before is not None

    response = client.patch(f"{TOPICS_URL}/{topic_id}", json={"title": "改之后", "score": 7.5})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["changed"] == ["score", "title"]
    assert body["topic"]["title"] == "改之后"
    assert body["topic"]["score"] == 7.5
    # 指纹是 R15 的判据：标题改了却留着旧指纹，等于让「这条跟谁像」从此说谎
    assert body["topic"]["dedup_hash"] != before.dedup_hash

    again = client.patch(f"{TOPICS_URL}/{topic_id}", json={"title": "改之后"})
    assert again.status_code == 200, again.text
    assert again.json()["changed"] == []

    audit = [op for op in AuditRepo(connection).list_recent(limit=20) if op.action == "topic.updated"]
    assert len(audit) == 1  # 什么都没改的那一次**不留痕**
    assert audit[0].target_id == topic_id
    assert audit[0].before is not None and audit[0].before["title"] == "改之前"
    assert audit[0].after is not None and audit[0].after["title"] == "改之后"


def test_patch_topic_requires_one_field_and_rejects_bad_values(client: TestClient) -> None:
    topic_id = _manual_topic(client)
    url = f"{TOPICS_URL}/{topic_id}"

    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"score": 99}).status_code == 422
    assert client.patch(url, json={"nope": 1}).status_code == 422

    blank = client.patch(url, json={"title": "   "})
    assert blank.status_code == 422
    assert blank.json()["code"] == "TOPIC_SELECT_INVALID"


def test_patch_topic_flags_a_rename_that_collides_with_the_pool(client: TestClient) -> None:
    """改名撞上库里已有的一条：**仍然改**（人的意图优先），但把「跟谁像」提示回来。"""
    _manual_topic(client, "老标题")
    topic_id = _manual_topic(client, "另一条")

    response = client.patch(f"{TOPICS_URL}/{topic_id}", json={"title": "老标题"})
    assert response.status_code == 200, response.text
    assert response.json()["warnings"]
    assert response.json()["topic"]["similar_to"]


def test_delete_topic_removes_the_row_and_leaves_an_audit(
    client: TestClient,
    connection: sqlite3.Connection,
) -> None:
    topic_id = _manual_topic(client)

    response = client.delete(f"{TOPICS_URL}/{topic_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"topic_id": topic_id, "title": "我自己想的一条", "deleted": True}
    assert TopicRepo(connection).get(topic_id) is None
    assert any(op.action == "topic.deleted" for op in AuditRepo(connection).list_recent(limit=20))

    missing = client.delete(f"{TOPICS_URL}/{topic_id}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "TOPIC_NOT_FOUND"


def test_delete_topic_refuses_when_a_task_already_exists(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经派生过任务的选题不给删 —— 删了那条任务就再也写不出稿（不是门禁，是断链）。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]
    assert client.post(SELECT_URL, json={"topic_ids": [topic_id]}).status_code == 200

    response = client.delete(f"{TOPICS_URL}/{topic_id}")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "TOPIC_SELECT_INVALID"
    assert body["context"]["task_id"]
    assert body["remediation"]
    assert TopicRepo(connection).get(topic_id) is not None


# ══════════════════════════════════════════════════════════════════════
# ⑩ 二级产物 · 视频标题 + 核心论点（文案三级流水线的中间一级）
# ══════════════════════════════════════════════════════════════════════

#: 二级定下来的两样东西（后面几条用例反复引用同一对值）
OUTLINE_TITLE = "这一跳为什么没人跳得过去"
OUTLINE_ARGUMENT = "新手翻车都在起跳前多按了一下空格"

#: ``writer_reply()`` 里 Writer 自己起的标题 —— 与二级产物**故意不同**，
#: 「标题被锁定」才是一条真的断言（两边一样的话，锁没锁都看不出来）。
WRITER_OWN_TITLE = "MC跑酷最难的一跳"


def _outline_url(topic_id: str) -> str:
    return f"{TOPICS_URL}/{topic_id}/outline"


def outliner_reply(title: str = OUTLINE_TITLE, core_argument: str = OUTLINE_ARGUMENT) -> str:
    """Outliner 的脚本化回复（这一级的产物只有这两个自由文本字段）。"""
    return json.dumps({"title": title, "core_argument": core_argument}, ensure_ascii=False)


def test_outline_is_null_until_something_defines_it(client: TestClient) -> None:
    """「还没定」是**正常状态**（三级会照旧自由发挥）⇒ 200 + null，不是 404。"""
    topic_id = _manual_topic(client)

    response = client.get(_outline_url(topic_id))
    assert response.status_code == 200, response.text
    assert response.json() == {"topic_id": topic_id, "outline": None}


def test_outline_generation_calls_the_model_once_and_lands(
    client: TestClient,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成一次 ⇒ 落库、可读回、留痕；这一级**没有规则重试**（一次调用）。"""
    topic_id = _manual_topic(client, "MC跑酷最难的一跳")
    transport = arm(monkeypatch, outliner_reply())

    response = client.post(_outline_url(topic_id))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ok"] is True
    assert body["generated"] is True  # 这一份是模型产出的
    assert body["title"] == OUTLINE_TITLE
    assert body["core_argument"] == OUTLINE_ARGUMENT
    assert body["llm_model"]  # 模型名在控制台可改 ⇒ 只断言「记下来了」
    assert body["prompt_version"]
    assert len(transport.calls) == 1

    # 生成完就能读回来（前端「展开编辑器」读的是同一条）
    stored = client.get(_outline_url(topic_id)).json()["outline"]
    assert stored is not None
    assert stored["title"] == OUTLINE_TITLE
    assert stored["core_argument"] == OUTLINE_ARGUMENT
    assert stored["updated_at"]

    actions = [op.action for op in AuditRepo(connection).list_recent(limit=20)]
    assert "outline.generated" in actions


def test_outline_can_be_handwritten_without_any_llm(
    client: TestClient,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配 Key 也要能先把标题与论点定下来 ⇒ 手写这条路一次 LLM 都不调。"""
    topic_id = _manual_topic(client)
    transport = arm(monkeypatch)  # 一条回复都不给：真调了就会露馅

    response = client.put(_outline_url(topic_id), json={"title": "手写的标题", "core_argument": "手写的论点"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["generated"] is False  # 手写的不是「模型产出」
    assert body["changed"] == ["title", "core_argument"]
    assert body["title"] == "手写的标题"
    assert transport.calls == []

    # 再改一次：留痕里 before/after 都在 —— 谁把论点掰弯了查得到
    again = client.put(_outline_url(topic_id), json={"title": "改过的标题", "core_argument": "改过的论点"})
    assert again.status_code == 200, again.text
    edits = [op for op in AuditRepo(connection).list_recent(limit=20) if op.action == "outline.updated"]
    assert len(edits) == 2
    assert any(not op.before for op in edits)  # 第一次是「从无到有」（``before`` 落库是空 dict）
    assert any(op.before is not None and op.before["title"] == "手写的标题" for op in edits)


def test_outline_rejects_blank_and_overlong_text(client: TestClient) -> None:
    """两个字段都是必填且各有上限：想撤掉这一级请用「清空」，不是存个空标题。"""
    url = _outline_url(_manual_topic(client))

    blank = client.put(url, json={"title": "   ", "core_argument": "有论点"})
    assert blank.status_code == 422
    assert blank.json()["code"] == "OUTLINE_INVALID"
    assert blank.json()["remediation"]

    empty_argument = client.put(url, json={"title": "有标题", "core_argument": "   "})
    assert empty_argument.status_code == 422
    assert empty_argument.json()["code"] == "OUTLINE_INVALID"

    # 上限由 schema 卡（标题 60 / 论点 200，pydantic 层就拦住，还没进服务）
    assert client.put(url, json={"title": "熊" * 61, "core_argument": "有论点"}).status_code == 422
    assert client.put(url, json={"title": "有标题", "core_argument": "熊" * 201}).status_code == 422


def test_outline_clear_is_idempotent(client: TestClient, connection: sqlite3.Connection) -> None:
    topic_id = _manual_topic(client)
    saved = client.put(_outline_url(topic_id), json={"title": "待清的标题", "core_argument": "待清的论点"})
    assert saved.status_code == 200, saved.text

    first = client.delete(_outline_url(topic_id))
    assert first.status_code == 200, first.text
    assert first.json()["changed"] == ["title", "core_argument"]
    assert client.get(_outline_url(topic_id)).json()["outline"] is None

    again = client.delete(_outline_url(topic_id))
    assert again.status_code == 200, again.text
    assert again.json()["changed"] == []

    deletes = [op for op in AuditRepo(connection).list_recent(limit=20) if op.action == "outline.deleted"]
    assert len(deletes) == 1  # 本来就没有的那一次不留痕


def test_outline_endpoints_need_a_known_topic(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = arm(monkeypatch, outliner_reply())
    missing = "01J0000000000000000000000"

    # 读：未知 id 也只是「还没定」（与「这一级还没做」同一形状，不额外造一个错误态）
    assert client.get(_outline_url(missing)).json()["outline"] is None

    created = client.post(_outline_url(missing))
    saved = client.put(_outline_url(missing), json={"title": "标题", "core_argument": "论点"})
    cleared = client.delete(_outline_url(missing))
    for response in (created, saved, cleared):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "TOPIC_NOT_FOUND"
    assert transport.calls == []  # 选题都不存在 ⇒ 一次 LLM 都不该调


def test_a_saved_outline_locks_the_script_title_and_feeds_the_prompt(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二级定了 ⇒ 三级**锁定**那个标题，并把标题与论点当主线喂给 Director 与 Writer。

    ``writer_reply()`` 里模型自己起的是 ``WRITER_OWN_TITLE``（另一个标题）—— 它不许覆盖
    二级定死的那一个：提示词要求照抄，服务层再强制一次。
    """
    assert WRITER_OWN_TITLE != OUTLINE_TITLE  # 断言有意义的前提
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]
    saved = client.put(
        _outline_url(topic_id), json={"title": OUTLINE_TITLE, "core_argument": OUTLINE_ARGUMENT}
    )
    assert saved.status_code == 200, saved.text

    transport = arm(monkeypatch, director_reply(), writer_reply())
    response = client.post(SELECT_URL, json={"topic_ids": [topic_id], "draft_now": True})
    assert response.status_code == 200, response.text
    draft = response.json()["selected"][0]["draft"]
    assert draft["ok"] is True, draft
    assert draft["title"] == OUTLINE_TITLE

    stored = connection.execute(
        "SELECT s.title FROM scripts s JOIN tasks t ON t.id = s.task_id WHERE t.source_topic_id = ?",
        (topic_id,),
    ).fetchone()
    assert stored is not None and stored["title"] == OUTLINE_TITLE

    # 提示词侧：Director 先（calls[0]）、Writer 后（calls[1]），两边的 system 都带着二级产物
    assert OUTLINE_TITLE in transport.calls[0][1][0].content
    assert OUTLINE_ARGUMENT in transport.calls[0][1][0].content
    assert OUTLINE_TITLE in transport.calls[1][1][0].content
    assert OUTLINE_ARGUMENT in transport.calls[1][1][0].content


# ══════════════════════════════════════════════════════════════════════
# ⑪ 方向 · 人工写 / 改 / 删（左列那一栏）
# ══════════════════════════════════════════════════════════════════════

DIRECTIONS_URL = "/api/v1/topics/directions"


def test_a_handwritten_direction_lands_in_the_batch_on_screen(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """人工写方向落进**当前批次**，而不是另起一个「手工批次」把模型那批盖掉。"""
    response = client.post(DIRECTIONS_URL, json={"title": "我手写的方向", "rationale": "先占位"})
    assert response.status_code == 200, response.text
    card = response.json()["direction"]
    assert card["title"] == "我手写的方向"
    assert card["rationale"] == "先占位"
    assert card["priority"] == 100
    assert card["status"] == "open"
    assert card["seq"] == 1  # 库里一条方向都没有 ⇒ 从 1 开始

    listed = client.get(DIRECTIONS_URL).json()
    assert [item["id"] for item in listed["directions"]] == [card["id"]]
    assert listed["batch_id"] == card["batch_id"]

    # 再写一条：排在上一条后面（同一个批次里顺序追加）
    second = client.post(DIRECTIONS_URL, json={"title": "第二个方向"})
    assert second.status_code == 200, second.text
    assert second.json()["direction"]["seq"] == 2
    assert second.json()["direction"]["batch_id"] == card["batch_id"]

    actions = [op.action for op in AuditRepo(connection).list_recent(limit=20)]
    assert "direction.created" in actions


def test_a_handwritten_direction_joins_the_planner_batch(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经跑过 Planner ⇒ 人写的方向**并进那一批**（左列上模型与人写的混在一起）。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    before = client.get(DIRECTIONS_URL).json()
    assert len(before["directions"]) == len(TITLE_SETS)

    response = client.post(DIRECTIONS_URL, json={"title": "人工补一个方向"})
    assert response.status_code == 200, response.text
    assert response.json()["direction"]["batch_id"] == before["batch_id"]

    after = client.get(DIRECTIONS_URL).json()
    assert len(after["directions"]) == len(TITLE_SETS) + 1
    assert after["directions"][-1]["title"] == "人工补一个方向"


def test_patch_direction_rewrites_only_the_named_columns(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    created = client.post(DIRECTIONS_URL, json={"title": "改之前", "rationale": "旧理由"})
    direction_id = created.json()["direction"]["id"]

    response = client.patch(f"{DIRECTIONS_URL}/{direction_id}", json={"title": "改之后"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == ["title"]
    assert body["direction"]["title"] == "改之后"
    assert body["direction"]["rationale"] == "旧理由"  # 没点名的列一个字节都不动

    again = client.patch(f"{DIRECTIONS_URL}/{direction_id}", json={"title": "改之后"})
    assert again.status_code == 200, again.text
    assert again.json()["changed"] == []

    updates = [op for op in AuditRepo(connection).list_recent(limit=20) if op.action == "direction.updated"]
    assert len(updates) == 1  # 什么都没改的那一次**不留痕**
    assert updates[0].before is not None and updates[0].before["title"] == "改之前"
    assert updates[0].after is not None and updates[0].after["title"] == "改之后"


def test_patch_direction_rejects_empty_and_unknown_fields(client: TestClient) -> None:
    direction_id = client.post(DIRECTIONS_URL, json={"title": "一条方向"}).json()["direction"]["id"]
    url = f"{DIRECTIONS_URL}/{direction_id}"

    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"nope": 1}).status_code == 422
    assert client.patch(url, json={"priority": -1}).status_code == 422

    blank = client.patch(url, json={"title": "   "})
    assert blank.status_code == 422
    assert blank.json()["code"] == "TOPIC_SELECT_INVALID"


def test_delete_direction_takes_its_candidates_with_it(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """级联删除：方向没了，它下面的候选一条都不剩（并如实报数）。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    listed = client.get(DIRECTIONS_URL).json()
    victim = listed["directions"][0]
    assert victim["topic_count"] > 0

    response = client.delete(f"{DIRECTIONS_URL}/{victim['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["title"] == victim["title"]
    assert body["cascaded_topics"] == victim["topic_count"]

    left = connection.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0]
    total_before = sum(item["topic_count"] for item in listed["directions"])
    assert left == total_before - victim["topic_count"]

    missing = client.delete(f"{DIRECTIONS_URL}/{victim['id']}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "TOPIC_NOT_FOUND"


def test_delete_direction_refuses_when_a_candidate_already_has_a_task(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经派生过任务的候选不给级联删 —— 删了那条任务就再也写不出稿。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]
    row = TopicRepo(connection).get(topic_id)
    assert row is not None
    direction_id = row.direction_id
    assert client.post(SELECT_URL, json={"topic_ids": [topic_id]}).status_code == 200

    response = client.delete(f"{DIRECTIONS_URL}/{direction_id}")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "TOPIC_SELECT_INVALID"
    assert body["context"]["task_ids"]
    assert body["remediation"]
    assert client.get(DIRECTIONS_URL).json()["directions"]


# ══════════════════════════════════════════════════════════════════════
# ⑫ 三级产物 · 生成完整文案并移交审核（候选卡上那一个按钮）
# ══════════════════════════════════════════════════════════════════════


def draft_review_url(topic_id: str) -> str:
    return f"{TOPICS_URL}/{topic_id}/draft-review"


def test_draft_review_writes_a_script_and_hands_it_to_review(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选中一条候选 ⇒ 成稿落库 ⇒ 任务推 ``reviewing``（写稿池接着跑评分 + 确认闸）。

    为什么钉死 ``reviewing`` 而不是 ``drafting``：``drafting`` 的语义是"正在写"，
    而这一刻稿子已经在库里了。停在 ``drafting`` 会让面板显示"写稿中"，然后由写稿池
    认领时才发现"已有稿件 ⇒ 跳过写稿直接审稿" —— 状态在没有任何写入的情况下自己
    往前跳一格，看着像有人偷偷动了它。
    """
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]
    arm(monkeypatch, director_reply(), writer_reply())

    response = client.post(draft_review_url(topic_id))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ok"] is True
    assert body["reused"] is False
    assert body["task_status"] == "reviewing"
    assert body["sentence_count"] == 20
    assert body["script_id"]
    assert body["title"]

    row = connection.execute("SELECT status FROM tasks WHERE id = ?", (body["task_id"],)).fetchone()
    assert row is not None and row[0] == "reviewing"
    # 稿件真的落库了（不是只回了一个 id）
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM script_sentences WHERE script_id = ?", (body["script_id"],)
        ).fetchone()[0]
        == 20
    )
    # 入队那一步的副作用：选题被标成 queued
    assert (
        connection.execute("SELECT status FROM topic_candidates WHERE id = ?", (topic_id,)).fetchone()[0]
        == "queued"
    )


def test_draft_review_reuses_the_script_instead_of_burning_another_call(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """再点一次 ⇒ **一个 token 都不烧**（重写会白烧 Director + Writer，还会盖掉正在审的那一版）。"""
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]
    arm(monkeypatch, director_reply(), writer_reply())
    first = client.post(draft_review_url(topic_id)).json()

    transport = arm(monkeypatch, director_reply(), writer_reply())
    second = client.post(draft_review_url(topic_id))
    assert second.status_code == 200, second.text
    body = second.json()

    assert body["ok"] is True
    assert body["reused"] is True
    assert body["script_id"] == first["script_id"]
    assert body["task_status"] == "reviewing"
    assert transport.calls == []  # 真的一次 LLM 都没发
    # 稿件没有被重写（版本号没变）
    assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 1


def test_draft_review_needs_a_known_topic(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    arm(monkeypatch, director_reply(), writer_reply())
    response = client.post(draft_review_url("tp_does_not_exist"))
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "TOPIC_NOT_FOUND"


def test_the_draft_job_is_not_claimable_while_the_inline_draft_runs(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """就地写稿期间，写稿池**看不到**这个任务。

    为什么单独钉死这一条：写稿池 ~1s 就来认领，而一次就地写稿要跑几分钟的 LLM。
    作业要是提前投出去，两边就并发跑同一份 Director + Writer —— 实测过一次，
    两条 director 调用的入参一字不差、起始时间只差 3.9 秒；token 翻倍之后
    `budget_exceeded` 把云端通道熔断，评分降级到本地小模型（等级与放行全跟着歪）。
    """
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]

    claimable: list[int] = []

    def probe() -> None:
        # 探针跑在请求那个线程里，而夹具那条连接是主线程的（sqlite3 跨线程直接报错）
        # ⇒ 现开一条只读连接。作业要是提前投了，那一行此时已经提交。
        with sqlite3.connect(paths.db_file) as probe_conn:
            claimable.append(
                probe_conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE pool = 'draft' AND status IN ('pending', 'claimed')"
                ).fetchone()[0]
            )

    arm(monkeypatch, director_reply(), writer_reply(), probe=probe)
    response = client.post(draft_review_url(topic_id))
    assert response.status_code == 200, response.text

    assert claimable, "一次 LLM 都没发 ⇒ 探针没挂上，这个用例什么也没测到"
    assert set(claimable) == {0}, f"就地写稿还在跑，作业已经可认领了：{claimable}"
    # 写完才投：池子认领到的是一条「稿件已在库里」的作业 ⇒ 只跑审稿
    rows = connection.execute(
        "SELECT pool, status FROM jobs WHERE unit_ref = ?", (response.json()["task_id"],)
    ).fetchall()
    assert [tuple(row) for row in rows] == [("draft", "pending")]


def test_select_hands_the_draft_job_to_the_pool_right_away(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """勾选入队**不跑写稿** ⇒ 作业必须立刻投出去（与「生成文案并送审」那条路正好相反）。

    两条路要是混了：轻的是勾了 6 条、面板上 6 个 pending 永远不动，
    重的是写稿池与就地写稿抢同一个任务（token 翻倍，见上一条用例）。
    """
    run_planner_and_ideator(client, monkeypatch, paths, connection)
    topic_id = client.get(TOPICS_URL).json()["topics"][0]["id"]

    response = client.post(SELECT_URL, json={"topic_ids": [topic_id]})
    assert response.status_code == 200, response.text
    task_id = response.json()["selected"][0]["task_id"]

    rows = connection.execute("SELECT pool, status FROM jobs WHERE unit_ref = ?", (task_id,)).fetchall()
    assert [tuple(row) for row in rows] == [("draft", "pending")]
