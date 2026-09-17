"""配置系统（T1.2 · §01.2 / §README.6）—— 8 份 YAML 的强类型契约与加载器。

设计要点
--------
1. **`extra="forbid"`**：未知键直接报错。拼错键若被静默忽略，配置就等于没生效，
   这类故障在运行时极难定位，必须在加载期拦截。
2. **env > yaml**：环境变量覆盖层用 ``STUDIO_CFG__<文件>__<路径>__<键>``
   （双下划线表示嵌套），值按 JSON 解析，失败则当字符串。
3. **密钥不入库**：``llm.yaml`` 只允许出现环境变量**名**（``api_key_env``）；
   写进密钥本体 ⇒ 加载期直接拒绝（:data:`SECRET_VALUE_PATTERN`）。
4. **越界路径拒绝**：``app.paths`` 与 ``outputs.watermark.path`` 必须落在
   仓库内且不在系统盘（R1），防止"配置一改就写爆 C 盘"。
5. **错误必带文件路径**：缺字段时报错信息直接给出该 YAML 的绝对路径与模板路径，
   满足"persona 缺字段 ⇒ 启动即报错并打印模板路径"。

文件清单（§02.2）::

    config/persona.yaml       频道定位（唯一人工必填）
    config/llm.yaml           LLM 双通道
    config/app.yaml           路径 / 保留期 / 超时 / 确认闸 / 定时
    config/pools.yaml         四池并发 · 租约 · 退避
    config/outputs.yaml       编码 profile / 水印 / 音频 / 字幕 / BGM
    config/randomization.yaml 防搬运随机化九维
    config/publish.yaml       平台 / 账号 / 限频 / 发布前校验
    config/logging.yaml       日志级别与脱敏
    config/secrets.yaml       密钥（**不入库**；secrets.example.yaml 为模板）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)

from studio.core.errors import ConfigError, ErrorCode
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths, is_on_system_drive

__all__ = [
    "CONFIG_FILE_NAMES",
    "ENV_OVERRIDE_PREFIX",
    "POOL_CONCURRENCY_MAX",
    "POOL_CONCURRENCY_MIN",
    "SCHEMA_VERSION",
    "SECRETS_FILE_NAME",
    "WATCHDOG_SERVICES",
    "AppConfig",
    "ConfigBundle",
    "ConfigSource",
    "LlmConfig",
    "LoadedConfig",
    "LoggingConfig",
    "OutputsConfig",
    "PersonaConfig",
    "PoolsConfig",
    "PublishConfig",
    "RandomizationConfig",
    "RuntimeSettings",
    "SecretsConfig",
    "WatchdogConfig",
    "concurrency_bounds",
    "load_app_config",
    "load_config",
    "load_outputs_config",
    "load_persona_file",
    "load_pools_config",
    "load_publish_config",
    "load_runtime_settings",
    "redact",
    "set_auto_approve_policy",
]

logger = get_logger("studio.core.config")

SCHEMA_VERSION: Final[str] = "1.0"

#: 环境变量覆盖层前缀（与 T1.1 的 STUDIO_HOME 等契约**不冲突**）
ENV_OVERRIDE_PREFIX: Final[str] = "STUDIO_CFG__"

#: 8 份配置文件的文件名（不含扩展名），顺序即加载顺序
#: 密钥文件名（不入库；可被 STUDIO_CFG__SECRETS__* 覆盖）
SECRETS_FILE_NAME: Final[str] = "secrets"

CONFIG_FILE_NAMES: Final[tuple[str, ...]] = (
    "persona",
    "llm",
    "app",
    "pools",
    "outputs",
    "randomization",
    "publish",
    "logging",
)

#: 密钥本体识别（写进 yaml 即视为泄漏）
SECRET_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(sk-[A-Za-z0-9_\-]{8,}|sk_[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|eyJ[A-Za-z0-9_\-]{10,})"
)

#: dump 时需脱敏的键名片段（小写匹配）
REDACT_KEY_PARTS: Final[tuple[str, ...]] = ("password", "api_key", "secret", "token", "cookie")

#: 错误信息里提示的模板文件（缺字段时打印）
TEMPLATE_FILES: Final[dict[str, str]] = {
    "persona": "config/persona.example.yaml",
    "secrets": "config/secrets.example.yaml",
}

_SchemaVersion = Annotated[str, Field(pattern=r"^\d+\.\d+$")]

#: 配置内的路径：dump 时统一输出正斜杠（与 config/*.yaml 的写法一致）
_ConfigPath = Annotated[
    Path, PlainSerializer(lambda path: path.as_posix(), return_type=str, when_used="json")
]


class _Base(BaseModel):
    """所有配置模型的基类：未知键报错 + 冻结后不可变。

    ``schema_version`` **只属于文件级模型**（见 :class:`_FileConfig`），
    不下沉到子节 —— 否则 dump 出来的每一节都会多一个 ``schema_version``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _FileConfig(_Base):
    """文件级模型：额外要求顶层 ``schema_version``。"""

    schema_version: _SchemaVersion = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version：{value}（本版仅支持 {SCHEMA_VERSION}）")
        return value


# ══════════════════════════════════════════════════════════════════════
# 1. persona.yaml —— 频道定位（唯一人工必填）
# ══════════════════════════════════════════════════════════════════════


class PersonaConfig(_FileConfig):
    """频道定位。对应 ``personas`` 表（§03.3.1）。"""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    is_active: bool = True
    role_desc: str = Field(min_length=4)
    tone: str = Field(min_length=2)
    audience: str = Field(min_length=2)
    catchphrases: list[str] = Field(min_length=2, max_length=20)
    forbidden: list[str] = Field(min_length=1, max_length=200)
    style_hint: str = ""
    target_chars_min: int = Field(default=600, ge=100, le=5000)
    target_chars_max: int = Field(default=800, ge=100, le=8000)
    max_duration_ms: int = Field(default=180_000, ge=10_000, le=1_800_000)

    @model_validator(mode="after")
    def _range_is_sane(self) -> PersonaConfig:
        if self.target_chars_min >= self.target_chars_max:
            raise ValueError("target_chars_min 必须小于 target_chars_max")
        if any(not word.strip() for word in self.catchphrases):
            raise ValueError("catchphrases 不得含空串")
        if any(not word.strip() for word in self.forbidden):
            raise ValueError("forbidden 不得含空串")
        return self


# ══════════════════════════════════════════════════════════════════════
# 2. llm.yaml —— 双通道网关
# ══════════════════════════════════════════════════════════════════════

LlmEngine = Literal["oi_compatible", "ollama", "llama_cpp"]
BudgetOnExceed = Literal["switch_to_local", "fail_task", "alert_only"]


class LlmProfileConfig(_Base):
    """单个通道（云端 / 本地）。"""

    engine: LlmEngine
    base_url: str = Field(min_length=1)
    api_key_env: str | None = None
    model: str = Field(min_length=1)
    timeout_sec: int = Field(default=120, ge=5, le=900)
    max_retries: int = Field(default=3, ge=0, le=10)
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    json_mode: bool = True
    cost_per_1k_in: float = Field(default=0.0, ge=0.0)
    cost_per_1k_out: float = Field(default=0.0, ge=0.0)

    @field_validator("api_key_env")
    @classmethod
    def _must_be_env_name(cls, value: str | None) -> str | None:
        """★ 密钥铁律：这里只能是**环境变量名**，不能是密钥本体。"""
        if value is None:
            return None
        if SECRET_VALUE_PATTERN.match(value):
            raise ValueError(
                "api_key_env 必须是环境变量名（如 STUDIO_LLM_API_KEY），不得写入密钥本体；密钥请放进环境变量"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"api_key_env 不是合法环境变量名：{value}")
        return value


class LlmRoutingItem(_Base):
    profile: str = Field(min_length=1)
    fallback: str | None = None


class LlmBudgetConfig(_Base):
    per_task_token_limit: int = Field(default=60_000, ge=1000)
    per_day_cost_usd_limit: float = Field(default=5.0, ge=0.0)
    on_exceed: BudgetOnExceed = "switch_to_local"


class LlmConfig(_FileConfig):
    """LLM 双通道（§01.2.4）。"""

    default_profile: str = Field(min_length=1)
    profiles: dict[str, LlmProfileConfig] = Field(min_length=1)
    routing: dict[str, LlmRoutingItem] = Field(min_length=1)
    budget: LlmBudgetConfig = Field(default_factory=LlmBudgetConfig)
    probe_local_on_start: bool = True

    @model_validator(mode="after")
    def _references_resolve(self) -> LlmConfig:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile 未在 profiles 中定义：{self.default_profile}")
        for agent, item in self.routing.items():
            if item.profile not in self.profiles:
                raise ValueError(f"routing.{agent}.profile 未定义：{item.profile}")
            if item.fallback is not None and item.fallback not in self.profiles:
                raise ValueError(f"routing.{agent}.fallback 未定义：{item.fallback}")
        return self


# ══════════════════════════════════════════════════════════════════════
# 3. app.yaml —— 路径 / 保留期 / 超时 / 确认闸 / 定时
# ══════════════════════════════════════════════════════════════════════

AutoApprovePolicy = Literal["off", "grade_a", "grade_ab"]


class WebConfig(_Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    allow_lan: bool = False
    cors_origins: list[str] = Field(default_factory=list)
    openapi_enabled: bool = True


class PathOverrides(_Base):
    """可选路径覆盖；留空用 :class:`StudioPaths` 默认值。

    硬约束（由 :func:`_guard_paths` 强制）：不得指向系统盘，且必须落在仓库内。
    """

    work_dir: _ConfigPath | None = None
    output_dir: _ConfigPath | None = None
    cache_dir: _ConfigPath | None = None
    logs_dir: _ConfigPath | None = None

    def items_set(self) -> tuple[tuple[str, Path], ...]:
        return tuple(
            (name, value)
            for name in ("work_dir", "output_dir", "cache_dir", "logs_dir")
            if (value := getattr(self, name)) is not None
        )


class ApprovalConfig(_Base):
    auto_approve_policy: AutoApprovePolicy = "grade_a"
    reject_requires_comment: bool = True
    approval_ttl_hours: int = Field(default=0, ge=0, le=8760)


class HandoffConfig(_Base):
    enabled: bool = False
    adapter: str = "local"
    output_dir: _ConfigPath = Path("data/handoff")


class RetentionConfig(_Base):
    sentence_audio_hours: int = Field(default=24, ge=1)
    voice_master_hours: int = Field(default=24, ge=1)
    scene_artifact_hours: int = Field(default=168, ge=1)
    system_logs_days: int = Field(default=30, ge=1)
    debug_logs_days: int = Field(default=7, ge=1)
    llm_calls_days: int = Field(default=90, ge=1)
    task_events_days: int = Field(default=90, ge=1)
    hot_archive_days: int = Field(default=90, ge=1)
    tts_cache_max_gb: float = Field(default=5.0, gt=0.0)


class TimeoutConfig(_Base):
    llm_sec: int = Field(default=120, ge=5)
    tts_sentence_sec: int = Field(default=60, ge=5)
    render_final_sec: int = Field(default=600, ge=10)
    task_sec: int = Field(default=1800, ge=60)


class SchedulerConfig(_Base):
    enabled: bool = True
    gc_cron: str = "0 4 * * *"
    backup_cron: str = "0 3 * * *"
    backup_daily_keep: int = Field(default=7, ge=1)
    backup_weekly_keep: int = Field(default=4, ge=0)


class DiskGateConfig(_Base):
    free_c_min_gb: float = Field(default=1.0, ge=0.0)
    free_d_min_gb: float = Field(default=15.0, ge=0.0)
    pause_pools_on_low: bool = True


class PipelineConfig(_Base):
    """流水线编排开关（§04.3.4 · 冲突 C7）。

    ``streaming_render`` 是"边配音边渲染"（原文 §3.3）的开关。**默认关，而且一期
    只能关**：成片时长以人声实测总长为**唯一**基准（C12），而边渲染边改人声会让这个
    基准在渲染途中漂移 —— 出来的是"某一句还没念完时"的时长。§04.3.4 把它留作可选
    能力，等二期场景化之后再评估。

    那为什么还要有这个键（而不是干脆不写）？因为"默认关闭"这件事本身要被**验**：
    配置面板、``studio config dump``、集成测试都读同一个键；没有键，那个"默认"
    就只是一句注释。
    """

    streaming_render: bool = False


class AppConfig(_FileConfig):
    """应用配置（§01.2.5 / §03.7.5 / §04.4.4）。"""

    web: WebConfig = Field(default_factory=WebConfig)
    paths: PathOverrides = Field(default_factory=PathOverrides)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    disk_gate: DiskGateConfig = Field(default_factory=DiskGateConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


# ══════════════════════════════════════════════════════════════════════
# 4. pools.yaml —— 四池并发 · 租约 · 退避
# ══════════════════════════════════════════════════════════════════════

PoolName = Literal["draft", "voice", "render", "publish"]
UnitType = Literal["task", "sentence", "scene", "final", "publish"]


class PoolConfig(_Base):
    """单池参数（§01.4.4）。"""

    unit_type: UnitType
    concurrency: int = Field(ge=1, le=16)
    poll_ms: int = Field(ge=50, le=60_000)
    lease_sec: int = Field(ge=10, le=7200)
    backoff_base_ms: int = Field(ge=50, le=600_000)
    backoff_max_ms: int = Field(ge=100, le=3_600_000)
    unit_timeout_sec: int = Field(ge=5, le=7200)
    max_attempts: int = Field(ge=1, le=10)
    priority: int = Field(ge=0, le=1000)
    daily_limit_per_account: int | None = Field(default=None, ge=1, le=100)
    min_gap_min: int | None = Field(default=None, ge=0, le=1440)

    @model_validator(mode="after")
    def _backoff_is_ordered(self) -> PoolConfig:
        if self.backoff_base_ms >= self.backoff_max_ms:
            raise ValueError("backoff_base_ms 必须小于 backoff_max_ms")
        # 注：允许 lease_sec < unit_timeout_sec（§1.4.4 的 render 即 600/900）。
        # 长单元靠 worker **续租**（T1.5）而非一次性长租约来避免误回收。
        return self


class PoolGlobalConfig(_Base):
    enabled: bool = True
    gpu_mutex_name: str = "Global\\studio_gpu_lock"
    render_threads: int = Field(default=5, ge=1, le=64)


class AutoConcurrencyConfig(_Base):
    enabled: bool = True
    oom_threshold: int = Field(default=2, ge=1, le=10)
    recover_after_min: int = Field(default=5, ge=1, le=120)
    gpu_mem_high_ratio: float = Field(default=0.60, ge=0.0, le=1.0)


class DeadLetterConfig(_Base):
    on_dead_alert: bool = True


#: 无人值守守护**允许**守护的进程名（与 `service_manager.SERVICE_NAMES` 同源）。
#: 写在这里而不是 import 那个常量：`core` 是最底层，不能反向 import `services`。
WATCHDOG_SERVICES: Final[tuple[str, ...]] = ("api", "tts", "draft", "voice", "render")


class WatchdogConfig(_Base):
    """无人值守守护（T4.11 · §01.2.1 / §03.7.5）。

    阈值可配是**硬要求**（T4.11 降级预案）：水位门禁误判时，改这一节就能改判据，
    不必改代码、不必重跑迁移。
    """

    enabled: bool = True
    tick_sec: float = Field(default=5.0, ge=1.0, le=60.0)
    #: 守哪些进程。默认五个都写进来 —— "守护 5 进程"是 §01.2.1 的原话；
    #: 但守护者自己所在的那个进程**它守不了**（见 `WatchdogService`），
    #: 运行时会把那一个标成 `self_guarded` 并在面板上说明。
    guard_services: tuple[str, ...] = WATCHDOG_SERVICES
    restart_base_ms: int = Field(default=5_000, ge=100, le=600_000)
    restart_max_ms: int = Field(default=300_000, ge=1_000, le=3_600_000)
    #: **窗口内**允许的重启次数上限；超限 ⇒ 停手 + 告警（防重启风暴）。
    restart_limit: int = Field(default=20, ge=1, le=1000)
    restart_window_sec: int = Field(default=600, ge=60, le=86_400)
    #: 重启后等就绪的预算。**刻意短**：守护自己也要活着 —— 一次重启最多占这么久。
    restart_ready_timeout_sec: float = Field(default=6.0, ge=1.0, le=60.0)
    #: 磁盘水位低时暂停认领的池（§01.7「free_D < 15 GB ⇒ 暂停 render/publish 认领」）
    disk_gate_pools: tuple[str, ...] = ("render", "publish")
    #: `tasks.attempt_count` 达到几次 ⇒ `manual_pool`（§1.7「反复失败进入人工池」）
    manual_pool_after: int = Field(default=3, ge=1, le=10)

    @field_validator("guard_services")
    @classmethod
    def _known_services(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [name for name in value if name not in WATCHDOG_SERVICES]
        if unknown:
            raise ValueError(f"未知进程名：{unknown}（合法：{list(WATCHDOG_SERVICES)}）")
        return value

    @field_validator("disk_gate_pools")
    @classmethod
    def _known_pools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [name for name in value if name not in POOL_CONCURRENCY_MAX]
        if unknown:
            raise ValueError(f"未知池名：{unknown}（合法：{sorted(POOL_CONCURRENCY_MAX)}）")
        return value


class PoolsConfig(_FileConfig):
    """四池配置。key 必须覆盖 draft/voice/render/publish（§01.4.4）。"""

    global_: PoolGlobalConfig = Field(default_factory=PoolGlobalConfig, alias="global")
    pools: dict[PoolName, PoolConfig]
    auto_concurrency: AutoConcurrencyConfig = Field(default_factory=AutoConcurrencyConfig)
    dead_letter: DeadLetterConfig = Field(default_factory=DeadLetterConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _all_pools_present(self) -> PoolsConfig:
        missing = {"draft", "voice", "render", "publish"} - set(self.pools)
        if missing:
            raise ValueError(f"pools 缺少必要池：{sorted(missing)}")
        return self


#: 并发旋钮的**下限**（T4.10 · §03.4.4）。
#: DDL 的 ``CHECK (concurrency BETWEEN 0 AND 8)`` 允许 0，但 WebUI **拒绝** 0：
#: 它看着像个并发数，行为却是"沉默的暂停"—— 与 ``paused=1`` 在面板上无法区分，
#: 而"暂停"有它自己的开关、自己的留痕、自己的语义说明（裁定 145）。
POOL_CONCURRENCY_MIN: Final[int] = 1

#: 并发旋钮的**硬上限**（按池）。与 DDL 的 8 同向但更严：
#: - 8 是"任何写入者都不得越过的**物理**上限"（DDL 兜底）；
#: - voice 3 是 8GB 显存下的**工程**上限（§01.4.4「T2.2 bench 后回写」）。
#: 两处不一致时以**本表**为准（它是旋钮真正会拦的那一份），DDL 只做最后一道保险。
POOL_CONCURRENCY_MAX: Final[Mapping[PoolName, int]] = {
    "draft": 8,
    "voice": 3,
    "render": 8,
    "publish": 8,
}


def concurrency_bounds(pool: str) -> tuple[int, int]:
    """池的并发上下限 ``(min, max)``（未知池名 ⇒ :class:`ConfigError`）。"""
    for name, ceiling in POOL_CONCURRENCY_MAX.items():
        if name == pool:
            return POOL_CONCURRENCY_MIN, ceiling
    raise ConfigError(
        f"未知池名：{pool}",
        code=ErrorCode.CONFIG_INVALID,
        context={"pool": pool, "valid": sorted(POOL_CONCURRENCY_MAX)},
        remediation="池名只能是 draft / voice / render / publish",
    )


def load_outputs_config(path: Path) -> OutputsConfig:
    """只读**一份** `config/outputs.yaml`（编码 profile / 水印 / 音频 / 字幕 / BGM）。

    参数是**文件路径**而不是 :class:`~studio.core.paths.StudioPaths`（与
    :func:`load_persona_file` 一致，与 :func:`load_pools_config` 不同）：合成配置面板
    与热重载仓库都要能对着"任意一份同名文件"读 —— 测试里读的是临时家目录下那一份，
    生产读的是仓库里那一份，两边走**同一条**校验路径，不会出现"测试放行、生产报错"。

    为什么不用 :func:`load_config`
    ------------------------------
    与 :func:`load_pools_config` 同一条理由：面板每次请求都要知道"当前这一档是什么"，
    走 :func:`load_config` 会把 9 份 YAML 全读一遍并全量校验 ⇒ `llm.yaml` 里少一个 key
    会让**合成配置面板打不开**。两件事之间没有任何关系。

    代价同样如实说明：`outputs.local.yaml` 覆盖层不生效（那是启动路径的机制），
    控制台读的是"盘上那份基准" —— 与 :func:`load_runtime_settings` 同一取舍。
    """
    model = _validate("outputs", _read_yaml(path), path)
    assert isinstance(model, OutputsConfig)
    return model


def load_pools_config(paths: StudioPaths) -> PoolsConfig:
    """只读 ``config/pools.yaml``（**只碰这一份文件**）。

    为什么不用 :func:`load_config`
    ------------------------------
    四池控制台（T4.10）每次请求都要知道"优先级 / 单元类型 / OOM 阈值"。走
    :func:`load_config` 会把 9 份 YAML 全读一遍并全量校验 —— 于是 ``llm.yaml``
    里少一个 key，**四池面板就打不开**。两件事之间没有任何关系，这种耦合
    （"改配置 B 弄坏了面板 A"）是最难查的一类故障。

    代价：``pools.local.yaml`` 覆盖层不生效。那是**启动路径**的机制（``load_config``
    负责），控制台读的是"盘上那份基准" —— 与 :func:`load_runtime_settings` 同一取舍。
    """
    path = paths.config_dir / "pools.yaml"
    model = _validate("pools", _read_yaml(path), path)
    assert isinstance(model, PoolsConfig)
    return model


def load_publish_config(paths: StudioPaths) -> PublishConfig:
    """只读 ``config/publish.yaml``（**只碰这一份文件**）。

    与 :func:`load_pools_config` 同一条理由：发布面板（T5.3 的"待人工"区块 / T5.5 的
    七区块）每次刷新都要知道"有哪些平台、哪些账号、开关状态"。走 :func:`load_config`
    会把 9 份 YAML 全读一遍并全量校验 —— 于是 ``llm.yaml`` 里少一个 key，
    **发布面板就打不开**，而这两件事之间没有任何关系。

    代价：``publish.local.yaml`` 覆盖层不生效（那是**启动路径**的机制，
    ``load_config`` 负责）。面板读的是"盘上那份基准" —— 与 :func:`load_pools_config`
    同一取舍。
    """
    path = paths.config_dir / "publish.yaml"
    model = _validate("publish", _read_yaml(path), path)
    assert isinstance(model, PublishConfig)
    return model


def load_app_config(paths: StudioPaths) -> AppConfig:
    """只读 ``config/app.yaml``（路径 / 保留期 / 超时 / 确认闸 / 定时）。

    与 :func:`load_pools_config` 同一条理由：媒资 GC（T4.12）每次跑都要知道
    "句子音频留多久 / TTS 缓存上限几个 G"。走 :func:`load_config` 会把 9 份 YAML
    全读一遍并全量校验 —— 于是 ``llm.yaml`` 里少一个 key，**每日 GC 直接停摆**，
    而它本该是"无人值守地清垃圾"的那件事。

    代价：``app.local.yaml`` 覆盖层不生效（那是**启动路径**的机制）。
    """
    path = paths.config_dir / "app.yaml"
    model = _validate("app", _read_yaml(path), path)
    assert isinstance(model, AppConfig)
    return model


# ══════════════════════════════════════════════════════════════════════
# 5. outputs.yaml —— 编码 profile / 水印 / 音频 / 字幕 / BGM
# ══════════════════════════════════════════════════════════════════════

VideoCodec = Literal["libx264", "h264_nvenc", "hevc_nvenc"]
ColorSpace = Literal["bt709", "bt601", "bt2020"]
#: 水印位置（§04.2.8.2）。`center` 忽略边距 —— 居中时边距没有意义。
WatermarkPosition = Literal["top_left", "top_right", "bottom_left", "bottom_right", "center"]

#: 水印宽度上限的除数（§04.2.8.2：禁止超过画布 1/4）
CANVAS_WIDTH_DIVISOR: Final[int] = 4


class EncodingProfileConfig(_Base):
    """单个输出 profile（§01.8.2）。"""

    width: int = Field(ge=64, le=7680)
    height: int = Field(ge=64, le=7680)
    fps: int = Field(default=30, ge=1, le=120)
    vcodec: VideoCodec = "libx264"
    crf: int | None = Field(default=None, ge=0, le=51)
    preset: str | None = None
    nvenc_preset: str | None = None
    cq: int | None = Field(default=None, ge=0, le=51)
    pix_fmt: Literal["yuv420p", "yuv420p10le"] = "yuv420p"
    colorspace: ColorSpace = "bt709"
    faststart: bool = True
    gop: int = Field(default=60, ge=1, le=600)
    acodec: Literal["aac", "libmp3lame"] = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = Field(default=48_000, ge=8000, le=192_000)
    audio_channels: Literal[1, 2] = 2
    platforms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _encoder_params_and_alignment(self) -> EncodingProfileConfig:
        if self.width % 2 or self.height % 2:
            raise ValueError(f"画布宽高必须为偶数（yuv420p 色度对齐）：{self.width}x{self.height}")
        if self.vcodec == "libx264" and self.crf is None:
            raise ValueError("vcodec=libx264 时必须指定 crf")
        if self.vcodec != "libx264" and self.cq is None:
            raise ValueError(f"vcodec={self.vcodec} 时必须指定 cq")
        return self


class WatermarkConfig(_Base):
    """固定水印（T3.2）。

    **缺失 ⇒ 跳过水印层继续出片**，不是拒绝渲染 —— 装饰品不该成为整条链路的
    单点阻塞（``render/watermark.py`` 的模块注释里有这段来龙去脉）。
    """

    path: _ConfigPath
    position: WatermarkPosition = "bottom_right"
    margin_x: int = Field(default=48, ge=0, le=2000)
    margin_y: int = Field(default=96, ge=0, le=2000)
    width_ratio: float = Field(default=0.22, gt=0.0, le=0.25)
    opacity: float = Field(default=0.85, ge=0.0, le=1.0)

    @field_validator("margin_x", "margin_y")
    @classmethod
    def _margins_must_be_even(cls, value: int) -> int:
        if value % 2:
            raise ValueError(f"水印边距必须为偶数（避免 overlay 1px 偏移）：{value}")
        return value

    def width_px_raw(self, canvas_width: int) -> int:
        """夹取前的像素宽。

        面板显示"配置里这个比例对应多宽"、编译器判断"这一档有没有被夹过"，
        用的都是这个数。
        """
        return round(self.width_ratio * canvas_width)

    def width_px_for(self, canvas_width: int) -> int:
        """最终像素宽 = **夹取到画布 1/4** + **取偶**（T3.2 · §04.2.8.2）。

        两个动作各自的理由：

        - **夹取**：规格写死"禁止超过画布 1/4"。``width_ratio`` 的上限已经是 0.25，
          但 ``round()`` 在奇数画布宽下仍会越界 1px（画布 1078 ⇒ round(269.5)=270 > 1078//4=269）；
        - **取偶**：``overlay`` 的 x/y 必须为偶数（yuv420p 色度对齐），而右下角的
          x 是 ``W - w - margin_x``（W 与 margin 都已偶数）⇒ 只有 w 取偶才能保证 x 偶。

        口径**只此一处**：面板（``services/outputs_service.py``）与编译器
        （``render/watermark.py``）都调这个方法，不许各写一份 —— 否则会出现
        "面板说 238、真正渲染时用 236"这种对不上的错。
        """
        width = min(self.width_px_raw(canvas_width), canvas_width // CANVAS_WIDTH_DIVISOR)
        return max(width - width % 2, 2)


class AudioConfig(_Base):
    voice_gain_db: float = Field(default=0.0, ge=-30.0, le=30.0)
    bgm_gain_db: float = Field(default=-21.0, ge=-60.0, le=10.0)
    duck_threshold: float = Field(default=0.05, gt=0.0, le=1.0)
    duck_ratio: float = Field(default=8.0, ge=1.0, le=20.0)
    duck_attack_ms: int = Field(default=20, ge=1, le=2000)
    duck_release_ms: int = Field(default=420, ge=1, le=9000)
    target_lufs: float = Field(default=-16.0, ge=-30.0, le=-5.0)
    true_peak_dbtp: float = Field(default=-1.0, ge=-9.0, le=0.0)
    tail_ms: int = Field(default=600, ge=0, le=10_000)


class SafeAreaConfig(_Base):
    """文字安全区（§03-data-model 模板 ``safe_area``，单位 = 画布像素）。

    字幕、标题卡这类"文字类组件"必须落在安全区内：抖音底部的点赞/评论条会盖住
    画面下方约 420px，标题区会盖住上方约 220px。写在配置里而不是代码常量里，
    是因为换平台（视频号 / B 站）这几个数不一样。
    """

    top: int = Field(default=220, ge=0, le=2000)
    bottom: int = Field(default=420, ge=0, le=2000)
    left: int = Field(default=60, ge=0, le=2000)
    right: int = Field(default=60, ge=0, le=2000)


class SubtitleConfig(_Base):
    """烧进画面的字幕（T3.5 · §04.2.6）。

    两个字段的口径值得单独说：

    - ``margin_bottom`` 是"字幕底边距画布底部的像素"。它与 ``safe_area.bottom``
      取 **max** 之后才是 ASS 的 ``MarginV``（§04.2.6 的"MarginV ≥ safe_area.bottom"）。
      配置里写小于安全区的值不会出事，只会被抬上来 —— 但默认值就直接写 420，
      免得"配置说 260、实际渲 420"这种对不上的事发生。
    - ``font_name`` 必须与**真实存在的字体家族名**一致，否则 libass 会画出一排
      豆腐块。这里默认写 Windows 自带的「微软雅黑」：本项目的 TTS 走 SAPI、
      进程管理走 taskkill，本来就是 Windows 专用，挑一个本机一定有的字体比
      引用一个"应该存在"的开源字体名更稳。要换成自己的字体，把 .ttf/.otf 放进
      ``templates/<模板>/assets/fonts/`` 再改这里。
    """

    enabled: bool = True
    font_name: str = "Microsoft YaHei"
    font_size: int = Field(default=64, ge=16, le=200)
    outline: int = Field(default=4, ge=0, le=20)
    shadow: int = Field(default=2, ge=0, le=20)
    margin_bottom: int = Field(default=420, ge=0, le=2000)
    max_chars_per_line: int = Field(default=13, ge=4, le=60)
    max_lines: int = Field(default=2, ge=1, le=6)
    safe_area: SafeAreaConfig = Field(default_factory=SafeAreaConfig)


class BgmConfig(_Base):
    enabled: bool = True
    glob: str = "data/assets/bgm/*.{mp3,m4a,wav,flac}"
    avoid_recent_tasks: int = Field(default=5, ge=0, le=50)
    start_offset_max_ms: int = Field(default=15_000, ge=0, le=120_000)


class OutputsConfig(_FileConfig):
    """输出 profile 与合成参数（§01.8.2 / §04.2.8）。"""

    default_profile: str = Field(min_length=1)
    profiles: dict[str, EncodingProfileConfig] = Field(min_length=1)
    watermark: WatermarkConfig
    audio: AudioConfig = Field(default_factory=AudioConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    bgm: BgmConfig = Field(default_factory=BgmConfig)

    @model_validator(mode="after")
    def _default_profile_exists(self) -> OutputsConfig:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile 未在 profiles 中定义：{self.default_profile}")
        return self

    def profile_for_platform(self, platform: str) -> EncodingProfileConfig:
        """按平台反查 profile；无匹配则回落到 ``default_profile``。"""
        for profile in self.profiles.values():
            if platform in profile.platforms:
                return profile
        return self.profiles[self.default_profile]


# ══════════════════════════════════════════════════════════════════════
# 6. randomization.yaml —— 防搬运随机化九维
# ══════════════════════════════════════════════════════════════════════

RandomizationMode = Literal["standard", "aggressive"]
SeedStrategy = Literal["task_template_attempt", "task_only", "random"]
SimilarityOnFail = Literal["resample", "warn"]


class SeedConfig(_Base):
    strategy: SeedStrategy = "task_template_attempt"
    attempt_max: int = Field(default=3, ge=1, le=10)


class ClipSelectionDim(_Base):
    enabled: bool = True
    avoid_recent_tasks: int = Field(default=10, ge=0, le=100)
    allow_reuse_in_same_video: bool = False
    min_usable_ms: int = Field(default=1500, ge=0, le=600_000)


class InPointDim(_Base):
    enabled: bool = True
    edge_guard_ms: int = Field(default=1500, ge=0, le=60_000)


class CutRhythmDim(_Base):
    enabled: bool = True
    min_segment_ms: int = Field(default=2500, ge=500, le=60_000)
    max_segment_ms: int = Field(default=5500, ge=500, le=120_000)
    hard_min_ms: int = Field(default=1500, ge=100, le=60_000)

    @model_validator(mode="after")
    def _ordered(self) -> CutRhythmDim:
        if self.hard_min_ms > self.min_segment_ms or self.min_segment_ms >= self.max_segment_ms:
            raise ValueError("要求 hard_min_ms ≤ min_segment_ms < max_segment_ms")
        return self


class GeometryDim(_Base):
    enabled: bool = False
    zoom_min: float = Field(default=1.02, ge=1.0, le=2.0)
    zoom_max: float = Field(default=1.12, ge=1.0, le=2.0)
    roi_jitter_ratio: float = Field(default=0.10, ge=0.0, le=0.5)
    mirror_probability: float = Field(default=0.35, ge=0.0, le=1.0)
    speed_min: float = Field(default=0.92, ge=0.5, le=2.0)
    speed_max: float = Field(default=1.10, ge=0.5, le=2.0)

    @model_validator(mode="after")
    def _ordered(self) -> GeometryDim:
        if self.zoom_min > self.zoom_max:
            raise ValueError("zoom_min 必须 ≤ zoom_max")
        if self.speed_min > self.speed_max:
            raise ValueError("speed_min 必须 ≤ speed_max")
        return self


class ColorDim(_Base):
    enabled: bool = False
    brightness_delta: float = Field(default=0.03, ge=0.0, le=0.5)
    contrast_delta: float = Field(default=0.06, ge=0.0, le=0.5)
    saturation_delta: float = Field(default=0.08, ge=0.0, le=0.5)
    gamma_delta: float = Field(default=0.04, ge=0.0, le=0.5)
    lut_pool: list[str] = Field(default_factory=list)


class TextureDim(_Base):
    enabled: bool = False
    noise_strength_pool: list[int] = Field(default_factory=lambda: [0, 4, 6])
    vignette_min: float = Field(default=0.15, ge=0.0, le=1.0)
    vignette_max: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> TextureDim:
        if self.vignette_min > self.vignette_max:
            raise ValueError("vignette_min 必须 ≤ vignette_max")
        if any(strength < 0 or strength > 20 for strength in self.noise_strength_pool):
            raise ValueError("noise_strength_pool 取值须在 [0, 20]（强度上限硬编码）")
        return self


class TransitionDim(_Base):
    enabled: bool = True
    pool: list[str] = Field(min_length=1)
    min_ms: int = Field(default=300, ge=0, le=5000)
    max_ms: int = Field(default=700, ge=0, le=5000)
    forbid_consecutive_same: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> TransitionDim:
        if self.min_ms > self.max_ms:
            raise ValueError("min_ms 必须 ≤ max_ms")
        return self


class RandomAudioDim(_Base):
    enabled: bool = True
    bgm_random: bool = True
    bgm_start_offset_max_ms: int = Field(default=15_000, ge=0, le=120_000)
    voice_layer_random: bool = False


class EncoderJitterDim(_Base):
    enabled: bool = True
    cq_delta: int = Field(default=2, ge=0, le=10)
    crf_delta: int = Field(default=1, ge=0, le=10)
    gop_min: int = Field(default=48, ge=1, le=600)
    gop_max: int = Field(default=72, ge=1, le=600)
    quality_floor_cq: int = Field(default=25, ge=0, le=51)

    @model_validator(mode="after")
    def _ordered(self) -> EncoderJitterDim:
        if self.gop_min > self.gop_max:
            raise ValueError("gop_min 必须 ≤ gop_max")
        return self


class DimensionsConfig(_Base):
    """九维随机化矩阵（§04.2.4.4）。standard 档只启用 1/2/3/7/8/9。"""

    clip_selection: ClipSelectionDim = Field(default_factory=ClipSelectionDim)
    in_point: InPointDim = Field(default_factory=InPointDim)
    cut_rhythm: CutRhythmDim = Field(default_factory=CutRhythmDim)
    geometry: GeometryDim = Field(default_factory=GeometryDim)
    color: ColorDim = Field(default_factory=ColorDim)
    texture: TextureDim = Field(default_factory=TextureDim)
    transition: TransitionDim
    audio: RandomAudioDim = Field(default_factory=RandomAudioDim)
    encoder_jitter: EncoderJitterDim = Field(default_factory=EncoderJitterDim)

    def aggressive_dimensions(self) -> tuple[str, ...]:
        """返回 aggressive 档专属维度名（standard 档必须为关闭态）。"""
        return ("geometry", "color", "texture")

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "clip_selection",
                "in_point",
                "cut_rhythm",
                "geometry",
                "color",
                "texture",
                "transition",
                "audio",
                "encoder_jitter",
            )
            if getattr(self, name).enabled
        )


class SimilarityAuditConfig(_Base):
    """相似度审计门禁（§04.2.4.5 · R3）。"""

    enabled: bool = True
    broll_overlap_max: float = Field(default=0.30, ge=0.0, le=1.0)
    phash_hamming_max: int = Field(default=8, ge=0, le=64)
    phash_high_similar_ratio_max: float = Field(default=0.05, ge=0.0, le=1.0)
    ssim_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    audio_similarity_max: float = Field(default=0.60, ge=0.0, le=1.0)
    on_fail: SimilarityOnFail = "resample"


class RandomizationConfig(_FileConfig):
    mode: RandomizationMode = "standard"
    seed: SeedConfig = Field(default_factory=SeedConfig)
    dimensions: DimensionsConfig
    similarity_audit: SimilarityAuditConfig = Field(default_factory=SimilarityAuditConfig)

    @model_validator(mode="after")
    def _standard_mode_keeps_heavy_dims_off(self) -> RandomizationConfig:
        if self.mode != "standard":
            return self
        leaked = [
            name for name in self.dimensions.aggressive_dimensions() if getattr(self.dimensions, name).enabled
        ]
        if leaked:
            raise ValueError(
                f"mode=standard 时 aggressive 专属维度必须关闭，当前开启：{leaked}"
                "（改用 mode: aggressive 或把对应 enabled 置 false）"
            )
        return self


# ══════════════════════════════════════════════════════════════════════
# 7. publish.yaml —— 平台 / 账号 / 限频 / 发布前校验
# ══════════════════════════════════════════════════════════════════════

PlatformCode = Literal["douyin", "kuaishou", "shipinhao", "xiaohongshu", "bilibili", "xigua", "weibo"]


class PublishHandoffConfig(_Base):
    enabled: bool = False
    adapter: str = "local"


class AccountConfig(_Base):
    """发布账号（D1：结构支持多账号，默认单账号）。"""

    account_id: str = Field(min_length=1, max_length=64)
    platform: PlatformCode
    display_name: str = ""
    profile_dir: _ConfigPath
    enabled: bool = True
    daily_limit: int = Field(default=3, ge=1, le=100)
    min_gap_min: int = Field(default=30, ge=0, le=1440)


class PlatformConfig(_Base):
    """平台差异参数（§06.2.2）。"""

    publisher: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    enabled: bool = False
    title_max: int = Field(ge=1, le=5000)
    caption_max: int = Field(ge=1, le=20_000)
    tag_syntax: str = "#{tag}"
    tag_max: int = Field(default=5, ge=0, le=50)
    daily_limit: int = Field(default=3, ge=1, le=100)
    min_gap_min: int = Field(default=30, ge=0, le=1440)
    cover_required: bool = False
    selectors_version: str = ""

    @field_validator("tag_syntax")
    @classmethod
    def _syntax_has_placeholder(cls, value: str) -> str:
        if "{tag}" not in value:
            raise ValueError(f"tag_syntax 必须含 {{tag}} 占位符：{value}")
        return value


class PrecheckConfig(_Base):
    """发布前二次校验（§06.4 三道门禁）。"""

    require_watermark: bool = True
    lufs_min: float = Field(default=-16.5, ge=-40.0, le=0.0)
    lufs_max: float = Field(default=-15.5, ge=-40.0, le=0.0)
    true_peak_max_dbtp: float = Field(default=-1.0, ge=-20.0, le=0.0)
    block_on_similarity: bool = False
    scan_forbidden_words: bool = True

    @model_validator(mode="after")
    def _lufs_range_ordered(self) -> PrecheckConfig:
        if self.lufs_min >= self.lufs_max:
            raise ValueError("lufs_min 必须小于 lufs_max")
        return self


class PublishConfig(_FileConfig):
    """发布配置（§06.2.3 · R13 / R14）。"""

    enabled: bool = False
    require_confirm: bool = True
    dry_run: bool = False
    handoff: PublishHandoffConfig = Field(default_factory=PublishHandoffConfig)
    accounts: list[AccountConfig] = Field(default_factory=list)
    platforms: dict[PlatformCode, PlatformConfig]
    metrics_schedule_hours: list[int] = Field(default_factory=lambda: [1, 6, 24, 72])
    precheck: PrecheckConfig = Field(default_factory=PrecheckConfig)

    @model_validator(mode="after")
    def _consistency(self) -> PublishConfig:
        account_ids = [account.account_id for account in self.accounts]
        if len(set(account_ids)) != len(account_ids):
            raise ValueError(f"accounts 中 account_id 重复：{account_ids}")
        if len({str(a.profile_dir).lower() for a in self.accounts}) != len(self.accounts):
            raise ValueError("accounts 中 profile_dir 重复（登录态必须按账号隔离）")
        for account in self.accounts:
            if account.enabled and account.platform not in self.platforms:
                raise ValueError(f"账号 {account.account_id} 的平台 {account.platform} 未在 platforms 中定义")
        if any(hour <= 0 or hour > 24 * 30 for hour in self.metrics_schedule_hours):
            raise ValueError("metrics_schedule_hours 取值须在 (0, 720] 小时")
        return self

    @property
    def enabled_accounts(self) -> tuple[AccountConfig, ...]:
        return tuple(account for account in self.accounts if account.enabled)

    @property
    def enabled_platforms(self) -> tuple[str, ...]:
        return tuple(code for code, cfg in self.platforms.items() if cfg.enabled)

    def assert_publish_guards(self) -> None:
        """R14 不可逆防护：**两个开关同时满足**才允许无人确认发布。"""
        if self.enabled and not self.require_confirm and not self.accounts:
            raise ConfigError(
                "publish.enabled=true 且 require_confirm=false，但没有配置任何账号",
                code=ErrorCode.CONFIG_INVALID,
                remediation="在 config/publish.yaml 的 accounts 中至少配置一个 enabled 账号",
            )


# ══════════════════════════════════════════════════════════════════════
# 8. logging.yaml —— 日志级别与脱敏
# ══════════════════════════════════════════════════════════════════════

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConsoleLogConfig(_Base):
    enabled: bool = True
    level: LogLevel = "INFO"
    json_output: bool = False


class FileLogConfig(_Base):
    enabled: bool = True
    level: LogLevel = "DEBUG"
    path: _ConfigPath = Path("data/logs/studio.log")
    rotate_mb: int = Field(default=32, ge=1, le=4096)
    keep_files: int = Field(default=10, ge=0, le=100)


class DatabaseLogConfig(_Base):
    enabled: bool = True
    min_level: LogLevel = "INFO"
    batch_size: int = Field(default=200, ge=1, le=5000)


class LoggingConfig(_FileConfig):
    console: ConsoleLogConfig = Field(default_factory=ConsoleLogConfig)
    file: FileLogConfig = Field(default_factory=FileLogConfig)
    database: DatabaseLogConfig = Field(default_factory=DatabaseLogConfig)
    source_levels: dict[str, LogLevel] = Field(default_factory=dict)
    redact_keys: list[str] = Field(default_factory=lambda: list(REDACT_KEY_PARTS))


# ══════════════════════════════════════════════════════════════════════
# 9. secrets.yaml（不入库；缺失时全部为空）
# ══════════════════════════════════════════════════════════════════════


class WebUiSecret(_Base):
    password: str | None = None


class LlmSecret(_Base):
    """LLM 密钥本体（**只有这一处允许存密钥本体**）。

    为什么密钥可以写在这里，而 ``llm.yaml`` 里写一个字节都不行
    -----------------------------------------------------------
    ``llm.yaml`` 是**入库**的，所以那里只允许出现环境变量名（``api_key_env``，
    见 :data:`SECRET_VALUE_PATTERN` 那条铁律）。本文件（``config/secrets.yaml``）
    **从不入库**（``.gitignore`` 覆盖），与 ``webui.password`` 同一档次 ——
    它是「这台机器的私事」，不是「项目的配置」。

    优先级：``STUDIO_LLM_API_KEY`` 环境变量 > 这里的 ``api_key``。
    容器 / CI 走环境变量；个人机器走这个文件（WebUI「设置」面板写的就是它）。
    """

    api_key: str | None = None


class SecretsConfig(_FileConfig):
    webui: WebUiSecret = Field(default_factory=WebUiSecret)
    llm: LlmSecret = Field(default_factory=LlmSecret)

    @property
    def has_webui_password(self) -> bool:
        return bool(self.webui.password)

    @property
    def llm_api_key(self) -> str | None:
        """生效的 LLM 密钥（环境变量已在加载期合并进来）。"""
        return self.llm.api_key


# ══════════════════════════════════════════════════════════════════════
# 聚合、加载器、守卫
# ══════════════════════════════════════════════════════════════════════

#: 文件名 → 配置模型（加载顺序即 CONFIG_FILE_NAMES）
CONFIG_MODELS: Final[dict[str, type[BaseModel]]] = {
    "persona": PersonaConfig,
    "llm": LlmConfig,
    "app": AppConfig,
    "pools": PoolsConfig,
    "outputs": OutputsConfig,
    "randomization": RandomizationConfig,
    "publish": PublishConfig,
    "logging": LoggingConfig,
}


class ConfigBundle(BaseModel):
    """8 份配置 + 密钥的聚合视图（加载后不可变）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    persona: PersonaConfig
    llm: LlmConfig
    app: AppConfig
    pools: PoolsConfig
    outputs: OutputsConfig
    randomization: RandomizationConfig
    publish: PublishConfig
    logging: LoggingConfig
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    def assert_lan_auth(self) -> None:
        """局域网鉴权守卫（§01.2.5 安全底线）。

        ``allow_lan=true`` 但既无环境变量密码也无 ``secrets.yaml`` 密码 ⇒ 拒绝启动。
        密码生成（首次启动写 ``config/secrets.yaml``）属于 serve 启动路径，见 T1.12。
        """
        if self.app.web.allow_lan and not self.secrets.has_webui_password:
            raise ConfigError(
                "app.web.allow_lan=true 但未设置 WebUI 密码 ⇒ 拒绝启动",
                code=ErrorCode.AUTH_LAN_WITHOUT_PASSWORD,
                context={
                    "allow_lan": True,
                    "host": self.app.web.host,
                    "checked": ["env:STUDIO_WEBUI_PASSWORD", "config/secrets.yaml:webui.password"],
                },
                remediation=(
                    "二选一：①设置环境变量 STUDIO_WEBUI_PASSWORD；"
                    "②Copy-Item config\\secrets.example.yaml config\\secrets.yaml 并填 webui.password"
                ),
            )


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """单个配置文件的来源留痕（可审计：这份配置到底从哪来）。"""

    name: str
    path: Path
    sha256: str
    env_overrides: tuple[str, ...] = ()
    local_override: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "sha256": self.sha256,
            "env_overrides": list(self.env_overrides),
            "local_override": str(self.local_override) if self.local_override else None,
        }


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """加载结果：配置本体 + 来源留痕 + 路径契约。"""

    bundle: ConfigBundle
    sources: tuple[ConfigSource, ...]
    paths: StudioPaths

    def source_of(self, name: str) -> ConfigSource:
        for source in self.sources:
            if source.name == name:
                return source
        raise KeyError(name)

    @property
    def warnings(self) -> tuple[str, ...]:
        """非阻塞提示（WebUI 与 doctor 展示）。"""
        notes: list[str] = []
        if not self.bundle.llm.profiles[self.bundle.llm.default_profile].api_key_env:
            notes.append("LLM 默认通道未声明 api_key_env ⇒ 只能走本地通道")
        elif not os.environ.get(self.bundle.llm.profiles[self.bundle.llm.default_profile].api_key_env or ""):
            notes.append(
                f"环境变量 {self.bundle.llm.profiles[self.bundle.llm.default_profile].api_key_env} 未设置"
                "（E6，T1.8 开工前必须提供）"
            )
        if not self.bundle.publish.accounts:
            notes.append("未配置任何发布账号 ⇒ 发布功能不可用（T5.x）")
        if not self.bundle.outputs.bgm.enabled:
            notes.append("BGM 已关闭（Q12 默认开启，确认是否手改）")
        return tuple(notes)

    def dump(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        """导出为 JSON 安全字典（``studio config dump --json`` 的载荷）。"""
        payload: dict[str, Any] = self.bundle.model_dump(mode="json", by_alias=True)
        if redact_secrets:
            payload = redact(payload)
        payload["_meta"] = {
            "studio_home": str(self.paths.home),
            "data_dir": str(self.paths.data_dir),
            "sources": [source.to_dict() for source in self.sources],
        }
        return payload


# ── 脱敏 ────────────────────────────────────────────────────────────────

_SENSITIVE_SUFFIXES: Final[tuple[str, ...]] = (
    "_password",
    "_secret",
    "_token",
    "_cookie",
    "_api_key",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in {"password", "api_key", "secret", "token", "cookie", "authorization"}:
        return True
    return lowered.endswith(_SENSITIVE_SUFFIXES)


def redact(value: Any, *, key: str = "") -> Any:
    """递归脱敏（落库、推送前端、dump 共用）。

    注意：``api_key_env`` **不算**敏感键 —— 它存的是环境变量名，不是密钥本体。
    """
    if isinstance(value, Mapping):
        return {str(name): redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key=key) for item in value]
    if key and _is_sensitive_key(key):
        return "***" if value else None
    return value


# ── 读取与合并 ──────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML；缺失 / 语法错 / 顶层非映射 ⇒ 带路径的 :class:`ConfigError`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"配置文件不存在：{path}",
            code=ErrorCode.CONFIG_MISSING,
            context={"file": str(path)},
            remediation=f"对照模板补建：{TEMPLATE_FILES.get(path.stem, 'config/ 下的同名 example 文件')}",
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"配置文件不可读：{path}（{exc}）",
            code=ErrorCode.CONFIG_MISSING,
            context={"file": str(path)},
        ) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"YAML 语法错误：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"file": str(path), "error": str(exc)},
        ) from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"配置文件顶层必须是映射（键值对）：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"file": str(path), "actual_type": type(raw).__name__},
        )
    return {str(key): value for key, value in raw.items()}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并（override 覆盖 base）；列表整体替换而非拼接。"""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _parse_env_value(raw: str) -> Any:
    """环境变量值解析：先按 JSON 解析，失败退化为字符串。"""
    text = raw.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _set_nested(data: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = data
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = value


def _apply_env_overrides(
    data: dict[str, Any],
    *,
    name: str,
    env: Mapping[str, str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """应用 ``STUDIO_CFG__<文件>__<路径>__<键>`` 覆盖（env > yaml）。"""
    prefix = f"{ENV_OVERRIDE_PREFIX}{name.upper()}__"
    result = data
    applied: list[str] = []
    for key in sorted(env):
        if not key.startswith(prefix):
            continue
        tail = key[len(prefix) :]
        parts = [part.lower() for part in tail.split("__")]
        if not tail or any(not part for part in parts):
            raise ConfigError(
                f"环境变量覆盖格式非法：{key}",
                code=ErrorCode.CONFIG_UNKNOWN_KEY,
                context={"env": key, "expected": f"{prefix}SECTION__KEY"},
                remediation=f"示例：{prefix}WEB__PORT=9000",
            )
        if result is data:
            result = dict(data)
        _set_nested(result, parts, _parse_env_value(env[key]))
        applied.append(key)
    return result, tuple(applied)


def _check_env_prefixes(env: Mapping[str, str]) -> None:
    """拦截 ``STUDIO_CFG__<拼错的文件名>__...``（防"配置静默失效"）。"""
    known = {name.upper() for name in (*CONFIG_FILE_NAMES, SECRETS_FILE_NAME)}
    for key in env:
        if not key.startswith(ENV_OVERRIDE_PREFIX):
            continue
        tail = key[len(ENV_OVERRIDE_PREFIX) :]
        section = tail.split("__", 1)[0].upper()
        if section not in known:
            raise ConfigError(
                f"环境变量覆盖指向未知配置文件：{key}",
                code=ErrorCode.CONFIG_UNKNOWN_KEY,
                context={"env": key, "section": section, "known": sorted(known)},
                remediation=f"合法前缀：{', '.join(f'{ENV_OVERRIDE_PREFIX}{n}__' for n in sorted(known))}",
            )


def _format_errors(exc: ValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ()))
        details.append({"field": location or "<root>", "error": str(item.get("msg", ""))})
    return details


def _validate(name: str, data: dict[str, Any], path: Path) -> BaseModel:
    """按模型强校验；失败时错误信息必须**带文件路径与模板路径**。"""
    model = CONFIG_MODELS[name]
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = _format_errors(exc)
        is_persona = name == "persona"
        example = path.parent / f"{path.stem}.example.yaml"
        hint = (
            f"对照模板修正：{example}"
            if example.exists()
            else f"对照 {path.name} 内的注释与 §02.2 契约修正（未知键会直接报错）"
        )
        raise ConfigError(
            f"配置校验失败：{path.name}（{len(details)} 处问题）",
            code=ErrorCode.CONFIG_PERSONA_INCOMPLETE if is_persona else ErrorCode.CONFIG_INVALID,
            context={"file": str(path), "errors": details},
            remediation=hint + ("；persona 是唯一人工必填项，缺失会阻塞 T1.9" if is_persona else ""),
        ) from exc


# ── 守卫 ────────────────────────────────────────────────────────────────


def _is_within(child: Path, parent: Path) -> bool:
    """``child`` 是否位于 ``parent`` 之内（含相等）。"""
    return child == parent or parent in child.parents


def _guard_paths(bundle: ConfigBundle, paths: StudioPaths) -> None:
    """路径越界守卫：``app.paths`` 覆盖项必须落在仓库内且不在系统盘（R1）。"""
    problems: list[dict[str, str]] = []
    for field_name, raw in bundle.app.paths.items_set():
        candidate = raw if raw.is_absolute() else paths.home / raw
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            problems.append(
                {"field": f"app.paths.{field_name}", "value": str(raw), "reason": f"无法解析：{exc}"}
            )
            continue
        if is_on_system_drive(resolved):
            problems.append(
                {
                    "field": f"app.paths.{field_name}",
                    "value": str(resolved),
                    "reason": "落在系统盘（R1 禁止）",
                }
            )
        elif not _is_within(resolved, paths.home):
            problems.append(
                {
                    "field": f"app.paths.{field_name}",
                    "value": str(resolved),
                    "reason": "逃出仓库根（越界路径）",
                }
            )
    if problems:
        raise ConfigError(
            f"配置中的路径越界（{len(problems)} 处）",
            code=ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
            context={"problems": problems, "studio_home": str(paths.home)},
            remediation="把 app.paths 下的路径改为仓库内相对路径，或指向非系统盘的仓库内位置",
        )


def _guard_watermark_path(bundle: ConfigBundle, paths: StudioPaths) -> None:
    """水印路径必须落在 ``templates/`` 内。

    注意：**只校验路径归属，不校验文件是否存在** —— 水印素材缺失（E2）在
    渲染期由 T3.2 以 ``RENDER_WATERMARK_MISSING`` 拒绝渲染，不在配置期拦。
    """
    raw = bundle.outputs.watermark.path
    resolved = (raw if raw.is_absolute() else paths.home / raw).resolve()
    if not _is_within(resolved, paths.templates_dir.resolve()):
        raise ConfigError(
            "outputs.watermark.path 必须落在 templates/ 目录内",
            code=ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
            context={
                "value": str(raw),
                "resolved": str(resolved),
                "templates_dir": str(paths.templates_dir),
            },
            remediation="改为 templates/<模板 id>/assets/images/watermark.png 形式",
        )


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ── 加载 ────────────────────────────────────────────────────────────────


def _load_secrets(paths: StudioPaths, env: Mapping[str, str]) -> SecretsConfig:
    """加载 ``config/secrets.yaml``（可缺）；环境变量 ``STUDIO_WEBUI_PASSWORD`` 优先。"""
    path = paths.config_dir / "secrets.yaml"
    data = _read_yaml(path) if path.exists() else {}
    data, _ = _apply_env_overrides(data, name="secrets", env=env)
    env_password = env.get("STUDIO_WEBUI_PASSWORD", "").strip()
    if env_password:
        webui = data.get("webui")
        merged = dict(webui) if isinstance(webui, dict) else {}
        merged["password"] = env_password
        data = dict(data) | {"webui": merged}
    env_llm_key = env.get("STUDIO_LLM_API_KEY", "").strip()
    if env_llm_key:
        llm = data.get("llm")
        merged_llm = dict(llm) if isinstance(llm, dict) else {}
        merged_llm["api_key"] = env_llm_key
        data = dict(data) | {"llm": merged_llm}
    try:
        return SecretsConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"配置校验失败：{path.name}",
            code=ErrorCode.CONFIG_INVALID,
            context={"file": str(path), "errors": _format_errors(exc)},
            remediation=f"对照模板修正：{TEMPLATE_FILES['secrets']}",
        ) from exc


def effective_paths(bundle: ConfigBundle, paths: StudioPaths) -> StudioPaths:
    """把 ``app.paths`` 覆盖项落到 :class:`StudioPaths` 上。"""
    overrides = dict(bundle.app.paths.items_set())
    resolved: dict[str, Path] = {}
    for field_name, raw in overrides.items():
        candidate = raw if raw.is_absolute() else paths.home / raw
        resolved[field_name] = candidate.resolve()
    return StudioPaths(
        home=paths.home,
        data_dir=paths.data_dir,
        work_dir_override=resolved.get("work_dir"),
        output_dir_override=resolved.get("output_dir"),
        cache_dir_override=resolved.get("cache_dir"),
        logs_dir_override=resolved.get("logs_dir"),
    )


def load_persona_file(path: Path) -> PersonaConfig:
    """读取并校验**单个** persona 文件（人物库与热重载共用同一套校验）。

    与 :func:`load_config` 走的是同一个 :data:`CONFIG_MODELS` 校验器，
    因此"人物库里的文件"与"激活的 persona.yaml"永远不会出现校验标准分叉。
    """
    data = _read_yaml(path)
    model = _validate("persona", data, path)
    assert isinstance(model, PersonaConfig)
    return model


def load_config(
    paths: StudioPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """加载并**强校验**全部配置（应用启动路径的入口）。

    :param paths: 路径契约；``None`` ⇒ 从环境变量推断
    :param env: 环境变量快照；``None`` ⇒ ``os.environ``（测试可注入）
    :raises ConfigError: 缺文件 / 语法错 / 未知键 / 非法值 / 越界路径 / 局域网无密码
    """
    source_env = dict(env if env is not None else os.environ)
    resolved_paths = paths if paths is not None else StudioPaths.from_env(source_env)
    _check_env_prefixes(source_env)

    config_dir = resolved_paths.config_dir
    sources: list[ConfigSource] = []
    validated: dict[str, BaseModel] = {}

    for name in CONFIG_FILE_NAMES:
        path = config_dir / f"{name}.yaml"
        data = _read_yaml(path)
        local_path = config_dir / f"{name}.local.yaml"
        local_used = local_path if local_path.exists() else None
        if local_used is not None:
            data = _deep_merge(data, _read_yaml(local_used))
        data, applied = _apply_env_overrides(data, name=name, env=source_env)
        validated[name] = _validate(name, data, path)
        sources.append(
            ConfigSource(
                name=name,
                path=path,
                sha256=_file_sha256(path),
                env_overrides=applied,
                local_override=local_used,
            )
        )

    secrets = _load_secrets(resolved_paths, source_env)
    payload = dict(validated) | {"secrets": secrets}
    try:
        bundle = ConfigBundle.model_validate(payload)
    except ValidationError as exc:  # 理论上不可达（各段已单独校验）
        raise ConfigError(
            "配置聚合失败",
            code=ErrorCode.CONFIG_INVALID,
            context={"errors": _format_errors(exc)},
        ) from exc

    bundle.assert_lan_auth()
    bundle.publish.assert_publish_guards()
    _guard_paths(bundle, resolved_paths)
    _guard_watermark_path(bundle, resolved_paths)

    return LoadedConfig(bundle=bundle, sources=tuple(sources), paths=resolved_paths)


# ══════════════════════════════════════════════════════════════════════
# 运行期可改的少量配置（T4.2 · §04.4.5 第 1 行的"一键全自动"）
# ══════════════════════════════════════════════════════════════════════

#: `approval.auto_approve_policy` 在 `config/app.yaml` 里的**唯一**一行（写回时定位用）。
#: 允许行尾带注释 —— 那份文件的每一行都带注释，抹掉它等于毁掉可读性。
_AUTO_POLICY_LINE: Final[re.Pattern[str]] = re.compile(r"^(\s*)auto_approve_policy\s*:\s*(\S*)(.*)$")


@dataclass(slots=True)
class RuntimeSettings:
    """`/overview` 与采样器要用的**少量**运行期配置（进程内单例，由 `AppState` 持有）。

    为什么不全量 `load_config`
    --------------------------
    `load_config` 要读 9 份 YAML、跑 pydantic 全量校验、查路径越界，一次几十毫秒。
    总览台是常驻页面（资源 5s 一拍），每拍重读一遍纯属浪费；而"一键全自动"改的
    就是这几个字段之一 —— 把它们单独拎出来，改完**就地更新**，
    `GET /overview` 下一拍就能看到（不用重读磁盘）。

    代价是"直接改了 YAML 但没重启 API"在内存里看不见。这是**刻意**的取舍：
    真热重载（mtime 轮询 + 半写状态 + 部分失败回滚）是 T4.11 无人值守编排的活；
    这里只保证「从 WebUI 改的立刻一致」这一条。
    """

    auto_approve_policy: AutoApprovePolicy = "grade_a"
    free_c_min_gb: float = 1.0
    free_d_min_gb: float = 15.0
    pause_pools_on_low: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_approve_policy": self.auto_approve_policy,
            "free_c_min_gb": self.free_c_min_gb,
            "free_d_min_gb": self.free_d_min_gb,
            "pause_pools_on_low": self.pause_pools_on_low,
        }


def load_runtime_settings(
    paths: StudioPaths,
    *,
    env: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    """只读 `config/app.yaml` 抽 :class:`RuntimeSettings`（**读不到就退回默认值**）。

    为什么容错而不抛：`build_state()` 在测试里会拿到"只有 data/ 没有 config/"的
    临时家目录。为了几个显示用的阈值让**整个 API 起不来**是不划算的 —— 真正的
    硬门禁在 `doctor` 与 `run_server`（它们该拒绝启动时绝不含糊）。
    """
    path = paths.config_dir / "app.yaml"
    try:
        data = _read_yaml(path)
        data, _applied = _apply_env_overrides(data, name="app", env=dict(env or os.environ))
        app = AppConfig.model_validate(data)
    except (ConfigError, ValidationError) as exc:
        logger.warning("config.runtime_settings_fallback", path=str(path), error=str(exc))
        return RuntimeSettings()
    return RuntimeSettings(
        auto_approve_policy=app.approval.auto_approve_policy,
        free_c_min_gb=app.disk_gate.free_c_min_gb,
        free_d_min_gb=app.disk_gate.free_d_min_gb,
        pause_pools_on_low=app.disk_gate.pause_pools_on_low,
    )


def set_auto_approve_policy(paths: StudioPaths, policy: AutoApprovePolicy) -> Path:
    """把 `approval.auto_approve_policy` 写回 `config/app.yaml`，返回该文件路径。

    为什么**不**用 `yaml.safe_dump` 整份重写
    --------------------------------------
    `config/app.yaml` 每一行都带着注释（阈值理由、来源条款）。整份重写会把它们
    全部抹掉 —— 于是"点了一次一键全自动"就永久毁掉了这份文件的可读性，而回退
    时人也看不出"这行原来是干什么的"。这里只**定位并替换那一行**，其余字节原样保留。

    写完**立刻回读校验**：写坏了要在这里炸，而不是等下一次启动才发现配置读不出来
    （那时候人已经忘了自己点过什么）。
    """
    path = paths.config_dir / "app.yaml"
    if not path.is_file():
        raise ConfigError(
            f"配置文件不存在：{path}",
            code=ErrorCode.CONFIG_MISSING,
            context={"path": str(path)},
            remediation="确认 STUDIO_HOME 指向仓库根，且 config/app.yaml 在位",
        )

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    for index, raw in enumerate(lines):
        body = raw.rstrip("\r\n")
        match = _AUTO_POLICY_LINE.match(body)
        if match is None:
            continue
        indent, _previous, tail = match.groups()
        ending = raw[len(body) :] or "\n"
        lines[index] = f"{indent}auto_approve_policy: {policy}{tail}{ending}"
        replaced = True
        break

    if not replaced:
        raise ConfigError(
            f"config/app.yaml 里找不到 auto_approve_policy：{path}",
            code=ErrorCode.CONFIG_MISSING,
            context={"path": str(path), "key": "approval.auto_approve_policy"},
            remediation="确认 approval: 段存在且含 auto_approve_policy 一行",
        )

    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    AppConfig.model_validate(_read_yaml(path))  # 回读校验：写坏必须当场炸
    logger.info("config.auto_approve_policy_written", path=str(path), policy=policy)
    return path
