"""端到端：配音阶段编排与降级演练（T2.8 验收 · §04.3.4 / §04.3.3）。

这一条问四件事
--------------
① 配音阶段真的走完了：投递 ⇒ 池认领 ⇒ 收口（母带 + 时间轴）⇒ 迁移到 ``queued_render``；
② ``--until voicing`` 能**真的停在**那一刻（母带还没写）；
③ 单句失败 ⇒ 退避重试 ⇒ 这一次成了（不是"一失败就降级"）；
④ 引擎全挂 ⇒ 全句 ``skipped`` + 等长静音 + **任务照样推进**（字幕模式，不卡死）。

为什么必须跑真的池与真的 ffmpeg
-------------------------------
③④ 是这一环最容易写错的地方，而它们的错都**不报错**：
- 失败计数记在单元里 ⇒ 每次重试都从 0 开始，这一句会一路失败到降级（少了一句人声）；
- 降级写成了"抛出去" ⇒ 任务停在 ``voicing``，产线卡死 —— 而单看某一句的状态，
  它只是"失败了一次"，看不出整体已经不动了。
所以这里跑真的 ``PoolWorker``（真认领 / 真退避 / 真收尾 + 真 SQLite）与真的
``settle_voice``（真 ffmpeg 拼母带），只把**引擎**换成写得出固定时长 WAV 的假件 ——
时长必须可控，否则"母带 = Σ句 + Σ停顿"这条断言没法钉死。

故障注入走**环境变量**（``STUDIO_FAULT``）而不是直接调假件：那样验的才是"演练
那条命令真的有效"，而不是"我们能把函数换掉"。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import wave
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import load_outputs_config
from studio.core.faults import FAULT_ENV
from studio.core.media import probe_media
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import ScriptRepo
from studio.domain import TaskService
from studio.domain.enums import TaskStatus
from studio.pools.voice_worker import build_voice_handler as real_build_voice_handler
from studio.services.pipeline_service import TTS_DEGRADE_REASON, PipelineReport, run_task

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本机没装的音色不该让"接线写错了"跟着一起红 —— 引擎是假件，音色只是个字符串
VOICE = "验收音色"

#: 假引擎写出来的 WAV 参数（单声道 16bit 22.05kHz，与真 SAPI 的档位一致）
SAMPLE_RATE = 22_050

#: 每句念多久（毫秒）。**700 能被 22050 整除**：重采样到 48k 之后仍是整数个采样点，
#: 于是"母带时长 = Σ句时长 + Σ停顿"是精确等式（30ms 的容差留给 ffprobe 的取整）。
SENTENCE_MS = 700

#: 任务的随机化种子（``tasks.payload_json.seed``）—— 停顿抖动由它派生
SEED = 42

TEXTS = (
    "第一句台词，说的是跑酷地图。",
    "第二句台词，讲的是开局只有一格方块。",
    "第三句台词，提醒大家点个关注。",
)

#: 每句后面的基础停顿（毫秒）。第二句是 0：紧接下一句。
PAUSES = (200, 0, 350)

GLOSSARY_YAML = 'schema_version: "1.0"\nabbreviations: {}\nterms: {}\nunits: {}\n'

#: 失败重试的退避压到 1 毫秒（库是池参数的唯一真相，见 :func:`_seed`）。
#: 不压的话，一次 ``tts_down`` 演练要真等 3s + 6s 的退避 —— 那是生产该有的节奏，
#: 不该由一条用例来承担。
FAST_BACKOFF_MS = 1


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("本机没有 ffmpeg（母带靠它拼、时长靠 ffprobe 实测，跳过）")


class CountingEngine:
    """写得出**固定时长** WAV 的假引擎（与 T2.6 / T2.7 的假件同一形态）。

    它写的是**真的 WAV**，所以 ``probe_media`` 那一环、以及"母带时长 = Σ句 + Σ停顿"
    这件事都是真验的。``calls`` 数得出"哪几句真的进过引擎"—— ``tts_down`` 时它必须
    是空的（故障要在引擎缝上，不能是"合成完再判失败"）。
    """

    name = "counting"
    revision = "test"

    def __init__(self, *, duration_ms: int = SENTENCE_MS) -> None:
        self.calls: list[str] = []
        self.duration_ms = duration_ms

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append(text)
        frames = max(1, round(SAMPLE_RATE * self.duration_ms / 1000))
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * frames)


@dataclass(frozen=True, slots=True)
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一个停在 ``queued_voice`` 的任务。"""

    paths: StudioPaths
    task_id: str
    sentence_ids: tuple[str, ...]


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> CountingEngine:
    """把**引擎**换成假件；池、编排、收口一律跑真的。

    只换这一个外部依赖：``build_voice_handler`` 是"引擎从哪来"的唯一装配点，
    在它外面包一层等价于"这台机器上装的是这个引擎" —— 其余一切（认领、退避、
    重试、降级、拼母带、迁移）都是被测对象本身。
    """
    fake = CountingEngine()

    def build(**kwargs: Any) -> Any:
        kwargs.pop("engine", None)
        return real_build_voice_handler(**kwargs, engine=fake)

    # 用**字符串**目标：``pipeline_service`` 的 ``__all__`` 里没有这个名字（它是借来的），
    # 直接取属性会被 mypy 判成"没有显式导出"。
    monkeypatch.setattr("studio.services.pipeline_service.build_voice_handler", build)
    return fake


def _stage(tmp_path: Path) -> StudioPaths:
    """临时家目录：真 ``outputs.yaml`` + 真 ``pools.yaml`` + 一份空词表。

    不 ``load_config``：临时家目录里只有这两份配置，凑齐七份只为跑一条配音阶段不值当
    （与 ``tests/integration/test_timeline.py`` 同一条取舍）。``pools.yaml`` 必须在，
    因为排空那一步要按池的旋钮组装 worker。
    """
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("outputs.yaml", "pools.yaml"):
        shutil.copyfile(REPO_ROOT / "config" / name, paths.config_dir / name)
    paths.glossary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.glossary_file.write_text(GLOSSARY_YAML, encoding="utf-8")
    return paths


def _seed(paths: StudioPaths, texts: Sequence[str] = TEXTS) -> Rig:
    """迁移 + 建任务（带种子）+ 落稿（逐句停顿）+ 推到 ``queued_voice``。

    **不预先投递作业**：投递是 T2.8 被测的第一步（``queued_voice → voicing``）。
    """
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    try:
        tasks = TaskService(connection)
        task_id = tasks.create(title="配音阶段验收", payload={"seed": SEED}).id
        saved = ScriptRepo(connection).save_draft(
            task_id=task_id,
            title="标题",
            hook="钩子",
            body_md="正文",
            cta="关注",
            word_count=60,
            est_duration_ms=20_000,
            speaker_ratio={"bigbear": 1.0},
            outline={"hook_3s": "钩子", "segments": []},
            sentences=[
                {
                    "seq": seq,
                    "text_raw": text,
                    "text": text,
                    "speaker": "bigbear",
                    "emotion": "neutral",
                    "pause_after_ms": PAUSES[(seq - 1) % len(PAUSES)],
                }
                for seq, text in enumerate(texts, start=1)
            ],
        )
        for target in (TaskStatus.DRAFTING, TaskStatus.REVIEWING, TaskStatus.QUEUED_VOICE):
            tasks.transition(task_id, target, actor="test", reason="setup")
        connection.execute(
            "UPDATE pool_settings SET backoff_base_ms = ?, backoff_max_ms = ? WHERE pool = 'voice'",
            (FAST_BACKOFF_MS, FAST_BACKOFF_MS),
        )
        connection.commit()
    finally:
        connection.close()
    return Rig(paths=paths, task_id=task_id, sentence_ids=saved.sentence_ids)


@contextmanager
def _db(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    """一条**用完就关**的连接（``sqlite3.Connection`` 的 ``with`` 不关连接）。"""
    connection = connect(paths.db_file)
    try:
        yield connection
    finally:
        connection.close()


def _run(rig: Rig, until: TaskStatus) -> PipelineReport:
    """跑一次**真的** ``run_task``（真投递 / 真排空 / 真收口 / 真迁移）。"""
    with _db(rig.paths) as connection:
        return run_task(
            task_id=rig.task_id,
            paths=rig.paths,
            outputs=load_outputs_config(rig.paths.config_dir / "outputs.yaml"),
            outputs_source=rig.paths.config_dir / "outputs.yaml",
            connection=connection,
            until=until,
        )


def _sentences(rig: Rig) -> list[tuple[int, str, int, int, int]]:
    """库里的 ``(seq, tts_status, tts_attempts, start_ms, end_ms)``（按句序）。"""
    with _db(rig.paths) as connection:
        rows = connection.execute(
            "SELECT seq, tts_status, tts_attempts, start_ms, end_ms FROM script_sentences"
            " WHERE task_id = ? ORDER BY seq",
            (rig.task_id,),
        ).fetchall()
    return [
        (
            int(row["seq"]),
            str(row["tts_status"]),
            int(row["tts_attempts"]),
            int(row["start_ms"]),
            int(row["end_ms"]),
        )
        for row in rows
    ]


def _status(rig: Rig) -> TaskStatus:
    with _db(rig.paths) as connection:
        return TaskService(connection).get(rig.task_id).status


def _timeline(rig: Rig) -> dict[str, Any]:
    """读盘上的 ``timeline.json``（收口那一步的产物，也是下游字幕的计时来源）。"""
    payload: dict[str, Any] = json.loads(rig.paths.timeline_json(rig.task_id).read_text(encoding="utf-8"))
    return payload


def _job_attempts(rig: Rig) -> dict[str, int]:
    """每个 voice 作业用掉了几次尝试（``unit_ref`` ⇒ ``attempts``）。"""
    with _db(rig.paths) as connection:
        rows = connection.execute(
            "SELECT unit_ref, attempts FROM jobs WHERE pool = 'voice' AND task_id = ?",
            (rig.task_id,),
        ).fetchall()
    return {str(row["unit_ref"]): int(row["attempts"]) for row in rows}


def _spoken_and_pauses(payload: dict[str, Any]) -> tuple[int, int]:
    """时间轴里"句子占了多久"与"句间停顿一共多久"。

    **必须从时间轴里读**，不能拿用例里那组 ``PAUSES`` 当答案：每句的停顿是
    ``base + jitter(seed)``（±80ms，T2.7 裁定 218），拿 base 去对账会差出几十毫秒 ——
    而那个差恰好落在容差边缘上，看起来像"母带拼错了"。
    """
    rows = payload["sentences"]
    return (
        sum(int(row["duration_ms"]) for row in rows),
        sum(int(row["pause_after_ms"]) for row in rows),
    )


# ══════════════════════════════════════════════════════════════════════
# ① 主线：投递 ⇒ 排空 ⇒ 收口 ⇒ 交给渲染
# ══════════════════════════════════════════════════════════════════════


def test_the_voice_stage_hands_off_then_settles(tmp_path: Path, engine: CountingEngine) -> None:
    """★ 主线：``queued_voice`` 一路走到 ``queued_render``，母带与时间轴都在。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))

    report = _run(rig, TaskStatus.QUEUED_RENDER)

    assert report.status_before == TaskStatus.QUEUED_VOICE.value
    assert report.status == TaskStatus.QUEUED_RENDER.value
    assert [step.stage for step in report.steps] == ["voice", "timeline"]

    # 每一句都念了（**一次**引擎调用 = 一次缓存未命中）
    assert sorted(engine.calls) == sorted(TEXTS)

    master = rig.paths.voice_master(rig.task_id)
    assert master.is_file() and master.stat().st_size > 0
    timeline_path = rig.paths.timeline_json(rig.task_id)
    assert timeline_path.is_file()

    rows = _sentences(rig)
    assert [row[1] for row in rows] == ["done"] * len(TEXTS)
    assert all(row[2] == 0 for row in rows), "一句都没失败过"
    # 回写进了库：单调、不重叠（面板与字幕都读这几列）
    assert rows[0][3] == 0
    for previous, current in pairwise(rows):
        assert current[3] >= previous[4]

    # 母带时长 = Σ句实测 + Σ停顿（母带**不含** tail，见 T2.7 裁定 217）
    payload = _timeline(rig)
    spoken, pauses = _spoken_and_pauses(payload)
    assert spoken == SENTENCE_MS * len(TEXTS), "三句都是固定时长，实测值应当一致"
    assert pauses > 0, "停顿真的进了音频（不是只记在时间轴上）"
    master_ms = probe_media(master).duration_ms
    assert abs(master_ms - spoken - pauses) <= 30
    assert payload["total_ms"] == spoken + pauses + payload["tail_ms"]


# ══════════════════════════════════════════════════════════════════════
# ② --until voicing 真的停得住
# ══════════════════════════════════════════════════════════════════════


def test_until_voicing_stops_before_the_master_is_built(tmp_path: Path, engine: CountingEngine) -> None:
    """★ 投递完就停：任务在 ``voicing``，母带还没影，一句都还没念。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))

    report = _run(rig, TaskStatus.VOICING)

    assert report.status == TaskStatus.VOICING.value
    assert [step.stage for step in report.steps] == ["voice"]
    assert engine.calls == []
    assert not rig.paths.voice_master(rig.task_id).exists()
    assert _status(rig) is TaskStatus.VOICING


def test_a_second_run_picks_up_from_voicing(tmp_path: Path, engine: CountingEngine) -> None:
    """★ 断点续跑：停在 ``voicing`` 之后，下一次接手**接着**排空 + 收口。"""
    _require_ffmpeg()
    rig = _seed(_stage(tmp_path))

    first = _run(rig, TaskStatus.VOICING)
    assert first.status == TaskStatus.VOICING.value

    second = _run(rig, TaskStatus.QUEUED_RENDER)

    assert second.status == TaskStatus.QUEUED_RENDER.value
    assert [step.stage for step in second.steps] == ["timeline"]
    assert rig.paths.voice_master(rig.task_id).is_file()
    assert sorted(engine.calls) == sorted(TEXTS)


# ══════════════════════════════════════════════════════════════════════
# ③ 单句失败 ⇒ 重试 ⇒ 成功
# ══════════════════════════════════════════════════════════════════════


def test_a_flaky_sentence_is_retried_and_wins(
    tmp_path: Path, engine: CountingEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ ``STUDIO_FAULT=tts_fail_sentence=3``：第 3 句挂两次，第三次念出来了。

    判据有两面：它**最终是 ``done``**（不是降级），而另外两句**一次都没失败过**
    （故障按句注入，没有误伤邻居）。
    """
    _require_ffmpeg()
    monkeypatch.setenv(FAULT_ENV, "tts_fail_sentence=3;tts_fail_times=2")
    rig = _seed(_stage(tmp_path))

    report = _run(rig, TaskStatus.QUEUED_RENDER)

    assert report.status == TaskStatus.QUEUED_RENDER.value
    rows = _sentences(rig)
    assert [row[1] for row in rows] == ["done"] * len(TEXTS)
    assert [row[2] for row in rows] == [0, 0, 2], "只有第 3 句挂了两次"
    # 失败的那两次**没有进引擎**（故障在引擎缝上），第 3 次才真的念出来
    assert len(engine.calls) == len(TEXTS)
    assert engine.calls.count(TEXTS[2]) == 1
    # 作业自己也重试了两回（退避重排），第三次才成功
    attempts = _job_attempts(rig)
    assert sorted(attempts.values()) == [1, 1, 3]
    # 降级过的静音占位**不该**出现：这一句是念出来的
    assert "降级" not in report.steps[-1].note


# ══════════════════════════════════════════════════════════════════════
# ④ 引擎全挂 ⇒ 字幕模式，任务照样推进
# ══════════════════════════════════════════════════════════════════════


def test_a_dead_engine_still_reaches_queued_render(
    tmp_path: Path, engine: CountingEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ ``STUDIO_FAULT=tts_down=1``：全句 ``skipped`` + 等长静音 + **不卡死**。

    §04.3.3：连续失败到线 ⇒ 降级是**成功**的一种（"这一句的产物是静音"），
    任务继续往下走（字幕模式），``quality_json`` 打 ``tts_unavailable``。
    """
    _require_ffmpeg()
    monkeypatch.setenv(FAULT_ENV, "tts_down=1")
    rig = _seed(_stage(tmp_path))

    report = _run(rig, TaskStatus.QUEUED_RENDER)

    assert report.status == TaskStatus.QUEUED_RENDER.value, "TTS 故障不允许阻塞产线"
    assert engine.calls == [], "故障要在引擎缝上 —— 引擎一次都不该被调到"
    rows = _sentences(rig)
    assert [row[1] for row in rows] == ["skipped"] * len(TEXTS)
    assert [row[2] for row in rows] == [3, 3, 3], "每句挂满 3 次才降级"

    # 每个作业都重试满 3 次才降级
    assert sorted(_job_attempts(rig).values()) == [3, 3, 3]

    # 等长静音真的落了盘，而且进了母带（母带不是空的、时长仍然对得上）
    master = rig.paths.voice_master(rig.task_id)
    assert master.is_file() and master.stat().st_size > 0
    payload = _timeline(rig)
    spoken, pauses = _spoken_and_pauses(payload)
    assert spoken > 0, "静音占位也有**实测**时长（估一个的话字幕会整体偏）"
    assert pauses > 0
    assert abs(probe_media(master).duration_ms - spoken - pauses) <= 30

    # 降级写进了 quality_json（面板靠它提示"这条片子进了字幕模式"）
    with _db(rig.paths) as connection:
        quality = TaskService(connection).get(rig.task_id).quality
    assert quality.degraded is True
    assert quality.degrade_reason == TTS_DEGRADE_REASON
    assert "3 句降级" in report.steps[-1].note


def test_a_dead_engine_does_not_leave_half_written_state(
    tmp_path: Path, engine: CountingEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 降级之后时间轴照算：每句的 ``start_ms`` / ``end_ms`` 都在，且单调。

    静音占位也是**真的音频**（有实测时长），所以时间轴与字幕照常成立 —— 这是
    "字幕模式"能用的前提。少写这一条的话，降级路径只在"能出片"这一层被验过。
    """
    _require_ffmpeg()
    monkeypatch.setenv(FAULT_ENV, "tts_down=1")
    rig = _seed(_stage(tmp_path))

    _run(rig, TaskStatus.QUEUED_RENDER)

    rows = _sentences(rig)
    assert rows[0][3] == 0
    # 单调、不重叠；第二句的基础停顿是 0 ⇒ 允许"紧接下一句"（那是一条真实分支）
    for previous, current in pairwise(rows):
        assert current[3] >= previous[4]
    assert any(current[3] > previous[4] for previous, current in pairwise(rows)), "有句间停顿"
    assert rig.paths.timeline_json(rig.task_id).is_file()


# ══════════════════════════════════════════════════════════════════════
# ⑤ 同一份缓存上再演一场：故障必须**照样**打到引擎
# ══════════════════════════════════════════════════════════════════════


def test_a_second_drill_still_reaches_the_engine(
    tmp_path: Path, engine: CountingEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 换个 plan 在同一份缓存上再演一遍，故障不许被缓存吃掉。

    真机演练里踩到的就是这个：``+fault`` 是**常量**后缀 ⇒ 第一场演练把念成功的音频
    按 ``sapi+fault`` 收进了缓存，第二场演练算出**同一个键** ⇒ 四句全命中缓存 ⇒
    引擎一次都没被调到 —— 而日志里 ``voice.fault_injected tts_down=True`` 照打，
    四句全都"念"出来了，门禁全绿。

    所以这里**故意复用同一个家目录**：真机上的缓存就是跨任务、跨进程复用的那一份。
    """
    _require_ffmpeg()
    paths = _stage(tmp_path)

    monkeypatch.setenv(FAULT_ENV, "tts_fail_sentence=3;tts_fail_times=2")
    first = _seed(paths)
    _run(first, TaskStatus.QUEUED_RENDER)
    assert len(engine.calls) == len(TEXTS), "第一场演练真的念了"

    monkeypatch.setenv(FAULT_ENV, "tts_down=1")
    engine.calls.clear()
    second = _seed(paths)
    report = _run(second, TaskStatus.QUEUED_RENDER)

    # 两场演练**都**"绿"，区别只在句子状态上 —— 判据就得钉在这儿
    statuses = [row[1] for row in _sentences(second)]
    assert statuses == ["skipped"] * len(TEXTS), f"tts_down 被第一场的缓存吃掉了：{statuses}"
    assert engine.calls == [], "故障在引擎缝上 ⇒ 内层引擎一次都不该被调到"
    assert report.status == TaskStatus.QUEUED_RENDER.value
