"""启动自检（``studio doctor`` / ``GET /api/v1/doctor``）—— T1.1 验收核心。

设计原则
--------
1. **逐项断言，不抽查**：R1 的教训是"漏一个路径就写爆 C 盘"。
2. **探测而非枚举**：NVENC 不能只看 ``-encoders`` 列表（``av1_nvenc`` 在列表里
   但 sm_75 硬件不支持）⇒ 真跑 1 秒 ``testsrc`` 编码。
3. **可降级项不阻塞**：项目字体 / AV1 / LLM Key 缺失只报 ``warn``；
   磁盘水位 / 环境变量 / journal_mode 失败则 ``fail`` 并**拒绝启动**。
4. **结论可机读**：``--json`` 输出即 WS 事件 ``system.doctor`` 的载荷。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from studio.core.config import load_config
from studio.core.errors import ConfigError, DoctorGateError, ErrorCode
from studio.core.media import run_command
from studio.core.paths import StudioPaths, is_on_system_drive
from studio.core.persona_store import PersonaStore
from studio.core.settings import (
    DISK_GATE_FREE_C_BYTES,
    DISK_GATE_FREE_D_BYTES,
    EnvContractReport,
    inspect_env_contract,
)

__all__ = ["CheckResult", "CheckStatus", "Doctor", "DoctorReport", "dumps", "render_text"]

CheckStatus = Literal["ok", "warn", "fail", "skip"]

# ── 断言清单（§01.6 / §README.6 / T1.1 验收）──────────────────
REQUIRED_FILTERS: Final[tuple[str, ...]] = (
    "ass",
    "subtitles",
    "xfade",
    "loudnorm",
    "sidechaincompress",
    "overlay_cuda",
    "zscale",
    "alimiter",
    "amix",
)
NVENC_CANDIDATES: Final[tuple[str, ...]] = ("h264_nvenc", "hevc_nvenc", "av1_nvenc")
#: sm_75（Turing）无 AV1 编码器 ⇒ 探测失败属预期，不阻塞
NVENC_EXPECTED_UNAVAILABLE: Final[frozenset[str]] = frozenset({"av1_nvenc"})
REQUIRED_SYSTEM_FONTS: Final[tuple[str, ...]] = ("msyh.ttc", "simhei.ttf", "simsun.ttc")
FONT_SUFFIXES: Final[frozenset[str]] = frozenset({".ttf", ".otf", ".ttc"})
PROBE_TIMEOUT_SEC: Final[int] = 60
TORCH_IMPORT_TIMEOUT_SEC: Final[int] = 300


@dataclass(frozen=True, slots=True)
class CheckResult:
    """单项自检结论。"""

    name: str
    status: CheckStatus
    detail: str
    blocking: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    @property
    def is_blocking_failure(self) -> bool:
        return self.blocking and self.status == "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "blocking": self.blocking,
            "detail": self.detail,
            "data": self.data,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """整体自检报告（``ok`` 为唯一放行判据）。"""

    checks: tuple[CheckResult, ...]
    host: dict[str, Any]
    duration_ms: int
    started_at: str

    @property
    def ok(self) -> bool:
        return not any(check.is_blocking_failure for check in self.checks)

    @property
    def blocking_failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.is_blocking_failure)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status == "warn")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "host": self.host,
            "checks": [check.to_dict() for check in self.checks],
            "blocking_failures": [check.name for check in self.blocking_failures],
            "warnings": [check.name for check in self.warnings],
        }

    def with_extra_checks(self, extra: Sequence[CheckResult]) -> DoctorReport:
        """追加检查项后返回新报告（供**上层**注入，core 不得反向依赖 db 层）。"""
        return replace(self, checks=(*self.checks, *extra))

    def raise_if_blocked(self) -> None:
        """门禁：阻塞项失败 ⇒ 拒绝启动（``ops/start_all.ps1`` 依赖此行为）。"""
        failures = self.blocking_failures
        if failures:
            raise DoctorGateError(
                "启动自检未通过：" + "；".join(f"{c.name}（{c.detail}）" for c in failures),
                code=ErrorCode.ENV_CONTRACT_VIOLATION,
                context={"failed_checks": [c.to_dict() for c in failures]},
                remediation="逐项修复后重跑 `uv run studio doctor`",
            )


def _run(argv: Sequence[str], *, timeout: int = PROBE_TIMEOUT_SEC) -> tuple[int, str, str]:
    """执行外部命令，返回 ``(returncode, stdout, stderr)``；不可执行时返回 ``(-1, "", err)``。

    实现搬到 :func:`studio.core.media.run_command`（T4.8）：素材入库与 T3 渲染要跑
    **同一件事**，而"超时怎么报 / 要不要 CREATE_NO_WINDOW"这类细节抄三份必然会分叉。
    这里只留一个签名兼容的薄壳，8 个调用点一个字都不用改。
    """
    result = run_command(argv, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _parse_filter_names(filters_output: str) -> frozenset[str]:
    """从 ``ffmpeg -filters`` 输出解析滤镜名（行格式：``<flags> <name> <in->out> <desc>``）。"""
    names: set[str] = set()
    for raw_line in filters_output.splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith(("Filters:", "  T..", "  .S.", "  ..C", "  A ", "  V ", "  N ")):
            continue
        tokens = line.split()
        if len(tokens) >= 3 and "->" in tokens[2]:
            names.add(tokens[1])
    return frozenset(names)


def _human_gb(num_bytes: int) -> float:
    return round(num_bytes / (1024**3), 2)


def _psutil() -> Any:
    """惰性导入 psutil（保持 core 层模块级依赖最小）。"""
    import psutil  # noqa: PLC0415

    return psutil


class Doctor:
    """逐项环境自检器。

    :param paths: 路径契约（``StudioPaths.from_env()``）
    :param env: 环境变量快照；默认取 ``os.environ``（测试可注入）
    :param skip_heavy: 跳过 torch 导入等重探测（快速冒烟用）
    """

    def __init__(
        self,
        paths: StudioPaths,
        *,
        env: Mapping[str, str] | None = None,
        skip_heavy: bool = False,
    ) -> None:
        self._paths = paths
        self._env: dict[str, str] = dict(env if env is not None else os.environ)
        self._skip_heavy = skip_heavy
        self._ffmpeg = self._env.get("STUDIO_FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = self._env.get("STUDIO_FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"
        self._tts_python = self._paths.home / "tts" / ".venv" / "Scripts" / "python.exe"

    # ── 编排 ────────────────────────────────────────────────
    def run(self) -> DoctorReport:
        """执行全部检查并汇总（不抛异常；门禁由 :meth:`DoctorReport.raise_if_blocked` 负责）。"""
        from studio.core.clock import format_iso, utc_now  # noqa: PLC0415

        started = utc_now()
        began = time.perf_counter()
        checks: list[CheckResult] = [
            self._check_env_contract(),
            self._check_config(),
            self._check_persona_library(),
            self._check_paths(),
            self._check_disk_gate(),
            self._check_python_app(),
            self._check_python_tts(),
            self._check_torch_cuda(),
            self._check_ffmpeg_binary(),
            self._check_ffmpeg_filters(),
            self._check_nvenc(),
            self._check_fonts(),
            self._check_database(),
            self._check_sqlite_version(),
            self._check_ram(),
            self._check_gpu(),
            self._check_node(),
            self._check_uv(),
        ]
        return DoctorReport(
            checks=tuple(checks),
            host=self._host_info(),
            duration_ms=int((time.perf_counter() - began) * 1000),
            started_at=format_iso(started),
        )

    # ── 1. 环境变量契约（§README.6 · 10 项逐项）─────────────
    def _check_env_contract(self) -> CheckResult:
        report: EnvContractReport = inspect_env_contract(self._env)
        items = {name: {"status": status, "value": value} for name, (status, value) in report.items.items()}
        if report.missing:
            return CheckResult(
                name="env.contract",
                status="fail",
                blocking=True,
                detail=f"{len(report.missing)} 项环境变量缺失：{', '.join(report.missing)}",
                data={"items": items},
                remediation=". .\\scripts\\env.ps1（或 Copy-Item .env.example .env 后修改）",
            )
        if report.wrong_drive:
            return CheckResult(
                name="env.contract",
                status="fail",
                blocking=True,
                detail=f"{len(report.wrong_drive)} 项指向系统盘：{', '.join(report.wrong_drive)}",
                data={"items": items, "system_drive": self._env.get("SystemDrive", "C:")},
                remediation="把缓存 / 临时目录全部指向非系统盘（R1：系统盘空间不足会全站停摆）",
            )
        return CheckResult(
            name="env.contract",
            status="ok",
            blocking=True,
            detail="10 项路径变量全部就位且均不在系统盘",
            data={"items": items},
        )

    # ── 2. 配置校验（persona 缺字段 / 越界路径 / 局域网无密码）──
    def _check_config(self) -> CheckResult:
        """8 份 YAML 强校验；任何一项失败都**拒绝启动**（含局域网鉴权底线）。"""
        try:
            loaded = load_config(self._paths, env=self._env)
        except ConfigError as exc:
            return CheckResult(
                name="config.valid",
                status="fail",
                blocking=True,
                detail=f"{exc.code}：{exc.message}",
                data={"code": str(exc.code), **exc.context},
                remediation=exc.remediation,
            )
        return CheckResult(
            name="config.valid",
            status="ok",
            blocking=True,
            detail=(
                f"8 份配置校验通过（persona={loaded.bundle.persona.name}，"
                f"pools={'/'.join(f'{k}×{v.concurrency}' for k, v in loaded.bundle.pools.pools.items())}）"
            ),
            data={
                "sources": [source.to_dict() for source in loaded.sources],
                "warnings": list(loaded.warnings),
            },
        )

    # ── 3. 人物库体检（热重载的配套：坏条目在切换前就暴露）────
    def _check_persona_library(self) -> CheckResult:
        """列出人物库并报告无效条目（**warn**：库文件坏了不影响激活人物）。"""
        try:
            store = PersonaStore(self._paths)
            snapshot = store.current()
            entries = store.library()
        except ConfigError as exc:
            # 激活人物本身读不出来 ⇒ 由 `config.valid`（阻塞项）负责报错，
            # 这里只做非阻塞提示，避免同一个问题产生两条阻塞失败。
            return CheckResult(
                name="persona.library",
                status="warn",
                blocking=False,
                detail=f"激活人物不可读（{exc.code}），详见 config.valid",
                data={"error": exc.to_dict()},
            )
        broken = [entry for entry in entries if not entry.valid]
        data = {
            "active": snapshot.to_dict(),
            "library": [entry.to_dict() for entry in entries],
            "broken": [entry.persona_id for entry in broken],
        }
        base = f"激活人物 {snapshot.persona_id}（{snapshot.config.name}）· 库 {len(entries)} 条"
        if broken:
            return CheckResult(
                name="persona.library",
                status="warn",
                blocking=False,
                detail=f"{base}，其中 {len(broken)} 条无效："
                + "；".join(f"{entry.persona_id}（{entry.error}）" for entry in broken),
                data=data,
                remediation="修正 config/personas/ 下对应文件（studio persona validate 可复现）",
            )
        return CheckResult(
            name="persona.library",
            status="ok",
            blocking=False,
            detail=base,
            data=data,
        )

    # ── 4. 运行期目录 ───────────────────────────────────────
    def _check_paths(self) -> CheckResult:
        try:
            created = self._paths.ensure_runtime_dirs()
        except OSError as exc:
            return CheckResult(
                name="paths.runtime",
                status="fail",
                blocking=True,
                detail=f"运行期目录创建失败：{exc}",
                remediation="检查磁盘权限与剩余空间",
            )
        return CheckResult(
            name="paths.runtime",
            status="ok",
            blocking=True,
            detail=f"{len(self._paths.runtime_dirs())} 个运行期目录就位（本次新建 {len(created)} 个）",
            data={"created": [str(p) for p in created]},
        )

    # ── 5. 磁盘水位门禁 ─────────────────────────────────────
    def _check_disk_gate(self) -> CheckResult:
        psutil = _psutil()
        system_drive = self._env.get("SystemDrive", "C:")
        data_drive = self._paths.data_dir.drive or system_drive
        usage: dict[str, Any] = {}
        try:
            for label, drive in (("C", system_drive), ("D", data_drive)):
                du = psutil.disk_usage(f"{drive}\\")
                usage[label] = {
                    "drive": drive,
                    "free_gb": _human_gb(du.free),
                    "total_gb": _human_gb(du.total),
                    "percent": du.percent,
                }
        except OSError as exc:
            return CheckResult(
                name="disk.gate",
                status="fail",
                blocking=True,
                detail=f"磁盘容量探测失败：{exc}",
                remediation="确认盘符存在且可访问",
            )

        free_c = int(usage["C"]["free_gb"] * 1024**3)
        free_d = int(usage["D"]["free_gb"] * 1024**3)
        gate_c = int(psutil.disk_usage(f"{system_drive}\\").free) >= DISK_GATE_FREE_C_BYTES
        gate_d = int(psutil.disk_usage(f"{data_drive}\\").free) >= DISK_GATE_FREE_D_BYTES
        detail = (
            f"C 盘可用 {usage['C']['free_gb']} GB（门禁 ≥1 GB）、"
            f"数据盘 {usage['D']['drive']} 可用 {usage['D']['free_gb']} GB（门禁 ≥15 GB）"
        )
        if not (gate_c and gate_d):
            return CheckResult(
                name="disk.gate",
                status="fail",
                blocking=True,
                detail=detail,
                data={"usage": usage, "free_c": free_c, "free_d": free_d},
                remediation="清理磁盘或迁移数据目录后再启动（门禁不通过 ⇒ 拒绝启动）",
            )
        return CheckResult(
            name="disk.gate",
            status="ok",
            blocking=True,
            detail=detail,
            data={"usage": usage, "free_c": free_c, "free_d": free_d},
        )

    # ── 6/7/8. Python 与 torch ──────────────────────────────
    def _check_python_app(self) -> CheckResult:
        version = platform.python_version()
        major, minor, _ = platform.python_version_tuple()
        if (major, minor) == ("3", "12"):
            return CheckResult(name="python.app", status="ok", blocking=True, detail=f"app 解释器 {version}")
        return CheckResult(
            name="python.app",
            status="fail",
            blocking=True,
            detail=f"app 解释器为 {version}，要求 3.12.x",
            remediation="uv venv --python 3.12 .venv && uv sync --extra dev",
        )

    def _check_python_tts(self) -> CheckResult:
        if not self._tts_python.exists():
            return CheckResult(
                name="python.tts",
                status="fail",
                blocking=True,
                detail=f"未找到 tts 解释器：{self._tts_python}",
                remediation="uv venv --python 3.11 tts\\.venv",
            )
        code, out, err = _run([str(self._tts_python), "-V"])
        if code != 0:
            return CheckResult(
                name="python.tts",
                status="fail",
                blocking=True,
                detail=f"tts 解释器无法启动：{err.strip() or out.strip()}",
                remediation="uv venv --python 3.11 tts\\.venv",
            )
        raw = (out or err).strip().replace("Python ", "")
        if raw.startswith("3.11"):
            return CheckResult(
                name="python.tts", status="ok", blocking=True, detail=f"tts 解释器 {raw}（torch cp311 匹配）"
            )
        return CheckResult(
            name="python.tts",
            status="fail",
            blocking=True,
            detail=f"tts 解释器为 {raw}，要求 3.11.x（D:\\Torch 的 wheel 为 cp311）",
            remediation="uv venv --python 3.11 tts\\.venv && uv sync --project tts",
        )

    def _check_torch_cuda(self) -> CheckResult:
        if self._skip_heavy:
            return CheckResult(name="torch.cuda", status="skip", detail="--skip-heavy 已跳过")
        if not self._tts_python.exists():
            return CheckResult(
                name="torch.cuda",
                status="skip",
                detail="tts 解释器缺失，跳过",
            )
        snippet = (
            "import torch;"
            "print(torch.__version__, torch.cuda.is_available(),"
            " torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
        )
        code, out, err = _run([str(self._tts_python), "-c", snippet], timeout=TORCH_IMPORT_TIMEOUT_SEC)
        message = (err or out).strip()
        if code != 0:
            last_line = message.splitlines()[-1] if message else "未知"
            return CheckResult(
                name="torch.cuda",
                status="fail",
                blocking=True,
                detail=f"torch 导入失败：{last_line}",
                remediation="uv sync --project tts（wheel 来源 D:\\Torch）",
            )
        parts = out.strip().split()
        version = parts[0] if parts else "?"
        available = len(parts) > 1 and parts[1] == "True"
        device = parts[2] if len(parts) > 2 else "-"
        detail = f"torch {version} / cuda_available={available} / {device}"
        if version.startswith("2.4.0") and available:
            return CheckResult(
                name="torch.cuda", status="ok", blocking=True, detail=detail, data={"device": device}
            )
        return CheckResult(
            name="torch.cuda",
            status="fail",
            blocking=True,
            detail=detail,
            remediation="期望 2.4.0+cu121 且 cuda_available=True；检查显卡驱动与 D:\\Torch wheel",
        )

    # ── 9/10/11. FFmpeg ─────────────────────────────────────
    def _check_ffmpeg_binary(self) -> CheckResult:
        code, out, err = _run([self._ffmpeg, "-hide_banner", "-version"])
        if code != 0:
            return CheckResult(
                name="ffmpeg.binary",
                status="fail",
                blocking=True,
                detail=f"ffmpeg 无法启动：{err.strip() or out.strip()}",
                remediation="安装 full build（gyan.dev）并确保 ffmpeg 在 PATH 中",
            )
        first_line = (out or err).splitlines()[0] if (out or err).strip() else ""
        fingerprint = next(
            (line.strip() for line in (out or err).splitlines() if line.startswith("configuration:")),
            "",
        )
        if "full_build" not in first_line and "--enable-libass" not in fingerprint:
            return CheckResult(
                name="ffmpeg.binary",
                status="warn",
                blocking=False,
                detail=f"{first_line}（非 full build，libass/CUDA 可能缺失）",
                data={"version_line": first_line},
                remediation="换用 gyan.dev full build",
            )
        return CheckResult(
            name="ffmpeg.binary",
            status="ok",
            blocking=True,
            detail=first_line,
            data={"version_line": first_line},
        )

    def _check_ffmpeg_filters(self) -> CheckResult:
        code, out, err = _run([self._ffmpeg, "-hide_banner", "-filters"])
        if code != 0:
            return CheckResult(
                name="ffmpeg.filters",
                status="fail",
                blocking=True,
                detail=f"无法列出滤镜：{err.strip()}",
                remediation="确认 ffmpeg 可执行且为 full build",
            )
        available = _parse_filter_names(out or err)
        missing = [name for name in REQUIRED_FILTERS if name not in available]
        present = [name for name in REQUIRED_FILTERS if name in available]
        if missing:
            return CheckResult(
                name="ffmpeg.filters",
                status="fail",
                blocking=True,
                detail=f"缺少必需滤镜：{', '.join(missing)}",
                data={"present": present, "missing": missing, "total_filters": len(available)},
                remediation="换用带 libass + CUDA 的 full build",
            )
        return CheckResult(
            name="ffmpeg.filters",
            status="ok",
            blocking=True,
            detail=f"{len(REQUIRED_FILTERS)} 个必需滤镜全部在位（构建共 {len(available)} 个）",
            data={"present": present, "total_filters": len(available)},
        )

    def _check_nvenc(self) -> CheckResult:
        """运行时探测：真跑 1 秒 testsrc 编码，而非只读 ``-encoders`` 列表。"""
        if self._skip_heavy:
            return CheckResult(name="ffmpeg.nvenc", status="skip", detail="--skip-heavy 已跳过")
        results: dict[str, bool] = {}
        for encoder in NVENC_CANDIDATES:
            code, _, _ = _run(
                [
                    self._ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x240:rate=30",
                    "-t",
                    "1",
                    "-c:v",
                    encoder,
                    "-f",
                    "null",
                    "-",
                ]
            )
            results[encoder] = code == 0

        usable = [name for name, ok in results.items() if ok]
        if "h264_nvenc" in usable:
            return CheckResult(
                name="ffmpeg.nvenc",
                status="ok",
                blocking=False,
                detail=f"硬件编码可用：{', '.join(usable)}（探测结果 {results}）",
                data={"probe": results},
            )
        if usable:
            return CheckResult(
                name="ffmpeg.nvenc",
                status="warn",
                blocking=False,
                detail=f"h264_nvenc 不可用，仅 {', '.join(usable)} 可用",
                data={"probe": results},
                remediation="输出 profile 将回落 libx264",
            )
        return CheckResult(
            name="ffmpeg.nvenc",
            status="warn",
            blocking=False,
            detail=f"NVENC 全部不可用（{results}）⇒ 渲染走 libx264（CPU +40%）",
            data={"probe": results},
            remediation="检查驱动版本 ≥535；不影响启动（§1.7 降级）",
        )

    # ── 12. 字体 ────────────────────────────────────────────
    def _check_fonts(self) -> CheckResult:
        windows_dir = Path(self._env.get("WINDIR", r"C:\Windows"))
        fonts_dir = windows_dir / "Fonts"
        system_present = [name for name in REQUIRED_SYSTEM_FONTS if (fonts_dir / name).exists()]
        project_fonts = [
            p
            for p in sorted(self._paths.templates_dir.glob("*/assets/fonts/*"))
            if p.suffix.lower() in FONT_SUFFIXES
        ]
        data = {
            "system_present": system_present,
            "system_expected": list(REQUIRED_SYSTEM_FONTS),
            "project_fonts": [str(p) for p in project_fonts],
        }
        if not system_present:
            return CheckResult(
                name="fonts.available",
                status="fail",
                blocking=True,
                detail=f"系统中文回退字体缺失（{fonts_dir}）",
                data=data,
                remediation="安装微软雅黑 / 黑体 / 宋体，或把字体放入 templates/<tid>/assets/fonts/",
            )
        if not project_fonts:
            return CheckResult(
                name="fonts.available",
                status="warn",
                blocking=False,
                detail=f"系统字体 {len(system_present)} 个可用；项目内置字体缺失（libass 需内置，见 E8）",
                data=data,
                remediation="把授权中文字体放入 templates/douyin_9x16_default/assets/fonts/（T3.5 前完成）",
            )
        return CheckResult(
            name="fonts.available",
            status="ok",
            blocking=True,
            detail=f"系统字体 {len(system_present)} 个 + 项目内置 {len(project_fonts)} 个",
            data=data,
        )

    # ── 13. 数据库可写 + WAL ────────────────────────────────
    def _check_database(self) -> CheckResult:
        probe = self._paths.data_dir / "_doctor_probe.db"
        try:
            probe.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(probe)
            try:
                journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                connection.execute("CREATE TABLE IF NOT EXISTS probe(x INTEGER)")
                connection.execute("INSERT INTO probe VALUES (1)")
                connection.commit()
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return CheckResult(
                name="db.writable",
                status="fail",
                blocking=True,
                detail=f"数据库不可写：{exc}",
                remediation="检查 STUDIO_DATA_DIR 权限；禁止放在网络盘 / OneDrive / SMB",
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                leftover = Path(str(probe) + suffix)
                if leftover.exists():
                    leftover.unlink(missing_ok=True)

        if journal_mode != "wal" or integrity != "ok":
            return CheckResult(
                name="db.writable",
                status="fail",
                blocking=True,
                detail=f"journal_mode={journal_mode} integrity={integrity}（要求 wal / ok）",
                data={"journal_mode": journal_mode, "integrity": integrity},
                remediation="数据库文件必须放在本地文件系统（WAL 需共享内存）",
            )
        return CheckResult(
            name="db.writable",
            status="ok",
            blocking=True,
            detail=f"数据目录可写，journal_mode=wal，integrity_check=ok（{self._paths.data_dir}）",
            data={
                "journal_mode": journal_mode,
                "integrity": integrity,
                "probe_dir": str(self._paths.data_dir),
            },
        )

    def _check_sqlite_version(self) -> CheckResult:
        version = sqlite3.sqlite_version
        parts = tuple(int(p) for p in version.split(".")[:3])
        if parts >= (3, 35, 0):
            return CheckResult(
                name="sqlite.version",
                status="ok",
                blocking=True,
                detail=f"SQLite {version}（RETURNING 可用，队列单语句认领依赖）",
            )
        return CheckResult(
            name="sqlite.version",
            status="fail",
            blocking=True,
            detail=f"SQLite {version} 过低（< 3.35，无 RETURNING）",
            remediation="更换 Python 运行时（内置 sqlite 版本随解释器）",
        )

    # ── 14/15/16/17. 资源与工具链 ───────────────────────────
    def _check_ram(self) -> CheckResult:
        psutil = _psutil()
        mem = psutil.virtual_memory()
        total_gb = _human_gb(mem.total)
        data = {"total_gb": total_gb, "available_gb": _human_gb(mem.available), "percent": mem.percent}
        if total_gb >= 16:
            return CheckResult(
                name="system.ram", status="ok", blocking=False, detail=f"内存 {total_gb} GB", data=data
            )
        return CheckResult(
            name="system.ram",
            status="warn",
            blocking=False,
            detail=f"内存仅 {total_gb} GB（推荐 ≥16 GB，TTS 推理可能 OOM）",
            data=data,
            remediation="降低 TTS 并发或改跑 CPU（§1.7 降级）",
        )

    def _check_gpu(self) -> CheckResult:
        code, out, err = _run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ]
        )
        if code != 0 or not out.strip():
            return CheckResult(
                name="gpu.nvidia",
                status="warn",
                blocking=False,
                detail=f"nvidia-smi 不可用：{(err or out).strip() or '未安装'}",
                remediation="GPU 不可用时 TTS 走 CPU（极慢，仅单条验证）",
            )
        row = [cell.strip() for cell in out.strip().splitlines()[0].split(",")]
        name = row[0] if row else "?"
        driver = row[1] if len(row) > 1 else "?"
        total_mib = int(float(row[2])) if len(row) > 2 else 0
        used_mib = int(float(row[3])) if len(row) > 3 else 0
        data = {
            "name": name,
            "driver_version": driver,
            "memory_total_mib": total_mib,
            "memory_used_mib": used_mib,
            "memory_free_mib": total_mib - used_mib,
        }
        if total_mib >= 8192:
            return CheckResult(
                name="gpu.nvidia",
                status="ok",
                blocking=False,
                detail=f"{name} / 驱动 {driver} / {used_mib} MiB 已用 of {total_mib} MiB",
                data=data,
            )
        return CheckResult(
            name="gpu.nvidia",
            status="warn",
            blocking=False,
            detail=f"{name} 显存 {total_mib} MiB < 8 GB，TTS 需 fp16 且降并发",
            data=data,
        )

    def _check_node(self) -> CheckResult:
        code, out, err = _run(["node", "--version"])
        if code != 0:
            return CheckResult(
                name="node.version",
                status="warn",
                blocking=False,
                detail="未找到 Node.js（仅前端构建需要，可零构建降级）",
                remediation="安装 Node ≥20",
            )
        version = (out or err).strip()
        return CheckResult(name="node.version", status="ok", blocking=False, detail=f"Node {version}")

    def _check_uv(self) -> CheckResult:
        """uv 版本 + **缓存目录落盘位置**（R1 加固：uv 会静默写 GB 级缓存）。"""
        code, out, err = _run(["uv", "--version"])
        if code != 0:
            return CheckResult(
                name="uv.version",
                status="warn",
                blocking=False,
                detail="未找到 uv",
                remediation="安装 uv 后重跑 uv sync",
            )
        version = (out or err).strip()

        cache_code, cache_out, cache_err = _run(["uv", "cache", "dir"])
        cache_dir = (cache_out or cache_err).strip()
        if cache_code != 0 or not cache_dir:
            return CheckResult(
                name="uv.version",
                status="warn",
                blocking=False,
                detail=f"{version}（无法解析 uv cache dir）",
                remediation="设置 UV_CACHE_DIR 到非系统盘后重跑",
            )
        if is_on_system_drive(cache_dir):
            return CheckResult(
                name="uv.version",
                status="fail",
                blocking=True,
                detail=f"{version}；uv 缓存落在系统盘：{cache_dir}",
                data={"cache_dir": cache_dir},
                remediation="设置 UV_CACHE_DIR 到非系统盘（. .\\scripts\\env.ps1）",
            )
        return CheckResult(
            name="uv.version",
            status="ok",
            blocking=False,
            detail=f"{version}；uv 缓存 {cache_dir}",
            data={"cache_dir": cache_dir},
        )

    # ── 主机指纹 ────────────────────────────────────────────
    def _host_info(self) -> dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "cpu_logical": os.cpu_count(),
            "studio_home": str(self._paths.home),
            "data_dir": str(self._paths.data_dir),
        }


def render_text(report: DoctorReport) -> str:
    """把报告渲染为人类可读文本（rich 由 CLI 负责着色，此处保持纯文本）。"""
    icon = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}
    lines = [f"studio doctor · {'PASS' if report.ok else 'BLOCKED'}  ({report.duration_ms} ms)"]
    for check in report.checks:
        lines.append(f"{icon.get(check.status, '[????]')} {check.name:<20} {check.detail}")
        if check.remediation:
            lines.append(f"          └─ 修复：{check.remediation}")
    if not report.ok:
        lines.append("")
        lines.append("阻塞项：" + "、".join(c.name for c in report.blocking_failures))
    return "\n".join(lines)


def dumps(report: DoctorReport) -> str:
    """序列化为 JSON 字符串（``--json`` / WS ``system.doctor`` 载荷）。"""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
