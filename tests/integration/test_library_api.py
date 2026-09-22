"""成片库 REST 面集成测试（T5.11 验收 · §06.11 / §06.12）。

验收七条，一条一个用例
----------------------
① 空盘 ⇒ 空列表 + 一句「去哪出片」；
② **一行 = 一条任务**：一条任务三支成片仍是一行（``versions`` 只是信息，不是选择）；
③ 认不出的 mp4 不进库（``data/output/videos/`` 是人也会放东西的地方）；
④ 任务没了照样列出来，但 ``task_found=false`` ⇒ 面板说得出发不了；
⑤ 批量投递：**一个请求**、逐条结论、作业真进 publish 池；
⑥ 投递期就挡掉两种「发不出去」：任务不存在（``missing``）与盘上没有成片（``skipped``）；
⑦ 入参不合法（空清单 / 超上限 / 多带字段）⇒ 422 ``VALIDATION_FAILED``。

四条纪律（与 ``test_multi_account.py`` 同一套）
--------------------------------------------
1. **临时家目录**：真配置抄一份 —— 发布池要读 ``publish.yaml`` 才知道哪些平台有账号。
2. **真库真盘**：成片是 tmp 里真写出来的文件，作业回 ``jobs`` 表查。「返回 200」只说明没抛。
3. **不碰真平台**：这一层只投作业（真发布是 publish 池 worker 干的），所以一个浏览器都不起。
4. **判据只有一份**：``read_library`` 与 REST 面返回的是同一份东西 —— 两条路各断言一次，
   免得「服务层对、接口错」这种只在面板上看得见的偏差溜过去。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.db.queue import Job, JobStore
from studio.domain.enums import TaskStatus
from studio.domain.publish import unit_ref
from studio.domain.task_service import TaskService
from studio.services.library_service import BATCH_MAX, read_library
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

LIBRARY_URL = "/api/v1/library"
PUBLISH_URL = "/api/v1/library/publish"

#: 演练台（T5.9）：唯一一个「发得出去但不会真发到网上」的目标。整条链路离线可验靠它。
REHEARSAL = "other"
#: 演练台那个账号（``config/publish.yaml`` 里的 ``_rehearsal``）。
REHEARSAL_ACCOUNT = "_rehearsal"

#: 任务状态机的「一路出片」路径（建任务 → 成片完成）。
TO_COMPLETED: tuple[TaskStatus, ...] = (
    TaskStatus.DRAFTING,
    TaskStatus.REVIEWING,
    TaskStatus.QUEUED_VOICE,
    TaskStatus.VOICING,
    TaskStatus.QUEUED_RENDER,
    TaskStatus.RENDERING,
    TaskStatus.COMPLETED,
)


class _Probe:
    """假资源探针（不碰真 ``nvidia-smi`` 与真磁盘）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-22T06:00:00.000Z",
            cpu_pct=10.0,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=50.0,
            disk_free_d_gb=100.0,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=False,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=8192,
        )


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真配置抄一份（发布池要读它才知道哪些平台有账号）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        shutil.copyfile(source, value.config_dir / source.name)
    return value


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
    )
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
# 小工具
# ══════════════════════════════════════════════════════════════════════


def _error(body: dict[str, Any]) -> str:
    assert "code" in body, body
    return str(body["code"])


def _completed_task(connection: sqlite3.Connection, *, title: str = "离谱跑酷地图") -> str:
    """建一条「已出片」的任务（``completed``）。成片由 :func:`_final` 另外放。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    for target in TO_COMPLETED:
        tasks.transition(task_id, target, actor="test", reason="setup")
    connection.commit()
    return task_id


def _final(
    paths: StudioPaths,
    task_id: str,
    *,
    stamp: str = "20260922-152331",
    degraded: bool = False,
    size: int = 64,
) -> Path:
    """往盘上放一支成片，名字与真机渲染产物同形（``{stamp}_{task_id}_final.mp4``）。"""
    suffix = "_final_720p.mp4" if degraded else "_final.mp4"
    video = paths.videos_dir / f"{stamp}_{task_id}{suffix}"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * size)
    return video


def _manifest(paths: StudioPaths, task_id: str, *, final: Path, duration_ms: int) -> None:
    """写 ``manifest.json``（出片那份的缩影：这一版成片在哪、多长）。"""
    target = paths.manifest_json(task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"task_id": task_id, "final": final.as_posix(), "duration_ms": duration_ms}
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _jobs(connection: sqlite3.Connection, task_id: str) -> tuple[Job, ...]:
    return JobStore(connection).list_jobs(pool="publish", task_id=task_id, limit=10)


# ══════════════════════════════════════════════════════════════════════
# ① 空盘
# ══════════════════════════════════════════════════════════════════════


def test_empty_library_says_where_to_go(client: TestClient) -> None:
    """一支成片都没有 ⇒ 空列表 + 一句「下一步去哪」。

    空列表本身不是问题，「我该干什么」才是 —— 所以 ``hint`` 是这一屏的一部分。
    """
    response = client.get(LIBRARY_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] > 0
    assert body["hint"], "空库必须给一句下一步，否则这一屏只是一块白板"


# ══════════════════════════════════════════════════════════════════════
# ② 一行 = 一条任务
# ══════════════════════════════════════════════════════════════════════


def test_three_versions_of_one_task_are_one_row(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """一条任务重出过三版 ⇒ **一行**，``versions=3``。

    按文件列三行的话，勾第二行与勾第三行的结果**一模一样**（发布池认任务号，自己去找
    成片）—— 那是这一屏最容易撒的一句谎。
    """
    task_id = _completed_task(connection)
    _final(paths, task_id, stamp="20260922-120000")
    _final(paths, task_id, stamp="20260922-140000")
    newest = _final(paths, task_id, stamp="20260922-152331")

    body = client.get(LIBRARY_URL).json()

    assert body["total"] == 1
    (row,) = body["items"]
    assert row["task_id"] == task_id
    assert row["versions"] == 3
    assert row["task_found"] is True
    assert row["title"] == "离谱跑酷地图"
    assert row["task_status"] == TaskStatus.COMPLETED.value
    assert row["video_name"] == newest.name, "发布池挑的是最新那一支，面板要显示同一支"
    assert row["video_url"] == f"/api/v1/render/videos/{newest.name}"
    assert row["video_path"] == newest.as_posix()
    assert row["size_bytes"] == newest.stat().st_size
    assert row["publications"] == []


def test_manifest_wins_over_directory_glob(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """``manifest.json`` 说的那一支优先（与发布池同一份判据）。

    时长也来自 manifest：成片库**不现场 ffprobe**（一屏几十行就是几十次子进程）。
    """
    task_id = _completed_task(connection)
    _final(paths, task_id, stamp="20260922-120000")
    picked = _final(paths, task_id, stamp="20260922-100000", degraded=True)
    _manifest(paths, task_id, final=picked, duration_ms=205_760)

    (row,) = client.get(LIBRARY_URL).json()["items"]

    assert row["video_name"] == picked.name
    assert row["duration_ms"] == 205_760
    assert row["versions"] == 2


def test_duration_is_null_without_manifest(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """没有 manifest ⇒ 时长为 ``None``，但**这一行照样在**（片子是真的，元数据是锦上添花）。"""
    task_id = _completed_task(connection)
    _final(paths, task_id)

    (row,) = client.get(LIBRARY_URL).json()["items"]

    assert row["duration_ms"] is None


# ══════════════════════════════════════════════════════════════════════
# ③ 认不出的文件
# ══════════════════════════════════════════════════════════════════════


def test_unrecognised_mp4_is_ignored(client: TestClient, paths: StudioPaths) -> None:
    """名字对不上的一律不进来：``data/output/videos/`` 是人也会放东西的地方。"""
    paths.videos_dir.mkdir(parents=True, exist_ok=True)
    (paths.videos_dir / "手工导出的片子.mp4").write_bytes(b"\x00" * 8)
    (paths.videos_dir / "20260922-152331_final.mp4").write_bytes(b"\x00" * 8)
    (paths.videos_dir / "readme.txt").write_text("不是片子", encoding="utf-8")

    body = client.get(LIBRARY_URL).json()

    assert body["items"] == [], "认不出来是常态，不是异常 —— 但它不该冒充一条成片"
    assert body["hint"]


# ══════════════════════════════════════════════════════════════════════
# ④ 任务没了
# ══════════════════════════════════════════════════════════════════════


def test_video_without_task_is_listed_but_blocked(client: TestClient, paths: StudioPaths) -> None:
    """任务被删、只剩片子 ⇒ **照样列出来**，但标着 ``task_found=false``。

    让它凭空消失的话，操作员会以为片子丢了；列出来 + 明说发不了，才是他需要的那句话
    （发布池要读任务，读不到就发不出去）。
    """
    _final(paths, "ghost-task")

    (row,) = client.get(LIBRARY_URL).json()["items"]

    assert row["task_id"] == "ghost-task"
    assert row["task_found"] is False
    assert row["title"] == ""
    assert row["task_status"] is None


# ══════════════════════════════════════════════════════════════════════
# ⑤ 批量投递
# ══════════════════════════════════════════════════════════════════════


def test_batch_publish_queues_every_picked_task(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """一次请求投两条 ⇒ 两条作业真进池，逐条回结论且**与入参同序同长**。"""
    first = _completed_task(connection, title="第一条")
    second = _completed_task(connection, title="第二条")
    _final(paths, first)
    _final(paths, second, stamp="20260922-160000")

    response = client.post(PUBLISH_URL, json={"task_ids": [first, second], "platforms": [REHEARSAL]})

    assert response.status_code == 200
    body = response.json()
    assert body["task_total"] == 2
    assert body["queued_total"] == 2
    assert body["platforms"] == [REHEARSAL]
    assert [item["task_id"] for item in body["items"]] == [first, second]
    for item in body["items"]:
        assert (item["queued"], item["skipped"], item["duplicates"]) == (1, [], [])
        assert item["missing"] is False

    assert len(_jobs(connection, first)) == 1, "作业要真进 publish 池，不只是回一句成功"
    assert len(_jobs(connection, second)) == 1
    assert _jobs(connection, first)[0].unit_ref == unit_ref(REHEARSAL, REHEARSAL_ACCOUNT)


def test_batch_publish_is_idempotent_per_task(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """再投一次 ⇒ 作业**不翻倍**，结论落在 ``duplicates`` 上。

    「早就投过」的处置动作是"什么都不用做"，与其余跳过分开 —— 合成一句"跳过 2 条"
    之后，操作员会去改一个本来就没问题的配置。
    """
    task_id = _completed_task(connection)
    _final(paths, task_id)
    body = {"task_ids": [task_id], "platforms": [REHEARSAL]}

    assert client.post(PUBLISH_URL, json=body).json()["queued_total"] == 1
    again = client.post(PUBLISH_URL, json=body).json()

    assert again["queued_total"] == 0
    (item,) = again["items"]
    assert item["duplicates"] == [f"{REHEARSAL}/{REHEARSAL_ACCOUNT}"]
    assert item["skipped"], "跳过也要说一句为什么（面板上那一行不能只有结论没有理由）"
    assert len(_jobs(connection, task_id)) == 1, "幂等命中不该多出一条作业"


def test_batch_publish_dedupes_repeated_task_ids(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """同一个任务号写两遍 ⇒ 只投一次（面板勾不出这种情况，接口不能靠"面板不会这样"）。"""
    task_id = _completed_task(connection)
    _final(paths, task_id)

    body = client.post(PUBLISH_URL, json={"task_ids": [task_id, task_id], "platforms": [REHEARSAL]}).json()

    assert body["task_total"] == 1
    assert body["queued_total"] == 1


# ══════════════════════════════════════════════════════════════════════
# ⑥ 投递期就挡掉的两种
# ══════════════════════════════════════════════════════════════════════


def test_missing_task_is_reported_as_missing_not_as_no_video(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """任务号打错了 ⇒ ``missing``，**不是**「盘上没有成片」。

    两句都是真话，但指向的下一步动作完全不同（改任务号 vs 先去出片）。先判任务再判
    成片，就是为了让面板能说出正确的那一句。
    """
    body = client.post(PUBLISH_URL, json={"task_ids": ["no-such-task"]}).json()

    (item,) = body["items"]
    assert item["missing"] is True
    assert item["queued"] == 0
    assert item["skipped"] == []
    assert body["queued_total"] == 0


def test_task_without_final_video_is_skipped(client: TestClient, connection: sqlite3.Connection) -> None:
    """任务在、盘上没成片 ⇒ ``skipped``，且**一条作业都不投**。

    投出去的话，发布池会在认领之后报 ``RENDER_FAILED``、那条作业进死信、``publications``
    里连一行都没有 —— 用户看到的是「投了，然后没了」。
    """
    task_id = _completed_task(connection)

    body = client.post(PUBLISH_URL, json={"task_ids": [task_id]}).json()

    (item,) = body["items"]
    assert item["queued"] == 0
    assert item["missing"] is False
    assert any("成片" in line for line in item["skipped"]), item["skipped"]
    assert _jobs(connection, task_id) == ()


def test_unselectable_platform_lands_in_skipped(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """点名一个 ``enabled=false`` 的平台 ⇒ 落 ``skipped`` 而**不抛**。

    批量指令里"有一路发不出去"是常态，把它做成异常的话，调用方就得为"投一批"写
    try/except —— 而这一批里其余几条本来能发出去。
    """
    task_id = _completed_task(connection)
    _final(paths, task_id)

    body = client.post(PUBLISH_URL, json={"task_ids": [task_id], "platforms": ["xiaohongshu"]}).json()

    (item,) = body["items"]
    assert item["queued"] == 0
    assert any("xiaohongshu" in line for line in item["skipped"]), item["skipped"]
    assert _jobs(connection, task_id) == ()


# ══════════════════════════════════════════════════════════════════════
# ⑦ 入参
# ══════════════════════════════════════════════════════════════════════


def test_empty_selection_is_422(client: TestClient) -> None:
    """空清单是**调用方**的错，不该混进 ``items`` 里冒充"这一条没投出去"。"""
    response = client.post(PUBLISH_URL, json={"task_ids": []})

    assert response.status_code == 422
    assert _error(response.json()) == ErrorCode.VALIDATION_FAILED.value


def test_over_batch_max_is_422(client: TestClient) -> None:
    """超过上限同样在**入参**就挡住：上限只有一份（服务端），面板据此禁用按钮。"""
    response = client.post(PUBLISH_URL, json={"task_ids": [f"t{index}" for index in range(BATCH_MAX + 1)]})

    assert response.status_code == 422
    assert _error(response.json()) == ErrorCode.VALIDATION_FAILED.value


def test_unknown_field_is_422(client: TestClient) -> None:
    """``extra="forbid"``：多带一个字段是**说错了话**，不是"忽略它继续"。

    静默忽略的代价是前端以为某个开关生效了，而服务端从来没看过它。
    """
    response = client.post(PUBLISH_URL, json={"task_ids": ["t1"], "platfroms": ["douyin"]})

    assert response.status_code == 422
    assert _error(response.json()) == ErrorCode.VALIDATION_FAILED.value


def test_limit_out_of_range_is_422(client: TestClient) -> None:
    """``limit`` 在**入参**就卡范围：不卡的话 ``limit=100000`` 就是"把整个盘拖过来"。"""
    for limit in (0, -1, 501):
        response = client.get(LIBRARY_URL, params={"limit": limit})
        assert response.status_code == 422, limit
        assert _error(response.json()) == ErrorCode.VALIDATION_FAILED.value


def test_limit_caps_the_rows(client: TestClient, connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """``limit`` 真起作用，且 ``total`` 说的是**这次回了几行**（面板据此说"还有更多"）。"""
    for index in range(3):
        task_id = _completed_task(connection, title=f"第 {index} 条")
        _final(paths, task_id, stamp=f"20260922-12000{index}")

    body = client.get(LIBRARY_URL, params={"limit": 2}).json()

    assert body["total"] == 2
    assert body["limit"] == 2
    assert len(body["items"]) == 2


# ══════════════════════════════════════════════════════════════════════
# ⑧ 判据只有一份（服务层 vs REST 面）
# ══════════════════════════════════════════════════════════════════════


def test_service_and_api_agree(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """同一份盘，服务层与 REST 面给出的任务号集合必须一模一样。

    两处各算一次的代价是"面板上看到的"与"发出去的"可以是两条不同的片子，
    而两边都不会报错。
    """
    task_id = _completed_task(connection)
    _final(paths, task_id, stamp="20260922-120000")
    _final(paths, task_id, stamp="20260922-152331")

    service = read_library(connection=connection, paths=paths)
    api = client.get(LIBRARY_URL).json()

    assert [item.task_id for item in service] == [row["task_id"] for row in api["items"]]
    assert [item.video_name for item in service] == [row["video_name"] for row in api["items"]]
    assert service[0].versions == api["items"][0]["versions"]
