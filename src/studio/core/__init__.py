"""横切基础设施：配置、路径、日志、时钟、ID、错误码、自检。

本层是依赖图的**最底层**：只允许依赖标准库与 pydantic / structlog / psutil，
不得导入 ``studio.db`` 及以上任何层。
"""

from __future__ import annotations

__all__: list[str] = []
