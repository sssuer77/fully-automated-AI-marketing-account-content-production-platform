"""一键出片 REST 面集成测试（T4.14 延伸 · §04.6.8）。

这一条验的是"**图形界面上那颗「开始出片」按钮真的能从稿子走到成片**"，不是
"函数返回值对"。所以它把面板会走的五步全走一遍：看首屏 → 看这条任务会怎样 →
登记一条 → 轮询到完成 → 回到首屏看见它。

四条测试纪律（与 `test_render_api.py` 同一条）
----------------------------------------------
1. **临时家目录**：`config/outputs.yaml` 与 `config/app.yaml` 各抄一份进 tmp。
   拿仓库根当 home，面板读到的就是生产那份配置 —— 改一次档位，这里的断言就跟着飘。
2. **假探针**：`build_state(metrics_probe=…)`，否则 `MetricsPump` 会去碰 `nvidia-smi`。
3. **执行体换掉，链路的形状不换**：真跑要 SAPI + ffmpeg（几十秒），而且失败原因是
   "这台机器装没装语音包"。这里 monkeypatch 的是 `pipeline_job_service.run_task`
   这一个名字，**登记 / 排队 / 进度 / 取消 / 结果**这一整套照跑 —— 面板真正依赖的
   正是后者。真正的编排纪律由 `tests/unit/services/test_pipeline_service.py` 负责，
   真链路（黑屏用例）由 `tests/integration/test_render_pipeline.py` 负责。
4. **取消是协作式的，所以用例也这么验**：先卡住工作线程，再按取消，再放行 ——
   断言它**在下一个检查点**变成 `canceled`，而不是"按了立刻停"。

预览那几条（`GET /pipeline/tasks/{id}`）用的是**真任务 + 真状态迁移**：这个端点的
全部价值就是"面板说的和流水线真正会做的是同一件事"，用假状态去验它等于没验。
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.db.repositories import ApprovalRepo, ScriptRepo
from studio.domain.enums import TaskStatus
from studio.domain.models import QualityReport
from studio.domain.task_service import TaskService
from studio.services.metrics_service import ResourceSnapshot
from studio.services.pipeline_job_service import MAX_LOG_LINES, supported_until
from studio.services.pipeline_service import PipelineReport, StepReport
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

CONSOLE_URL = "/api/v1/pipeline/console"
JOBS_URL = "/api/v1/pipeline/jobs"

#: 轮询上限：任务在工作线程里跑，正常几十毫秒就完事；给 10 秒是留给被卡住的 CI
POLL_TIMEOUT_SEC = 10.0

SENTENCES = ("第一句。", "第二句。")


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）。"""

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at=now_iso(),
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


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """临时家目录：真 `outputs.yaml` + 真 `app.yaml`（`streaming_render` 那条读它）。"""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    for name in ("outputs.yaml", "app.yaml"):
        shutil.copyfile(REPO_ROOT / "config" / name, root / "config" / name)
    return root


@pytest.fixture
def paths(home: Path, tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=home, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    paths.ensure_runtime_dirs()
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
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════

#: 建任务要走的状态链（走**真实迁移**，不直接 UPDATE —— 那是另一条纪律）
_WALK: dict[TaskStatus, tuple[TaskStatus, ...]] = {
    TaskStatus.PENDING: (),
    TaskStatus.REVIEWING: (TaskStatus.DRAFTING, TaskStatus.REVIEWING),
    TaskStatus.AWAITING_APPROVAL: (
        TaskStatus.DRAFTING,
        TaskStatus.REVIEWING,
        TaskStatus.AWAITING_APPROVAL,
    ),
    TaskStatus.FAILED: (TaskStatus.DRAFTING, TaskStatus.FAILED),
    TaskStatus.COMPLETED: (
        TaskStatus.DRAFTING,
        TaskStatus.REVIEWING,
        TaskStatus.QUEUED_VOICE,
        TaskStatus.VOICING,
        TaskStatus.QUEUED_RENDER,
        TaskStatus.RENDERING,
        TaskStatus.COMPLETED,
    ),
}


def _task(
    connection: Any,
    *,
    status: TaskStatus = TaskStatus.REVIEWING,
    with_script: bool = True,
) -> str:
    """建一个任务并推到 ``status``。"""
    tasks = TaskService(connection)
    task = tasks.create(title="一键出片用例", actor="test")
    if with_script:
        ScriptRepo(connection).save_draft(
            task_id=task.id,
            title="测试稿",
            hook="开场",
            body_md="正文",
            cta="结尾",
            word_count=10,
            est_duration_ms=3000,
            speaker_ratio={"bigbear": 1.0},
            outline={},
            sentences=[
                {"seq": index, "text_raw": text, "text": text, "speaker": "bigbear"}
                for index, text in enumerate(SENTENCES, start=1)
            ],
        )
    for target in _WALK[status]:
        tasks.transition(task.id, target, actor="test", reason="setup")
    if status is TaskStatus.AWAITING_APPROVAL:
        # 走真实仓储开一条待审（不用裸 SQL 拼列名：DDL 改了这里会静默错位）
        ApprovalRepo(connection).request(
            task_id=task.id, script_id=None, grade="B", score_total=6.5, revision_round=0
        )
    return task.id


def _report(paths: StudioPaths, task_id: str, until: str) -> PipelineReport:
    """一份假的流水线结论：成片真的落到 ``videos_dir``（面板要能播它）。"""
    final = paths.videos_dir / f"20260917-120000_{task_id}_final.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
    return PipelineReport(
        task_id=task_id,
        status_before="queued_render",
        status="completed",
        until=until,
        steps=(
            StepReport(stage="voice", status_before="queued_voice", status="voicing", note="假排空"),
            StepReport(
                stage="render",
                status_before="rendering",
                status="completed",
                note=final.as_posix(),
            ),
        ),
        final=final,
        quality=QualityReport(),
    )


def _install(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    """换掉服务层真正调用的那个名字（登记 / 排队 / 进度 / 取消那一套照跑）。"""
    monkeypatch.setattr("studio.services.pipeline_job_service.run_task", runner)


def _runner_ok(paths: StudioPaths, seen: list[str]) -> Any:
    """跑得完的假执行体（中途报两次进度，面板上那条进度条读的就是它）。"""

    def run(**kwargs: Any) -> PipelineReport:
        task_id = kwargs["task_id"]
        seen.append(task_id)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress("voice", 1, 2, "第一句念完了")
            on_progress("voice", 2, 2, "第二句念完了")
            on_progress("render", 0, 1, "开始渲染")
        return _report(paths, task_id, kwargs["until"].value)

    return run


def _runner_gated(paths: StudioPaths, started: threading.Event, release: threading.Event) -> Any:
    """卡得住的假执行体：``release`` 之后才碰第二个进度点（那个点就是取消检查点）。"""

    def run(**kwargs: Any) -> PipelineReport:
        on_progress = kwargs["on_progress"]
        on_progress("voice", 1, 2, "第一句念完了")
        started.set()
        release.wait(timeout=POLL_TIMEOUT_SEC)
        on_progress("voice", 2, 2, "第二句念完了")
        return _report(paths, kwargs["task_id"], kwargs["until"].value)

    return run


def _wait_for(client: TestClient, job_id: str, statuses: set[str]) -> dict[str, Any]:
    """轮询到某个状态为止（面板做的就是这件事，只是它按秒轮）。"""
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{JOBS_URL}/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    raise AssertionError(f"{job_id} 在 {POLL_TIMEOUT_SEC}s 内没有走到 {statuses}，最后是 {body}")


# ══════════════════════════════════════════════════════════════════════
# 首屏
# ══════════════════════════════════════════════════════════════════════


def test_console_lists_every_supported_until_with_a_label(client: TestClient) -> None:
    """★ 契约：下拉框里的落点**一个不多一个不少**，而且每个都有名字。

    少一个的后果不是"少一个选项"这么轻 —— 那个落点会从界面上**消失**，而没有任何
    地方会报错。所以这里直接拿 `supported_until()`（唯一真相）去比对。
    """
    body = client.get(CONSOLE_URL).json()

    assert [item["value"] for item in body["until_options"]] == list(supported_until())
    for item in body["until_options"]:
        assert item["label"], item
        assert item["description"], item

    assert body["default_until"] == "completed"
    assert body["max_log_lines"] == MAX_LOG_LINES
    assert body["active"] is None
    assert body["jobs"] == []

    # 没有音色不算错：`engine_ready=False` + 一句怎么办
    assert isinstance(body["engine_ready"], bool)
    if body["engine_ready"]:
        assert body["default_voice"]
    else:
        assert body["engine_hint"]


# ══════════════════════════════════════════════════════════════════════
# 预览：这条任务点了会怎样
# ══════════════════════════════════════════════════════════════════════


def test_preview_says_what_will_happen(client: TestClient, state: AppState) -> None:
    """在**审稿中**的任务：默认落点能跑，落点就是它自己时如实说"不会重跑"。"""
    task_id = _task(state.connections.get(), status=TaskStatus.REVIEWING)

    body = client.get(f"/api/v1/pipeline/tasks/{task_id}").json()
    assert body["status"] == "reviewing"
    assert body["title"] == "一键出片用例"
    assert body["runnable"] is True
    assert body["reason"] is None
    assert "completed" in body["note"]
    assert body["has_script"] is True
    assert body["active_job"] is None

    # 换个落点，同一行字跟着变（面板换下拉框时重调的就是它）
    same = client.get(f"/api/v1/pipeline/tasks/{task_id}", params={"until": "reviewing"}).json()
    assert same["runnable"] is True
    assert "幂等" in same["note"]


def test_preview_refuses_the_human_gate_and_stuck_states(client: TestClient, state: AppState) -> None:
    """★ 三条硬规矩里有两条在这里：**不代按确认闸**、**不接 stuck 状态**。

    这两条要是靠"点了之后返回 409"来体现，用户看到的就是"填了任务号、按了、红了"，
    而不知道该去哪儿。预览把话说在前面。
    """
    connection = state.connections.get()

    gated = _task(connection, status=TaskStatus.AWAITING_APPROVAL)
    body = client.get(f"/api/v1/pipeline/tasks/{gated}").json()
    assert body["runnable"] is False
    assert "确认闸" in (body["reason"] or "")
    assert body["note"]

    stuck = _task(connection, status=TaskStatus.FAILED)
    body = client.get(f"/api/v1/pipeline/tasks/{stuck}").json()
    assert body["runnable"] is False
    assert "failed" in (body["reason"] or "")


def test_preview_flags_a_missing_script(client: TestClient, state: AppState) -> None:
    """没有生效稿件 ⇒ 提前说（流水线不写稿，`pending` 的任务一步都走不了）。"""
    task_id = _task(state.connections.get(), status=TaskStatus.PENDING, with_script=False)

    body = client.get(f"/api/v1/pipeline/tasks/{task_id}").json()
    assert body["has_script"] is False
    # 状态本身是能跑的（`pending` 不在 stuck 里），走不动是因为没稿子 —— 两件事分开说
    assert body["runnable"] is True


def test_preview_rejects_an_unknown_until(client: TestClient, state: AppState) -> None:
    """落点写错 ⇒ **422**（入参问题），并把能用的那些给出来。

    用 409（`STATE_TRANSITION_ILLEGAL`）的话，人会以为是任务状态不对，去查任务号。
    """
    task_id = _task(state.connections.get(), status=TaskStatus.REVIEWING)

    response = client.get(f"/api/v1/pipeline/tasks/{task_id}", params={"until": "rendering"})
    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["context"]["supported"] == list(supported_until())


def test_unknown_task_is_404(client: TestClient) -> None:
    """任务号不存在和"这个任务现在不能跑"是两句不同的话。"""
    assert client.get("/api/v1/pipeline/tasks/nosuchtask").status_code == 404
    # id 形状不对 ⇒ 422（在路由那一步就被挡下，不必进服务里再判一次）
    assert client.get("/api/v1/pipeline/tasks/bad id").status_code == 422


# ══════════════════════════════════════════════════════════════════════
# 跑一条：从登记到成片
# ══════════════════════════════════════════════════════════════════════


def test_run_all_the_way_to_a_final_mp4(
    client: TestClient, state: AppState, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 主路径：填任务号 → 开始出片 → 轮询到成功 → 成片能播。

    这条就是"图形界面上那颗按钮真的能出片"。
    """
    task_id = _task(state.connections.get(), status=TaskStatus.REVIEWING)
    seen: list[str] = []
    _install(monkeypatch, _runner_ok(paths, seen))

    created = client.post(JOBS_URL, json={"task_id": task_id})
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["deduped"] is False
    job = payload["job"]
    assert job["status"] == "queued"
    assert job["task_id"] == task_id
    assert job["until"] == "completed"

    done = _wait_for(client, job["id"], {"succeeded", "failed", "canceled"})
    assert done["status"] == "succeeded", done
    assert done["percent"] == 100
    assert seen == [task_id]

    # 进度回调真的被记下来了（面板上那个滚动框读的就是它）
    assert any("[voice]" in line for line in done["logs"])
    assert any("[done]" in line for line in done["logs"])
    # 走过的每一步也留着（"这条命令到底做了什么"）
    assert [step["stage"] for step in done["steps"]] == ["voice", "render"]
    assert done["quality"] == {"degraded": False} or isinstance(done["quality"], dict)

    final = Path(done["final"])
    assert final.is_file()

    # 回到首屏：在跑的那条清空、最近几条里有它（用户下一步就是去那儿看结果）
    console = client.get(CONSOLE_URL).json()
    assert console["active"] is None
    assert [item["id"] for item in console["jobs"]] == [job["id"]]


def test_submit_is_idempotent_for_the_same_task(
    client: TestClient, state: AppState, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 连点两次"开始出片"：**不会**开出第二条。

    两条链路同时改一个任务的状态，轻则互相踩状态迁移，重则两条都渲染一遍 ——
    而后者花的是真金白银的编码时间。
    """
    task_id = _task(state.connections.get(), status=TaskStatus.REVIEWING)
    started = threading.Event()
    release = threading.Event()
    _install(monkeypatch, _runner_gated(paths, started, release))

    first = client.post(JOBS_URL, json={"task_id": task_id}).json()
    assert started.wait(timeout=POLL_TIMEOUT_SEC), "工作线程没有起来"

    second = client.post(JOBS_URL, json={"task_id": task_id}).json()
    assert second["deduped"] is True
    assert second["job"]["id"] == first["job"]["id"]

    # 面板据此把按钮换成"看进度"：预览里就带着那条在跑的 job
    preview = client.get(f"/api/v1/pipeline/tasks/{task_id}").json()
    assert preview["active_job"]["id"] == first["job"]["id"]

    release.set()
    assert _wait_for(client, first["job"]["id"], {"succeeded"})["status"] == "succeeded"

    # 跑完之后再提交 ⇒ 是一条**新的**（幂等只挡"同时在跑的两条"）
    third = client.post(JOBS_URL, json={"task_id": task_id}).json()
    assert third["deduped"] is False
    assert third["job"]["id"] != first["job"]["id"]


def test_cancel_is_cooperative(
    client: TestClient, state: AppState, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 取消：按下去**不会立刻停**，它在下一个进度检查点才生效 —— 面板上就是这么写的。"""
    task_id = _task(state.connections.get(), status=TaskStatus.REVIEWING)
    started = threading.Event()
    release = threading.Event()
    _install(monkeypatch, _runner_gated(paths, started, release))

    job_id = client.post(JOBS_URL, json={"task_id": task_id}).json()["job"]["id"]
    assert started.wait(timeout=POLL_TIMEOUT_SEC), "工作线程没有起来"

    # 跑着的时候百分比是真的（1/2 ⇒ 50），不是一直挂 0
    running = client.get(f"{JOBS_URL}/{job_id}").json()
    assert running["status"] == "running"
    assert running["percent"] == 50
    assert running["stage"] == "voice"

    # 按取消：立刻返回，但状态**还是 running**（这就是"协作式"的样子）
    cancelled = client.post(f"{JOBS_URL}/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "running"

    release.set()
    done = _wait_for(client, job_id, {"canceled", "succeeded", "failed"})
    assert done["status"] == "canceled", done
    assert "取消" in done["note"]


def test_request_body_rejects_unknown_fields(client: TestClient) -> None:
    """`extra="forbid"`：把 `until` 写成 `untill` 会被**当面拒掉**，而不是静默跑默认落点。"""
    response = client.post(JOBS_URL, json={"task_id": "api001", "untill": "voicing"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"

    # 空任务号也拒（它是这条片子和稿件 / 审计留痕的挂钩）
    assert client.post(JOBS_URL, json={"task_id": ""}).status_code == 422
    # 落点不在白名单里 ⇒ 422，且把能用的那些给出来
    bad = client.post(JOBS_URL, json={"task_id": "api001", "until": "rendering"})
    assert bad.status_code == 422
    assert bad.json()["context"]["supported"] == list(supported_until())


def test_unknown_job_id_is_404(client: TestClient) -> None:
    """登记表在内存里：查不到就是查不到，不能假装成功。"""
    assert client.get(f"{JOBS_URL}/p9999").status_code == 404
    assert client.post(f"{JOBS_URL}/p9999/cancel").status_code == 404
    # id 形状不对 ⇒ 422（前缀 `p` 与渲染面板的 `r` 分开，混了就是 422 而不是查错表）
    assert client.get(f"{JOBS_URL}/r0001").status_code == 422
