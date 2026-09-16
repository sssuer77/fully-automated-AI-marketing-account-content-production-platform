"""契约：``Publisher`` 抽象与平台注册表（T5.2 · §4.6.1 / §06.5.4 · §06.13）。

为什么值得一条契约测试
----------------------
"平台差异只允许出现在三个地方"（§4.6.1）是一条**跨文件的**不变式：选择器在 yaml、
长度上限在 ``config/publish.yaml``、流程在子类。任何一处漏了，症状都是"某个平台
发布行为与别的平台不一样"，而那种差异**不会在单元测试里报错** —— 它只会在真机上
以"这个平台发不出去"收场。

所以这里**直接解析规格书**，把"文档 vs 代码"的差异变成红灯：

1. §4.6.1 的三个抽象方法、七个平台代号、七个错误码 —— 逐字比对；
2. §06.5.4 的发布状态机六个状态 —— 逐字比对；
3. ``config/publish.yaml`` 的 ``platforms`` 键 —— 必须与注册表**一一对应**
   （多一个 = 配置写了没实现的平台；少一个 = 实现了但发不出去）。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from pathlib import Path

import pytest

import studio.publish.base as base_module
import studio.publish.playwright_publisher as playwright_module
from studio.core.config import PublishConfig, load_config
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.publish.base import (
    PUBLISHERS,
    Publisher,
    PublishEvidence,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from studio.publish.platforms import NON_PLATFORM_CODES, REAL_PLATFORMS
from studio.publish.playwright_publisher import PlaywrightPublisher
from studio.publish.selectors import load_selector_pack, selector_root

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DOC = REPO_ROOT / "docs" / "spec" / "04-contracts.md"
PUBLICATION_DOC = REPO_ROOT / "docs" / "spec" / "06-publication.md"

#: §4.6.1 的平台矩阵（一线 3 + 二线 4）。
SPEC_PLATFORMS = ("douyin", "kuaishou", "shipinhao", "xiaohongshu", "bilibili", "xigua", "weibo")

#: §4.6.1 列出的 ``PublishResult.error_code`` 取值。
SPEC_ERROR_CODES = (
    "PUBLISH_LOGIN_EXPIRED",
    "PUBLISH_RATELIMIT",
    "PUBLISH_SELECTOR_MISS",
    "PUBLISH_UPLOAD_FAILED",
    "PUBLISH_REVIEW_REJECTED",
    "PUBLISH_TIMEOUT",
    "PUBLISH_UNKNOWN",
)

#: §06.5.4 的发布状态机。
SPEC_STATUSES = ("queued", "uploading", "published", "failed", "manual_required", "canceled")


def _contracts() -> str:
    return CONTRACTS_DOC.read_text(encoding="utf-8")


def _publication() -> str:
    return PUBLICATION_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def publish_config() -> PublishConfig:
    return load_config(StudioPaths.from_env()).bundle.publish


class TestAbstractSurface:
    def test_three_abstract_methods(self) -> None:
        """§4.6.1 逐字：``health`` / ``publish`` / ``fetch_metrics``，**三个都要有**。"""
        assert Publisher.__abstractmethods__ == frozenset({"health", "publish", "fetch_metrics"})

    def test_spec_lists_the_same_three(self) -> None:
        section = _contracts()
        start = section.index("### 4.6.1")
        body = section[start : start + 4000]
        for name in ("health", "publish", "fetch_metrics"):
            assert f"async def {name}(" in body, name

    def test_signatures_are_async(self) -> None:
        for name in ("health", "publish", "fetch_metrics"):
            assert inspect.iscoroutinefunction(getattr(Publisher, name)), name

    def test_publish_request_matches_the_spec_fields(self) -> None:
        """§4.6.1 的 ``PublishRequest`` 字段集（去掉 HTTP 专属的 ``model_config``）。"""
        spec_fields = {
            "task_id",
            "platform",
            "account_id",
            "video_path",
            "cover_path",
            "title",
            "caption",
            "tags",
            "scheduled_at",
            "dry_run",
        }
        assert {f.name for f in fields(PublishRequest)} == spec_fields

    def test_publish_result_matches_the_spec_fields(self) -> None:
        spec_fields = {
            "ok",
            "status",
            "url",
            "platform_post_id",
            "published_at",
            "error_code",
            "error_message",
            "evidence",
            "elapsed_ms",
        }
        assert {f.name for f in fields(PublishResult)} == spec_fields

    def test_publish_evidence_matches_the_spec_fields(self) -> None:
        spec_fields = {
            "screenshot_path",
            "dom_snapshot_path",
            "stderr_tail",
            "selector_version",
            "platform_text",
        }
        # ``stage`` 是**实现补的**：dry-run 要回答"停在第几步"，而 §06.5.3 第 ⑥ 步
        # 只说了"在此停止 + 截图"、没给字段。补它比把阶段信息塞进 stderr_tail 干净。
        assert {f.name for f in fields(PublishEvidence)} == spec_fields | {"stage"}

    def test_publish_health_matches_the_spec_fields(self) -> None:
        spec_fields = {"ready", "logged_in", "account_name", "last_check_at", "hint"}
        assert {f.name for f in fields(PublishHealth)} == spec_fields

    def test_publish_metrics_matches_the_spec_fields(self) -> None:
        spec_fields = {"views", "likes", "comments", "shares", "collected_at"}
        assert {f.name for f in fields(PublishMetrics)} == spec_fields


class TestStatusMachine:
    def test_statuses_match_the_spec(self) -> None:
        """§06.5.4 的六个状态，逐字。"""
        assert {s.value for s in PublishStatus} == set(SPEC_STATUSES)

    def test_spec_lists_the_same_six(self) -> None:
        body = _publication()
        start = body.index("### 6.5.4")
        section = body[start : start + 900]
        for status in SPEC_STATUSES:
            assert status in section, status

    def test_publish_failure_does_not_touch_the_task_status(self) -> None:
        """§06.5.4「关键约定」：发布失败**不回退任务状态**。

        代码里没有任何一处把 ``PublishStatus`` 映射成 ``TaskStatus`` —— 这条测试
        把这个"缺席"钉住：一旦有人加了映射，`publish/` 就再也不能独立重试了。
        """
        assert "TaskStatus" not in dir(base_module)
        assert not hasattr(base_module, "TaskStatus")


class TestErrorCodes:
    @pytest.mark.parametrize("code", SPEC_ERROR_CODES)
    def test_spec_codes_exist_in_the_enum(self, code: str) -> None:
        assert code in {member.value for member in ErrorCode}

    def test_spec_lists_the_same_codes(self) -> None:
        body = _contracts()
        start = body.index("### 4.6.1")
        section = body[start : start + 4000]
        for code in SPEC_ERROR_CODES:
            assert code in section, code

    def test_unknown_and_failed_are_different_codes(self) -> None:
        """``UNKNOWN`` 与 ``FAILED`` **不许揉成一个**（§4.6.1 的错误码表两者都有）。

        揉起来之后，"异常从没预期的地方冒出来"与"上传那一步坏了"在库里长得一样，
        而排障动作完全不同：前者要 traceback，后者照着 ``evidence.stage`` 看。
        """
        codes = {ErrorCode.PUBLISH_UNKNOWN.value, ErrorCode.PUBLISH_FAILED.value}
        assert len(codes) == 2, "UNKNOWN 与 FAILED 揉成了一个码"
        assert ErrorCode.PUBLISH_UNKNOWN.value == "PUBLISH_UNKNOWN"

    def test_selector_miss_uses_the_spec_name(self) -> None:
        """规格书 §06.10 用 ``PUBLISH_SELECTOR_MISS``；早先草稿写作 ``..._STALE``。

        两个名字都在枚举里（旧名字留着不删），但**流程里发出的必须是规格书那个** ——
        否则运维按 runbook 去查 ``SELECTOR_MISS`` 时一条记录都找不到。
        """
        package = Path(playwright_module.__file__).parent
        sources = "\n".join(path.read_text(encoding="utf-8") for path in sorted(package.rglob("*.py")))
        assert "ErrorCode.PUBLISH_SELECTOR_MISS" in sources
        assert "ErrorCode.PUBLISH_SELECTOR_STALE" not in sources
        assert ErrorCode.PUBLISH_SELECTOR_MISS.value == "PUBLISH_SELECTOR_MISS"


class TestRegistry:
    def test_every_spec_platform_is_registered(self) -> None:
        missing = set(SPEC_PLATFORMS) - set(PUBLISHERS)
        assert not missing, f"注册表缺：{sorted(missing)}"

    def test_registry_has_no_extra_real_platform(self) -> None:
        extra = set(REAL_PLATFORMS) - set(SPEC_PLATFORMS)
        assert not extra, f"注册表多了规格书里没有的平台：{sorted(extra)}"

    def test_registry_matches_config_platforms(self, publish_config: PublishConfig) -> None:
        """``config/publish.yaml`` 的 ``platforms`` 键必须与注册表一一对应。

        多一个 = 配置写了没实现的平台（真机上才发现）；少一个 = 实现了但发不出去
        （面板上根本列不出来）。
        """
        assert set(publish_config.platforms) == set(REAL_PLATFORMS)

    def test_each_platform_class_declares_its_code(self) -> None:
        for code, cls in PUBLISHERS.items():
            assert cls.platform == code, f"{cls.__name__} 声明的是 {cls.platform}"

    def test_first_tier_has_real_implementations(self) -> None:
        """一线三个必须是**真实现**（不是二线那个空壳）。"""
        for code in ("douyin", "kuaishou", "shipinhao"):
            assert issubclass(PUBLISHERS[code], PlaywrightPublisher), code

    def test_second_tier_is_declared_but_empty(self) -> None:
        """二线四个：接口在、实现空（§06.2.1 · Q9）。"""
        for code in ("xiaohongshu", "bilibili", "xigua", "weibo"):
            assert not issubclass(PUBLISHERS[code], PlaywrightPublisher), code

    def test_fixture_is_not_a_real_platform(self) -> None:
        assert "fixture" in PUBLISHERS
        assert "fixture" in NON_PLATFORM_CODES
        assert "fixture" not in REAL_PLATFORMS

    def test_fixture_code_is_not_a_valid_publication_platform(self) -> None:
        """纵深防御：``publications.platform`` 的 CHECK 约束里没有 ``fixture``。

        就算有人把靶页接进发布池，落库那一步也会被数据库挡下 —— 它**发不出去**。
        """
        sql = (REPO_ROOT / "src" / "studio" / "db" / "migrations" / "0004_publish.sql").read_text(
            encoding="utf-8"
        )
        check = re.search(r"platform\s+TEXT NOT NULL CHECK \(platform IN \(([^)]*)\)\)", sql)
        assert check is not None
        allowed = set(re.findall(r"'([a-z_]+)'", check.group(1)))
        assert allowed == set(SPEC_PLATFORMS) | {"other"}
        assert "fixture" not in allowed


class TestSelectorCoverage:
    """选择器只对**有实现的**平台是必需的（二线没有 yaml 是对的）。"""

    def test_first_tier_has_selector_files(self) -> None:
        for code in ("douyin", "kuaishou", "shipinhao"):
            assert (selector_root() / f"{code}.yaml").is_file(), code

    def test_every_playwright_publisher_has_a_selector_pack(self) -> None:
        for code, cls in PUBLISHERS.items():
            if issubclass(cls, PlaywrightPublisher):
                pack = load_selector_pack(code)
                assert pack.platform == code

    def test_platform_config_selectors_version_is_recorded(self, publish_config: PublishConfig) -> None:
        """``selectors_version`` 会被写进 ``publications.evidence_json``，不能是空的。

        它是"页面改版后定位"的唯一线索（R13）。空着等于没有。
        """
        for code, cfg in publish_config.platforms.items():
            if cfg.enabled:
                assert cfg.selectors_version, code
