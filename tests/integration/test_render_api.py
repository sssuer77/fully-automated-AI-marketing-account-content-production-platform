"""渲染面板 REST 面集成测试（T4.6 验收 · §04.2.8）。

这一条验的是"**图形界面上那颗「出片」按钮真的能出片**"，不是"函数返回值对"。
所以它把面板会走的四步全走一遍：看首屏 → 登记一条 → 轮询到完成 → 取成片。

四条测试纪律
------------
1. **临时家目录**：`config/outputs.yaml` 抄一份进 tmp。拿仓库根当 home，面板读到的
   就是生产那份配置 —— 改一次档位，这里的断言就跟着飘。
2. **假探针**：`build_state(metrics_probe=…)`（与四池 / 总览台同一手法），否则
   `MetricsPump` 会去碰 `nvidia-smi`。
3. **渲染器换掉，链路的形状不换**：真跑 ffmpeg + SAPI 要几十秒，而且失败原因是
   "这台机器装没装语音包"。这里 monkeypatch 的是 `produce_video` 这一个名字，
   **登记 / 排队 / 进度 / 取消 / 结果**这一整套照跑 —— 面板真正依赖的正是后者
   （真 ffmpeg 那条链路由 `tests/integration/test_render_pipeline.py` 负责）。
4. **取消是协作式的，所以用例也这么验**：先卡住工作线程，再按取消，再放行 ——
   断言它**在下一个检查点**变成 `canceled`，而不是"按了立刻停"。
"""

from __future__ import annotations

import os
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
from studio.db.repositories.asset_repo import BrollClipRepo
from studio.render.composite import CompositeResult
from studio.render.subtitle import SubtitlePlan
from studio.services.asset_service import DisabledAssets
from studio.services.metrics_service import ResourceSnapshot
from studio.services.render_service import ProduceRequest, ProduceResult
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

CONSOLE_URL = "/api/v1/render/console"
JOBS_URL = "/api/v1/render/jobs"

#: 一段够短的中文口播（面板上用户手打的就是这种）
SPEECH = "今天我们来看一张特别离谱的跑酷地图，开局只有一格方块。"

#: 轮询上限：任务在工作线程里跑，正常几十毫秒就完事；给 10 秒是留给被卡住的 CI
POLL_TIMEOUT_SEC = 10.0


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
    """临时家目录：真 `outputs.yaml` + 一份最小的前端产物。

    `web/dist/index.html` 是**故意**放进去的：它让"API 直接托管图形界面"这条
    有东西可验，同时顺带验"挂载在 `/` 没把 `/api` 吃掉"。
    """
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", root / "config" / "outputs.yaml")
    dist = root / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>制片台</title>", encoding="utf-8")
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


def _fake_mp4(paths: StudioPaths, task_id: str) -> Path:
    """盘上放一个**长得像 mp4** 的文件（`ftyp` 魔数 + 一点填充）。"""
    final = paths.videos_dir / f"20260915-120000_{task_id}_final.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
    return final


def _result(paths: StudioPaths, request: ProduceRequest) -> ProduceResult:
    final = _fake_mp4(paths, request.task_id)
    return ProduceResult(
        task_id=request.task_id,
        final=final,
        clip=paths.mc_parkour_dir / "parkour_test.mp4",
        bgm=None,
        voice=None,
        voice_duration_ms=1_000,
        duration_ms=1_600,
        size_bytes=final.stat().st_size,
        watermark_enabled=False,
        watermark_skipped_reason="水印文件不存在，跳过",
        profile_name="douyin_1080x1920_30fps_v1",
        manifest=paths.work_dir_for(request.task_id) / "manifest.json",
        timeline=paths.timeline_json(request.task_id),
        composite=CompositeResult(
            output=final,
            duration_ms=1_600,
            size_bytes=final.stat().st_size,
            watermark_applied=False,
            bgm_applied=False,
            subtitle_applied=False,
            bg_fill="broll",
            loudness=None,
            warnings=(),
            argv=("ffmpeg", "-i", "parkour_test.mp4", str(final)),
        ),
        subtitle=SubtitlePlan(
            enabled=False,
            ass_path=None,
            cues=(),
            font_dir=None,
            font_name="Microsoft YaHei",
            skipped_reason="配置里关掉了字幕（subtitle.enabled=False）",
        ),
        bg_fill="broll",
        composite_hash="0123456789abcdef0123456789abcdef",
        degraded=False,
        degrade_reason=None,
        attempts=(),
    )


def _install_producer(monkeypatch: pytest.MonkeyPatch, producer: Any) -> None:
    """换掉服务层真正调用的那个名字（登记 / 排队 / 进度 / 取消那一套照跑）。"""
    monkeypatch.setattr("studio.services.render_job_service.produce_video", producer)


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


def test_console_reports_profiles_voices_and_watermark(client: TestClient) -> None:
    """首屏一次拿全：画布档（带**真实**质量字段）/ 音色 / 水印处置 / 文案上限。"""
    body = client.get(CONSOLE_URL).json()

    assert body["engine"] == "sapi"
    assert body["default_profile"] == "douyin_1080x1920_30fps_v1"
    assert body["max_speech_chars"] == 5000

    profiles = {item["name"]: item for item in body["profiles"]}
    assert "fallback_720x1280_v1" in profiles
    # 质量字段**经过 `resolve_profile`**：libx264 是 crf、NVENC 是 cq。
    # 直接读配置的话这里会是空的，而面板上那个旋钮就会显示错的东西。
    assert profiles["douyin_1080x1920_30fps_v1"]["quality_field"] == "crf"
    assert profiles["fallback_720x1280_v1"]["quality_field"] == "cq"
    assert profiles["douyin_1080x1920_30fps_v1"]["is_default"] is True

    # 没有音色不算错：`engine_ready=False` + 一句怎么办
    assert isinstance(body["engine_ready"], bool)
    if not body["engine_ready"]:
        assert body["engine_hint"]

    # 水印是可选装饰：不贴就必须给出原因
    assert body["watermark_enabled"] is False
    assert body["watermark_hint"]

    assert body["running"] is None
    assert body["jobs"] == []
    assert body["videos"] == []


def test_ui_is_served_at_root_without_shadowing_the_api(client: TestClient) -> None:
    """★ 图形界面：`/` 直接给 `web/dist/index.html`，而 `/api` 与 `/ws` 一个都没被吃掉。"""
    root = client.get("/")
    assert root.status_code == 200
    assert "制片台" in root.text

    # 挂载在 `/` 之后仍然打得通（注册顺序：API 路由在前、兜底挂载在后）
    assert client.get("/api/v1/health").status_code == 200
    assert client.get(CONSOLE_URL).status_code == 200


def test_missing_web_dist_does_not_swallow_the_router(tmp_path: Path) -> None:
    """产物不在 ⇒ **根本不挂载**，路由表与从前一模一样。

    这条挡的是一个很隐蔽的退化：把 StaticFiles 挂在一个不存在的目录上，代价不是
    "首页 404"，而是**所有没匹配上路由的请求都掉进 StaticFiles**，而它在目录不存在时
    抛 `RuntimeError` ⇒ `POST /api/v1/metrics`（本该 405）、打错的路径（本该 404）
    全变成 500。前端还没构建，不该让后端的语义跟着变。
    """
    root = tmp_path / "bare"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", root / "config" / "outputs.yaml")
    paths = StudioPaths(home=root, data_dir=tmp_path / "bare_data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
    )
    try:
        with TestClient(create_app(state=built)) as bare_client:
            assert bare_client.get("/").status_code == 404
            assert bare_client.post("/api/v1/metrics", json={}).status_code == 405
            assert bare_client.get("/api/v1/definitely-not-a-route").status_code == 404
    finally:
        built.close()


# ══════════════════════════════════════════════════════════════════════
# 成片列表与播放
# ══════════════════════════════════════════════════════════════════════


def test_videos_are_listed_newest_first_and_served(client: TestClient, paths: StudioPaths) -> None:
    """成片列表按时间倒序；`<video>` 取的就是列表里那个 `url`。"""
    older = _fake_mp4(paths, "old")
    newer = _fake_mp4(paths, "new")
    # 显式设时间戳：两次创建可能落在同一个时间片里，靠"谁先写"来判先后会 flaky
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    videos = client.get(CONSOLE_URL).json()["videos"]
    assert [item["name"] for item in videos] == [newer.name, older.name]
    assert videos[0]["url"] == f"/api/v1/render/videos/{newer.name}"
    assert videos[0]["size_bytes"] == newer.stat().st_size

    served = client.get(videos[0]["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/mp4"
    assert int(served.headers["content-length"]) == newer.stat().st_size

    # `<video>` 拖进度条靠的就是范围请求
    ranged = client.get(videos[0]["url"], headers={"Range": "bytes=0-15"})
    assert ranged.status_code == 206
    assert ranged.headers["content-range"].startswith("bytes 0-15/")


def test_video_name_must_be_a_plain_mp4_name(client: TestClient, paths: StudioPaths) -> None:
    """文件名只允许"不含路径分隔符、以 .mp4 结尾"——`../` 在正则那一步就被拒了。"""
    secret = paths.videos_dir.parent / "secret.mp4"
    secret.write_bytes(b"\x00" * 16)

    for name in ("notes.txt", "no-extension", "a b.mp4.exe"):
        assert client.get(f"/api/v1/render/videos/{name}").status_code == 422, name

    # 反斜杠（Windows 的路径分隔符）也必须在门禁之内
    assert client.get("/api/v1/render/videos/a%5Cb.mp4").status_code == 422

    # 名字合法但盘上没有 ⇒ 404（不是 500：这不是"服务端崩了"）
    assert client.get("/api/v1/render/videos/20260101-000000_x_final.mp4").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 登记 → 轮询 → 取片
# ══════════════════════════════════════════════════════════════════════


def test_submit_poll_and_play(
    client: TestClient, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 主线：面板填一个任务号 + 一段文案 ⇒ 点「出片」⇒ 成片出现在列表里。"""
    seen: list[str] = []

    def producer(request: ProduceRequest, *, on_progress: Any, **kwargs: Any) -> ProduceResult:
        del kwargs
        seen.append(request.task_id)
        on_progress("voice", 1, 2, "配音 1/2")
        on_progress("render", 2, 2, "合成完成")
        return _result(paths, request)

    _install_producer(monkeypatch, producer)

    created = client.post(JOBS_URL, json={"task_id": "api001", "text": SPEECH})
    assert created.status_code == 200, created.text
    job = created.json()
    assert job["status"] == "queued"
    assert job["task_id"] == "api001"
    # 请求摘要里**不含全文**（面板上只显示前 60 字），但要能看出填了多少字
    assert job["request"]["text_chars"] == len(SPEECH)

    done = _wait_for(client, job["id"], {"succeeded", "failed", "canceled"})
    assert done["status"] == "succeeded", done
    assert done["percent"] == 100
    assert seen == ["api001"]

    # 进度回调真的被记下来了（面板上那个滚动框读的就是它）
    assert any("[voice]" in line for line in done["logs"])
    assert any("[done]" in line for line in done["logs"])

    final = done["result"]["final"]
    assert isinstance(final, str)
    name = Path(final).name
    assert client.get(f"/api/v1/render/videos/{name}").status_code == 200

    # 成片与任务都出现在首屏（用户下一步就是去那里点播放）
    console = client.get(CONSOLE_URL).json()
    assert [item["name"] for item in console["videos"]] == [name]
    assert console["running"] is None
    assert [item["id"] for item in console["jobs"]] == [job["id"]]


def test_panel_render_honours_the_disabled_asset_list(
    client: TestClient, paths: StudioPaths, state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 面板上点过「停用」的素材，出片不再挑到它（T4.8 验收：禁用 ⇒ 随机化不再选中）。

    这一条**必须走面板那条路**（``RenderJobService._produce``），而不是直接调
    ``produce_video``：停用名单要在工作线程里、用**那个线程自己的**连接查出来
    （sqlite 连接是线程亲和的），所以"查库那一步到底接上了没有"只有在这里才验得到。
    直接调 ``produce_video`` 的用例验不了这件事 —— 它把 ``disabled=`` 当参数收下了。
    """
    clip = paths.mc_parkour_dir / "parkour_001.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
    repo = BrollClipRepo(state.connections.get())
    repo.upsert(
        clip_id="parkour_001",
        path=clip,
        sha256="a" * 64,
        duration_ms=30_000,
        license="cc0",
    )
    assert repo.set_enabled("parkour_001", False) is True

    seen: list[dict[str, Any]] = []

    def producer(request: ProduceRequest, **kwargs: Any) -> ProduceResult:
        seen.append(kwargs)
        return _result(paths, request)

    _install_producer(monkeypatch, producer)

    created = client.post(JOBS_URL, json={"task_id": "api-disabled", "text": SPEECH})
    assert created.status_code == 200, created.text
    done = _wait_for(client, created.json()["id"], {"succeeded", "failed", "canceled"})
    assert done["status"] == "succeeded", done

    assert seen, "出片服务没被调到"
    disabled = seen[0]["disabled"]
    assert isinstance(disabled, DisabledAssets)
    assert disabled.clips == frozenset({"parkour_001.mp4"})


def test_empty_text_falls_back_to_the_stored_script(
    client: TestClient, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文案留空 ⇒ 按任务号去库里找那一版生效稿件；找不到就**当面说清楚**。

    这一条同时挡住一种很容易发生的退化：把"没给文案"静默当成空字符串，一路跑到渲染，
    最后报一个"ffmpeg 输入为空" —— 那个错和用户真正要做的事（先去出一版稿）毫无关系。
    """

    def producer(request: ProduceRequest, **kwargs: Any) -> ProduceResult:
        del kwargs  # 没有文案时**不该**走到渲染；真走到了这条用例会在下面断言里炸
        return _result(paths, request)

    _install_producer(monkeypatch, producer)

    created = client.post(JOBS_URL, json={"task_id": "api002", "text": ""})
    assert created.status_code == 200
    failed = _wait_for(client, created.json()["id"], {"succeeded", "failed", "canceled"})
    assert failed["status"] == "failed"
    assert failed["error_code"] == "SCRIPT_NOT_FOUND"
    assert failed["remediation"]


def test_cancel_is_cooperative(
    client: TestClient, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 取消：按下去**不会立刻停**，它在下一个进度检查点才生效 —— 面板上就是这么写的。"""
    started = threading.Event()
    release = threading.Event()

    def producer(request: ProduceRequest, *, on_progress: Any, **kwargs: Any) -> ProduceResult:
        del kwargs
        on_progress("voice", 0, 1, "开始配音")
        started.set()
        release.wait(timeout=POLL_TIMEOUT_SEC)
        # 检查点：用户按过取消 ⇒ 这里必须抛，服务把它翻成 `canceled`
        on_progress("voice", 1, 1, "配音完成")
        return _result(paths, request)

    _install_producer(monkeypatch, producer)

    job_id = client.post(JOBS_URL, json={"task_id": "api003", "text": SPEECH}).json()["id"]
    assert started.wait(timeout=POLL_TIMEOUT_SEC), "工作线程没有起来"

    # 按取消：立刻返回，但状态**还是 running**（这就是"协作式"的样子）
    cancelled = client.post(f"{JOBS_URL}/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "running"

    release.set()
    done = _wait_for(client, job_id, {"canceled", "succeeded", "failed"})
    assert done["status"] == "canceled", done
    assert "取消" in done["note"]


def test_unknown_job_id_is_404(client: TestClient) -> None:
    """登记表在内存里：查不到就是查不到，不能假装成功。"""
    assert client.get(f"{JOBS_URL}/r9999").status_code == 404
    assert client.post(f"{JOBS_URL}/r9999/cancel").status_code == 404
    # id 形状不对 ⇒ 422（在路由那一步就被挡下，不必进服务里再判一次）
    assert client.get(f"{JOBS_URL}/not-a-job").status_code == 422


def test_request_body_rejects_unknown_fields(client: TestClient) -> None:
    """`extra="forbid"`：把 `profile` 写成 `profle` 会被**当面拒掉**，而不是静默用默认档。"""
    response = client.post(JOBS_URL, json={"task_id": "api004", "profle": "x"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"

    # 空任务号也拒（它是这条片子和稿件 / 审计留痕的挂钩）
    assert client.post(JOBS_URL, json={"task_id": ""}).status_code == 422
    # 线程数越界同理
    assert client.post(JOBS_URL, json={"task_id": "api005", "threads": 0}).status_code == 422
