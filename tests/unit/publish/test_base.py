"""``Publisher`` 抽象与注册表（T5.2 · §4.6.1）。

两条容易写错、写错了又不报错的东西，各有一组用例：

1. **失败结果的默认状态**。:meth:`PublishResult.failure` 默认落 ``manual_required``
   而不是 ``failed`` —— 后者是"还会自动重试"的中间态，让"忘记传状态"落到它上面，
   会把一条发不出去的片子重试到天亮（§06.10 六种场景里五种都该转人工）。
2. **``PublishHealth`` 探不出来时不许默认放行**。"探测失败 ⇒ ready=True"会把
   一个问题从"登录态过期"（一眼可修）推迟成"上传失败"（要去翻截图）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import ClassVar

import pytest

from studio.core.config import AccountConfig, PlatformConfig
from studio.core.errors import ErrorCode, PublishError
from studio.core.paths import StudioPaths
from studio.publish.base import (
    PUBLISHERS,
    Publisher,
    PublisherContext,
    PublishEvidence,
    PublishHealth,
    PublishMetrics,
    PublishRequest,
    PublishResult,
    PublishStatus,
    get_publisher,
    register_publisher,
)
from studio.publish.platforms import NON_PLATFORM_CODES, REAL_PLATFORMS
from studio.publish.platforms.douyin import DouyinPublisher
from studio.publish.playwright_publisher import PlaywrightPublisher

# ── 上下文 ────────────────────────────────────────────────────────────


def _ctx(tmp_paths: StudioPaths) -> PublisherContext:
    return PublisherContext(
        paths=tmp_paths,
        account=AccountConfig(
            account_id="acc_main",
            platform="douyin",
            profile_dir=tmp_paths.browser_profile_dir / "acc_main",
        ),
        platform=PlatformConfig(
            publisher="douyin", profile="p1", enabled=True, title_max=55, caption_max=1000
        ),
    )


class TestPublisherContext:
    def test_profile_dir_is_per_account(self, tmp_paths: StudioPaths) -> None:
        """§06.2.4「登录态隔离」：每账号一个目录，互不干扰。"""
        ctx = _ctx(tmp_paths)
        assert ctx.profile_dir == tmp_paths.browser_profile_dir / "acc_main"

    def test_two_accounts_never_share_a_profile_dir(self, tmp_paths: StudioPaths) -> None:
        first = _ctx(tmp_paths)
        second = PublisherContext(
            paths=tmp_paths,
            account=AccountConfig(
                account_id="acc_second",
                platform="douyin",
                profile_dir=tmp_paths.browser_profile_dir / "acc_second",
            ),
            platform=first.platform,
        )
        assert first.profile_dir != second.profile_dir

    def test_convenience_accessors(self, tmp_paths: StudioPaths) -> None:
        ctx = _ctx(tmp_paths)
        assert ctx.account_id == "acc_main"
        assert ctx.platform_code == "douyin"

    def test_headless_defaults_to_true(self, tmp_paths: StudioPaths) -> None:
        """§06.5.2：headless 默认 true；首次扫码才临时开成 false。"""
        assert _ctx(tmp_paths).headless is True

    def test_probe_defaults_to_empty(self, tmp_paths: StudioPaths) -> None:
        assert _ctx(tmp_paths).probe == ""


# ── 结果构造 ──────────────────────────────────────────────────────────


class TestPublishResult:
    def test_failure_defaults_to_manual_required(self) -> None:
        """★ §06.10：六种触发场景里五种都该转人工。"""
        result = PublishResult.failure(ErrorCode.PUBLISH_LOGIN_EXPIRED, "登录态失效")
        assert not result.ok
        assert result.status == PublishStatus.MANUAL_REQUIRED
        assert result.error_code == "PUBLISH_LOGIN_EXPIRED"

    def test_failure_can_explicitly_ask_for_retry(self) -> None:
        """限频是**唯一**该回 ``failed`` 的那一种（job 回 pending 顺延，§06.10）。"""
        result = PublishResult.failure(
            ErrorCode.PUBLISH_RATELIMIT, "今日额度用完", status=PublishStatus.FAILED
        )
        assert result.status == PublishStatus.FAILED

    def test_failure_accepts_a_plain_string_code(self) -> None:
        assert PublishResult.failure("PUBLISH_UNKNOWN", "x").error_code == "PUBLISH_UNKNOWN"

    def test_published_fills_published_at(self) -> None:
        result = PublishResult.published(url="https://x/1", platform_post_id="1")
        assert result.ok and result.status == PublishStatus.PUBLISHED
        assert result.published_at

    def test_published_without_url_is_allowed(self) -> None:
        """平台没给出链接时不该把一次成功的发布判成失败。"""
        result = PublishResult.published(url=None, platform_post_id=None)
        assert result.ok and result.url is None

    def test_stopped_before_publish_is_ok_but_not_published(self) -> None:
        """★ dry-run 的落点（§06.5.3 第 ⑥ 步：``ok=true, status='queued'``）。

        写成 ``ok=false`` 会让"演练成功"在面板上与"演练失败"长得一样。
        """
        result = PublishResult.stopped_before_publish()
        assert result.ok
        assert result.status == PublishStatus.QUEUED
        assert result.published_at is None
        assert result.url is None

    def test_evidence_round_trips(self) -> None:
        evidence = PublishEvidence(
            screenshot_path=Path("data/work/t/publish/douyin/x.png"),
            selector_version="2026-09-13",
            stage="99-failure",
        )
        assert PublishResult.failure("X", "y", evidence=evidence).evidence is evidence

    def test_result_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            PublishResult.stopped_before_publish().ok = False  # type: ignore[misc]

    def test_elapsed_ms_defaults_to_zero(self) -> None:
        assert PublishResult.stopped_before_publish().elapsed_ms == 0


class TestPublishHealth:
    def test_unknown_is_not_ready(self) -> None:
        """★ 探不出来 ⇒ ``ready=False``，**不是**默认放行。"""
        health = PublishHealth.unknown("探测失败")
        assert not health.ready and not health.logged_in
        assert health.hint == "探测失败"
        assert health.last_check_at

    def test_ready_and_logged_in_are_separate_fields(self) -> None:
        """`ready` 说"这次探测做成了没"，`logged_in` 说"登录态在不在"。"""
        health = PublishHealth(ready=True, logged_in=False, last_check_at="2026-09-16T00:00:00Z")
        assert health.ready and not health.logged_in

    def test_health_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            PublishHealth.unknown("x").ready = True  # type: ignore[misc]


class TestPublishMetrics:
    def test_all_optional_except_collected_at(self) -> None:
        metrics = PublishMetrics(collected_at="2026-09-16T00:00:00Z")
        assert metrics.views is None and metrics.shares is None

    def test_accepts_numbers(self) -> None:
        metrics = PublishMetrics(collected_at="x", views=1, likes=2, comments=3, shares=4)
        assert (metrics.views, metrics.likes, metrics.comments, metrics.shares) == (1, 2, 3, 4)


class TestPublishRequest:
    def test_defaults_are_conservative(self) -> None:
        req = PublishRequest(task_id="01T", platform="douyin", account_id="acc", video_path=Path("x.mp4"))
        assert req.dry_run is False
        assert req.title == "" and req.caption == "" and req.tags == ()
        assert req.cover_path is None and req.scheduled_at is None

    def test_is_frozen(self) -> None:
        req = PublishRequest(task_id="01T", platform="douyin", account_id="acc", video_path=Path("x.mp4"))
        with pytest.raises(FrozenInstanceError):
            req.title = "x"  # type: ignore[misc]


# ── 抽象 ──────────────────────────────────────────────────────────────


class _StubPublisher(Publisher):
    platform: ClassVar[str] = "stub"

    async def health(self) -> PublishHealth:
        return PublishHealth(ready=True, logged_in=True, last_check_at="now")

    async def publish(self, req: PublishRequest) -> PublishResult:
        return PublishResult.stopped_before_publish()

    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:
        return PublishMetrics(collected_at="now", views=1)


class TestPublisherAbc:
    def test_cannot_instantiate_the_abc(self, tmp_paths: StudioPaths) -> None:
        with pytest.raises(TypeError):
            Publisher(_ctx(tmp_paths))  # type: ignore[abstract]

    def test_incomplete_subclass_cannot_be_instantiated(self, tmp_paths: StudioPaths) -> None:
        class _Half(Publisher):
            platform: ClassVar[str] = "half"

            async def health(self) -> PublishHealth:
                return PublishHealth(ready=True, logged_in=True, last_check_at="now")

        with pytest.raises(TypeError):
            _Half(_ctx(tmp_paths))  # type: ignore[abstract]

    async def test_mock_implementation_is_substitutable(self, tmp_paths: StudioPaths) -> None:
        """§06.13 的验收口径："Mock 实现可替换"。"""
        stub = _StubPublisher(_ctx(tmp_paths))
        assert (await stub.health()).ready
        assert (await stub.publish(_request())).ok
        assert (await stub.fetch_metrics("1")).views == 1

    async def test_login_default_is_an_honest_not_implemented(self, tmp_paths: StudioPaths) -> None:
        """没做扫码登录的实现 ⇒ **抛** ``PUBLISH_NOT_IMPLEMENTED``，不是回"没登录"。

        回 ``ready=False`` 会让用户以为是自己没扫，于是去点第二次、第三次；
        抛出来面板才能说"这个平台一期没做"。这两句话的下一步动作完全不同。
        """
        with pytest.raises(PublishError) as info:
            await _StubPublisher(_ctx(tmp_paths)).login()
        assert info.value.code == ErrorCode.PUBLISH_NOT_IMPLEMENTED
        assert "stub" in info.value.message


def _request() -> PublishRequest:
    return PublishRequest(task_id="01T", platform="stub", account_id="acc", video_path=Path("x.mp4"))


# ── 注册表 ────────────────────────────────────────────────────────────


class TestRegistry:
    def test_known_platforms_are_registered(self) -> None:
        """§4.6.1 的注册表：七个真平台（一线 3 + 二线 4）+ 靶页。"""
        assert {"douyin", "kuaishou", "shipinhao"} <= set(PUBLISHERS)
        assert {"xiaohongshu", "bilibili", "xigua", "weibo"} <= set(PUBLISHERS)

    def test_get_publisher_returns_the_class(self) -> None:
        assert get_publisher("douyin") is DouyinPublisher

    def test_unknown_platform_raises_not_implemented(self) -> None:
        with pytest.raises(PublishError) as info:
            get_publisher("myspace")
        assert info.value.code == ErrorCode.PUBLISH_NOT_IMPLEMENTED
        assert "myspace" in info.value.message

    def test_registering_without_a_platform_code_is_rejected(self) -> None:
        class _NoCode(Publisher):
            platform: ClassVar[str] = ""

            async def health(self) -> PublishHealth:  # pragma: no cover - 不会跑到
                raise NotImplementedError

            async def publish(self, req: PublishRequest) -> PublishResult:  # pragma: no cover
                raise NotImplementedError

            async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(PublishError) as info:
            register_publisher(_NoCode)
        assert "platform" in info.value.message

    def test_registering_the_same_code_twice_is_rejected(self) -> None:
        class _A(Publisher):
            platform: ClassVar[str] = "dup_test"

            async def health(self) -> PublishHealth:  # pragma: no cover
                raise NotImplementedError

            async def publish(self, req: PublishRequest) -> PublishResult:  # pragma: no cover
                raise NotImplementedError

            async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics:  # pragma: no cover
                raise NotImplementedError

        class _B(_A):
            pass

        register_publisher(_A)
        try:
            with pytest.raises(PublishError) as info:
                register_publisher(_B)
            assert "dup_test" in info.value.message
        finally:
            PUBLISHERS.pop("dup_test", None)

    def test_reregistering_the_same_class_is_idempotent(self) -> None:
        """模块被重复导入时（比如 reload）不该炸。"""
        register_publisher(DouyinPublisher)
        assert PUBLISHERS["douyin"] is DouyinPublisher

    def test_real_platforms_excludes_the_fixture_code(self) -> None:
        assert "fixture" in NON_PLATFORM_CODES
        assert "fixture" not in REAL_PLATFORMS
        assert set(REAL_PLATFORMS) == set(PUBLISHERS) - set(NON_PLATFORM_CODES)

    def test_every_real_platform_is_a_real_implementation(self) -> None:
        """七个真平台**全部**是真实现（二线那四个空壳换掉了）。

        为什么值得钉：注册表回答的是"实现**在不在**"，而"能不能发"是**另一个**问题
        —— 它由 ``selectors/<platform>.yaml`` 的 ``calibrated`` / ``known_gaps`` 回答
        （七个里只有抖音验过，见 ``test_selectors.py::TestCalibrationStatus``）。
        把两件事混在一起正是空壳时代的毛病：那时"没做"与"做了但没校准"看起来一样。

        ⚠️ 这条**不开浏览器、不联网** —— 只是查注册表。原来那三条（"二线的 health 不抛"、
        "publish 返回 NOT_IMPLEMENTED"）会**真的去启动 Chromium 打 B 站**：空壳时代它们
        廉价，现在它们既慢又在联网。空壳没了，那三条也就没有了要验的东西。
        """
        for code in REAL_PLATFORMS:
            assert issubclass(PUBLISHERS[code], PlaywrightPublisher), code
