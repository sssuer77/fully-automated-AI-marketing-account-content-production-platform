"""人物热重载仓库（T1.2 · "随时改人物"的工程实现）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.config import load_persona_file
from studio.core.errors import ConfigError, ErrorCode
from studio.core.paths import StudioPaths
from studio.core.persona_store import (
    PersonaChange,
    PersonaStore,
    get_persona_store,
    reset_persona_store,
)

PERSONA_TEMPLATE = """\
schema_version: "1.0"
id: {persona_id}
name: {name}
is_active: true
role_desc: 测试用人设描述文本
tone: {tone}
audience: 测试受众
catchphrases:
  - 口癖一
  - 口癖二
forbidden:
  - 禁区词
target_chars_min: 600
target_chars_max: 800
max_duration_ms: 180000
"""


def _persona_yaml(persona_id: str, *, name: str = "测试人物", tone: str = "测试口吻") -> str:
    return PERSONA_TEMPLATE.format(persona_id=persona_id, name=name, tone=tone)


@pytest.fixture
def persona_paths(tmp_path: Path) -> StudioPaths:
    home = tmp_path / "studio"
    (home / "config" / "personas").mkdir(parents=True)
    (home / "config" / "persona.yaml").write_text(_persona_yaml("persona_default"), encoding="utf-8")
    return StudioPaths(home=home, data_dir=home / "data")


@pytest.fixture
def store(persona_paths: StudioPaths) -> PersonaStore:
    return PersonaStore(persona_paths)


def _write_library(paths: StudioPaths, persona_id: str, content: str | None = None) -> Path:
    path = paths.persona_library_dir / f"{persona_id}.yaml"
    path.write_text(content if content is not None else _persona_yaml(persona_id), encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# 首次加载
# ══════════════════════════════════════════════════════════════════════


def test_initial_load(store: PersonaStore, persona_paths: StudioPaths) -> None:
    snapshot = store.current()
    assert snapshot.persona_id == "persona_default"
    assert snapshot.config.name == "测试人物"
    assert snapshot.version == 1
    assert snapshot.sha256
    assert snapshot.path == persona_paths.persona_file
    assert store.last_error is None


def test_initial_load_missing_file_raises(persona_paths: StudioPaths) -> None:
    persona_paths.persona_file.unlink()
    store = PersonaStore(persona_paths)
    with pytest.raises(ConfigError) as excinfo:
        store.current()
    assert excinfo.value.code is ErrorCode.CONFIG_MISSING


def test_initial_load_invalid_file_raises(persona_paths: StudioPaths) -> None:
    persona_paths.persona_file.write_text("id: x\nname: [未闭合\n", encoding="utf-8")
    store = PersonaStore(persona_paths)
    with pytest.raises(ConfigError) as excinfo:
        store.current()
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


# ══════════════════════════════════════════════════════════════════════
# 热重载：改文件即刻生效
# ══════════════════════════════════════════════════════════════════════


def test_hot_reload_picks_up_edit(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    persona_paths.persona_file.write_text(
        _persona_yaml("persona_default", name="换了个名字", tone="换成京腔"), encoding="utf-8"
    )
    snapshot = store.current()
    assert snapshot.config.name == "换了个名字"
    assert snapshot.config.tone == "换成京腔"
    assert snapshot.version == 2


def test_no_change_keeps_version(store: PersonaStore) -> None:
    first = store.current()
    assert store.current().version == first.version
    assert store.current().sha256 == first.sha256


def test_version_increases_monotonically(store: PersonaStore, persona_paths: StudioPaths) -> None:
    versions = [store.current().version]
    for index in range(3):
        persona_paths.persona_file.write_text(
            _persona_yaml("persona_default", name=f"第{index}版"), encoding="utf-8"
        )
        versions.append(store.current().version)
    assert versions == [1, 2, 3, 4]


def test_hot_reload_switches_persona_id(store: PersonaStore, persona_paths: StudioPaths) -> None:
    """换人物（id 也变）—— 这正是"随时可能改人物"的核心场景。"""
    store.current()
    persona_paths.persona_file.write_text(_persona_yaml("another_host", name="另一个人"), encoding="utf-8")
    snapshot = store.current()
    assert snapshot.persona_id == "another_host"
    assert snapshot.config.name == "另一个人"


# ══════════════════════════════════════════════════════════════════════
# 失败不中断（DoD 6：可降级、无静默失败）
# ══════════════════════════════════════════════════════════════════════


def test_broken_edit_keeps_last_good_snapshot(store: PersonaStore, persona_paths: StudioPaths) -> None:
    good = store.current()
    persona_paths.persona_file.write_text("id: x\nname: [未闭合\n", encoding="utf-8")
    snapshot = store.current()
    assert snapshot.sha256 == good.sha256
    assert snapshot.version == good.version
    assert store.last_error is not None
    assert store.last_error.code is ErrorCode.CONFIG_INVALID


def test_broken_edit_missing_field_also_keeps_snapshot(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    good = store.current()
    persona_paths.persona_file.write_text('schema_version: "1.0"\nid: persona_default\n', encoding="utf-8")
    assert store.current().sha256 == good.sha256
    assert store.last_error is not None
    assert store.last_error.code is ErrorCode.CONFIG_PERSONA_INCOMPLETE


def test_explicit_reload_raises_on_broken(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    persona_paths.persona_file.write_text("not: [valid\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        store.reload()


def test_recovery_clears_last_error(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    persona_paths.persona_file.write_text("bad: [\n", encoding="utf-8")
    store.current()
    broken_error = store.last_error
    assert broken_error is not None
    persona_paths.persona_file.write_text(_persona_yaml("persona_default", name="修好了"), encoding="utf-8")
    snapshot = store.current()
    assert store.last_error is None
    assert snapshot.config.name == "修好了"


def test_file_deleted_at_runtime_keeps_snapshot(store: PersonaStore, persona_paths: StudioPaths) -> None:
    good = store.current()
    persona_paths.persona_file.unlink()
    assert store.current().sha256 == good.sha256
    assert store.last_error is not None


# ══════════════════════════════════════════════════════════════════════
# 人物库
# ══════════════════════════════════════════════════════════════════════


def test_library_empty_when_dir_missing(persona_paths: StudioPaths) -> None:
    persona_paths.persona_library_dir.rmdir()
    assert PersonaStore(persona_paths).library() == ()


def test_library_lists_entries_sorted(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "zeta", _persona_yaml("zeta", name="Z"))
    _write_library(persona_paths, "alpha", _persona_yaml("alpha", name="A"))
    entries = store.library()
    assert [entry.persona_id for entry in entries] == ["alpha", "zeta"]
    assert all(entry.valid for entry in entries)
    assert entries[0].name == "A"


def test_library_reports_invalid_entry_without_failing(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    _write_library(persona_paths, "good", _persona_yaml("good"))
    _write_library(persona_paths, "broken", "id: broken\nname: [\n")
    entries = {entry.persona_id: entry for entry in store.library()}
    assert entries["good"].valid
    assert not entries["broken"].valid
    assert entries["broken"].error


def test_library_flags_id_filename_mismatch(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "wrong_name", _persona_yaml("other_id"))
    entry = store.library()[0]
    assert not entry.valid
    assert "不一致" in entry.error


def test_library_ignores_non_yaml(store: PersonaStore, persona_paths: StudioPaths) -> None:
    (persona_paths.persona_library_dir / "README.md").write_text("# 说明\n", encoding="utf-8")
    _write_library(persona_paths, "alpha", _persona_yaml("alpha"))
    assert [entry.persona_id for entry in store.library()] == ["alpha"]


def test_library_entry_unknown_raises(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "alpha", _persona_yaml("alpha"))
    with pytest.raises(ConfigError) as excinfo:
        store.library_entry("nope")
    assert excinfo.value.code is ErrorCode.CONFIG_MISSING
    assert "alpha" in (excinfo.value.remediation or "")


# ══════════════════════════════════════════════════════════════════════
# 一键切换（activate）
# ══════════════════════════════════════════════════════════════════════


def test_activate_switches_active_persona(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "solo", _persona_yaml("solo", name="单人解说"))
    snapshot = store.activate("solo")
    assert snapshot.persona_id == "solo"
    assert snapshot.config.name == "单人解说"
    assert persona_paths.persona_file.read_text(encoding="utf-8") == _persona_yaml("solo", name="单人解说")
    # 切换后 current() 立刻反映新人物，且不再触发一次重载
    assert store.current().persona_id == "solo"
    assert store.current().version == snapshot.version


def test_activate_backs_up_previous(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    _write_library(persona_paths, "solo", _persona_yaml("solo", name="单人解说"))
    store.activate("solo")
    backups = list(store.backup_dir.glob("*.yaml"))
    assert len(backups) == 1
    assert "persona_default" in backups[0].name
    assert backups[0].read_text(encoding="utf-8") == _persona_yaml("persona_default")


def test_activate_backup_names_cold_process(store: PersonaStore, persona_paths: StudioPaths) -> None:
    """冷进程直接切换（CLI 路径）：备份名必须带**切换前**的人物 id。"""
    _write_library(persona_paths, "solo", _persona_yaml("solo", name="单人解说"))
    store.activate("solo")
    backups = list(store.backup_dir.glob("*.yaml"))
    assert len(backups) == 1
    assert "persona_default" in backups[0].name


def test_activate_backup_name_falls_back_to_file_id(store: PersonaStore, persona_paths: StudioPaths) -> None:
    """激活文件损坏 ⇒ 仍从原文里抠出 id 作备份名（不留 unknown 噪音）。"""
    _write_library(persona_paths, "solo", _persona_yaml("solo"))
    persona_paths.persona_file.write_text(
        'schema_version: "1.0"\nid: broken_but_named\nname: [\n', encoding="utf-8"
    )
    store.activate("solo")
    backups = list(store.backup_dir.glob("*.yaml"))
    assert len(backups) == 1
    assert "broken_but_named" in backups[0].name


def test_activate_backup_name_unknown_when_unparsable(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    """彻底不可解析 ⇒ 退回 unknown，但备份照样落盘（回滚永远可用）。"""
    _write_library(persona_paths, "solo", _persona_yaml("solo"))
    persona_paths.persona_file.write_text("这不是 YAML\n", encoding="utf-8")
    store.activate("solo")
    backups = list(store.backup_dir.glob("*.yaml"))
    assert len(backups) == 1
    assert "unknown" in backups[0].name
    assert backups[0].read_text(encoding="utf-8") == "这不是 YAML\n"


def test_activate_can_skip_backup(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    _write_library(persona_paths, "solo", _persona_yaml("solo"))
    store.activate("solo", backup=False)
    assert not store.backup_dir.exists() or not list(store.backup_dir.glob("*.yaml"))


def test_activate_unknown_id_raises(store: PersonaStore, persona_paths: StudioPaths) -> None:
    with pytest.raises(ConfigError) as excinfo:
        store.activate("ghost")
    assert excinfo.value.code is ErrorCode.CONFIG_MISSING


def test_activate_invalid_entry_does_not_touch_active(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    _write_library(persona_paths, "broken", "id: broken\nname: [\n")
    before = persona_paths.persona_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        store.activate("broken")
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert persona_paths.persona_file.read_text(encoding="utf-8") == before


def test_activate_id_filename_mismatch_rejected(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "wrong_name", _persona_yaml("other_id"))
    before = persona_paths.persona_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        store.activate("wrong_name")
    assert persona_paths.persona_file.read_text(encoding="utf-8") == before


def test_activate_round_trip(store: PersonaStore, persona_paths: StudioPaths) -> None:
    """换过去再换回来，内容与版本都正确（"改错人"永远能回滚）。"""
    _write_library(persona_paths, "persona_default", _persona_yaml("persona_default"))
    _write_library(persona_paths, "solo", _persona_yaml("solo", name="单人解说"))
    store.activate("solo")
    assert store.current().persona_id == "solo"
    store.activate("persona_default")
    assert store.current().config.name == "测试人物"
    assert store.current().persona_id == "persona_default"


@pytest.mark.parametrize("bad_id", ["", "UPPER", "有中文", "has space", "-lead", "a" * 65])
def test_invalid_persona_id_rejected(store: PersonaStore, bad_id: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        store.activate(bad_id)
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


# ══════════════════════════════════════════════════════════════════════
# save-as（改完存一份）
# ══════════════════════════════════════════════════════════════════════


def test_save_as_writes_library_entry(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.current()
    target = store.save_as("my_v2")
    assert target == persona_paths.persona_library_dir / "my_v2.yaml"
    assert target.read_text(encoding="utf-8") == persona_paths.persona_file.read_text(encoding="utf-8")
    # 存进去的是当前人物内容，但 id 仍是 persona_default ⇒ 文件名不匹配 ⇒ 标记为无效
    entry = next(item for item in store.library() if item.persona_id == "my_v2")
    assert not entry.valid
    assert "不一致" in entry.error


def test_save_as_refuses_existing_without_overwrite(store: PersonaStore) -> None:
    store.current()
    store.save_as("snap")
    with pytest.raises(ConfigError) as excinfo:
        store.save_as("snap")
    assert "已存在" in excinfo.value.message


def test_save_as_overwrite_succeeds(store: PersonaStore) -> None:
    store.current()
    target = store.save_as("snap")
    target.write_text("stale: true\n", encoding="utf-8")
    store.save_as("snap", overwrite=True)
    assert "stale" not in target.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# 订阅（T1.7 接 WS system.persona_changed）
# ══════════════════════════════════════════════════════════════════════


def test_subscriber_receives_change(persona_paths: StudioPaths) -> None:
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    persona_paths.persona_file.write_text(_persona_yaml("persona_default", name="新版"), encoding="utf-8")
    store.current()
    assert len(events) == 2
    assert events[0].previous is None
    assert events[0].reason == "initial"
    assert events[1].reason == "hot_reload"
    assert events[1].previous is not None
    assert events[1].new.config.name == "新版"
    assert events[1].to_dict()["previous_version"] == 1


def test_subscribe_after_construction(persona_paths: StudioPaths) -> None:
    store = PersonaStore(persona_paths)
    seen: list[int] = []
    store.subscribe(lambda change: seen.append(change.version))
    store.current()
    assert seen == [1]


def test_listener_exception_does_not_break_load(persona_paths: StudioPaths) -> None:
    def boom(change: PersonaChange) -> None:
        raise RuntimeError("订阅者炸了")

    store = PersonaStore(persona_paths, listeners=(boom,))
    assert store.current().version == 1


def test_no_event_when_content_unchanged(persona_paths: StudioPaths) -> None:
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    store.current()
    assert len(events) == 1


# ══════════════════════════════════════════════════════════════════════
# 序列化与单例
# ══════════════════════════════════════════════════════════════════════


def test_snapshot_to_dict(store: PersonaStore) -> None:
    payload = store.current().to_dict()
    assert payload["persona_id"] == "persona_default"
    assert payload["name"] == "测试人物"
    assert payload["version"] == 1
    assert payload["source"] == "active"
    assert payload["catchphrases"] == ["口癖一", "口癖二"]
    assert payload["forbidden_count"] == 1


def test_library_entry_to_dict(store: PersonaStore, persona_paths: StudioPaths) -> None:
    _write_library(persona_paths, "alpha", _persona_yaml("alpha", name="A"))
    payload = store.library()[0].to_dict()
    assert payload["persona_id"] == "alpha"
    assert payload["valid"] is True
    assert payload["error"] == ""


def test_singleton_is_reused_and_resettable(persona_paths: StudioPaths) -> None:
    reset_persona_store()
    try:
        first = get_persona_store(persona_paths)
        assert get_persona_store(persona_paths) is first
        reset_persona_store()
        assert get_persona_store(persona_paths) is not first
    finally:
        reset_persona_store()


def test_matches_load_persona_file(persona_paths: StudioPaths) -> None:
    """人物库与激活文件走**同一套**校验器 ⇒ 校验标准不分叉。"""

    store = PersonaStore(persona_paths)
    assert load_persona_file(persona_paths.persona_file).model_dump() == store.current().config.model_dump()


def test_same_error_code_as_config_layer(persona_paths: StudioPaths) -> None:
    """同一个坏文件，store 与 config 层必须报**同一个**错误码。"""

    persona_paths.persona_file.write_text('schema_version: "1.0"\nid: x\n', encoding="utf-8")
    with pytest.raises(ConfigError) as from_config:
        load_persona_file(persona_paths.persona_file)
    store = PersonaStore(persona_paths)
    with pytest.raises(ConfigError) as from_store:
        store.current()
    assert from_config.value.code is from_store.value.code is ErrorCode.CONFIG_PERSONA_INCOMPLETE


def test_backup_dir_defaults_under_data(persona_paths: StudioPaths) -> None:
    assert PersonaStore(persona_paths).backup_dir == persona_paths.backups_dir / "persona"


def test_custom_backup_dir(persona_paths: StudioPaths, tmp_path: Path) -> None:
    custom = tmp_path / "custom_backups"
    assert PersonaStore(persona_paths, backup_dir=custom).backup_dir == custom


# ══════════════════════════════════════════════════════════════════════
# 编辑（T4.13：表单保存 · **先校验 → 再备份 → 后落盘**）
# ══════════════════════════════════════════════════════════════════════


def test_update_writes_only_the_given_fields(store: PersonaStore, persona_paths: StudioPaths) -> None:
    before = store.current().config
    snapshot = store.update({"tone": "京腔、快嘴"})
    assert snapshot.config.tone == "京腔、快嘴"
    assert snapshot.config.name == before.name
    assert snapshot.config.catchphrases == before.catchphrases
    assert snapshot.version == 2
    assert load_persona_file(persona_paths.persona_file).tone == "京腔、快嘴"


def test_update_renders_a_loadable_file_with_schema_version(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    """序列化出来必须**能被自己读回去**（`schema_version` 在顶层、中文不转义）。"""
    store.update({"name": "熊大熊二·改"})
    text = persona_paths.persona_file.read_text(encoding="utf-8")
    assert text.startswith("# ")
    assert "schema_version" in text
    assert "熊大熊二·改" in text, "中文必须原样（allow_unicode）"
    assert load_persona_file(persona_paths.persona_file).name == "熊大熊二·改"


def test_update_rejects_invalid_form_without_touching_anything(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    """★ 校验不过 ⇒ 文件、备份、版本号三处都**一点没动**。"""
    store.current()  # 先有一次正常加载，这样"版本没动"才是可判的
    before = persona_paths.persona_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        store.update({"name": ""})
    assert excinfo.value.code is ErrorCode.PERSONA_INVALID
    assert excinfo.value.context["field_errors"][0]["field"] == "name"
    assert persona_paths.persona_file.read_text(encoding="utf-8") == before
    assert store.backups() == ()
    assert store.version == 1, "连内存版本都不该动"


def test_update_rejects_unknown_field(store: PersonaStore) -> None:
    """`id` 不在白名单 ⇒ 拒绝（它是文件名 / 备份名 / 审计 target_id 三处共用的身份）。"""
    with pytest.raises(ConfigError) as excinfo:
        store.update({"id": "other"})
    assert excinfo.value.code is ErrorCode.PERSONA_INVALID
    assert excinfo.value.context["field"] == "id"


def test_update_backs_up_previous_content(store: PersonaStore, persona_paths: StudioPaths) -> None:
    original = persona_paths.persona_file.read_text(encoding="utf-8")
    store.update({"tone": "京腔"})
    backups = store.backups()
    assert len(backups) == 1
    assert backups[0].valid is True
    assert backups[0].persona_id == "persona_default"
    assert backups[0].path.read_text(encoding="utf-8") == original


def test_update_can_skip_backup(store: PersonaStore) -> None:
    store.update({"tone": "京腔"}, backup=False)
    assert store.backups() == ()


def test_preview_validates_without_writing(store: PersonaStore, persona_paths: StudioPaths) -> None:
    before = persona_paths.persona_file.read_text(encoding="utf-8")
    config = store.preview({"target_chars_min": 700, "target_chars_max": 900})
    assert config.target_chars_min == 700
    assert persona_paths.persona_file.read_text(encoding="utf-8") == before
    assert store.backups() == ()
    with pytest.raises(ConfigError) as excinfo:
        store.preview({"tone": "x"})
    assert excinfo.value.code is ErrorCode.PERSONA_INVALID


# ══════════════════════════════════════════════════════════════════════
# 备份与回滚（T4.13：改错了永远退得回来）
# ══════════════════════════════════════════════════════════════════════


def test_backups_empty_when_dir_missing(store: PersonaStore) -> None:
    assert store.backups() == ()


def test_backups_newest_first_and_capped(store: PersonaStore) -> None:
    """按 **mtime** 新到旧（不是按文件名：同秒的三份名字只差 `-2` / `-3` 后缀）。"""
    for index in range(3):
        store.update({"tone": f"口吻{index}"})
    backups = store.backups()
    assert len(backups) == 3
    assert [item.created_at for item in backups] == sorted(
        (item.created_at for item in backups), reverse=True
    )
    # 最新那份备份装的是**倒数第二次改动之后**的内容（每次写前先备份）
    assert "口吻1" in backups[0].path.read_text(encoding="utf-8")
    assert len(store.backups(limit=2)) == 2


def test_backups_flags_invalid_entry(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.update({"tone": "京腔"})
    broken = store.backup_dir / "20200101-000000_persona_default.yaml"
    broken.write_text("id: persona_default\nname: [\n", encoding="utf-8")
    listed = {item.name: item for item in store.backups()}
    assert listed[broken.name].valid is False
    assert listed[broken.name].error
    assert listed[broken.name].persona_id == "persona_default"


def test_rollback_restores_and_backs_up_current(store: PersonaStore, persona_paths: StudioPaths) -> None:
    original = persona_paths.persona_file.read_text(encoding="utf-8")
    store.update({"tone": "京腔"})
    target = store.backups()[0].name
    snapshot = store.rollback(target)
    assert snapshot.config.tone != "京腔"
    assert persona_paths.persona_file.read_text(encoding="utf-8") == original
    assert len(store.backups()) == 2, "回滚前也备份 ⇒ 回滚本身还能再回滚"


def test_rollback_same_second_does_not_clobber_its_own_backup(
    store: PersonaStore, persona_paths: StudioPaths
) -> None:
    """★ 同秒"改一次 + 回滚一次"不能把要回滚的那份备份**覆盖掉**。

    备份名只精确到秒（`file_stamp()`），撞名时若直接覆盖，回滚读到的就是刚写下的
    那一份 —— 回滚等于没回滚，而且看不出来（版本 +1、sha 不变）。
    """
    original = persona_paths.persona_file.read_text(encoding="utf-8")
    store.update({"tone": "京腔"})
    target = store.backups()[0].name
    store.rollback(target)
    assert persona_paths.persona_file.read_text(encoding="utf-8") == original
    assert len({item.name for item in store.backups()}) == 2


def test_rollback_rejects_broken_backup(store: PersonaStore, persona_paths: StudioPaths) -> None:
    store.update({"tone": "京腔"})
    before = persona_paths.persona_file.read_text(encoding="utf-8")
    broken = store.backup_dir / "20200101-000000_persona_default.yaml"
    broken.write_text("id: persona_default\nname: [\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        store.rollback(broken.name)
    assert excinfo.value.code is ErrorCode.PERSONA_INVALID
    assert persona_paths.persona_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "name",
    ["../../config/persona.yaml", "..\\persona.yaml", "/etc/passwd", "nope.yaml", "", "a" * 200],
)
def test_backup_name_is_whitelisted(store: PersonaStore, name: str) -> None:
    """白名单正则：路径穿越 / 任意文件名一个都进不来。"""
    with pytest.raises(ConfigError) as excinfo:
        store.backup_entry(name)
    assert excinfo.value.code is ErrorCode.PERSONA_NOT_FOUND


def test_backup_entry_unknown_lists_recent(store: PersonaStore) -> None:
    store.update({"tone": "京腔"})
    with pytest.raises(ConfigError) as excinfo:
        store.backup_entry("20200101-000000_persona_default.yaml")
    assert "最近的备份" in (excinfo.value.remediation or "")


def test_backup_entry_to_dict(store: PersonaStore) -> None:
    store.update({"tone": "京腔"})
    payload = store.backups()[0].to_dict()
    assert set(payload) == {"name", "path", "persona_id", "created_at", "sha256", "size", "valid", "error"}
    assert payload["valid"] is True
    size = payload["size"]
    assert isinstance(size, int)
    assert size > 0


# ══════════════════════════════════════════════════════════════════════
# 退订与失败广播（T4.13）
# ══════════════════════════════════════════════════════════════════════


def test_unsubscribe_removes_the_listener(persona_paths: StudioPaths) -> None:
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    assert store.unsubscribe(events.append) is True
    assert store.unsubscribe(events.append) is False, "重复退订是幂等的"
    events.clear()  # 上面那次初始加载当然进过 events；这里看的是退订**之后**
    persona_paths.persona_file.write_text(_persona_yaml("persona_default", tone="换了口吻"), encoding="utf-8")
    store.current()
    assert events == []


def test_failure_broadcast_once_per_broken_file(persona_paths: StudioPaths) -> None:
    """同一份坏文件只广播一次（否则每次读都是一条告警）。"""
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    events.clear()  # 只看"改坏之后"这一段
    persona_paths.persona_file.write_text("id: persona_default\nname: [\n", encoding="utf-8")
    store.current()
    store.current()
    store.current()
    assert [item.failed for item in events] == [True]
    assert events[0].new is events[0].previous, "`new` 是仍在生效的上一份"
    assert events[0].to_dict()["error"]


def test_failure_broadcast_again_after_a_new_break(persona_paths: StudioPaths) -> None:
    """换成"另一种坏法" ⇒ 重新广播（状态迁移才报，不是永远哑掉）。"""
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    events.clear()
    persona_paths.persona_file.write_text("id: persona_default\nname: [\n", encoding="utf-8")
    store.current()
    persona_paths.persona_file.write_text("id: persona_default\n", encoding="utf-8")
    store.current()
    assert [item.failed for item in events] == [True, True]
    assert events[0].error != events[1].error


def test_no_failure_broadcast_without_a_good_snapshot(persona_paths: StudioPaths) -> None:
    """首次加载就失败 ⇒ 不广播（没有"仍在生效的人物"可谈，面板走 `active_error`）。"""
    persona_paths.persona_file.write_text("id: persona_default\nname: [\n", encoding="utf-8")
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    with pytest.raises(ConfigError):
        store.current()
    assert events == []


def test_change_to_dict_carries_failure_fields(persona_paths: StudioPaths) -> None:
    events: list[PersonaChange] = []
    store = PersonaStore(persona_paths, listeners=(events.append,))
    store.current()
    payload = events[0].to_dict()
    assert payload["failed"] is False
    assert payload["error"] is None
    assert payload["source"] == "active"
    assert payload["reason"] == "initial"
