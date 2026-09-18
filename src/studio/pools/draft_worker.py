"""写稿池的单元处理器（T4.11 · §04.7 时序图 · 补齐 §04.8.5 的 `handler_missing`）。

`draft/task` 单元 = 「一个任务从 `pending` 走到等配音 / 等确认 / 已废弃」
--------------------------------------------------------------------
```text
claim(draft/task, unit_ref = task_id)
   ├─ 任务已被别的路径推过了（awaiting_approval / queued_voice …）⇒ 空操作成功
   ├─ 任务停在 failed 且断点在写稿段 ⇒ 先回到断点（自动重试的落点）
   ├─ 库里没有生效稿件 ⇒ ScriptService.draft（Director → Writer → 逐句落库）
   ├─ ReviewService.review（规则 + LLM 六维 ⇒ 分级 ⇒ 放行 / 改稿 / 进闸 / 废弃）
   └─ 返回一份"这次到底干了什么"的摘要（进 `jobs.result_json`，面板与排障都读它）
```

为什么单元处理器放在 `pools/` 而不是 `services/`
-----------------------------------------------
它只做三件事：把认领到的 id 翻成服务调用、把失败翻成 `StudioError`（好让队列的
重试 / 退避 / 死信 / 告警逻辑接手）、把结果压成可留痕的摘要。真正的业务规则全在
`services/`。放在 `pools/` 是因为它实现的是 `UnitHandler` 协议 —— 那是**池**的契约。

三条纪律
--------
1. **不自己收尾**。`succeed` / `fail` / 退避 / 死信一律由 `PoolWorker` 负责（T1.6 裁定）：
   单元处理器只"产出结果 + 抛异常"。否则每个池都会长出一份自己的重试逻辑，
   而重试语义必然漂移。
2. **可重入**。同一条 `draft/task` 被重投时不能从头再来一遍 LLM —— 库里有稿件就
   直接进审稿，任务已经被推过就空操作成功。这是"中断后恢复"（M4 验收）在写稿池上的落点。
3. **不在单元里装配 LLM 网关**。网关（以及它背后的 8 样东西）由**进程入口**建一次、
   整个进程复用（`build_draft_services`）；单元只拿装配好的服务。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from studio.agents.director import DirectorAgent
from studio.agents.editor import EditorAgent
from studio.agents.gateway import LlmGateway, LogSink
from studio.agents.gateway_factory import build_gateway
from studio.agents.prompts import PromptLibrary
from studio.agents.reviewer import ReviewerAgent
from studio.agents.writer import WriterAgent
from studio.core.config import PersonaConfig, load_config
from studio.core.errors import ErrorCode, StudioError, WorkerError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.persona_store import get_persona_store
from studio.core.proto import Severity
from studio.db import connect
from studio.domain.enums import AutoApprovePolicy, TaskStatus, UnitType
from studio.domain.task_service import TaskService
from studio.pools.worker_base import UnitContext
from studio.services.log_service import LogService
from studio.services.review_service import ReviewService
from studio.services.script_service import ScriptService, read_active_script

__all__ = [
    "DRAFT_UNIT_TYPES",
    "DraftServices",
    "DraftTaskHandler",
    "DraftUnitOutcome",
    "build_draft_handler",
    "build_draft_services",
]

logger = get_logger("studio.pools.draft")

#: 本处理器认领的单元类型（`jobs.unit_type`）
DRAFT_UNIT_TYPES: Final[frozenset[str]] = frozenset({UnitType.TASK.value})

#: 还要（重新）走一遍写稿的状态。`FAILED` 在里面：队列重投时任务停在失败态，
#: 得先按 `retry_from` 回到断点，否则状态机会拒绝后面的每一条边。
_DRAFT_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.PENDING, TaskStatus.DRAFTING, TaskStatus.FAILED}
)

#: 稿件已在库里、只剩审稿的状态（`editing` = 人工退回后改稿工人刚交回来）
_REVIEW_STATUSES: Final[frozenset[TaskStatus]] = frozenset({TaskStatus.REVIEWING, TaskStatus.EDITING})

#: `retry_from` 白名单里属于"写稿段"的那两个（断点回到这里才有意义）
_DRAFT_RETRY_POINTS: Final[frozenset[TaskStatus]] = frozenset({TaskStatus.PENDING, TaskStatus.DRAFTING})


@dataclass(frozen=True, slots=True)
class DraftUnitOutcome:
    """一次 `draft/task` 单元的结论（进 `jobs.result_json`，也是面板/排障的读数）。"""

    task_id: str
    topic_id: str
    status: str
    drafted: bool = False
    reviewed: bool = False
    grade: str | None = None
    action: str | None = None
    script_id: str | None = None
    sentence_count: int = 0
    word_count: int = 0
    warnings: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "topic_id": self.topic_id,
            "status": self.status,
            "drafted": self.drafted,
            "reviewed": self.reviewed,
            "grade": self.grade,
            "action": self.action,
            "script_id": self.script_id,
            "sentence_count": self.sentence_count,
            "word_count": self.word_count,
            "warnings": list(self.warnings),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class DraftServices:
    """一次单元所需的两个服务（**同一连接**：两条流水线要看见彼此刚写下的行）。"""

    scripts: ScriptService
    reviews: ReviewService


class DraftTaskHandler:
    """`draft/task` 的单元处理器（`pools.runner.register_handler("draft", …)` 注册它）。

    :param paths: 路径契约（读配置、定位 prompts 都在装配时用到）
    :param services_factory: 连接 ⇒ 服务对（**唯一**的注入点：测试塞假 Agent 走这里）
    :param persona_provider: 取当前人物（热重载单例 ⇒ 改人物不必重启 worker）
    :param connection_factory: 单元自己的连接（缺省按 `paths.db_file` 现造）
    :param runner: 跑协程的实现（缺省 `asyncio.run`；测试可注入假件，不碰事件循环）
    """

    unit_types: frozenset[str] = DRAFT_UNIT_TYPES

    def __init__(
        self,
        *,
        paths: StudioPaths,
        services_factory: Callable[[sqlite3.Connection], DraftServices],
        persona_provider: Callable[[], PersonaConfig],
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        runner: Callable[[Any], Any] = asyncio.run,
    ) -> None:
        self._paths = paths
        self._services_factory = services_factory
        self._persona_provider = persona_provider
        self._connection_factory = connection_factory or (lambda: connect(paths.db_file))
        self._runner = runner

    def run(self, ctx: UnitContext) -> Mapping[str, Any]:
        """跑一个单元。**不吞异常**：失败抛 `StudioError`，收尾交给 `PoolWorker`。"""
        if ctx.unit_type not in self.unit_types:
            raise WorkerError(
                f"写稿池收到不认识的单元类型：{ctx.unit_type}",
                code=ErrorCode.INTERNAL,
                context={"unit_type": ctx.unit_type, "accepted": sorted(self.unit_types)},
                remediation="检查 jobs.unit_type：写稿池只认 draft/task（§03.4）",
            )
        task_id = ctx.unit_ref
        actor = f"worker:{ctx.worker_id}"
        connection = self._connection_factory()
        try:
            topic_id = _topic_of(connection, task_id)
            status = TaskService(connection).get(task_id).status
            if status is TaskStatus.FAILED:
                status = self._rewind(connection, task_id, actor=actor)
                if status is TaskStatus.FAILED:
                    # 断点不在写稿段（任务是在配音 / 渲染段挂的）⇒ 这条单元
                    # 没活干。**空操作成功**，而不是接着去审稿：
                    # 往下走会拿一个 `failed` 的任务去撞 `failed → reviewing`
                    # 这条不存在的边，把"重投死信"变成"再死一次"（T1.9 裁定 316）。
                    return self._skip(task_id, topic_id, status)
            if status not in _DRAFT_STATUSES and status not in _REVIEW_STATUSES:
                # 已经被别的路径推过去了（确认闸放行 / 人工捞回 / 上一轮就跑完了）。
                # **空操作成功**：报错只会让队列无意义地重试三次然后进死信。
                return self._skip(task_id, topic_id, status)
            services = self._services_factory(connection)
            persona = self._persona_provider()
            return self._drive(
                connection,
                services,
                task_id=task_id,
                topic_id=topic_id,
                persona=persona,
                actor=actor,
                status=status,
            )
        finally:
            connection.close()

    # ── 内部 ────────────────────────────────────────────────────────────

    def _rewind(self, connection: sqlite3.Connection, task_id: str, *, actor: str) -> TaskStatus:
        """`failed` ⇒ 按 `retry_from` 回到断点（队列重投时任务得先回到轨道上）。

        断点不在写稿段（比如任务是在配音阶段挂的）⇒ **原样返回 `failed`**：
        那不是这条单元该干的活，硬把它拽回 `drafting` 只会白烧一次 LLM。
        """
        tasks = TaskService(connection)
        target = tasks.get(task_id).retry_from
        if target not in _DRAFT_RETRY_POINTS:
            return TaskStatus.FAILED
        tasks.transition(task_id, target, actor=actor, reason="写稿池重投：回到断点")
        logger.info("draft.rewound", task_id=task_id, to=target.value)
        return target

    def _skip(self, task_id: str, topic_id: str, status: TaskStatus) -> Mapping[str, Any]:
        """任务已越过写稿段 ⇒ 空操作成功（不重跑 LLM，也不把它判成失败）。"""
        logger.info("draft.unit_skipped", task_id=task_id, status=status.value)
        return DraftUnitOutcome(
            task_id=task_id,
            topic_id=topic_id,
            status=status.value,
            note="任务已越过写稿段，空操作（不重跑 LLM）",
        ).to_dict()

    def _drive(
        self,
        connection: sqlite3.Connection,
        services: DraftServices,
        *,
        task_id: str,
        topic_id: str,
        persona: PersonaConfig,
        actor: str,
        status: TaskStatus,
    ) -> Mapping[str, Any]:
        """写稿（按需）⇒ 审稿 ⇒ 放行/改稿/进闸/废弃。"""
        tasks = TaskService(connection)
        if status is TaskStatus.PENDING:
            # `review()` 只认 `drafting` / `editing` 两个入口（它自己会把 `editing`
            # 归一成 `reviewing`）；从 `pending` 直接进审稿会撞上一条不存在的边。
            tasks.transition(task_id, TaskStatus.DRAFTING, actor=actor, reason="写稿池认领")
            status = TaskStatus.DRAFTING

        warnings: list[str] = []
        drafted = False
        script_id: str | None = None
        sentence_count = 0
        word_count = 0
        if status is TaskStatus.DRAFTING:
            existing = read_active_script(connection, task_id)
            if existing is None:
                report = self._runner(
                    services.scripts.draft(topic_id=topic_id, persona=persona, task_id=task_id, actor=actor)
                )
                if not report.ok:
                    # 迟到的失败：CLI 的 `script draft` 与池里这条单元会跑同一个任务，
                    # 先跑完的那条会把任务推过写稿段。此时**空操作成功** —— 任务不是
                    # 这条单元的活了，把它记成死信只会天天报一次假警（T1.9 裁定 316）。
                    current = tasks.get(task_id).status
                    if current not in _DRAFT_STATUSES and current not in _REVIEW_STATUSES:
                        return self._skip(task_id, topic_id, current)
                    raise _failure(
                        report.error_code,
                        report.error_message,
                        task_id=task_id,
                        fallback=ErrorCode.SCRIPT_DRAFT_FAILED,
                    )
                drafted = True
                script_id = report.script_id
                sentence_count = report.sentence_count
                word_count = report.word_count
                warnings.extend(report.warnings)
            else:
                # 上次崩在"落库之后、审稿之前"：稿件已在库里 ⇒ **不重写**（重写会白烧一次 LLM）
                script_row, sentence_rows = existing
                script_id = script_row.id
                sentence_count = len(sentence_rows)
                word_count = script_row.word_count
                warnings.append("已有生效稿件：跳过写稿，直接审稿（断点续传）")

        review = self._runner(services.reviews.review(task_id=task_id, persona=persona, actor=actor))
        if not review.ok:
            raise _failure(
                review.error_code,
                review.error_message,
                task_id=task_id,
                fallback=ErrorCode.REVIEW_FAILED,
            )
        warnings.extend(review.warnings)
        outcome = DraftUnitOutcome(
            task_id=task_id,
            topic_id=topic_id,
            status=review.status,
            drafted=drafted,
            reviewed=True,
            grade=review.grade,
            action=review.action,
            script_id=review.script_id or script_id,
            sentence_count=sentence_count,
            word_count=word_count,
            warnings=tuple(warnings),
        )
        logger.info("draft.unit_done", **outcome.to_dict())
        return outcome.to_dict()


# ── 装配（进程入口用）───────────────────────────────────────────────────


def build_draft_services(
    connection: sqlite3.Connection,
    *,
    gateway: LlmGateway,
    prompts: PromptLibrary,
    policy: AutoApprovePolicy,
    paths: StudioPaths,
    log: LogService,
) -> DraftServices:
    """按一份**已装配好的** Agent 组造服务（每单元现造，见 :func:`build_draft_handler`）。

    服务本身很轻（几个仓储包装 + 一个连接），Agent 才是重的那部分 ——
    所以重的那部分在外面建一次，这里只做"把连接缝进去"。
    """
    return DraftServices(
        scripts=ScriptService(
            connection,
            director=DirectorAgent(gateway, prompts),
            writer=WriterAgent(gateway, prompts),
            paths=paths,
            log=log.append,
        ),
        reviews=ReviewService(
            connection,
            reviewer=ReviewerAgent(gateway, prompts),
            editor=EditorAgent(gateway, prompts),
            policy=policy,
            paths=paths,
            log=log.append,
        ),
    )


def build_draft_handler(
    *,
    paths: StudioPaths,
    log: LogService,
    persona_provider: Callable[[], PersonaConfig] | None = None,
) -> DraftTaskHandler:
    """写稿池进程入口的装配（`workers/run_draft.py` 调它）。

    **Agent 建一次、服务每单元现造**：Agent 背后的网关带着熔断器与 token 预算，
    那是有状态的东西，跨单元必须存活（否则"连续失败 ⇒ 熔断"永远凑不满次数）；
    而服务只是仓储包装，现造没有成本 —— 反过来把单元连接也共享出去，
    就得回答"谁负责关它"这个没人想回答的问题。

    :param persona_provider: 取当前人物；缺省走**进程级**热重载单例
        （`get_persona_store`），于是"面板上改了人物"对 worker 立刻生效。
    """
    loaded = load_config(paths)
    prompts = PromptLibrary.load(paths.prompts_dir)
    gateway = build_gateway(
        connection=connect(paths.db_file),
        llm=loaded.bundle.llm,
        paths=paths,
        log=_log_sink(log),
    )
    # `config.ApprovalConfig.auto_approve_policy` 是 **Literal** 别名
    # （`core` 不许 import `domain`，所以那边只能写字面量），而 `ReviewService`
    # 要的是 `domain.enums.AutoApprovePolicy` 枚举 ⇒ 在这里显式转一次。
    policy = AutoApprovePolicy(loaded.bundle.app.approval.auto_approve_policy)
    store = get_persona_store(paths)

    def services_for(connection: sqlite3.Connection) -> DraftServices:
        return build_draft_services(
            connection, gateway=gateway, prompts=prompts, policy=policy, paths=paths, log=log
        )

    return DraftTaskHandler(
        paths=paths,
        services_factory=services_for,
        persona_provider=persona_provider or (lambda: store.current().config),
    )


# ── 内部 ────────────────────────────────────────────────────────────────


def _topic_of(connection: sqlite3.Connection, task_id: str) -> str:
    """任务来自哪个选题（`tasks.source_topic_id`）。

    为什么现查而不放进 job 的 `payload_json`：`payload` 是**入队那一刻的快照**，
    而 `source_topic_id` 是行上的真相。两处都存，就会出现"改了一处忘了另一处"。
    """
    row = connection.execute("SELECT source_topic_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise StudioError(
            f"任务不存在：{task_id}",
            code=ErrorCode.TASK_NOT_FOUND,
            context={"task_id": task_id},
            remediation="确认 unit_ref：写稿池的 unit_ref 就是 tasks.id（§03.4）",
        )
    topic_id = row["source_topic_id"]
    if not topic_id:
        raise StudioError(
            f"任务 {task_id} 没有来源选题，写稿池不知道该写什么",
            code=ErrorCode.TOPIC_NOT_FOUND,
            context={"task_id": task_id},
            remediation="人工加的任务没有 source_topic_id ⇒ 用 `studio script draft <topic_id>` 直接写稿",
        )
    return str(topic_id)


def _failure(raw_code: str | None, message: str | None, *, task_id: str, fallback: ErrorCode) -> StudioError:
    """把服务层"失败报告"翻成异常 ⇒ 交给队列的重试 / 退避 / 死信 / 告警。

    为什么不直接返回一份 `ok=False` 的结果：那会让作业**显示成功**，
    而"看着成功、实际什么都没产出"正是 DoD 5 要防的那种失败。
    """
    return StudioError(
        message or "写稿流水线未返回结论",
        code=_code_of(raw_code, fallback),
        context={"task_id": task_id, "reported_code": raw_code},
        remediation="看「实时日志」里 agent.director / agent.writer / agent.reviewer 那几行；"
        "修好后让队列退避重试，或在死信区一键重投",
    )


def _code_of(raw: str | None, fallback: ErrorCode) -> ErrorCode:
    """字符串错误码 ⇒ :class:`ErrorCode`（不认识的一律退回 `fallback`，**不炸**）。"""
    if not raw:
        return fallback
    try:
        return ErrorCode(raw)
    except ValueError:
        logger.warning("draft.unknown_error_code", code=raw)
        return fallback


def _log_sink(log: LogService) -> LogSink:
    """`LogService.append` → 网关的日志出口。

    为什么要一层瘦适配：`LogSink` 声明返回 `None`，而 `append` 返回落库的行
    （供调用方断言用）—— 直接把方法当回调传，mypy 会因为返回类型不一致拒绝。
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
