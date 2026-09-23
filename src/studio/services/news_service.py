"""今日新闻抓取（T5.12 · 「一键拉取今日社会新闻」）。

五个源，**合并**而不是"首个成功即返回"（2026-09-23 扩源）
------------------------------------------------------
原先只有两个源（今日头条热榜 / 中新网滚动），且语义是"谁先答就用谁" —— 那是**降级**：
第二家只在第一家挂掉时才被问一次。用户要的是**宽度**（「新闻拉取渠道再加上短视频平台
热搜的社会问题，增加信息渠道的宽度」），所以这里改成**全问一遍、再合并**。

- 新闻侧（带正文首段的）：中新网滚动（RSS · ``description`` 就是正文第一段）、今日头条热榜。
- 短视频平台侧（只有标题 + 热度）：抖音热搜、B站热搜、快手热榜 —— 它们回答的是
  "今天平台上大家在吵什么"，与新闻侧**不是同一批题**（同一个事件往往只在其中一侧上榜）。

五家逐个实测过（2026-09-23）：抖音 / B站 / 快手 / 头条 / 中新网都拿得到结构化数据；
微博热搜 403、知乎热榜 401、小红书 404 —— 抓一个"今天不一定还活着"的源，不如把这五个
确定的写死。顺序 = 优先级（同一条标题在多处上榜时，靠前的那一条胜出）。

合并的代价用两道闸压住
----------------------
① **轮转交错**（``_interleave``）：五家各取第 1 条、再各取第 2 条…… 于是 ``limit=50``
   落在五家头上大约是每家的前 10 条，而不是"头条 50 条 + 其余四家一条没有"。宽度是
   用户要的，深度不是。
② **跨源去重**（``_dedupe``）：同一个事件在抖音与 B站同时上榜是常态（真机实测：
   「U23国足小组第一出线」与「亚运U23国足小组第一出线」）。不去重的话，50 个名额会被
   同几件事占掉，模型也会为同一件事写两条方向 —— 而每一条方向都要花一次写稿的钱。
   命中时**留信息更多的那一条**（有摘要的胜过光标题），所以中新网那一条会顶掉抖音那条。

为什么源内失败降级、源间失败报错
--------------------------------
单个源挂了（改版 / 限流 / 网络抖）不该让整颗按钮变砖：另外四家照常合并。**五家全挂** ⇒
抛 ``NEWS_FETCH_FAILED`` —— 那是"今天这条链路真的没通"，必须让人看见，而不是回一个空
列表让他以为"今天没有新闻"（陷阱 #207）。部分源挂掉时逐条 ``logger.warning`` 留痕：
``source`` 那一列本来就写着每一条来自哪家，所以"今天少了两家"在面板上是看得见的。

为什么不落盘
------------
新闻是**一次性输入**：点一次、评测一次，留下的产物是**方向**（落库）。顺手落一份
``data/hot/news-*.md`` 会把新闻塞进 ``hot_items``，被下一次 Planner 当热点再消费一遍 ——
那是用户没要求过的副作用。

为什么不做日期过滤
------------------
五个源本身就是"此刻 / 今日滚动"的性质（热榜实时、滚动新闻按时间倒序）。再按 ``pubDate``
切一刀要处理时区与跨零点，而收益只是把昨天夜里那几条挡掉 —— 值不值得写本来就由模型判。

为什么带上了摘要（而标题照旧是第一身份）
----------------------------------------
评测要产出一条"事件总结"（谁 / 何时 / 何地 / 数字 / 结果），而**只有标题**时模型只能照着
标题猜 —— 猜出来的东西会被写稿环节当成事实用，那是幻觉的起点。中新网 RSS 的
``description`` 就是正文第一段，源站自己写的，直接拿来当事实源（2026-09-23 实测：
每一条都带）。四个热榜**没有**这个字段（标题 + 热度而已），所以那里照旧留空 —— 宁可让
模型说"信息不足"，也不拿标题去凑一段摘要。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Final, Protocol

import httpx

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.domain.topics import (
    DEDUP_SIMILARITY_THRESHOLD,
    NEWS_SUMMARY_MAX,
    NewsItemSpec,
    normalize_title,
    similarity,
)

__all__ = [
    "BILIBILI_URL",
    "CHINANEWS_URL",
    "DOUYIN_URL",
    "KUAISHOU_URL",
    "NEWS_LIMIT",
    "NEWS_SOURCE_LABELS",
    "NEWS_TIMEOUT_SEC",
    "TOUTIAO_URL",
    "NewsFetcherLike",
    "NewsParser",
    "fetch_news",
    "parse_bilibili",
    "parse_chinanews",
    "parse_douyin",
    "parse_kuaishou",
    "parse_toutiao",
]

logger = get_logger("studio.services.news")

#: 一次拉多少条（**合并之后**的总数，不是每个源各拉这么多）
NEWS_LIMIT: Final[int] = 50

#: 单源超时（五个源串行 ⇒ 最坏约 5×；这是"点一下按钮"的尺度）
NEWS_TIMEOUT_SEC: Final[float] = 10.0

TOUTIAO_URL: Final[str] = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
CHINANEWS_URL: Final[str] = "https://www.chinanews.com.cn/rss/scroll-news.xml"
DOUYIN_URL: Final[str] = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
BILIBILI_URL: Final[str] = "https://api.bilibili.com/x/web-interface/search/square?limit=50"
KUAISHOU_URL: Final[str] = "https://www.kuaishou.com/graphql"

#: 源的显示名（落进 ``NewsItemSpec.source`` 与日志，**不写 URL**：日志里没人想读长链接）
NEWS_SOURCE_LABELS: Final[Mapping[str, str]] = {
    "toutiao": "今日头条热榜",
    "chinanews": "中新网滚动",
    "douyin": "抖音热搜",
    "bilibili": "B站热搜",
    "kuaishou": "快手热榜",
}

#: 浏览器 UA：这几个源对默认的 ``python-httpx`` UA 不友好（微博 403 / 知乎 401 也是这么来的）
_HEADERS: Final[Mapping[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

#: 快手热榜只有一个 graphql 入口（GET 会回 ``400 GET query missing``），所以要 POST
_KUAISHOU_QUERY: Final[Mapping[str, object]] = {
    "operationName": "visionHotRankQuery",
    "variables": {"page": "home", "webPageArea": "pc_home_hot_rank"},
    "query": (
        "query visionHotRankQuery($page: String, $webPageArea: String) {"
        " visionHotRank(page: $page, webPageArea: $webPageArea) {"
        " result items { id name hotValue } } }"
    ),
}

#: 解析一个源的响应正文 ⇒ 归一化的新闻条目（``ref`` 由 :func:`_with_refs` 补）
NewsParser = Callable[[str, int], list[NewsItemSpec]]


class NewsFetcherLike(Protocol):
    """抓取器的结构类型（:func:`fetch_news` 满足它；测试注入假件用）。"""

    async def __call__(self, *, limit: int) -> list[NewsItemSpec]: ...


@dataclass(frozen=True, slots=True)
class _Source:
    """一个源：怎么问、拿到正文怎么读。

    ``payload`` 非空 ⇒ 走 POST（快手那个 graphql 入口）。方法与载荷一起写在**源的定义**里，
    而不是在 :func:`fetch_news` 里按 key 分支 —— 加第六个源时只该动这一张表。
    """

    key: str
    url: str
    parser: NewsParser
    payload: Mapping[str, object] | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)


def parse_toutiao(text: str, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
    """解析今日头条热榜 JSON（``data[].Title / HotValue / Url``）。

    字段名是**首字母大写**的（服务端结构体的默认序列化），少一个大写就静默拿到空列表
    —— 所以取字段统一走 :func:`_first`，两种拼法都认。

    **不给摘要**：这一份响应里只有 ``Title`` / ``HotValue`` / ``Url``（2026-09-23 逐字段
    核过），没有正文也没有首段。热榜的 ``Url`` 是个话题聚合页，为了一条摘要去多打一次
    请求、还得解析别人的 HTML —— 那是"抓一个随时会改版的页面"，收益却只是把
    "信息不足"换成一段可能过时的转述。所以这里 ``summary`` 留空，由模型自己老实说。
    """
    rows = _rows(text, "data", label="今日头条热榜")
    items: list[NewsItemSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean(_first(row, "Title", "title"))
        if not title:
            continue
        items.append(
            NewsItemSpec(
                ref="",
                title=title,
                source=NEWS_SOURCE_LABELS["toutiao"],
                url=_clean(_first(row, "Url", "url")),
                heat=_int_or_none(_first(row, "HotValue", "hotValue")),
            )
        )
        if len(items) >= limit:
            break
    return _with_refs(items)


def parse_chinanews(text: str, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
    """解析中新网滚动 RSS（``channel/item`` 的 ``title`` / ``link``）。

    只认标准 RSS 结构：HTML 伪装成 feed（36kr 那种）会在 ``ET.fromstring`` 这一步直接
    报错，于是自动降级到下一个源 —— 这正是想要的，**解析不了就别硬猜**。
    """
    root = ET.fromstring(text)
    items: list[NewsItemSpec] = []
    for node in root.iter("item"):
        title = _clean(node.findtext("title"))
        if not title:
            continue
        items.append(
            NewsItemSpec(
                ref="",
                title=title,
                source=NEWS_SOURCE_LABELS["chinanews"],
                url=_clean(node.findtext("link")),
                heat=None,
                summary=_clean_summary(node.findtext("description")),
            )
        )
        if len(items) >= limit:
            break
    return _with_refs(items)


def parse_douyin(text: str, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
    """解析抖音热搜榜 JSON（``word_list[].word / hot_value``）。

    真机形态（2026-09-23）：``{"active_time": …, "status_code": 0, "word_list": [{"word":
    "…", "hot_value": 12088119, "label": 3}, …]}`` —— 50 条，``hot_value`` 是数字。
    ``label`` 是"新 / 热 / 爆"那一档角标，这里不取：它是**展示**用的，判"值不值得写"
    用不上，而多一个字段就多一处会随平台改版失效的地方。
    """
    rows = _rows(text, "word_list", label="抖音热搜")
    items: list[NewsItemSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean(_first(row, "word", "Word"))
        if not title:
            continue
        items.append(
            NewsItemSpec(
                ref="",
                title=title,
                source=NEWS_SOURCE_LABELS["douyin"],
                url="",
                heat=_int_or_none(_first(row, "hot_value", "hotValue")),
            )
        )
        if len(items) >= limit:
            break
    return _with_refs(items)


def parse_bilibili(text: str, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
    """解析 B站热搜 JSON（``data.trending.list[].keyword / heat_score``）。

    真机形态（2026-09-23）：``{"code": 0, "data": {"trending": {"list": [{"keyword": "…",
    "show_name": "…", "heat_score": 2150149}, …]}}}`` —— 50 条。

    ``code != 0`` **要当场报错**：B站把"风控拦了"也回 **200**，只在体内写一个非零 ``code``。
    不看这个字段的话，``data`` 是 ``None`` ⇒ 走到 ``_rows`` 报"没有 list 数组"，而那句话
    会把人引去查选择器/结构，真正的原因却是"这次请求被拒了"。两种失败的处置完全不同。
    """
    payload: object = json.loads(text)
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0):
            raise ValueError(f"B站热搜回了 code={code}（{_clean(payload.get('message'))}）")
    data = payload.get("data") if isinstance(payload, dict) else None
    trending = data.get("trending") if isinstance(data, dict) else None
    rows = trending.get("list") if isinstance(trending, dict) else None
    if not isinstance(rows, list):
        raise TypeError("B站热搜响应里没有 data.trending.list 数组")

    items: list[NewsItemSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean(_first(row, "keyword", "show_name", "Keyword"))
        if not title:
            continue
        items.append(
            NewsItemSpec(
                ref="",
                title=title,
                source=NEWS_SOURCE_LABELS["bilibili"],
                url="",
                heat=_int_or_none(_first(row, "heat_score", "heatScore")),
            )
        )
        if len(items) >= limit:
            break
    return _with_refs(items)


def parse_kuaishou(text: str, limit: int = NEWS_LIMIT) -> list[NewsItemSpec]:
    """解析快手热榜 JSON（graphql：``data.visionHotRank.items[].name / hotValue``）。

    真机形态（2026-09-23）：``{"data": {"visionHotRank": {"result": 1, "items": [{"id":
    "…", "name": "今日秋分", "hotValue": "1271.3万"}, …]}}}``。

    ⚠️ 这里的 ``hotValue`` 是**带单位的字符串**（``"1271.3万"``），也见过 ``null``
    （置顶的官方条目没有热度）⇒ 走 :func:`_hot_count`，读不出来就留 ``None``。拿
    ``int()`` 硬转的话，一条置顶条目就会让整家源被记成"解析失败"。
    """
    payload: object = json.loads(text)
    data = payload.get("data") if isinstance(payload, dict) else None
    rank = data.get("visionHotRank") if isinstance(data, dict) else None
    rows = rank.get("items") if isinstance(rank, dict) else None
    if not isinstance(rows, list):
        raise TypeError("快手热榜响应里没有 data.visionHotRank.items 数组")

    items: list[NewsItemSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean(_first(row, "name", "Name"))
        if not title:
            continue
        items.append(
            NewsItemSpec(
                ref="",
                title=title,
                source=NEWS_SOURCE_LABELS["kuaishou"],
                url="",
                heat=_hot_count(_first(row, "hotValue", "hot_value")),
            )
        )
        if len(items) >= limit:
            break
    return _with_refs(items)


#: 抓取顺序 = 优先级（同一条标题在多处上榜时，靠前的那一条胜出）
_SOURCES: Final[tuple[_Source, ...]] = (
    _Source("toutiao", TOUTIAO_URL, parse_toutiao),
    _Source("chinanews", CHINANEWS_URL, parse_chinanews),
    _Source("douyin", DOUYIN_URL, parse_douyin),
    _Source("bilibili", BILIBILI_URL, parse_bilibili),
    _Source(
        "kuaishou",
        KUAISHOU_URL,
        parse_kuaishou,
        payload=_KUAISHOU_QUERY,
        extra_headers={
            "Content-Type": "application/json",
            "Origin": "https://www.kuaishou.com",
            "Referer": "https://www.kuaishou.com/",
        },
    ),
)


async def fetch_news(*, limit: int = NEWS_LIMIT, timeout_sec: float = NEWS_TIMEOUT_SEC) -> list[NewsItemSpec]:
    """把五个源各问一遍、合并成一份清单（全挂 ⇒ ``NEWS_FETCH_FAILED``）。

    **不抛裸异常**：单个源的失败（超时 / 403 / 改版 / 一条都没解析出来）都只是"少一家"
    的理由，只有五家全部试完、一条都没拿到，才把每一家的原因一起报出来 —— 报"都挂了"
    时，用户需要知道的正是这几句原因。

    合并顺序见 :func:`_interleave`，去重见 :func:`_dedupe`。
    """
    problems: list[str] = []
    groups: list[list[NewsItemSpec]] = []
    answered: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec), follow_redirects=True) as client:
        for source in _SOURCES:
            label = NEWS_SOURCE_LABELS[source.key]
            try:
                response = await _request(client, source)
                response.raise_for_status()
                items = source.parser(response.text, limit)
            except Exception as exc:  # 超时 / HTTP 状态 / 解析失败：都只是"少一家"
                reason = f"{type(exc).__name__} {exc}"
                problems.append(f"{label}：{reason}")
                logger.warning("news.source_failed", source=source.key, error=reason)
                continue
            if not items:
                problems.append(f"{label}：一条都没解析出来")
                logger.warning("news.source_empty", source=source.key)
                continue
            answered.append(label)
            groups.append(items)

    if not groups:
        raise StudioError(
            "今日新闻一条都没拉到",
            code=ErrorCode.NEWS_FETCH_FAILED,
            context={"sources": list(NEWS_SOURCE_LABELS.values()), "problems": problems},
            remediation="检查本机能不能出网；也可以把新闻直接粘进「输入源 · 粘贴」",
        )

    merged = _dedupe(_interleave(groups))[:limit]
    logger.info(
        "news.fetched",
        sources=answered,
        fetched=len(merged),
        candidates=sum(len(group) for group in groups),
        problems=problems,
    )
    return _with_refs(merged)


async def _request(client: httpx.AsyncClient, source: _Source) -> httpx.Response:
    """按源的定义发一次请求（``payload`` 非空 ⇒ POST）。"""
    headers = {**_HEADERS, **source.extra_headers}
    if source.payload is None:
        return await client.get(source.url, headers=headers)
    return await client.post(source.url, json=source.payload, headers=headers)


def _interleave(groups: Sequence[Sequence[NewsItemSpec]]) -> list[NewsItemSpec]:
    """轮转交错：五家各取第 1 条、再各取第 2 条……

    为什么不是"按源拼接"：``limit`` 会在合并之后截断，而按源拼接的截断结果是"头条那 50
    条"—— 后四家一条都进不来，扩源等于白扩。交错之后，同一份 ``limit`` 落在五家头上
    大致是每家的前 1/5，这才是用户要的**宽度**。
    """
    return [item for row in zip_longest(*groups) for item in row if item is not None]


def _dedupe(items: Iterable[NewsItemSpec]) -> list[NewsItemSpec]:
    """跨源去重（同一个事件在几处同时上榜是常态）。

    两条判据，与选题那套**同一份**（§04.1.3：归一化完全命中 / 相似度 ≥ 0.85）：
    ① 归一化后**逐字相同** ⇒ 同一条；
    ② 相似度 ≥ :data:`DEDUP_SIMILARITY_THRESHOLD` ⇒ 同一个事件（真机实测：「U23国足
       小组第一出线」与「亚运U23国足小组第一出线」相似度 0.92）。

    命中时**留信息更多的那一条**：有摘要的顶掉光标题的。所以中新网那条会赢过抖音那条 ——
    事件总结能站在源站写的正文首段上，而不是"信息不足"。

    ⚠️ 刻意**不用** :func:`hash_title`：它把数字折成 ``#``（"5 个技巧"与"7 个技巧"同键），
    那是给"选题模板"去重用的；新闻标题里的数字是**事实**，折掉会把两件不同的事并成一件。
    """
    kept: list[NewsItemSpec] = []
    for item in items:
        index = _duplicate_index(kept, item)
        if index is None:
            kept.append(item)
        elif not kept[index].summary and item.summary:
            kept[index] = item
    return kept


def _duplicate_index(kept: Sequence[NewsItemSpec], item: NewsItemSpec) -> int | None:
    """``item`` 是不是已经收过（是 ⇒ 返回那一条的下标）。"""
    folded = normalize_title(item.title)
    for index, existing in enumerate(kept):
        if normalize_title(existing.title) == folded:
            return index
        if similarity(existing.title, item.title) >= DEDUP_SIMILARITY_THRESHOLD:
            return index
    return None


def _rows(text: str, key: str, *, label: str) -> list[object]:
    """从 JSON 正文里取一个**必须是数组**的顶层字段。

    形状不对就抛 ``TypeError``：那与 500 是同一类事（"这一家今天给不了我们认的东西"），
    由 :func:`fetch_news` 统一记成"少一家"。**不返回空列表** —— 空列表会被读成
    "这一家今天没内容"，而真正发生的是"它改版了 / 它把我们拦了"。
    """
    payload: object = json.loads(text)
    rows = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise TypeError(f"{label}响应里没有 {key} 数组")
    return rows


def _first(row: Mapping[str, object], *keys: str) -> object:
    """取第一个非空字段（大小写两种拼法都试，见 :func:`parse_toutiao`）。"""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _clean(value: object) -> str:
    """转字符串 + 去空白（``None`` ⇒ 空串）。"""
    return "" if value is None else str(value).strip()


def _clean_summary(value: object) -> str:
    """摘要归一化：折掉换行与缩进（RSS 的 ``description`` 是多行缩进的）。

    截到 :data:`NEWS_SUMMARY_MAX`：源站给的是**第一段**，正常几十到一百多字；真遇到
    一整篇的（改版 / 别家源），带进提示词也只是白烧 token —— 事件总结要的是首段事实，
    不是全文。
    """
    return " ".join(_clean(value).split())[:NEWS_SUMMARY_MAX]


def _int_or_none(value: object) -> int | None:
    """热度取整（``"1234567"`` / ``1234567`` 都认；非数字 ⇒ ``None``）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _hot_count(value: object) -> int | None:
    """带单位的热度（快手：``"1271.3万"`` / ``"1.2亿"``）⇒ 整数；读不出来 ⇒ ``None``。

    置顶的官方条目 ``hotValue`` 就是 ``null`` —— 那一条照样是内容，不能因为读不出热度
    就把它（或整家源）判掉。
    """
    text = _clean(value)
    if not text:
        return None
    units = (("亿", 100_000_000), ("万", 10_000))
    for suffix, scale in units:
        if text.endswith(suffix):
            number = _float_or_none(text[: -len(suffix)])
            return None if number is None else int(number * scale)
    return _int_or_none(text)


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _with_refs(items: Sequence[NewsItemSpec]) -> list[NewsItemSpec]:
    """补上回抄锚点（``n01`` / ``n02`` …）。

    锚点在**抓取侧**生成，而不是用新闻自带的 id / URL：标题会重复、URL 带查询串，让模型
    对着它们回抄，等于把"对齐"这件事赌在模型会不会改写字符串上。
    """
    return [item.model_copy(update={"ref": f"n{index:02d}"}) for index, item in enumerate(items, start=1)]
