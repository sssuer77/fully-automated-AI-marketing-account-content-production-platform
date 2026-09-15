"""应用层（FastAPI + WS）：`create_app()` 是唯一入口。"""

from __future__ import annotations

from studio.app.deps import AppState, build_state
from studio.app.main import create_app

__all__ = ["AppState", "build_state", "create_app"]
