"""选择器包（T5.2 · §06.5.2「选择器集中化」· R13 可热修）。

为什么选择器是**数据**（yaml）而不是代码里的常量
------------------------------------------------
平台改版是**常态**（R13 的原文就是"页面改版导致选择器失效"）。选择器写在
``platforms/douyin.py`` 里时，修一个 CSS 选择器要：改代码 → 跑门禁 → 重启 worker；
写在 yaml 里时只要改文件 + 重启 worker（选择器在装配期读一次）。而
``selectors_version`` 会随这次改版一起进 ``publications.evidence_json`` ——
事后能回答"这条是哪个版本的选择器发的"。

**本仓库里这些选择器是未实测的初稿**
------------------------------------
T5.2 的验收是"演练链路跑通"，而真机校准需要**一个已登录的真实账号**
（§06.2.2 也把"实测校准"写成了 T5.2 的现场动作）。所以：
- 结构（要哪些键、怎么校验）是这里定的，**这一层是可靠的**；
- 具体的 CSS 值是按各平台创作页的公开结构写的**初稿**，第一次真机跑必然会改；
- 改法见 ``docs/runbook/publish_selector.md``（改 yaml、不动代码）。

把这件事写在模块开头，是因为"选择器看起来挺像那么回事"最容易让人以为它验过了。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from studio.core.errors import ErrorCode, PublishError

__all__ = [
    "DEFAULT_READBACK",
    "METRIC_KEYS",
    "POST_ID_PLACEHOLDER",
    "READBACK_KINDS",
    "REQUIRED_SELECTORS",
    "SELECTOR_KEY_GROUPS",
    "SelectorPack",
    "load_selector_pack",
    "selector_root",
]

#: 选择器 yaml 的目录（随包走，不随 ``STUDIO_HOME`` 走）。
_SELECTOR_DIRNAME: Final[str] = "selectors"

#: **必须有**的选择器。缺一个就装不起来 —— 装起来再在真机上以
#: ``PUBLISH_SELECTOR_MISS`` 收场，等于把"配置写错了"推迟成"发不出去"。
REQUIRED_SELECTORS: Final[tuple[str, ...]] = (
    "login_ok",  # 已登录的标志元素（health 用）
    "login_required",  # 未登录的标志元素（health 用）
    "upload_input",  # 文件输入框（八步第 ② 步）
    "title_input",  # 标题输入框（第 ③ 步）
    "publish_button",  # 发布按钮（第 ⑥ 步，dry-run **不点**）
)

#: 可选选择器：缺了只是少做一步，不该拦下整条链路。
OPTIONAL_SELECTORS: Final[tuple[str, ...]] = (
    "caption_input",  # 文案框；有些平台标题与文案是同一个框
    "upload_progress",  # 上传进度元素（轮询百分比）
    "cover_trigger",  # 打开"设置封面"弹层的按钮（第 ④ 步）
    "cover_input",  # 弹层里的图片输入框（第 ④ 步）
    "success_marker",  # 结果页标志（第 ⑦ 步）
    "reject_marker",  # 审核不通过的标志（§06.10 的 PUBLISH_REVIEW_REJECTED）
    # ── 数据回收（T5.4 · §06.6）───────────────────────────────────────
    # 这一组**全是可选**：采不到数只是"这条作品的趋势图空着"，不该让发布链路
    # 或整条回收循环挂掉（§06.6「不阻塞其他发布」）。四个计数各占一个键而不是
    # 一个"容器选择器"，是因为平台把它们放在**四个兄弟节点**里（没有共同父节点
    # 能一次读全），而"读一个节点"这件事已经有现成的 `text_content` 了。
    "metric_row",  # 管理页上**这一条作品**的那一行（内含 `{post_id}` 占位符）
    "metric_views",  # 播放量（**相对** metric_row 的后代选择器）
    "metric_likes",  # 点赞
    "metric_comments",  # 评论
    "metric_shares",  # 分享
)

#: ``metric_row`` 里的作品号占位符（T5.4）。用 ``str.replace`` 而不是 ``str.format``：
#: 选择器里本来就全是 ``{}``（CSS 属性选择器、``:has-text()``），拿 ``format`` 去填
#: 一个占位符会把其余的 ``{}`` 当成字段名 —— 报错还算好的，改坏成静默的空串才要命。
POST_ID_PLACEHOLDER: Final[str] = "{post_id}"

#: 四个计数的键（顺序 = 面板上的列顺序）。**`parse_metric_count` 按它遍历**，
#: 于是"加一个计数维度"（比如收藏）只改这一行。
METRIC_KEYS: Final[tuple[str, ...]] = ("views", "likes", "comments", "shares")

#: 回读方式的取值（§06.5.3 第 ⑤ 步）。**为什么这是数据不是常量**：标题框在四个平台
#: 上都是 ``<input>``（读 ``input_value``），而文案框有的是 ``<textarea>``（也是
#: ``input_value``）、有的是 ``contenteditable``（只能读 ``inner_text``）——
#: 对着 ``contenteditable`` 调 ``input_value`` 会直接抛，对着 ``<textarea>`` 调
#: ``inner_text`` 读到的是**初始内容**而不是当前值（一个安静的错值）。
READBACK_KINDS: Final[tuple[str, ...]] = ("value", "text")

#: 回读方式的默认值（pack 没写时用）。
DEFAULT_READBACK: Final[dict[str, str]] = {"title": "value", "caption": "text"}

#: 必须有正文的**文本**标记组（不是选择器，是页面上的中文提示语）。
REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "login_expired_text",  # 与"从未登录"区分开：见 PlaywrightPublisher.health
    "review_rejected_text",  # 平台审核不通过的提示语
)

SELECTOR_KEY_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "required": REQUIRED_SELECTORS,
    "optional": OPTIONAL_SELECTORS,
}


def selector_root() -> Path:
    """选择器目录 ``src/studio/publish/selectors/``。"""
    return Path(__file__).resolve().parent / _SELECTOR_DIRNAME


@dataclass(frozen=True, slots=True)
class SelectorPack:
    """一个平台的选择器 + 页面地址 + 文本标记（**加载后只读**）。"""

    platform: str
    version: str
    urls: dict[str, str]
    selectors: dict[str, str]
    markers: dict[str, tuple[str, ...]]
    readback: dict[str, str]
    source: Path

    def url(self, key: str) -> str:
        return self._get(self.urls, key, kind="url")

    def selector(self, key: str) -> str:
        """取一个选择器；缺 ⇒ ``PUBLISH_SELECTOR_MISS``。

        **必选项在加载时已经校验过**，所以这里抛出的只可能是"要了一个可选键"。
        运行期仍要抛而不是返回空串：空串喂给 ``page.click()`` 会变成
        "在当前页面上随便点了某个东西"，比一个明确的报错危险得多。
        """
        return self._get(self.selectors, key, kind="selector")

    def has(self, key: str) -> bool:
        return bool(self.selectors.get(key))

    def texts(self, key: str) -> tuple[str, ...]:
        return self.markers.get(key, ())

    def readback_kind(self, key: str) -> str:
        """该字段怎么读回来：``value``（``<input>``/``<textarea>``）或 ``text``（富文本）。"""
        return self.readback.get(key) or DEFAULT_READBACK.get(key, "text")

    @staticmethod
    def _get(mapping: dict[str, str], key: str, *, kind: str) -> str:
        value = mapping.get(key, "")
        if not value:
            raise PublishError(
                f"选择器包缺少 {kind}：{key}",
                code=ErrorCode.PUBLISH_SELECTOR_MISS,
                context={"kind": kind, "key": key},
                remediation="在 selectors/<platform>.yaml 里补上这一项"
                "（见 docs/runbook/publish_selector.md）",
            )
        return value


def load_selector_pack(platform: str, *, root: Path | None = None) -> SelectorPack:
    """读 ``selectors/<platform>.yaml`` 并**在装配期**把结构性错误全部暴露出来。

    校验放在这里（而不是第一次用到时）的理由：装配发生在 worker 启动的几十毫秒里，
    而"第一次用到"发生在视频已经上传完之后 —— 后者要重传一次 12MB。
    """
    path = (root or selector_root()) / f"{platform}.yaml"
    if not path.is_file():
        raise PublishError(
            f"没有 {platform} 的选择器文件：{path}",
            code=ErrorCode.PUBLISH_SELECTOR_MISS,
            context={"platform": platform, "path": path.as_posix()},
            remediation="一期只带 douyin / kuaishou / shipinhao 三份（§06.2.1）",
        )

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 触发靠坏文件
        raise PublishError(
            f"选择器文件不是合法 YAML：{path.name}",
            code=ErrorCode.PUBLISH_SELECTOR_MISS,
            context={"path": path.as_posix(), "error": str(exc)},
            remediation="按 docs/runbook/publish_selector.md 的骨架改回来",
        ) from exc

    if not isinstance(payload, dict):
        raise _bad(path, "顶层必须是一个映射（platform / version / urls / selectors / markers）")

    declared = str(payload.get("platform", "")).strip()
    if declared != platform:
        raise _bad(path, f"文件里写的 platform 是 {declared!r}，与文件名 {platform!r} 不一致")

    version = str(payload.get("version", "")).strip()
    if not version:
        raise _bad(path, "缺少 version（它会被写进 publications.evidence_json，事后定位改版用）")

    urls = _string_map(path, payload.get("urls"), field="urls")
    if not urls.get("upload"):
        raise _bad(path, "urls.upload 是必填（八步第 ① 步要打开的页面）")

    selectors = _string_map(path, payload.get("selectors"), field="selectors")
    missing = [key for key in REQUIRED_SELECTORS if not selectors.get(key)]
    if missing:
        raise _bad(path, f"缺少必需选择器：{', '.join(missing)}")

    readback = _string_map(path, payload.get("readback"), field="readback")
    bad_kinds = {key: value for key, value in readback.items() if value not in READBACK_KINDS}
    if bad_kinds:
        raise _bad(path, f"readback 只允许 {'/'.join(READBACK_KINDS)}：{bad_kinds}")

    markers_raw = payload.get("markers") or {}
    if not isinstance(markers_raw, dict):
        raise _bad(path, "markers 必须是映射（键 → 文本列表）")
    markers: dict[str, tuple[str, ...]] = {}
    for key, value in markers_raw.items():
        if isinstance(value, str):
            markers[str(key)] = (value,)
        elif isinstance(value, list):
            markers[str(key)] = tuple(str(item) for item in value)
        else:
            raise _bad(path, f"markers.{key} 必须是字符串或字符串列表")
    for key in REQUIRED_MARKERS:
        if not markers.get(key):
            raise _bad(path, f"缺少必需文本标记：markers.{key}")

    return SelectorPack(
        platform=platform,
        version=version,
        urls=urls,
        selectors=selectors,
        markers=markers,
        readback=readback,
        source=path,
    )


def _string_map(path: Path, value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad(path, f"{field} 必须是映射")
    out: dict[str, str] = {}
    for key, raw in value.items():
        text = str(raw).strip()
        if text:
            out[str(key)] = text
    return out


def _bad(path: Path, reason: str) -> PublishError:
    return PublishError(
        f"选择器文件不合法（{path.name}）：{reason}",
        code=ErrorCode.PUBLISH_SELECTOR_MISS,
        context={"path": path.as_posix(), "reason": reason},
        remediation="按 docs/runbook/publish_selector.md 的骨架修",
    )
