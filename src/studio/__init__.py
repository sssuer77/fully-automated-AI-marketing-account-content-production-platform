"""AI 全自动营销号制片台 · 本地部署单机全链路。

规格书：``docs/spec/``（v3.2 终版）
施工入口：``todolist.md``

分层（自下而上，禁止反向依赖）::

    core  ->  db  ->  domain  ->  services  ->  pools / pipeline  ->  app

``core`` 只依赖标准库与少量基础设施三方库，**不得**导入 ``db`` / ``domain``。
"""

from __future__ import annotations

__all__ = ["__spec_version__", "__version__"]

__version__ = "0.1.0"
__spec_version__ = "3.2"
