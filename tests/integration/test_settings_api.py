"""设置面板的 REST 面（T6.1）—— LLM 通道与密钥的界面化配置。

四个端点，四件事
----------------
① ``GET  /api/v1/settings/llm`` —— 通道卡片 + 路由表 + 密钥状态（**只有掩码**）；
② ``PUT  /api/v1/settings/llm`` —— 保存 / 清除密钥（**先校验、后落盘**）；
③ ``PUT  /api/v1/settings/llm/profile`` —— 改通道参数（模型名 / base_url，写回 llm.yaml）；
④ ``POST /api/v1/settings/llm/probe`` —— 按需探测（只发只读 GET）。

为什么「明文一个字节都不许出现」要单独验
----------------------------------------
这是唯一一个**响应体里带密钥痕迹**的端点。一旦哪天有人为了「方便调试」加一个
``?include=key``，泄漏面就是「任何能打开面板的人」。所以这里对**整个响应文本**
做断言，而不是只看某个字段。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.core.secret_store import LLM_API_KEY_ENV, SECRETS_FILE_NAME
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.services import llm_settings_service
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

SETTINGS_URL = "/api/v1/settings/llm"
PROFILE_URL = "/api/v1/settings/llm/profile"
PROBE_URL = "/api/v1/settings/llm/probe"

GOOD_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"


class _Probe:
    """假资源探针 —— 不碰真 ``nvidia-smi`` 与真磁盘（与发布池那条同一手法）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-17T06:00:00.000Z",
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


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """假 httpx 客户端：**不发真请求**，只记下 URL 与请求头（断言密钥真的带上了）。"""

    def __init__(self, routes: dict[str, Any], **kwargs: object) -> None:
        del kwargs
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        self.calls.append((url, dict(headers or {})))
        result = self.routes.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return _FakeResponse(404)
        return result


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录 + 真配置抄一份（**但不抄 secrets.yaml** —— 密钥是这台机器的私事）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        if source.name == SECRETS_FILE_NAME:
            continue
        shutil.copyfile(source, value.config_dir / source.name)
    return value


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
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


def _cloud(client: TestClient) -> dict[str, Any]:
    """当前配置里 ``cloud`` 通道的样子（模型名 + base_url）。

    为什么不写死 ``https://api.openai.com/v1``：那是**仓库默认配置**里的值，而
    ``config/llm.yaml`` 是用户可改的（T6.1 的整个卖点就是"换服务商不用手改 YAML"）。
    把默认值钉在断言里，等于每换一次服务商就有三个用例变红，而它们想验的是
    "改完真的落盘了""探测真的带上了密钥"，与服务商是谁无关。
    """
    return _profile(client.get(SETTINGS_URL).json(), "cloud")


def _probe_routes(client: TestClient, **extra: Any) -> dict[str, Any]:
    """按**当前配置**铺探测路由：cloud 与 local 各一条。"""
    return {
        f"{_cloud(client)['base_url'].rstrip('/')}/models": _FakeResponse(200),
        "http://127.0.0.1:11434/api/tags": _FakeResponse(200, {"models": [{"name": "qwen2.5:7b"}]}),
        **extra,
    }


def _routes(routes: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeClient]:
    """把服务层里的 httpx 换成假件（探测**不碰真网络**）。"""
    holder: dict[str, _FakeClient] = {}

    def factory(**kwargs: object) -> _FakeClient:
        holder["client"] = _FakeClient(routes, **kwargs)
        return holder["client"]

    class _FakeHttpx:
        Timeout = staticmethod(lambda *args, **kwargs: None)
        HTTPError = httpx.HTTPError
        AsyncClient = staticmethod(factory)

    monkeypatch.setattr(llm_settings_service, "httpx", _FakeHttpx)
    return holder


# ── 读 ──────────────────────────────────────────────────────────────────


def test_first_screen_lists_profiles_and_an_empty_key(client: TestClient) -> None:
    """出厂状态：通道卡片齐全、密钥如实说「尚未配置」——而不是报错。"""
    body = client.get(SETTINGS_URL).json()
    assert body["key"]["configured"] is False
    assert body["key"]["source"] == "none"
    assert body["key"]["masked_key"] is None
    assert body["key"]["env_var"] == LLM_API_KEY_ENV
    assert body["key"]["env_overrides_file"] is False

    names = {row["name"] for row in body["profiles"]}
    assert {"cloud", "local"} <= names
    cloud = next(row for row in body["profiles"] if row["name"] == "cloud")
    assert cloud["needs_key"] is True
    assert cloud["usable"] is False
    assert "缺密钥" in cloud["detail"]
    local = next(row for row in body["profiles"] if row["name"] == "local")
    assert local["needs_key"] is False
    assert local["usable"] is True

    assert body["routing"]
    assert body["limits"]["min_len"] > 0


# ── 写 ──────────────────────────────────────────────────────────────────


def test_put_stores_the_key_and_never_echoes_it(client: TestClient, paths: StudioPaths) -> None:
    """保存：状态立刻变、掩码认得出来、**明文一个字节都不回**。"""
    response = client.put(SETTINGS_URL, json={"api_key": GOOD_KEY, "reason": "接上云端通道"})
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["cleared"] is False
    assert body["key"]["configured"] is True
    assert body["key"]["source"] == "file"
    assert body["key"]["source_label"] == "config/secrets.yaml"
    assert body["key"]["masked_key"] is not None
    assert GOOD_KEY not in response.text
    assert GOOD_KEY not in response.json()["key"]["masked_key"]

    cloud = next(row for row in body["profiles"] if row["name"] == "cloud")
    assert cloud["usable"] is True

    assert GOOD_KEY in (paths.config_dir / SECRETS_FILE_NAME).read_text(encoding="utf-8")


def test_put_rejects_a_bad_key_and_writes_nothing(client: TestClient, paths: StudioPaths) -> None:
    """**先校验、后落盘**：贴了半截 ⇒ 422，盘上连文件都不出现。"""
    response = client.put(SETTINGS_URL, json={"api_key": "short"})
    assert response.status_code == 422
    assert "太短" in response.text
    assert not (paths.config_dir / SECRETS_FILE_NAME).exists()


@pytest.mark.parametrize(
    "payload",
    [{}, {"clear": False}, {"api_key": GOOD_KEY, "clear": True}],
)
def test_put_demands_exactly_one_intent(client: TestClient, payload: dict[str, Any]) -> None:
    """「没说要干嘛」与「同时说两件事」都是 422 —— 清空必须是显式动作。"""
    assert client.put(SETTINGS_URL, json=payload).status_code == 422


def test_validation_error_never_echoes_the_submitted_key(client: TestClient) -> None:
    """请求体校验失败时**不能把原文抄回响应** —— 那里面就是密钥本身。

    这条盯的是 ``app/errors.py`` 的 ``RequestValidationError`` 分支：它一度把
    pydantic 的 ``errors()`` 原样塞进 ``context``，而那份结构里既有用户提交的
    ``input``（就是密钥），也可能有**异常对象**（于是 ``json.dumps`` 直接炸成 500）。
    """
    response = client.put(SETTINGS_URL, json={"api_key": GOOD_KEY, "clear": True})
    assert response.status_code == 422
    assert GOOD_KEY not in response.text


def test_oversized_key_is_rejected_without_echo(client: TestClient) -> None:
    """超长的值由 pydantic 在进业务之前拦下（``max_length``），同样不许回显。"""
    huge = "sk-" + "a" * 5000
    response = client.put(SETTINGS_URL, json={"api_key": huge})
    assert response.status_code == 422
    assert huge not in response.text


def test_put_clear_removes_it(client: TestClient, state: AppState) -> None:
    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY})
    body = client.put(SETTINGS_URL, json={"clear": True, "reason": "换成环境变量"}).json()
    assert body["cleared"] is True
    assert body["changed"] is True
    assert body["key"]["configured"] is False
    assert state.secrets.api_key() is None


def test_put_audits_with_masks_only(client: TestClient, connection: sqlite3.Connection) -> None:
    """留痕必须有，且 ``before/after`` 里**只能有掩码**（审计表不是密钥表）。"""
    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY, "reason": "接上云端通道"})
    row = AuditRepo(connection).list_recent(limit=1)[0]
    assert row.action == "settings.llm_key_set"
    assert row.target_type == "config"
    assert row.target_id == "secrets.llm.api_key"
    assert row.reason == "接上云端通道"
    assert row.before == {"masked_key": None}
    assert row.after is not None
    assert GOOD_KEY not in str(row.after)


def test_put_twice_does_not_write_again(client: TestClient) -> None:
    """同一把 Key 连点两次 ⇒ 第二次 ``changed=false``（不假装做了一次操作）。"""
    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY})
    again = client.put(SETTINGS_URL, json={"api_key": GOOD_KEY}).json()
    assert again["changed"] is False


def test_env_variable_overrides_the_file_and_says_so(
    client: TestClient, state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量占着位置时，面板存的那份**不生效** —— 这件事必须写在 ``notes`` 里。

    不写的话，用户填完发现没生效，会以为这个面板坏了（而其实只需要去改环境变量）。
    """
    monkeypatch.setenv(LLM_API_KEY_ENV, "sk-from-environment-0001")
    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY})

    body = client.get(SETTINGS_URL).json()
    assert body["key"]["source"] == "env"
    assert body["key"]["env_overrides_file"] is True
    assert any(LLM_API_KEY_ENV in note for note in body["notes"])
    assert state.secrets.api_key() == "sk-from-environment-0001"


# ── 探测 ────────────────────────────────────────────────────────────────


# ── 写 · 通道参数（模型名 / base_url）──────────────────────────────────


def _profile(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in body["profiles"] if item["name"] == name)


def test_put_profile_changes_the_model_name(client: TestClient, paths: StudioPaths) -> None:
    """改模型名 ⇒ 盘上真的变了、回包里就是新的、**别的通道一个字节都没动**。"""
    before = client.get(SETTINGS_URL).json()
    old_model = _profile(before, "cloud")["model"]
    local_model = _profile(before, "local")["model"]

    response = client.put(PROFILE_URL, json={"profile": "cloud", "model": "brand-new-model"})

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["profile"] == "cloud"
    assert _profile(body, "cloud")["model"] == "brand-new-model"
    assert _profile(body, "local")["model"] == local_model
    text = (paths.config_dir / "llm.yaml").read_text(encoding="utf-8")
    assert "model: brand-new-model" in text
    assert f"model: {old_model}" not in text


def test_put_profile_can_rewrite_base_url(client: TestClient, paths: StudioPaths) -> None:
    body = client.put(PROFILE_URL, json={"profile": "local", "base_url": "http://127.0.0.1:9999/v1"}).json()

    assert body["changed"] is True
    assert _profile(body, "local")["base_url"] == "http://127.0.0.1:9999/v1"


def test_put_profile_audits_before_and_after(client: TestClient, connection: sqlite3.Connection) -> None:
    """留痕要有，且 before/after 是**模型名**这一对（不是掩码那一路）。"""
    before = _cloud(client)

    client.put(PROFILE_URL, json={"profile": "cloud", "model": "audited-model"})

    row = AuditRepo(connection).list_recent(limit=1)[0]
    assert row.action == "settings.llm_profile_updated"
    assert row.target_id == "llm.profiles.cloud"
    # base_url 没被点名 ⇒ 留痕里它原样回抄当前配置（换服务商的人不该因此看到红字）
    assert row.before == {"model": before["model"], "base_url": before["base_url"]}
    assert row.after is not None
    assert row.after["model"] == "audited-model"


def test_put_profile_twice_does_not_write_again(client: TestClient, connection: sqlite3.Connection) -> None:
    """同一件事做两次不该在审计里出现两行（与密钥那条同一条纪律）。"""
    client.put(PROFILE_URL, json={"profile": "cloud", "model": "same-model"})
    second = client.put(PROFILE_URL, json={"profile": "cloud", "model": "same-model"})

    assert second.status_code == 200
    assert second.json()["changed"] is False
    assert AuditRepo(connection).list_recent(limit=5)[0].action == "settings.llm_profile_updated"


def test_put_profile_rejects_an_unknown_profile(client: TestClient, paths: StudioPaths) -> None:
    before = (paths.config_dir / "llm.yaml").read_text(encoding="utf-8")

    response = client.put(PROFILE_URL, json={"profile": "nope", "model": "x"})

    assert response.status_code == 400
    assert response.json()["code"] == "CONFIG_INVALID"
    assert (paths.config_dir / "llm.yaml").read_text(encoding="utf-8") == before


def test_put_profile_demands_a_field(client: TestClient, paths: StudioPaths) -> None:
    """两个都不给 ⇒ 422（不知道你想改什么），且一个字节都不写。"""
    before = (paths.config_dir / "llm.yaml").read_text(encoding="utf-8")

    response = client.put(PROFILE_URL, json={"profile": "cloud"})

    assert response.status_code == 422
    assert (paths.config_dir / "llm.yaml").read_text(encoding="utf-8") == before


def test_put_profile_rejects_a_blank_model(client: TestClient, paths: StudioPaths) -> None:
    before = (paths.config_dir / "llm.yaml").read_text(encoding="utf-8")

    response = client.put(PROFILE_URL, json={"profile": "cloud", "model": "   "})

    assert response.status_code == 422
    assert (paths.config_dir / "llm.yaml").read_text(encoding="utf-8") == before


def test_put_profile_rejects_a_non_http_base_url(client: TestClient) -> None:
    response = client.put(PROFILE_URL, json={"profile": "local", "base_url": "ftp://x/v1"})

    assert response.status_code == 422


def test_probe_reports_missing_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _routes({}, monkeypatch)
    body = client.post(PROBE_URL).json()
    rows = {row["profile"]: row for row in body["rows"]}
    assert rows["cloud"]["status"] == "no_key"
    assert body["ok_count"] == 0


def test_probe_sends_the_stored_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """配好之后探测要**真的带上密钥**（带错地方就永远是 401，而面板会说"已连接"）。"""
    holder = _routes(_probe_routes(client), monkeypatch)
    cloud_base = _cloud(client)["base_url"].rstrip("/")

    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY})
    body = client.post(PROBE_URL).json()
    rows = {row["profile"]: row for row in body["rows"]}
    assert rows["cloud"]["status"] == "ok"
    assert rows["local"]["status"] == "ok"
    assert "1 个" in rows["local"]["detail"]
    assert body["ok_count"] == 2

    fake = holder["client"]
    cloud_call = next(call for call in fake.calls if call[0].startswith(cloud_base))
    assert cloud_call[1]["Authorization"] == f"Bearer {GOOD_KEY}"


def test_probe_reports_unreachable_instead_of_raising(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连不上 ⇒ 如实报 ``unreachable``（面板要能画出红字，而不是整屏 500）。"""
    cloud_base = _cloud(client)["base_url"].rstrip("/")
    _routes({f"{cloud_base}/models": httpx.ConnectError("boom")}, monkeypatch)
    client.put(SETTINGS_URL, json={"api_key": GOOD_KEY})
    rows = {row["profile"]: row for row in client.post(PROBE_URL).json()["rows"]}
    assert rows["cloud"]["status"] == "unreachable"
