"""媒资回收的 CLI 面集成测试（T4.12 · §03.7.5）。

验收口径来自 todolist T4.12：「GC 按 TTL 清理且**不误删成片**」。

为什么测 CLI 而不是只测 ``gc`` 包
--------------------------------
``scripts/gc_media.ps1`` 只是 ``uv run studio gc run`` 的薄壳，真正的契约在
命令的**参数与退出码**上：计划任务靠退出码判断成败，``--dry-run`` 靠参数决定
"这一次到底动没动手"。把 ``gc_media`` 测得很全、却让 ``--dry-run`` 这个开关
没人验，是运维面最常见的漏测。

另外两条只在这里能验的东西：

1. **配置 → 策略**的接线（``config/app.yaml → retention`` 真的被读到了）；
2. **读不到配置 / 没有库**时的降级行为（GC 是无人值守跑的，不该因此停摆）。
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studio.cli import app
from studio.core.clock import format_iso
from studio.core.paths import StudioPaths
from studio.db.engine import connect
from studio.db.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("STUDIO_HOME", str(root))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    return root


def _paths() -> StudioPaths:
    paths = StudioPaths.from_env()
    paths.ensure_runtime_dirs()
    return paths


def _with_config() -> None:
    paths = _paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "config" / "app.yaml", paths.config_dir / "app.yaml")


def _seed_log(paths: StudioPaths, *, days: float) -> None:
    connection = connect(paths.db_file)
    try:
        moment = format_iso(NOW - timedelta(days=days))
        connection.execute(
            "INSERT INTO system_logs(level, source, message, ts, created_at) VALUES ('info','test','x',?,?)",
            (moment, moment),
        )
    finally:
        connection.close()


def _log_rows(paths: StudioPaths) -> int:
    connection = connect(paths.db_file, read_only=True)
    try:
        return int(connection.execute("SELECT count(*) FROM system_logs").fetchone()[0])
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════
# 参数与退出码
# ══════════════════════════════════════════════════════════════════════


def test_dry_run_reports_without_touching_anything(home: Path) -> None:
    """★ ``--dry-run`` 是"上生产前先看一眼"的那个开关，必须真的不动手。"""
    paths = _paths()
    migrate(paths.db_file)
    _seed_log(paths, days=40)

    result = runner.invoke(app, ["gc", "run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "演练" in result.output
    assert _log_rows(paths) == 1


def test_run_actually_deletes_expired_rows(home: Path) -> None:
    paths = _paths()
    migrate(paths.db_file)
    _seed_log(paths, days=40)
    _seed_log(paths, days=1)

    result = runner.invoke(app, ["gc", "run"])

    assert result.exit_code == 0, result.output
    assert _log_rows(paths) == 1


def test_rows_only_and_media_only_are_mutually_exclusive(home: Path) -> None:
    _paths()

    result = runner.invoke(app, ["gc", "run", "--rows-only", "--media-only"])

    assert result.exit_code == 1
    assert "不能同时给" in result.output


def test_rows_only_leaves_files_alone(home: Path) -> None:
    paths = _paths()
    migrate(paths.db_file)
    stale = paths.scenes_dir_for("t1") / "s001.mp4"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")

    result = runner.invoke(app, ["gc", "run", "--rows-only", "--json"])

    assert result.exit_code == 0, result.output
    assert stale.is_file()
    assert '"media": null' in result.output.replace("'", '"')


def test_media_only_leaves_rows_alone(home: Path) -> None:
    paths = _paths()
    migrate(paths.db_file)
    _seed_log(paths, days=40)

    result = runner.invoke(app, ["gc", "run", "--media-only"])

    assert result.exit_code == 0, result.output
    assert _log_rows(paths) == 1


# ══════════════════════════════════════════════════════════════════════
# 降级行为（无人值守 ⇒ 不许因此停摆）
# ══════════════════════════════════════════════════════════════════════


def test_a_missing_database_is_reported_and_does_not_create_one(home: Path) -> None:
    """★ 没有库 ⇒ 跳过 DB 部分并如实记一条 note；**不许**顺手建出 0 字节的库。"""
    paths = _paths()

    result = runner.invoke(app, ["gc", "run", "--json"])

    assert result.exit_code == 0, result.output
    assert not paths.db_file.exists()
    assert "数据库不存在" in result.output


def test_a_missing_config_falls_back_to_the_spec_defaults(home: Path) -> None:
    """★ 读不到 ``config/app.yaml`` ⇒ 用 §03.7.5 的默认值 + **把降级说出来**。"""
    paths = _paths()
    migrate(paths.db_file)

    result = runner.invoke(app, ["gc", "run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "默认值执行" in result.output


def test_retention_comes_from_app_yaml(home: Path) -> None:
    """★ 配置 → 策略的接线：把 30 天改成 1 天，5 天前的日志就该被清掉。"""
    _with_config()
    paths = _paths()
    migrate(paths.db_file)
    config = paths.config_dir / "app.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("system_logs_days: 30", "system_logs_days: 1"),
        encoding="utf-8",
    )
    _seed_log(paths, days=5)

    result = runner.invoke(app, ["gc", "run"])

    assert result.exit_code == 0, result.output
    assert _log_rows(paths) == 0


# ══════════════════════════════════════════════════════════════════════
# 端到端：文件
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_hot_item_is_moved_not_deleted(home: Path) -> None:
    """★ §03.7.5「移动而非删除」：热点进归档区，一个字节都不丢。"""
    paths = _paths()
    stale = paths.hot_dir / "hot_20260101_x.md"
    stale.write_text("旧的", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(stale, (old, old))

    result = runner.invoke(app, ["gc", "run", "--media-only"])

    assert result.exit_code == 0, result.output
    assert not stale.exists()
    archived = list(paths.hot_archive_dir.rglob("hot_20260101_x.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "旧的"


def test_a_final_video_survives_a_full_run(home: Path) -> None:
    """★ 端到端验收："GC 按 TTL 清理且**不误删成片**"。"""
    paths = _paths()
    migrate(paths.db_file)
    final = paths.videos_dir / "20260901-101500_t1_final.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"MP4")
    stale = paths.scenes_dir_for("t1") / "s001.mp4"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")
    old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(stale, (old, old))

    result = runner.invoke(app, ["gc", "run"])

    assert result.exit_code == 0, result.output
    assert final.is_file()
    assert not stale.exists()


def test_json_output_is_machine_readable(home: Path) -> None:
    paths = _paths()
    migrate(paths.db_file)

    result = runner.invoke(app, ["gc", "run", "--json"])

    assert result.exit_code == 0, result.output
    for key in ('"rows"', '"media"', '"policy"', '"ok"', '"dry_run"', '"db_freelist_bytes"'):
        assert key in result.output
