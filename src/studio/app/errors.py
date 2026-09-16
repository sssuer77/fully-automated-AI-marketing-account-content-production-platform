"""REST 面的异常翻译（T4.4 · §04.4.4）。

为什么要有这一层
----------------
:class:`~studio.core.errors.StudioError` 是**业务**异常
（``APPROVAL_COMMENT_REQUIRED`` / ``TASK_NOT_FOUND`` …），它不知道 HTTP 是什么。
让每个控制器自己 ``try/except`` 翻状态码，等于把映射表抄进每个端点 —— 抄漏一处，
前端就会把"退回没写意见"当成 500 去重试。

所以映射表**只有一份**，挂成应用级 handler。响应体就是
:meth:`StudioError.to_dict` 的输出（前端只认一种错误形状，见
``tests/contract/test_web_contracts.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from studio.core.errors import ErrorCode, StudioError

__all__ = ["DEFAULT_STATUS", "HTTP_STATUS_BY_CODE", "install_error_handlers", "status_for"]

#: 兜底：没登记的码按 500 —— "我们不知道这是什么错"不该假装成客户端的问题
DEFAULT_STATUS: Final[int] = 500

#: 业务错误码 → HTTP 状态码（只登记 REST 面真的会撞到的那些）
HTTP_STATUS_BY_CODE: Final[Mapping[ErrorCode, int]] = {
    # 找不到
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.PATH_MISSING: 404,
    ErrorCode.REVIEW_SCRIPT_MISSING: 404,
    ErrorCode.SCRIPT_NOT_FOUND: 404,
    ErrorCode.TOPIC_NOT_FOUND: 404,
    ErrorCode.TOPIC_BATCH_NOT_FOUND: 404,
    # 状态不对："你要做的这件事，现在这个状态做不了"
    ErrorCode.APPROVAL_NOT_PENDING: 409,
    ErrorCode.STATE_TRANSITION_ILLEGAL: 409,
    ErrorCode.STATE_VERSION_CONFLICT: 409,
    ErrorCode.TOPIC_BATCH_RUNNING: 409,
    ErrorCode.SERVICE_START_BUSY: 409,
    ErrorCode.SERVICE_ALREADY_RUNNING: 409,
    ErrorCode.SERVICE_PORT_BUSY: 409,
    ErrorCode.SERVICE_START_FAILED: 409,
    ErrorCode.SERVICE_START_TIMEOUT: 409,
    # doctor 门禁没过 ⇒ "现在这台机器不能起服务"（磁盘水位 / 环境变量 / journal_mode），
    # 是**状态冲突**而不是"请求错了"（422）或"我们崩了"（500）。
    ErrorCode.ENV_CONTRACT_VIOLATION: 409,
    # 入参不合法
    ErrorCode.APPROVAL_COMMENT_REQUIRED: 422,
    ErrorCode.SCRIPT_WORD_COUNT: 422,
    ErrorCode.TOPIC_DIRECTION_EMPTY: 422,
    ErrorCode.TOPIC_SELECT_INVALID: 422,
    ErrorCode.HOT_TEXT_EMPTY: 422,
    # 并发越界是**入参**问题（不是状态冲突）：voice 池 4 路在任何时刻都不该被接受，
    # 与「池现在是暂停的」那种 409 不是一回事。
    ErrorCode.POOL_CONCURRENCY_LIMIT: 422,
    ErrorCode.CONFIG_INVALID: 400,
    ErrorCode.VALIDATION_FAILED: 422,
    # 人物表单校验不通过是**入参**问题（不是状态冲突）：改一个字重提交就好，
    # 422 让前端把 `context.field_errors` 标到输入框上。
    ErrorCode.PERSONA_INVALID: 422,
    ErrorCode.PERSONA_NOT_FOUND: 404,
    # 同名冲突是**状态**问题（"那个 id 已经有人了"），不是入参写错了 ⇒ 409。
    # 面板据此弹「覆盖 / 换个 id」，而不是让用户对着 422 猜哪个字写错了。
    ErrorCode.PERSONA_EXISTS: 409,
    # 合成配置（T4.7）：表单校验不通过是**入参**问题（改一个数字重提交就好），
    # 422 让前端把 `context.field_errors` 标到输入框上。
    ErrorCode.OUTPUTS_INVALID: 422,
    ErrorCode.OUTPUTS_NOT_FOUND: 404,
    # 并发编辑（`source_sha256` 对不上）是**状态**问题 —— 请求本身没写错，
    # 是"你手上那份已经过时了" ⇒ 409。面板据此提示"刷新后重试"。
    ErrorCode.OUTPUTS_STALE: 409,
    # 素材库（T4.8）：素材本身不合格是**入参**问题（换一个文件 / 改一下授权就好），
    # 422 让面板把红字标到那一条上；"这条不在库里"是 404。
    ErrorCode.ASSET_INVALID: 422,
    ErrorCode.ASSET_NOT_FOUND: 404,
    ErrorCode.MEDIA_UNDECODABLE: 422,
    # 探不出来是**环境**问题（ffprobe 不在 PATH / 超时）⇒ 409：与 doctor 门禁
    # 「现在这台机器不能干这件事」同一条，而不是"我们崩了"。
    ErrorCode.MEDIA_PROBE_FAILED: 409,
    # 配音操作面（T2.9）：音色名是**入参**，写错了改一个字符串重提交就好 ⇒ 422，
    # 面板据此把红字标在音色下拉框上，并在 `context.available` 里给出能用的那些。
    ErrorCode.TTS_VOICE_MISSING: 422,
    # 「会重配 N 句」是**状态**问题而不是入参问题：请求一个字都没写错，缺的是
    # "你确认过这个代价了" ⇒ 409，与 `PERSONA_EXISTS` 的「覆盖 / 换个 id」同一条。
    ErrorCode.VOICE_MAP_CONFIRM_REQUIRED: 409,
}


def status_for(code: ErrorCode) -> int:
    """错误码 → 状态码（未登记 ⇒ :data:`DEFAULT_STATUS`）。"""
    return HTTP_STATUS_BY_CODE.get(code, DEFAULT_STATUS)


def install_error_handlers(app: FastAPI) -> None:
    """把 ``StudioError`` 挂成应用级 handler。"""

    @app.exception_handler(StudioError)
    async def _handle_studio_error(request: Request, exc: StudioError) -> JSONResponse:
        del request  # 请求内容不进响应体（避免把入参原样回显出去）
        return JSONResponse(status_code=status_for(exc.code), content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI 的入参校验错误也翻成同一个信封。

        默认它是 ``{"detail": [...]}`` —— 与业务错误的形状**不同**，于是前端
        要写两套解析（"422 时先看有没有 code"）。统一成一个形状，前端只认一种。
        """
        del request
        error = StudioError(
            "请求参数不合法",
            code=ErrorCode.VALIDATION_FAILED,
            context={"errors": exc.errors()},
        )
        return JSONResponse(status_code=status_for(error.code), content=error.to_dict())
