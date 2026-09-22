"""合成配置热重载仓库（T4.7 · §04.5.9）—— 读 / 改 / 并发 / 幂等 / 坏文件。

四条纪律
--------
1. **临时家目录 + 真 `outputs.yaml` 抄一份**：本用例要写盘，拿仓库根当 home 会把真配置改掉。
   抄进来的那一份就是生产那一份（含全部注释），于是"注释有没有被吃掉"也是真的在验。
2. **断言也看盘**：「校验不过一个字节都不写」这句承诺，只有比对**改动前后的字节**才算验过。
3. **并发是文件层的事**：`source_sha256` 比对发生在读盘之后、写盘之前，与进程内缓存无关。
4. **坏文件不吞掉上一份好的**：热重载失败时 `current()` 仍返回可用快照 + `last_error`；
   显式 `reload()` 才抛 —— 面板与 doctor 要的东西不一样。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from studio.core.config import OutputsConfig
from studio.core.errors import ConfigError, ErrorCode
from studio.core.outputs_store import OutputsStore, quality_field_of
from studio.core.paths import StudioPaths

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 一份**语法就是坏的** YAML（`default_profile: [` 让解析器直接炸）
BROKEN_YAML = 'schema_version: "1.0"\ndefault_profile: [\n'

DOUYIN = "douyin_1080x1920_30fps_v1"
FALLBACK = "fallback_720x1280_v1"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", root / "config" / "outputs.yaml")
    return root


@pytest.fixture
def paths(home: Path) -> StudioPaths:
    return StudioPaths(home=home, data_dir=home / "data")


@pytest.fixture
def store(paths: StudioPaths) -> OutputsStore:
    return OutputsStore(paths)


def _file(paths: StudioPaths) -> Path:
    return paths.config_dir / "outputs.yaml"


def _read(paths: StudioPaths) -> str:
    return _file(paths).read_text(encoding="utf-8")


def _write(paths: StudioPaths, text: str) -> None:
    _file(paths).write_text(text, encoding="utf-8", newline="\n")


def _lines(paths: StudioPaths) -> list[str]:
    return _read(paths).splitlines(keepends=True)


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


def test_initial_load_reads_the_real_config(store: OutputsStore, paths: StudioPaths) -> None:
    snapshot = store.current()
    assert snapshot.version == 1
    assert snapshot.path == _file(paths)
    assert snapshot.default_profile == DOUYIN
    assert len(snapshot.config.profiles) == 4
    assert len(snapshot.sha256) == 64
    assert snapshot.loaded_at.endswith("Z")
    assert store.last_error is None


def test_current_is_free_when_the_file_did_not_change(store: OutputsStore) -> None:
    """没变 ⇒ 同一份快照、版本号不动（热重载要每次读取都探一次，不能每次都 +1）。"""
    first = store.current()
    second = store.current()
    assert second is first
    assert second.version == 1


def test_hot_reload_picks_up_an_external_edit(store: OutputsStore, paths: StudioPaths) -> None:
    """人直接编辑 YAML（不经面板）⇒ 下一次 `current()` 就该看到新值。"""
    first = store.current()
    _write(paths, _read(paths).replace("  margin_x: 48", "  margin_x: 148"))
    second = store.current()
    assert second.version == first.version + 1
    assert second.config.watermark.margin_x == 148
    assert second.sha256 != first.sha256


def test_missing_file_raises_on_the_first_load(paths: StudioPaths) -> None:
    _file(paths).unlink()
    store = OutputsStore(paths)
    with pytest.raises(ConfigError) as exc:
        store.current()
    assert exc.value.code == ErrorCode.CONFIG_MISSING
    assert store.last_error is not None


def test_broken_file_keeps_the_last_good_snapshot(store: OutputsStore, paths: StudioPaths) -> None:
    """磁盘那份坏了 ⇒ 热重载返回**上一份好的** + `last_error`（不吞掉、也不假装没事）。"""
    first = store.current()
    _write(paths, BROKEN_YAML)
    second = store.current()
    assert second is first
    assert store.version == first.version
    assert store.last_error is not None
    with pytest.raises(ConfigError):
        store.reload()


# ══════════════════════════════════════════════════════════════════════
# 改
# ══════════════════════════════════════════════════════════════════════


def test_update_changes_exactly_one_line_and_keeps_the_comments(
    store: OutputsStore, paths: StudioPaths
) -> None:
    """★ 只动那一行 —— 行数不变、注释还在（整份 `safe_dump` 会毁掉这份文件的可读性）。"""
    before = _lines(paths)
    snapshot = store.update({"subtitle": {"font_size": 72}})
    after = _lines(paths)
    assert len(after) == len(before)
    changed = [index for index, line in enumerate(before) if after[index] != line]
    assert len(changed) == 1
    assert after[changed[0]].strip() == "font_size: 72"
    assert snapshot.config.subtitle.font_size == 72
    assert snapshot.version == 2
    assert "# ★ 水印是可选装饰" in _read(paths)
    assert "# 相对画布宽度；上限 0.25（T3.2 夹取）" in _read(paths)


def test_update_writes_the_validated_value_not_the_raw_one(store: OutputsStore, paths: StudioPaths) -> None:
    """写盘的是**模型上**那一份：`0.22000000001` 这种入参不该原样落进文件。"""
    store.update({"watermark": {"width_ratio": 0.22000000001}})
    assert "width_ratio: 0.22" in _read(paths)


def test_update_with_the_same_value_does_not_touch_the_file(store: OutputsStore, paths: StudioPaths) -> None:
    """幂等：值没变 ⇒ 不写盘、版本号不凭空 +1（「第 3 版与第 2 版一样」是个假信号）。"""
    before = _file(paths).read_bytes()
    first = store.current()
    again = store.update({"subtitle": {"font_size": 64}})
    assert _file(paths).read_bytes() == before
    assert again is first
    assert again.version == first.version


def test_preview_validates_without_writing(store: OutputsStore, paths: StudioPaths) -> None:
    before = _file(paths).read_bytes()
    config = store.preview({"subtitle": {"font_size": 80}})
    assert isinstance(config, OutputsConfig)
    assert config.subtitle.font_size == 80
    assert _file(paths).read_bytes() == before
    with pytest.raises(ConfigError) as exc:
        store.preview({"subtitle": {"font_size": 9999}})
    assert exc.value.code == ErrorCode.OUTPUTS_INVALID
    assert _file(paths).read_bytes() == before


def test_quality_lands_on_crf_for_libx264_and_cq_for_nvenc(store: OutputsStore, paths: StudioPaths) -> None:
    """`quality` 是**抽象字段**：面板上只有一个数字框，落盘按编码器分方言。"""
    assert quality_field_of("libx264") == "crf"
    assert quality_field_of("h264_nvenc") == "cq"

    store.update({"profiles": {DOUYIN: {"quality": 18}}})
    assert "crf: 18" in _read(paths)

    store.update({"profiles": {FALLBACK: {"quality": 30}}})
    assert "cq: 30" in _read(paths)
    assert store.current().config.profiles[FALLBACK].cq == 30


def test_a_key_missing_from_the_file_is_reported_not_silently_inserted(
    store: OutputsStore, paths: StudioPaths
) -> None:
    """文件里没有这一行 ⇒ 报错（不插一行进去）。这多半意味着配置与代码版本对不上。"""
    _write(paths, _read(paths).replace("  outline: 4\n", ""))
    store.reload()
    before = _file(paths).read_bytes()
    with pytest.raises(ConfigError) as exc:
        store.update({"subtitle": {"outline": 6}})
    assert exc.value.code == ErrorCode.OUTPUTS_INVALID
    assert exc.value.context["field_errors"][0]["field"] == "subtitle.outline"
    assert _file(paths).read_bytes() == before


# ══════════════════════════════════════════════════════════════════════
# 并发
# ══════════════════════════════════════════════════════════════════════


def test_update_with_a_stale_sha_writes_nothing(store: OutputsStore, paths: StudioPaths) -> None:
    """★ 别人先改了 ⇒ 409 `OUTPUTS_STALE`，**一个字节都不写**（不是"覆盖掉再说"）。"""
    before = _file(paths).read_bytes()
    with pytest.raises(ConfigError) as exc:
        store.update({"subtitle": {"font_size": 72}}, expected_sha256="0" * 64)
    assert exc.value.code == ErrorCode.OUTPUTS_STALE
    assert _file(paths).read_bytes() == before
    assert exc.value.context["expected_sha256"] == "0" * 64
    assert exc.value.context["actual_sha256"] == store.current().sha256


def test_update_with_the_current_sha_passes(store: OutputsStore, paths: StudioPaths) -> None:
    """指纹对得上就照常写（这条防的是"守卫写太紧 ⇒ 面板永远存不进去"）。"""
    snapshot = store.current()
    written = store.update({"subtitle": {"font_size": 72}}, expected_sha256=snapshot.sha256)
    assert written.config.subtitle.font_size == 72


def test_a_manual_edit_between_load_and_save_is_detected(store: OutputsStore, paths: StudioPaths) -> None:
    """面板打开 -> 人直接改文件 -> 面板保存：必须被拦下（而不是把人的改动吃掉）。"""
    snapshot = store.current()
    _write(paths, _read(paths).replace("  margin_y: 420", "  margin_y: 196"))
    before = _file(paths).read_bytes()
    with pytest.raises(ConfigError) as exc:
        store.update({"subtitle": {"font_size": 72}}, expected_sha256=snapshot.sha256)
    assert exc.value.code == ErrorCode.OUTPUTS_STALE
    assert _file(paths).read_bytes() == before


# ══════════════════════════════════════════════════════════════════════
# 非法改动：全都要在**写之前**被拦下
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"audio": {"voice_gain_db": 3.0}}, "audio"),
        ({"profiles": {"nope_v1": {"width": 720}}}, "profiles.nope_v1"),
        ({"profiles": {DOUYIN: {"preset": "slow"}}}, f"profiles.{DOUYIN}.preset"),
        ({"subtitle": {"font_size": 9999}}, "subtitle.font_size"),
        ({"subtitle": {"shadow": 9}}, "subtitle.shadow"),
        ({"watermark": {"margin_x": 49}}, "watermark.margin_x"),
        ({"watermark": {"width_ratio": 0.0}}, "watermark.width_ratio"),
        ({"watermark": {"width_ratio": 0.9}}, "watermark.width_ratio"),
        ({"profiles": {DOUYIN: {"width": 1081}}}, f"profiles.{DOUYIN}"),
        ({"default_profile": "nope_v1"}, "<root>"),
    ],
)
def test_invalid_changes_are_rejected_and_nothing_is_written(
    store: OutputsStore,
    paths: StudioPaths,
    changes: dict[str, object],
    field: str,
) -> None:
    before = _file(paths).read_bytes()
    with pytest.raises(ConfigError) as exc:
        store.update(changes)
    assert exc.value.code == ErrorCode.OUTPUTS_INVALID
    assert [item["field"] for item in exc.value.context["field_errors"]] == [field]
    assert _file(paths).read_bytes() == before
    assert store.current().config.subtitle.font_size == 64
