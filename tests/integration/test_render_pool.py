"""端到端：一条 ``render/final`` 作业走完**真的渲染池 worker**（T3.7 收口）。

为什么这条用例不能被单元测试代替
--------------------------------
单元测试把 ``produce_video`` 换成假件 —— "结论有没有被投影"能测，但
"进度落库与 worker 的续租 / 收尾用的是不是同一条租约"测不到。这两件事一旦错位，
表现是"面板上的进度永远停在 0%"或"作业成功了但进度写着失败"，而两者都是**静默**的。

所以这里跑一个真的 :class:`~studio.pools.worker_base.PoolWorker`：真入队、真认领、
真跑 ffmpeg + SAPI、真收尾，并且**从另一条连接**在单元运行中读作业行 ——
面板就是这么读的（另一个进程、另一条连接）。

环境不满足（没 ffmpeg / 没 SAPI 音色）⇒ **skip**，不是 fail：CI 上缺音色不该把
"渲染池接线写错了"和"这台机器没装语音包"混成同一个红灯。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import PoolConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import probe_media, run_command
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.pools.render_worker import build_render_handler
from studio.pools.worker_base import PoolWorker
from studio.services.render_service import ProduceRequest, ProduceResult, produce_video
from studio.tts.sapi import sapi_available

pytestmark = pytest.mark.e2e

TASK_ID = "pool001"

#: 一段够短、够有标点的中文口播（会被切成好几句 —— 顺带验逐句进度）
SPEECH = "今天我们来看一张特别离谱的跑酷地图。开局只有一格方块，脚下就是虚空。"


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("本机没有 ffmpeg")
    return binary


def _require_voice() -> None:
    if not sapi_available():
        pytest.skip("本机没有 SAPI 音色（装一个中文语音包再跑）")


def _stage_home(tmp_path: Path) -> StudioPaths:
    """一个带配置的临时家目录（测试**不碰**真实 data/ 与 templates/）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    (paths.home / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(__file__).resolve().parents[2] / "config" / "outputs.yaml",
        paths.config_dir / "outputs.yaml",
    )
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "prompts",
        paths.home / "prompts",
        dirs_exist_ok=True,
    )
    clip = paths.mc_parkour_dir / "parkour_test.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    result = run_command(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=30:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip),
        ]
    )
    assert result.ok, result.tail()
    return paths


def _pool_config() -> PoolConfig:
    """render 池的旋钮（**只取这条用例需要的几个**）。

    不 ``load_config``：临时家目录里只有 ``outputs.yaml``，凑齐七份配置只为跑一条
    用例是把"配置系统"也拖进这条链路 —— 而它有自己的测试。
    """
    return PoolConfig(
        unit_type="final",
        concurrency=1,
        poll_ms=50,
        lease_sec=120,
        backoff_base_ms=1000,
        backoff_max_ms=5000,
        unit_timeout_sec=600,
        max_attempts=1,
        priority=300,
    )


def _seed(paths: StudioPaths) -> tuple[str, sqlite3.Connection]:
    """迁移 + 建任务 + 入队一条 ``render/final``；返回（job_id, 探针连接）。"""
    migrate(paths.db_file)
    setup = connect(paths.db_file)
    setup.execute("INSERT INTO tasks(id, title) VALUES (?, '渲染池验收')", (TASK_ID,))
    # ⚠️ 生效的 `max_attempts` 取自 **pool_settings**（`enqueue` 读它），
    # 不是 `PoolConfig` —— 后者只喂 worker 自己的循环。两者不一致时，
    # "作业重试几次"由库说了算（DDL 是唯一真相）。
    setup.execute("UPDATE pool_settings SET max_attempts = 1 WHERE pool = 'render'")
    job_id = JobStore(setup).enqueue(
        task_id=TASK_ID,
        pool="render",
        unit_type="final",
        unit_ref="final",
        payload={"text": SPEECH, "seed": 1},
    )
    assert job_id is not None
    setup.close()
    return job_id, connect(paths.db_file)


@pytest.mark.slow
def test_render_final_unit_runs_through_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 队列里的 ``render/final`` ⇒ 真的出一支能播的 MP4，且进度对**别的连接**可见。"""
    _require_voice()
    paths = _stage_home(tmp_path)
    job_id, probe = _seed(paths)

    # 进度探针：每次进度回调之后，从**另一条连接**读一次作业行。
    # 这不是"多测一遍 report_progress" —— 单元测试里那条连接就是处理器自己的；
    # 这里要证的是**面板（另一个进程、另一条连接）看得见**。
    seen: list[dict[str, Any]] = []

    def spy(request: ProduceRequest, **kwargs: Any) -> ProduceResult:
        inner = kwargs["on_progress"]

        def progress(stage: str, done: int, total: int, note: str) -> None:
            inner(stage, done, total, note)
            row = probe.execute("SELECT result_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            seen.append(json.loads(row["result_json"]))

        return produce_video(request, **{**kwargs, "on_progress": progress})

    # 打桩打在**处理器模块的全局**上：它调的是 `produce_video(...)`，
    # 也就是 `studio.pools.render_worker` 这个命名空间里的那个名字。
    monkeypatch.setattr("studio.pools.render_worker.produce_video", spy)

    worker = PoolWorker(
        pool="render",
        handler=build_render_handler(paths=paths),
        pool_config=_pool_config(),
        paths=paths,
        slot=1,
        version="test",
    )
    report = worker.run(max_units=1)

    assert report.units_done == 1
    assert report.units_failed == 0
    assert report.stop_reason == "max_units"

    # ① 进度：面板在单元**运行中**就能读到阶段与计数，而且读过不止一次
    assert len(seen) >= 3, seen
    assert {item["stage"] for item in seen} >= {"voice", "render"}
    assert all(item["task_id"] == TASK_ID for item in seen)
    assert any(item["done"] > 0 and item["total"] > 0 for item in seen)

    # ② 收尾：作业成功、结果落在 result_json 里（面板读的是同一份）
    job = JobStore(probe).get(job_id)
    assert job.status == "succeeded"
    assert job.attempts == 1
    assert job.finished_at is not None
    assert job.result["stage"] == "done"
    assert job.result["quality_backfilled"] is True
    assert job.result["composite_hash"]

    final = Path(str(job.result["final"]))
    assert final.is_file() and final.stat().st_size > 0
    info = probe_media(final)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (1080, 1920)

    # ③ QC：成片自己的读数落进 tasks.quality_json（发布门禁读的是同一个数）
    row = probe.execute("SELECT quality_json FROM tasks WHERE id = ?", (TASK_ID,)).fetchone()
    assert row is not None
    quality = json.loads(row["quality_json"])
    assert -16.5 <= quality["lufs"] <= -15.5, quality
    assert quality["true_peak"] <= -1.0, quality

    # ④ 留痕：池进程写的日志能按任务捞出来（面板的"日志尾巴"读的就是它）
    sources = {
        item["source"]
        for item in probe.execute("SELECT source FROM system_logs WHERE task_id = ?", (TASK_ID,)).fetchall()
    }
    assert "studio.pools.render" in sources

    probe.close()


@pytest.mark.slow
def test_a_failed_unit_lands_in_the_dead_letter_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """渲染真失败 ⇒ 作业进**死信**（``max_attempts=1``）并留下可读的错误现场。

    这条是"禁止静默失败"（DoD 5）在渲染池上的落点：片子没出来，队列里必须看得见，
    而不是 worker 日志里一行 traceback 之后就没了。
    """
    _require_voice()
    paths = _stage_home(tmp_path)
    job_id, probe = _seed(paths)

    def boom(request: ProduceRequest, **kwargs: Any) -> ProduceResult:
        inner = kwargs["on_progress"]
        inner("render", 0, 1, "合成中")
        raise StudioError(
            "注入的编码失败",
            code=ErrorCode.RENDER_FAILED,
            context={"task_id": request.task_id},
            remediation="换 720P 保底档，或看 data/logs/render.log",
        )

    monkeypatch.setattr("studio.pools.render_worker.produce_video", boom)

    worker = PoolWorker(
        pool="render",
        handler=build_render_handler(paths=paths),
        pool_config=_pool_config(),
        paths=paths,
        slot=1,
        version="test",
    )
    report = worker.run(max_units=1)
    assert report.units_failed == 1

    job = JobStore(probe).get(job_id)
    assert job.status == "dead"  # max_attempts=1 ⇒ 一次失败即死信
    assert job.error_code == "RENDER_FAILED"
    assert job.error_message == "注入的编码失败"
    # 现场留着：停在哪一阶段 + 下一步怎么办（remediation 不在 jobs 表里，只能进 result_json）
    assert job.result["stage"] == "render"
    assert "720P" in job.result["remediation"]

    alerts = probe.execute(
        "SELECT payload_json FROM system_logs WHERE task_id = ? AND payload_json LIKE '%JOB_DEAD%'",
        (TASK_ID,),
    ).fetchall()
    assert alerts, "死信必须告警（§03.4.3）"
    probe.close()
