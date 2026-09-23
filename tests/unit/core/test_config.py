"""配置系统（T1.2 · §01.2 / §README.6）。

规格要求的五类用例全部覆盖：
缺字段 / 非法值 / 未知键 / 越界路径 / 未设密码但开启局域网。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from studio.core.config import (
    CONFIG_FILE_NAMES,
    SECRETS_FILE_NAME,
    AccountConfig,
    ConfigBundle,
    LlmConfigHotReload,
    effective_paths,
    llm_config_provider,
    load_config,
    load_llm_config,
    load_publish_config,
    parse_publish_account,
    redact,
    set_llm_profile_model,
    write_publish_accounts,
)
from studio.core.errors import ConfigError, ErrorCode
from studio.core.paths import StudioPaths
from tests.support import restore_factory_publish_config

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG_DIR = REPO_ROOT / "config"

Mutator = Callable[[dict[str, Any]], None]


@pytest.fixture
def config_paths(tmp_path: Path) -> StudioPaths:
    """把真实 config/*.yaml 复制到临时仓库，供"改一处再加载"的用例使用。"""
    home = tmp_path / "studio"
    (home / "config").mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILE_NAMES:
        shutil.copy2(REAL_CONFIG_DIR / f"{name}.yaml", home / "config" / f"{name}.yaml")
    # 模板文件也要在（报错信息会指向 config/<name>.example.yaml）
    for example in REAL_CONFIG_DIR.glob("*.example.yaml"):
        shutil.copy2(example, home / "config" / example.name)
    # publish.yaml 是**面板会就地改写**的那一份（T6.4）：账号段与总开关都是操作员的
    # 运行期状态。这一层验的是**出厂**那份（"出厂有一个 douyin 号" / "出厂不发出去"），
    # 所以把它摆回出厂 —— 否则"在面板上删过一个号、打开过开关"会让这一整片红在与
    # 它们无关的地方（见 tests/support.py）。
    restore_factory_publish_config(home / "config" / "publish.yaml")
    return StudioPaths(home=home, data_dir=home / "data")


def _edit(paths: StudioPaths, name: str, mutate: Mutator) -> Path:
    """就地改写某份 YAML（保留其余键）。"""
    path = paths.config_dir / f"{name}.yaml"
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _load(paths: StudioPaths, env: dict[str, str] | None = None) -> Any:
    return load_config(paths, env=env or {})


def _expect(paths: StudioPaths, code: ErrorCode, env: dict[str, str] | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        _load(paths, env)
    assert excinfo.value.code is code, excinfo.value.to_dict()
    return excinfo.value


# ══════════════════════════════════════════════════════════════════════
# 基线：9 份文件全部可加载
# ══════════════════════════════════════════════════════════════════════


def test_loads_all_eight_files(config_paths: StudioPaths) -> None:
    loaded = _load(config_paths)
    assert tuple(source.name for source in loaded.sources) == CONFIG_FILE_NAMES
    assert len(loaded.sources) == 9
    assert all(source.sha256 for source in loaded.sources)


def test_bundle_shape(config_paths: StudioPaths) -> None:
    bundle = _load(config_paths).bundle
    assert isinstance(bundle, ConfigBundle)
    assert set(bundle.pools.pools) == {"draft", "voice", "render", "publish"}
    assert bundle.pools.pools["draft"].concurrency == 2
    assert bundle.pools.pools["voice"].concurrency == 1
    assert bundle.randomization.mode == "standard"
    assert bundle.publish.enabled is False
    assert bundle.publish.require_confirm is True


def test_source_sha256_tracks_content(config_paths: StudioPaths) -> None:
    before = _load(config_paths).source_of("app").sha256
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("port", 9999))
    assert _load(config_paths).source_of("app").sha256 != before


# ══════════════════════════════════════════════════════════════════════
# ① 缺字段
# ══════════════════════════════════════════════════════════════════════


def test_missing_config_file(config_paths: StudioPaths) -> None:
    (config_paths.config_dir / "app.yaml").unlink()
    error = _expect(config_paths, ErrorCode.CONFIG_MISSING)
    assert "app.yaml" in str(error)


@pytest.mark.parametrize("field", ["role_desc", "tone", "audience", "catchphrases", "forbidden"])
def test_persona_missing_required_field(config_paths: StudioPaths, field: str) -> None:
    _edit(config_paths, "persona", lambda data: data.pop(field))
    error = _expect(config_paths, ErrorCode.CONFIG_PERSONA_INCOMPLETE)
    assert error.context["file"].endswith("persona.yaml")
    assert any(field in item["field"] for item in error.context["errors"])
    assert "persona.example.yaml" in (error.remediation or "")


def test_pools_missing_pool(config_paths: StudioPaths) -> None:
    _edit(config_paths, "pools", lambda data: data["pools"].pop("publish"))
    error = _expect(config_paths, ErrorCode.CONFIG_INVALID)
    assert any("publish" in item["error"] for item in error.context["errors"])


def test_outputs_missing_watermark(config_paths: StudioPaths) -> None:
    _edit(config_paths, "outputs", lambda data: data.pop("watermark"))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


# ══════════════════════════════════════════════════════════════════════
# ② 非法值
# ══════════════════════════════════════════════════════════════════════


def test_persona_chars_range_inverted(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["target_chars_min"] = 900
        data["target_chars_max"] = 600

    _edit(config_paths, "persona", mutate)
    error = _expect(config_paths, ErrorCode.CONFIG_PERSONA_INCOMPLETE)
    assert any("target_chars_min" in item["error"] for item in error.context["errors"])


def test_persona_too_few_catchphrases(config_paths: StudioPaths) -> None:
    _edit(config_paths, "persona", lambda data: data.__setitem__("catchphrases", ["只有一个"]))
    _expect(config_paths, ErrorCode.CONFIG_PERSONA_INCOMPLETE)


def test_web_port_out_of_range(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("port", 70000))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_schema_version_unknown(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data.__setitem__("schema_version", "9.9"))
    error = _expect(config_paths, ErrorCode.CONFIG_INVALID)
    assert any("schema_version" in item["field"] for item in error.context["errors"])


def test_pools_backoff_ordering(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["pools"]["draft"]["backoff_base_ms"] = 90000
        data["pools"]["draft"]["backoff_max_ms"] = 30000

    _edit(config_paths, "pools", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_encoding_profile_requires_crf_or_cq(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["profiles"]["douyin_1080x1920_30fps_v1"]["crf"]

    _edit(config_paths, "outputs", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_encoding_profile_canvas_must_be_even(config_paths: StudioPaths) -> None:
    _edit(
        config_paths,
        "outputs",
        lambda data: data["profiles"]["fallback_720x1280_v1"].__setitem__("width", 721),
    )
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_watermark_margin_must_be_even(config_paths: StudioPaths) -> None:
    _edit(config_paths, "outputs", lambda data: data["watermark"].__setitem__("margin_x", 47))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_watermark_width_ratio_capped(config_paths: StudioPaths) -> None:
    _edit(config_paths, "outputs", lambda data: data["watermark"].__setitem__("width_ratio", 0.5))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_default_profile_must_exist(config_paths: StudioPaths) -> None:
    _edit(config_paths, "outputs", lambda data: data.__setitem__("default_profile", "nope_v1"))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_llm_routing_profile_must_exist(config_paths: StudioPaths) -> None:
    _edit(config_paths, "llm", lambda data: data["routing"]["planner"].__setitem__("profile", "ghost"))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_standard_mode_rejects_aggressive_dimensions(config_paths: StudioPaths) -> None:
    _edit(
        config_paths,
        "randomization",
        lambda data: data["dimensions"]["geometry"].__setitem__("enabled", True),
    )
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_aggressive_mode_allows_geometry(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["mode"] = "aggressive"
        data["dimensions"]["geometry"]["enabled"] = True

    _edit(config_paths, "randomization", mutate)
    bundle = _load(config_paths).bundle
    assert "geometry" in bundle.randomization.dimensions.enabled_names()


def test_publish_lufs_range_inverted(config_paths: StudioPaths) -> None:
    _edit(config_paths, "publish", lambda data: data["precheck"].__setitem__("lufs_min", -10.0))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_tag_syntax_needs_placeholder(config_paths: StudioPaths) -> None:
    _edit(
        config_paths, "publish", lambda data: data["platforms"]["douyin"].__setitem__("tag_syntax", "plain")
    )
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_duplicate_account_id_rejected(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["accounts"].append(dict(data["accounts"][0]) | {"profile_dir": "data/browser_profile/other"})

    _edit(config_paths, "publish", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_account_platform_must_be_declared(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["platforms"]["weibo"]
        data["accounts"][0]["platform"] = "weibo"

    _edit(config_paths, "publish", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_account_profile_dir_must_be_unique(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["accounts"].append(dict(data["accounts"][0]) | {"account_id": "acc_second"})

    _edit(config_paths, "publish", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


# ══════════════════════════════════════════════════════════════════════
# ③ 未知键（extra="forbid"：防拼写错误静默失效）
# ══════════════════════════════════════════════════════════════════════


def test_unknown_top_level_key(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data.__setitem__("webb", {"port": 1}))
    error = _expect(config_paths, ErrorCode.CONFIG_INVALID)
    assert any("webb" in item["error"] or "webb" in item["field"] for item in error.context["errors"])


def test_unknown_nested_key(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("prot", 9000))
    error = _expect(config_paths, ErrorCode.CONFIG_INVALID)
    assert any("prot" in item["error"] or "prot" in item["field"] for item in error.context["errors"])


def test_unknown_key_in_pool(config_paths: StudioPaths) -> None:
    _edit(config_paths, "pools", lambda data: data["pools"]["draft"].__setitem__("concurrencyy", 8))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_unknown_key_in_platform(config_paths: StudioPaths) -> None:
    _edit(config_paths, "publish", lambda data: data["platforms"]["douyin"].__setitem__("title_maxx", 10))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


# ══════════════════════════════════════════════════════════════════════
# ④ 越界路径（R1：配置一改就写爆系统盘）
# ══════════════════════════════════════════════════════════════════════


def test_path_escaping_repo_root(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["paths"].__setitem__("work_dir", "../../escape"))
    error = _expect(config_paths, ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS)
    assert error.context["problems"][0]["field"] == "app.paths.work_dir"


def test_path_on_system_drive(config_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    _edit(config_paths, "app", lambda data: data["paths"].__setitem__("cache_dir", r"C:\cache\studio"))
    error = _expect(config_paths, ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS)
    assert "系统盘" in error.context["problems"][0]["reason"]


def test_path_inside_repo_is_allowed(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["paths"].__setitem__("work_dir", "var/work"))
    loaded = _load(config_paths)
    assert loaded.bundle.app.paths.work_dir is not None


def test_watermark_must_live_under_templates(config_paths: StudioPaths) -> None:
    _edit(config_paths, "outputs", lambda data: data["watermark"].__setitem__("path", "data/logo.png"))
    error = _expect(config_paths, ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS)
    assert "templates" in str(error)


def test_watermark_path_need_not_exist_yet(config_paths: StudioPaths) -> None:
    """E2 素材缺失不在配置期拦 —— 渲染期由 T3.2 以 RENDER_WATERMARK_MISSING 拒绝。"""
    assert not (config_paths.home / "templates").exists()
    loaded = _load(config_paths)
    assert loaded.bundle.outputs.watermark.path.name == "watermark.png"


# ══════════════════════════════════════════════════════════════════════
# ⑤ 未设密码但开启局域网（§01.2.5 安全底线）
# ══════════════════════════════════════════════════════════════════════


def test_lan_without_password_rejected(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("allow_lan", True))
    error = _expect(config_paths, ErrorCode.AUTH_LAN_WITHOUT_PASSWORD)
    assert error.context["allow_lan"] is True
    assert "STUDIO_WEBUI_PASSWORD" in (error.remediation or "")


def test_lan_with_env_password_ok(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("allow_lan", True))
    loaded = _load(config_paths, {"STUDIO_WEBUI_PASSWORD": "s3cret"})
    assert loaded.bundle.secrets.has_webui_password


def test_lan_with_secrets_file_ok(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("allow_lan", True))
    (config_paths.config_dir / f"{SECRETS_FILE_NAME}.yaml").write_text(
        'schema_version: "1.0"\nwebui:\n  password: from-file\n', encoding="utf-8"
    )
    loaded = _load(config_paths)
    assert loaded.bundle.secrets.has_webui_password


def test_env_password_beats_secrets_file(config_paths: StudioPaths) -> None:
    (config_paths.config_dir / f"{SECRETS_FILE_NAME}.yaml").write_text(
        'schema_version: "1.0"\nwebui:\n  password: from-file\n', encoding="utf-8"
    )
    loaded = _load(config_paths, {"STUDIO_WEBUI_PASSWORD": "from-env"})
    assert loaded.bundle.secrets.webui.password == "from-env"


def test_loopback_without_password_ok(config_paths: StudioPaths) -> None:
    assert _load(config_paths).bundle.app.web.allow_lan is False


# ══════════════════════════════════════════════════════════════════════
# 密钥铁律：llm.yaml 只存环境变量名
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "leaked",
    ["sk-abcdefghijklmnop", "sk_live_abcdefghijkl", "Bearer abcdefghijklmn", "eyJhbGciOiJIUzI1NiJ9"],
)
def test_secret_written_into_llm_yaml_rejected(config_paths: StudioPaths, leaked: str) -> None:
    _edit(config_paths, "llm", lambda data: data["profiles"]["cloud"].__setitem__("api_key_env", leaked))
    error = _expect(config_paths, ErrorCode.CONFIG_INVALID)
    assert any("环境变量名" in item["error"] for item in error.context["errors"])


def test_api_key_env_must_be_identifier(config_paths: StudioPaths) -> None:
    _edit(config_paths, "llm", lambda data: data["profiles"]["cloud"].__setitem__("api_key_env", "1BAD-NAME"))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


def test_api_key_env_null_is_allowed_for_local(config_paths: StudioPaths) -> None:
    assert _load(config_paths).bundle.llm.profiles["local"].api_key_env is None


# ══════════════════════════════════════════════════════════════════════
# 环境变量覆盖层（env > yaml）
# ══════════════════════════════════════════════════════════════════════


def test_env_override_scalar_beats_yaml(config_paths: StudioPaths) -> None:
    loaded = _load(config_paths, {"STUDIO_CFG__APP__WEB__PORT": "9000"})
    assert loaded.bundle.app.web.port == 9000
    assert loaded.source_of("app").env_overrides == ("STUDIO_CFG__APP__WEB__PORT",)


def test_env_override_parses_json_types(config_paths: StudioPaths) -> None:
    env = {
        "STUDIO_CFG__PUBLISH__METRICS_SCHEDULE_HOURS": "[2, 12]",
        "STUDIO_CFG__POOLS__POOLS__DRAFT__CONCURRENCY": "4",
        "STUDIO_CFG__APP__WEB__HOST": "0.0.0.0",
    }
    bundle = _load(config_paths, env).bundle
    assert bundle.publish.metrics_schedule_hours == [2, 12]
    assert bundle.pools.pools["draft"].concurrency == 4
    assert bundle.app.web.host == "0.0.0.0"


def test_env_override_bool_requires_lan_password(config_paths: StudioPaths) -> None:
    env = {"STUDIO_CFG__APP__WEB__ALLOW_LAN": "true"}
    _expect(config_paths, ErrorCode.AUTH_LAN_WITHOUT_PASSWORD, env)
    ok = _load(config_paths, env | {"STUDIO_WEBUI_PASSWORD": "pw"})
    assert ok.bundle.app.web.allow_lan is True


def test_env_override_list_replacement_not_merge(config_paths: StudioPaths) -> None:
    env = {"STUDIO_CFG__PERSONA__CATCHPHRASES": '["一","二"]'}
    assert _load(config_paths, env).bundle.persona.catchphrases == ["一", "二"]


def test_env_override_unknown_section_rejected(config_paths: StudioPaths) -> None:
    error = _expect(config_paths, ErrorCode.CONFIG_UNKNOWN_KEY, {"STUDIO_CFG__APPS__WEB__PORT": "9000"})
    assert error.context["section"] == "APPS"


def test_env_override_unknown_key_rejected(config_paths: StudioPaths) -> None:
    _expect(config_paths, ErrorCode.CONFIG_INVALID, {"STUDIO_CFG__APP__WEB__PROT": "9000"})


def test_env_override_malformed_rejected(config_paths: StudioPaths) -> None:
    _expect(config_paths, ErrorCode.CONFIG_UNKNOWN_KEY, {"STUDIO_CFG__APP__": "1"})


def test_env_override_can_open_lan_with_password(config_paths: StudioPaths) -> None:
    env = {"STUDIO_CFG__APP__WEB__ALLOW_LAN": "true", "STUDIO_WEBUI_PASSWORD": "pw"}
    assert _load(config_paths, env).bundle.app.web.allow_lan is True


def test_unrelated_env_vars_ignored(config_paths: StudioPaths) -> None:
    loaded = _load(config_paths, {"STUDIO_HOME": r"D:\x", "PATH": "/usr/bin"})
    assert loaded.source_of("app").env_overrides == ()


# ══════════════════════════════════════════════════════════════════════
# dump 一致性 + 脱敏
# ══════════════════════════════════════════════════════════════════════


def _assert_subset(expected: Any, actual: Any, trail: str) -> None:
    """dump 必须**原样保留** YAML 里的每个键值（允许 dump 多出显式 null 字段）。"""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{trail} 类型不符：{type(actual).__name__}"
        for key, value in expected.items():
            assert key in actual, f"{trail}.{key} 在 dump 中缺失"
            _assert_subset(value, actual[key], f"{trail}.{key}")
    else:
        assert expected == actual, f"{trail} 不一致：yaml={expected!r} dump={actual!r}"


def test_dump_is_consistent_with_every_yaml_key(config_paths: StudioPaths) -> None:
    """★ T1.2 验收：`studio config dump --json` 与 config/*.yaml 一致。"""
    payload = _load(config_paths).dump()
    for name in CONFIG_FILE_NAMES:
        raw = yaml.safe_load((config_paths.config_dir / f"{name}.yaml").read_text(encoding="utf-8"))
        _assert_subset(raw, payload[name], name)


def test_dump_uses_forward_slashes_for_paths(config_paths: StudioPaths) -> None:
    payload = _load(config_paths).dump()
    assert payload["app"]["handoff"]["output_dir"] == "data/handoff"
    assert payload["logging"]["file"]["path"] == "data/logs/studio.log"
    assert payload["publish"]["accounts"][0]["profile_dir"] == "data/browser_profile/acc_main"


def test_dump_has_no_nested_schema_version(config_paths: StudioPaths) -> None:
    """schema_version 只属于文件级，不得下沉到子节。"""
    payload = _load(config_paths).dump()
    assert payload["app"]["schema_version"] == "1.0"
    assert "schema_version" not in payload["app"]["web"]
    assert "schema_version" not in payload["pools"]["pools"]["draft"]


def test_dump_meta_records_sources(config_paths: StudioPaths) -> None:
    payload = _load(config_paths).dump()
    assert payload["_meta"]["studio_home"] == str(config_paths.home)
    assert len(payload["_meta"]["sources"]) == 9
    assert {item["name"] for item in payload["_meta"]["sources"]} == set(CONFIG_FILE_NAMES)


def test_dump_reflects_env_override(config_paths: StudioPaths) -> None:
    payload = _load(config_paths, {"STUDIO_CFG__APP__WEB__PORT": "9001"}).dump()
    assert payload["app"]["web"]["port"] == 9001
    assert payload["_meta"]["sources"][2]["env_overrides"] == ["STUDIO_CFG__APP__WEB__PORT"]


def test_dump_redacts_password_but_keeps_key_name(config_paths: StudioPaths) -> None:
    (config_paths.config_dir / f"{SECRETS_FILE_NAME}.yaml").write_text(
        'schema_version: "1.0"\nwebui:\n  password: super-secret\n', encoding="utf-8"
    )
    payload = _load(config_paths).dump()
    assert payload["secrets"]["webui"]["password"] == "***"
    # api_key_env 存的是环境变量名，不是密钥本体 ⇒ 不脱敏
    assert payload["llm"]["profiles"]["cloud"]["api_key_env"] == "STUDIO_LLM_API_KEY"


def test_dump_can_show_secrets_when_asked(config_paths: StudioPaths) -> None:
    (config_paths.config_dir / f"{SECRETS_FILE_NAME}.yaml").write_text(
        'schema_version: "1.0"\nwebui:\n  password: super-secret\n', encoding="utf-8"
    )
    payload = _load(config_paths).dump(redact_secrets=False)
    assert payload["secrets"]["webui"]["password"] == "super-secret"


def test_redact_handles_nested_structures() -> None:
    payload = {
        "webui": {"password": "x", "user": "admin"},
        "llm": {"profiles": [{"api_key": "sk-1"}, {"api_key_env": "STUDIO_LLM_API_KEY"}]},
        "empty_token": "",
    }
    cleaned = redact(payload)
    assert cleaned["webui"]["password"] == "***"
    assert cleaned["webui"]["user"] == "admin"
    assert cleaned["llm"]["profiles"][0]["api_key"] == "***"
    assert cleaned["llm"]["profiles"][1]["api_key_env"] == "STUDIO_LLM_API_KEY"
    assert cleaned["empty_token"] is None


# ══════════════════════════════════════════════════════════════════════
# 覆盖文件、派生路径、告警
# ══════════════════════════════════════════════════════════════════════


def test_local_override_deep_merges(config_paths: StudioPaths) -> None:
    (config_paths.config_dir / "app.local.yaml").write_text("web:\n  port: 8899\n", encoding="utf-8")
    loaded = _load(config_paths)
    assert loaded.bundle.app.web.port == 8899
    assert loaded.bundle.app.web.host == "127.0.0.1"
    assert loaded.source_of("app").local_override is not None


def test_effective_paths_applies_overrides(config_paths: StudioPaths) -> None:
    _edit(config_paths, "app", lambda data: data["paths"].__setitem__("work_dir", "var/work"))
    loaded = _load(config_paths)
    derived = effective_paths(loaded.bundle, loaded.paths)
    assert derived.work_dir == (config_paths.home / "var" / "work").resolve()
    assert derived.output_dir == loaded.paths.output_dir


def test_warnings_flag_missing_llm_key(config_paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_LLM_API_KEY", raising=False)
    notes = _load(config_paths).warnings
    assert any("STUDIO_LLM_API_KEY" in note for note in notes)


# ══════════════════════════════════════════════════════════════════════
# 通道参数写回（T6.1：设置页可改模型名 / base_url）
# ══════════════════════════════════════════════════════════════════════


def _llm_yaml(paths: StudioPaths) -> dict[str, Any]:
    data = yaml.safe_load((paths.config_dir / "llm.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _llm_value(paths: StudioPaths, profile: str, key: str) -> str:
    return str(_llm_yaml(paths)["profiles"][profile][key])


def test_load_llm_config_reads_only_that_file(config_paths: StudioPaths) -> None:
    """``app.yaml`` 写坏了，LLM 通道照样读得出来 —— 两件事之间没有关系。"""
    _edit(config_paths, "app", lambda data: data["web"].__setitem__("port", 999999))
    _expect(config_paths, ErrorCode.CONFIG_INVALID)

    assert load_llm_config(config_paths).profiles["cloud"].model == _llm_value(config_paths, "cloud", "model")


def test_set_llm_profile_model_rewrites_exactly_one_line(config_paths: StudioPaths) -> None:
    """逐字节：只有那一行变了 —— 注释、routing、budget 一个字节都没动。"""
    path = config_paths.config_dir / "llm.yaml"
    before = path.read_text(encoding="utf-8")
    old_model = _llm_value(config_paths, "cloud", "model")
    local_model = _llm_value(config_paths, "local", "model")

    set_llm_profile_model(config_paths, profile="cloud", model="brand-new-model")

    after = path.read_text(encoding="utf-8")
    assert load_llm_config(config_paths).profiles["cloud"].model == "brand-new-model"
    assert after == before.replace(f"model: {old_model}", "model: brand-new-model")
    assert load_llm_config(config_paths).profiles["local"].model == local_model


def test_set_llm_profile_model_keeps_a_trailing_comment(config_paths: StudioPaths) -> None:
    """行尾注释原样保留（那份文件的注释就是"为什么这么配"的唯一记录）。"""
    path = config_paths.config_dir / "llm.yaml"
    old_model = _llm_value(config_paths, "cloud", "model")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(f"model: {old_model}", f"model: {old_model}  # 换档位时改这一行"),
        encoding="utf-8",
    )

    set_llm_profile_model(config_paths, profile="cloud", model="next-model")

    assert "model: next-model  # 换档位时改这一行" in path.read_text(encoding="utf-8")


def test_set_llm_profile_model_can_rewrite_base_url(config_paths: StudioPaths) -> None:
    """换服务商要能改 base_url（模型名与地址是同一件事的两半）。"""
    set_llm_profile_model(config_paths, profile="local", base_url="http://127.0.0.1:9999/v1")

    assert load_llm_config(config_paths).profiles["local"].base_url == "http://127.0.0.1:9999/v1"
    assert load_llm_config(config_paths).profiles["local"].model == _llm_value(config_paths, "local", "model")


def test_set_llm_profile_model_rejects_an_unknown_profile(config_paths: StudioPaths) -> None:
    with pytest.raises(ConfigError) as excinfo:
        set_llm_profile_model(config_paths, profile="nope", model="x")

    assert excinfo.value.code is ErrorCode.CONFIG_MISSING
    assert "nope" in str(excinfo.value)


def test_set_llm_profile_model_needs_something_to_write(config_paths: StudioPaths) -> None:
    with pytest.raises(ValueError):
        set_llm_profile_model(config_paths, profile="cloud")


def test_set_llm_profile_model_refuses_a_bad_value_without_touching_the_file(
    config_paths: StudioPaths,
) -> None:
    """空模型名 ⇒ 写盘**之前**就炸：一份读不出来的配置不该留在盘上。"""
    path = config_paths.config_dir / "llm.yaml"
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        set_llm_profile_model(config_paths, profile="cloud", model="")

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert path.read_text(encoding="utf-8") == before


def test_llm_config_hot_reload_picks_up_a_rewrite(config_paths: StudioPaths) -> None:
    """常驻进程（写稿 worker）靠它发现"面板上换了模型名"。"""
    provider = llm_config_provider(config_paths)
    assert provider().profiles["cloud"].model == _llm_value(config_paths, "cloud", "model")

    set_llm_profile_model(config_paths, profile="cloud", model="hot-reloaded-model")

    assert provider().profiles["cloud"].model == "hot-reloaded-model"


def test_llm_config_hot_reload_keeps_the_last_good_snapshot(config_paths: StudioPaths) -> None:
    """改到一半（YAML 语法错）⇒ 沿用上一份可用值，**不**把在跑的任务打断。"""
    provider = llm_config_provider(config_paths)
    good = provider()

    (config_paths.config_dir / "llm.yaml").write_text("profiles: [", encoding="utf-8")

    assert provider().profiles["cloud"].model == good.profiles["cloud"].model


def test_llm_config_hot_reload_raises_when_nothing_was_ever_read(
    config_paths: StudioPaths,
) -> None:
    """从没读到过 ⇒ 必须炸：不能装作"有一份配置"。"""
    (config_paths.config_dir / "llm.yaml").write_text("profiles: [", encoding="utf-8")

    with pytest.raises(ConfigError):
        llm_config_provider(config_paths)()


def test_llm_config_hot_reload_caches_until_the_file_moves(config_paths: StudioPaths) -> None:
    """文件没动 ⇒ 不重读（一次 ``stat``，不是一次全量校验）。"""
    calls: list[int] = []
    original = load_llm_config

    def counting(paths: StudioPaths, *, env: Any = None) -> Any:
        calls.append(1)
        return original(paths, env=env)

    hot = LlmConfigHotReload(config_paths, loader=lambda: counting(config_paths))

    first = hot.current()
    second = hot.current()

    assert first is second
    assert len(calls) == 1


def test_publish_guard_blocks_confirm_free_without_accounts(config_paths: StudioPaths) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["enabled"] = True
        data["require_confirm"] = False
        data["accounts"] = []

    _edit(config_paths, "publish", mutate)
    _expect(config_paths, ErrorCode.CONFIG_INVALID)


# ══════════════════════════════════════════════════════════════════════
# 面板改账号：按行改写 config/publish.yaml（T6.4）
# ══════════════════════════════════════════════════════════════════════
#
# 这一组验的不是"写进去了"，而是**只写了该写的地方**：
# 那份文件里 ``platforms`` 段、``precheck`` 段、以及每行行尾的注释都是"为什么这么配"
# 的唯一记录，被面板顺手抹掉一次，就再也回不来了（同 `set_llm_profile_model` 的取舍）。


def _publish_text(paths: StudioPaths) -> str:
    return (paths.config_dir / "publish.yaml").read_text(encoding="utf-8")


def _accounts_block(text: str) -> str:
    """``accounts:`` 段到下一个顶层键之间的正文（**含**段头那一行）。"""
    head = text.index("\naccounts:")
    tail = text.index("\nplatforms:")
    return text[head + 1 : tail]


def _account(**overrides: Any) -> AccountConfig:
    payload: dict[str, Any] = {
        "account_id": "dy_backup",
        "platform": "douyin",
        "display_name": "抖音副号",
        "profile_dir": "data/browser_profile/dy_backup",
        "enabled": True,
        "daily_limit": 2,
        "min_gap_min": 60,
    }
    return AccountConfig.model_validate(payload | overrides)


def test_write_publish_accounts_appends_and_leaves_the_rest_alone(config_paths: StudioPaths) -> None:
    """加一个号：段外一个字节都不动（``platforms`` / ``precheck`` / 注释全留着）。"""
    before = _publish_text(config_paths)
    write_publish_accounts(config_paths, [*load_publish_config(config_paths).accounts, _account()])
    after = _publish_text(config_paths)

    assert [account.account_id for account in load_publish_config(config_paths).accounts] == [
        "acc_main",
        "_rehearsal",
        "dy_backup",
    ]
    # 段外（头 + platforms 之后）逐字未变
    assert before.split("\naccounts:")[0] == after.split("\naccounts:")[0]
    assert before.split("\nplatforms:", 1)[1] == after.split("\nplatforms:", 1)[1]
    # 新条目长成了出厂文件那个形状（字段齐全、缩进两级）
    assert "  - account_id: dy_backup\n" in after
    assert "    daily_limit: 2\n" in after


def test_write_publish_accounts_keeps_inline_comments_when_updating(config_paths: StudioPaths) -> None:
    """改一个值：行尾那句注释**必须还在** —— 它是"这个字段为什么这么配"的记录。"""
    accounts = list(load_publish_config(config_paths).accounts)
    accounts[0] = accounts[0].model_copy(update={"daily_limit": 5})

    write_publish_accounts(config_paths, accounts)
    text = _publish_text(config_paths)

    assert "    daily_limit: 5\n" in text
    assert "    profile_dir: data/browser_profile/acc_main   # 持久化登录态（.gitignore）\n" in text


def test_write_publish_accounts_drops_the_removed_account_comments(config_paths: StudioPaths) -> None:
    """删一个号：**它上方那几行注释跟着走**（留着就是一段描述不存在账号的文字）。"""
    accounts = [a for a in load_publish_config(config_paths).accounts if a.account_id != "_rehearsal"]

    write_publish_accounts(config_paths, accounts)
    block = _accounts_block(_publish_text(config_paths))

    assert "_rehearsal" not in block
    assert "本地演练台的账号" not in block
    # 段级注释（属于 platforms: 的开场白）不能被顺手带走
    assert "# 一线平台（一期必做）" in _publish_text(config_paths)


def test_write_publish_accounts_writes_an_explicit_empty_list(config_paths: StudioPaths) -> None:
    """删光了要留 ``accounts: []``：光秃秃的 ``accounts:`` 读回来是 ``None`` ⇒ 下一次加载炸。"""
    write_publish_accounts(config_paths, [])

    assert load_publish_config(config_paths).accounts == []
    assert "accounts: []\n" in _publish_text(config_paths)


def test_write_publish_accounts_refuses_duplicate_profile_dir(config_paths: StudioPaths) -> None:
    """两个号共用一个 ``profile_dir`` ⇒ 拒绝，且**盘上一个字节都不动**。"""
    before = _publish_text(config_paths)
    accounts = [
        *load_publish_config(config_paths).accounts,
        _account(profile_dir="data/browser_profile/acc_main"),
    ]

    with pytest.raises(ConfigError) as excinfo:
        write_publish_accounts(config_paths, accounts)

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "profile_dir" in str(excinfo.value.context)
    assert _publish_text(config_paths) == before


def test_write_publish_accounts_is_idempotent(config_paths: StudioPaths) -> None:
    """同一份清单写两次 ⇒ 第二次一个字节都不改（mtime 白跳会惊动热重载）。"""
    accounts = [*load_publish_config(config_paths).accounts, _account()]
    write_publish_accounts(config_paths, accounts)
    once = _publish_text(config_paths)

    write_publish_accounts(config_paths, accounts)

    assert _publish_text(config_paths) == once


def test_write_publish_accounts_can_refill_after_emptying(config_paths: StudioPaths) -> None:
    """``accounts: []`` 之后再加号 ⇒ 头行上的 ``[]`` 要清掉（否则列表接在它后面是非法 YAML）。"""
    write_publish_accounts(config_paths, [])
    write_publish_accounts(config_paths, [_account(account_id="acc_second")])

    assert [a.account_id for a in load_publish_config(config_paths).accounts] == ["acc_second"]
    assert "accounts: []" not in _publish_text(config_paths)


def test_parse_publish_account_reports_form_errors_as_validation_failed() -> None:
    """表单不合法 ⇒ 422 那一档（``VALIDATION_FAILED``）+ 逐字段的错误，面板据此标红。"""
    with pytest.raises(ConfigError) as excinfo:
        parse_publish_account({"account_id": "acc_x", "platform": "tiktok", "profile_dir": "data/x"})

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    fields = {row["field"] for row in excinfo.value.context["errors"]}
    assert any("platform" in field for field in fields)


def test_parse_publish_account_quotes_ambiguous_scalars() -> None:
    """``display_name: yes`` 读回来是**布尔** —— 渲染时必须加引号（否则下次加载炸）。"""
    account = parse_publish_account(
        {"account_id": "acc_y", "platform": "douyin", "profile_dir": "data/x", "display_name": "yes"}
    )

    assert account.display_name == "yes"
