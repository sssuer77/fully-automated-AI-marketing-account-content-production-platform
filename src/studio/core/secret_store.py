"""密钥热重载仓库 —— 「填一次 API Key」不该需要重启任何进程。

为什么需要它
------------
``config/llm.yaml`` 里只有环境变量**名**（``api_key_env``，密钥铁律），密钥本体
要么在环境变量里，要么在 ``config/secrets.yaml`` 里。而 ``load_config()`` 返回的是
**冻结快照**（进程启动时读一次）—— 若密钥钉死在那份快照上，用户在面板里填完 Key
得重启 API + 4 个 worker 才生效，而那正是他最不想做的事（他刚被「没 Key」拦下来）。

本模块提供**运行期唯一的密钥来源**：

1. **热重载**：``snapshot()`` 每次做一次廉价 ``stat``（mtime_ns + size）比对，
   变了才真正重新解析。与 :mod:`studio.core.persona_store` 同一条手法。
2. **优先级 env > 文件**：与 ``webui.password`` 一致。容器 / CI 走环境变量，
   个人机器走这个文件（WebUI「设置」面板写的就是它）。
3. **先校验、后落盘**：校验不过**一个字节都不写**（``validate_api_key``）。
4. **失败不中断**：文件被改坏 ⇒ 保留上一份可用快照并记 ``last_error``，
   绝不把正在跑的任务打死（DoD 6）。
5. **不留备份**：这是密钥，不是配置 —— ``data/backups/`` 是给人翻的目录，
   把密钥复制进去等于让「已经删掉的那把」继续躺在盘上。退路本来就有：
   去服务商后台重新签一把。

多进程说明
----------
5 个进程各自持有一个 store 实例，靠**文件 mtime** 独立发现变更 —— 无需 IPC。
最坏情况下于下一次 ``api_key()`` 生效（worker 每个 job 开始前都会取一次）。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from studio.core.clock import format_iso, utc_now
from studio.core.errors import ConfigError, ErrorCode
from studio.core.files import stat_key
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths

__all__ = [
    "API_KEY_MAX_LEN",
    "API_KEY_MIN_LEN",
    "LLM_API_KEY_ENV",
    "SECRETS_FILE_NAME",
    "KeySource",
    "SecretChange",
    "SecretSnapshot",
    "SecretStore",
    "get_secret_store",
    "mask_secret",
    "reset_secret_store",
    "validate_api_key",
]

logger = get_logger("studio.secrets")

#: 密钥文件名（与 :data:`studio.core.config.SECRETS_FILE_NAME` 同一个）
SECRETS_FILE_NAME: Final[str] = "secrets.yaml"

#: 密钥文件里的哪个条目对应哪个环境变量
LLM_API_KEY_ENV: Final[str] = "STUDIO_LLM_API_KEY"

#: 掩码保留的头 / 尾字符数
MASK_PREFIX: Final[int] = 3
MASK_SUFFIX: Final[int] = 4

#: 表单校验的长度区间。下限挡「手滑贴了半截」，上限挡「把整篇文档贴进来」
API_KEY_MIN_LEN: Final[int] = 8
API_KEY_MAX_LEN: Final[int] = 512

#: 密钥来源
KeySource = Literal["env", "file", "none"]

#: 写盘时加在文件头的一段话（**人工写在原文件里的注释不会再出现**）
_RENDERED_HEADER: Final[str] = chr(10).join(
    (
        "# ── 本机密钥（**绝不入库**；模板见 secrets.example.yaml）────────────────",
        "# 由 WebUI「设置」面板或手工编辑写入。",
        "# 优先级：环境变量 > 本文件。改完立刻生效，**不需要重启任何进程**。",
        "",
    )
)


def mask_secret(value: str | None) -> str | None:
    """把密钥压成可安全显示的形状（``sk-…cdef``）。

    为什么留头留尾而不是全遮：全遮之后用户无法判断「面板里存的是不是我刚填的那把」
    —— 换 Key 时最常见的动作就是「贴一把新的、确认它真的换上了」。留 3 + 4 个字符
    足够认出是哪一把，又不足以被拿去用（长度信息一并暴露，但那不是秘密）。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= MASK_PREFIX + MASK_SUFFIX:
        return "…" + "*" * len(text)
    return f"{text[:MASK_PREFIX]}…{text[-MASK_SUFFIX:]}"


def validate_api_key(value: str) -> str:
    """表单校验：非空、无空白、长度在区间内；返回**去空白后**的值。

    为什么是 ``VALIDATION_FAILED`` 而不是 ``CONFIG_INVALID``：后者在 REST 面映到
    **400**（「启动时读到一份坏配置」），而这里说的是「**你刚提交的这一份**不能保存」
    ⇒ 422，面板据此把红字标到输入框上（与 `PERSONA_INVALID` / `OUTPUTS_INVALID`
    同一条取舍）。文件本身读不出来仍然走 ``CONFIG_INVALID`` —— 那是两件事。

    为什么必须挡换行：这个值要写进 YAML。带换行的值会被 ``yaml.safe_dump`` 转义成
    带引号的字符串（不会真的注入），但**下一次**读回来就是一个含换行的密钥，
    请求头里带上它 ⇒ ``httpx`` 抛 ``LocalProtocolError``，而报错点离「谁填的」
    已经很远了。在这一步拦掉，代价是一句人话。
    """
    text = value.strip()
    if not text:
        raise ConfigError(
            "API Key 不能为空（要清除请点「清除密钥」）",
            code=ErrorCode.VALIDATION_FAILED,
            context={"field": "api_key"},
            remediation="把服务商控制台里的 Key 整段贴进来",
        )
    if len(text) < API_KEY_MIN_LEN:
        raise ConfigError(
            f"API Key 太短（{len(text)} 个字符，至少 {API_KEY_MIN_LEN} 个）—— 多半是没贴全",
            code=ErrorCode.VALIDATION_FAILED,
            context={"field": "api_key", "length": len(text), "min": API_KEY_MIN_LEN},
            remediation="回服务商控制台复制完整的那一串",
        )
    if len(text) > API_KEY_MAX_LEN:
        raise ConfigError(
            f"API Key 太长（{len(text)} 个字符，上限 {API_KEY_MAX_LEN}）",
            code=ErrorCode.VALIDATION_FAILED,
            context={"field": "api_key", "length": len(text), "max": API_KEY_MAX_LEN},
            remediation="确认贴的是密钥本身，不是整份配置文件",
        )
    if any(ch.isspace() for ch in text):
        raise ConfigError(
            "API Key 里不能有空格或换行",
            code=ErrorCode.VALIDATION_FAILED,
            context={"field": "api_key"},
            remediation="重新复制一次；有些终端会把折行也一起复制进来",
        )
    return text


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    """一份**已解析**的密钥快照（``api_key`` 永远是明文，只在本进程内流转）。"""

    path: Path
    exists: bool
    api_key: str | None
    version: int
    loaded_at: str

    @property
    def configured(self) -> bool:
        return self.api_key is not None

    def to_dict(self) -> dict[str, object]:
        """给 REST 面用的形状 —— **只有掩码，没有明文**。"""
        return {
            "path": str(self.path),
            "exists": self.exists,
            "configured": self.configured,
            "masked_key": mask_secret(self.api_key),
            "version": self.version,
            "loaded_at": self.loaded_at,
        }


@dataclass(frozen=True, slots=True)
class SecretChange:
    """一次写入的结果（``changed=False`` ⇒ 本来就是它，没写盘）。"""

    changed: bool
    before: SecretSnapshot
    after: SecretSnapshot
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "reason": self.reason,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


class SecretStore:
    """``config/secrets.yaml`` 的读写入口（**唯一**改密钥的正门）。"""

    def __init__(self, paths: StudioPaths, env: Mapping[str, str] | None = None) -> None:
        self._path = paths.config_dir / SECRETS_FILE_NAME
        self._env = env
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._api_key: str | None = None
        self._exists = False
        self._version = 0
        self._loaded_at = ""
        self._last_error: str | None = None

    # ── 读 ──────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> str | None:
        """上一次重载失败的原因（``None`` ⇒ 一切正常）。"""
        return self._last_error

    def env_api_key(self) -> str | None:
        """环境变量里的密钥（**优先级最高**）。"""
        raw = self._env_map().get(LLM_API_KEY_ENV, "").strip()
        return raw or None

    def snapshot(self) -> SecretSnapshot:
        """当前快照（每次都探一次 mtime；变了才重新解析）。"""
        with self._lock:
            stamp = stat_key(self._path)
            if stamp != self._stamp:
                self._reload(stamp)
            return SecretSnapshot(
                path=self._path,
                exists=self._exists,
                api_key=self._api_key,
                version=self._version,
                loaded_at=self._loaded_at,
            )

    def api_key(self) -> str | None:
        """生效的密钥：环境变量 > 文件。"""
        return self.env_api_key() or self.snapshot().api_key

    def source(self) -> KeySource:
        """生效的那把是从哪来的（面板要如实显示「来自环境变量」还是「来自本文件」）。"""
        if self.env_api_key() is not None:
            return "env"
        return "file" if self.snapshot().configured else "none"

    def masked(self) -> str | None:
        return mask_secret(self.api_key())

    def lookup(self, env_name: str) -> str | None:
        """按**环境变量名**取值（网关用）。

        为什么按名字而不是直接给 ``llm.api_key``：``llm.yaml`` 里配的就是变量名
        （``api_key_env``），网关只知道名字。密钥文件因此是「环境变量的一个持久化
        替身」—— 两者同名同义。

        目前只有 :data:`LLM_API_KEY_ENV` 在文件里有对应条目；别的名字（比如给自建
        代理另起一个变量）仍然只认环境变量 —— 如实返回 ``None``，不猜。
        """
        value = self._env_map().get(env_name, "").strip()
        if value:
            return value
        if env_name != LLM_API_KEY_ENV:
            return None
        return self.snapshot().api_key

    # ── 写 ──────────────────────────────────────────────────────────────

    def set_api_key(self, value: str | None, *, reason: str | None = None) -> SecretChange:
        """写入 / 清除密钥（**先校验、后落盘**）。

        ``value=None`` ⇒ 删掉这个条目（面板上的「清除密钥」）。删一个本来就没有的
        条目 ⇒ ``changed=False``，且**不碰盘**（与人物库「我已经是他了」同一条取舍：
        不假装做了一次操作，也不为了「看起来成功」写一次无意义的盘）。
        """
        cleaned = None if value is None else validate_api_key(value)
        with self._lock:
            before = self.snapshot()
            if before.api_key == cleaned:
                return SecretChange(changed=False, before=before, after=before, reason=reason)
            data = self._read_raw()
            section = data.get("llm")
            merged: dict[str, Any] = dict(section) if isinstance(section, dict) else {}
            if cleaned is None:
                merged.pop("api_key", None)
            else:
                merged["api_key"] = cleaned
            if merged:
                data["llm"] = merged
            else:
                data.pop("llm", None)
            self._write_raw(data)
            self._stamp = None
            after = self.snapshot()
            logger.info(
                "secret.llm_key_written",
                path=self._path.as_posix(),
                cleared=cleaned is None,
                masked=mask_secret(cleaned),
            )
            return SecretChange(changed=True, before=before, after=after, reason=reason)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _env_map(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def _reload(self, stamp: tuple[int, int] | None) -> None:
        """重新解析文件；失败 ⇒ **保留上一份可用快照**并记 ``last_error``。"""
        self._stamp = stamp
        if stamp is None:
            self._exists = False
            self._api_key = None
            self._version += 1
            self._loaded_at = format_iso(utc_now())
            self._last_error = None
            return
        try:
            data = self._read_raw()
        except ConfigError as exc:
            self._last_error = exc.message
            logger.warning("secret.reload_failed", path=self._path.as_posix(), error=exc.message)
            return
        section = data.get("llm")
        raw = section.get("api_key") if isinstance(section, dict) else None
        text = raw.strip() if isinstance(raw, str) else None
        self._exists = True
        self._api_key = text or None
        self._version += 1
        self._loaded_at = format_iso(utc_now())
        self._last_error = None

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            loaded = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(
                f"密钥文件读不出来：{self._path.name}",
                code=ErrorCode.CONFIG_INVALID,
                context={"file": str(self._path), "error": str(exc)},
                remediation=f"对照模板修正：{self._path.parent.as_posix()}/secrets.example.yaml",
            ) from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"密钥文件的顶层必须是一个映射：{self._path.name}",
                code=ErrorCode.CONFIG_INVALID,
                context={"file": str(self._path), "type": type(loaded).__name__},
                remediation=f"对照模板修正：{self._path.parent.as_posix()}/secrets.example.yaml",
            )
        return loaded

    def _write_raw(self, data: Mapping[str, Any]) -> None:
        """原子落盘（临时文件 + ``os.replace``）：写一半断电不会留下半份配置。"""
        payload = yaml.safe_dump(
            dict(data),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(_RENDERED_HEADER + payload, encoding="utf-8", newline=chr(10))
        tmp.replace(self._path)
        _restrict(self._path)


def _restrict(path: Path) -> None:
    """尽力收紧权限。

    Windows 上 ``chmod`` 只影响只读位，拿不到 POSIX 那套权限模型，故**失败不报错**
    （做不到的事假装做到了，比做不到更坏）。
    """
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("secret.chmod_skipped", path=path.as_posix())


_STORES: dict[str, SecretStore] = {}
_STORES_LOCK = threading.RLock()


def get_secret_store(
    paths: StudioPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> SecretStore:
    """取进程级单例（按 ``config_dir`` 分桶 —— 测试用临时家目录时不会串到真仓库）。"""
    resolved = paths or StudioPaths.from_env()
    key = str(resolved.config_dir)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = SecretStore(resolved, env=env)
            _STORES[key] = store
        return store


def reset_secret_store() -> None:
    """丢弃全部单例（测试 / 切换 STUDIO_HOME 后使用）。"""
    with _STORES_LOCK:
        _STORES.clear()
