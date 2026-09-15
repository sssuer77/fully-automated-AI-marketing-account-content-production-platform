"""稿件面板 REST 面集成测试（T4.4 验收 · §04.4.5 第 3 行）。

验收三条（todolist T4.4）
-------------------------
① **双通道明细**：``rule_detail`` 与 ``llm_detail`` 各自原样透出（"为什么是 B 级"
   要能答上来），且 ``issues`` 可见；
② **版本对照**：``v1 → v2`` 只报真正改动的那几句（插入一句不能把后面全标成"改了"）；
③ ``revision_round`` 可见 —— 原文 §2.2⑦ 的"稿件 + 评分 + 修改次数"三样齐备。

为什么分数与轮次都自己造
------------------------
面板读的是 ``review_scores`` / ``scripts`` 的行。真跑 Reviewer 只会让这几条用例
依赖网络与提示词版本，而它们要验的是**读取口径**有没有漏字段。
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
from studio.core.paths import StudioPaths
from studio.db import migrate
from studio.db.repositories import ApprovalRepo, ReviewRepo, ScriptRepo
from studio.domain import TaskService, TaskStatus
from studio.domain.scoring import LLM_DIMENSIONS
from studio.ws.hub import HubSettings

DETAIL_URL = "/api/v1/scripts/{task_id}"
VERSIONS_URL = "/api/v1/scripts/{task_id}/versions"
DIFF_URL = "/api/v1/scripts/{task_id}/diff"

ISSUE: dict[str, Any] = {
    "code": "weak_hook",
    "severity": "major",
    "target": "hook",
    "detail": "开场太平",
    "suggestion": "把结果前置",
}


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
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


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def seed_task(connection: sqlite3.Connection, *, title: str = "MC跑酷最难的一跳") -> str:
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    tasks.transition(task_id, TaskStatus.DRAFTING, actor="test")
    return task_id


def save_version(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    texts: list[str],
    revision_round: int = 0,
    hook: str = "熊大又整活了",
) -> str:
    """落一版稿件（``texts`` 是逐句文本，``seq`` 按位置生成）。"""
    body = "".join(texts)
    return (
        ScriptRepo(connection)
        .save_draft(
            task_id=task_id,
            title="MC跑酷最难的一跳",
            hook=hook,
            body_md=body,
            cta="点个关注看下集",
            word_count=len(body),
            est_duration_ms=len(body) * 200,
            speaker_ratio={"bigbear": 0.5, "littlebear": 0.5},
            outline={"hook_3s": hook, "segments": [], "cta": "点个关注看下集"},
            revision_round=revision_round,
            editor_notes=["把开场换成结果前置"],
            sentences=[
                {
                    "seq": index,
                    "text_raw": text,
                    "text": text,
                    "speaker": "bigbear" if index % 2 else "littlebear",
                    "emotion": "兴奋",
                    "pause_after_ms": 200,
                }
                for index, text in enumerate(texts, start=1)
            ],
        )
        .script_id
    )


def save_review(
    connection: sqlite3.Connection,
    task_id: str,
    script_id: str,
    *,
    round_no: int,
    total: float = 7.2,
    grade: str = "B",
    llm_score: float = 6.0,
) -> None:
    ReviewRepo(connection).insert(
        task_id=task_id,
        script_id=script_id,
        round_no=round_no,
        rule_total=10.0,
        rule_detail={"positioning": {"score": 10.0, "detail": "选题与大纲一致"}},
        llm_total=llm_score,
        llm_detail={name: {"score": llm_score, "comment": f"{name} 的评语"} for name in LLM_DIMENSIONS},
        total=total,
        grade=grade,
        decision="need_edit",
        issues=[ISSUE],
        llm_model="fake-model",
        prompt_version="v-test",
    )


# ══════════════════════════════════════════════════════════════════════
# ① 双通道明细
# ══════════════════════════════════════════════════════════════════════


def test_detail_exposes_both_channels_and_issues(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id = seed_task(connection)
    script_id = save_version(connection, task_id, texts=["甲。", "乙。", "丙。"])
    save_review(connection, task_id, script_id, round_no=1)

    body = client.get(DETAIL_URL.format(task_id=task_id)).json()
    assert body["task_status"] == "drafting"
    assert body["script"]["body_md"] == "甲。乙。丙。"
    assert [item["seq"] for item in body["sentences"]] == [1, 2, 3]
    (review,) = body["reviews"]
    assert review["rule_total"] == 10.0
    assert review["llm_total"] == 6.0
    assert set(review["llm_detail"]) == set(LLM_DIMENSIONS)
    assert review["rule_detail"]["positioning"]["detail"] == "选题与大纲一致"
    assert review["issues"] == [ISSUE]
    assert review["grade"] == "B"


def test_detail_lists_every_round_in_order(client: TestClient, connection: sqlite3.Connection) -> None:
    """面板要能并排显示"第 1 轮 6.3 分 / 第 2 轮 7.1 分"，否则"改稿有没有用"说不清。"""
    task_id = seed_task(connection)
    first = save_version(connection, task_id, texts=["甲。", "乙。"], revision_round=0)
    save_review(connection, task_id, first, round_no=1, total=6.3, llm_score=5.0)
    second = save_version(connection, task_id, texts=["甲改。", "乙。"], revision_round=1)
    save_review(connection, task_id, second, round_no=2, total=7.1, llm_score=6.5)

    body = client.get(DETAIL_URL.format(task_id=task_id)).json()
    assert [item["round_no"] for item in body["reviews"]] == [1, 2]
    assert [item["total"] for item in body["reviews"]] == [6.3, 7.1]
    assert body["script"]["version"] == 2
    assert body["script"]["is_active"] is True


def test_detail_includes_the_pending_approval(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id = seed_task(connection)
    script_id = save_version(connection, task_id, texts=["甲。"])
    ApprovalRepo(connection).request(
        task_id=task_id, script_id=script_id, grade="B", score_total=7.2, revision_round=1
    )
    body = client.get(DETAIL_URL.format(task_id=task_id)).json()
    assert body["approval"] is not None
    assert body["approval"]["grade"] == "B"
    assert body["approval"]["status"] == "pending"


def test_detail_without_an_approval_says_so(client: TestClient, connection: sqlite3.Connection) -> None:
    """不在闸里 ⇒ ``approval=None``（面板据此决定是否显示决断条）。"""
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。"])
    assert client.get(DETAIL_URL.format(task_id=task_id)).json()["approval"] is None


def test_detail_shows_the_revision_round_from_the_task(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """原文 §2.2⑦ 的"修改次数"读的是**任务**上的轮次（面板要显示的就是它）。"""
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。"])
    tasks = TaskService(connection)
    tasks.transition(task_id, TaskStatus.REVIEWING, actor="test")
    tasks.transition(task_id, TaskStatus.EDITING, actor="test", bump_revision=True)
    assert client.get(DETAIL_URL.format(task_id=task_id)).json()["revision_round"] == 1


# ══════════════════════════════════════════════════════════════════════
# 找不到的两种"没有"
# ══════════════════════════════════════════════════════════════════════


def test_unknown_task_is_not_found(client: TestClient) -> None:
    response = client.get(DETAIL_URL.format(task_id="no-such-task"))
    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_task_without_a_script_is_not_found(client: TestClient, connection: sqlite3.Connection) -> None:
    """两种"没有"分开报：任务不存在 vs 任务在但还没写稿 —— 面板提示不一样。"""
    task_id = seed_task(connection)
    response = client.get(DETAIL_URL.format(task_id=task_id))
    assert response.status_code == 404
    assert response.json()["code"] == "REVIEW_SCRIPT_MISSING"


# ══════════════════════════════════════════════════════════════════════
# ② 版本对照
# ══════════════════════════════════════════════════════════════════════


def test_versions_list_carries_sentence_counts(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。", "乙。"], revision_round=0)
    save_version(connection, task_id, texts=["甲。", "乙。", "丙。"], revision_round=1)
    body = client.get(VERSIONS_URL.format(task_id=task_id)).json()
    assert [item["version"] for item in body["versions"]] == [2, 1]
    assert [item["sentence_count"] for item in body["versions"]] == [3, 2]
    assert [item["is_active"] for item in body["versions"]] == [True, False]


def test_diff_reports_only_the_changed_sentence(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。", "乙。", "丙。"])
    save_version(connection, task_id, texts=["甲。", "乙改。", "丙。"])
    body = client.get(DIFF_URL.format(task_id=task_id), params={"from_version": 1, "to_version": 2}).json()
    assert body["from_version"] == 1 and body["to_version"] == 2
    assert body["summary"] == {"unchanged": 2, "changed": 1, "added": 0, "removed": 0, "total": 1}
    changed = [item for item in body["changes"] if item["op"] != "equal"]
    assert changed == [
        {
            "op": "replace",
            "old_seq": 2,
            "new_seq": 2,
            "old_text": "乙。",
            "new_text": "乙改。",
        }
    ]


def test_diff_insert_does_not_flag_everything_after_it(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """★ 插入一句不能让后面全部显示成"改了"（那会让人以为整篇重写了）。"""
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。", "乙。", "丙。"])
    save_version(connection, task_id, texts=["甲。", "乙。", "插。", "丙。"])
    body = client.get(DIFF_URL.format(task_id=task_id), params={"from_version": 1, "to_version": 2}).json()
    assert body["summary"]["added"] == 1
    assert body["summary"]["changed"] == 0
    assert body["summary"]["unchanged"] == 3


def test_diff_can_be_read_backwards(client: TestClient, connection: sqlite3.Connection) -> None:
    """反向对照（v2 → v1）是合法用法：看"改回去了没有"。"""
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。"])
    save_version(connection, task_id, texts=["甲改。"])
    body = client.get(DIFF_URL.format(task_id=task_id), params={"from_version": 2, "to_version": 1}).json()
    assert body["summary"]["changed"] == 1
    assert body["changes"][0]["old_text"] == "甲改。"
    assert body["changes"][0]["new_text"] == "甲。"


def test_diff_with_an_unknown_version_is_not_found(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    task_id = seed_task(connection)
    save_version(connection, task_id, texts=["甲。"])
    response = client.get(DIFF_URL.format(task_id=task_id), params={"from_version": 1, "to_version": 9})
    assert response.status_code == 404
    assert response.json()["code"] == "SCRIPT_NOT_FOUND"


def test_diff_requires_both_versions(client: TestClient, connection: sqlite3.Connection) -> None:
    """两个版本号都必须显式给 —— 默认值会让人对着错误的对照下结论。"""
    task_id = seed_task(connection)
    response = client.get(DIFF_URL.format(task_id=task_id), params={"from_version": 1})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
