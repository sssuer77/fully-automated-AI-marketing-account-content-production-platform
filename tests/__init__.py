"""测试包树（T1.7）。

`tests/` 及「需要被其它测试模块 import 的目录」必须带 `__init__.py`：
否则 mypy 会把同一个文件按「目录短名」和「tests.* 全名」各看一遍，
报 `Source file found twice under different module names`。
"""
