"""TTS 子系统（T2.x）—— 归一化 / 切分 / 引擎适配 / 常驻服务。

依赖方向（§02.1）：`tts/` **不 import FastAPI**；`agents/` 不得 import `tts/`。
本包可以依赖 `core/` 与 `domain/`（纯数据 + 状态机，零外部依赖）。
"""
