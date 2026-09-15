"""`core/` 层单元测试包。

T4.8 起这里的 `test_media.py` 与 `tests/unit/gc/test_media.py` **同名**，而 pytest 默认
按「文件短名」收集模块 ⇒ 两条路径只能收集到一条（`import file mismatch`）。带上
`__init__.py` 之后模块名变成 `tests.unit.core.test_media`，两条路径各归各的包。

这不是"为了测试方便"的妥协：`tests/unit/core/` 下的模块本来就属于同一个包树
（见 `tests/__init__.py`），补上它只是把这件事说清楚。
"""
