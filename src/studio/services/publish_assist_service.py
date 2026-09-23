"""人工过验证（T6.4 · 真机 2026-09-23）：把"要人输验证码"那一步接回发布链路。

问题长什么样
------------
``acc_douyin`` 那条发布**每一步都跑通了**（上传 / 填字 / 回读 / 点发布），平台却在点下
发布之后弹一个「接收短信验证码」—— 要人在**那个浏览器窗口**里点「获取验证码」、再输
手机上收到的六位数。而那个窗口是**无头**的、跑完就关：人既看不见它，也没有地方填。

于是面板上只剩三个选择（重试 / 取消 / 标记已处理），而**重试一百次只会弹一百次**
（真机实测：四条失败记录，四次都弹同一个框 —— 平台问的是"这台机器/这个出口 IP 是不是
你本人"，那是个**一次性质询**，跟重试次数无关）。

这个服务做什么
--------------
开一个**可见**的窗口，用**同一个 profile**（同一份登录态），把片子传上去、标题文案填好、
点下发布，然后**停在那儿等人** —— 人输完六位数字，流程自己接着走完并落库。

为什么不是"替人过验证"
----------------------
R13 的底线一条都没动：系统**不点**「获取验证码」、**不读**短信、**不填**码，也不存任何
凭据。它做的事只是**把窗口摆在人面前**，然后等人。这一步无论怎么自动化都得有人在。

为什么"可见窗口"这件事本身就是修法的一半
----------------------------------------
发布池的 worker 是**无头**的（``workers/run_publish.py`` 写死），它起的浏览器 UA 里带着
``HeadlessChrome``。真机实测：换浏览器二进制（``channel="chrome"`` / ``"chromium"``）
**改不动**那个 UA，只有 ``headless=False`` 才会让 UA 变成普通的 ``Chrome``
（四组读数见 ``docs/runbook/publish_stuck.md``）。所以这条路不只是"给人一个能输入的框"，
它同时让**平台看到的那一次请求**更像这台机器上的普通浏览。

⚠️ 这**不是**伪装：没有改 UA、没有加任何反检测参数，只是真的开了一个窗口。

与自动发布的关系
----------------
八步流程**一行都没有分叉** —— 走的是同一个 ``PlaywrightPublisher``、同一份选择器。
唯一的差别是 ``PublisherContext`` 上那两个开关（``headless=False`` +
``await_manual_verify=True``），以及"谁把结果落库"：worker 走作业、这里走这条记录本身。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Final

from studio.core.clock import format_iso, utc_now
from studio.core.config import PublishConfig, load_publish_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.queue import CLAIMED_STATUS, JobStore
from studio.db.repositories import AuditRepo
from studio.db.repositories.publication_repo import (
    CANCELED,
    PUBLISHED,
    PublicationRepo,
    PublicationRow,
)
from studio.domain.publish import parse_unit_ref
from studio.publish.base import (
    PublishEvidence,
    PublishRequest,
    PublishResult,
    PublishStatus,
)
from studio.services.publish_accounts_service import PublisherBuilder
from studio.services.publish_service import next_metric_at, publisher_for, resolve_account

__all__ = ["AUDIT_ASSIST", "LOG_SOURCE", "PublishAssistService"]

logger = get_logger("studio.publish_assist")

#: 日志的 ``source`` 字段（``system_logs.source``）。
LOG_SOURCE: Final[str] = "publish_assist"

#: 审计动作名（``audit_ops.action``）。与 ``publish.retry`` / ``publish.cancel`` 同一族。
AUDIT_ASSIST: Final[str] = "publish.assist"


class PublishAssistService:
    """面板上「人工过验证」的入口。

    :param paths: 仓库路径（``config/publish.yaml`` / ``data/browser_profile`` 从它派生）
    :param connection: 库连接（读那条记录、把结论写回去）
    :param audit: ``audit_ops`` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    :param publisher_builder: 发布器装配器（``None`` ⇒ 真 Playwright；测试注入假件）
    """

    def __init__(
        self,
        paths: StudioPaths,
        *,
        connection: Any,
        audit: AuditRepo | None = None,
        log: Any = None,
        publisher_builder: PublisherBuilder | None = None,
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._audit = audit
        self._log = log
        self._publisher_builder = publisher_builder

    # ── 对外 ────────────────────────────────────────────────────────────

    async def assist(
        self,
        publication_id: str,
        *,
        reason: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        """开一个可见窗口，把这条**重新发一遍**，途中停下来等人过验证。

        **为什么是"重新发一遍"而不是"接着上次"**：上一次那个浏览器会话已经没了
        （窗口一关，页面状态就没了），而登录态活在 profile 目录里 —— 所以能复用的是
        **登录态**，不是**那次上传**。代价是重新上传一遍成片；换来的是"这条路径与
        自动发布跑的是同一份代码"，而不是一段只在人盯着时才走的旁路。

        :raises StudioError: 这条记录不该走这条路（已发布 / 已取消 / 演练登记 /
            成片不在 / 账号没启用 / 发布池正在发同一个号）
        """
        started = time.monotonic()
        config = load_publish_config(self._paths)
        repo = PublicationRepo(self._connection)
        row = repo.get(publication_id)
        if row is None:
            raise StudioError(
                f"发布记录不存在：{publication_id}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": publication_id},
                remediation="刷新发布面板 —— 任务被删会级联删掉它",
            )
        self._refuse(row)
        account = resolve_account(config, platform=row.platform, account_id=row.account_id)
        video = self._video(row)
        self._refuse_busy_profile(row)

        repo.mark_uploading(row.id)
        self._log_line(
            "info",
            f"人工过验证开始：{row.platform}/{row.account_id} —— 浏览器窗口即将打开，"
            "请在窗口里点「获取验证码」并输入手机上收到的六位数",
            payload={"publication_id": row.id, "account_id": row.account_id},
        )

        result = await self._builder(config)(account, headless=False, await_manual_verify=True).publish(
            PublishRequest(
                task_id=row.task_id,
                platform=row.platform,
                account_id=row.account_id,
                video_path=video,
                title=row.title,
                caption=row.caption,
                tags=row.tags,
                cover_path=self._cover(row),
                scheduled_at=row.scheduled_at,
                # 真发。这条路径**没有** dry-run 形态：它的全部意义就是"把这一条发出去"，
                # 而演练已经有一条更便宜的入口（``studio publish dry-run``）。
                dry_run=False,
            )
        )
        fresh = self._settle(repo, row, result, config=config, reason=reason, source=source)
        waited = round(time.monotonic() - started, 1)
        return {
            "publication": fresh,
            "action": "assist",
            "message": _outcome_message(fresh, result),
            "waited_sec": waited,
        }

    # ── 守卫 ────────────────────────────────────────────────────────────

    def _refuse(self, row: PublicationRow) -> None:
        """三种"这条路不该走"的状态 —— 每一种都要说清**该走哪条**。"""
        if row.status == PUBLISHED:
            # R14：发布不可逆。这条**已经**在平台上了，再走一遍就是第二条作品。
            raise StudioError(
                "这条已经发出去了，不能再走一遍（发布不可逆）",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": row.id, "url": row.url},
                remediation="如果平台上其实没有它，先用「取消」作废这条记录，再重新投递一次",
            )
        if row.status == CANCELED:
            raise StudioError(
                "这条已经被取消了",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": row.id},
                remediation="要发就重新投递一次（取消是**作废**，不是暂停）",
            )
        if row.dry_run:
            # 演练登记的记录是"停在第 ⑥ 步之前"的事实。拿它走真发布，等于让一次演练
            # 的残留变成一条真作品 —— 而用户从没说过要发它。
            raise StudioError(
                "这条记录是演练登记（dry_run），不能用真发布走一遍",
                code=ErrorCode.VALIDATION_FAILED,
                context={"publication_id": row.id},
                remediation="要真发就在投递面板上不勾「演练」重新投一次",
            )

    def _video(self, row: PublicationRow) -> Path:
        video = Path(row.video_path)
        if not video.is_file():
            raise StudioError(
                f"盘上找不到这条记录的成片：{video}",
                code=ErrorCode.RENDER_FAILED,
                context={"publication_id": row.id, "video_path": video.as_posix()},
                remediation="先重新出一遍片，或者在成片库里换一条能用的",
            )
        return video

    @staticmethod
    def _cover(row: PublicationRow) -> Path | None:
        if not row.cover_path:
            return None
        cover = Path(row.cover_path)
        # 封面**找不到就算了**（与第 ④ 步同一条取舍）：它是装饰，不该拦下一条能发的片子。
        return cover if cover.is_file() else None

    def _refuse_busy_profile(self, row: PublicationRow) -> None:
        """发布池正在发**同一个账号** ⇒ 拒绝。

        两个浏览器抢同一个 ``user_data_dir`` 是**发不出去**的（Chromium 有单实例锁），
        但它的报错长得像"浏览器坏了"，而不是"你撞上了" —— 所以在这里提前拦一句。

        更要紧的是另一半：这条路径会**重新上传并点发布**。要是池子那边同时也在发同一个
        账号，最坏的结果不是报错，而是**同一条内容发出去两遍**（R14 不可逆）。
        """
        now = format_iso(utc_now())
        jobs = JobStore(self._connection).list_jobs(pool="publish", status=CLAIMED_STATUS, limit=100)
        for job in jobs:
            if job.lease_expires_at is not None and job.lease_expires_at < now:
                # 租约过期的认领**不算"正在发"**：那是**死掉的 worker 留下的孤儿行**。
                # 真机 2026-09-23 就有一条：`publish#1@65928` 崩了之后，那条作业在库里
                # 挂了一个多小时还写着 `claimed`，而发布池里一个在跑的进程都没有。
                # 拿它拦人，等于让"上一次崩了"变成"这一次不许你修"。
                continue
            platform, hint = parse_unit_ref(job.unit_ref)
            account_id = hint or job.payload.get("account_id")
            if platform == row.platform and account_id == row.account_id:
                raise StudioError(
                    f"发布池正在发这个账号（{row.platform}/{row.account_id}），等它跑完再来",
                    code=ErrorCode.VALIDATION_FAILED,
                    context={"publication_id": row.id, "job_id": job.id, "unit_ref": job.unit_ref},
                    remediation="同一个账号同时只能开一个浏览器（登录态目录是独占的）；"
                    "到四池面板看一眼 publish 池，或者把它暂停掉再点这里",
                )

    # ── 落库 ────────────────────────────────────────────────────────────

    def _settle(
        self,
        repo: PublicationRepo,
        row: PublicationRow,
        result: PublishResult,
        *,
        config: PublishConfig,
        reason: str | None,
        source: str,
    ) -> PublicationRow:
        """把 ``PublishResult`` 落回这条记录（与 worker 的 ``_settle`` 同一套落点）。

        **失败一律转 ``manual_required``**，不交回队列：这条路是人点的，他就在机器前面，
        再排一次队只会让他等一个自己刚看完的过程。落 ``manual_required`` 还顺带把
        「重试 / 取消 / 标记已处理」三个按钮留在原地 —— 他可以就地再来一次。
        """
        evidence = _evidence(result.evidence)
        if result.ok and result.status is PublishStatus.PUBLISHED:
            fresh = repo.mark_published(
                row.id,
                url=result.url,
                platform_post_id=result.platform_post_id,
                evidence=evidence,
                next_metric_at=next_metric_at(config),
            )
            self._record(
                row=row,
                fresh=fresh,
                reason=reason,
                source=source,
                result=result,
            )
            self._log_line(
                "info",
                f"人工过验证成功：{fresh.url or fresh.platform_post_id or '已发布'}",
                payload={"publication_id": fresh.id},
            )
            return fresh

        code = result.error_code or ErrorCode.PUBLISH_FAILED.value
        message = result.error_message or "发布失败（平台没给出原因）"
        fresh = repo.mark_manual_required(row.id, error_code=code, error_message=message, evidence=evidence)
        self._record(row=row, fresh=fresh, reason=reason, source=source, result=result)
        self._log_line(
            "warn",
            f"人工过验证没成功：{message}",
            payload={"publication_id": fresh.id, "error_code": code},
        )
        return fresh

    # ── 留痕 ────────────────────────────────────────────────────────────

    def _record(
        self,
        *,
        row: PublicationRow,
        fresh: PublicationRow,
        reason: str | None,
        source: str,
        result: PublishResult,
    ) -> None:
        """审计：记下"人点了这条路，它最后落在哪"。"""
        if self._audit is None:
            return
        try:
            self._audit.record(
                actor="user",
                action=AUDIT_ASSIST,
                target_type="publication",
                target_id=row.id,
                before={"status": row.status, "attempt_count": row.attempt_count},
                after={
                    "status": fresh.status,
                    "url": fresh.url,
                    "platform_post_id": fresh.platform_post_id,
                    "error_code": result.error_code,
                },
                reason=reason or "面板上点了「人工过验证」",
                source=source,
            )
        except Exception:  # 库里已经写完了：这里只能记账，不能回滚那次发布
            logger.exception("publish_assist.audit_failed", publication_id=row.id)

    def _log_line(self, level: str, message: str, *, payload: dict[str, Any]) -> None:
        if self._log is None:
            return
        self._log(level=level, source=LOG_SOURCE, message=message, payload=payload)

    def _builder(self, config: PublishConfig) -> PublisherBuilder:
        """本次调用用的装配器（注入过就用注入的那个）。"""
        if self._publisher_builder is not None:
            return self._publisher_builder
        return lambda account, *, headless, await_manual_verify=False: publisher_for(
            self._paths,
            config,
            account,
            headless=headless,
            await_manual_verify=await_manual_verify,
        )


def _evidence(evidence: PublishEvidence | None) -> dict[str, Any]:
    """取证压成 ``evidence_json`` 的形状（``None`` 的字段**不写**，别塞一堆 null）。"""
    if evidence is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in (
        ("screenshot_path", evidence.screenshot_path),
        ("dom_snapshot_path", evidence.dom_snapshot_path),
        ("stderr_tail", evidence.stderr_tail),
        ("selector_version", evidence.selector_version),
        ("platform_text", evidence.platform_text),
        ("stage", evidence.stage),
    ):
        if value is None:
            continue
        out[key] = value.as_posix() if isinstance(value, Path) else value
    return out


def _outcome_message(row: PublicationRow, result: PublishResult) -> str:
    """面板上那一句人话（**服务端算**，前端不必自己拼）。"""
    if result.ok and result.status is PublishStatus.PUBLISHED:
        return f"已发出去了：{row.url or row.platform_post_id or '平台已收下'}"
    return f"这一趟没成：{row.error_message or '平台没给出原因'}（这条仍在待人工）"
