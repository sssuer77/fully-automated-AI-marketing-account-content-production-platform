"""契约：发布账号面板（T6.4 · §06.2.4 / §06.12）。

五个端点，五件事
----------------
① ``GET    /api/v1/publish/accounts`` —— 账号清单（含停用的）+ 平台清单 + 表单上下限；
② ``PUT    /api/v1/publish/accounts/{id}`` —— 加号 / 改号（**先校验、后落盘**）；
③ ``DELETE /api/v1/publish/accounts/{id}`` —— 删号（**登录态目录不碰**）；
④ ``POST   /api/v1/publish/accounts/{id}/probe`` —— 无头探一眼登录态；
⑤ ``POST   /api/v1/publish/accounts/{id}/login`` —— 开**可见**窗口等人扫码（R13）。

为什么"盘上那份文件"也要断言
----------------------------
这一层的返回值全是"我们打算写什么"，而用户真正得到的是 ``config/publish.yaml``
**变成了什么**。只断言响应体的用例，会在"写盘那一步悄悄把 ``platforms`` 段抹了"
的时候全绿 —— 而那正是这一层最贵的失败（下一次启动起不来，且注释再也回不来）。

为什么"没变就不留痕"单独验
--------------------------
同一个账号存两次在审计里出现两行，等于让 ``audit_ops`` 回答不了"这个号是谁什么时候
加的"：读的人分不清哪一行才是真加的那次。

为什么发布器是**注入**的
------------------------
「扫码登录」要起一个真浏览器。用例以"这台机器装没装 Chromium、有没有网"为前提，
迟早会变成最难查的那种 flaky。注入之后，这一层验的是**拿到 health 之后面板说什么**、
以及"窗口可见不可见"（``headless=False``）这件事有没有被传对。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.support import restore_factory_accounts

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.config import AccountConfig, load_publish_config
from studio.core.errors import ErrorCode, PublishError
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.db.repositories.publication_repo import PUBLISHED, PublicationRepo
from studio.domain import TaskService
from studio.publish.base import Publisher, PublishHealth, PublishRequest, PublishResult
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

ACCOUNTS_URL = "/api/v1/publish/accounts"

PUBLISH_URL = "/api/v1/publish"


class _FakePublisher:
    """假发布器：登录态探测与扫码登录都只回答预设的那句话。

    刻意把 ``login`` 与 ``health`` 的答案**分开**：真机上"窗口里扫完了"与"下一次
    无头探测说登录了"本来就可能不一致（页面没跳转），而服务层正好要处理那一格。
    """

    def __init__(
        self,
        *,
        ready: bool = True,
        hint: str | None = None,
        account_name: str | None = "主账号",
        login_ready: bool | None = None,
        health_ready: bool | None = None,
        error: Exception | None = None,
        publish_result: PublishResult | None = None,
    ) -> None:
        self.ready = ready
        self.hint = hint
        self.account_name = account_name
        self.login_ready = ready if login_ready is None else login_ready
        self.health_ready = ready if health_ready is None else health_ready
        self.error = error
        self.publish_result = publish_result or PublishResult.published(
            url="https://example.invalid/v/1", platform_post_id="post-1"
        )
        self.login_timeouts: list[float] = []
        self.health_calls = 0
        #: 「人工过验证」那条路要的东西：每一次真发布请求（**这条路不碰队列**）。
        self.publish_requests: list[PublishRequest] = []

    async def health(self) -> PublishHealth:
        self.health_calls += 1
        if self.error is not None:
            raise self.error
        return PublishHealth(
            ready=self.health_ready,
            logged_in=self.health_ready,
            last_check_at="2026-09-22T06:00:00.000Z",
            account_name=self.account_name,
            hint=None if self.health_ready else (self.hint or "尚未登录，需人工扫码登录"),
        )

    async def login(self, *, timeout_sec: float = 0.0) -> PublishHealth:
        self.login_timeouts.append(timeout_sec)
        if self.error is not None:
            raise self.error
        return PublishHealth(
            ready=self.login_ready,
            logged_in=self.login_ready,
            last_check_at="2026-09-22T06:00:00.000Z",
            account_name=self.account_name,
            hint=None if self.login_ready else (self.hint or "等了 180 秒没等到扫码完成"),
        )

    async def publish(self, req: PublishRequest) -> PublishResult:
        self.publish_requests.append(req)
        return self.publish_result


class _FakeBuilder:
    """可换的假装配器 —— 顺带记下"这一次要的是可见窗口还是无头"（**这条最要紧**），
    以及"这一次窗口前面有没有人"（``await_manual_verify``）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self, publisher: _FakePublisher | None = None) -> None:
        self.publisher = publisher or _FakePublisher()
        self.headless: list[bool] = []
        self.accounts: list[str] = []
        self.await_manual_verify: list[bool] = []

    def __call__(
        self,
        account: AccountConfig,
        *,
        headless: bool,
        await_manual_verify: bool = False,
    ) -> Publisher:
        self.headless.append(headless)
        self.accounts.append(account.account_id)
        self.await_manual_verify.append(await_manual_verify)
        return cast("Publisher", self.publisher)


#: 用例之间共享同一个装配器（`state` 夹具把它的**函数**交给服务层），
#: 每个用例开头由下面那条 autouse 夹具复位 —— 换一个假件就够，不必重建 app。
PUBLISHERS = _FakeBuilder()


@pytest.fixture(autouse=True)
def _reset_publishers() -> Iterator[None]:
    PUBLISHERS.reset()
    yield
    PUBLISHERS.reset()


class _Probe:
    """假资源探针 —— 不碰真 ``nvidia-smi`` 与真磁盘（与设置面板那条同一手法）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-22T06:00:00.000Z",
            cpu_pct=10.0,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=50.0,
            disk_free_d_gb=100.0,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=False,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=8192,
        )


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录 + 真配置抄一份（**不抄 secrets.yaml** —— 密钥是这台机器的私事）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        if source.name == "secrets.yaml":
            continue
        shutil.copyfile(source, value.config_dir / source.name)
    # 抄完之后把账号段**换回出厂那两个**（段外的平台表 / 注释照旧是盘上那份）：
    # 下面这些用例断言的就是账号清单本身（"加一个号之后是三条" / "删到一个不剩 ⇒
    # ``accounts: []``"），而盘上那份是**这台机器的现状**（见 tests/support.py）。
    restore_factory_accounts(value.config_dir / "publish.yaml")
    return value


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
        publish_publishers=PUBLISHERS,
    )
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def connection(state: AppState) -> sqlite3.Connection:
    return state.connections.get()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


def _config_text(paths: StudioPaths) -> str:
    return (paths.config_dir / "publish.yaml").read_text(encoding="utf-8")


def _account_ids(paths: StudioPaths) -> list[str]:
    return [account.account_id for account in load_publish_config(paths).accounts]


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": "douyin",
        "display_name": "副账号",
        "enabled": True,
        "daily_limit": 2,
        "min_gap_min": 60,
    }
    return payload | overrides


# ── 读 ──────────────────────────────────────────────────────────────────


def test_first_screen_lists_accounts_platforms_and_limits(client: TestClient) -> None:
    """出厂状态：两个账号（含停用的演练台）、七个平台、表单上下限、以及必须说的话。"""
    body = client.get(ACCOUNTS_URL).json()

    assert [row["account_id"] for row in body["accounts"]] == ["acc_main", "_rehearsal"]
    main = body["accounts"][0]
    assert main["platform"] == "douyin"
    assert main["profile_dir"] == "data/browser_profile/acc_main"
    assert main["runtime_profile_dir"].endswith("data/browser_profile/acc_main")
    assert main["profile_dir_matches_runtime"] is True
    assert body["profile_dir_prefix"] == "data/browser_profile"

    codes = {row["code"] for row in body["platforms"]}
    assert {"douyin", "kuaishou", "shipinhao", "other"} <= codes
    assert body["limits"]["account_id_max"] > 0
    assert body["limits"]["daily_limit_max"] > body["limits"]["daily_limit_min"]
    # "扫码是人的活"必须跟着账号区块一起给（R13：不自动登录）
    assert any("扫码" in note for note in body["notes"])


def test_account_view_says_whether_a_login_dir_exists(client: TestClient, paths: StudioPaths) -> None:
    """登录态目录不存在 ⇒ 明说"还没登录"；存在 ⇒ 只说"登录过"（会话是否有效要真发一次）。"""
    fresh = next(
        row for row in client.get(ACCOUNTS_URL).json()["accounts"] if row["account_id"] == "acc_main"
    )
    assert fresh["profile_dir_exists"] is False
    assert "还没有登录态" in fresh["note"]

    (paths.browser_profile_dir / "acc_main").mkdir(parents=True, exist_ok=True)
    logged = next(
        row for row in client.get(ACCOUNTS_URL).json()["accounts"] if row["account_id"] == "acc_main"
    )
    assert logged["profile_dir_exists"] is True
    assert "登录过" in logged["note"]


def test_account_on_a_disabled_platform_says_it_will_be_skipped(
    client: TestClient, paths: StudioPaths
) -> None:
    """二线平台（``enabled=false``）上的账号 ⇒ 面板当场说"投了会被跳过"。"""
    response = client.put(
        f"{ACCOUNTS_URL}/xhs_main",
        json=_body(platform="xiaohongshu", display_name="小红书主号"),
    )
    assert response.status_code == 200

    row = next(item for item in response.json()["accounts"] if item["account_id"] == "xhs_main")
    assert "未启用" in row["note"]


# ── 写：加号 / 改号 ─────────────────────────────────────────────────────


def test_put_creates_the_account_and_writes_the_file(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """加一个号：文件里真的多了一条、响应带刷新后的整屏、审计里有一行。"""
    response = client.put(f"{ACCOUNTS_URL}/acc_second", json=_body(reason="开第二个号"))

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["changed"] is True
    assert _account_ids(paths) == ["acc_main", "_rehearsal", "acc_second"]

    ops = AuditRepo(connection).list_recent(limit=10)
    assert [op.action for op in ops] == ["publish.account_created"]
    assert ops[0].target_id == "publish.accounts.acc_second"
    assert ops[0].after["daily_limit"] == 2
    assert ops[0].before == {}
    assert ops[0].reason == "开第二个号"


def test_put_defaults_profile_dir_to_the_runtime_path(client: TestClient, paths: StudioPaths) -> None:
    """``profile_dir`` 留空 ⇒ 补上**运行期真正会用的**那个目录，而不是留个空值。"""
    response = client.put(f"{ACCOUNTS_URL}/acc_second", json=_body())

    assert response.status_code == 200
    row = next(item for item in response.json()["accounts"] if item["account_id"] == "acc_second")
    assert row["profile_dir"] == "data/browser_profile/acc_second"
    assert row["profile_dir_matches_runtime"] is True


def test_put_twice_with_the_same_body_writes_nothing_twice(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """同一个号存两次 ⇒ ``changed=false``、文件不动、审计**只有一行**。"""
    client.put(f"{ACCOUNTS_URL}/acc_second", json=_body())
    before = _config_text(paths)

    second = client.put(f"{ACCOUNTS_URL}/acc_second", json=_body())

    assert second.json()["changed"] is False
    assert second.json()["created"] is False
    assert _config_text(paths) == before
    assert len(AuditRepo(connection).list_recent(limit=10)) == 1


def test_put_updates_an_existing_account_in_place(client: TestClient, paths: StudioPaths) -> None:
    """改号：值换掉，而**段外的 platforms 与行尾注释一个字节都不动**。"""
    before = _config_text(paths)

    response = client.put(f"{ACCOUNTS_URL}/acc_main", json=_body(daily_limit=5, enabled=False))

    after = _config_text(paths)
    assert response.json()["created"] is False
    assert response.json()["changed"] is True
    assert "    daily_limit: 5\n" in after
    assert "    profile_dir: data/browser_profile/acc_main   # 持久化登录态（.gitignore）\n" in after
    assert before.split("\nplatforms:", 1)[1] == after.split("\nplatforms:", 1)[1]


def test_put_reports_form_errors_field_by_field(client: TestClient, paths: StudioPaths) -> None:
    """表单不合法 ⇒ 422 + ``context.errors``（面板据此把红字标到那个框上）。"""
    before = _config_text(paths)

    response = client.put(f"{ACCOUNTS_URL}/acc_bad", json=_body(platform="tiktok", daily_limit=0))

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == ErrorCode.VALIDATION_FAILED.value
    fields = " ".join(row["field"] for row in payload["context"]["errors"])
    assert "platform" in fields
    assert "daily_limit" in fields
    assert _config_text(paths) == before


def test_put_refuses_two_accounts_sharing_a_profile_dir(client: TestClient, paths: StudioPaths) -> None:
    """共用 ``profile_dir`` ⇒ 400 且盘上不动（共用目录会让两个号轮流失效）。"""
    before = _config_text(paths)

    response = client.put(
        f"{ACCOUNTS_URL}/acc_second",
        json=_body(profile_dir="data/browser_profile/acc_main"),
    )

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.CONFIG_INVALID.value
    assert _config_text(paths) == before


# ── 写：删号 ────────────────────────────────────────────────────────────


def test_delete_removes_the_account_and_keeps_the_login_dir(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """删号：配置里没了、**登录态目录还在**（凭据不是面板该动的东西）、审计有 ``before``。"""
    client.put(f"{ACCOUNTS_URL}/acc_second", json=_body())
    login_dir = paths.browser_profile_dir / "acc_second"
    login_dir.mkdir(parents=True, exist_ok=True)

    response = client.delete(f"{ACCOUNTS_URL}/acc_second", params={"reason": "不做了"})

    assert response.status_code == 200
    assert response.json()["removed"] == "acc_second"
    assert _account_ids(paths) == ["acc_main", "_rehearsal"]
    assert login_dir.is_dir(), "面板不该碰 data/browser_profile/ —— 那是凭据（§02.5）"

    removed = next(
        op for op in AuditRepo(connection).list_recent(limit=10) if op.action == "publish.account_removed"
    )
    assert removed.target_id == "publish.accounts.acc_second"
    assert removed.before["platform"] == "douyin"
    assert removed.reason == "不做了"


def test_delete_of_an_unknown_account_is_a_404(client: TestClient) -> None:
    """删一个配置里没有的号 ⇒ 404（"你手上这一屏过时了"，不是"请求写错了"）。"""
    response = client.delete(f"{ACCOUNTS_URL}/acc_ghost")

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.PUBLISH_ACCOUNT_NOT_FOUND.value


def test_delete_can_empty_the_accounts_block(client: TestClient, paths: StudioPaths) -> None:
    """删到一个不剩 ⇒ 文件里留 ``accounts: []``（留空行会让下一次加载炸）。"""
    for account_id in ("_rehearsal", "acc_main"):
        assert client.delete(f"{ACCOUNTS_URL}/{account_id}").status_code == 200

    assert _account_ids(paths) == []
    assert "accounts: []\n" in _config_text(paths)
    assert client.get(ACCOUNTS_URL).json()["accounts"] == []


def test_notes_warn_when_nothing_is_enabled(client: TestClient, paths: StudioPaths) -> None:
    """一个启用的号都没有 ⇒ 面板必须当场说出来（否则投递看起来"点了没反应"）。"""
    client.put(f"{ACCOUNTS_URL}/acc_main", json=_body(enabled=False))
    client.put(f"{ACCOUNTS_URL}/_rehearsal", json=_body(platform="other", enabled=False))

    notes = client.get(ACCOUNTS_URL).json()["notes"]
    assert any("一个启用的账号都没有" in note for note in notes)


# ── 登录态：探一眼 ──────────────────────────────────────────────────────


def test_probe_reports_a_usable_login_state(client: TestClient, paths: StudioPaths) -> None:
    """探测成功 ⇒ 整屏 + health + 一句"可以投递了"，而且**盘上一个字节都没动**。"""
    before = _config_text(paths)

    response = client.post(f"{ACCOUNTS_URL}/acc_main/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "probe"
    assert body["health"]["ready"] is True
    assert body["health"]["account_name"] == "主账号"
    assert "可以投递" in body["note"]
    # 回的是**整屏**（与写操作同一形状）：面板拿到它就能整块重画，不必再发一次 GET
    assert [row["account_id"] for row in body["accounts"]] == ["acc_main", "_rehearsal"]
    assert _config_text(paths) == before
    # 探测走无头（看不见、跑得快）；只有扫码登录才需要可见窗口
    assert PUBLISHERS.headless == [True]
    assert PUBLISHERS.accounts == ["acc_main"]


def test_probe_that_finds_nobody_logged_in_says_what_to_do_next(client: TestClient) -> None:
    """没登录 ⇒ 200 + 一句**照做**的话（不是红叉，也不是"失败了"）。"""
    PUBLISHERS.reset(_FakePublisher(ready=False, hint="尚未登录，需人工扫码登录"))

    body = client.post(f"{ACCOUNTS_URL}/acc_main/probe").json()

    assert body["health"]["ready"] is False
    assert "扫码登录" in body["note"]


def test_probe_of_an_unknown_account_is_a_404(client: TestClient) -> None:
    """探一个配置里没有的号 ⇒ 404（与删号同一条判据）。"""
    response = client.post(f"{ACCOUNTS_URL}/acc_ghost/probe")

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == ErrorCode.PUBLISH_ACCOUNT_NOT_FOUND.value
    assert payload["context"]["known"] == ["acc_main", "_rehearsal"]


def test_probe_does_not_write_audit_rows(client: TestClient, connection: sqlite3.Connection) -> None:
    """ "看了一眼登录态"不进 ``audit_ops``（审计记的是决定，不是观看）。"""
    client.post(f"{ACCOUNTS_URL}/acc_main/probe")

    actions = [op.action for op in AuditRepo(connection).list_recent(limit=20)]
    assert "publish.account_logged_in" not in actions


# ── 登录态：扫码登录 ────────────────────────────────────────────────────


def test_login_opens_a_visible_window(client: TestClient) -> None:
    """扫码登录必须开**可见**窗口 —— 无头窗口里没有人能扫那个码。"""
    response = client.post(f"{ACCOUNTS_URL}/acc_main/login")

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "login"
    assert body["health"]["ready"] is True
    assert PUBLISHERS.headless == [False]
    assert PUBLISHERS.publisher.login_timeouts == [180.0]


def test_login_timeout_tells_you_which_app_to_scan_with(client: TestClient) -> None:
    """等不到扫码 ⇒ **200**（不是错误）+ 一句写全的话：用哪个 App 扫、扫完会怎样。"""
    PUBLISHERS.reset(_FakePublisher(ready=False))

    response = client.post(f"{ACCOUNTS_URL}/acc_main/login")

    assert response.status_code == 200
    body = response.json()
    assert body["health"]["ready"] is False
    # "扫码"两个字对没配过账号的人不够：平台那个码只认**自家 App**
    assert "App" in body["note"]
    assert "扫一扫" in body["note"]
    # 超时之后又无头探了一次（人可能真扫了，只是页面没跳转）
    assert PUBLISHERS.headless == [False, True]


def test_login_succeeds_even_when_the_page_never_redirected(client: TestClient) -> None:
    """扫了码但页面没跳转 ⇒ 以**随后那次无头探测**为准，而不是报"没扫到"。

    报超时会让用户对着一个其实已经好的账号再扫一遍 —— 而"到底登上了没有"的判据
    始终是平台页面说的那句话，不是我们这一次窗口里看到的那一帧。
    """
    PUBLISHERS.reset(_FakePublisher(login_ready=False, health_ready=True))

    body = client.post(f"{ACCOUNTS_URL}/acc_main/login").json()

    assert body["health"]["ready"] is True
    assert "可以投递" in body["note"]
    assert PUBLISHERS.headless == [False, True]


def test_login_records_the_credential_event(client: TestClient, connection: sqlite3.Connection) -> None:
    """登上了 ⇒ 留一行 ``publish.account_logged_in``（凭据状态变了，值得记）。"""
    client.post(f"{ACCOUNTS_URL}/acc_main/login")

    op = next(
        row
        for row in AuditRepo(connection).list_recent(limit=10)
        if row.action == "publish.account_logged_in"
    )
    assert op.target_id == "publish.accounts.acc_main"
    assert op.after["account_name"] == "主账号"


def test_login_failure_is_not_audited(client: TestClient, connection: sqlite3.Connection) -> None:
    """没登上 ⇒ 不进审计（没发生的事不该在 ``audit_ops`` 里留下一行"登录"）。"""
    PUBLISHERS.reset(_FakePublisher(ready=False))

    client.post(f"{ACCOUNTS_URL}/acc_main/login")

    actions = [op.action for op in AuditRepo(connection).list_recent(limit=20)]
    assert "publish.account_logged_in" not in actions


def test_login_timeout_is_capped(client: TestClient) -> None:
    """等扫码的上限有边界：``timeout_sec=5`` ⇒ 422，不能让人把窗口开一整天。"""
    assert client.post(f"{ACCOUNTS_URL}/acc_main/login", params={"timeout_sec": 5}).status_code == 422
    assert client.post(f"{ACCOUNTS_URL}/acc_main/login", params={"timeout_sec": 9999}).status_code == 422


def test_login_on_a_platform_without_an_adapter_is_a_503(client: TestClient) -> None:
    """这个平台没覆盖 ``login``（靶页 / 未启用平台）⇒ 503（服务端此刻的能力问题），不是 500。"""
    PUBLISHERS.reset(
        _FakePublisher(
            error=PublishError(
                "平台 bilibili 不支持从面板扫码登录",
                code=ErrorCode.PUBLISH_NOT_IMPLEMENTED,
            )
        )
    )

    response = client.post(f"{ACCOUNTS_URL}/acc_main/login")

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.PUBLISH_NOT_IMPLEMENTED.value


# ── 「人工过验证」（T6.4 · 真机 2026-09-23）─────────────────────────────


@pytest.fixture
def waiting(state: AppState, paths: StudioPaths) -> str:
    """一条**待人工**的发布记录 —— 真机上那种"点下发布之后平台要短信验证"的形态。

    为什么造在库里而不是打一个假接口：这条路要的是**一条真记录 + 盘上一份真成片**
    （服务会去 ``Path(row.video_path).is_file()``）。少了任何一样，用例验的就成了
    夹具自己的假设。
    """
    connection = state.connections.get()
    task = TaskService(connection).create(title="跑酷合集")
    video = paths.videos_dir / f"20260923-120000_{task.id}_final.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 64)
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=task.id,
        platform="douyin",
        account_id="acc_main",
        video_path=video.as_posix(),
        video_sha256="0" * 64,
        title="离谱跑酷地图",
        caption="点个关注",
    )
    repo.mark_uploading(row.id)
    repo.mark_manual_required(
        row.id,
        error_code=ErrorCode.PUBLISH_FAILED.value,
        error_message="平台要求短信验证：这一步要人来做，自动流程到此为止",
    )
    return row.id


def test_assist_opens_a_visible_window_and_waits_for_the_human(
    client: TestClient, waiting: str, connection: sqlite3.Connection
) -> None:
    """两个开关**一起**：窗口可见（人看得见）＋ 有人在场（人没输码时它不许走）。"""
    response = client.post(f"{PUBLISH_URL}/{waiting}/assist", json={"reason": "验证码已输"})

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "assist"
    assert body["publication"]["status"] == PUBLISHED
    assert body["publication"]["url"] == "https://example.invalid/v/1"
    assert body["waited_sec"] >= 0.0
    assert PUBLISHERS.headless == [False]
    assert PUBLISHERS.await_manual_verify == [True]
    assert PUBLISHERS.accounts == ["acc_main"]
    request = PUBLISHERS.publisher.publish_requests[0]
    # 这条路**没有**演练形态：它的全部意义就是把这一条发出去。
    assert request.dry_run is False
    assert request.title == "离谱跑酷地图"
    ops = AuditRepo(connection).list_recent(limit=10)
    assert [op.action for op in ops] == ["publish.assist"]
    assert ops[0].reason == "验证码已输"


def test_assist_that_did_not_work_keeps_the_row_waiting_for_a_human(client: TestClient, waiting: str) -> None:
    """人过完了还是没发出去 ⇒ 回到待人工（**不是**交回队列）：三个按钮留在原地。"""
    PUBLISHERS.reset(
        _FakePublisher(
            publish_result=PublishResult.failure(ErrorCode.PUBLISH_TIMEOUT, "点下发布后等不到结果页")
        )
    )

    body = client.post(f"{PUBLISH_URL}/{waiting}/assist").json()

    assert body["publication"]["status"] == "manual_required"
    assert "仍在待人工" in body["message"]
    assert PUBLISHERS.await_manual_verify == [True]


def test_assist_refuses_a_row_that_is_already_out_there(
    client: TestClient, waiting: str, connection: sqlite3.Connection
) -> None:
    """R14：发布不可逆。已经发出去过的记录再走一遍就是**第二条**作品。"""
    PublicationRepo(connection).mark_published(waiting, url="https://example.invalid/v/1")

    response = client.post(f"{PUBLISH_URL}/{waiting}/assist")

    assert response.status_code == 422
    assert "不能再走一遍" in response.json()["message"]
    assert PUBLISHERS.headless == []


def test_assist_on_an_unknown_publication_is_refused(client: TestClient) -> None:
    response = client.post(f"{PUBLISH_URL}/01NOPE00000000000000000000/assist")

    assert response.status_code == 422
    assert "发布记录不存在" in response.json()["message"]
    assert PUBLISHERS.headless == []
