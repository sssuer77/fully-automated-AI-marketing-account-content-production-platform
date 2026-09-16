"""真机演练：真 Playwright 打本地靶页（T5.2 · §06.13 的验收口径）。

```
studio publish dry-run --task <id> --platform douyin 走完流程到"确认发布"前一步并截图
```

这条命令打真平台需要一个**已登录的账号**（R13：不自动登录）。所以这一层用靶页
（``publish/fixtures/upload_form.html``）替代平台侧：**真浏览器、真导航、真选文件、
真填字、真回读、真截图**，只有"平台那边"换成了一个本地表单。

与 ``tests/unit/publish/test_playwright_publisher.py`` 的分工
------------------------------------------------------------
那一层用假页面把**判据**逐条验完（毫秒级）；这一层验的是"同一份流程代码对着真浏览器
也成立" —— 尤其是三件只有真浏览器才能证伪的事：``set_input_files`` 真的能选中文件、
``inner_text`` 读到的富文本真的就是刚填进去的、以及**那个按钮真的没被点**。

"按钮真的没被点"怎么验
----------------------
靶页在点下"发布"时会 ``POST /__published`` 到**本地服务器**（测试起的那个）。
所以断言不是"看像素里有没有结果块"，而是"**服务器从来没收到过那一下**" ——
这正是真平台上的性质：我们点没点，平台那边的日志说了算。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

import pytest

from studio.core.config import AccountConfig, PlatformConfig, PublishConfig
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.publish.base import (
    PublisherContext,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from studio.publish.browser import open_session
from studio.publish.platforms.fixture import FIXTURE_PAGE, FixturePublisher
from studio.publish.playwright_publisher import PlaywrightPublisher
from studio.publish.selectors import load_selector_pack
from studio.services.publish_service import DryRunRequest, PublishService

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

FIXTURES_DIR = FIXTURE_PAGE.parent

#: 靶页在点下"发布"时会打这个路径（见 fixtures/upload_form.html 的脚本）。
PUBLISHED_PATH = "/__published"


def _browser_available() -> bool:
    """Playwright 装了没、Chromium 在不在。

    不在 ⇒ **跳过**而不是失败：浏览器二进制是外部依赖（§06.5.2 把它重定向到 D 盘），
    没装它的机器上"发布链路跑不了"不是一条测试失败，而是一条环境未就绪。
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception:
        return False
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:
        return False


requires_browser = pytest.mark.skipif(
    not _browser_available(),
    reason="没有可用的 Chromium（python -m playwright install chromium）",
)


class _Handler(SimpleHTTPRequestHandler):
    """静态靶页 + 一个记账端点（POST ``/__published``）。"""

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] == PUBLISHED_PATH:
            self.server.published.append(self.path)  # type: ignore[attr-defined]
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self) -> None:
        # ``?probe=...`` 这类查询串不该让静态文件找不到。
        self.path = self.path.split("?", 1)[0]
        super().do_GET()

    def log_message(self, *_: Any) -> None:
        """闭嘴：默认实现会往 stderr 打每一条请求，把 pytest 的输出淹掉。"""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: 收到过的"点下发布"次数（**dry-run 之后必须仍然是 0**）。
        self.published: list[str] = []


@pytest.fixture
def fixture_server() -> Iterator[_Server]:
    handler = partial(_Handler, directory=str(FIXTURES_DIR))
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _ClickableFixture(FixturePublisher):
    """靶页发布器**只摘掉 dry-run 守卫**的版本（只给测试用）。

    为什么要它：:class:`FixturePublisher` 刻意拒绝 ``dry_run=False``（纵深防御），
    于是"按钮真的点得动"这条**反面对照**在它身上验不了 —— 而没有那条对照，
    "dry-run 没点"可能是因为按钮根本点不动（选择器写错了），那条断言就成了
    一个永远不会响的报警。

    继承 ``FixturePublisher`` 而不是 ``PlaywrightPublisher``：URL 组装（把
    ``ctx.probe`` 拼上去）是靶页那一层的事，绕开它会让 ``?reject=1`` 这类故障注入
    静默失效 —— 于是这条用例会"通过"，但验的是**没有故障的那条路**。
    """

    async def publish(self, req: PublishRequest) -> Any:
        return await PlaywrightPublisher.publish(self, req)


#: typeshed 里 ``BaseServer.server_address`` 的形状（主机位可能是 bytes）。
ServerAddress = tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]


class _Addressable(Protocol):
    """``_publisher`` 对服务器的**全部要求**：报一个能拼进 URL 的地址。

    写成 Protocol 而不是 ``_Server``：真服务器与只带一个假地址的桩都满足它，
    而「测试不启服务器」那几条用例不必为了迁就类型去起一个真的。
    """

    server_address: ServerAddress


def _publisher(
    tmp_paths: StudioPaths,
    server: _Addressable,
    *,
    query: str = "",
    clickable: bool = False,
) -> PlaywrightPublisher:
    """真 Playwright 的靶页发布器（URL 指向本地服务器）。"""
    base = load_selector_pack("fixture")
    host, port = server.server_address[:2]
    # typeshed 把主机位写成 ``str | bytes | bytearray``。我们自己起的服务器永远是 str，
    # 但拼 URL 前还是收一下 —— 不然将来换个实现就会拼出 ``http://b'127.0.0.1':1/``。
    host = host.decode() if isinstance(host, (bytes, bytearray)) else host
    url = f"http://{host}:{port}/{FIXTURE_PAGE.name}"
    ctx = PublisherContext(
        paths=tmp_paths,
        account=AccountConfig(
            account_id="_fixture",
            platform="douyin",
            display_name="本地靶页",
            profile_dir=tmp_paths.browser_profile_dir / "_fixture",
        ),
        platform=PlatformConfig(
            publisher="fixture",
            profile="douyin_1080x1920_30fps_v1",
            enabled=True,
            title_max=55,
            caption_max=1000,
        ),
        headless=True,
        # 故障注入走 ``probe``（与 CLI 的 ``--probe`` 同一条路径）：靶页靠它制造
        # "未登录 / 吞字 / 审核不通过"。
        probe=query,
    )
    factory = _ClickableFixture if clickable else FixturePublisher
    return factory(
        ctx,
        pack=replace(base, urls={"upload": url, "manage": url}),
        session_factory=lambda c: open_session(profile_dir=c.profile_dir, headless=True),
    )


def _request(tmp_path: Path, **overrides: Any) -> PublishRequest:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake")
    data: dict[str, Any] = {
        "task_id": "01TASK",
        "platform": "douyin",
        "account_id": "_fixture",
        "video_path": video,
        "title": "离谱跑酷地图",
        "caption": "点个关注 #跑酷",
        "dry_run": True,
    }
    data.update(overrides)
    return PublishRequest(**data)


# ── 登录态（真浏览器）────────────────────────────────────────────────


@requires_browser
class TestHealthInARealBrowser:
    async def test_logged_in(self, tmp_paths: StudioPaths, fixture_server: _Server) -> None:
        health = await _publisher(tmp_paths, fixture_server).health()
        assert health.ready and health.logged_in

    async def test_not_logged_in(self, tmp_paths: StudioPaths, fixture_server: _Server) -> None:
        health = await _publisher(tmp_paths, fixture_server, query="?logged_out=1").health()
        assert not health.ready and not health.logged_in
        assert health.hint == "尚未登录，需人工扫码登录"

    async def test_expired(self, tmp_paths: StudioPaths, fixture_server: _Server) -> None:
        """§06.13："能正确报告'未登录'与'登录态已过期'"。"""
        health = await _publisher(tmp_paths, fixture_server, query="?logged_out=1&expired=1").health()
        assert health.hint == "登录态已过期，需人工重新扫码登录"


# ── 演练（真浏览器）──────────────────────────────────────────────────


@requires_browser
class TestDryRunInARealBrowser:
    async def test_walks_the_first_seven_steps(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        result = await _publisher(tmp_paths, fixture_server).publish(_request(tmp_path))
        assert result.ok
        assert result.status == PublishStatus.QUEUED

    async def test_never_clicks_publish(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        """★ R14 的核心保护，在**真浏览器**上再验一次。

        靶页点下按钮会 POST ``/__published``；dry-run 之后服务器必须**一次都没收到**。
        这条断言比"截图里没有结果块"强得多：它验的是"那一下动作根本没发生"，
        而不是"发生之后页面看起来还行"。
        """
        await _publisher(tmp_paths, fixture_server).publish(_request(tmp_path))
        assert fixture_server.published == []

    async def test_screenshot_is_written_under_the_task(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        result = await _publisher(tmp_paths, fixture_server).publish(_request(tmp_path))
        assert result.evidence is not None
        path = result.evidence.screenshot_path
        assert path is not None and path.is_file()
        assert path.parent == tmp_paths.publish_evidence_dir("01TASK", "fixture")
        assert result.evidence.stage == "06-before-publish"

    async def test_readback_catches_swallowed_emoji(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        """第 ⑤ 步在真浏览器上真的在比：靶页**静默**吞 emoji，我们得自己发现。"""
        publisher = _publisher(tmp_paths, fixture_server, query="?eat=emoji")
        result = await publisher.publish(_request(tmp_path, caption="点个关注 😀🔥"))
        assert not result.ok
        assert result.error_code == ErrorCode.PUBLISH_UPLOAD_FAILED
        assert "emoji_stripped" in (result.error_message or "")

    async def test_readback_catches_normalised_newlines(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        publisher = _publisher(tmp_paths, fixture_server, query="?eat=newlines")
        result = await publisher.publish(_request(tmp_path, caption="第一行\n第二行"))
        assert not result.ok
        assert "whitespace_only" in (result.error_message or "")

    async def test_failure_captures_a_dom_snapshot(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        """§06.10：选择器失效要"截图 + DOM 快照"，运维据此热修 yaml。"""
        publisher = _publisher(tmp_paths, fixture_server, query="?eat=emoji")
        result = await publisher.publish(_request(tmp_path, caption="点个关注 😀"))
        assert result.evidence is not None
        dom = result.evidence.dom_snapshot_path
        assert dom is not None and dom.is_file()
        assert "本地靶页" in dom.read_text(encoding="utf-8")

    async def test_not_logged_in_blocks_before_upload(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        publisher = _publisher(tmp_paths, fixture_server, query="?logged_out=1")
        result = await publisher.publish(_request(tmp_path))
        assert result.error_code == ErrorCode.PUBLISH_LOGIN_EXPIRED
        assert result.status == PublishStatus.MANUAL_REQUIRED
        assert fixture_server.published == []

    async def test_real_publish_does_click_the_button(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        """反面对照：``dry_run=False`` 时服务器**必须**收到那一下。

        没有这条，"dry-run 没点"可能是因为**按钮根本点不动**（选择器写错了），
        而那种情况下整条链路其实是坏的 —— 一条永远不会响的报警。
        """
        result = await _publisher(tmp_paths, fixture_server, clickable=True).publish(
            _request(tmp_path, dry_run=False)
        )
        assert result.ok and result.status == PublishStatus.PUBLISHED
        assert len(fixture_server.published) == 1

    async def test_review_rejection_in_a_real_browser(
        self, tmp_paths: StudioPaths, tmp_path: Path, fixture_server: _Server
    ) -> None:
        result = await _publisher(tmp_paths, fixture_server, query="?reject=1", clickable=True).publish(
            _request(tmp_path, dry_run=False)
        )
        assert result.error_code == ErrorCode.PUBLISH_REVIEW_REJECTED
        assert result.evidence is not None
        assert result.evidence.platform_text == "审核不通过"


# ── 服务层（真浏览器 + 真库）─────────────────────────────────────────


@requires_browser
class TestServiceDryRun:
    """``PublishService.dry_run`` 把上面那些接起来 —— 这一层验"接线对不对"。"""

    async def test_missing_platform_is_rejected(self, tmp_paths: StudioPaths) -> None:
        service = PublishService(None, paths=tmp_paths, publish=_config(tmp_paths))
        with pytest.raises(Exception) as info:
            await service.dry_run(DryRunRequest(task_id="01TASK", platform="myspace", target="fixture"))
        assert "myspace" in str(info.value)

    async def test_bad_target_is_rejected(self, tmp_paths: StudioPaths) -> None:
        service = PublishService(None, paths=tmp_paths, publish=_config(tmp_paths))
        with pytest.raises(Exception) as info:
            await service.dry_run(DryRunRequest(task_id="01TASK", platform="douyin", target="nowhere"))
        assert "nowhere" in str(info.value)

    async def test_fixture_target_refuses_to_really_publish(self, tmp_paths: StudioPaths) -> None:
        """靶页发布器自己就拒绝 ``dry_run=False``（纵深防御，见 platforms/fixture.py）。"""
        publisher = _publisher(tmp_paths, _DummyServer())
        result: PublishResult = await publisher.publish(
            PublishRequest(
                task_id="01TASK",
                platform="douyin",
                account_id="_fixture",
                video_path=Path("x.mp4"),
                dry_run=False,
            )
        )
        assert result.error_code == ErrorCode.PUBLISH_NOT_IMPLEMENTED


class _DummyServer:
    #: 与 :class:`_Addressable` 同型（``_publisher`` 只读这一个属性，不该为了它起真服务器）。
    server_address: ServerAddress = ("127.0.0.1", 1)


def _config(tmp_paths: StudioPaths) -> Any:
    return PublishConfig(
        platforms={
            "douyin": PlatformConfig(
                publisher="fixture",
                profile="p1",
                enabled=True,
                title_max=55,
                caption_max=1000,
            )
        }
    )
