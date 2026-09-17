"""``tts`` 进程入口：CosyVoice 常驻推理服务（T1.12 留入口 · T2.2 落地实现）。

**未就绪时怎么办**：直接抛错退出，退出码 1，原因写进 ``data/logs/tts.log``。
启动器**先问再拉**（裁定 103），所以这个进程在一期根本不会被拉起 —— 其余四个
进程照常运行（降级模式 §1.7：没有 GPU / 没有模型时链路不断）。
"""

from __future__ import annotations

from collections.abc import Callable

from studio.core.entry import run_entry
from studio.core.errors import ErrorCode, StudioError

#: CosyVoice 常驻推理服务入口（`None` = 还没落地）。
_serve_server: Callable[[], None] | None = None

try:  # T2.2 落地后自动可用，不必回来改这个文件
    # 三处讲究，都是为了让上面那句承诺真的成立（均已实测）：
    # ① 先导入到中间名 `_serve_impl` 再转手赋值，**不直接 `as _serve_server`** ——
    #    声明 + 同名 import 会让 mypy 在「模块还不存在」时报 no-redef。
    # ② `unused-ignore` 与 `import-untyped` **并列写** —— 模块一旦真的存在，这条
    #    ignore 就成了多余的，strict 的 warn_unused_ignores 会反过来报 unused-ignore。
    # ③ 变量类型写全 `| None` —— 否则 T2.2 落地后 `if _serve_server is None`
    #    会被 warn_unreachable 判成永远为假。
    # （不能改用 mypy.ini 的 `ignore_missing_imports`：它只管 import-not-found，
    #  管不了「已安装但缺 py.typed」这条 import-untyped。）
    from studio.tts.server import serve as _serve_impl  # type: ignore[import-untyped, unused-ignore]

    _serve_server = _serve_impl
except ImportError:
    pass


def _serve() -> None:
    if _serve_server is None:
        raise StudioError(
            "CosyVoice 常驻推理服务尚未落地（studio.tts.server）",
            code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
            context={"module": "studio.tts.server", "task": "T2.2"},
            remediation="T2.2 落地后本进程自动就绪；在此之前制片台以降级模式运行（§1.7）",
        )
    _serve_server()


if __name__ == "__main__":
    raise SystemExit(run_entry("tts", _serve))
