"""设置面板的服务层（LLM 通道与密钥）。

分层与人物库同一条
------------------
``router`` 只做「HTTP ↔ dict」，判定与落盘都在这里，审计与日志的写法也在这里。
这样 CLI（``studio llm probe``）与 REST 走的是**同一份**判定，不会出现
「命令行说能进、面板说不能」（陷阱 #150 / #151 是同一族）。

三条硬规矩
----------
1. **明文密钥不出这个模块**：回给 REST 的形状里只有掩码，审计的 ``before/after``
   里也只有掩码。要显示就显示掩码，没有第二条路。
2. **审计失败不回滚**：文件已经写完了，这时回滚等于「把用户刚填好的 Key 又删掉」。
   只能记账（与 ``persona_service`` 同一条）。
3. **没变就不留痕**：填了同一把 Key ⇒ ``changed=false``，不写盘也不写审计 ——
   不假装做了一次操作，也不给审计表灌噪音。

同一屏里的两件事
----------------
① **密钥**（走 ``secret_store``，明文只在那一处出现）；
② **通道参数**（模型名 / base_url，走 ``core.config.set_llm_profile_model``，
   按行改写 ``config/llm.yaml``，注释原样保留）。

两件事的"生效"方式不同，必须如实区分：密钥是**每次调用现取**，通道参数靠
``llm_config_provider`` 的 mtime 热重载 —— 都是"存完立刻生效"，**都不需要重启**。
"""

from __future__ import annotations

from typing import Any, Final

from studio.core.clock import format_iso, utc_now
from studio.core.config import load_llm_config, set_llm_profile_model
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.secret_store import (
    API_KEY_MAX_LEN,
    API_KEY_MIN_LEN,
    LLM_API_KEY_ENV,
    SecretStore,
    mask_secret,
)
from studio.db.repositories import AuditRepo
from studio.services.llm_settings_service import key_source_label, probe_profiles, profile_cards

__all__ = ["LOG_SOURCE", "SettingsService"]

logger = get_logger("studio.settings")

#: 日志的 ``source`` 字段
LOG_SOURCE: Final[str] = "settings"


class SettingsService:
    """设置面板的读写入口。

    :param store: 密钥热重载仓库（由 ``AppState`` 持有 —— 与网关**同一个实例**，
        否则「面板写完、网关还读旧的」）
    :param audit: ``audit_ops`` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    """

    def __init__(
        self,
        paths: StudioPaths,
        store: SecretStore,
        *,
        audit: AuditRepo | None = None,
        log: Any = None,
    ) -> None:
        self._paths = paths
        self._store = store
        self._audit = audit
        self._log = log

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self) -> dict[str, Any]:
        """一次读全：通道卡片 + 路由表 + 密钥状态 + 上下限。"""
        llm = load_llm_config(self._paths)
        return {
            "generated_at": format_iso(utc_now()),
            "default_profile": llm.default_profile,
            "profiles": [card.to_dict() for card in profile_cards(llm, self._store)],
            "routing": [
                {"agent": agent, "profile": route.profile, "fallback": route.fallback}
                for agent, route in sorted(llm.routing.items())
            ],
            "key": self._key_view(),
            "limits": {"min_len": API_KEY_MIN_LEN, "max_len": API_KEY_MAX_LEN},
            "notes": self._notes(),
        }

    def _key_view(self) -> dict[str, Any]:
        snapshot = self._store.snapshot()
        source = self._store.source()
        return {
            "configured": self._store.api_key() is not None,
            "masked_key": self._store.masked(),
            "source": source,
            "source_label": key_source_label(source),
            "env_var": LLM_API_KEY_ENV,
            "path": str(self._store.path),
            "file_exists": snapshot.exists,
            "version": snapshot.version,
            "loaded_at": snapshot.loaded_at,
            "last_error": self._store.last_error,
            "env_overrides_file": self._store.env_api_key() is not None,
        }

    def _notes(self) -> list[str]:
        """面板要说、但不构成错误的话。

        「环境变量优先」必须说出来：不说的话，用户在面板里填完、发现**没生效**
        （因为环境变量还占着位置），会以为这个面板坏了 —— 而他其实只需要去
        环境变量那边改。这类「静默不生效」正是本仓库反复钉的那一类 bug。
        """
        notes: list[str] = []
        if self._store.env_api_key() is not None:
            notes.append(
                f"环境变量 {LLM_API_KEY_ENV} 已设置，它**优先于**本文件；"
                "在这里保存的密钥要等那个环境变量清掉之后才生效"
            )
        error = self._store.last_error
        if error is not None:
            notes.append(f"密钥文件读不出来（正在沿用上一份可用值）：{error}")
        return notes

    # ── 写 ──────────────────────────────────────────────────────────────

    def write_key(
        self,
        *,
        api_key: str | None = None,
        clear: bool = False,
        reason: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        """写入 / 清除密钥（**先校验、后落盘**；校验不过一个字节都不写）。"""
        change = self._store.set_api_key(None if clear else api_key, reason=reason)
        action = "settings.llm_key_cleared" if clear else "settings.llm_key_set"
        if change.changed:
            self._record(
                action=action,
                change_before=change.before.api_key,
                change_after=change.after.api_key,
                reason=reason,
                source=source,
            )
        return {
            **self.read(),
            "changed": change.changed,
            "cleared": clear,
            "reason": reason,
        }

    def write_profile(
        self,
        *,
        profile: str,
        model: str | None = None,
        base_url: str | None = None,
        reason: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        """改写某条通道的**模型名 / base_url**（**先校验、后落盘**）。

        为什么模型名要能在面板上改：``llm.yaml`` 是入库的基线配置，而"这个月用哪个
        模型"是会变的（换服务商、换档位、临时降本）。不能改的话，用户只能去手改
        YAML —— 而那份文件的注释正是"为什么这么配"的唯一记录，手改迟早改坏。

        与 :meth:`write_key` 同一条纪律：**没变就不写盘、不留痕**（同一件事做两次
        不该在审计里出现两行），变了才记 ``before/after``。

        :raises StudioError: 通道名不存在 / ``model`` 与 ``base_url`` 一个都没给
        """
        llm = load_llm_config(self._paths)
        current = llm.profiles.get(profile)
        if current is None:
            raise StudioError(
                f"没有这条通道：{profile}",
                code=ErrorCode.CONFIG_INVALID,
                context={"profile": profile, "available": sorted(llm.profiles)},
                remediation="通道名见设置页的通道卡片（唯一真相是 config/llm.yaml 的 profiles:）",
            )
        if model is None and base_url is None:
            raise StudioError(
                "至少要给 model 或 base_url 之一",
                code=ErrorCode.VALIDATION_FAILED,
                context={"profile": profile},
                remediation="填新的模型名即可；base_url 只有换服务商时才需要改",
            )

        before = {"model": current.model, "base_url": current.base_url}
        after = {
            "model": current.model if model is None else model,
            "base_url": current.base_url if base_url is None else base_url,
        }
        changed = before != after
        if changed:
            set_llm_profile_model(self._paths, profile=profile, model=model, base_url=base_url)
            self._record_profile(profile=profile, before=before, after=after, reason=reason, source=source)
        return {
            **self.read(),
            "changed": changed,
            "profile": profile,
            "model": after["model"],
            "base_url": after["base_url"],
            "reason": reason,
        }

    def _record_profile(
        self,
        *,
        profile: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str | None,
        source: str,
    ) -> None:
        """通道参数留痕（与 :meth:`_record` 同一纪律：先落盘、再留痕，失败只记账）。"""
        action = "settings.llm_profile_updated"
        if self._audit is not None:
            try:
                self._audit.record(
                    actor="user",
                    action=action,
                    target_type="config",
                    target_id=f"llm.profiles.{profile}",
                    before=dict(before),
                    after=dict(after),
                    reason=reason,
                    source=source,
                )
            except Exception:  # 配置已经落盘：这里只能记账，不能回滚文件
                logger.exception("settings.audit_failed", action=action)
        if self._log is not None:
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"LLM 通道 {profile} 已更新（model={after['model']}）",
                payload={"action": action, "profile": profile, "reason": reason, **after},
            )

    def _record(
        self,
        *,
        action: str,
        change_before: str | None,
        change_after: str | None,
        reason: str | None,
        source: str,
    ) -> None:
        """先落盘（调用方已做完）→ 再留痕 → 再写日志。两处失败都不许静默。"""
        if self._audit is not None:
            try:
                self._audit.record(
                    actor="user",
                    action=action,
                    target_type="config",
                    target_id="secrets.llm.api_key",
                    before={"masked_key": mask_secret(change_before)},
                    after={"masked_key": mask_secret(change_after)},
                    reason=reason,
                    source=source,
                )
            except Exception:  # 密钥已经落盘：这里只能记账，不能回滚文件
                logger.exception("settings.audit_failed", action=action)
        if self._log is not None:
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"LLM 密钥已更新（{mask_secret(change_after) or '已清除'}）",
                payload={
                    "action": action,
                    "reason": reason,
                    "masked_key": mask_secret(change_after),
                },
            )

    # ── 探测 ────────────────────────────────────────────────────────────

    async def probe(self) -> dict[str, Any]:
        """按需探测各通道（**只发只读 GET**，不产生一次计费调用）。"""
        llm = load_llm_config(self._paths)
        rows = await probe_profiles(llm.profiles, self._store)
        return {
            "generated_at": format_iso(utc_now()),
            "rows": [row.to_dict() for row in rows],
            "ok_count": sum(1 for row in rows if row.status == "ok"),
        }
