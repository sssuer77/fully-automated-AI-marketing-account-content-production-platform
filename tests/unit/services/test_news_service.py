"""新闻抓取（T5.12 · 「一键拉取今日社会新闻」）。

这一层只测**纯解析**与**多源合并**：真正的出网留给人点一次按钮去验（源站今天在不在，
不是单测能替你回答的问题）。所以这里用 ``httpx.MockTransport`` 把响应换掉 —— 测的是
"拿到这份正文会怎么理解"，而不是"今天头条热榜长什么样"。

⚠️ 合并之后**五个源都会被问一遍**（不是"谁先答就用谁"），所以下面那些用例的 handler
必须给**每一个**源一个交代：没列进表里的 URL 一律回 404，而不是让 ``_route`` 抛
``KeyError`` —— 后者会把"这条用例漏配了一个源"伪装成"抓取器崩了"。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.services import news_service
from studio.services.news_service import (
    BILIBILI_URL,
    CHINANEWS_URL,
    DOUYIN_URL,
    KUAISHOU_URL,
    TOUTIAO_URL,
    fetch_news,
    parse_bilibili,
    parse_chinanews,
    parse_douyin,
    parse_kuaishou,
    parse_toutiao,
)

#: 热榜正文（字段名首字母大写，与线上一致）
TOUTIAO_BODY = json.dumps(
    {
        "data": [
            {"Title": "聚能环致一家三口身亡", "HotValue": 921000, "Url": "https://example.com/1"},
            {"Title": "某队客场 2:1 取胜", "HotValue": "410000", "Url": "https://example.com/2"},
            {"Title": "   ", "HotValue": 1, "Url": "https://example.com/3"},
        ]
    },
    ensure_ascii=False,
)

RSS_BODY = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item><title>小区电梯困人 40 分钟</title><link>https://example.com/a</link>
        <description>
　　　　22 日晚，某小区电梯故障，两人被困 40 分钟后被救出。</description>
        <pubDate>Mon, 22 Sep 2026 10:00:00 +0800</pubDate></item>
  <item><title></title><link>https://example.com/b</link></item>
</channel></rss>
"""

#: 抖音热搜榜（真机形态：``word_list`` + 数字 ``hot_value``）
DOUYIN_BODY = json.dumps(
    {
        "active_time": "2026-09-23 16:47:32",
        "status_code": 0,
        "word_list": [
            {"word": "外卖员超时被扣钱", "hot_value": 12088195, "label": 3},
            {"word": "  ", "hot_value": 1},
        ],
    },
    ensure_ascii=False,
)

#: B站热搜（真机形态：``code`` + ``data.trending.list``）
BILIBILI_BODY = json.dumps(
    {
        "code": 0,
        "message": "OK",
        "data": {
            "trending": {
                "title": "bilibili热搜",
                "list": [
                    {"keyword": "35岁门槛该不该取消", "show_name": "35岁门槛", "heat_score": 2150149},
                    {"show_name": "只有 show_name 的条目", "heat_score": 12},
                ],
            }
        },
    },
    ensure_ascii=False,
)

#: 快手热榜（真机形态：``hotValue`` 是**带单位字符串**，置顶条目是 ``null``）
KUAISHOU_BODY = json.dumps(
    {
        "data": {
            "visionHotRank": {
                "result": 1,
                "items": [
                    {"id": "x1", "name": "租房押金难退", "hotValue": "1271.3万"},
                    {"id": "x2", "name": "置顶的官方条目", "hotValue": None},
                ],
            }
        }
    },
    ensure_ascii=False,
)


class _StubHttpx:
    """只实现 :func:`fetch_news` 用到的那两样：``AsyncClient`` 与 ``Timeout``。

    换掉**整个模块对象**而不是 ``httpx.AsyncClient``：后者是全局的，改它会影响同一进程里
    别的库（monkeypatch 会还原，但"这期间还有谁在用 httpx"没人说得清）。这里的替身只在
    本模块的命名空间里生效，作用域就是这两个函数。
    """

    Timeout = httpx.Timeout

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def AsyncClient(self, **kwargs: Any) -> httpx.AsyncClient:  # noqa: N802 —— 顶替的是 httpx 的类名
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler), **kwargs)


def _route(by_url: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    """按 URL 分派；**没列进去的源回 404**（见模块 docstring：别把漏配伪装成崩溃）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return by_url.get(str(request.url), httpx.Response(404, text="not configured"))

    return handler


class TestParseToutiao:
    def test_normalizes_and_numbers_the_items(self) -> None:
        items = parse_toutiao(TOUTIAO_BODY)
        assert [item.ref for item in items] == ["n01", "n02"]
        assert items[0].title == "聚能环致一家三口身亡"
        assert items[0].source == "今日头条热榜"
        assert items[0].url == "https://example.com/1"
        assert items[0].heat == 921000

    def test_heat_may_arrive_as_a_string(self) -> None:
        assert parse_toutiao(TOUTIAO_BODY)[1].heat == 410000

    def test_blank_titles_are_dropped(self) -> None:
        assert len(parse_toutiao(TOUTIAO_BODY)) == 2

    def test_limit_is_respected(self) -> None:
        assert len(parse_toutiao(TOUTIAO_BODY, 1)) == 1

    def test_the_hot_board_gives_no_summary(self) -> None:
        """热榜只有标题 / 热度 / 链接（2026-09-23 逐字段核过）⇒ 摘要留空。

        这里**不**拿标题去凑一段摘要：那等于把幻觉提前到抓取这一步，而下游会把它当成
        源站说过的话。宁可空着，让模型自己老实写"信息不足"。
        """
        assert parse_toutiao(TOUTIAO_BODY)[0].summary == ""

    def test_a_response_without_data_is_not_a_silent_empty_list(self) -> None:
        with pytest.raises(TypeError):
            parse_toutiao(json.dumps({"message": "ok"}))


class TestParseChinanews:
    def test_reads_the_rss_items(self) -> None:
        items = parse_chinanews(RSS_BODY)
        assert [item.ref for item in items] == ["n01"]
        assert items[0].title == "小区电梯困人 40 分钟"
        assert items[0].source == "中新网滚动"
        assert items[0].heat is None

    def test_the_description_becomes_the_summary(self) -> None:
        """``description`` 是源站自己写的正文首段 ⇒ 当**事实源**（事件总结的依据）。

        RSS 里它是多行缩进的，换行与全角空格要折掉 —— 原样带进提示词等于让模型读一堆
        空白，而"这一条没有摘要"与"摘要是一堆空格"在下游是两种完全不同的信号。
        """
        assert parse_chinanews(RSS_BODY)[0].summary == "22 日晚，某小区电梯故障，两人被困 40 分钟后被救出。"

    def test_a_missing_description_is_an_empty_summary(self) -> None:
        """没有 ``description`` ⇒ 空串（不是 ``None``，也不是拿标题凑一段）。"""
        body = '<rss version="2.0"><channel><item><title>只有标题</title></item></channel></rss>'
        assert parse_chinanews(body)[0].summary == ""

    def test_html_is_not_a_feed(self) -> None:
        """真 HTML（不闭合标签）在 ``fromstring`` 这一步就报错 —— 这正是想要的降级信号。"""
        with pytest.raises(ET.ParseError):
            parse_chinanews("<html><body>不是 feed<br></body></html>")

    def test_well_formed_but_itemless_is_an_empty_list(self) -> None:
        """能解析但没有 ``item`` ⇒ 空列表。

        "这一源今天没内容"不是异常：要不要换源是 :func:`fetch_news` 的决定（它把空列表
        与解析失败一视同仁）。
        """
        assert parse_chinanews("<rss><channel></channel></rss>") == []


class TestParseDouyin:
    def test_reads_the_word_list(self) -> None:
        items = parse_douyin(DOUYIN_BODY)
        assert [item.ref for item in items] == ["n01"]
        assert items[0].title == "外卖员超时被扣钱"
        assert items[0].source == "抖音热搜"
        assert items[0].heat == 12088195

    def test_blank_words_are_dropped(self) -> None:
        assert len(parse_douyin(DOUYIN_BODY)) == 1

    def test_the_hot_search_gives_no_summary(self) -> None:
        """热搜只有一句词 + 热度 ⇒ 摘要留空（同头条热榜那条判据）。"""
        assert parse_douyin(DOUYIN_BODY)[0].summary == ""

    def test_a_response_without_the_word_list_is_not_a_silent_empty_list(self) -> None:
        with pytest.raises(TypeError):
            parse_douyin(json.dumps({"status_code": 0}))

    def test_limit_is_respected(self) -> None:
        body = json.dumps({"word_list": [{"word": "甲"}, {"word": "乙"}]})
        assert [item.title for item in parse_douyin(body, 1)] == ["甲"]


class TestParseBilibili:
    def test_reads_the_trending_list(self) -> None:
        items = parse_bilibili(BILIBILI_BODY)
        assert [item.ref for item in items] == ["n01", "n02"]
        assert items[0].title == "35岁门槛该不该取消"
        assert items[0].source == "B站热搜"
        assert items[0].heat == 2150149

    def test_falls_back_to_show_name(self) -> None:
        """只有 ``show_name`` 的条目照样收 —— 它是展示名，与 ``keyword`` 同一个意思。"""
        assert parse_bilibili(BILIBILI_BODY)[1].title == "只有 show_name 的条目"

    def test_a_non_zero_code_is_not_read_as_a_shape_problem(self) -> None:
        """⚠️ B站把"风控拦了"也回 **200**，只在体内写一个非零 ``code``。

        不认这个字段的话，``data`` 是 ``None`` ⇒ 报"没有 list 数组"，而那句话会把人引去
        查结构，真正的原因却是"这次请求被拒了" —— 两种失败的处置完全不同。
        """
        body = json.dumps({"code": -412, "message": "请求被拦截", "data": None})
        with pytest.raises(ValueError, match="-412"):
            parse_bilibili(body)

    def test_a_response_without_the_list_is_not_a_silent_empty_list(self) -> None:
        with pytest.raises(TypeError):
            parse_bilibili(json.dumps({"code": 0, "data": {"trending": {}}}))


class TestParseKuaishou:
    def test_reads_the_rank_items(self) -> None:
        items = parse_kuaishou(KUAISHOU_BODY)
        assert [item.ref for item in items] == ["n01", "n02"]
        assert items[0].title == "租房押金难退"
        assert items[0].source == "快手热榜"

    def test_the_unit_suffix_becomes_a_number(self) -> None:
        """``"1271.3万"`` ⇒ ``12713000``。

        真机实测：快手给的是**带单位的字符串**。拿 ``int()`` 硬转的话，整家源都会被记成
        "解析失败" —— 而它明明给了热度。
        """
        assert parse_kuaishou(KUAISHOU_BODY)[0].heat == 12713000

    def test_a_null_heat_does_not_kill_the_item(self) -> None:
        """置顶的官方条目 ``hotValue`` 就是 ``null``：它照样是内容，不能因为读不出热度就丢。"""
        assert parse_kuaishou(KUAISHOU_BODY)[1].heat is None

    def test_a_response_without_the_rank_is_not_a_silent_empty_list(self) -> None:
        with pytest.raises(TypeError):
            parse_kuaishou(json.dumps({"data": {"visionHotRank": {}}}))


def _table(**bodies: str) -> dict[str, httpx.Response]:
    """五个源各回一份 200（缺省用上面那几份真机形态的样本）。

    返回**表**而不是 handler：个别用例只想换掉其中一家（让它 500 / 改版），拿表改一行
    比再写一遍五个源安全 —— 漏配一个源会被 ``_route`` 记成 404，而那看起来就像"这家源
    今天挂了"，用例还是会绿（只是绿在了错的地方）。
    """
    table = {
        TOUTIAO_URL: bodies.get("toutiao", TOUTIAO_BODY),
        CHINANEWS_URL: bodies.get("chinanews", RSS_BODY),
        DOUYIN_URL: bodies.get("douyin", DOUYIN_BODY),
        BILIBILI_URL: bodies.get("bilibili", BILIBILI_BODY),
        KUAISHOU_URL: bodies.get("kuaishou", KUAISHOU_BODY),
    }
    return {url: httpx.Response(200, text=text) for url, text in table.items()}


def _all_sources(**bodies: str) -> Callable[[httpx.Request], httpx.Response]:
    """五个源各回一份正文。"""
    return _route(_table(**bodies))


class TestFetchNews:
    async def test_asks_every_source_and_merges_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 扩源的核心：五个源**全问一遍**，谁都没被跳过（旧语义是"谁先答就用谁"）。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=TOUTIAO_BODY)

        monkeypatch.setattr(news_service, "httpx", _StubHttpx(handler))
        items = await fetch_news()
        assert seen == [TOUTIAO_URL, CHINANEWS_URL, DOUYIN_URL, BILIBILI_URL, KUAISHOU_URL]
        assert items[0].title == "聚能环致一家三口身亡"

    async def test_the_five_sources_are_interleaved_not_concatenated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 轮转交错：前五条各来自一家。

        按源拼接的话，``limit`` 一截断就只剩头条那几十条，后四家一条都进不来 —— 扩源
        等于白扩。这条用例钉的就是"截断之前已经交错好了"。
        """
        monkeypatch.setattr(news_service, "httpx", _StubHttpx(_all_sources()))
        items = await fetch_news()
        assert [item.source for item in items[:5]] == [
            "今日头条热榜",
            "中新网滚动",
            "抖音热搜",
            "B站热搜",
            "快手热榜",
        ]

    async def test_the_same_event_on_two_platforms_is_kept_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 跨源去重（真机实测：抖音与B站同一天都上「今日秋分」）。"""
        monkeypatch.setattr(
            news_service,
            "httpx",
            _StubHttpx(
                _all_sources(
                    douyin=json.dumps({"word_list": [{"word": "今日秋分", "hot_value": 1}]}),
                    bilibili=json.dumps(
                        {"code": 0, "data": {"trending": {"list": [{"keyword": "今日秋分"}]}}}
                    ),
                )
            ),
        )
        titles = [item.title for item in await fetch_news()]
        assert titles.count("今日秋分") == 1

    async def test_a_duplicate_with_a_summary_replaces_the_bare_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """命中重复时留**信息更多**的那一条：有摘要的顶掉光标题的。

        中新网那一条背后有源站写的正文首段，事件总结就能站在事实上；抖音那条只有一句词，
        总结只能是"信息不足"。同一件事，两条路的质量差在这里。
        """
        rss = (
            '<rss version="2.0"><channel><item><title>今日秋分</title>'
            "<description>22 日，某地举办秋分活动。</description></item></channel></rss>"
        )
        monkeypatch.setattr(
            news_service,
            "httpx",
            _StubHttpx(
                _all_sources(
                    toutiao=json.dumps({"data": [{"Title": "今日秋分"}]}),
                    chinanews=rss,
                    douyin=json.dumps({"word_list": []}),
                    bilibili=json.dumps({"code": 0, "data": {"trending": {"list": []}}}),
                    kuaishou=json.dumps({"data": {"visionHotRank": {"items": []}}}),
                )
            ),
        )
        items = await fetch_news()
        assert [item.title for item in items] == ["今日秋分"]
        assert items[0].source == "中新网滚动"
        assert items[0].summary == "22 日，某地举办秋分活动。"

    async def test_one_dead_source_does_not_take_the_others_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单源挂掉只是"少一家"，另外四家照常合并。"""
        table = _table()
        table[DOUYIN_URL] = httpx.Response(500, text="boom")
        monkeypatch.setattr(news_service, "httpx", _StubHttpx(_route(table)))
        items = await fetch_news()
        assert "抖音热搜" not in {item.source for item in items}
        assert {"今日头条热榜", "中新网滚动", "B站热搜", "快手热榜"} <= {item.source for item in items}

    async def test_a_source_that_changes_shape_is_just_a_missing_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """改版（200 但不是我们认的形状）与 500 是同一类事：少一家，不中断。"""
        table = _table()
        table[TOUTIAO_URL] = httpx.Response(200, text="<html>改版了</html>")
        monkeypatch.setattr(news_service, "httpx", _StubHttpx(_route(table)))
        items = await fetch_news()
        assert "今日头条热榜" not in {item.source for item in items}
        assert items

    async def test_reports_every_source_when_all_of_them_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            news_service,
            "httpx",
            _StubHttpx(lambda request: httpx.Response(503, text="down")),
        )
        with pytest.raises(StudioError) as caught:
            await fetch_news()
        assert caught.value.code is ErrorCode.NEWS_FETCH_FAILED
        assert len(caught.value.context["problems"]) == 5

    async def test_limit_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(news_service, "httpx", _StubHttpx(_all_sources()))
        assert len(await fetch_news(limit=2)) == 2

    async def test_refs_are_renumbered_after_the_merge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ref`` 在**合并之后**才编号 —— 否则去重一砍，锚点就会带空洞。"""
        monkeypatch.setattr(news_service, "httpx", _StubHttpx(_all_sources()))
        items = await fetch_news()
        assert [item.ref for item in items] == [f"n{index:02d}" for index in range(1, len(items) + 1)]
