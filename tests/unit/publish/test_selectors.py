"""选择器包（T5.2 · §06.5.2「选择器集中化」· R13）。

这一层的价值**全在装配期**：结构错误（少一个必需选择器、platform 与文件名不符、
yaml 写坏了）必须在 worker 启动的几十毫秒里炸出来，而不是等到视频传完 12MB 之后。

所以这里的测试大半是"喂一份坏 yaml，断言它在**加载时**就抛"，而不是"用到时才发现"。
真机上的 CSS 值对不对是另一回事（那要一个已登录的账号，见 selectors.py 的模块注释）。

顺带钉住一条**随仓库走的**不变式：三份一线平台的 yaml 必须真的能被加载 ——
它们会随包发布，一份坏的 yaml 会让整个发布池起不来。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, PublishError
from studio.publish.selectors import (
    DEFAULT_READBACK,
    READBACK_KINDS,
    REQUIRED_MARKERS,
    REQUIRED_SELECTORS,
    SelectorPack,
    load_selector_pack,
    selector_root,
)

# ── 随包的三份 yaml ──────────────────────────────────────────────────

FIRST_TIER = ("douyin", "kuaishou", "shipinhao")


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

    @pytest.mark.parametrize("platform", FIRST_TIER)
    def test_first_tier_packs_load(self, platform: str) -> None:
        """三份一线 yaml 必须真的能加载 —— 它们随包走，坏一份会让发布池起不来。"""
        pack = load_selector_pack(platform)
        assert pack.platform == platform
        assert pack.version
        assert pack.url("upload").startswith("https://")

    @pytest.mark.parametrize("platform", (*FIRST_TIER, "fixture"))
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

    @pytest.mark.parametrize("platform", (*FIRST_TIER, "fixture"))
    def test_shipped_packs_declare_readback(self, platform: str) -> None:
        pack = load_selector_pack(platform)
        assert pack.readback_kind("title") in READBACK_KINDS
        assert pack.readback_kind("caption") in READBACK_KINDS


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
