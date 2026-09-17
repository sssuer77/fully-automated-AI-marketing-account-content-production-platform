"""``studio`` 命令行入口（T1.1 起）。

约定
----
- CLI 是**运维与自检**入口；业务流程一律走 WebUI（P4：除确认闸外零人工）。
- 退出码：``0`` 成功 / ``1`` 业务失败（含自检门禁） / ``2`` 用法错误（Typer 默认）。
- 机读输出统一 ``--json``，字段即 WS 事件载荷，前端与 CI 共用同一份契约。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from studio import __spec_version__, __version__
from studio.agents.budget import TokenBudget
from studio.agents.cost import LlmCallStore
from studio.agents.cover import CoverAgent
from studio.agents.director import DirectorAgent
from studio.agents.feedback_classifier import FeedbackClassifierAgent
from studio.agents.gateway import LogSink
from studio.agents.gateway_factory import build_gateway
from studio.agents.ideator import IdeatorAgent
from studio.agents.planner import PlannerAgent
from studio.agents.prompts import PromptLibrary
from studio.agents.writer import WriterAgent
from studio.app.main import run_server
from studio.core.clock import utc_now
from studio.core.config import (
    CONFIG_FILE_NAMES,
    LlmProfileConfig,
    LoadedConfig,
    PersonaConfig,
    load_app_config,
    load_config,
    load_outputs_config,
    load_publish_config,
)
from studio.core.doctor import Doctor, dumps, render_text
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.core.persona_store import PersonaStore, get_persona_store
from studio.core.proto import Severity
from studio.core.settings import inspect_env_contract
from studio.db import TopicRepo, connect, discover, doctor_check, footprint, read_applied
from studio.db import check as db_check
from studio.db import migrate as db_migrate
from studio.db import plan as plan_migrations
from studio.db.backup import (
    DAILY_KEEP,
    WEEKLY_KEEP,
    backup_database,
    list_backups,
    restore_backup,
    snapshot_age_hours,
)
from studio.domain.enums import TaskStatus
from studio.domain.task_service import TaskService
from studio.gc import GcReport, RetentionPolicy, resolve_policy, run_gc
from studio.pools import HeartbeatStore, run_pool
from studio.publish.precheck import PrecheckReport
from studio.render.profiles import (
    FALLBACK_PROFILE_NAME,
    RenderProfileReport,
    build_render_profile_report,
)
from studio.services import (
    DraftReport,
    LogService,
    ScriptService,
    StartReport,
    StopReport,
    TopicService,
    default_manager,
    read_active_script,
)
from studio.services.pipeline_service import SUPPORTED_UNTIL, PipelineReport, run_task
from studio.services.publish_service import (
    CoverReport,
    CoverRequest,
    DryRunReport,
    DryRunRequest,
    PublishService,
    build_board,
    cancel_publication,
    enqueue_publications,
    mark_manual_done,
    retry_publication,
)
from studio.services.render_service import (
    ProduceRequest,
    ProduceResult,
    produce_video,
)
from studio.services.topic_service import TOPICS_PER_DIRECTION

config_app = typer.Typer(name="config", help="配置系统：校验与导出（T1.2）", no_args_is_help=True)
persona_app = typer.Typer(
    name="persona",
    help="人物（频道定位）：热重载 + 人物库 + 一键切换",
    no_args_is_help=True,
)
db_app = typer.Typer(name="db", help="数据库：迁移与自检（T1.3）", no_args_is_help=True)
pool_app = typer.Typer(name="pool", help="四池 worker：启动 / 心跳 / 判死（T1.6）", no_args_is_help=True)
llm_app = typer.Typer(name="llm", help="LLM 双通道：通道探测 / 预算查询（T1.8）", no_args_is_help=True)
prompts_app = typer.Typer(name="prompts", help="提示词注册表：校验 / 列表（T1.8）", no_args_is_help=True)
topics_app = typer.Typer(
    name="topics",
    help="选题池：输入源解析 + Planner 方向 + Ideator 选题（T1.9）",
    no_args_is_help=True,
)
script_app = typer.Typer(
    name="script",
    help="写稿：Director 大纲 + Writer 成稿 + 逐句落库（T1.10）",
    no_args_is_help=True,
)
service_app = typer.Typer(
    name="service",
    help="五进程编排：一键启动 / 优雅关停 / 查看（T1.12）",
    no_args_is_help=True,
)
gc_app = typer.Typer(
    name="gc",
    help="媒资回收：DB 行 + 句子音频 + 缓存 + 热点归档（T4.12）",
    no_args_is_help=True,
)
render_app = typer.Typer(
    name="render",
    help="渲染：合成 profile 与单遍合成（T3.x）",
    no_args_is_help=True,
)
pipeline_app = typer.Typer(
    name="pipeline",
    help="流水线：把一个任务从当前状态推到 --until（T3.7）",
    no_args_is_help=True,
)
publish_app = typer.Typer(
    name="publish",
    help=(
        "发布链路：封面合成 + 二次校验 + 演练（T5.1 / T5.2）+ "
        "投递 / 看板 / 人工处置（T5.3；真发布由 publish 池 worker 执行）"
    ),
    no_args_is_help=True,
)

app = typer.Typer(
    name="studio",
    help="AI 全自动营销号制片台 · 本地部署运维入口",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
console = Console()

app.add_typer(config_app, name="config")
app.add_typer(persona_app, name="persona")
app.add_typer(db_app, name="db")
app.add_typer(pool_app, name="pool")
app.add_typer(llm_app, name="llm")
app.add_typer(prompts_app, name="prompts")
app.add_typer(topics_app, name="topics")
app.add_typer(script_app, name="script")
app.add_typer(service_app, name="service")
app.add_typer(gc_app, name="gc")
app.add_typer(render_app, name="render")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(publish_app, name="publish")


@app.callback(invoke_without_command=True)
def _root(
    version: Annotated[bool, typer.Option("--version", "-V", help="打印版本后退出")] = False,
) -> None:
    """AI 全自动营销号制片台。"""
    if version:
        console.print(f"studio {__version__} (spec {__spec_version__})")
        raise typer.Exit


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON（CI / WS 同构）")] = False,
    skip_heavy: Annotated[
        bool, typer.Option("--skip-heavy", help="跳过 torch 导入与 NVENC 实编码探测")
    ] = False,
    no_gate: Annotated[bool, typer.Option("--no-gate", help="仅诊断：即使有阻塞项也返回 0")] = False,
) -> None:
    """启动自检：FFmpeg / 滤镜 / NVENC / 字体 / 磁盘 / DB / 环境变量逐项断言。"""
    paths = StudioPaths.from_env()
    # db 层检查由上层注入：core 不得反向依赖 db（分层 core -> db -> domain）
    report = Doctor(paths, skip_heavy=skip_heavy).run().with_extra_checks([doctor_check(paths)])

    if json_output:
        console.print_json(dumps(report))
    else:
        console.print(render_text(report))

    if not report.ok and not no_gate:
        raise typer.Exit(code=1)


def _persona_store() -> PersonaStore:
    return get_persona_store()


@persona_app.command("show")
def persona_show(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """打印**当前激活**人物（含 sha256 / version / 是否热重载过）。"""
    store = _persona_store()
    snapshot = store.current()
    if json_output:
        console.print_json(
            data={
                **snapshot.to_dict(),
                "config": snapshot.config.model_dump(mode="json"),
                "last_error": store.last_error.to_dict() if store.last_error else None,
            }
        )
        return

    persona = snapshot.config
    table = Table(title=f"当前人物 · {persona.name}", show_lines=False)
    table.add_column("字段", style="cyan", no_wrap=True)
    table.add_column("取值", overflow="fold")
    table.add_row("id", persona.id)
    table.add_row("来源", f"{snapshot.path}（v{snapshot.version}，{snapshot.sha256[:12]}）")
    table.add_row("加载于", snapshot.loaded_at)
    table.add_row("人设", persona.role_desc)
    table.add_row("口吻", persona.tone)
    table.add_row("受众", persona.audience)
    table.add_row("口癖", "、".join(persona.catchphrases))
    table.add_row("禁区", f"{len(persona.forbidden)} 条：" + "、".join(persona.forbidden[:8]) + " …")
    table.add_row("篇幅", f"{persona.target_chars_min}-{persona.target_chars_max} 字")
    table.add_row("时长上限", f"{persona.max_duration_ms / 1000:.0f}s")
    console.print(table)
    if store.last_error:
        console.print(f"[red]注意：人物文件当前有错，正在沿用上一份快照[/red] — {store.last_error}")
        if store.last_error.remediation:
            console.print(f"[dim]修复：{store.last_error.remediation}[/dim]")


@persona_app.command("list")
def persona_list(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """列出当前激活人物 + 人物库中的全部备选。"""
    store = _persona_store()
    snapshot = store.current()
    entries = store.library()
    if json_output:
        console.print_json(
            data={
                "active": snapshot.to_dict(),
                "library": [entry.to_dict() for entry in entries],
                "last_error": store.last_error.to_dict() if store.last_error else None,
            }
        )
        return

    table = Table(title="人物库", show_lines=False)
    table.add_column("", width=2)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("名称", overflow="fold")
    table.add_column("口吻", overflow="fold")
    table.add_column("状态")
    for entry in entries:
        mark = "★" if entry.persona_id == snapshot.persona_id else ""
        status = "[green]可用[/green]" if entry.valid else f"[red]无效：{entry.error}[/red]"
        table.add_row(mark, entry.persona_id, entry.name or "-", entry.tone or "-", status)
    console.print(table)
    console.print(
        f"当前激活：[bold]{snapshot.persona_id}[/bold]（{snapshot.config.name}，v{snapshot.version}）"
    )
    console.print("[dim]切换：studio persona use <id>；存一份：studio persona save-as <id>[/dim]")


@persona_app.command("use")
def persona_use(
    persona_id: Annotated[str, typer.Argument(help="人物库中的 id（文件名去掉 .yaml）")],
    no_backup: Annotated[bool, typer.Option("--no-backup", help="不备份旧版（不推荐）")] = False,
) -> None:
    """一键切换人物（覆盖 config/persona.yaml，旧版自动备份）。"""
    store = _persona_store()
    try:
        snapshot = store.activate(persona_id, backup=not no_backup)
    except StudioError as exc:
        console.print(f"[red]{exc}[/red]")
        if exc.remediation:
            console.print(f"[dim]修复：{exc.remediation}[/dim]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已切换人物[/green] → {snapshot.config.name}（id={snapshot.persona_id}）")
    console.print(f"[dim]备份目录：{store.backup_dir}[/dim]")
    console.print("[dim]5 个进程会在各自下一次取人物时自动生效，无需重启[/dim]")


@persona_app.command("save-as")
def persona_save_as(
    persona_id: Annotated[str, typer.Argument(help="存入人物库的 id")],
    overwrite: Annotated[bool, typer.Option("--overwrite", help="覆盖同名条目")] = False,
) -> None:
    """把当前激活人物存进人物库（改完存一份，便于随时切回）。"""
    store = _persona_store()
    try:
        target = store.save_as(persona_id, overwrite=overwrite)
    except StudioError as exc:
        console.print(f"[red]{exc}[/red]")
        if exc.remediation:
            console.print(f"[dim]修复：{exc.remediation}[/dim]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]已存入人物库[/green]：{target}")


@persona_app.command("validate")
def persona_validate(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """校验激活人物与人物库全部条目（热重载前的"体检"）。"""
    store = _persona_store()
    try:
        snapshot = store.reload(force=True)
    except StudioError as exc:
        if json_output:
            console.print_json(data={"ok": False, "active": exc.to_dict()})
        else:
            console.print(f"[red]激活人物校验失败[/red]：{exc}")
            if exc.remediation:
                console.print(f"[dim]修复：{exc.remediation}[/dim]")
        raise typer.Exit(code=1) from exc

    entries = store.library()
    broken = [entry for entry in entries if not entry.valid]
    if json_output:
        console.print_json(
            data={
                "ok": not broken,
                "active": snapshot.to_dict(),
                "library": [entry.to_dict() for entry in entries],
                "broken": [entry.persona_id for entry in broken],
            }
        )
    else:
        console.print(f"[green]激活人物 OK[/green]：{snapshot.config.name}（v{snapshot.version}）")
        console.print(f"人物库：{len(entries)} 条，其中无效 {len(broken)} 条")
        for entry in broken:
            console.print(f"[yellow]  ✗ {entry.persona_id}[/yellow]：{entry.error}")
    if broken:
        raise typer.Exit(code=1)


@config_app.command("dump")
def config_dump(
    json_output: Annotated[
        bool, typer.Option("--json", help="输出机读 JSON（与 config/*.yaml 一致）")
    ] = False,
    section: Annotated[
        str | None, typer.Option("--section", "-s", help="只导出某一节（persona/llm/app/...）")
    ] = None,
    show_secrets: Annotated[
        bool, typer.Option("--show-secrets", help="★ 不脱敏（仅本机排障；禁止重定向到文件）")
    ] = False,
) -> None:
    """导出**解析后**的完整配置（含 env 覆盖结果与来源留痕）。"""
    loaded = load_config()
    payload = loaded.dump(redact_secrets=not show_secrets)
    if section:
        if section not in payload:
            console.print(f"[red]未知配置节：{section}[/red]")
            console.print(f"[dim]可选：{', '.join([*CONFIG_FILE_NAMES, '_meta'])}[/dim]")
            raise typer.Exit(code=2)
        payload = {section: payload[section], "_meta": payload["_meta"]}
    if json_output:
        console.print_json(data=payload)
    else:
        console.print(_render_config_text(loaded))
        console.print("[dim]提示：加 --json 可导出完整结构[/dim]")


@config_app.command("validate")
def config_validate(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """强校验全部配置（缺字段 / 非法值 / 未知键 / 越界路径 / 局域网无密码）。"""
    try:
        loaded = load_config()
    except StudioError as exc:
        if json_output:
            console.print_json(data={"ok": False, **exc.to_dict()})
        else:
            console.print(f"[red]{exc}[/red]")
            if exc.remediation:
                console.print(f"[dim]修复：{exc.remediation}[/dim]")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(
            data={
                "ok": True,
                "sources": [source.to_dict() for source in loaded.sources],
                "warnings": list(loaded.warnings),
            }
        )
    else:
        console.print(f"[green]配置校验通过[/green]（{len(loaded.sources)} 份文件）")
        console.print(_render_config_text(loaded))
        for note in loaded.warnings:
            console.print(f"[yellow]warn[/yellow] {note}")


def _db_path() -> Path:
    return StudioPaths.from_env().db_file


@db_app.command("migrate")
def db_migrate_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只打印计划，不写库")] = False,
    no_backup: Annotated[
        bool, typer.Option("--no-backup", help="跳过迁移前检查点（默认会先钉一份）")
    ] = False,
) -> None:
    """把数据库前滚到最新版本（幂等；历史文件被改 ⇒ 拒绝执行）。

    有**待应用**的迁移时先产一份检查点到 `data/backups/checkpoints/`：迁移是唯一
    会改 schema 的操作，也是唯一"失败后只能靠备份"的操作（§03.7.2 规则 4）。
    """
    checkpoint: Path | None = None
    if not dry_run and not no_backup:
        # 迁移是**唯一**会改 schema 的操作（§03.7.2 规则 4：可前滚不可回滚），
        # 也是唯一"失败了就只能靠备份"的操作。所以先钉一份检查点再动手。
        checkpoint = _pre_migration_checkpoint()
    try:
        report = db_migrate(_db_path(), dry_run=dry_run)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        table = Table(title="数据库迁移", show_lines=False)
        table.add_column("项", style="cyan", no_wrap=True)
        table.add_column("值", overflow="fold")
        table.add_row("数据库", str(report.db_path))
        table.add_row("已应用", f"{len(report.plan.applied)} 个")
        table.add_row("本次执行", "、".join(report.executed) or "（无，已是最新）")
        table.add_row("待应用", f"{len(report.plan.pending)} 个")
        table.add_row("声明表数", str(report.table_count))
        if checkpoint is not None:
            table.add_row("迁移前检查点", str(checkpoint))
        table.add_row("耗时", f"{report.duration_ms} ms")
        console.print(table)
    if report.plan.pending:
        console.print("[yellow]仍有待应用迁移，请重跑（上一次可能中断）[/yellow]")
        raise typer.Exit(code=1)


@db_app.command("check")
def db_check_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
    no_gate: Annotated[bool, typer.Option("--no-gate", help="仅诊断：有失败也返回 0")] = False,
) -> None:
    """库自检：WAL / 外键 / integrity / 对象清单 / 迁移状态（§03.7.2 规则 6）。"""
    path = _db_path()
    if not path.is_file():
        message = StudioError(
            f"数据库不存在：{path}",
            code=ErrorCode.DB_NOT_WRITABLE,
            context={"db_path": str(path)},
            remediation="先跑 `studio db migrate` 初始化",
        )
        _fail(message, json_output)
        raise typer.Exit(code=1)

    try:
        report = db_check(path)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        table = Table(title="数据库自检", show_lines=False)
        table.add_column("检查项", style="cyan", no_wrap=True)
        table.add_column("状态")
        table.add_column("明细", overflow="fold")
        palette = {"ok": "green", "warn": "yellow", "fail": "red"}
        for item in report.items:
            table.add_row(item.name, f"[{palette.get(item.status, 'white')}]{item.status}", item.detail)
        console.print(table)
        console.print(f"[dim]{report.db_path}[/dim]")

    if not report.ok and not no_gate:
        raise typer.Exit(code=1)


@db_app.command("status")
def db_status(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """列出迁移版本与校验和（磁盘文件 vs 库内登记）。"""
    path = _db_path()
    files = discover()
    if not path.is_file():
        payload = {
            "db_path": str(path),
            "initialized": False,
            "files": [f.to_dict() for f in files],
            "applied": [],
        }
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(f"[yellow]数据库尚未初始化[/yellow]：{path}")
            console.print(f"[dim]跑 `studio db migrate` 初始化（共 {len(files)} 个迁移）[/dim]")
        return

    connection = connect(path, read_only=True)
    try:
        applied = {a.version: a for a in read_applied(connection)}
    finally:
        connection.close()

    if json_output:
        console.print_json(
            data={
                "db_path": str(path),
                "initialized": True,
                "files": [f.to_dict() for f in files],
                "applied": [a.to_dict() for a in applied.values()],
            }
        )
        return

    table = Table(title="迁移状态", show_lines=False)
    table.add_column("版本", style="cyan", no_wrap=True)
    table.add_column("名称")
    table.add_column("状态")
    table.add_column("sha256", overflow="fold")
    table.add_column("应用时间", overflow="fold")
    for migration in files:
        done = applied.get(migration.version)
        if done is None:
            table.add_row(migration.version, migration.name, "[yellow]待应用", "-", "-")
            continue
        drift = "" if done.checksum == migration.checksum else " [red]（已改动！）"
        table.add_row(
            migration.version,
            migration.name,
            f"[green]已应用{drift}",
            migration.checksum[:12],
            done.applied_at,
        )
    console.print(table)
    console.print(f"[dim]{path}[/dim]")


# ── 备份与恢复（T4.12 · §03.7.4）─────────────────────────────────────


def _checkpoints_dir() -> Path:
    """迁移前检查点**单独一个目录**（§03.7.4「关键节点备份」）。

    为什么不和日备放一起：日备会被轮转（7 日 + 4 周），而"迁移前那一刻的库"是
    出事时唯一能回去的点 —— 让它被一次例行轮转顺手删掉，等于没备份。
    分开之后两条保留策略各管各的，互不干扰。
    """
    return StudioPaths.from_env().backups_dir / "checkpoints"


def _pre_migration_checkpoint() -> Path | None:
    """有待应用迁移 ⇒ 先钉一份检查点。**失败不阻断迁移**（只记警告）。

    为什么容错：检查点写不出来（磁盘满 / 目录只读）时，把它变成"迁移也跑不了"，
    是把一个可恢复的小问题升级成停机。真正该拦住迁移的是 `doctor` 的门禁。
    """
    path = _db_path()
    if not path.is_file():
        # 首建库：没有"之前那一版"可回，检查点没有意义（而且此时库还没建）。
        return None
    connection = connect(path, apply=False)
    try:
        pending = plan_migrations(connection).pending
    except sqlite3.Error:
        # 连 `schema_migrations` 都读不出来 ⇒ 库本身有更大的问题，交给 migrate 报。
        return None
    finally:
        connection.close()
    if not pending:
        return None
    try:
        result = backup_database(
            _db_path(),
            dest_dir=_checkpoints_dir(),
            overwrite=True,
            daily_keep=0,
            weekly_keep=0,
            name=f"premigrate_{utc_now().astimezone().strftime('%Y%m%d-%H%M%S')}.db",
        )
    except (StudioError, OSError) as exc:
        console.print(f"[yellow]迁移前检查点未产出（继续迁移）：{exc}[/yellow]")
        return None
    console.print(f"[dim]迁移前检查点：{result.path}[/dim]")
    return result.path


@db_app.command("backup")
def db_backup_cmd(
    dest: Annotated[Path | None, typer.Option("--dest", help="备份目录（默认 data/backups）")] = None,
    keep_daily: Annotated[int, typer.Option("--keep-daily", help="保留几份日备")] = DAILY_KEEP,
    keep_weekly: Annotated[int, typer.Option("--keep-weekly", help="保留几份周备（周日）")] = WEEKLY_KEEP,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="今天的备份已存在时覆盖它")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """热备一份库到 `data/backups/studio_YYYYMMDD.db` 并轮转旧备份（§03.7.4）。

    WAL 下**不阻塞写**（`VACUUM INTO` 读的是事务视图），所以日备可以直接对着
    正在跑的库执行 —— 不需要停进程。
    """
    target_dir = dest if dest is not None else StudioPaths.from_env().backups_dir
    try:
        result = backup_database(
            _db_path(),
            dest_dir=target_dir,
            daily_keep=keep_daily,
            weekly_keep=keep_weekly,
            overwrite=overwrite,
        )
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=result.to_dict())
        return

    table = Table(title="数据库备份", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("值", overflow="fold")
    table.add_row("产出", str(result.path))
    table.add_row("大小", f"{result.size_bytes / 1024 / 1024:.2f} MB")
    table.add_row("耗时", f"{result.duration_ms} ms")
    table.add_row("轮转删除", "、".join(p.name for p in result.pruned) or "（无）")
    table.add_row("当前保留", f"{len(result.kept)} 份 · {result.kept_bytes / 1024 / 1024:.2f} MB")
    console.print(table)


@db_app.command("backups")
def db_backups_cmd(
    dest: Annotated[Path | None, typer.Option("--dest", help="备份目录（默认 data/backups）")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """列出备份与新鲜度（"磁盘上有备份"与"备份是新的"是两件事）。"""
    target_dir = dest if dest is not None else StudioPaths.from_env().backups_dir
    entries = list_backups(target_dir)
    age = snapshot_age_hours(entries)
    if json_output:
        console.print_json(
            data={
                "dir": str(target_dir),
                "age_hours": age,
                "total_bytes": sum(item.size_bytes for item in entries),
                "entries": [item.to_dict() for item in entries],
            }
        )
        return
    if not entries:
        console.print(f"[yellow]备份目录里没有可识别的备份[/yellow]：{target_dir}")
        console.print("[dim]跑 `studio db backup` 产出一份[/dim]")
        return
    table = Table(title="数据库备份", show_lines=False)
    table.add_column("日期", style="cyan", no_wrap=True)
    table.add_column("周备")
    table.add_column("大小", justify="right")
    table.add_column("文件", overflow="fold")
    for item in entries:
        table.add_row(
            item.label,
            "[green]是" if item.weekly else "",
            f"{item.size_bytes / 1024 / 1024:.2f} MB",
            str(item.path),
        )
    console.print(table)
    console.print(f"[dim]最新一份距今 {age:.1f} 小时 · 共 {len(entries)} 份[/dim]")


@db_app.command("restore")
def db_restore_cmd(
    backup: Annotated[Path, typer.Option("--from", help="备份文件")],
    target: Annotated[Path, typer.Option("--to", help="还原到哪个库（**必须不是活库**）")],
    force: Annotated[bool, typer.Option("--force", help="目标已存在时覆盖它")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """恢复演练：还原到临时库 ⇒ `db check` ⇒ 抽查 3 张表行数（§03.7.4）。

    **拒绝还原到活库**：这条路径是"验证备份可用"，不是"就地回滚"。真要回滚，
    先停进程再手工替换 `studio.db`（`docs/runbook/restore_drill.md` 有步骤）。
    """
    try:
        report = restore_backup(
            backup,
            target=target,
            live_db=_db_path(),
            overwrite=force,
        )
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        table = Table(title="恢复演练", show_lines=False)
        table.add_column("项", style="cyan", no_wrap=True)
        table.add_column("值", overflow="fold")
        table.add_row("备份", str(report.backup_path))
        table.add_row("还原到", str(report.target))
        table.add_row(
            "行数抽查",
            "、".join(f"{item.name}={item.rows}" for item in report.table_counts) or "（无）",
        )
        if report.skipped_tables:
            table.add_row("缺失表", "、".join(report.skipped_tables))
        checks = report.check_report
        table.add_row(
            "db check",
            "[green]ok" if checks is not None and checks.ok else "[yellow]有失败项",
        )
        if report.baseline_checked:
            # 与活库基线比对：活库自己欠迁移不算"备份坏了"（见 RestoreReport）。
            table.add_row(
                "活库基线失败项",
                "、".join(report.baseline_failures) or "（无，活库全绿）",
            )
        if report.schema_lag:
            table.add_row(
                "备份版本落后",
                "、".join(report.schema_lag) + "（备份比活库旧；回滚时补跑一次 `studio db migrate`）",
            )
        table.add_row(
            "备份自己的问题",
            "、".join(report.extra_failures) or "[green]（无）",
        )
        table.add_row("耗时", f"{report.duration_ms} ms")
        console.print(table)
        if not report.ok:
            console.print(
                "[red]演练未通过[/red]：备份**不可用**，或抽查表缺失。"
                "先换更早的一份重试，确认是「这一份坏了」还是「备份一直没产出」。"
            )

    if not report.ok:
        raise typer.Exit(code=1)


@db_app.command("vacuum")
def db_vacuum_cmd(
    yes: Annotated[bool, typer.Option("--yes", help="确认执行（需要约 2 倍库体积的空闲空间）")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """回收删行留下的空洞（`VACUUM`）——**先备份、再确认、且不能有进程在写**。

    为什么默认拒绝：`DELETE` 不会让库文件变小，而 `VACUUM` 是**重写整个文件** ——
    中途没空间就是库损坏，且它只能在没有任何进程写库时跑。所以这里要显式 `--yes`，
    并在此之前**自己钉一份检查点**（`db/backup.py` 的 `VACUUM INTO` 正好是同一套机制）。

    为什么这次检查点失败就**中止**（而迁移前检查点失败只警告）：迁移是增量的、
    出问题还能往前修；`VACUUM` 是把库整个重写一遍，手里没有可用备份就不该开始。
    """
    path = _db_path()
    if not path.is_file():
        console.print(f"[red]数据库不存在：{path}[/red]")
        raise typer.Exit(code=1)

    before = _db_footprint(path)
    payload = {
        "db": str(path),
        "size_bytes": before[0],
        "freelist_bytes": before[1],
        "vacuumed": False,
    }
    if before[1] == 0:
        if json_output:
            console.print_json(data=payload)
        else:
            console.print("[green]没有可回收的空洞[/green]（`DELETE` 删掉的行都已经还给盘了）")
        return

    if not yes:
        payload["hint"] = "加 --yes 执行"
        if json_output:
            console.print_json(data=payload)
        else:
            console.print(
                f"可回收 [cyan]{before[1] / 1024 / 1024:.2f} MB[/cyan]（库 {before[0] / 1024 / 1024:.2f} MB）"
            )
            console.print(
                "[yellow]加 --yes 执行[/yellow]；执行前请确认没有任务在跑（见 docs/runbook/disk_full.md）"
            )
        raise typer.Exit(code=1)

    live = sorted(StudioPaths.from_env().logs_dir.glob("*.pid"))
    if live:
        console.print(f"[yellow]注意：仍有 PID 台账存在 —— {'、'.join(item.name for item in live)}[/yellow]")

    try:
        checkpoint = backup_database(
            path,
            dest_dir=_checkpoints_dir(),
            overwrite=True,
            daily_keep=0,
            weekly_keep=0,
            name=f"prevacuum_{utc_now().astimezone().strftime('%Y%m%d-%H%M%S')}.db",
        )
    except (StudioError, OSError) as exc:
        console.print(f"[red]检查点未产出，中止 VACUUM：{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[dim]VACUUM 前检查点：{checkpoint.path}[/dim]")

    connection = connect(path)
    try:
        connection.execute("VACUUM")
    except sqlite3.Error as exc:
        console.print(f"[red]VACUUM 失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    after = _db_footprint(path)
    payload.update(
        {
            "vacuumed": True,
            "checkpoint": str(checkpoint.path),
            "size_after_bytes": after[0],
            "reclaimed_bytes": before[0] - after[0],
        }
    )
    if json_output:
        console.print_json(data=payload)
        return
    table = Table(title="VACUUM", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("值", overflow="fold")
    table.add_row("库", str(path))
    table.add_row("体积", f"{before[0] / 1024 / 1024:.2f} MB → {after[0] / 1024 / 1024:.2f} MB")
    table.add_row("已还盘", f"{(before[0] - after[0]) / 1024 / 1024:.2f} MB")
    table.add_row("检查点", str(checkpoint.path))
    console.print(table)


def _db_footprint(path: Path) -> tuple[int, int]:
    """``(库文件字节, 空洞字节)`` —— 与 ``studio gc run`` / 观测面板同一套算法。"""
    connection = connect(path)
    try:
        return footprint(connection)
    finally:
        connection.close()


# ── 媒资回收（T4.12 · §03.7.5）─────────────────────────────────────


def _gc_policy() -> tuple[RetentionPolicy, str | None]:
    """读 `config/app.yaml → retention`（容错与降级说明都在 `gc.resolve_policy`）。"""
    return resolve_policy(StudioPaths.from_env())


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.2f} MB"


def _gb(size: int) -> str:
    """TTS 缓存的量级是 GB（上限默认 5.0），用 MB 读起来是"5120.00"。"""
    return f"{size / 1024**3:.2f} GB"


def _render_gc(report: GcReport) -> None:
    table = Table(title="媒资回收（§03.7.5）", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("值", overflow="fold")
    table.add_row("模式", "[yellow]演练（一个字节都没动）[/yellow]" if report.dry_run else "实际回收")
    for note in report.notes:
        table.add_row("注意", f"[yellow]{note}[/yellow]")
    for item in report.rows:
        table.add_row(f"DB · {item.label}", f"{item.deleted} 行（保留 {item.days} 天）")
    media = report.media
    if media is not None:
        table.add_row("删除文件", f"{len(media.deleted)} 个 · {_mb(media.freed_bytes)}")
        table.add_row("移动文件", f"{len(media.moved)} 个 · {_mb(media.moved_bytes)}（热点归档）")
        table.add_row(
            "TTS 缓存",
            f"{_gb(media.tts_cache_bytes)} / {_gb(media.tts_cache_limit_bytes)}（LRU 上限）",
        )
        table.add_row("热点归档区", _mb(media.hot_archive_bytes))
        table.add_row("tmp（不动）", _mb(media.tmp_bytes))
    table.add_row("库体积", f"{_mb(report.db_bytes)} · 空洞 {_mb(report.db_freelist_bytes)}")
    table.add_row(
        "拒绝",
        "、".join(f"{item.path.name}（{item.code}）" for item in report.refused) or "[green]（无）[/green]",
    )
    if media is not None and media.skipped:
        table.add_row("跳过", "、".join(f"{item.path.name}（{item.code}）" for item in media.skipped))
    table.add_row("耗时", f"{report.duration_ms} ms")
    console.print(table)
    if not report.ok:
        console.print(
            "[red]有候选被守卫拒绝[/red]：白名单里的东西被列进了删除清单 —— 这是候选收集的 bug，"
            "不是噪音（见 src/studio/gc/media.py）。"
        )


@gc_app.command("run")
def gc_run_cmd(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只报告，一个字节都不动")] = False,
    rows_only: Annotated[bool, typer.Option("--rows-only", help="只回收 DB 行")] = False,
    media_only: Annotated[bool, typer.Option("--media-only", help="只回收文件")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """按 §03.7.5 的保留期回收媒资（每日 04:00，见 `config/app.yaml → scheduler`）。

    **白名单永不清理**：成片 / 封面 / 稿件 / 时间轴 / manifest / 滤镜图 / 备份 /
    素材库 / 热点归档。想删它们一律被拒（`GC_REFUSED_PROTECTED`）并让本命令以
    退出码 1 结束 —— 那说明候选清单写错了，是 bug 不是噪音。
    """
    if rows_only and media_only:
        console.print("[red]--rows-only 与 --media-only 不能同时给[/red]")
        raise typer.Exit(code=1)
    policy, warning = _gc_policy()
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")
    report = run_gc(
        StudioPaths.from_env(),
        policy=policy,
        dry_run=dry_run,
        rows=not media_only,
        media=not rows_only,
    )
    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_gc(report)
    if not report.ok:
        raise typer.Exit(code=1)


@pool_app.command("run")
def pool_run(
    pool: Annotated[str, typer.Option("--pool", help="池名：draft / voice / render / publish")],
    slot: Annotated[int, typer.Option("--slot", help="同池内的槽位（1 起）")] = 1,
    max_units: Annotated[
        int | None, typer.Option("--max-units", help="跑满 N 个单元即退出（排障用）")
    ] = None,
    max_empty_rounds: Annotated[
        int | None, typer.Option("--max-empty-rounds", help="连续 N 轮空池即退出（排障用）")
    ] = None,
) -> None:
    """前台启动一个池 worker。Ctrl+C ⇒ **draining**（跑完当前单元再退出，不丢进度）。"""
    try:
        report = run_pool(pool=pool, slot=slot, max_units=max_units, max_empty_rounds=max_empty_rounds)
    except StudioError as exc:
        _fail(exc, json_output=False)
        raise typer.Exit(code=1) from exc
    console.print_json(data=report.to_dict())


@pool_app.command("status")
def pool_status(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """列出各池 worker 心跳（存活 / 忙闲 / RSS / 当前 job）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}")
        console.print("[dim]跑 `studio db migrate` 初始化[/dim]")
        raise typer.Exit(code=1)
    connection = connect(paths.db_file, read_only=True)
    try:
        beats = HeartbeatStore(connection)
        workers = beats.list_workers()
        stale = {beat.worker_id for beat in beats.stale()}
    finally:
        connection.close()

    rows = [
        {
            "worker_id": beat.worker_id,
            "pool": beat.pool,
            "status": beat.status,
            "pid": beat.pid,
            "current_job_id": beat.current_job_id,
            "last_seen_at": beat.last_seen_at,
            "rss_mb": beat.rss_mb,
            "gpu_mem_mb": beat.gpu_mem_mb,
            "stale": beat.worker_id in stale,
        }
        for beat in workers
    ]
    if json_output:
        console.print_json(data={"count": len(rows), "workers": rows})
        return

    table = Table(title="worker 心跳（§04.5.1）", show_lines=False)
    table.add_column("worker_id", style="cyan", no_wrap=True)
    table.add_column("状态")
    table.add_column("PID", justify="right")
    table.add_column("当前 job", overflow="fold")
    table.add_column("RSS MB", justify="right")
    table.add_column("显存 MB", justify="right")
    table.add_column("最后心跳", overflow="fold")
    for beat in workers:
        palette = {"idle": "green", "busy": "yellow", "draining": "yellow", "dead": "red"}
        mark = " [red]（超时！）" if beat.worker_id in stale else ""
        table.add_row(
            beat.worker_id,
            f"[{palette.get(beat.status, 'white')}]{beat.status}{mark}",
            str(beat.pid or "-"),
            beat.current_job_id or "-",
            str(beat.rss_mb if beat.rss_mb is not None else "-"),
            str(beat.gpu_mem_mb if beat.gpu_mem_mb is not None else "-"),
            beat.last_seen_at,
        )
    console.print(table)
    if not workers:
        console.print("[dim]当前没有 worker 心跳（没有池进程在跑，或都已优雅退出）[/dim]")


@pool_app.command("reap")
def pool_reap(
    timeout_sec: Annotated[float, typer.Option("--timeout-sec", help="心跳超时阈值（默认 15s）")] = 15.0,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """把心跳超时的 worker 判死并留痕（supervisor 每轮自动做；这里给运维手动跑）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}")
        raise typer.Exit(code=1)
    connection = connect(paths.db_file)
    try:
        reaped = HeartbeatStore(connection).reap_stale(timeout_sec=timeout_sec)
    finally:
        connection.close()
    if json_output:
        console.print_json(data={"reaped": list(reaped), "timeout_sec": timeout_sec})
        return
    if reaped:
        console.print(f"[red]判死 {len(reaped)} 个 worker[/red]：" + "、".join(reaped))
    else:
        console.print("[green]没有超时的 worker[/green]")


#: 探测结果为"这条通道不可用"的状态集合（``no_key`` 不算：本地优先模式下正常）
_PROBE_FAILED: frozenset[str] = frozenset({"unreachable", "http_error"})


@llm_app.command("probe")
def llm_probe(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """探测双通道可用性（本地 ``GET /api/tags`` / 云端 ``GET /models``，**只读不烧钱**）。"""
    paths = StudioPaths.from_env()
    try:
        loaded = load_config(paths)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    rows = asyncio.run(_probe_profiles(loaded.bundle.llm.profiles))
    if json_output:
        console.print_json(data={"profiles": rows, "routing": _routing_rows(loaded.bundle.llm)})
    else:
        table = Table(title="LLM 通道探测（§01.2.4）", show_lines=False)
        table.add_column("通道", style="cyan", no_wrap=True)
        table.add_column("engine")
        table.add_column("模型")
        table.add_column("状态")
        table.add_column("详情", overflow="fold")
        palette = {"ok": "green", "no_key": "yellow", "unreachable": "red", "http_error": "red"}
        for row in rows:
            table.add_row(
                str(row["profile"]),
                str(row["engine"]),
                str(row["model"]),
                f"[{palette.get(str(row['status']), 'white')}]{row['status']}",
                str(row["detail"]),
            )
        console.print(table)
        route_table = Table(title="Agent → 通道路由", show_lines=False)
        route_table.add_column("Agent", style="cyan", no_wrap=True)
        route_table.add_column("主通道")
        route_table.add_column("兜底")
        for agent, primary, fallback in _routing_rows(loaded.bundle.llm):
            route_table.add_row(agent, primary, fallback)
        console.print(route_table)

    # 只有 no_key 不算失败（本地优先模式下云端未配置密钥是正常态）；
    # unreachable / http_error 都是"这条通道不可用" ⇒ 启动自检必须红灯。
    if any(row["status"] in _PROBE_FAILED for row in rows):
        raise typer.Exit(code=1)


@llm_app.command("budget")
def llm_budget(
    task_id: Annotated[str | None, typer.Option("--task-id", help="只看某个任务")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """按 ``llm_calls`` 聚合打印预算判定（R16：任务 token 上限 / 当日成本上限）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}")
        raise typer.Exit(code=1)
    try:
        loaded = load_config(paths)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    connection = connect(paths.db_file, read_only=True)
    try:
        store = LlmCallStore(connection)
        verdict = TokenBudget(loaded.bundle.llm.budget, store).check(task_id=task_id)
        calls = store.count_for_task(task_id) if task_id else 0
    finally:
        connection.close()

    if json_output:
        console.print_json(data={**verdict.model_dump(mode="json"), "task_id": task_id, "calls": calls})
        return
    console.print(
        f"[bold]预算判定[/bold] action={verdict.action} exceeded={verdict.exceeded}\n"
        f"任务 token：{verdict.spent_tokens} / {verdict.token_limit}\n"
        f"当日成本：{verdict.spent_cost_usd:.4f} / {verdict.cost_limit_usd:.4f} USD\n"
        f"理由：{verdict.reason}"
    )


async def _probe_profiles(profiles: dict[str, LlmProfileConfig]) -> list[dict[str, Any]]:
    """并发探测各通道（只发只读 GET；本地走 ``/api/tags``）。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        return [await _probe_one(client, name, profile) for name, profile in sorted(profiles.items())]


async def _probe_one(client: httpx.AsyncClient, name: str, profile: LlmProfileConfig) -> dict[str, Any]:
    base = profile.base_url.rstrip("/")
    row: dict[str, Any] = {
        "profile": name,
        "engine": profile.engine,
        "model": profile.model,
        "base_url": profile.base_url,
    }
    if profile.engine == "ollama":
        url, headers = f"{base}/api/tags", {}
        api_key = None
    else:
        api_key = os.environ.get(profile.api_key_env) if profile.api_key_env else None
        if profile.api_key_env and not api_key:
            return {**row, "status": "no_key", "detail": f"环境变量 {profile.api_key_env} 未设置"}
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {**row, "status": "unreachable", "detail": f"{type(exc).__name__}: {url}"}
    if response.status_code >= 400:
        return {**row, "status": "http_error", "detail": f"HTTP {response.status_code}"}
    detail = "已连接"
    if profile.engine == "ollama":
        payload = response.json()
        names = [item.get("name") for item in payload.get("models", []) if isinstance(item, dict)]
        detail = f"本地模型 {len(names)} 个" + (f"（{names[0]}…）" if names else "（尚未 pull 模型）")
    return {**row, "status": "ok", "detail": detail}


def _routing_rows(llm: object) -> list[tuple[str, str, str]]:
    routing = getattr(llm, "routing", {})
    return [(agent, item.profile, item.fallback or "—") for agent, item in sorted(routing.items())]


@prompts_app.command("verify")
def prompts_verify(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """校验提示词注册表与文件内容是否一致（漂移 ⇒ 退出码 1）。"""
    paths = StudioPaths.from_env()
    try:
        library = PromptLibrary.load(paths.prompts_dir)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc
    problems = library.verify()
    versions = {name: library.prompt_version(name) for name in library.names()}
    if json_output:
        console.print_json(data={"ok": not problems, "problems": problems, "versions": versions})
    else:
        table = Table(title="提示词注册表", show_lines=False)
        table.add_column("条目", style="cyan", no_wrap=True)
        table.add_column("prompt_version", overflow="fold")
        for name, version in versions.items():
            table.add_row(name, version)
        console.print(table)
        if problems:
            console.print("[red]漂移：[/red]\n" + "\n".join(f"  - {item}" for item in problems))
        else:
            console.print("[green]全部与 manifest.yaml 一致[/green]")
    if problems:
        raise typer.Exit(code=1)


@prompts_app.command("list")
def prompts_list(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """列出已登记提示词（含未登记的 Agent 缺口提示）。"""
    paths = StudioPaths.from_env()
    try:
        library = PromptLibrary.load(paths.prompts_dir)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc
    entries = [
        {
            "name": name,
            "system": entry.system,
            "user": entry.user,
            "version": entry.version,
            "prompt_version": library.prompt_version(name),
            "description": entry.description,
        }
        for name, entry in library.iter_entries()
    ]
    if json_output:
        console.print_json(data={"root": str(library.root), "count": len(entries), "prompts": entries})
        return
    table = Table(title=f"提示词（{library.root}）", show_lines=False)
    table.add_column("条目", style="cyan", no_wrap=True)
    table.add_column("文件", overflow="fold")
    table.add_column("prompt_version")
    table.add_column("说明", overflow="fold")
    for item in entries:
        files = " + ".join(part for part in (item["system"], item["user"]) if part) or "—"
        table.add_row(str(item["name"]), files, str(item["prompt_version"]), str(item["description"]))
    console.print(table)


@topics_app.command("analyze")
def topics_analyze(
    no_import: Annotated[
        bool, typer.Option("--no-import", help="跳过 data/hot 与 data/feedback 的重新导入")
    ] = False,
    batch_id: Annotated[str | None, typer.Option("--batch-id", help="指定批次 id（默认自动生成）")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """热点 + 历史反馈 ⇒ 5–8 个内容方向（Planner · §04.1.2）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_topic_service(paths, connection)
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
        report = asyncio.run(
            service.run_planner(persona=_active_persona(), batch_id=batch_id, import_sources=not no_import)
        )
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        table = Table(title=f"内容方向 · 批次 {report.batch_id or '—'}", show_lines=False)
        table.add_column("seq", justify="right")
        table.add_column("方向", overflow="fold")
        table.add_column("理由", overflow="fold")
        table.add_column("优先级", justify="right")
        table.add_column("依据", overflow="fold")
        table.add_column("风险", overflow="fold")
        for row in report.directions:
            table.add_row(
                str(row.seq),
                row.title,
                row.rationale,
                str(row.priority),
                _grounding_text(row.grounded_on),
                "、".join(row.risk_flags) or "—",
            )
        console.print(table)
        console.print(
            f"热点：导入 {report.hot_imported}（坏行 {report.hot_bad}）· "
            f"消费 {report.hot_consumed} · 归档 {len(report.archived)} 个文件\n"
            f"反馈：导入 {report.feedback_imported} · 分类 {report.feedback_classified}"
        )
        if report.warnings:
            console.print("[yellow]告警：[/yellow]\n" + "\n".join(f"  - {item}" for item in report.warnings))
    if not report.ok:
        if not json_output:
            console.print(f"[red]{report.error_code}[/red]：{report.error_message}")
        raise typer.Exit(code=1)


@topics_app.command("ideate")
def topics_ideate(
    batch_id: Annotated[str | None, typer.Option("--batch-id", help="批次 id（默认取最近一批）")] = None,
    direction_id: Annotated[
        list[str] | None, typer.Option("--direction-id", help="只跑指定方向（可重复传）")
    ] = None,
    per_direction: Annotated[int, typer.Option("--per-direction", help="每方向目标选题数")] = (
        TOPICS_PER_DIRECTION
    ),
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """每个方向 ⇒ 3–5 个选题（Ideator · §04.1.3，**方向之间互不影响**）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_topic_service(paths, connection)
            report = asyncio.run(
                service.run_ideator(
                    persona=_active_persona(),
                    batch_id=batch_id,
                    direction_ids=direction_id,
                    per_direction=per_direction,
                )
            )
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        table = Table(title=f"选题产出 · 批次 {report.batch_id}", show_lines=False)
        table.add_column("方向", overflow="fold")
        table.add_column("结果")
        table.add_column("新增", justify="right")
        table.add_column("丢弃", justify="right")
        table.add_column("降分", justify="right")
        table.add_column("详情", overflow="fold")
        for item in report.outcomes:
            table.add_row(
                item.title,
                "[green]ok[/green]" if item.ok else "[red]failed[/red]",
                str(item.inserted),
                str(item.dropped),
                str(item.demoted),
                item.error_message or "—",
            )
        console.print(table)
        console.print(
            f"合计新增 [bold]{report.inserted}[/bold] 个选题"
            f"（丢弃重复 {report.dropped}，降分 {report.demoted}，失败方向 {len(report.failed)}）"
        )
    if not report.ok:
        raise typer.Exit(code=1)


@topics_app.command("list")
def topics_list(
    status: Annotated[
        str, typer.Option("--status", help="筛选状态（candidate/selected/queued/rejected/expired）")
    ] = "candidate",
    limit: Annotated[int, typer.Option("--limit", help="最多显示条数")] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """选题池瀑布流（按分数倒序 · §03.3.5）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file, read_only=True)
    try:
        rows = TopicRepo(connection).list_pool(status=status, limit=limit)
    finally:
        connection.close()

    if json_output:
        console.print_json(
            data={
                "status": status,
                "count": len(rows),
                "topics": [
                    {
                        "id": row.id,
                        "direction_id": row.direction_id,
                        "title": row.title,
                        "hook_type": row.hook_type,
                        "angle": row.angle,
                        "score": row.score,
                        "reason": row.reason,
                        "dedup_hash": row.dedup_hash,
                        "similar_to": row.similar_to,
                    }
                    for row in rows
                ],
            }
        )
        return

    table = Table(title=f"选题池 · {status}（{len(rows)} 条）", show_lines=False)
    table.add_column("分数", justify="right")
    table.add_column("标题", overflow="fold")
    table.add_column("钩子")
    table.add_column("角度", overflow="fold")
    table.add_column("相似", overflow="fold")
    for row in rows:
        table.add_row(
            "—" if row.score is None else f"{row.score:.1f}",
            row.title,
            row.hook_type or "—",
            row.angle,
            "；".join(str(item.get("target_title", "?")) for item in row.similar_to) or "—",
        )
    console.print(table)


@script_app.command("draft")
def script_draft(
    topic_id: Annotated[str, typer.Argument(help="选题 id（`studio topics list` 可查）")],
    task_id: Annotated[str | None, typer.Option("--task-id", help="复用已有任务（默认按选题新建）")] = None,
    duration_ms: Annotated[
        int | None, typer.Option("--duration-ms", help="目标时长（毫秒，默认取 persona 上限）")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """选题 ⇒ 大纲 ⇒ 600–800 字口播稿 ⇒ 逐句落库（§04.1.4 / §04.1.5）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_script_service(paths, connection)
            report = asyncio.run(
                service.draft(
                    topic_id=topic_id,
                    persona=_active_persona(),
                    task_id=task_id,
                    target_duration_ms=duration_ms,
                )
            )
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_draft(report)
    if not report.ok:
        raise typer.Exit(code=1)


@script_app.command("show")
def script_show(
    task_id: Annotated[str, typer.Argument(help="任务 id")],
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """当前生效稿件 + 逐句表（断点续传看到的就是这份数据）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file, read_only=True)
    try:
        payload = read_active_script(connection, task_id)
        if payload is None:
            # 区分"任务不存在"（id 打错了）与"任务还没有稿"（还没跑到写稿）：
            # 混为一谈会让排查的人去找一份永远不会出现的稿子。任务不存在 ⇒ TaskNotFound。
            TaskService(connection).get(task_id)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if payload is None:
        if json_output:
            console.print_json(data={"ok": False, "reason": "no_script", "task_id": task_id})
        else:
            console.print(f"[yellow]任务 {task_id} 还没有稿件[/yellow]")
        raise typer.Exit(code=1)
    script, sentences = payload

    if json_output:
        console.print_json(
            data={
                "script": {
                    "id": script.id,
                    "task_id": script.task_id,
                    "version": script.version,
                    "title": script.title,
                    "hook": script.hook,
                    "cta": script.cta,
                    "body_md": script.body_md,
                    "word_count": script.word_count,
                    "est_duration_ms": script.est_duration_ms,
                    "speaker_ratio": script.speaker_ratio,
                    "outline": script.outline,
                    "target_chars": script.target_chars,
                    "llm_model": script.llm_model,
                    "prompt_version": script.prompt_version,
                },
                "sentences": [
                    {
                        "seq": row.seq,
                        "text": row.text,
                        "speaker": row.speaker,
                        "emotion": row.emotion,
                        "pause_after_ms": row.pause_after_ms,
                        "tts_status": row.tts_status,
                    }
                    for row in sentences
                ],
            }
        )
        return

    console.print(f"[bold]{script.title or '（无标题）'}[/bold]  v{script.version}")
    console.print(
        f"{script.word_count} 字 · {len(sentences)} 句 · 约 {(script.est_duration_ms or 0) / 1000:.0f} 秒"
    )
    table = Table(title="逐句表（按句配音 / 断点续传的粒度）", show_lines=False)
    table.add_column("seq", justify="right")
    table.add_column("说话人")
    table.add_column("情绪")
    table.add_column("文本", overflow="fold")
    table.add_column("TTS")
    for row in sentences:
        table.add_row(str(row.seq), row.speaker, row.emotion, row.text, row.tts_status)
    console.print(table)


def _render_draft(report: DraftReport) -> None:
    """写稿结果的人读视图（与 ``--json`` 同一份数据）。"""
    if not report.ok:
        console.print(f"[red]{report.error_code}[/red]：{report.error_message}")
        return
    console.print(f"[green]稿件已生成[/green]：{report.title}")
    console.print(
        f"任务 {report.task_id} · 稿件 {report.script_id} v{report.version} · "
        f"{report.word_count} 字 · {report.sentence_count} 句 · 约 {report.est_duration_ms / 1000:.0f} 秒"
    )
    if report.speaker_ratio:
        console.print(
            "说话人占比：" + "、".join(f"{name} {share:.0%}" for name, share in report.speaker_ratio.items())
        )
    if report.catchphrases_used:
        console.print("口癖命中：" + "、".join(report.catchphrases_used))
    if report.warnings:
        console.print("[yellow]告警：[/yellow]\n" + "\n".join(f"  - {item}" for item in report.warnings))


def _active_persona() -> PersonaConfig:
    """当前激活人物（热重载单例；`studio persona use` 切的就是它）。"""
    return _persona_store().current().config


def _build_topic_service(paths: StudioPaths, connection: sqlite3.Connection) -> TopicService:
    """装配选题服务：配置 → 提示词 → 网关 → 三个 Agent → 服务。

    装配放 CLI 而不是服务层：服务只认 Protocol（可注入假件），
    "去哪里拿配置与提示词"是**入口**的责任（T1.9 裁定 70 的延伸）。
    """
    loaded = load_config(paths)
    prompts = PromptLibrary.load(paths.prompts_dir)
    log = LogService(connection)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        paths=paths,
        log=_gateway_log_sink(log),
    )
    return TopicService(
        connection,
        planner=PlannerAgent(gateway, prompts),
        ideator=IdeatorAgent(gateway, prompts),
        classifier=FeedbackClassifierAgent(gateway, prompts),
        paths=paths,
        log=log.append,
    )


def _build_script_service(paths: StudioPaths, connection: sqlite3.Connection) -> ScriptService:
    """装配写稿服务：配置 → 提示词 → 网关 → Director/Writer → 服务。

    与 ``_build_topic_service`` 同一手法：装配是**入口**的责任，
    服务层只认 Protocol（可注入假件）。
    """
    loaded = load_config(paths)
    prompts = PromptLibrary.load(paths.prompts_dir)
    log = LogService(connection)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        paths=paths,
        log=_gateway_log_sink(log),
    )
    return ScriptService(
        connection,
        director=DirectorAgent(gateway, prompts),
        writer=WriterAgent(gateway, prompts),
        paths=paths,
        log=log.append,
    )


def _gateway_log_sink(log: LogService) -> LogSink:
    """``LogService.append`` → 网关的日志出口。

    为什么要一层瘦适配：``LogSink`` 声明返回 ``None``，而 ``append`` 返回落库的行
    （供调用方断言用）—— 直接把方法当回调传，mypy 会因为返回类型不一致拒绝
    （T1.9 施工记录）。丢掉返回值即可，语义没有任何损失。
    """

    def sink(
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        log.append(level=level, source=source, message=message, task_id=task_id, payload=payload)

    return sink


def _grounding_text(items: list[dict[str, Any]]) -> str:
    """``grounded_on_json`` → 一行可读依据（WebUI 展示的是同一份数据）。"""
    labels = {"persona": "定位", "hot": "热点", "feedback": "反馈"}
    parts: list[str] = []
    for item in items:
        label = labels.get(str(item.get("type")), str(item.get("type")))
        if item.get("kind"):
            label = f"{label}/{item['kind']}"
        if item.get("quote"):
            label = f"{label}：{item['quote']}"
        parts.append(label)
    return "；".join(parts) or "—"


@render_app.command("profile")
def render_profile(
    show: Annotated[
        bool,
        typer.Option("--show/--no-show", help="打印完整合成 profile（输出参数 + 水印实测 + 硬门禁）"),
    ] = True,
    name: Annotated[
        str | None, typer.Option("--name", help="指定 profile 名（默认取 default_profile）")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """打印合成 profile：画布 / 编码参数 / 水印（T3.2 · §04.2.8.3）。

    退出码恒为 0 —— 这是**诊断**命令。水印缺失不是阻塞项（有就贴、没有就跳过），
    所以这里只如实说明"这次会不会贴"，不判"能不能出片"。
    """
    paths = StudioPaths.from_env()
    source = paths.config_dir / "outputs.yaml"
    try:
        outputs = load_outputs_config(source)
        report = build_render_profile_report(outputs, source=source, home=paths.home, name=name)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=report.to_dict())
        return
    _render_profile_report(report, show=show)


def _render_profile_report(report: RenderProfileReport, *, show: bool) -> None:
    """把报告画成人看的表（``--no-show`` 只打印一行摘要）。"""
    profile = report.profile
    headline = (
        f"{profile.name} · {profile.width}x{profile.height} · {profile.fps}fps · "
        f"{profile.vcodec} {profile.quality_field.upper()}{profile.quality}"
    )
    if not show:
        console.print(f"{headline} · 水印={'贴' if report.watermark_enabled else '跳过'}")
        return

    canvas = Table(title=f"合成 profile · {profile.name}", show_lines=False)
    canvas.add_column("项", style="cyan", no_wrap=True)
    canvas.add_column("取值", overflow="fold")
    canvas.add_row("来源", f"{report.source}（default_profile={report.default_profile}）")
    canvas.add_row("画布", f"{profile.width}x{profile.height} @ {profile.fps}fps")
    canvas.add_row("色彩", f"{profile.colorspace} · {profile.pix_fmt} · gop={profile.gop}")
    canvas.add_row(
        "编码",
        f"{profile.vcodec} · {profile.quality_field}={profile.quality} · preset={profile.preset}",
    )
    canvas.add_row(
        "音频",
        f"{profile.acodec} {profile.audio_bitrate} @ {profile.audio_sample_rate}Hz "
        f"{profile.audio_channels}ch",
    )
    canvas.add_row("faststart", "✅" if profile.faststart else "❌")
    canvas.add_row("平台", "、".join(profile.platforms) or "—（保底档，不参与按平台选档）")
    canvas.add_row("降级保底档", FALLBACK_PROFILE_NAME)
    canvas.add_row("输出参数", " ".join(profile.output_args()))
    console.print(canvas)

    table = Table(title="全部 profile", show_lines=False)
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("画布", no_wrap=True)
    table.add_column("编码", no_wrap=True)
    table.add_column("备注", overflow="fold")
    for item in report.profiles:
        marks: list[str] = []
        if item.name == report.default_profile:
            marks.append("默认")
        if item.is_fallback:
            marks.append("降级保底")
        table.add_row(
            item.name,
            f"{item.width}x{item.height}",
            f"{item.vcodec} {item.quality_field}={item.quality}",
            "、".join(marks) or "—",
        )
    console.print(table)

    plan = report.watermark
    wm = Table(title="固定水印（有就贴，没有就跳过）", show_lines=False)
    wm.add_column("项", style="cyan", no_wrap=True)
    wm.add_column("取值", overflow="fold")
    wm.add_row("路径", plan.spec.image_path.as_posix())
    wm.add_row("位置", plan.spec.position)
    wm.add_row("边距", f"{plan.spec.margin_px[0]}, {plan.spec.margin_px[1]}")
    wm.add_row("宽度", f"{plan.spec.width_px}px")
    wm.add_row("透明度", f"{plan.spec.opacity}")
    wm.add_row("文件", _watermark_file_cell(report))
    if plan.placement is not None:
        wm.add_row(
            "摆放",
            f"x={plan.placement.x} y={plan.placement.y} "
            f"（{plan.placement.width_px}x{plan.placement.height_px}）",
        )
    wm.add_row("结论", "[green]贴[/green]" if plan.enabled else "[yellow]跳过[/yellow]")
    console.print(wm)

    for note in plan.warnings:
        console.print(f"[yellow]warn[/yellow] {note}")
    if plan.enabled:
        console.print("[green]水印[/green]：这一档会贴到成片上")
    else:
        console.print(f"[yellow]水印[/yellow]：跳过（{plan.skipped_reason}）—— **不影响出片**")


def _watermark_file_cell(report: RenderProfileReport) -> str:
    """水印文件那一格的文案（缺文件 / 坏文件 / 正常，三种状态一眼分清）。

    三种状态都**不阻塞出片** —— 缺了就是"这一版没有水印"，不是"这一版出不来"。
    """
    asset = report.watermark.asset
    if not asset.exists:
        return "[yellow]缺失[/yellow]（跳过水印，照常出片）"
    if not asset.usable:
        return f"[yellow]不可用[/yellow]：{asset.problem}（跳过水印，照常出片）"
    return f"[green]OK[/green] {asset.width_px}x{asset.height_px}（含透明通道，{asset.sha256[:12]}）"


def _script_text_for(
    task_id: str,
    *,
    text: str | None,
    text_file: Path | None,
    paths: StudioPaths,
) -> str:
    """文案从哪来：``--text`` > ``--text-file`` > 库里的生效稿件（优先级从高到低）。"""
    if text is not None and text.strip():
        return text
    if text_file is not None:
        if not text_file.is_file():
            raise StudioError(
                f"文案文件不存在：{text_file}",
                code=ErrorCode.PATH_MISSING,
                context={"path": text_file.as_posix()},
                remediation="确认路径拼写，或改用 --text 直接给文案",
            )
        return text_file.read_text(encoding="utf-8")
    if not paths.db_file.is_file():
        raise StudioError(
            f"数据库尚未初始化：{paths.db_file}",
            code=ErrorCode.PATH_MISSING,
            context={"db": paths.db_file.as_posix()},
            remediation="先跑 `studio db migrate`，或改用 --text / --text-file 直接给文案",
        )

    connection = connect(paths.db_file, read_only=True)
    try:
        payload = read_active_script(connection, task_id)
    finally:
        connection.close()
    if payload is None:
        raise StudioError(
            f"任务 {task_id} 没有生效稿件，也没有给 --text",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id},
            remediation="先用 `studio script draft` 出稿，或直接 `--text '口播文案'`",
        )
    _script, sentences = payload
    return "".join(row.text for row in sentences)


def _render_produce_result(result: ProduceResult) -> None:
    """出片结果表（成片路径放第一行 —— 跑完最想看的就是它在哪）。"""
    table = Table(title="出片结果", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("取值", overflow="fold")
    table.add_row("成片", f"[green]{result.final.as_posix()}[/green]")
    table.add_row(
        "时长",
        f"{result.duration_ms / 1000:.2f}s（人声 {result.voice_duration_ms / 1000:.2f}s）",
    )
    table.add_row("体积", f"{result.size_bytes / 1024 / 1024:.1f} MB")
    table.add_row("profile", result.profile_name)
    table.add_row("底片", result.clip.name if result.clip else "[yellow]（无 ⇒ 纯黑底降级）[/yellow]")
    table.add_row("BGM", result.bgm.name if result.bgm else "—（跳过：没有 BGM 素材）")
    table.add_row(
        "水印",
        "[green]已贴[/green]" if result.watermark_enabled else "[yellow]跳过[/yellow]",
    )
    if not result.watermark_enabled and result.watermark_skipped_reason:
        table.add_row("水印跳过原因", result.watermark_skipped_reason)
    table.add_row(
        "字幕",
        f"[green]已烧 {len(result.subtitle.cues)} 句[/green]"
        if result.subtitle.enabled
        else "[yellow]跳过[/yellow]",
    )
    if not result.subtitle.enabled and result.subtitle.skipped_reason:
        table.add_row("字幕跳过原因", result.subtitle.skipped_reason)
    if result.output_loudness is not None:
        table.add_row(
            "响度（成片实测）",
            f"{result.output_loudness.input_i:.1f} LUFS / {result.output_loudness.input_tp:.1f} dBTP",
        )
    if result.degraded:
        table.add_row("降级", f"[yellow]{result.degrade_reason}[/yellow]")
    table.add_row("时间轴", result.timeline.as_posix())
    for warning in result.warnings:
        table.add_row("[yellow]降级说明[/yellow]", warning)
    table.add_row("manifest", result.manifest.as_posix())
    console.print(table)


@render_app.command("make")
def render_make(
    task_id: Annotated[str, typer.Option("--task-id", help="任务 id（产物按它归档）")],
    text: Annotated[str | None, typer.Option("--text", help="直接给口播文案")] = None,
    text_file: Annotated[Path | None, typer.Option("--text-file", help="从 UTF-8 文件读文案")] = None,
    profile_name: Annotated[
        str | None, typer.Option("--profile", help="合成 profile 名（默认取 default_profile）")
    ] = None,
    voice: Annotated[str | None, typer.Option("--voice", help="音色名（默认自动挑一个中文音色）")] = None,
    reuse_voice: Annotated[
        bool,
        typer.Option("--reuse-voice", help="复用已有 voice_master.wav（反复调渲染参数时用）"),
    ] = False,
    threads: Annotated[int | None, typer.Option("--threads", help="ffmpeg -threads")] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="固定挑素材的随机种子（复现同一条片子）")] = None,
    subtitle: Annotated[
        bool | None,
        typer.Option("--subtitle/--no-subtitle", help="烧字幕（默认听 outputs.yaml → subtitle.enabled）"),
    ] = None,
    force_render: Annotated[
        bool,
        typer.Option("--force-render", help="无视整片级缓存，重跑一遍 ffmpeg"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """**文案 → 配音 → 字幕 → 混音 → 渲染**：出一条 MP4（T3.x 主线）。

    文案来源三选一（优先级从高到低）：``--text`` / ``--text-file`` / 库里的生效稿件。

    水印 / BGM / 字幕都是**可选**：有就贴、没有就跳过 —— 缺它们不影响出片。
    底片从 ``data/assets/mc_parkour/`` 里随机挑一条 mp4。

    字幕时间取自**逐句实测**的配音时长（不是按字数估的），所以它与人声是对齐的；
    字体优先用 ``templates/<模板>/assets/fonts/``，没有则退到系统字体目录。

    同 ``composite_hash`` 的片子已经在盘上（同文案 / 同素材 / 同水印）⇒ **直接复用那一支**，
    一次 ffmpeg 都不跑；``--force-render`` 可强制重渲。注意底片是随机挑的 —— 不带 ``--seed``
    的重跑通常挑到另一条底片，哈希不同，照常重渲（设计如此，见 ``render/cache.py``）。
    """
    paths = StudioPaths.from_env()
    try:
        script_text = _script_text_for(task_id, text=text, text_file=text_file, paths=paths)
        outputs_source = paths.config_dir / "outputs.yaml"
        outputs = load_outputs_config(outputs_source)

        def progress(stage: str, done: int, total: int, note: str) -> None:
            if not json_output:
                console.print(f"[dim]{stage}[/dim] {done}/{total} {note}")

        result = produce_video(
            ProduceRequest(
                task_id=task_id,
                text=script_text,
                profile_name=profile_name,
                voice=voice,
                reuse_voice=reuse_voice,
                threads=threads,
                seed=seed,
                subtitle=subtitle,
                force_render=force_render,
            ),
            paths=paths,
            outputs=outputs,
            outputs_source=outputs_source,
            on_progress=progress,
        )
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=result.to_dict())
        return
    _render_produce_result(result)


def _render_pipeline_report(report: PipelineReport) -> None:
    """流水线结果表（最终状态与成片放最上面 —— 跑完最想看的就是这两行）。"""
    table = Table(title="流水线", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("取值", overflow="fold")
    table.add_row("任务", report.task_id)
    table.add_row(
        "状态",
        f"{report.status_before} → [green]{report.status}[/green]（--until {report.until}）",
    )
    if report.final is not None:
        table.add_row("成片", f"[green]{report.final.as_posix()}[/green]")
    if report.quality is not None:
        loudness = (
            f"{report.quality.lufs:.1f} LUFS / {report.quality.true_peak:.1f} dBTP"
            if report.quality.lufs is not None and report.quality.true_peak is not None
            else "—（没量出来）"
        )
        table.add_row("QC（成片实测）", loudness)
        if report.quality.degraded:
            table.add_row("降级", f"[yellow]{report.quality.degrade_reason}[/yellow]")
    console.print(table)

    if report.steps:
        steps = Table(title="走过哪几步", show_lines=False)
        steps.add_column("阶段", style="cyan", no_wrap=True)
        steps.add_column("状态", no_wrap=True)
        steps.add_column("说明", overflow="fold")
        for step in report.steps:
            steps.add_row(step.stage, f"{step.status_before} → {step.status}", step.note)
        console.print(steps)


@pipeline_app.command("run")
def pipeline_run(
    task_id: Annotated[str, typer.Argument(help="任务 id（`studio db status` / 面板可查）")],
    until: Annotated[
        str,
        typer.Option(
            "--until",
            help="跑到哪个状态就停：" + " / ".join(item.value for item in SUPPORTED_UNTIL),
        ),
    ] = TaskStatus.COMPLETED.value,
    profile_name: Annotated[
        str | None, typer.Option("--profile", help="合成 profile 名（默认取 default_profile）")
    ] = None,
    voice: Annotated[str | None, typer.Option("--voice", help="音色名（默认自动挑一个中文音色）")] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="固定挑素材的随机种子")] = None,
    subtitle: Annotated[
        bool | None,
        typer.Option("--subtitle/--no-subtitle", help="烧字幕（默认听 outputs.yaml → subtitle.enabled）"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """**把一条任务跑到成片**：配音 ⇒ 渲染 ⇒ 回填 QC（T3.7 的验收命令）。

    按任务**当前状态**决定从哪一步接手，所以"断点续跑"与"只跑到配音为止"是同一条命令，
    跑第二遍不会重渲已经出好的片子（幂等）。

    四件事它**故意不做**（每一条都有理由，见 `services/pipeline_service.py`）：
    **不写稿**（没有生效稿件就直接报错，不去偷偷调 LLM）、**不审稿**、**不代按确认闸**、
    **不发布**。

    底片从 ``data/assets/mc_parkour/`` 随机挑；挑不到 ⇒ 纯黑底照出（黑屏降级）。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    try:
        target = TaskStatus(until)
    except ValueError as exc:
        console.print(
            f"[red]--until 不认识 {until!r}[/red]；只能是 "
            + " / ".join(item.value for item in SUPPORTED_UNTIL)
        )
        raise typer.Exit(code=2) from exc

    outputs_source = paths.config_dir / "outputs.yaml"
    outputs = load_outputs_config(outputs_source)
    # 边配音边渲染（C7）默认关。这里**如实**读配置再传下去，不写死 False ——
    # 写死的话，改了 YAML 的人只会看到"改了没生效"，而那种故障最难查。
    streaming_render = load_app_config(paths).pipeline.streaming_render
    connection = connect(paths.db_file)
    try:

        def progress(stage: str, done: int, total: int, note: str) -> None:
            if not json_output:
                console.print(f"[dim]{stage}[/dim] {done}/{total} {note}")

        try:
            report = run_task(
                task_id=task_id,
                paths=paths,
                outputs=outputs,
                outputs_source=outputs_source,
                connection=connection,
                until=target,
                profile_name=profile_name,
                voice=voice,
                seed=seed,
                subtitle=subtitle,
                streaming_render=streaming_render,
                on_progress=progress,
            )
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
        return
    _render_pipeline_report(report)


@service_app.command("start")
def service_start(
    only: Annotated[str | None, typer.Option("--only", help="只拉起这些服务（逗号分隔，排障用）")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="不自动打开浏览器")] = False,
    no_doctor: Annotated[
        bool, typer.Option("--no-doctor", help="跳过 doctor 门禁（**仅排障**，正常启动禁止）")
    ] = False,
    ready_timeout_sec: Annotated[
        float, typer.Option("--ready-timeout-sec", help="等就绪的总预算（秒）")
    ] = 60.0,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """一键拉起五进程（API / TTS / draft / voice / render）并等就绪（T1.12 · 原文附2）。

    顺序固定：**doctor 门禁 ⇒ 清理陈旧台账与残留关停标志 ⇒ 逐进程拉起 ⇒ 等就绪 ⇒ 开浏览器**。
    未就绪的进程（池 handler 未注册 / TTS 服务未落地）报 `degraded` 并**不阻塞**其余进程。
    """
    paths = StudioPaths.from_env()
    manager = default_manager(paths)
    selected = [item.strip() for item in (only or "").split(",") if item.strip()] or None
    try:
        report = manager.start(
            open_browser=not no_browser,
            only=selected,
            doctor_gate=not no_doctor,
            ready_timeout_sec=ready_timeout_sec,
        )
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_start(report)
    raise typer.Exit(code=0 if report.ok else 1)


@service_app.command("stop")
def service_stop(
    timeout_sec: Annotated[float, typer.Option("--timeout-sec", help="优雅关停的宽限期（秒）")] = 10.0,
    only: Annotated[str | None, typer.Option("--only", help="只关停这些服务（逗号分隔）")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """优雅关停全部服务（标志文件 ⇒ Ctrl-Break ⇒ 强杀，T1.12 · 裁定 102）。

    强杀过的进程会被**如实列进** `forced` —— 那意味着它可能丢了当前单元，
    不假装优雅。
    """
    paths = StudioPaths.from_env()
    manager = default_manager(paths)
    selected = [item.strip() for item in (only or "").split(",") if item.strip()] or None
    report = manager.stop(timeout_sec=timeout_sec, only=selected)
    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_stop(report)
    raise typer.Exit(code=0 if report.ok else 1)


@service_app.command("status")
def service_status(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """查看五进程的台账 / 存活 / 端口（T1.12）。"""
    paths = StudioPaths.from_env()
    manager = default_manager(paths)
    rows = manager.status()
    if json_output:
        console.print_json(data={"services": [row.to_dict() for row in rows]})
        return

    table = Table(title="制片台进程（T1.12 · 原文附2）", show_lines=False)
    table.add_column("服务", style="cyan", no_wrap=True)
    table.add_column("PID", justify="right")
    table.add_column("存活")
    table.add_column("端口", justify="right")
    table.add_column("端口占用")
    table.add_column("日志", overflow="fold")
    for row in rows:
        alive = "[green]是[/green]" if row.alive else "[dim]否[/dim]"
        port = str(row.port) if row.port is not None else "-"
        opened = "[green]是[/green]" if row.port_open else "[dim]否[/dim]"
        table.add_row(row.name, str(row.pid or "-"), alive, port, opened, str(row.log_file))
    console.print(table)

    readiness = manager.readiness_all()
    not_ready = [item for item in readiness if not item.ready]
    for item in not_ready:
        console.print(f"[yellow]{item.name} 未就绪[/yellow]：{item.detail}")
        if item.remediation:
            console.print(f"[dim]  修复：{item.remediation}[/dim]")


def _render_start(report: StartReport) -> None:
    """启动结论（人读）。"""
    console.print(
        f"[green]已就绪[/green] {len(report.ready)} 个："
        f"{', '.join(report.ready) if report.ready else '（无）'}"
    )
    if report.started:
        console.print(f"[dim]本次拉起：{', '.join(report.started)}[/dim]")
    if report.already_running:
        console.print(f"[dim]已在运行：{', '.join(report.already_running)}[/dim]")
    for name in report.degraded:
        console.print(f"[yellow]降级（未拉起）[/yellow]：{name}")
    for name in report.port_busy:
        console.print(f"[red]端口被占用[/red]：{name}（先 `studio service status` 看清是谁）")
    for name in report.failed:
        console.print(f"[red]启动失败[/red]：{name}（看 data/logs/{name}.log）")
    if report.ready:
        opened = "，浏览器已打开" if report.browser_opened else ""
        console.print(f"[green]WebUI[/green] {report.url}/ （WS: /ws/ui）{opened}")
    console.print(f"[dim]耗时 {report.elapsed_ms} ms[/dim]")


def _render_stop(report: StopReport) -> None:
    """关停结论（人读）。"""
    if report.stopped:
        console.print(f"[green]优雅退出[/green]：{', '.join(report.stopped)}")
    if report.forced:
        console.print(
            f"[red]强制终止[/red]：{', '.join(report.forced)}"
            "（**可能丢当前单元**，看对应 data/logs/*.log 确认）"
        )
    if report.missing:
        console.print(f"[dim]陈旧台账已清理：{', '.join(report.missing)}[/dim]")
    if report.skipped:
        console.print(f"[dim]无台账：{', '.join(report.skipped)}[/dim]")
    if not (report.stopped or report.forced or report.missing):
        console.print("[yellow]没有正在运行的进程。[/yellow]")
    console.print(f"[dim]耗时 {report.elapsed_ms} ms[/dim]")


@app.command()
def serve(
    host: Annotated[str | None, typer.Option("--host", help="监听地址（默认取 app.yaml: web.host）")] = None,
    port: Annotated[int | None, typer.Option("--port", help="监听端口（默认取 app.yaml: web.port）")] = None,
    tail_interval_sec: Annotated[
        float, typer.Option("--tail-interval-sec", help="日志 tail 周期（秒）")
    ] = 0.25,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON（只打印启动参数）")] = False,
) -> None:
    """启动 WebUI 操作台（FastAPI + `/ws/ui` 实时推送 · T1.7 骨架 / T1.12 一键启动）。"""
    paths = StudioPaths.from_env()
    try:
        loaded = load_config(paths)
    except StudioError as exc:
        _fail(exc, json_output)
        raise typer.Exit(code=1) from exc
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)
    web = loaded.bundle.app.web
    bind_host = host or web.host
    bind_port = port or web.port
    if json_output:
        console.print_json(data={"host": bind_host, "port": bind_port, "db": str(paths.db_file)})
        return
    console.print(f"[green]WebUI[/green] http://{bind_host}:{bind_port}/ （WS: /ws/ui）")
    run_server(paths=paths, host=bind_host, port=bind_port, tail_interval_sec=tail_interval_sec)


# ── publish：发布前准备（T5.1 · §06.3 / §06.4）────────────────────────


def _build_publish_service(
    paths: StudioPaths, connection: sqlite3.Connection, *, with_agent: bool
) -> PublishService:
    """装配发布前准备服务。

    ``with_agent=False`` 时**连网关都不建**：二次校验（§06.4）一个字节都不调 LLM，
    为它去读提示词、探通道，只会让"能不能发"这件本该确定的事多几个可能失败的点。
    """
    loaded = load_config(paths)
    if not with_agent:
        return PublishService(connection, paths=paths, publish=loaded.bundle.publish)
    prompts = PromptLibrary.load(paths.prompts_dir)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        paths=paths,
        log=_gateway_log_sink(LogService(connection)),
    )
    return PublishService(
        connection,
        paths=paths,
        publish=loaded.bundle.publish,
        persona=_active_persona(),
        cover_agent=CoverAgent(gateway, prompts),
    )


@publish_app.command("cover")
def publish_cover(
    task_id: Annotated[str, typer.Option("--task", "--task-id", help="任务 id")],
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="不走 Cover Agent，直接用规则兜底文案")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """生成发布封面（§06.3）：抽帧 + 主/次文案 + 高亮，落 ``data/output/covers/``。

    抽帧点取 ``timeline.json`` 第一句的 ``start_ms + 500``（开口那一瞬）；
    文案走 Cover Agent，通道不可用 / 命中禁区 ⇒ **自动退到规则兜底**（标题当主文案）。

    封面**没有出来**时退出码 1，但那**不影响发布**：§06.3 写明没有封面就用平台首帧。
    这条命令的职责是"出一张封面"，它没做到就是没做到 —— 而发布链路不因此中断。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_publish_service(paths, connection, with_agent=not no_agent)
            report = asyncio.run(service.make_cover(CoverRequest(task_id=task_id, use_agent=not no_agent)))
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_cover(report)
    if not report.ok:
        raise typer.Exit(code=1)


def _render_cover(report: CoverReport) -> None:
    """封面结论（人读）。"""
    if report.cover_path is not None:
        console.print(f"[green]封面已出[/green] {report.cover_path}")
    else:
        console.print("[yellow]封面没出来[/yellow] —— §06.3 允许无封面发布（平台会用成片首帧）")
    console.print(
        f"[dim]抽帧 {report.frame_at_ms} ms / 成片 {report.duration_ms} ms · 文案来源 {report.source}[/dim]"
    )
    plan = report.plan
    lines = plan.get("title_lines") or []
    console.print(
        f"[dim]主文案：{plan.get('title_text')}（{plan.get('title_font_size')}pt · {len(lines)} 行）[/dim]"
    )
    if plan.get("sub_text"):
        console.print(f"[dim]次文案：{plan.get('sub_text')}[/dim]")
    if report.fallback_background:
        console.print("[yellow]底图是纯色[/yellow]：抽帧失败，封面仍可用（§06.3 第一级降级）")
    for item in report.warnings:
        console.print(f"[yellow]提示[/yellow] {item}")


@publish_app.command("precheck")
def publish_precheck(
    task_id: Annotated[str, typer.Option("--task", "--task-id", help="任务 id")],
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """发布前二次校验（§06.4）：三道门禁 + 禁区扫描。不过 ⇒ 退出码 1。

    **不静默放行**：任何一道阻断门禁没过，都会打出 ``manual_required`` 与对应的
    ``error_code``（``PRECHECK_*``）。响度那一项是**重量盘上那个成片**得出的
    （不是读当初的结论）—— 发布不可逆（R14），读数过期就等于没量。

    这条命令**不发布**：``publish.enabled=false`` 是出厂状态（R14 不可逆防护）。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_publish_service(paths, connection, with_agent=False)
            report = service.precheck(task_id)
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        _render_precheck(report)
    if not report.passed:
        raise typer.Exit(code=1)


def _render_precheck(report: PrecheckReport) -> None:
    """二次校验结论（人读）。"""
    table = Table(title=f"发布前二次校验 · {report.task_id}", show_lines=False)
    table.add_column("门禁", style="cyan", no_wrap=True)
    table.add_column("结论")
    table.add_column("阻断")
    table.add_column("说明", overflow="fold")
    for gate in report.gates:
        if gate.passed:
            mark = "[green]通过[/green]"
        elif gate.blocking:
            mark = "[red]不过[/red]"
        else:
            mark = "[yellow]建议复核[/yellow]"
        table.add_row(gate.name, mark, "是" if gate.blocking else "否", gate.detail)
    console.print(table)

    for item in report.warnings:
        console.print(f"[yellow]提示[/yellow] {item}")
    for key, value in report.diagnostics.items():
        console.print(f"[dim]诊断 {key} = {value}[/dim]")

    if report.passed:
        console.print("[green]可以发布[/green]（本次**没有真的发**：publish.enabled=false · R14）")
        return
    console.print(f"[red]拒绝发布[/red] ⇒ manual_required（error_code={report.error_code}）")
    console.print(
        "[dim]这不是“没检查”，是“检查了不合格”：修完再跑一次本命令，别绕过它 —— 发出去就收不回来了。[/dim]"
    )


@publish_app.command("dry-run")
def publish_dry_run(
    task_id: Annotated[str, typer.Option("--task", "--task-id", help="任务 id")],
    platform: Annotated[
        str, typer.Option("--platform", help="平台代号：douyin / kuaishou / shipinhao / …")
    ] = "douyin",
    account: Annotated[
        str | None, typer.Option("--account", help="账号 id（默认取该平台第一个启用的账号）")
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="live = 真平台（要登录态）/ fixture = 本地靶页（不需要）")
    ] = "live",
    probe: Annotated[
        str,
        typer.Option("--probe", help="靶页故障注入：?logged_out=1 / ?eat=emoji / ?eat=newlines / ?reject=1"),
    ] = "",
    show_browser: Annotated[
        bool, typer.Option("--show-browser", help="显示浏览器窗口（首次扫码登录 / 排障）")
    ] = False,
    title: Annotated[str | None, typer.Option("--title", help="覆盖标题（默认取稿件标题）")] = None,
    caption: Annotated[str | None, typer.Option("--caption", help="覆盖文案（默认取稿件 CTA）")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="话题，可重复传")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """发布演练（§06.5.3）：走完前七步，**停在第 ⑥ 步之前**，一个字节都不发出去。

    八步里前七步都会真的走一遍：打开创作页 → 选视频 → 填标题/文案 → 选封面 →
    **回读比对** → 截图 → 停。只有第 ⑥ 步那个"点发布"的动作被跳过。

    ``--target fixture`` 打本地靶页（``publish/fixtures/upload_form.html``）：
    **不需要账号、不需要网络**，用真浏览器把同一份流程代码跑一遍。这是在没有
    登录态时唯一能验到"第 ⑤ 步回读比对真的在比"的办法。

    这条命令**不看** ``publish.enabled``：演练的意义就是在开关还关着的时候验证链路。
    真正的保护是发布器收到 ``dry_run=True`` 后不点那个按钮（§06.5.3 第 ⑥ 步）。

    登录态没过 ⇒ 退出码 1 并打出 ``health.hint``（"需人工扫码登录" / "登录态已过期"）；
    演练本身失败 ⇒ 退出码 1 并打出 ``error_code`` 与截图路径。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            service = _build_publish_service(paths, connection, with_agent=False)
            report = asyncio.run(
                service.dry_run(
                    DryRunRequest(
                        task_id=task_id,
                        platform=platform,
                        account_id=account,
                        target=target,
                        headless=not show_browser,
                        probe=probe,
                        title=title,
                        caption=caption,
                        tags=tuple(tag or ()),
                    )
                )
            )
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
    else:
        _render_dry_run(report)
    if not report.ok:
        raise typer.Exit(code=1)


def _render_dry_run(report: DryRunReport) -> None:
    """演练结论（人读）。"""
    where = "本地靶页" if report.target == "fixture" else f"真平台 {report.platform}"
    console.print(
        f"[cyan]发布演练[/cyan] {where} · 账号 {report.account_id} · 选择器 {report.selectors_version}"
    )

    if not report.health.ready:
        console.print(f"[red]登录态没过[/red] {report.health.hint or '未登录'}")
        console.print("[dim]登录只发生在人扫码那一下（R13：不自动登录、不绕验证码）。[/dim]")
        for item in report.warnings:
            console.print(f"[yellow]提示[/yellow] {item}")
        return

    console.print(f"[green]登录态正常[/green] {report.health.account_name or report.account_id}")

    table = Table(title="这次要发的内容", show_lines=False)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("值", overflow="fold")
    table.add_row("标题", report.title)
    table.add_row("文案", report.caption or "[dim]（空）[/dim]")
    table.add_row("话题", " ".join(report.tags) or "[dim]（无）[/dim]")
    table.add_row("成片", "[dim]—[/dim]" if report.video_path is None else report.video_path.as_posix())
    cover = "（无，平台用首帧）" if report.cover_path is None else report.cover_path.as_posix()
    table.add_row("封面", cover)
    console.print(table)

    result = report.result
    if result is None:  # 只有 health 不过时才会走到这，上面已经 return 了
        return
    if result.ok:
        console.print("[green]演练通过[/green]：前七步都走通了，停在第 ⑥ 步**之前**（没点发布）")
    else:
        console.print(f"[red]演练失败[/red] {result.error_code}：{result.error_message}")
    if result.evidence is not None:
        if result.evidence.screenshot_path is not None:
            console.print(f"[dim]截图 {result.evidence.screenshot_path}[/dim]")
        if result.evidence.dom_snapshot_path is not None:
            console.print(f"[dim]DOM 快照 {result.evidence.dom_snapshot_path}[/dim]")
    console.print(f"[dim]耗时 {result.elapsed_ms} ms[/dim]")
    for item in report.warnings:
        console.print(f"[yellow]提示[/yellow] {item}")
    console.print("[dim]本次**没有真的发**：publish.enabled=false 是出厂状态（R14）。[/dim]")


@publish_app.command("enqueue")
def publish_enqueue(
    task_id: Annotated[str, typer.Option("--task", "--task-id", help="任务 id")],
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="目标平台，可重复传；缺省 = 所有启用账号所在的平台"),
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", help="账号 id（缺省 = 该平台唯一启用的那个）")
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option("--dry-run/--live", help="演练（停在第 ⑥ 步之前）；缺省 = 跟随 publish.yaml"),
    ] = None,
    scheduled_at: Annotated[
        str | None, typer.Option("--scheduled-at", help="定时发布时刻（T5.6 用，ISO 串）")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """把这条任务排进发布池（T5.3 · §03.3.10）。

    **幂等**：``(task_id, publish, publish, <平台>)`` 上有唯一约束 ⇒ "任务完成后自动投一遍
    + 人工再点一遍"不会发两次，第二次会出现在 ``skipped`` 里。

    这条命令**只投作业**，不发布、不看 ``publish.enabled``：开关关着时作业照样进队列，
    由 worker 那一侧带 ``PUBLISH_DISABLED`` 转人工（R14：不可逆的动作必须有人点头）。
    要看"投进去之后发生了什么"跑 ``studio publish queue``。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            config = load_publish_config(paths)
            report = enqueue_publications(
                connection=connection,
                task_id=task_id,
                config=config,
                platforms=tuple(platform) if platform else None,
                account_id=account,
                dry_run=dry_run,
                scheduled_at=scheduled_at,
            )
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data=report.to_dict())
        return
    if report.missing:
        console.print(f"[red]任务不存在[/red] {task_id}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]已投递[/green] {report.task_id} · {report.queued} 条作业"
        f"（平台：{'、'.join(report.platforms) or '无'}）"
    )
    for item in report.skipped:
        console.print(f"[yellow]跳过[/yellow] {item}")
    if report.queued:
        console.print("[dim]publish 池会按限频（≤3 条/天/账号 · 间隔 ≥30min）串行处理。[/dim]")


@publish_app.command("queue")
def publish_queue(
    limit: Annotated[int, typer.Option("--limit", help="每个状态最多几条")] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """发布看板：六状态计数 + 待人工那几条（T5.3 / §06.10）。

    与面板的 ``GET /api/v1/publish/queue`` 读的是**同一份数据**（同一个服务函数）——
    命令行看到"待人工 2 条"而网页上写着 3 条，那种差异查起来最费劲。
    """
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        board = build_board(connection, limit=limit)
    finally:
        connection.close()

    if json_output:
        console.print_json(data=board.to_dict())
        return

    table = Table(title="发布看板", show_lines=False)
    table.add_column("状态", style="cyan", no_wrap=True)
    table.add_column("条数", justify="right")
    for status, count in board.counts.items():
        table.add_row(status, str(count))
    console.print(table)

    manual = board.rows("manual_required")
    if not manual:
        console.print("[green]待人工队列是空的[/green]")
        return
    console.print(f"[yellow]待人工 {len(manual)} 条[/yellow]（要人做决定的那几条）：")
    for row in manual:
        console.print(
            f"  · {row.id} {row.platform}/{row.account_id} "
            f"[dim]{row.error_code or ''} {row.error_message or ''}[/dim]"
        )
        if row.evidence.get("screenshot_path"):
            console.print(f"    [dim]截图 {row.evidence['screenshot_path']}[/dim]")
    console.print("[dim]处置：studio publish retry / cancel / manual-done --publication <id>[/dim]")


@publish_app.command("retry")
def publish_retry(
    publication_id: Annotated[str, typer.Option("--publication", "--id", help="发布记录 id")],
    reason: Annotated[str | None, typer.Option("--reason", help="写进留痕的说明")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """人工重试一条发布（§06.10）：记录回 ``queued`` + 作业重排 + 写 ``audit_ops``。"""
    _publish_action(
        action="publish.retry",
        publication_id=publication_id,
        reason=reason,
        json_output=json_output,
        run=lambda connection: retry_publication(
            connection=connection,
            publication_id=publication_id,
            actor="user",
            reason=reason,
            source="cli",
        ),
        done="已重排：这条会重新走一遍发布流程（计数已归零）",
    )


@publish_app.command("cancel")
def publish_cancel(
    publication_id: Annotated[str, typer.Option("--publication", "--id", help="发布记录 id")],
    reason: Annotated[str | None, typer.Option("--reason", help="写进留痕的说明")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """人工取消一条发布（§06.10）：``canceled`` + 作废未认领的作业 + 写 ``audit_ops``。

    已经 ``published`` 的取消不了（平台上那条作品还在）—— 那种情况要人去平台处理。
    """
    _publish_action(
        action="publish.cancel",
        publication_id=publication_id,
        reason=reason,
        json_output=json_output,
        run=lambda connection: cancel_publication(
            connection=connection,
            publication_id=publication_id,
            actor="user",
            reason=reason,
            source="cli",
        ),
        done="已取消：这条不会再自动发布",
    )


@publish_app.command("manual-done")
def publish_manual_done(
    publication_id: Annotated[str, typer.Option("--publication", "--id", help="发布记录 id")],
    reason: Annotated[str, typer.Option("--reason", help="必填：人工发出去了，还是决定不发")],
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """标记"这条我处理完了"（§06.10）：从待人工队列摘下 + 写 ``audit_ops``。

    ``--reason`` **必填**：这条动作的全部信息量就是那句人话（服务层会拒空串）。
    """
    _publish_action(
        action="publish.manual_done",
        publication_id=publication_id,
        reason=reason,
        json_output=json_output,
        run=lambda connection: mark_manual_done(
            connection=connection,
            publication_id=publication_id,
            actor="user",
            reason=reason,
            source="cli",
        ),
        done="已从待人工队列摘下（留痕里记着这句说明）",
    )


def _publish_action(
    *,
    action: str,
    publication_id: str,
    reason: str | None,
    json_output: bool,
    run: Callable[[sqlite3.Connection], Any],
    done: str,
) -> None:
    """三个人工处置命令共用的外壳（与 REST 的 ``_action`` 同一条）。"""
    paths = StudioPaths.from_env()
    if not paths.db_file.is_file():
        console.print(f"[yellow]数据库尚未初始化[/yellow]：{paths.db_file}\n先跑 `studio db migrate`")
        raise typer.Exit(code=1)

    connection = connect(paths.db_file)
    try:
        try:
            row = run(connection)
        except StudioError as exc:
            _fail(exc, json_output)
            raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    if json_output:
        console.print_json(data={"action": action, "publication": row.to_dict()})
        return
    console.print(f"[green]{done}[/green] {row.id} · {row.platform}/{row.account_id} ⇒ {row.status}")


def _fail(error: StudioError, json_output: bool) -> None:
    """统一失败输出（人读 / 机读）。"""
    if json_output:
        console.print_json(data={"ok": False, **error.to_dict()})
        return
    console.print(f"[red]{error}[/red]")
    if error.remediation:
        console.print(f"[dim]修复：{error.remediation}[/dim]")


def _render_config_text(loaded: LoadedConfig) -> Table:
    """配置摘要表（人读）。"""
    bundle = loaded.bundle
    table = Table(title="配置摘要", show_lines=False)
    table.add_column("配置节", style="cyan", no_wrap=True)
    table.add_column("来源文件", overflow="fold")
    table.add_column("env 覆盖", justify="right")
    table.add_column("sha256", overflow="fold")
    for source in loaded.sources:
        table.add_row(
            source.name,
            str(source.path),
            str(len(source.env_overrides)),
            source.sha256[:12] or "-",
        )
    table.add_section()
    table.add_row("persona", f"{bundle.persona.name} · {len(bundle.persona.catchphrases)} 口癖", "", "")
    table.add_row("pools", " / ".join(f"{k}×{v.concurrency}" for k, v in bundle.pools.pools.items()), "", "")
    table.add_row(
        "outputs", f"{len(bundle.outputs.profiles)} profile · 默认 {bundle.outputs.default_profile}", "", ""
    )
    table.add_row(
        "publish", f"enabled={bundle.publish.enabled} · 账号 {len(bundle.publish.enabled_accounts)}", "", ""
    )
    table.add_row("randomization", bundle.randomization.mode, "", "")
    return table


@app.command()
def env(
    json_output: Annotated[bool, typer.Option("--json", help="输出机读 JSON")] = False,
) -> None:
    """打印环境变量重定向契约的逐项断言结果（§README.6）。"""
    report = inspect_env_contract()
    if json_output:
        console.print_json(
            data={
                "ok": report.ok,
                "missing": list(report.missing),
                "wrong_drive": list(report.wrong_drive),
                "items": {name: {"status": s, "value": v} for name, (s, v) in report.items.items()},
            }
        )
    else:
        table = Table(title="环境变量重定向契约", show_lines=False)
        table.add_column("变量", style="cyan", no_wrap=True)
        table.add_column("状态")
        table.add_column("取值", overflow="fold")
        palette = {"ok": "green", "missing": "red", "wrong_drive": "red", "not_absolute": "yellow"}
        for name, (status, value) in report.items.items():
            table.add_row(name, f"[{palette.get(status, 'white')}]{status}", value or "-")
        console.print(table)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """打印版本信息。"""
    console.print(f"studio {__version__} (spec {__spec_version__}) · python {sys.version.split()[0]}")


def main() -> None:
    """``studio`` console_scripts 入口。"""
    try:
        app()
    except StudioError as exc:
        console.print(f"[red]{exc}[/red]")
        if exc.remediation:
            console.print(f"[dim]修复：{exc.remediation}[/dim]")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
