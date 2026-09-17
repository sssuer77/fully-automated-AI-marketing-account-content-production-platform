"""密钥热重载仓库（T6.1）。

这一组测的是**四条会被用户直接踩到的规矩**：
① 环境变量优先于文件（容器与个人机器两种用法不能互相打架）；
② 写盘是原子的、且**只动该动的键**（别把 ``webui.password`` 顺手抹了）；
③ 「填了同一把」⇒ 不写盘（不假装做了一次操作）；
④ 文件被改坏 ⇒ **沿用上一份可用值**并如实记 ``last_error``（DoD 6：可降级）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from studio.core.errors import ConfigError
from studio.core.paths import StudioPaths
from studio.core.secret_store import (
    API_KEY_MAX_LEN,
    API_KEY_MIN_LEN,
    LLM_API_KEY_ENV,
    SECRETS_FILE_NAME,
    SecretStore,
    mask_secret,
    validate_api_key,
)

GOOD_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    return value


@pytest.fixture
def store(paths: StudioPaths) -> Iterator[SecretStore]:
    yield SecretStore(paths, env={})


# ── 掩码 ────────────────────────────────────────────────────────────────


def test_mask_keeps_head_and_tail_only() -> None:
    """留头留尾是为了「认得出是哪一把」，中间一律星号。"""
    masked = mask_secret(GOOD_KEY)
    assert masked is not None
    assert masked.startswith("sk-")
    assert masked.endswith("012345"[-4:])
    assert GOOD_KEY not in masked
    assert "*" not in masked


def test_mask_hides_short_values_entirely() -> None:
    """短到「留头留尾就等于全露」的值 ⇒ 一个字符都不给。"""
    assert mask_secret("abc") == "…***"


def test_mask_of_nothing_is_none() -> None:
    assert mask_secret(None) is None
    assert mask_secret("   ") is None


# ── 表单校验 ────────────────────────────────────────────────────────────


def test_validate_strips_surrounding_whitespace() -> None:
    """复制粘贴常带首尾空白 —— 去掉它，而不是拒绝用户。"""
    assert validate_api_key(f"  {GOOD_KEY}  ") == GOOD_KEY


@pytest.mark.parametrize(
    ("value", "keyword"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        ("short", "太短"),
        ("a" * (API_KEY_MAX_LEN + 1), "太长"),
        ("sk-abc def ghijklmn", "空格或换行"),
        ("sk-abc" + chr(10) + "defghijklmn", "空格或换行"),
    ],
)
def test_validate_rejects_bad_values(value: str, keyword: str) -> None:
    """每一条拒绝都要说清楚**为什么**（面板把这句话直接显示在输入框下）。"""
    with pytest.raises(ConfigError) as caught:
        validate_api_key(value)
    assert keyword in caught.value.message


def test_validate_accepts_a_minimal_key() -> None:
    assert len(validate_api_key("a" * API_KEY_MIN_LEN)) == API_KEY_MIN_LEN


# ── 读 ──────────────────────────────────────────────────────────────────


def test_missing_file_is_not_an_error(store: SecretStore, paths: StudioPaths) -> None:
    """出厂状态（没有 secrets.yaml）必须能正常回答，而不是抛异常。"""
    snapshot = store.snapshot()
    assert snapshot.exists is False
    assert snapshot.configured is False
    assert store.api_key() is None
    assert store.source() == "none"
    assert store.path == paths.config_dir / SECRETS_FILE_NAME


def test_env_wins_over_file(store: SecretStore, paths: StudioPaths) -> None:
    """环境变量优先 —— 容器 / CI 的用法不能被盘上那一份压过。"""
    store.set_api_key("sk-filefilefilefile")
    assert store.api_key() == "sk-filefilefilefile"
    assert store.source() == "file"

    env_store = SecretStore(paths, env={LLM_API_KEY_ENV: "sk-envenvenvenvenv"})
    assert env_store.api_key() == "sk-envenvenvenvenv"
    assert env_store.source() == "env"
    # 文件里那份**还在**（只是不生效）—— 清掉环境变量就该回来
    assert env_store.snapshot().api_key == "sk-filefilefilefile"


def test_lookup_only_knows_the_llm_variable(store: SecretStore) -> None:
    """别的变量名只认环境变量，不猜、不串（自建代理另起的变量名走 env）。"""
    store.set_api_key(GOOD_KEY)
    assert store.lookup(LLM_API_KEY_ENV) == GOOD_KEY
    assert store.lookup("SOME_OTHER_KEY") is None


# ── 写 ──────────────────────────────────────────────────────────────────


def test_write_then_read_back(store: SecretStore, paths: StudioPaths) -> None:
    change = store.set_api_key(GOOD_KEY)
    assert change.changed is True
    assert store.api_key() == GOOD_KEY
    assert store.source() == "file"
    assert store.snapshot().exists is True

    raw = yaml.safe_load((paths.config_dir / SECRETS_FILE_NAME).read_text(encoding="utf-8"))
    assert raw["llm"]["api_key"] == GOOD_KEY


def test_write_preserves_other_sections(paths: StudioPaths) -> None:
    """写密钥**只动 ``llm.api_key``**：把 webui 密码顺手抹掉是最难查的一类事故。"""
    target = paths.config_dir / SECRETS_FILE_NAME
    target.write_text(
        yaml.safe_dump({"schema_version": "1.0", "webui": {"password": "keep-me"}}),
        encoding="utf-8",
    )
    SecretStore(paths, env={}).set_api_key(GOOD_KEY)
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["webui"]["password"] == "keep-me"
    assert raw["llm"]["api_key"] == GOOD_KEY
    assert raw["schema_version"] == "1.0"


def test_rewriting_the_same_key_touches_nothing(store: SecretStore, paths: StudioPaths) -> None:
    """填了同一把 ⇒ ``changed=False`` 且**不写盘**（不假装做了一次操作）。"""
    store.set_api_key(GOOD_KEY)
    target = paths.config_dir / SECRETS_FILE_NAME
    stamp = target.stat().st_mtime_ns

    again = store.set_api_key(GOOD_KEY)
    assert again.changed is False
    assert target.stat().st_mtime_ns == stamp


def test_clear_removes_the_entry_and_is_idempotent(store: SecretStore, paths: StudioPaths) -> None:
    store.set_api_key(GOOD_KEY)
    cleared = store.set_api_key(None)
    assert cleared.changed is True
    assert store.api_key() is None
    assert store.source() == "none"

    raw = yaml.safe_load((paths.config_dir / SECRETS_FILE_NAME).read_text(encoding="utf-8"))
    assert "llm" not in raw  # 空节不留（下次读回来是 {}，不是 {"llm": {}}）

    assert store.set_api_key(None).changed is False


def test_write_rejects_bad_value_without_touching_disk(store: SecretStore, paths: StudioPaths) -> None:
    """**先校验、后落盘**：校验不过时盘上连文件都不该出现。"""
    with pytest.raises(ConfigError):
        store.set_api_key("short")
    assert not (paths.config_dir / SECRETS_FILE_NAME).exists()


def test_written_file_carries_the_header(store: SecretStore, paths: StudioPaths) -> None:
    """文件头要写明「绝不入库」与「改完立刻生效」，免得有人手改时把它当普通配置。"""
    store.set_api_key(GOOD_KEY)
    text = (paths.config_dir / SECRETS_FILE_NAME).read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "绝不入库" in text
    assert "不需要重启" in text


# ── 热重载与降级 ────────────────────────────────────────────────────────


def test_external_edit_is_picked_up(store: SecretStore, paths: StudioPaths) -> None:
    """手工改文件也要生效 —— 面板不是唯一的写入口。"""
    store.set_api_key(GOOD_KEY)
    target = paths.config_dir / SECRETS_FILE_NAME
    target.write_text(
        yaml.safe_dump({"schema_version": "1.0", "llm": {"api_key": "sk-externally-edited-01"}}),
        encoding="utf-8",
    )
    assert store.api_key() == "sk-externally-edited-01"


def test_broken_file_keeps_the_last_good_value(store: SecretStore, paths: StudioPaths) -> None:
    """改坏了 ⇒ **沿用上一份可用值**并记 ``last_error``（不把在跑的任务打死）。"""
    store.set_api_key(GOOD_KEY)
    target = paths.config_dir / SECRETS_FILE_NAME
    target.write_text("llm: [unclosed", encoding="utf-8")

    assert store.api_key() == GOOD_KEY
    assert store.last_error is not None


def test_deleting_the_file_clears_the_value(store: SecretStore, paths: StudioPaths) -> None:
    """文件被删掉 ⇒ 如实回答「没配了」（与「读不出来」是两件事）。"""
    store.set_api_key(GOOD_KEY)
    (paths.config_dir / SECRETS_FILE_NAME).unlink()
    assert store.api_key() is None
    assert store.last_error is None
    assert store.snapshot().exists is False
