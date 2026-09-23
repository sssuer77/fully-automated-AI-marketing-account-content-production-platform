"""选题面板「一键拉取今日新闻」REST 面（T5.12）。

验收五条，一条一个用例
----------------------
① 值得写的**留成了方向**，不值得写的**如实报出来**；
② 留下的方向落在**当前正在看的那个批次**里（与手写方向同一条路，可改可删）；
③ ``ref`` 对不上 ⇒ **丢弃而不是猜**（模型没给这一条，就当没挑中）；
④ 模型没给方向标题 ⇒ 退回**新闻标题**（用户点的是"这条新闻值得写"）；
⑤ 抓取挂了**不是**"今天没有新闻"：503 + ``NEWS_FETCH_FAILED``；
   评测没跑出来 ⇒ ``ok=False`` 且**库里一个方向都没多**。
⑥ 源站摘要进提示词、事件总结落进方向的**依据**（写稿时的事实源），
   模型说"信息不足"则不落。

三条纪律
--------
1. **真 schema / 真提示词**：家目录指向仓库根，所以 ``news_scout`` 那两份提示词与
   ``schemas/news_scout.schema.json`` 走的是生产那一份（网关的修复重试也是真的）。
2. **不真出网**：抓取换成假件（``news_service.fetch_news``）—— "源站今天在不在"不是这些
   用例该回答的问题，它们回答的是"拿到这几条新闻之后会发生什么"。
3. **判据落在库上**：返回 200 只说明没抛。方向条数、批次、留痕都回 SQLite 查。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit.agents.fakes import Reply, ScriptedTransport

from studio.agents import gateway_factory
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.agents.llm_client import ChatMessage, LlmCall, LlmResponse
from studio.app import deps
from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.app.watchdog import WatchdogPump
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.core.persona_store import reset_persona_store
from studio.db import migrate
from studio.domain.topics import NewsItemSpec
from studio.services import news_service
from studio.services.news_service import NEWS_LIMIT
from studio.services.topic_service import MANUAL_BATCH_ID
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

NEWS_URL = "/api/v1/topics/news-pull"
DIRECTIONS_URL = "/api/v1/topics/directions"

#: 三条"今日新闻"（一真两假：只有第一条与第三条有可讲的观点）
NEWS: tuple[NewsItemSpec, ...] = (
    NewsItemSpec(
        ref="n01",
        title="聚能环致一家三口身亡",
        source="今日头条热榜",
        url="https://example.com/1",
        heat=921000,
    ),
    NewsItemSpec(
        ref="n02", title="某队客场 2:1 取胜", source="今日头条热榜", url="https://example.com/2", heat=410000
    ),
    NewsItemSpec(
        ref="n03",
        title="小区电梯困人 40 分钟",
        source="今日头条热榜",
        url="https://example.com/3",
        heat=330000,
    ),
)


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
    monkeypatch.setenv("STUDIO_HOME", str(REPO_ROOT))
    reset_persona_store()
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
    # 与 `test_topics_api.py` 同一条：本文件测的是选题 REST 面，守护起不起跳与它无关，
    # 而真起跳会 Popen 出孤儿进程攥着 tmp 里的日志文件（收尾删目录时 WinError 32）。
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


class _ProbingTransport:
    """``ScriptedTransport`` 外面套一层探针（``ScriptedTransport`` 是 ``slots`` 数据类）。"""

    def __init__(self, probe: Callable[[], None], inner: ScriptedTransport) -> None:
        self._probe = probe
        self._inner = inner

    async def chat(self, call: LlmCall, messages: Sequence[ChatMessage]) -> LlmResponse:
        self._probe()
        return await self._inner.chat(call, messages)

    async def aclose(self) -> None:
        await self._inner.aclose()


def arm(
    monkeypatch: pytest.MonkeyPatch,
    *replies: str,
    probe: Callable[[], None] | None = None,
) -> ScriptedTransport:
    """把 LLM 传输换成脚本（其余装配照旧：真 schema / 真提示词 / 真预算闸门）。"""
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


def use_news(monkeypatch: pytest.MonkeyPatch, items: Sequence[NewsItemSpec] = NEWS) -> None:
    """把抓取换成假件（**不动网关**：评测那一段仍走真 schema / 真提示词）。"""

    async def fake_fetch(*, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
        return list(items)[:limit]

    # 打的是**模块属性**：`topic_service_for` 每次现装配，请求时才取这个属性。
    monkeypatch.setattr(news_service, "fetch_news", fake_fetch)


def verdict(
    ref: str,
    *,
    keep: bool,
    title: str = "",
    rationale: str = "",
    summary: str = "",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "keep": keep,
        "direction_title": title,
        "rationale": rationale,
        "event_summary": summary,
    }


def scout_reply(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def directions_in_db(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT title, rationale, batch_id, seq FROM content_directions ORDER BY seq"
    ).fetchall()


# ══════════════════════════════════════════════════════════════════════
# ① 值得写的留下、不值得写的如实报
# ══════════════════════════════════════════════════════════════════════


def test_pull_news_keeps_the_worthy_ones_as_directions(
    client: TestClient, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm(
        monkeypatch,
        scout_reply(
            verdict("n01", keep=True, title="聚能环致死：省钱省出的命案", rationale="有冲突也有具体后果"),
            verdict("n02", keep=False, rationale="只有赛果没有观点"),
            verdict("n03", keep=True, title="电梯困人 40 分钟：物业在等什么", rationale="民生小事讲得出来"),
        ),
    )
    use_news(monkeypatch)

    response = client.post(NEWS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "今日头条热榜"
    assert body["fetched"] == 3
    assert body["evaluated"] == 3
    assert body["kept_count"] == 2
    assert [item["title"] for item in body["kept"]] == [
        "聚能环致死：省钱省出的命案",
        "电梯困人 40 分钟：物业在等什么",
    ]
    assert body["skipped"] == [{"title": "某队客场 2:1 取胜", "reason": "只有赛果没有观点"}]
    assert body["warnings"] == []

    # 返回 200 只说明没抛：方向真进了库，理由里认得出它来自哪条新闻
    rows = directions_in_db(connection)
    assert [row["title"] for row in rows] == [
        "聚能环致死：省钱省出的命案",
        "电梯困人 40 分钟：物业在等什么",
    ]
    assert rows[0]["rationale"].startswith("今日新闻《聚能环致一家三口身亡》")
    assert "https://example.com/1" in rows[0]["rationale"]

    # 留痕要认得出"这个方向是模型从今天的新闻里挑出来的"，而不是人写的。
    # actor 只能是那四个之一（audit_ops 的 CHECK）⇒ 区分靠 actor_ref。
    audit = connection.execute(
        "SELECT actor, actor_ref, reason FROM audit_ops WHERE action = 'direction.created'"
    ).fetchall()
    assert {row["actor"] for row in audit} == {"system"}
    assert {row["actor_ref"] for row in audit} == {"news_scout"}
    assert {row["reason"] for row in audit} == {"今日新闻挑出来的方向"}


def test_the_source_summary_names_every_channel_that_answered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 五源**合并**之后，``source`` 那一栏必须如实说"这一批来自哪几家"。

    原来是 ``items[0].source``（"首个成功即返回"时代的口径：一批只有一个来源）。合并之后
    再取第一条，面板上会写着「今日头条热榜：评测 50 条」—— 用户会以为今天只从头条抓了，
    而**另外四家到底有没有生效、有没有挂掉**就再也看不出来了。
    """
    mixed = [
        NEWS[0].model_copy(update={"source": "今日头条热榜"}),
        NEWS[1].model_copy(update={"source": "抖音热搜"}),
        NEWS[2].model_copy(update={"source": "中新网滚动"}),
        NEWS[2].model_copy(update={"source": "抖音热搜"}),
    ]
    arm(
        monkeypatch,
        scout_reply(
            verdict("n01", keep=False, rationale="只有赛果"),
            verdict("n02", keep=False, rationale="只有赛果"),
            verdict("n03", keep=False, rationale="只有赛果"),
            verdict("n04", keep=False, rationale="只有赛果"),
        ),
    )
    use_news(monkeypatch, mixed)

    body = client.post(NEWS_URL).json()
    assert body["fetched"] == 4
    # 按**首次出现**的顺序去重拼接；重复出现的"抖音热搜"只写一次
    assert body["source"] == "今日头条热榜、抖音热搜、中新网滚动"


def test_pull_news_lands_in_the_batch_being_viewed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「保留在选题方向里」= 落进**当前那一批**，而不是另起一批把人正在看的方向盖掉。"""
    manual = client.post(DIRECTIONS_URL, json={"title": "手写的方向"}).json()
    assert manual["direction"]["batch_id"] == MANUAL_BATCH_ID

    arm(
        monkeypatch,
        scout_reply(verdict("n01", keep=True, title="聚能环致死：省钱省出的命案", rationale="值得写")),
    )
    use_news(monkeypatch, NEWS[:1])

    body = client.post(NEWS_URL).json()
    assert body["batch_id"] == MANUAL_BATCH_ID
    assert body["kept"][0]["batch_id"] == MANUAL_BATCH_ID


# ══════════════════════════════════════════════════════════════════════
# ② ref 对不上 / 没给标题
# ══════════════════════════════════════════════════════════════════════


def test_pull_news_reports_a_ref_the_model_never_answered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """漏条 ⇒ 那几条当没挑中并**留下警告**（猜错会把"不值得写"当成"值得写"）。"""
    arm(monkeypatch, scout_reply(verdict("n01", keep=False, rationale="只有赛果")))
    use_news(monkeypatch)

    body = client.post(NEWS_URL).json()
    assert body["ok"] is True
    assert body["evaluated"] == 1
    assert body["kept_count"] == 0
    assert [item["reason"] for item in body["skipped"]] == [
        "只有赛果",
        "模型没给这一条的判定",
        "模型没给这一条的判定",
    ]
    assert "news_ref_missing:n02" in body["warnings"]


def test_pull_news_falls_back_to_the_headline_when_the_model_gives_no_title(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm(monkeypatch, scout_reply(verdict("n01", keep=True, rationale="值得写")))
    use_news(monkeypatch, NEWS[:1])

    body = client.post(NEWS_URL).json()
    assert body["kept"][0]["title"] == "聚能环致一家三口身亡"


def test_pull_news_keeps_the_link_but_drops_the_tracking_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源链接只留 origin + path：真机第一版把 400 字的埋点参数塞进了卡片那一行。"""
    tracked = NewsItemSpec(
        ref="n01",
        title="聚能环致一家三口身亡",
        source="今日头条热榜",
        url="https://www.toutiao.com/trending/123/?log_pb=%7B%22a%22%3A1%7D&style_id=40132",
        heat=921000,
    )
    arm(
        monkeypatch,
        scout_reply(verdict("n01", keep=True, title="4.5 元的聚能环，省出人命", rationale="后果具体")),
    )
    use_news(monkeypatch, [tracked])

    rationale = client.post(NEWS_URL).json()["kept"][0]["rationale"]
    assert rationale.startswith("今日新闻《聚能环致一家三口身亡》（https://www.toutiao.com/trending/123/）：")
    assert "log_pb" not in rationale


# ══════════════════════════════════════════════════════════════════════
# ③ 事件总结（防"写稿照着标题编"的事实源）
# ══════════════════════════════════════════════════════════════════════


def test_the_source_summary_reaches_the_scout_prompt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源站摘要要真的喂进评测提示词 —— 没有它，事件总结只能照着标题猜。"""
    item = NewsItemSpec(
        ref="n01",
        title="未拔充电器，客厅被烧光",
        source="中新网滚动",
        url="https://example.com/a",
        summary="一住户出门没拔充电器，客厅被烧光，无人员伤亡。",
    )
    summary = "一住户出门没拔充电器，客厅被烧光，无人员伤亡。"
    transport = arm(
        monkeypatch,
        scout_reply(
            verdict("n01", keep=True, title="充电器别一直插着", rationale="家家都有", summary=summary)
        ),
    )
    use_news(monkeypatch, [item])

    client.post(NEWS_URL)
    sent = "\n".join(message.content for _, messages in transport.calls for message in messages)
    assert summary in sent


def test_the_event_summary_lands_in_the_grounding_not_the_rationale(
    client: TestClient, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件总结进 ``grounded_on``（依据），**不进** rationale。

    为什么分开放：rationale 是"为什么值得写"（判断），事件总结是"发生了什么"（事实）；
    而且 rationale 的上游契约（``DirectionSpec.rationale``）只有 200 字 —— 两样挤一行，
    长标题一撞上限，这个方向就再也生成不出选题（``direction_spec_from_row`` 会抛）。
    """
    summary = "一家三口在密闭房间用聚能环取暖，三人一氧化碳中毒身亡。"
    arm(
        monkeypatch,
        scout_reply(
            verdict("n01", keep=True, title="4.5 元的聚能环，省出人命", rationale="后果具体", summary=summary)
        ),
    )
    use_news(monkeypatch, NEWS[:1])

    kept = client.post(NEWS_URL).json()["kept"][0]
    assert [(ref["type"], ref["kind"], ref["quote"]) for ref in kept["grounded_on"]] == [
        ("hot", "news", summary)
    ]
    assert "一氧化碳" not in kept["rationale"]

    # 判据落在库上：写稿那一步正是从这一列把事实读回去的（script_service._facts_for）
    stored = json.loads(connection.execute("SELECT grounded_on_json FROM content_directions").fetchone()[0])
    assert [ref["quote"] for ref in stored] == [summary]


def test_insufficient_information_is_not_stored_as_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型说"信息不足" ⇒ **不落依据**：那不是事实，是一句"我不知道"。"""
    arm(
        monkeypatch,
        scout_reply(
            verdict("n01", keep=True, title="标题里看不出发生了什么", rationale="先占位", summary="信息不足")
        ),
    )
    use_news(monkeypatch, NEWS[:1])

    assert client.post(NEWS_URL).json()["kept"][0]["grounded_on"] == []


# ══════════════════════════════════════════════════════════════════════
# ④ 抓取挂了 / 评测挂了
# ══════════════════════════════════════════════════════════════════════


def test_a_dead_source_is_not_an_empty_day(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """抓不到 ⇒ 503 ``NEWS_FETCH_FAILED``：那是"链路没通"，不是"今天没有值得写的"。"""

    async def dead(*, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
        raise StudioError(
            "今日新闻一条都没拉到",
            code=ErrorCode.NEWS_FETCH_FAILED,
            context={"problems": ["今日头条热榜：ConnectError", "中新网滚动：ReadTimeout"]},
        )

    monkeypatch.setattr(news_service, "fetch_news", dead)
    response = client.post(NEWS_URL)
    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.NEWS_FETCH_FAILED.value


def test_a_model_that_never_answers_leaves_no_half_written_directions(
    client: TestClient, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评测没跑出来 ⇒ ``ok=False``，且**库里一个方向都没多**（逐条原因照报）。"""
    arm(monkeypatch, "这不是 JSON")
    use_news(monkeypatch)

    response = client.post(NEWS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"]
    assert body["error_message"]
    assert body["kept"] == []
    assert len(body["skipped"]) == 3
    assert directions_in_db(connection) == []
