"""发布账号的面板服务层（T6.4：加号 / 改号 / 停用 / 删号，写回 ``config/publish.yaml``）。

分层与设置面板同一条
--------------------
``router`` 只做「HTTP ↔ dict」，判定与落盘都在这里，审计与日志的写法也在这里。
CLI（``studio config validate`` / ``studio publish enqueue``）与 REST 读的是**同一份**
``config/publish.yaml``，面板写进去的就是它们下一次现读的那一份 —— 没有缓存，
**不需要重启任何进程**（``app/deps.py`` 的 ``publish_config_for`` 每次现读）。

三件必须跟着账号行一起显示的事
------------------------------
1. **扫码是人的活**：本系统不自动登录、不绕过验证码（R13 合规底线）。面板只把账号
   配好；新号在扫码之前投出去，只会得到 ``PUBLISH_LOGIN_EXPIRED`` 转人工。这句话
   写在每一行的 ``note`` 里，而不是藏在文档里。
2. **``profile_dir`` 只是"声明"**：运行期真正用的登录态目录是
   ``data/browser_profile/<account_id>``（见 ``publish/base.py`` 的
   ``PublisherContext.profile_dir``）；配置里那一列的作用是"登录态必须按账号隔离"的
   唯一性校验。两者不一致时**必须说出来** —— 否则用户改了半天那个值、发现毫无效果，
   又是一次静默失效（P4 最反对的那类）。
3. **删号不删登录态**：``data/browser_profile/<id>/`` 是凭据，面板一个字节都不碰
   （§02.5）。要清就人自己去清，清之前也该知道"重新扫码"的代价。

为什么"没变就不写盘"
--------------------
与 :class:`~studio.services.settings_service.SettingsService` 同一条：把同一个值存两次
不该在审计里出现两行，也不该让文件 mtime 白跳一次（有进程靠 mtime 判热重载）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Final, Protocol

from studio.core.clock import format_iso, utc_now
from studio.core.config import (
    AccountConfig,
    PublishConfig,
    load_publish_config,
    parse_publish_account,
    write_publish_accounts,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.repositories import AuditRepo
from studio.publish.base import LOGIN_TIMEOUT_SEC, Publisher, PublishHealth
from studio.services.publish_service import health_payload, platform_options, publisher_for

__all__ = ["LOG_SOURCE", "PublishAccountsService", "PublisherBuilder", "account_limits"]

logger = get_logger("studio.publish_accounts")

#: 日志的 ``source`` 字段
LOG_SOURCE: Final[str] = "publish_accounts"


class PublisherBuilder(Protocol):
    """按账号装配一个发布器（``headless=False`` ⇒ 浏览器窗口可见）。

    打成一个可注入的缝，理由与 ``asset_service_for`` 注入探测器同一条：**单测不该
    依赖这台机器装没装 Chromium、有没有网**。"登录态探测"这条链路的判据是
    "拿到 health 之后面板说什么"，不是"Playwright 起不起得来"。

    ``await_manual_verify`` 是给「人工过验证」（``publish_assist_service``）用的
    第三个参数，见 :class:`~studio.publish.base.PublisherContext`。**它带默认值**：
    探测与扫码登录两条路径都不关心它，而给它们各加一个恒为 False 的实参只是噪音。
    """

    def __call__(
        self, account: AccountConfig, *, headless: bool, await_manual_verify: bool = False
    ) -> Publisher: ...


def _bound(field: str, attr: str, default: int) -> int:
    """从 :class:`AccountConfig` 的字段约束里取一个上下限。

    **不手抄**：手抄的那一份迟早与 ``Field(ge=…, le=…)`` 分叉，而分叉的表现是
    "面板让填、后端拒收" —— 用户对着一个红框猜自己哪里写错了。
    """
    for meta in AccountConfig.model_fields[field].metadata:
        value = getattr(meta, attr, None)
        if isinstance(value, int):
            return value
    return default


def account_limits() -> dict[str, int]:
    """面板表单的上下限（判据只有一处：配置模型上的那几条约束）。"""
    return {
        "account_id_min": _bound("account_id", "min_length", 1),
        "account_id_max": _bound("account_id", "max_length", 64),
        "daily_limit_min": _bound("daily_limit", "ge", 1),
        "daily_limit_max": _bound("daily_limit", "le", 100),
        "min_gap_min_min": _bound("min_gap_min", "ge", 0),
        "min_gap_min_max": _bound("min_gap_min", "le", 1440),
    }


def _account_payload(account: AccountConfig) -> dict[str, Any]:
    """审计里的 ``before/after``（**与面板同形**：一眼看出改了哪个字段）。"""
    return {
        "platform": account.platform,
        "display_name": account.display_name,
        "profile_dir": account.profile_dir.as_posix(),
        "enabled": account.enabled,
        "daily_limit": account.daily_limit,
        "min_gap_min": account.min_gap_min,
    }


def _health_note(health: PublishHealth, *, action: str) -> str:
    """探测 / 登录之后面板要说的那一句（**下一步做什么**，不是"失败了"）。

    "扫码"这两个字对没配过账号的人是不够的：他会去拿微信扫一扫，而平台那个码
    只认**自家 App**。所以失败那一支把这一步写全 —— 与 ``_account_note`` 里
    "还没有登录态"那一句是同一条理由（面板是唯一会被读到的地方）。
    """
    if health.ready:
        who = health.account_name or ""
        return f"登录态可用{('：' + who) if who else ''} —— 这个号可以投递了。"
    if action == "login":
        return (
            f"{health.hint or '还没登录'}。"
            "扫的时候用**手机上对应的那个 App**（抖音 / 快手 / 视频号）里的扫一扫，"
            "不是微信扫一扫；扫完在手机上点确认，窗口会自己关掉。"
        )
    return f"{health.hint or '还没登录'} —— 点这一行的「扫码登录」去扫一次。"


class PublishAccountsService:
    """发布账号的读写入口。

    :param paths: 仓库路径（``config/publish.yaml`` 与 ``data/browser_profile`` 都从这里取）
    :param audit: ``audit_ops`` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    """

    def __init__(
        self,
        paths: StudioPaths,
        *,
        audit: AuditRepo | None = None,
        log: Any = None,
        publisher_builder: PublisherBuilder | None = None,
    ) -> None:
        self._paths = paths
        self._audit = audit
        self._log = log
        self._publisher_builder = publisher_builder

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self) -> dict[str, Any]:
        """一次读全：账号清单（带状态）+ 平台清单 + 表单上下限 + 必须说的话。"""
        config = load_publish_config(self._paths)
        return {
            "generated_at": format_iso(utc_now()),
            "config_path": str(self._paths.config_dir / "publish.yaml"),
            "publish_enabled": config.enabled,
            "require_confirm": config.require_confirm,
            "accounts": [self._account_view(account, config) for account in config.accounts],
            "platforms": [option.to_dict() for option in platform_options(config)],
            "profile_dir_prefix": self._profile_dir_prefix(),
            "limits": account_limits(),
            "notes": self._notes(config),
        }

    def _account_view(self, account: AccountConfig, config: PublishConfig) -> dict[str, Any]:
        runtime_dir = self._paths.browser_profile_dir / account.account_id
        declared = account.profile_dir.as_posix()
        # 配置里写的是**相对 STUDIO_HOME** 的路径（``data/browser_profile/acc_main``），
        # 而运行期那个是绝对路径：要比就得先落到同一个坐标系里，否则这一行永远是
        # "不一致" —— 一句永远为真的警告等于没有警告。
        declared_path = (
            account.profile_dir
            if account.profile_dir.is_absolute()
            else self._paths.home / account.profile_dir
        )
        matches = declared_path == runtime_dir
        return {
            "account_id": account.account_id,
            "platform": account.platform,
            "display_name": account.display_name,
            "profile_dir": declared,
            "runtime_profile_dir": runtime_dir.as_posix(),
            # 目录在 ⇒ 至少登录过一次。**不等于**会话还有效（那要真发一次才知道），
            # 所以面板上的文案只说"登录过"，不说"可用"。
            "profile_dir_exists": runtime_dir.exists(),
            "profile_dir_matches_runtime": matches,
            "enabled": account.enabled,
            "daily_limit": account.daily_limit,
            "min_gap_min": account.min_gap_min,
            "note": self._account_note(account, config, runtime_dir),
        }

    def _account_note(self, account: AccountConfig, config: PublishConfig, runtime_dir: Any) -> str:
        """这一行账号**现在**是什么状态、下一步该做什么（面板直接显示，不自己拼）。"""
        if not account.enabled:
            return "已停用：投递与定时计划都不会选它"
        platform_cfg = config.platforms.get(account.platform)
        if platform_cfg is None or not platform_cfg.enabled:
            return f"平台 {account.platform} 未启用 ⇒ 投了会被跳过（platforms.{account.platform}.enabled）"
        if not runtime_dir.exists():
            return (
                "还没有登录态：先人工扫码一次 —— "
                f"studio publish dry-run --account {account.account_id} --show-browser"
            )
        return "登录过（会话是否仍有效，要真发一次才知道）"

    def _profile_dir_prefix(self) -> str:
        """``data/browser_profile`` 的**相对**写法（与 ``config/publish.yaml`` 同形）。"""
        root = self._paths.browser_profile_dir
        try:
            return root.relative_to(self._paths.home).as_posix()
        except ValueError:  # 家目录之外（测试用的临时树）：给绝对路径，别假装是相对的
            return root.as_posix()

    def _notes(self, config: PublishConfig) -> list[str]:
        """面板要说、但不构成错误的话（顺序 = 先说最挡路的）。"""
        notes: list[str] = []
        if not any(account.enabled for account in config.accounts):
            notes.append("现在**一个启用的账号都没有**：投递没有任何目标（投出去会被整条跳过）。")
        if not config.enabled:
            notes.append(
                "总开关 publish.enabled=false：账号配好了也发不出去 —— 投递会直接进死信"
                "（在「四池调度」里看），发布面板上不会出现记录。"
            )
        notes.append(
            "登录态只住在 data/browser_profile/<account_id>/ 里，且**只属于这个账号**："
            "不要复制给别人、不要放进任何备份（§02.5）。本系统不自动登录、不绕过验证码（R13）。"
        )
        notes.append(
            "加完账号要人工扫码一次才会生效 —— 点那一行的「扫码登录」，窗口会开在"
            "**跑着 studio 服务的那台电脑**上（不是手机上跳出什么）。"
        )
        return notes

    # ── 写 ──────────────────────────────────────────────────────────────

    def save(
        self,
        account_id: str,
        payload: Mapping[str, Any],
        *,
        reason: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        """新增或改写一个账号（**先校验、后落盘**；没变就不写盘、不留痕）。

        ``account_id`` 取自路径，**不接受请求体里另写一个** —— 身份只有一个来源，
        否则"改 A 结果新增了 B"这种错法迟早出现。

        :raises StudioError: 表单不合法（``VALIDATION_FAILED`` / 422）；
            与其它账号冲突（重复 ``profile_dir`` 等，``CONFIG_INVALID`` / 400）
        """
        data = dict(payload)
        data["account_id"] = account_id
        if not str(data.get("profile_dir") or "").strip():
            # 留空 ⇒ 按运行期**真正会用的**那个目录补上。这个字段的作用是"登录态必须
            # 按账号隔离"的唯一性校验，默认值与现实一致，才不会让人填出一个
            # "看着对、其实没用"的值。
            data["profile_dir"] = f"{self._profile_dir_prefix()}/{account_id}"
        account = parse_publish_account(data)

        config = load_publish_config(self._paths)
        before = next((item for item in config.accounts if item.account_id == account_id), None)
        accounts = [account if item.account_id == account_id else item for item in config.accounts]
        if before is None:
            accounts.append(account)
        changed = before != account
        if changed:
            write_publish_accounts(self._paths, accounts)
            self._record(
                action="publish.account_created" if before is None else "publish.account_updated",
                account_id=account_id,
                before=None if before is None else _account_payload(before),
                after=_account_payload(account),
                reason=reason,
                source=source,
            )
        return {
            **self.read(),
            "changed": changed,
            "created": before is None,
            "account_id": account_id,
            "reason": reason,
        }

    def remove(
        self,
        account_id: str,
        *,
        reason: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        """从配置里删掉一个账号（**登录态目录不碰** —— 那是凭据，删不删由人决定）。

        :raises StudioError: 配置里没有这个账号（``PUBLISH_ACCOUNT_NOT_FOUND`` / 404）
        """
        config = load_publish_config(self._paths)
        target = self._require_account(config, account_id)
        write_publish_accounts(
            self._paths, [item for item in config.accounts if item.account_id != account_id]
        )
        self._record(
            action="publish.account_removed",
            account_id=account_id,
            before=_account_payload(target),
            after=None,
            reason=reason,
            source=source,
        )
        return {**self.read(), "changed": True, "removed": account_id, "reason": reason}

    # ── 登录态（T6.4：面板上点一下就能探测 / 扫码登录）────────────────────

    def _require_account(self, config: PublishConfig, account_id: str) -> AccountConfig:
        """配置里那个账号；没有 ⇒ ``PUBLISH_ACCOUNT_NOT_FOUND``（404）。

        与 ``remove`` 共用：三处（改 / 删 / 登录）问的是同一个问题
        "这个 id 现在还在不在"，各写一份的代价是某一天其中一处忘了带 ``known``，
        而面板正好靠它显示"现存的是哪几个"。
        """
        account = next((item for item in config.accounts if item.account_id == account_id), None)
        if account is None:
            raise StudioError(
                f"配置里没有这个账号：{account_id}",
                code=ErrorCode.PUBLISH_ACCOUNT_NOT_FOUND,
                context={
                    "account_id": account_id,
                    "known": [item.account_id for item in config.accounts],
                },
                remediation="刷新面板 —— 它可能已经被删掉了",
            )
        return account

    def _builder(self, config: PublishConfig) -> PublisherBuilder:
        """本次调用用的装配器（注入过就用注入的那个）。"""
        if self._publisher_builder is not None:
            return self._publisher_builder
        return lambda account, *, headless, await_manual_verify=False: publisher_for(
            self._paths, config, account, headless=headless, await_manual_verify=await_manual_verify
        )

    async def probe(self, account_id: str) -> dict[str, Any]:
        """无头探一次登录态（T6.4 · **不动配置、不写盘**）。

        为什么探测**不进** ``audit_ops``：审计记的是"人做了什么决定"，而"看了一眼
        登录态"既没改配置也没改凭据 —— 每点一次就多一行，真正的改动会被淹掉。
        它进的是日志（``system_logs``），那才是"刚才发生了什么"该待的地方。
        """
        config = load_publish_config(self._paths)
        account = self._require_account(config, account_id)
        started = time.monotonic()
        health = await self._builder(config)(account, headless=True).health()
        self._log_health(account_id, action="probe", health=health)
        return self._health_outcome(account_id, action="probe", health=health, started=started)

    async def login(self, account_id: str, *, timeout_sec: float = LOGIN_TIMEOUT_SEC) -> dict[str, Any]:
        """开一个**可见**的浏览器窗口等人扫码（T6.4 · R13：登录只发生在人扫码那一下）。

        窗口出现在**跑着 studio 服务的那台电脑**上（浏览器是它起的）—— 面板上那句
        "去扫一下"说的就是这个窗口，不是手机上会跳出什么东西来。

        超时之后再探一次（见下面那段注释）：人**可能确实扫了**，只是那个页面没跳转，
        直接报"没扫到"会让他对着一个其实已经好的账号再扫一遍。
        """
        config = load_publish_config(self._paths)
        account = self._require_account(config, account_id)
        build = self._builder(config)
        started = time.monotonic()
        health = await build(account, headless=False).login(timeout_sec=timeout_sec)
        if not health.ready:
            # 走到这里窗口已经关了（``login`` 的 ``async with`` 退出来了）⇒ profile
            # 不再被占用，可以安全地再起一个无头会话。判据仍然是**平台页面**说的那句话，
            # 不是我们的推测 —— 与 ``health()`` 用的是同一套选择器。
            follow_up = await build(account, headless=True).health()
            if follow_up.ready:
                health = follow_up
        if health.ready:
            self._record(
                action="publish.account_logged_in",
                account_id=account_id,
                before=None,
                after={
                    "account_name": health.account_name,
                    "profile_dir": account.profile_dir.as_posix(),
                },
                reason=None,
                source="webui",
            )
        self._log_health(account_id, action="login", health=health)
        return self._health_outcome(account_id, action="login", health=health, started=started)

    def _health_outcome(
        self,
        account_id: str,
        *,
        action: str,
        health: PublishHealth,
        started: float,
    ) -> dict[str, Any]:
        """探测 / 登录的结论：**刷新后的整屏** + 这一次的 health（与写操作同一形状）。"""
        return {
            **self.read(),
            "account_id": account_id,
            "action": action,
            "health": health_payload(health),
            "note": _health_note(health, action=action),
            "waited_sec": round(time.monotonic() - started, 1),
        }

    def _log_health(self, account_id: str, *, action: str, health: PublishHealth) -> None:
        if self._log is None:
            return
        self._log(
            # 级别取值是 ``system_logs`` 的 CHECK 约束（debug/info/warn/error/fatal）——
            # 写成 Python 那边的 ``"warning"`` 会在**写日志那一刻**炸成 IntegrityError，
            # 而那正是"探测没探到"这条最该安静走完的路径。
            level="info" if health.ready else "warn",
            source=LOG_SOURCE,
            message=(
                f"发布账号 {account_id} 登录态{'可用' if health.ready else '不可用'}"
                f"（{action}）" + (f"：{health.hint}" if health.hint else "")
            ),
            payload={
                "action": action,
                "account_id": account_id,
                "ready": health.ready,
                "logged_in": health.logged_in,
            },
        )

    def _record(
        self,
        *,
        action: str,
        account_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        reason: str | None,
        source: str,
    ) -> None:
        """先落盘（调用方已做完）→ 再留痕 → 再写日志。两处失败都不许静默。

        审计失败**不回滚文件**：配置已经写完了，这时回滚等于"把用户刚加好的账号
        又删掉"（与 ``settings_service`` / ``persona_service`` 同一条）。
        """
        if self._audit is not None:
            try:
                self._audit.record(
                    actor="user",
                    action=action,
                    target_type="config",
                    target_id=f"publish.accounts.{account_id}",
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            except Exception:  # 配置已经落盘：这里只能记账，不能回滚文件
                logger.exception("publish_accounts.audit_failed", action=action)
        if self._log is not None:
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"发布账号 {account_id} 已更新（{action.rsplit('.', 1)[-1]}）",
                payload={"action": action, "account_id": account_id, "reason": reason},
            )
