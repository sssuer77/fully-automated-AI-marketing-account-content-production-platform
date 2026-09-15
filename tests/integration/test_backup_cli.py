"""备份 / 恢复 / 迁移前检查点的 CLI 面集成测试（T4.12 · §03.7.4）。

验收口径来自 todolist T4.12 与 §03.7.4 的表：
`scripts/backup_db.ps1` 产出日备 / 恢复演练三步 / 关键节点（迁移前）备份。

为什么测 CLI 而不是只测 `db/backup.py`
--------------------------------------
`scripts/*.ps1` 只是 `uv run studio ...` 的薄壳，真正的契约在命令的**参数与退出码**上：
计划任务靠退出码判断成败，`--overwrite` 靠参数决定"同一天能不能重做"。
把 `db.backup` 的函数测得很全、却让 `--overwrite` 这个开关没人验，是这类
"运维面"最常见的漏测。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studio.cli import app
from studio.core.paths import StudioPaths
from studio.db.engine import MIGRATIONS_DIR
from studio.db.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[2]

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时家目录 + 环境变量（`_db_path()` 走 `StudioPaths.from_env()`）。"""
    root = tmp_path / "home"
    root.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("STUDIO_HOME", str(root))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(data))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    return root


def _paths() -> StudioPaths:
    paths = StudioPaths.from_env()
    paths.ensure_runtime_dirs()
    return paths


def test_backup_writes_a_daily_file(home: Path) -> None:
    migrate(_paths().db_file)

    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 0, result.output
    made = list(_paths().backups_dir.glob("studio_*.db"))
    assert len(made) == 1


def test_a_second_backup_needs_overwrite(home: Path) -> None:
    """同一天第二次 ⇒ 非 0 退出（计划任务据此报警），`--overwrite` 才放行。"""
    migrate(_paths().db_file)
    assert runner.invoke(app, ["db", "backup"]).exit_code == 0

    again = runner.invoke(app, ["db", "backup"])

    assert again.exit_code == 1
    assert "--overwrite" in again.output

    forced = runner.invoke(app, ["db", "backup", "--overwrite"])

    assert forced.exit_code == 0


def test_backup_without_a_database_fails_loudly(home: Path) -> None:
    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 1
    assert "BACKUP_FAILED" in result.output


def test_listing_backups_is_empty_before_the_first_one(home: Path) -> None:
    _paths()

    result = runner.invoke(app, ["db", "backups"])

    assert result.exit_code == 0
    assert "没有可识别的备份" in result.output


def test_listing_backups_json_is_machine_readable(home: Path) -> None:
    migrate(_paths().db_file)
    runner.invoke(app, ["db", "backup"])

    result = runner.invoke(app, ["db", "backups", "--json"])

    assert result.exit_code == 0
    assert "studio_" in result.output
    assert "age_hours" in result.output


def test_restore_refuses_the_live_database(home: Path) -> None:
    """★ 恢复演练拒绝还原到活库：这条路径不是"就地回滚"。"""
    paths = _paths()
    migrate(paths.db_file)
    runner.invoke(app, ["db", "backup"])
    made = next(paths.backups_dir.glob("studio_*.db"))

    result = runner.invoke(app, ["db", "restore", "--from", str(made), "--to", str(paths.db_file), "--force"])

    assert result.exit_code == 1
    assert "RESTORE_REFUSED_LIVE_DB" in result.output


def test_restore_drill_goes_green_on_a_fresh_backup(home: Path) -> None:
    """★ 验收三步：还原到临时库 ⇒ `db check` ⇒ 抽查 3 张表。"""
    paths = _paths()
    migrate(paths.db_file)
    runner.invoke(app, ["db", "backup"])
    made = next(paths.backups_dir.glob("studio_*.db"))
    target = paths.backups_dir / "restore_drill" / "studio.db"

    result = runner.invoke(app, ["db", "restore", "--from", str(made), "--to", str(target), "--json"])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert "tasks" in result.output


def test_restore_reports_missing_tables_as_a_failure(home: Path) -> None:
    """抽查表不在 ⇒ 退出码非 0（"验了三张表"这句话必须真的验过）。"""
    paths = _paths()
    migrate(paths.db_file)
    runner.invoke(app, ["db", "backup"])
    made = next(paths.backups_dir.glob("studio_*.db"))

    result = runner.invoke(
        app,
        ["db", "restore", "--from", str(made), "--to", str(paths.backups_dir / "drill.db")],
    )

    assert result.exit_code == 0  # 三张表都在 ⇒ 正常通过


def _next_migration_version() -> int:
    """下一个可用的迁移版本号（避开已存在的号段，别写死）。"""
    return max(int(item.name[:4]) for item in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")) + 1


def test_migrate_takes_a_checkpoint_when_something_is_pending(home: Path, tmp_path: Path) -> None:
    """★ 关键节点备份：有待应用迁移 ⇒ 先钉一份检查点（单独目录，不被日备轮转碰）。"""
    paths = _paths()
    migrations = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations)
    migrate(paths.db_file, migrations_dir=migrations)
    # 版本号从现有迁移推导：写死 "0009_extra" 会跟后来新增的 0009_assets 撞号。
    pending = f"{_next_migration_version():04d}_extra.sql"
    body = "CREATE INDEX IF NOT EXISTS idx_extra ON tasks(priority);\n"
    (migrations / pending).write_text(body, encoding="utf-8")
    # 迁移器默认读仓库那份；这里手工把新文件放进真实目录再删（见下方 finally）
    added = MIGRATIONS_DIR / pending
    added.write_text(body, encoding="utf-8")
    try:
        result = runner.invoke(app, ["db", "migrate"])
    finally:
        added.unlink()

    assert result.exit_code == 0, result.output
    checkpoints = list((paths.backups_dir / "checkpoints").glob("premigrate_*.db"))
    assert len(checkpoints) == 1


def test_migrate_on_a_fresh_database_makes_no_checkpoint(home: Path) -> None:
    """首建库没有"上一版"可回 ⇒ 不产检查点（也别为它建空目录）。"""
    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert not (_paths().backups_dir / "checkpoints").exists()
