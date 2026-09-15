"""`tests/unit/domain` 包标记。

理由同 `tests/__init__.py`：本目录下的 `test_scoring.py` 与
`tests/integration/test_scoring.py` 重名，不带包标记时 pytest 会把两者都按顶层
模块 `test_scoring` 收集，直接报 `import file mismatch`（裁定 101）。
"""
