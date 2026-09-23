"""选择器包（T5.2 · §06.5.2「选择器集中化」· R13）。

这一层的价值**全在装配期**：结构错误（少一个必需选择器、platform 与文件名不符、
yaml 写坏了）必须在 worker 启动的几十毫秒里炸出来，而不是等到视频传完 12MB 之后。

所以这里的测试大半是"喂一份坏 yaml，断言它在**加载时**就抛"，而不是"用到时才发现"。
真机上的 CSS 值对不对是另一回事（那要一个已登录的账号，见 selectors.py 的模块注释）。

顺带钉住两条**随仓库走的**不变式：

① 七份真平台 pack 必须真的能被加载 —— 它们会随包发布，一份坏的 yaml 会让整个发布池
   起不来（而症状是"发布池根本起不来"，不是"某个平台发不出去"）；
② 每份 pack 的**校准状态**必须如实（``calibrated`` / ``calibrated_at`` / ``known_gaps``）
   —— 面板上那一列直接照着它显示，而它决定了操作员敢不敢拿这个平台真发一条（R14）。
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, PublishError
from studio.publish.selectors import (
    DEFAULT_READBACK,
    POST_ID_PLACEHOLDER,
    READBACK_KINDS,
    REQUIRED_MARKERS,
    REQUIRED_SELECTORS,
    SelectorPack,
    load_selector_pack,
    selector_root,
)

# ── 随包的七份真平台 yaml（+ 靶页）────────────────────────────────────

#: §06.2.1 的平台矩阵（顺序 = 矩阵的书写顺序）。加一个平台就要在这里加一个 ——
#: 这份清单与 `config/publish.yaml` 的 platforms 键、注册表三者由契约测试钉在一起。
REAL_PACKS = ("douyin", "kuaishou", "shipinhao", "xiaohongshu", "bilibili", "xigua", "weibo")

#: 真平台 + 本地靶页。装配期的校验对靶页**一视同仁**（它是同一份加载路径）。
ALL_PACKS = (*REAL_PACKS, "fixture")

#: 引擎前缀出现在**带逗号的选择器列表**里 ⇒ 整条会被当成 CSS 去解析，于是抛错。
#: 装配期会因此拒绝整份 pack（见 `load_selector_pack` 里那条校验）；这里再对着随包的
#: 七份 yaml 独立断言一遍 —— 万一有人把那条校验删了，这几条也要红。
ENGINE_PREFIX = re.compile(r"(?:^|,)\s*(?:text|xpath|css|id|data-testid|role|nth)=")

#: 发布按钮里的**包含**匹配（见 `load_selector_pack` 里那条校验 / 陷阱 #227）。
LOOSE_PUBLISH = re.compile(r":has-text\(|(?:^|,)\s*text=")


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def _good(platform: str = "douyin") -> str:
    return f"""platform: {platform}
version: "1.0"
urls:
  upload: "https://example.invalid/upload"
selectors:
  login_ok: "#ok"
  login_required: "#login"
  upload_input: "#video"
  title_input: "#title"
  publish_button: "#publish"
  success_marker: "#result"
markers:
  login_expired_text: ["登录已过期"]
  review_rejected_text: ["审核不通过"]
"""


class TestShippedPacks:
    def test_selector_root_points_at_the_package(self) -> None:
        root = selector_root()
        assert root.is_dir()
        assert root.name == "selectors"
        assert (root.parent / "__init__.py").is_file()

    @pytest.mark.parametrize("platform", REAL_PACKS)
    def test_every_shipped_pack_loads(self, platform: str) -> None:
        """七份真平台 yaml 必须真的能加载 —— 它们随包走，坏一份会让发布池起不来。

        注意这条**不看** ``calibrated``：未校准的 pack 照样要能装起来（它本来就该能
        投递、能演练，只是面板上会明说"没真机校准过"）。装配期拦的是**结构**错误。
        """
        pack = load_selector_pack(platform)
        assert pack.platform == platform
        assert pack.version
        assert pack.url("upload").startswith("https://")

    @pytest.mark.parametrize("platform", ALL_PACKS)
    def test_every_pack_declares_all_required_keys(self, platform: str) -> None:
        pack = load_selector_pack(platform)
        for key in REQUIRED_SELECTORS:
            assert pack.selector(key), f"{platform} 缺 {key}"
        for key in REQUIRED_MARKERS:
            assert pack.texts(key), f"{platform} 缺 markers.{key}"

    def test_fixture_pack_exists_and_is_not_a_real_platform(self) -> None:
        """靶页代号不在平台矩阵里（见 platforms/fixture.py 的注释）。"""
        pack = load_selector_pack("fixture")
        assert pack.platform == "fixture"

    @pytest.mark.parametrize("platform", ALL_PACKS)
    def test_no_pack_uses_a_selector_engine_prefix(self, platform: str) -> None:
        """★ 真机坑（2026-09-23）：`div.success-page, text=发布成功` 让第 ⑦ 步永远判不出来。

        Playwright 的 `query_selector` 走 CSS 解析器，引擎前缀只能**单独**出现；拼进
        选择器列表会抛 `Unexpected token "=" while parsing css selector`，而
        `_present()` 把异常吞成"元素不在" ⇒ 发布成功也一路等到 600s 超时（陷阱 #226）。
        随包的几份 pack 原本都是这个写法。
        """
        pack = load_selector_pack(platform)
        for key, value in pack.selectors.items():
            assert not ("," in value and ENGINE_PREFIX.search(value)), f"{platform}.{key} = {value!r}"

    @pytest.mark.parametrize("platform", ALL_PACKS)
    def test_no_pack_uses_a_loose_publish_button(self, platform: str) -> None:
        """★ 真机坑（2026-09-23）：发布按钮**必须**全等匹配（陷阱 #227）。

        随包的几份原本都写着 `button:has-text('发布')` —— 它命中的是左侧导航
        「作品发布」（文档序更早），于是"点发布"这一步什么都没发生。装配期会拒掉这种
        写法（见 `load_selector_pack`），这里再对着随包的 yaml 独立断言一遍。
        """
        value = load_selector_pack(platform).selector("publish_button")
        assert not LOOSE_PUBLISH.search(value), f"{platform}.publish_button = {value!r}"

    @pytest.mark.parametrize("platform", ALL_PACKS)
    def test_no_pack_uses_a_substring_result_marker(self, platform: str) -> None:
        """★ 真机坑（2026-09-23）：结果页标志**必须**全等匹配（陷阱 #229）。

        `:text('发布成功')` 会命中「视频发布成功后，价格将无法更改」—— 那句提示在
        **发布之前**就在页面上，于是"发布成功"永远为真，一条根本没发出去的内容被记成
        published。随包的几份 pack 原本都是这个写法。
        """
        pack = load_selector_pack(platform)
        for key in ("success_marker", "reject_marker", "verify_marker"):
            assert ":text(" not in pack.selectors.get(key, ""), f"{platform}.{key}"


# ── 加载期校验 ────────────────────────────────────────────────────────


class TestLoadValidation:
    def test_missing_file_names_the_platform(self, tmp_path: Path) -> None:
        with pytest.raises(PublishError) as info:
            load_selector_pack("nope", root=tmp_path)
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS
        assert "nope" in info.value.message

    def test_platform_must_match_the_filename(self, tmp_path: Path) -> None:
        """文件名与内容不符 ⇒ 装不起来。

        这条防的是**复制粘贴**：拿 douyin.yaml 改成 kuaishou 时忘了改 ``platform:``，
        结果是"快手的发布用着抖音的选择器" —— 症状是发到快手时选择器全不命中，
        而报错只会说"选择器失效"。
        """
        _write(tmp_path, "douyin.yaml", _good("kuaishou"))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "platform" in info.value.message

    def test_missing_version_is_rejected(self, tmp_path: Path) -> None:
        """``version`` 会被写进 ``publications.evidence_json``，缺了就没法事后定位改版。"""
        _write(tmp_path, "douyin.yaml", _good().replace('version: "1.0"\n', ""))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "version" in info.value.message

    def test_missing_upload_url_is_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good().replace('  upload: "https://example.invalid/upload"\n', ""))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "upload" in info.value.message

    @pytest.mark.parametrize("key", REQUIRED_SELECTORS)
    def test_each_required_selector_is_enforced(self, tmp_path: Path, key: str) -> None:
        body = _good()
        line = {
            "login_ok": '  login_ok: "#ok"\n',
            "login_required": '  login_required: "#login"\n',
            "upload_input": '  upload_input: "#video"\n',
            "title_input": '  title_input: "#title"\n',
            "publish_button": '  publish_button: "#publish"\n',
        }[key]
        _write(tmp_path, "douyin.yaml", body.replace(line, ""))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert key in info.value.message

    @pytest.mark.parametrize("key", REQUIRED_MARKERS)
    def test_each_required_marker_is_enforced(self, tmp_path: Path, key: str) -> None:
        body = _good().replace(f'  {key}: ["登录已过期"]\n', "").replace(f'  {key}: ["审核不通过"]\n', "")
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert key in info.value.message

    def test_broken_yaml_reports_a_yaml_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", "platform: douyin\n  bad indent: [\n")
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS

    def test_selector_engine_prefix_in_a_list_is_rejected(self, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23）：`div.x, text=文案` 在 Playwright 里是**解析错**。

        它不该等到真发布才暴露：装配期拒掉，错误里直接说清该改成 `:text('文案')`。
        放行的话症状是"发布成功却一路等到 600s 超时"，而现场没有任何一行日志指向选择器。
        """
        body = _good().replace('  publish_button: "#publish"\n', '  publish_button: "div.x, text=发布成功"\n')
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS
        assert "publish_button" in info.value.message
        assert ":text(" in info.value.message

    def test_a_bare_engine_prefix_is_still_allowed(self, tmp_path: Path) -> None:
        """单独一个 `text=文案` 是**合法**的（Playwright 只在列表里才拒绝它）。"""
        body = _good().replace('  title_input: "#title"\n', '  title_input: "text=标题"\n')
        _write(tmp_path, "douyin.yaml", body)
        assert load_selector_pack("douyin", root=tmp_path).selector("title_input") == "text=标题"

    def test_a_loose_publish_button_is_rejected(self, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23）：`button:has-text('发布')` 命中的是左侧导航「作品发布」。

        它在文档序里排在真按钮**前面** ⇒ 点下去只是切了个页面、表单被重置，真按钮
        一次都没被碰过，而现场看到的是"点了发布什么都没发生"，随后第 ⑦ 步一路等到超时
        （陷阱 #227）。发布按钮的文案都很短，包含匹配在这个位置是**必然出错**的。
        """
        for loose in ("button:has-text('发布')", "text=发布"):
            body = _good().replace('  publish_button: "#publish"\n', f'  publish_button: "{loose}"\n')
            _write(tmp_path, "douyin.yaml", body)
            with pytest.raises(PublishError) as info:
                load_selector_pack("douyin", root=tmp_path)
            assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS
            assert ":text-is(" in info.value.message

    def test_an_exact_publish_button_is_accepted(self, tmp_path: Path) -> None:
        """全等匹配（`:text-is()`）是推荐的写法 —— 它只命中那一个按钮。"""
        body = _good().replace(
            '  publish_button: "#publish"\n', "  publish_button: \"button:text-is('发布')\"\n"
        )
        _write(tmp_path, "douyin.yaml", body)
        assert load_selector_pack("douyin", root=tmp_path).has("publish_button")

    def test_a_substring_result_marker_is_rejected(self, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23）：`:text('发布成功')` 命中的是**发布之前**就在的提示句。

        真机上那句是「视频发布成功后，价格将无法更改」⇒ "发布成功"恒为真，一条根本没
        发出去的内容被记成 published（陷阱 #229）。这种错**不会报错**，只会说谎，
        所以必须在装配期拦下。
        """
        # **替换**掉原来那一行，不是往后追加 —— 同一个键写两遍时 YAML 取的是**最后**
        # 一个，追加的坏值会被原来的好值盖掉，测试就变成了"什么都没验"。
        body = _good().replace(
            '  success_marker: "#result"\n',
            "  success_marker: \"div.x, :text('发布成功')\"\n",
        )
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS
        assert ":text-is(" in info.value.message

    def test_an_exact_result_marker_is_accepted(self, tmp_path: Path) -> None:
        body = _good().replace(
            '  publish_button: "#publish"\n',
            '  publish_button: "#publish"\n  success_marker: "div.x, :text-is(\'发布成功\')"\n',
        )
        _write(tmp_path, "douyin.yaml", body)
        assert load_selector_pack("douyin", root=tmp_path).has("success_marker")

    def test_the_text_pseudo_class_is_allowed(self, tmp_path: Path) -> None:
        """`:text("…")` 是 CSS 伪类，可以安全地跟在逗号后面 —— 这是推荐的写法。"""
        body = _good().replace(
            '  publish_button: "#publish"\n', "  publish_button: \"div.x, :text('发布')\"\n"
        )
        _write(tmp_path, "douyin.yaml", body)
        assert load_selector_pack("douyin", root=tmp_path).has("publish_button")

    def test_top_level_must_be_a_mapping(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", "- a\n- b\n")
        with pytest.raises(PublishError):
            load_selector_pack("douyin", root=tmp_path)

    def test_selectors_must_be_a_mapping(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good().replace("selectors:\n", "selectors: [1, 2]\n"))
        with pytest.raises(PublishError):
            load_selector_pack("douyin", root=tmp_path)

    def test_marker_must_be_string_or_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good().replace('["登录已过期"]', "{a: 1}"))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "markers" in info.value.message

    def test_bare_string_marker_is_accepted(self, tmp_path: Path) -> None:
        """写一个字符串而不是列表是常见手误，直接当单元素列表收下。"""
        _write(tmp_path, "douyin.yaml", _good().replace('["登录已过期"]', "登录已过期"))
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.texts("login_expired_text") == ("登录已过期",)

    def test_blank_selector_values_are_dropped_then_caught_as_missing(self, tmp_path: Path) -> None:
        """空串等于没写：留着它会让 ``page.click("")`` 变成"随便点一个"。"""
        _write(tmp_path, "douyin.yaml", _good().replace('  title_input: "#title"\n', '  title_input: ""\n'))
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "title_input" in info.value.message


# ── readback 声明 ─────────────────────────────────────────────────────


class TestReadback:
    def test_defaults_are_used_when_pack_is_silent(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good())
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.readback_kind("title") == DEFAULT_READBACK["title"]
        assert pack.readback_kind("caption") == DEFAULT_READBACK["caption"]

    def test_pack_can_override(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good() + "readback:\n  caption: value\n")
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.readback_kind("caption") == "value"

    def test_unknown_kind_is_rejected_at_load(self, tmp_path: Path) -> None:
        """``contenteditable`` 上读 ``input_value`` 会直接抛 —— 那种错不该在真机上撞。"""
        _write(tmp_path, "douyin.yaml", _good() + "readback:\n  caption: innerHTML\n")
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "readback" in info.value.message

    def test_unknown_field_falls_back_to_text(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good())
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.readback_kind("something_else") in READBACK_KINDS

    @pytest.mark.parametrize("platform", ALL_PACKS)
    def test_shipped_packs_declare_readback(self, platform: str) -> None:
        pack = load_selector_pack(platform)
        assert pack.readback_kind("title") in READBACK_KINDS
        assert pack.readback_kind("caption") in READBACK_KINDS


# ── 判据里**必然失灵**的那几条（装配期拦下，见 `_check_semantics`）──────────


class TestSemanticValidation:
    """这些选择器/文案**语法合法、也能命中**，但它们在**任何**状态下都给不出可用答案。

    与上面那组（缺键 / 引擎前缀 / 包含匹配）的分工：那几条判的是"这句话写错了"
    （Playwright 会抛、或者命中的是另一个元素）；这里判的是"这句话永远不成立"。
    两类错误的**现场症状一模一样** —— 第 ⑦ 步一路等到超时、截图里看不出问题 ——
    所以都在装配期拦（那时改一行 yaml 就够了）。
    """

    def test_an_ambiguous_login_marker_is_rejected(self, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23 · 陷阱 #212）："扫码登录"是**登录页自己的按钮文案**。

        两种状态（从没登录过 / 登录过期）下它都在页面上 ⇒ 拿它当判据等于永远判成
        "过期"。真机上就是这么给一个**从来没登录过**的新号报了"登录态已过期，需人工
        重新扫码登录"，把操作员赶去重扫一个其实好好的号（两句给的动作正好相反）。
        随包的 kuaishou / shipinhao 原本都带着这一句。
        """
        body = _good().replace('["登录已过期"]', '["登录已过期", "扫码登录"]')
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS
        assert "扫码登录" in info.value.message
        assert "212" in info.value.message

    def test_a_clean_login_marker_set_is_accepted(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good())
        assert load_selector_pack("douyin", root=tmp_path).texts("login_expired_text") == ("登录已过期",)

    def test_no_success_criterion_at_all_is_rejected(self, tmp_path: Path) -> None:
        """★ 真机坑（2026-09-23 · 陷阱 #228）：两个判据都空 ⇒ 第 ⑦ 步必然等到超时。

        抖音发布成功后**不发**"发布成功"这几个字，而是把页面跳走。只认元素的话，
        一条**已经发出去**的内容会在 600s 之后被记成失败 —— 而那时人已经在平台上
        看到它了。那种记录会被"重试"成第二条（R14 不可逆）。
        """
        body = _good().replace('  success_marker: "#result"\n', "")
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "success_url_contains" in info.value.message

    def test_a_url_fragment_alone_is_a_valid_success_criterion(self, tmp_path: Path) -> None:
        """抖音那一版的真实形状：没有结果页元素，只有"地址跳走了"这一个判据。"""
        body = (
            _good()
            .replace('  success_marker: "#result"\n', "")
            .replace(
                '  review_rejected_text: ["审核不通过"]\n',
                '  review_rejected_text: ["审核不通过"]\n  success_url_contains: ["/manage"]\n',
            )
        )
        _write(tmp_path, "douyin.yaml", body)
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.texts("success_url_contains") == ("/manage",)
        assert not pack.has("success_marker")

    def test_metric_row_without_the_placeholder_is_rejected(self, tmp_path: Path) -> None:
        """★ T5.4 最贵的一类错：**错得看不出来**。

        `metric_row` 少了 `{post_id}` ⇒ 四个计数是相对**第一条**作品取的 ——
        于是"这一条的数据"读的是别人的数，而面板上两个数字都长得像正常的数。
        """
        body = _good().replace("markers:\n", '  metric_row: "tr.note-item"\nmarkers:\n')
        _write(tmp_path, "douyin.yaml", body)
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert POST_ID_PLACEHOLDER in info.value.message

    def test_metric_row_with_the_placeholder_is_accepted(self, tmp_path: Path) -> None:
        body = _good().replace("markers:\n", '  metric_row: "tr[data-id=\\"{post_id}\\"]"\nmarkers:\n')
        _write(tmp_path, "douyin.yaml", body)
        assert load_selector_pack("douyin", root=tmp_path).has("metric_row")

    def test_claiming_calibration_without_a_date_is_rejected(self, tmp_path: Path) -> None:
        """ "校准过"与"什么时候校准的"是同一件事的两半（平台会改版）。"""
        _write(tmp_path, "douyin.yaml", _good() + "calibrated: true\n")
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "calibrated_at" in info.value.message

    def test_claiming_calibration_with_a_date_is_accepted(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good() + 'calibrated: true\ncalibrated_at: "2026-09-23"\n')
        pack = load_selector_pack("douyin", root=tmp_path)
        assert pack.calibrated and pack.calibrated_at == "2026-09-23"


# ── 校准状态：一份**数据**，不是一句注释 ───────────────────────────────


class TestCalibrationStatus:
    """``calibrated`` / ``calibrated_at`` / ``known_gaps``（见 selectors.py 的模块注释）。

    为什么值得一整组测试：面板上"这个平台校准过没有"那一列**直接照这几个字段显示**，
    而它决定了操作员敢不敢拿这个平台真发一条（R14 不可回滚）。这里钉住的不是
    "七份 pack 都校准过了"（那是假的），而是"**唯一校准过的那份敢说自己校准过，
    其余的都不敢**"—— 以及"没做完的那部分（``known_gaps``）也说得出来"。
    """

    def test_only_douyin_claims_calibration(self) -> None:
        """2026-09-23 那次真机校准只做过抖音那一份。

        这条会在有人"顺手"给别的 pack 补上 ``calibrated: true`` 时变红 —— 而那句话的
        代价是操作员以为"这个平台验过了，可以直接发"，真发一条才发现选择器全是猜的。
        要让它变绿，正确做法是**去真机上校准**，不是改这个测试。
        """
        claimed = {code for code in REAL_PACKS if load_selector_pack(code).calibrated}
        assert claimed == {"douyin"}, f"声称校准过的是 {sorted(claimed)}"

    def test_the_calibrated_pack_carries_its_date(self) -> None:
        pack = load_selector_pack("douyin")
        assert pack.calibrated
        assert pack.calibrated_at == "2026-09-23"

    @pytest.mark.parametrize("platform", [code for code in REAL_PACKS if code != "douyin"])
    def test_uncalibrated_packs_say_so_in_the_file(self, platform: str) -> None:
        """``calibrated: false`` 要**写在文件里**，不能靠"没写就是 false"。

        读文件的人在 yaml 里找的是这一行（它旁边就是"怎么校准"的说明）；而"没写"
        与"写了 false"在人眼里是两件事 —— 前者像"忘了"，后者才是"知道，但还没做"。
        """
        text = (selector_root() / f"{platform}.yaml").read_text(encoding="utf-8")
        assert "calibrated: false" in text, platform

    def test_known_gaps_are_read_back(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good() + 'known_gaps:\n  - "缺一段流程"\n  - 也是缺口\n')
        assert load_selector_pack("douyin", root=tmp_path).known_gaps == ("缺一段流程", "也是缺口")

    def test_known_gaps_default_to_empty(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good())
        assert load_selector_pack("douyin", root=tmp_path).known_gaps == ()

    def test_known_gaps_reject_a_mapping(self, tmp_path: Path) -> None:
        _write(tmp_path, "douyin.yaml", _good() + "known_gaps:\n  a: 1\n")
        with pytest.raises(PublishError) as info:
            load_selector_pack("douyin", root=tmp_path)
        assert "known_gaps" in info.value.message

    def test_bilibili_declares_the_partition_gap(self) -> None:
        """ "未校准"与"缺一段流程"是**两件事**（§06.2.2 明写 B 站必选分区）。

        只显示前者会让人以为"校准完就能发了" —— 而分区那一步压根没写。
        """
        gaps = " ".join(load_selector_pack("bilibili").known_gaps)
        assert "分区" in gaps
        assert "bilibili.py" in gaps


# ── SelectorPack 的行为 ───────────────────────────────────────────────


class TestSelectorPack:
    def _pack(self, tmp_path: Path) -> SelectorPack:
        _write(tmp_path, "douyin.yaml", _good())
        return load_selector_pack("douyin", root=tmp_path)

    def test_missing_optional_selector_raises_rather_than_returning_empty(self, tmp_path: Path) -> None:
        """**空串喂给 ``page.click()`` 会变成"在当前页面上随便点某个东西"** —— 比报错危险得多。"""
        pack = self._pack(tmp_path)
        with pytest.raises(PublishError) as info:
            pack.selector("cover_trigger")
        assert info.value.code == ErrorCode.PUBLISH_SELECTOR_MISS

    def test_has_reports_optional_absence(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path)
        assert pack.has("title_input")
        assert not pack.has("cover_trigger")

    def test_missing_url_raises(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path)
        with pytest.raises(PublishError):
            pack.url("manage")

    def test_texts_of_unknown_marker_is_empty(self, tmp_path: Path) -> None:
        assert self._pack(tmp_path).texts("nope") == ()

    def test_pack_is_frozen(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path)
        with pytest.raises(FrozenInstanceError):
            pack.version = "9"  # type: ignore[misc]

    def test_source_points_at_the_file(self, tmp_path: Path) -> None:
        assert self._pack(tmp_path).source.name == "douyin.yaml"
