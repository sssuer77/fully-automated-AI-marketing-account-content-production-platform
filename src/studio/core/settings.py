"""环境变量重定向契约（§README.6）—— 防写爆系统盘的第一道闸门。

契约表（11 项硬断言 + 1 项密钥可缺）::

    STUDIO_HOME             仓库根（相对路径基准）
    STUDIO_DATA_DIR         媒资 / DB 主目录          ← 必须离开系统盘
    HF_HOME                 HuggingFace 权重缓存       ← 必须离开系统盘
    MODELSCOPE_CACHE        CosyVoice 权重缓存         ← 必须离开系统盘
    TORCH_HOME              torch 预训练缓存           ← 必须离开系统盘
    UV_CACHE_DIR            uv 缓存                    ← 必须离开系统盘
    PIP_CACHE_DIR           pip 缓存                   ← 必须离开系统盘
    PLAYWRIGHT_BROWSERS_PATH Playwright 运行时（约 150 MB）← 必须离开系统盘
    NPM_CONFIG_CACHE        npm 缓存（前端依赖，T4.1）  ← 必须离开系统盘
    TMP / TEMP              FFmpeg 临时文件 / 滤镜图    ← 必须离开系统盘
    STUDIO_LLM_API_KEY      密钥（不入库、不入 git）    ← 可缺（E6，非阻塞）

门禁（§README.6）：``free_C < 1 GB`` 或 ``free_D < 15 GB`` ⇒ 拒绝启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from studio.core.errors import ConfigError, ErrorCode
from studio.core.paths import is_on_system_drive, system_drive

__all__ = [
    "DISK_GATE_FREE_C_BYTES",
    "DISK_GATE_FREE_D_BYTES",
    "OFF_SYSTEM_DRIVE_FIELDS",
    "REQUIRED_PATH_VARS",
    "EnvContractReport",
    "EnvSettings",
    "VarStatus",
    "inspect_env_contract",
    "load_env_settings",
]

# ── 磁盘门禁阈值（§README.6 / §README.7）──────────────────────
DISK_GATE_FREE_C_BYTES: Final[int] = 1 * 1024**3
DISK_GATE_FREE_D_BYTES: Final[int] = 15 * 1024**3

# ── 必须离开系统盘的字段（R1 核心约束）────────────────────────
OFF_SYSTEM_DRIVE_FIELDS: Final[tuple[str, ...]] = (
    "studio_data_dir",
    "hf_home",
    "modelscope_cache",
    "torch_home",
    "uv_cache_dir",
    "pip_cache_dir",
    "playwright_browsers_path",
    "npm_config_cache",
    "tmp",
    "temp",
)

# ── 11 项硬断言路径变量（doctor 逐项断言依据）─────────────────
REQUIRED_PATH_VARS: Final[tuple[str, ...]] = (
    "STUDIO_HOME",
    "STUDIO_DATA_DIR",
    "HF_HOME",
    "MODELSCOPE_CACHE",
    "TORCH_HOME",
    "UV_CACHE_DIR",
    "PIP_CACHE_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "NPM_CONFIG_CACHE",
    "TMP",
    "TEMP",
)

VarStatus = Literal["ok", "missing", "wrong_drive", "not_absolute"]


class EnvSettings(BaseSettings):
    """环境变量契约的强类型视图（字段名即变量名，大小写不敏感）。"""

    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        env_file=None,
        validate_default=True,
    )

    studio_home: Path
    studio_data_dir: Path
    hf_home: Path
    modelscope_cache: Path
    torch_home: Path
    uv_cache_dir: Path
    pip_cache_dir: Path
    playwright_browsers_path: Path
    npm_config_cache: Path
    tmp: Path
    temp: Path

    studio_llm_api_key: SecretStr | None = None
    studio_llm_base_url: str | None = None
    studio_webui_password: SecretStr | None = None

    @field_validator(
        "studio_home",
        "studio_data_dir",
        "hf_home",
        "modelscope_cache",
        "torch_home",
        "uv_cache_dir",
        "pip_cache_dir",
        "playwright_browsers_path",
        "npm_config_cache",
        "tmp",
        "temp",
        mode="after",
    )
    @classmethod
    def _must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"必须是绝对路径，收到：{value}")
        return value

    @property
    def off_system_drive_paths(self) -> dict[str, Path]:
        """需要接受"非系统盘"检查的字段映射。"""
        return {name: getattr(self, name) for name in OFF_SYSTEM_DRIVE_FIELDS}

    def system_drive_violations(self) -> list[str]:
        """返回落在系统盘上的字段名（空列表 = 通过）。"""
        return [name for name, path in self.off_system_drive_paths.items() if is_on_system_drive(path)]

    def has_llm_key(self) -> bool:
        return self.studio_llm_api_key is not None and bool(self.studio_llm_api_key.get_secret_value())


def load_env_settings(env: dict[str, str] | None = None) -> EnvSettings:
    """加载并**强校验**环境变量；任何缺失 / 非法 / 落系统盘 ⇒ 抛 :class:`ConfigError`。

    这是应用启动路径（``studio serve`` / Worker）的入口；
    自检路径请改用 :func:`inspect_env_contract`，它不抛异常、逐项给结论。
    """
    source = env if env is not None else dict(os.environ)
    missing = [name for name in REQUIRED_PATH_VARS if not source.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "环境变量缺失，拒绝启动（先执行 . .\\scripts\\env.ps1 或复制 .env.example 为 .env）",
            code=ErrorCode.ENV_MISSING,
            context={"missing": missing, "required": list(REQUIRED_PATH_VARS)},
            remediation=". .\\scripts\\env.ps1",
        )

    try:
        payload = {name.lower(): source[name] for name in REQUIRED_PATH_VARS} | _optional(source)
        settings = EnvSettings.model_validate(payload)
    except ValueError as exc:
        raise ConfigError(
            "环境变量取值非法",
            code=ErrorCode.ENV_CONTRACT_VIOLATION,
            context={"error": str(exc)},
            remediation="检查 .env 中各项是否为绝对路径",
        ) from exc

    violations = settings.system_drive_violations()
    if violations:
        raise ConfigError(
            "环境变量指向系统盘，拒绝启动（R1：系统盘空间不足会导致全站停摆）",
            code=ErrorCode.ENV_NOT_ON_D_DRIVE,
            context={
                "violations": {name: str(getattr(settings, name)) for name in violations},
                "system_drive": system_drive(),
            },
            remediation="把 TMP/TEMP/HF_HOME/MODELSCOPE_CACHE/TORCH_HOME/UV_CACHE_DIR/"
            "PIP_CACHE_DIR/PLAYWRIGHT_BROWSERS_PATH/NPM_CONFIG_CACHE/STUDIO_DATA_DIR 全部指向非系统盘",
        )
    return settings


def _optional(source: dict[str, str]) -> dict[str, str]:
    """收集非必填变量（有值才传，避免空串污染 Optional 字段）。"""
    picked: dict[str, str] = {}
    for name in ("STUDIO_LLM_API_KEY", "STUDIO_LLM_BASE_URL", "STUDIO_WEBUI_PASSWORD"):
        value = source.get(name, "").strip()
        if value:
            picked[name.lower()] = value
    return picked


@dataclass(frozen=True, slots=True)
class EnvContractReport:
    """逐项环境变量断言结果（doctor 直接消费）。"""

    items: dict[str, tuple[VarStatus, str]]
    missing: tuple[str, ...]
    wrong_drive: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.wrong_drive


def inspect_env_contract(env: dict[str, str] | None = None) -> EnvContractReport:
    """**不抛异常**地逐项断言 11 个路径变量（doctor 专用）。"""
    source = env if env is not None else dict(os.environ)
    items: dict[str, tuple[VarStatus, str]] = {}
    missing: list[str] = []
    wrong_drive: list[str] = []

    for name in REQUIRED_PATH_VARS:
        raw = source.get(name, "").strip()
        if not raw:
            items[name] = ("missing", "")
            missing.append(name)
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            items[name] = ("not_absolute", raw)
            continue
        if name == "STUDIO_HOME":
            items[name] = ("ok", str(candidate))
            continue
        if is_on_system_drive(candidate):
            items[name] = ("wrong_drive", str(candidate))
            wrong_drive.append(name)
            continue
        items[name] = ("ok", str(candidate))

    return EnvContractReport(items=items, missing=tuple(missing), wrong_drive=tuple(wrong_drive))
