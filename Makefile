# 开发任务入口（与 tasks.ps1 等价，供 WSL / git-bash 使用）
# 用法：make check / make fmt / make doctor
SHELL := /bin/bash
PY    := uv run

.PHONY: check fmt lint type test test-all doctor sync sync-tts

check: fmt-check lint type test

fmt:
	$(PY) ruff format src tests workers scripts

fmt-check:
	$(PY) ruff format --check src tests workers scripts

lint:
	$(PY) ruff check src tests workers scripts --fix

type:
	$(PY) mypy src tests workers scripts

test:
	$(PY) pytest -m "not gpu and not slow and not net" -q

test-all:
	$(PY) pytest -q

doctor:
	$(PY) studio doctor

sync:
	uv sync --extra dev

sync-tts:
	uv sync --project tts