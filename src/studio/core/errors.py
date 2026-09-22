"""错误码注册表与异常基类（§02.4：大写下划线，前缀标明子系统）。

设计约束
--------
1. **禁止内联错误码字符串**：所有错误码必须登记在 :class:`ErrorCode`。
2. **禁止静默失败**（DoD 5）：异常必须带 ``code``，最终落 ``system_logs``。
3. 异常可携带 ``context``（结构化字典）与 ``remediation``（人类可读的修复动作）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = [
    "ConfigError",
    "DoctorGateError",
    "ErrorCode",
    "ExternalToolError",
    "LlmBudgetError",
    "LlmError",
    "LlmSchemaError",
    "LlmTransportError",
    "MediaError",
    "PublishError",
    "QueueError",
    "RenderError",
    "StateTransitionError",
    "StudioError",
    "TtsError",
    "WorkerError",
]


class ErrorCode(StrEnum):
    """全站错误码唯一真相。"""

    # ── core · 环境与门禁（T1.1）─────────────────────────────
    ENV_MISSING = "ENV_MISSING"
    ENV_NOT_ON_D_DRIVE = "ENV_NOT_ON_D_DRIVE"
    ENV_CONTRACT_VIOLATION = "ENV_CONTRACT_VIOLATION"
    DISK_LOW_C = "DISK_LOW_C"
    DISK_LOW_D = "DISK_LOW_D"
    PATH_MISSING = "PATH_MISSING"
    PATH_NOT_WRITABLE = "PATH_NOT_WRITABLE"

    # ── core · 外部工具 ─────────────────────────────────────
    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    FFMPEG_FILTER_MISSING = "FFMPEG_FILTER_MISSING"
    NVENC_UNAVAILABLE = "NVENC_UNAVAILABLE"
    FONT_MISSING = "FONT_MISSING"

    # ── core · 配置（T1.2）──────────────────────────────────
    CONFIG_MISSING = "CONFIG_MISSING"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_UNKNOWN_KEY = "CONFIG_UNKNOWN_KEY"
    CONFIG_PERSONA_INCOMPLETE = "CONFIG_PERSONA_INCOMPLETE"
    CONFIG_SECRET_LEAK = "CONFIG_SECRET_LEAK"
    CONFIG_PATH_OUT_OF_BOUNDS = "CONFIG_PATH_OUT_OF_BOUNDS"
    AUTH_LAN_WITHOUT_PASSWORD = "AUTH_LAN_WITHOUT_PASSWORD"

    # ── db（T1.3）───────────────────────────────────────────
    DB_NOT_WRITABLE = "DB_NOT_WRITABLE"
    DB_JOURNAL_MODE_INVALID = "DB_JOURNAL_MODE_INVALID"
    DB_MIGRATION_CHECKSUM_MISMATCH = "DB_MIGRATION_CHECKSUM_MISMATCH"
    DB_INTEGRITY_FAILED = "DB_INTEGRITY_FAILED"
    DB_MIGRATION_FAILED = "DB_MIGRATION_FAILED"
    DB_MIGRATION_MISSING_FILE = "DB_MIGRATION_MISSING_FILE"
    DB_MIGRATION_OUT_OF_ORDER = "DB_MIGRATION_OUT_OF_ORDER"
    DB_SCHEMA_DRIFT = "DB_SCHEMA_DRIFT"

    # ── 运维 · 备份 / 恢复 / GC（T4.12 · §03.7.4 / §03.7.5）──
    BACKUP_FAILED = "BACKUP_FAILED"
    BACKUP_NOT_FOUND = "BACKUP_NOT_FOUND"
    RESTORE_TARGET_EXISTS = "RESTORE_TARGET_EXISTS"
    RESTORE_REFUSED_LIVE_DB = "RESTORE_REFUSED_LIVE_DB"
    GC_PATH_OUT_OF_BOUNDS = "GC_PATH_OUT_OF_BOUNDS"
    GC_REFUSED_PROTECTED = "GC_REFUSED_PROTECTED"
    GC_ACTION_FAILED = "GC_ACTION_FAILED"

    # ── domain · 状态机（T1.4）──────────────────────────────
    STATE_TRANSITION_ILLEGAL = "STATE_TRANSITION_ILLEGAL"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"

    # ── queue / pool（T1.5 · T1.6）──────────────────────────
    JOB_DEAD = "JOB_DEAD"
    JOB_LEASE_LOST = "JOB_LEASE_LOST"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    POOL_CONCURRENCY_LIMIT = "POOL_CONCURRENCY_LIMIT"
    POOL_RATE_LIMITED = "POOL_RATE_LIMITED"
    UNIT_TIMEOUT = "UNIT_TIMEOUT"
    WORKER_DEAD = "WORKER_DEAD"

    # ── llm / agents（T1.8）─────────────────────────────────
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    LLM_UPSTREAM = "LLM_UPSTREAM"
    LLM_CIRCUIT_OPEN = "LLM_CIRCUIT_OPEN"
    LLM_ROUTE_MISSING = "LLM_ROUTE_MISSING"
    LLM_PROMPT_MISSING = "LLM_PROMPT_MISSING"
    LLM_PROMPT_DRIFT = "LLM_PROMPT_DRIFT"

    # ── ws / 实时推送（T1.7）────────────────────────────────
    WS_SUBSCRIPTION_INVALID = "WS_SUBSCRIPTION_INVALID"
    WS_CLIENT_SLOW = "WS_CLIENT_SLOW"
    WS_PROTOCOL_VIOLATION = "WS_PROTOCOL_VIOLATION"

    # ── tts（T2.x）──────────────────────────────────────────
    TTS_ENGINE_UNAVAILABLE = "TTS_ENGINE_UNAVAILABLE"
    TTS_OOM = "TTS_OOM"
    TTS_SENTENCE_FAILED = "TTS_SENTENCE_FAILED"
    TTS_AUDIO_QC_FAILED = "TTS_AUDIO_QC_FAILED"
    #: 引擎进程本身挂了（§04.3.3 的决策表里它与"这一句念不出来"是两件事：
    #: 前者要重载模型 / 重启服务，后者要换切分或降级）。T2.8 的
    #: ``STUDIO_FAULT=tts_down`` 注入的就是它。
    TTS_ENGINE_DOWN = "TTS_ENGINE_DOWN"
    #: 要用的音色在这台机器上**不存在**（§04.3.3 决策表的"音色缺失"）。
    #: 它必须在**写入** ``voice_map`` 的那一刻就报出来：音色名拼错了若放过去，
    #: 三句失败之后整条片子会降级成静音模式 —— 用户看到的是"换音色成功"，
    #: 拿到的是一支没有人声的成片。
    TTS_VOICE_MISSING = "TTS_VOICE_MISSING"
    #: 服务排队已满（429 背压）。与 ``TTS_OOM`` 分开：那个要**降并发**，
    #: 这个只是**此刻忙**，退避重试就会好 —— 两者的修复动作完全不同。
    TTS_BUSY = "TTS_BUSY"
    #: 引擎在超时内没念完（§04.3.3 决策表：**长句是超时主因** ⇒ 再切分后分段合成）。
    #: 与 ``TTS_ENGINE_DOWN`` 分开：那个是"引擎没了"，这个是"引擎在，只是这一句
    #: 它念得太久" —— 前者要唤醒引擎，后者要换切分。
    TTS_TIMEOUT = "TTS_TIMEOUT"
    #: 引擎**报成功**、产物却近乎无声（``RMS < -50 dBFS``，§04.3.3）。
    #: 这是最坏的一种"成功"：库里写着 ``done``、时间轴按实测时长排得好好的，
    #: 成片里那一句却没人声 —— 而且没有任何一处会报错（陷阱 #154 的第三种现身）。
    TTS_SILENT = "TTS_SILENT"
    #: 产物削波（峰值 ``> -0.5 dBFS``）：听感是破音，而且它会连带污染混音那一步。
    TTS_CLIP = "TTS_CLIP"
    #: 这一句的文本引擎吃不下（超长 / 含它读不出的字符）。与 ``TTS_SENTENCE_FAILED``
    #: 分开：那个要重试，这个重试多少次都一样，要**换切分**或直接占位。
    TTS_TEXT_INVALID = "TTS_TEXT_INVALID"
    #: 换音色会让 N 句重新合成，调用方**没有带上确认**（T2.9 · §05 的"二次确认"）。
    #: 与 ``VALIDATION_FAILED`` 分开：请求本身一个字都没写错，缺的是"你确认过代价了"。
    VOICE_MAP_CONFIRM_REQUIRED = "VOICE_MAP_CONFIRM_REQUIRED"

    # ── render（T3.x）───────────────────────────────────────
    RENDER_FILTER_SYNTAX = "RENDER_FILTER_SYNTAX"
    RENDER_WATERMARK_MISSING = "RENDER_WATERMARK_MISSING"
    #: 水印文件在，但**不可用**（不是 PNG / 没有透明通道 / 尺寸为 0 / 摆放越界）。
    #: 与 MISSING 分开：一个要「去拿图」，一个要「去修图或调边距」，修复动作不同。
    RENDER_WATERMARK_INVALID = "RENDER_WATERMARK_INVALID"
    RENDER_BROLL_MISSING = "RENDER_BROLL_MISSING"
    RENDER_TIMEOUT = "RENDER_TIMEOUT"
    RENDER_FAILED = "RENDER_FAILED"

    # ── publish（T5.x）──────────────────────────────────────
    PUBLISH_DISABLED = "PUBLISH_DISABLED"
    #: 二期平台的接口已定、实现为空（§06.2.1 · Q9）。与 DISABLED 分开：一个要「去开开关」，
    #: 一个要「去写实现」—— 面板上给的下一步动作完全不同。
    PUBLISH_NOT_IMPLEMENTED = "PUBLISH_NOT_IMPLEMENTED"
    PUBLISH_LOGIN_EXPIRED = "PUBLISH_LOGIN_EXPIRED"
    #: 选择器没命中（页面改版）。**取规格书 §06.10 的名字**：早先草稿写作
    #: ``PUBLISH_SELECTOR_STALE``，但"stale（过期）"说的是我们的认知，"miss（没命中）"
    #: 说的是这一次的观测 —— 排障时后者能直接对上截图，前者不能。
    PUBLISH_SELECTOR_MISS = "PUBLISH_SELECTOR_MISS"
    #: 限频触顶（§06.10）。**不是失败**：job 回 pending 顺延，不写 manual_required。
    PUBLISH_RATELIMIT = "PUBLISH_RATELIMIT"
    PUBLISH_UPLOAD_FAILED = "PUBLISH_UPLOAD_FAILED"
    PUBLISH_REVIEW_REJECTED = "PUBLISH_REVIEW_REJECTED"
    PUBLISH_TIMEOUT = "PUBLISH_TIMEOUT"
    #: 通用发布失败（``PublishError`` 的默认码）。
    PUBLISH_FAILED = "PUBLISH_FAILED"
    #: **我们没分类出来**的那一类（§4.6.1 的错误码表里有它）。
    #: 与 FAILED 分开：FAILED 是"知道哪一步坏了"，UNKNOWN 是"异常从没预期的地方冒出来"——
    #: 排障时前者照着 ``evidence.stage`` 看，后者要去看 traceback。
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"
    # T5.1 发布前二次校验（§06.4 / §06.10）：每一个都对应一道阻断门禁
    PRECHECK_WATERMARK = "PRECHECK_WATERMARK"
    PRECHECK_LOUDNESS = "PRECHECK_LOUDNESS"
    PRECHECK_SIMILARITY = "PRECHECK_SIMILARITY"
    PRECHECK_BANNED = "PRECHECK_BANNED"
    COVER_FAILED = "COVER_FAILED"

    # ── 定时发布调度（T5.6 · §04.6.5.1）─────────────────────
    #: 计划参数非法（窗口不是 HH:MM、start >= end、抖动越界、at_time 不带时区…）。
    #: 与 ``VALIDATION_FAILED`` 分开：规格书写的是 **400**（请求本身写错了），
    #: 而 422 在本项目里留给表单字段校验，前端据此把红字标到输入框上。
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    SCHEDULE_NOT_FOUND = "SCHEDULE_NOT_FOUND"

    # ── 数据报告（T5.7 · §03.3.19 / §06.7）─────────────────────
    #: 报告周期参数非法（weekly 缺 weekday、monthly 的 day_of_month 越界…）。
    #: 与 ``SCHEDULE_INVALID`` 同样是 **400**，但码分开：排障时
    #: 「是发布计划写错了还是报告周期写错了」是两个去处。
    REPORT_INVALID = "REPORT_INVALID"
    REPORT_NOT_FOUND = "REPORT_NOT_FOUND"

    # ── 输入源与选题池（T1.9 · §04.1.2 / §04.1.3 / §04.1.7）─────
    INPUT_SOURCE_EMPTY = "INPUT_SOURCE_EMPTY"
    INPUT_PARSE_FAILED = "INPUT_PARSE_FAILED"
    TOPIC_BATCH_NOT_FOUND = "TOPIC_BATCH_NOT_FOUND"
    TOPIC_DIRECTION_EMPTY = "TOPIC_DIRECTION_EMPTY"
    TOPIC_RULE_VIOLATED = "TOPIC_RULE_VIOLATED"
    TOPIC_NOT_FOUND = "TOPIC_NOT_FOUND"
    TOPIC_BATCH_RUNNING = "TOPIC_BATCH_RUNNING"
    TOPIC_SELECT_INVALID = "TOPIC_SELECT_INVALID"
    HOT_TEXT_EMPTY = "HOT_TEXT_EMPTY"

    # ── 写稿（T1.10 · §04.1.4 / §04.1.5）────────────────────────
    #: 二级产物（视频标题 + 核心论点）不合法：空 / 超长（文案三级流水线的中间一级）
    OUTLINE_INVALID = "OUTLINE_INVALID"
    SCRIPT_FORBIDDEN = "SCRIPT_FORBIDDEN"
    SCRIPT_NOT_FOUND = "SCRIPT_NOT_FOUND"
    SCRIPT_WORD_COUNT = "SCRIPT_WORD_COUNT"
    SCRIPT_DRAFT_FAILED = "SCRIPT_DRAFT_FAILED"

    # ── 审稿 / 改稿（T1.11 · §04.1.6）───────────────────────────
    REVIEW_FAILED = "REVIEW_FAILED"
    REVIEW_SCRIPT_MISSING = "REVIEW_SCRIPT_MISSING"
    EDIT_ROUNDS_EXHAUSTED = "EDIT_ROUNDS_EXHAUSTED"
    EDIT_FAILED = "EDIT_FAILED"

    # ── 确认闸（T1.11 · §04.4.4）────────────────────────────────
    APPROVAL_NOT_PENDING = "APPROVAL_NOT_PENDING"
    APPROVAL_COMMENT_REQUIRED = "APPROVAL_COMMENT_REQUIRED"

    # ── 进程编排（T1.12 · 原文附2 / §7.4）───────────────────────
    SERVICE_PORT_BUSY = "SERVICE_PORT_BUSY"
    SERVICE_ALREADY_RUNNING = "SERVICE_ALREADY_RUNNING"
    SERVICE_START_FAILED = "SERVICE_START_FAILED"
    SERVICE_START_TIMEOUT = "SERVICE_START_TIMEOUT"
    SERVICE_STOP_FAILED = "SERVICE_STOP_FAILED"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"
    #: 已经有一次"启动"在跑（T4.2）：`ServiceManager.start` 不是并发安全的，
    #: 两次点击会各自读到"没在跑"然后各拉一份进程（端口冲突 / 双份 worker）。
    SERVICE_START_BUSY = "SERVICE_START_BUSY"

    # ── REST 入参（T4.4）────────────────────────────────────
    VALIDATION_FAILED = "VALIDATION_FAILED"

    # ── 人物库（T4.13 · §02.4 / §04.1.6）─────────────────────
    #: 表单 / 库文件没过 `PersonaConfig` 校验（`context.field_errors` 逐字段给原因）。
    #: 与 `CONFIG_INVALID` 分开：那个是"启动时读到一份坏配置"，这个是"**你刚提交的
    #: 这一份**不能保存" —— 面板要能把红字标到具体输入框上，两者混用就标不出来了。
    PERSONA_INVALID = "PERSONA_INVALID"
    #: 人物库里没有这个 id / 备份目录里没有这个文件（回滚目标打错了）。
    PERSONA_NOT_FOUND = "PERSONA_NOT_FOUND"
    #: 「另存为」撞上同名条目（要覆盖得显式说一声）。
    PERSONA_EXISTS = "PERSONA_EXISTS"

    # ── 合成配置（T4.7 · §04.2.8 / §04.5.9）──────────────────
    #: 表单没过 `OutputsConfig` 校验，或改到了不可编辑的段 / 字段
    #: （`context.field_errors` 逐字段给原因）。与 `CONFIG_INVALID` 分开：
    #: 那个是"启动时读到一份坏配置"，这个是"**你刚提交的这一份**不能保存"。
    OUTPUTS_INVALID = "OUTPUTS_INVALID"
    #: `config/outputs.yaml` 不在（首次加载就失败，没有上一份可退回）。
    OUTPUTS_NOT_FOUND = "OUTPUTS_NOT_FOUND"
    #: 并发编辑：提交时带的 `source_sha256` 与盘上现状不符（**一个字节都没写**）。
    #: 409 而不是 422 —— 请求本身没写错，是"这个世界变了"。
    OUTPUTS_STALE = "OUTPUTS_STALE"

    # ── 媒资探测与素材库（T4.8 · §3.3.14 / §04.2.8）──────────────
    #: ``ffprobe`` / ``ffmpeg`` **跑不起来**（不在 PATH、超时、无法启动）。
    #: 与 ``MEDIA_UNDECODABLE`` 分开：那个是"这个文件坏了"，这个是"工具不在" ——
    #: 两者的处置完全不同（一个去修文件，一个去修环境），混成一个码就分不出来了。
    MEDIA_PROBE_FAILED = "MEDIA_PROBE_FAILED"
    #: 文件读不出来 / 是空的 / 不是可解码的媒体（0 字节、截断、扩展名骗人）。
    MEDIA_UNDECODABLE = "MEDIA_UNDECODABLE"
    #: 素材没过入库校验（时长 / 采样率 / 削波 / 段数），``context.problems`` 逐条给原因。
    ASSET_INVALID = "ASSET_INVALID"
    #: 素材库里没有这个 id（面板点了一个刚被删掉的条目）。
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    #: 上传的目标文件名已经被占了（409）。与 ``PERSONA_EXISTS`` 同一条裁定：
    #: "那个 id 已经有人了"是**状态**问题，不是入参写错了 —— 面板据此提示
    #: 「覆盖 / 换个名字」，而不是让用户对着 422 猜哪个字写错了。
    ASSET_EXISTS = "ASSET_EXISTS"

    # ── 兜底 ────────────────────────────────────────────────
    INTERNAL = "INTERNAL"


class StudioError(Exception):
    """全站异常基类。

    :param message: 人类可读描述（中文）
    :param code: 错误码；子类可设默认值
    :param context: 结构化上下文，落库与推送前端
    :param remediation: 建议的修复动作（doctor / WebUI 直接展示）
    """

    default_code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        context: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code: ErrorCode = code or self.default_code
        self.context: dict[str, Any] = dict(context or {})
        self.remediation = remediation

    def to_dict(self) -> dict[str, Any]:
        """序列化为可落库 / 可 JSON 化的字典。"""
        return {
            "code": str(self.code),
            "message": self.message,
            "context": self.context,
            "remediation": self.remediation,
            "type": type(self).__name__,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ConfigError(StudioError):
    """配置缺失 / 非法 / 未知键。"""

    default_code = ErrorCode.CONFIG_INVALID


class DoctorGateError(StudioError):
    """自检门禁未通过 ⇒ 拒绝启动（磁盘水位、journal_mode、局域网鉴权等）。"""

    default_code = ErrorCode.ENV_CONTRACT_VIOLATION


class ExternalToolError(StudioError):
    """外部进程（ffmpeg / ffprobe / nvidia-smi）不可用或非零退出。"""

    default_code = ErrorCode.FFMPEG_NOT_FOUND


class LlmError(StudioError):
    """LLM 网关失败（路由 / 传输 / 校验 / 预算）。

    网关**不抛裸异常**（§04.1.1 硬约束 5）：失败一律转成
    :class:`studio.agents.base.AgentResult` 的 ``error_code``。
    本异常族用于"需要中断当前调用"的场合（配置缺失、预算熔断、schema 漂移）。
    """

    default_code = ErrorCode.LLM_UPSTREAM


class LlmTransportError(LlmError):
    """一次 HTTP 往返失败（连接 / 超时 / 非 2xx / 响应体不合法）。

    :param retryable: 是否值得重试 —— 5xx / 429 / 超时 / 连接错误为 ``True``；
        4xx（除 408/409/425/429）为 ``False``（重试不会变好，白烧钱）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool = True,
        status: int | None = None,
        context: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        merged = dict(context or {})
        if status is not None:
            merged["status"] = status
        merged["retryable"] = retryable
        super().__init__(message, code=code, context=merged, remediation=remediation)
        self.retryable = retryable
        self.status = status


class LlmSchemaError(LlmError):
    """结构化输出不合法（解析失败或未过 JSON Schema）。"""

    default_code = ErrorCode.LLM_SCHEMA_INVALID


class LlmBudgetError(LlmError):
    """token / 成本预算超限且策略为 ``fail_task``。"""

    default_code = ErrorCode.LLM_BUDGET_EXCEEDED


class MediaError(StudioError):
    """媒资探测 / 编解码失败。"""

    default_code = ErrorCode.RENDER_FAILED


class TtsError(StudioError):
    """配音子系统失败。"""

    default_code = ErrorCode.TTS_ENGINE_UNAVAILABLE


class RenderError(StudioError):
    """渲染子系统失败。"""

    default_code = ErrorCode.RENDER_FAILED


class PublishError(StudioError):
    """发布子系统失败。"""

    default_code = ErrorCode.PUBLISH_FAILED


class QueueError(StudioError):
    """队列 / 租约 / 死信。"""

    default_code = ErrorCode.JOB_LEASE_LOST


class WorkerError(StudioError):
    """worker 生命周期（心跳超时、脉冲线程异常、重启次数超限）。"""

    default_code = ErrorCode.WORKER_DEAD


class StateTransitionError(StudioError):
    """状态机非法迁移或乐观锁冲突。"""

    default_code = ErrorCode.STATE_TRANSITION_ILLEGAL
