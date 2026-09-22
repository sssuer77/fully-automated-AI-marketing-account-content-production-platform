"""提示词面板 REST 面（T6.2）。

四个断言，四件事
----------------
① ``GET`` 给全条目 + 生效正文 + 版本 + 覆盖目录；
② ``PUT`` 存一份覆盖：**仓库文件一个字节都不动**，生效正文与版本都变；
③ 校验不过 ⇒ 422 ``VALIDATION_FAILED``，且盘上**没有**多出文件；
④ ``DELETE`` 还原 ⇒ 退回仓库那一份（幂等）。

为什么这里要专门验"仓库文件没动"
--------------------------------
``prompts/`` 是**入库的**，``prompts verify`` 拿 ``manifest.yaml`` 的 sha256 逐字
校验它。面板上一次误写如果落到仓库文件上，症状不是"报错"，而是"以后每次
``prompts verify`` 都红，而没人记得是谁改的" —— 那是最难查的一类回归。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.app.watchdog import WatchdogPump
from studio.core.paths import StudioPaths
from studio.db import migrate
from studio.db.repositories import AuditRepo
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

PROMPTS_URL = "/api/v1/prompts"

#: 一个真的注册过的条目（两段文件齐全）
NAME = "ideator"
SYSTEM_REL = "ideator/system.md"


def override_url(name: str, role: str) -> str:
    return f"{PROMPTS_URL}/{name}/override?file={role}"


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ 提示词是**生产那一份**，覆盖落 tmp。"""
    return StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppState]:
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
    # 与 test_topics_api 同一条理由：守护会真的 Popen 写稿池进程，而它攥着本用例
    # tmp 里的日志文件 ⇒ pytest 收尾删目录时 WinError 32。本文件与它无关。
    monkeypatch.setattr(WatchdogPump, "runnable", property(lambda self: False))
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def connection(state: AppState) -> sqlite3.Connection:
    return state.connections.get()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


def entry_of(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in payload["prompts"] if item["name"] == name)


# ══════════════════════════════════════════════════════════════════════
# ① 读
# ══════════════════════════════════════════════════════════════════════


def test_catalog_lists_every_prompt_with_its_live_version(client: TestClient, state: AppState) -> None:
    response = client.get(PROMPTS_URL)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["count"] == len(payload["prompts"])
    assert payload["count"] > 0
    assert payload["override_dir"] == state.paths.prompts_override_dir.as_posix()

    item = entry_of(payload, NAME)
    assert item["overridden"] is False
    assert item["prompt_version"].startswith(f"{item['version']}+")
    assert [file["role"] for file in item["files"]] == ["system", "user"]
    assert "persona_name" in item["variables"]
    assert item["files"][0]["text"]  # 生效正文真的读出来了


def test_catalog_rejects_nothing_it_does_not_know(client: TestClient) -> None:
    """``GET`` 不需要任何参数 —— 面板打开就能画。"""
    assert client.get(PROMPTS_URL, params={"nope": 1}).status_code == 200


# ══════════════════════════════════════════════════════════════════════
# ② 写：覆盖落 data/prompts，仓库文件不动
# ══════════════════════════════════════════════════════════════════════


def test_saving_an_override_leaves_the_repo_file_alone(client: TestClient, state: AppState) -> None:
    repo_before = (state.paths.prompts_dir / SYSTEM_REL).read_text(encoding="utf-8")
    version_before = entry_of(client.get(PROMPTS_URL).json(), NAME)["prompt_version"]

    response = client.put(f"{PROMPTS_URL}/{NAME}", json={"system": f"{repo_before}\n面板上加的一句。\n"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["changed"] == ["system"]
    assert body["restored"] is False
    assert body["entry"]["overridden"] is True
    assert body["entry"]["prompt_version"] != version_before
    assert "面板上加的一句" in body["entry"]["files"][0]["text"]

    # 仓库文件逐字未动；覆盖文件落在 data/prompts 下的**同一个相对路径**
    assert (state.paths.prompts_dir / SYSTEM_REL).read_text(encoding="utf-8") == repo_before
    assert (state.paths.prompts_override_dir / SYSTEM_REL).is_file()

    # 再读一次：生效的就是覆盖那份
    assert "面板上加的一句" in entry_of(client.get(PROMPTS_URL).json(), NAME)["files"][0]["text"]

    ops = AuditRepo(state.connections.get()).list_recent(limit=10)
    assert [op.action for op in ops] == ["prompt.updated"]
    assert ops[0].before["prompt_version"] == version_before
    assert ops[0].after["prompt_version"] == body["entry"]["prompt_version"]


def test_saving_the_same_text_changes_nothing(client: TestClient, state: AppState) -> None:
    current = entry_of(client.get(PROMPTS_URL).json(), NAME)["files"][0]["text"]

    response = client.put(f"{PROMPTS_URL}/{NAME}", json={"system": current})
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == []
    assert response.json()["entry"]["overridden"] is False
    assert not (state.paths.prompts_override_dir / SYSTEM_REL).exists()
    assert AuditRepo(state.connections.get()).list_recent(limit=10) == []


# ══════════════════════════════════════════════════════════════════════
# ③ 校验不过 ⇒ 一个字节都不落盘
# ══════════════════════════════════════════════════════════════════════


def test_an_unknown_variable_is_rejected_without_writing(client: TestClient, state: AppState) -> None:
    response = client.put(f"{PROMPTS_URL}/{NAME}", json={"system": "标题：{{nobody_fills_this}}"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["context"]["unknown"] == ["nobody_fills_this"]
    assert body["remediation"]
    assert not (state.paths.prompts_override_dir / SYSTEM_REL).exists()


def test_the_body_needs_at_least_one_segment(client: TestClient) -> None:
    assert client.put(f"{PROMPTS_URL}/{NAME}", json={}).status_code == 422
    assert client.put(f"{PROMPTS_URL}/{NAME}", json={"nope": "x"}).status_code == 422


def test_an_unregistered_name_is_a_422_not_a_500(client: TestClient) -> None:
    response = client.put(f"{PROMPTS_URL}/nope.nothing", json={"system": "随便写点什么。"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_FAILED"


# ══════════════════════════════════════════════════════════════════════
# ④ 还原
# ══════════════════════════════════════════════════════════════════════


def test_restore_goes_back_to_the_repo_copy(client: TestClient, state: AppState) -> None:
    version_before = entry_of(client.get(PROMPTS_URL).json(), NAME)["prompt_version"]
    client.put(f"{PROMPTS_URL}/{NAME}", json={"system": "改一下。\n"})

    response = client.delete(override_url(NAME, "system"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == ["system"]
    assert body["restored"] is True
    assert body["entry"]["overridden"] is False
    assert body["entry"]["prompt_version"] == version_before
    assert not (state.paths.prompts_override_dir / SYSTEM_REL).exists()


def test_restore_is_idempotent_and_needs_a_known_role(client: TestClient) -> None:
    again = client.delete(override_url(NAME, "system"))
    assert again.status_code == 200, again.text
    assert again.json()["changed"] == []
    assert again.json()["restored"] is True

    bad = client.delete(override_url(NAME, "nope"))
    assert bad.status_code == 422  # Query(pattern=...) 在契约层就拦掉了
