"""配音操作面 REST 集成测试（T2.9 验收 · §04.3 / T4.5）。

这一条验的是"**面板上那三颗按钮真的管用**"
-------------------------------------------
看逐句状态 → 重配一句 → 试听 → 换音色。四条测试纪律：

1. **临时家目录**：`outputs.yaml` / `pools.yaml` 各抄一份进 tmp。拿仓库根当 home，
   面板读到的就是生产那份配置 —— 改一次档位，这里的断言就跟着飘。
2. **假音色清单**：`usable_voices` 会去起一个 PowerShell 列系统音色（1–2 秒，且
   结果跟着"这台机器装没装语音包"变）。用例把它换成固定的两个名字：验的是
   "换音色这条链路的形状"，不是"这台机器有哪些嗓子"。
3. **真库真盘**：句子状态、作业状态、留痕一律回库查；音频文件真写到
   `data/output/voice/<task_id>/s00N.wav`。"REST 返回 200"只说明没抛。
4. **时间轴那条走真 ffmpeg**（`test_resynth_moves_the_timeline`）：T2.9 的验收口径是
   "总时长变化**可见**"，那就必须真拼一次母带、真量一次时长。

为什么"重配"要连**作业表**一起验
--------------------------------
只改业务表也能让 REST 返回 200 —— 而队列的幂等键 `(task_id, pool, unit_type,
unit_ref)` 让一条单元一辈子只有一条作业，`succeeded` 之后没有任何 worker 会再看
这一句。症状是"点了重配没反应、不报错、任务卡在 `voicing`"（陷阱 #115）。
所以这里断言的是**那条作业回到了 `pending`、并且真的能被认领**。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import wave
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.config import load_outputs_config, load_pools_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths, preview_slug
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.db.repositories import AuditRepo, ScriptRepo, SentenceRepo
from studio.domain.enums import TaskStatus
from studio.domain.task_service import TaskService
from studio.gc.policy import protected_roots
from studio.pools.voice_worker import build_voice_handler
from studio.pools.worker_base import PoolWorker
from studio.services.metrics_service import ResourceSnapshot
from studio.services.voice_preview import PREVIEW_TEXT, VoicePreviewService
from studio.services.voice_service import settle_voice
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

SENTENCES_URL = "/api/v1/sentences"
VOICES_URL = "/api/v1/voices"

#: 本机没装的音色不该让"接线写错了"跟着一起红 —— 引擎是假件，音色只是个字符串
VOICE_DA = "验收音色·熊大"
VOICE_XIAO = "验收音色·熊二"

#: 假音色清单（`usable_voices` 的输入）
INSTALLED = (VOICE_DA, VOICE_XIAO)

#: 三句台词：前两句熊大、第三句熊二 —— "换音色只影响说话的那几句"才验得出来
SCRIPT: tuple[tuple[str, str], ...] = (
    ("bigbear", "第一句台词，说的是跑酷地图。"),
    ("bigbear", "第二句台词，讲的是开局只有一格方块。"),
    ("littlebear", "第三句台词，提醒大家点个关注。"),
)

#: 每句后面停多久（毫秒）
PAUSES = (200, 0, 350)

#: 假引擎写出来的 WAV 参数（单声道 16bit 22.05kHz，与真 SAPI 的档位一致）
SAMPLE_RATE = 22_050

#: 种子句的时长（毫秒）。700 能被 22050 整除 ⇒ 重采样到 48k 后仍是整数个采样点，
#: 于是"重配后总时长 = 原时长 + 这一句的差"是精确等式（容差留给 ffprobe 取整）。
SENTENCE_MS = 700

#: 重配之后那一句念多久 —— 与 :data:`SENTENCE_MS` 差 700ms，"时长变化可见"靠它
RESYNTH_MS = 1400

SEED = 42

GLOSSARY_YAML = 'schema_version: "1.0"\nabbreviations: {}\nterms: {}\nunits: {}\n'


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("本机没有 ffmpeg（母带靠它拼、时长靠 ffprobe 实测，跳过）")


class CountingEngine:
    """写得出**固定时长** WAV 的假引擎（与 T2.6 / T2.8 的假件同一形态）。

    ``calls`` 数得出"哪几句真的进过引擎" —— "只重念被点的那一句"这件事必须有账可查
    （其余句子走 ``done`` 短路：产物在盘上就直接收工）。
    """

    name = "counting"
    revision = "test"

    def __init__(self, *, duration_ms: int = SENTENCE_MS) -> None:
        self.calls: list[str] = []
        self.duration_ms = duration_ms

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        del voice, rate
        self.calls.append(text)
        frames = max(1, round(SAMPLE_RATE * self.duration_ms / 1000))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * frames)


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）—— 不碰真 `nvidia-smi` 与真磁盘。"""

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


@dataclass(frozen=True, slots=True)
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 一条配音已定局的任务。"""

    paths: StudioPaths
    task_id: str
    sentence_ids: tuple[str, ...]


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真 ``outputs.yaml``（母带参数）+ 真 ``pools.yaml``（排空要按池组装）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("outputs.yaml", "pools.yaml"):
        shutil.copyfile(REPO_ROOT / "config" / name, value.config_dir / name)
    value.glossary_file.parent.mkdir(parents=True, exist_ok=True)
    value.glossary_file.write_text(GLOSSARY_YAML, encoding="utf-8")
    return value


@pytest.fixture(autouse=True)
def installed_voices(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """把"本机有哪些音色"钉死（真列一次要起 PowerShell，且结果跟着系统变）。"""
    monkeypatch.setattr("studio.services.voice_service.list_voices_cached", lambda: INSTALLED)
    return INSTALLED


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


def _wav(path: Path, *, duration_ms: int = SENTENCE_MS) -> Path:
    """盘上真放一个 WAV（试听与时间轴读的都是**盘上那一份**）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(SAMPLE_RATE * duration_ms / 1000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * frames)
    return path


#: `_seed` 默认给任务写的音色映射（两个都"装了" —— 见模块头第 2 条纪律）
DEFAULT_VOICE_MAP: dict[str, str] = {"bigbear": VOICE_DA, "littlebear": VOICE_XIAO}


def _seed(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    *,
    duration_ms: int = SENTENCE_MS,
    script: Sequence[tuple[str, str]] = SCRIPT,
    voice_map: Mapping[str, str] | None = DEFAULT_VOICE_MAP,
) -> Rig:
    """建任务（音色映射指到装了的两个音色）+ 落稿 + 把三句都推到 ``done`` + 作业收尾。

    **作业要真的走到 ``succeeded``**：T2.9 的关键就在"已经结束的作业怎么重排"，
    作业还停在 ``pending`` 的话，这条路径根本不会被碰到（陷阱 #115）。

    默认指到"装了的两个音色"；``voice_map=None`` ⇒ 用 ``TaskPayload`` 的**默认值**
    （逻辑角色名占位，真机上新建的任务就是这一种）。
    """
    tasks = TaskService(connection)
    payload: dict[str, Any] = {"seed": SEED}
    if voice_map is not None:
        payload["voice_map"] = dict(voice_map)
    task_id = tasks.create(title="配音操作面验收", payload=payload).id
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook="钩子",
        body_md="正文",
        cta="关注",
        word_count=60,
        est_duration_ms=20_000,
        speaker_ratio={"bigbear": 0.67, "littlebear": 0.33},
        outline={"hook_3s": "钩子", "segments": []},
        sentences=[
            {
                "seq": seq,
                "text_raw": text,
                "text": text,
                "speaker": speaker,
                "emotion": "neutral",
                "pause_after_ms": PAUSES[(seq - 1) % len(PAUSES)],
            }
            for seq, (speaker, text) in enumerate(script, start=1)
        ],
    )
    for target in (
        TaskStatus.DRAFTING,
        TaskStatus.REVIEWING,
        TaskStatus.QUEUED_VOICE,
        TaskStatus.VOICING,
    ):
        tasks.transition(task_id, target, actor="test", reason="setup")

    repo = SentenceRepo(connection)
    store = JobStore(connection)
    for sentence_id in saved.sentence_ids:
        row = repo.get(sentence_id)
        assert row is not None
        audio = _wav(paths.sentence_wav(task_id, row.seq), duration_ms=duration_ms)
        repo.finish(
            sentence_id,
            expected_version=row.version,
            audio_path=str(audio),
            duration_ms=duration_ms,
            sample_rate=SAMPLE_RATE,
            tts_hash=f"hash-{row.seq:03d}",
            engine="counting",
            voice_id=VOICE_DA if row.speaker == "bigbear" else VOICE_XIAO,
        )
        job_id = store.enqueue(
            task_id=task_id,
            pool="voice",
            unit_type="sentence",
            unit_ref=sentence_id,
            payload={"voice": VOICE_DA if row.speaker == "bigbear" else VOICE_XIAO},
        )
        assert job_id is not None
        claimed = store.claim(pool="voice", worker_id="seed")
        assert claimed is not None
        assert store.succeed(job_id=claimed.id, worker_id="seed", result={"ok": True})
    connection.commit()
    return Rig(paths=paths, task_id=task_id, sentence_ids=saved.sentence_ids)


def _sentences(connection: sqlite3.Connection, task_id: str) -> list[tuple[int, str, str, int]]:
    """库里的 ``(seq, speaker, tts_status, tts_attempts)``（按句序）。"""
    rows = connection.execute(
        "SELECT seq, speaker, tts_status, tts_attempts FROM script_sentences WHERE task_id = ? ORDER BY seq",
        (task_id,),
    ).fetchall()
    return [(int(r["seq"]), str(r["speaker"]), str(r["tts_status"]), int(r["tts_attempts"])) for r in rows]


def _job(connection: sqlite3.Connection, sentence_id: str) -> tuple[str, int, dict[str, Any]]:
    """这一句的作业 ``(status, attempts, payload)``。"""
    row = connection.execute(
        "SELECT status, attempts, payload_json FROM jobs WHERE unit_ref = ?", (sentence_id,)
    ).fetchone()
    assert row is not None, f"句子 {sentence_id} 没有作业"
    return str(row["status"]), int(row["attempts"]), json.loads(row["payload_json"] or "{}")


def _audit(connection: sqlite3.Connection, task_id: str, action: str) -> list[Any]:
    return [row for row in AuditRepo(connection).list_for_task(task_id) if row.action == action]


@contextmanager
def _worker(paths: StudioPaths, engine: CountingEngine) -> Iterator[PoolWorker]:
    """一个**真的** voice 池 worker（连接用完就关，Windows 上临时目录才删得掉）。"""
    connection = connect(paths.db_file)
    try:
        handler = build_voice_handler(paths=paths, connection=connection, engine=engine, voice=VOICE_DA)
        yield PoolWorker(
            pool="voice",
            handler=handler,
            pool_config=load_pools_config(paths).pools["voice"],
            paths=paths,
            slot=1,
            version="test",
        )
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════
# ① 逐句状态
# ══════════════════════════════════════════════════════════════════════


def test_the_panel_sees_every_sentence_with_a_playable_url(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 主线：面板一打开就有逐句状态 + 每句能播的 url。"""
    rig = _seed(connection, paths)

    response = client.get(SENTENCES_URL, params={"task_id": rig.task_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == rig.task_id
    assert body["progress"]["total"] == 3
    assert body["progress"]["done"] == 3
    assert body["progress"]["ratio"] == 1.0
    assert body["voice_map"] == {"bigbear": VOICE_DA, "littlebear": VOICE_XIAO}
    # 盘上还没有时间轴 ⇒ 不能说"它过期了"（过期是"有一份旧的"，不是"没有"）
    assert body["timeline_total_ms"] is None
    assert body["timeline_stale"] is False

    seqs = [row["seq"] for row in body["sentences"]]
    assert seqs == [1, 2, 3]
    for row in body["sentences"]:
        assert row["audio_url"] == f"/api/v1/media/voice/{rig.task_id}/s{row['seq']:03d}.wav"
        assert row["audio_source"] == "sentence"
        assert row["can_resynth"] is True
        assert row["tts_voice_id"] in (VOICE_DA, VOICE_XIAO)


def test_the_panel_sees_which_voices_it_can_pick(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """音色下拉框：本机这两个（假清单），并顺带带上这条任务现在的映射。"""
    rig = _seed(connection, paths)

    body = client.get(VOICES_URL, params={"task_id": rig.task_id}).json()

    # 顺序是**排过**的（下拉框两次打开必须一样），所以这里比集合而不是比原顺序
    assert [option["id"] for option in body["voices"]] == sorted(INSTALLED)
    assert {option["source"] for option in body["voices"]} == {"sapi"}
    assert body["voice_map"] == {"bigbear": VOICE_DA, "littlebear": VOICE_XIAO}
    assert body["note"] is None


# ══════════════════════════════════════════════════════════════════════
# ② 试听
# ══════════════════════════════════════════════════════════════════════


def test_media_serves_the_wav_with_range_support(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 单句 wav 可播放：整份能取，``Range`` 也能取（``<audio>`` 拖动进度条靠它）。"""
    rig = _seed(connection, paths)
    url = f"/api/v1/media/voice/{rig.task_id}/s002.wav"
    on_disk = paths.sentence_wav(rig.task_id, 2).read_bytes()

    whole = client.get(url)

    assert whole.status_code == 200, whole.text
    assert whole.content == on_disk
    assert whole.headers["accept-ranges"] == "bytes"

    partial = client.get(url, headers={"Range": "bytes=0-3"})

    assert partial.status_code == 206, partial.text
    assert partial.content == on_disk[:4]
    assert partial.headers["content-range"] == f"bytes 0-3/{len(on_disk)}"


def test_media_refuses_anything_that_is_not_a_sentence_wav(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 路径安全：只认 ``voice/<task_id>/s00N.wav`` 这一种形状。

    ``../`` 在**正则**那一步就被拒（422），而不是靠 ``resolve()`` 之后的比较兜底 ——
    后者的失败方式取决于平台与大小写，而前者一个字节都不进服务层。
    """
    rig = _seed(connection, paths)
    bad = [
        f"voice/{rig.task_id}/../../config/app.yaml",
        f"voice/{rig.task_id}/s002.txt",
        f"voice/{rig.task_id}/s2.wav",
        "voice/../app.yaml",
        "C:/Windows/win.ini",
        "config/app.yaml",
    ]
    for key in bad:
        response = client.get(f"/api/v1/media/{key}")
        assert response.status_code == 422, f"{key} ⇒ {response.status_code}"
        assert response.json()["code"] == "VALIDATION_FAILED"


def test_voice_paths_accept_the_task_ids_the_render_panel_hands_out(client: TestClient) -> None:
    """★ 面板默认任务号 ``ui-YYYYMMDD-HHMMSS`` 带连字符，路径契约必须认它。

    任务号是**调用方起的名**（``RenderJobRequest.task_id`` 只限长度、不限字符集），
    渲染面板默认给的就是带连字符的那一种。路径 pattern 按 ``[0-9A-Za-z]`` 卡的话，
    面板自己创建的任务号一进配音面板就整屏 422，而 422 报的是"路径不合法" ——
    排查的人会去查任务号存不存在，不会想到是那条正则（陷阱 #122）。

    这里只验**形状**：形状过了就是 404（"没有这条任务 / 这一句"），形状没过才是 422。
    """
    ghost = "ui-20260915-120000"
    assert client.get(f"/api/v1/media/voice/{ghost}/s001.wav").status_code == 404
    patched = client.patch(f"/api/v1/tasks/{ghost}/voice_map", json={"voice_map": {"bigbear": "x"}})
    assert patched.status_code == 404, patched.text

    # 放开的是**任务号**那一段，媒资键的整串形状一个字都没松：``../`` 仍然在正则那一步被拒。
    assert client.get(f"/api/v1/media/voice/{ghost}/../s001.wav").status_code == 422
    assert client.get(f"/api/v1/media/voice/{ghost}/s1.wav").status_code == 422


def test_media_reports_the_missing_audio_instead_of_synthesizing(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 试听**不触发合成**（§05 T2.9）：没有音频就 404，且盘上仍然什么都没有。

    点一下试听就排一次合成，等于把"看一眼"变成"重跑一遍" —— 而重跑还会覆盖交付产物。
    所以这里断言的是"服务端没有偷偷补一份"：状态没变、文件没多出来。
    """
    rig = _seed(connection, paths)
    target = paths.sentence_wav(rig.task_id, 2)
    target.unlink()
    before = sorted(p.name for p in paths.voice_dir_for(rig.task_id).iterdir())

    response = client.get(f"/api/v1/media/voice/{rig.task_id}/s002.wav")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "PATH_MISSING"
    assert not target.exists()
    assert sorted(p.name for p in paths.voice_dir_for(rig.task_id).iterdir()) == before
    assert _sentences(connection, rig.task_id)[1][2] == "done"  # 库里的结论也没被改


def test_media_reports_a_sentence_that_does_not_exist(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    rig = _seed(connection, paths)

    response = client.get(f"/api/v1/media/voice/{rig.task_id}/s099.wav")

    assert response.status_code == 404
    assert response.json()["code"] == "SCRIPT_NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════
# ③ 单句重配
# ══════════════════════════════════════════════════════════════════════


def test_resynth_puts_the_sentence_back_in_the_queue(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 重配 = 业务表退回待办 **+** 那条已经结束的作业排回池 + 留痕。

    少做第二件事的后果：REST 照样 200、库里也显示 ``pending``，但没有任何 worker 会
    再看这一句一眼（幂等键 ⇒ 一条单元一辈子一条作业）⇒ 任务卡在 ``voicing``，
    而且**不报错**（陷阱 #115）。
    """
    rig = _seed(connection, paths)
    sentence_id = rig.sentence_ids[1]

    response = client.post(f"/api/v1/sentences/{sentence_id}/resynth")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["seq"] == 2
    assert body["status"] == "pending"
    assert body["job_created"] is False  # 复用的是原来那条作业
    assert body["timeline_stale"] is True
    assert body["progress"]["pending"] == 1
    assert body["progress"]["done"] == 2

    assert _sentences(connection, rig.task_id) == [
        (1, "bigbear", "done", 0),
        (2, "bigbear", "pending", 0),
        (3, "littlebear", "done", 0),
    ]
    status, attempts, payload = _job(connection, sentence_id)
    assert (status, attempts) == ("pending", 0)
    assert payload == {"voice": VOICE_DA}

    # 关键：这条作业**真的能被认领**（"排回池"不是"改了个状态字段"）
    claimed = JobStore(connection).claim(pool="voice", worker_id="probe")
    assert claimed is not None
    assert claimed.unit_ref == sentence_id

    audits = _audit(connection, rig.task_id, "sentence.resynth")
    assert len(audits) == 1
    assert audits[0].target_id == sentence_id
    assert audits[0].actor == "user"
    assert audits[0].source == "webui"
    assert audits[0].before["tts_status"] == "done"
    assert audits[0].after["tts_status"] == "pending"
    assert audits[0].after["job_id"] == claimed.id


def test_resynth_zeroes_the_attempts_so_the_next_failure_does_not_degrade(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 降级句重配之后要有**完整**的 3 次机会（陷阱 #116）。

    不归零的话，上一轮已经撞过 ``tts_attempts >= 3`` 的句子重配时**再失败一次**
    就直接降级 —— 用户点"重配"的本意是"再给它一次机会"，结果只给了一次。
    这个错**看不见**：不归零也能跑通，只是失败得比预期早得多。
    """
    rig = _seed(connection, paths)
    sentence_id = rig.sentence_ids[0]
    row = SentenceRepo(connection).get(sentence_id)
    assert row is not None
    connection.execute(
        "UPDATE script_sentences SET tts_status = 'skipped', tts_attempts = 3 WHERE id = ?",
        (sentence_id,),
    )
    connection.commit()

    response = client.post(f"/api/v1/sentences/{sentence_id}/resynth")

    assert response.status_code == 200, response.text
    assert _sentences(connection, rig.task_id)[0] == (1, "bigbear", "pending", 0)
    _, attempts, _ = _job(connection, sentence_id)
    assert attempts == 0


def test_resynth_refuses_a_sentence_that_is_being_spoken(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ ``synthesizing`` 不能重配：抢在 worker 眼皮底下改状态会让两个 worker 写同一个文件。

    用例走**公开路径**把它置成 ``synthesizing``（``reopen`` ⇒ ``begin``），不写裸 SQL ——
    那样验的才是"真实运行时会遇到的那个状态"。
    """
    rig = _seed(connection, paths)
    sentence_id = rig.sentence_ids[0]
    repo = SentenceRepo(connection)
    row = repo.get(sentence_id)
    assert row is not None
    assert repo.reopen(sentence_id, expected_version=row.version)
    assert repo.begin(sentence_id, engine="counting", voice_id=VOICE_DA)

    response = client.post(f"/api/v1/sentences/{sentence_id}/resynth")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "STATE_TRANSITION_ILLEGAL"
    assert _sentences(connection, rig.task_id)[0][2] == "synthesizing"


def test_resynth_moves_the_timeline(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★★ T2.9 的验收口径：重配之后**总时长变化可见**（真 ffmpeg / 真 ffprobe）。

    走完整条链路：收口一次（拿到基准时长）⇒ 点重配 ⇒ 真 voice 池重念那一句
    ⇒ 再收口一次。两个断言各自钉死一件事：

    - ``calls`` 只有那一句 ⇒ 其余两句**没有**被重念（``done`` + 产物在盘上就收工）；
    - ``total_ms`` 差出整整一句 ⇒ 时间轴真的全量重算了（陷阱 #26：只有"整条重算"
      这一种更新方式，"跳过改过的那一行"会留下自相矛盾的 start_ms）。
    """
    _require_ffmpeg()
    rig = _seed(connection, paths)
    outputs = load_outputs_config(paths.config_dir / "outputs.yaml")
    before = settle_voice(paths=paths, connection=connection, task_id=rig.task_id, outputs=outputs)
    assert (
        before.timeline.total_ms
        == sum(sentence.duration_ms + sentence.pause_after_ms for sentence in before.timeline.sentences)
        + before.timeline.tail_ms
    )

    response = client.post(f"/api/v1/sentences/{rig.sentence_ids[1]}/resynth")
    assert response.status_code == 200, response.text

    engine = CountingEngine(duration_ms=RESYNTH_MS)
    with _worker(paths, engine) as worker:
        report = worker.run(max_units=1, max_empty_rounds=1)
    assert report.stop_reason == "max_units"

    after = settle_voice(paths=paths, connection=connection, task_id=rig.task_id, outputs=outputs)

    assert engine.calls == [SCRIPT[1][1]]  # 只有被点的那一句进过引擎
    assert after.timeline.total_ms == before.timeline.total_ms + (RESYNTH_MS - SENTENCE_MS)
    assert after.voice_master_ms == before.voice_master_ms + (RESYNTH_MS - SENTENCE_MS)
    # 落盘的那一份也要跟着变（面板读的是它，不是内存里那份）
    written = json.loads(paths.timeline_json(rig.task_id).read_text(encoding="utf-8"))
    assert written["total_ms"] == after.timeline.total_ms


# ══════════════════════════════════════════════════════════════════════
# ④ 任务级换音色
# ══════════════════════════════════════════════════════════════════════


def test_voice_map_asks_for_confirmation_before_spending_the_work(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 换音色 = 重配 N 句 ⇒ 先算代价、抛 409 让面板弹确认框，**一个字节都不写**。"""
    rig = _seed(connection, paths)

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map", json={"voice_map": {"bigbear": VOICE_XIAO}}
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "VOICE_MAP_CONFIRM_REQUIRED"
    assert body["context"]["affected"] == 2
    assert body["context"]["total"] == 3
    assert body["context"]["sentences"] == [1, 2]

    # 没确认 ⇒ 什么都没动：映射还是旧的、句子还是 done、作业还是 succeeded
    assert TaskService(connection).get(rig.task_id).payload.voice_map == {
        "bigbear": VOICE_DA,
        "littlebear": VOICE_XIAO,
    }
    assert [row[2] for row in _sentences(connection, rig.task_id)] == ["done"] * 3
    assert _job(connection, rig.sentence_ids[0])[0] == "succeeded"
    assert _audit(connection, rig.task_id, "task.voice_map") == []


def test_voice_map_invalidates_only_the_speaker_that_changed(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 确认之后：受影响的两句全部失效 + 作业带着**新音色**重排；熊二那句不动。

    三处必须一起改（``payload_json.voice_map`` / ``tts_status`` / ``jobs.payload_json``）。
    只改前两处的话，重排出去的作业还是旧音色，而库里显示的是新音色 ——
    "换音色成功、成片还是旧嗓子"，且不报错（陷阱 #117）。
    """
    rig = _seed(connection, paths)

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"bigbear": VOICE_XIAO}, "confirm": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["affected"] == 2
    assert body["requeued"] == 2
    assert body["created_jobs"] == 0
    assert body["busy"] == []
    assert body["timeline_stale"] is True
    assert body["progress"]["pending"] == 2
    assert body["progress"]["done"] == 1
    assert body["voice_map"] == {"bigbear": VOICE_XIAO, "littlebear": VOICE_XIAO}
    assert body["changes"] == [
        {
            "speaker": "bigbear",
            "before": VOICE_DA,
            "after": VOICE_XIAO,
            "affected": 2,
            "sentences": [1, 2],
        }
    ]

    assert _sentences(connection, rig.task_id) == [
        (1, "bigbear", "pending", 0),
        (2, "bigbear", "pending", 0),
        (3, "littlebear", "done", 0),  # 熊二没换音色 ⇒ 它的音频照样算数
    ]
    for sentence_id in rig.sentence_ids[:2]:
        status, attempts, payload = _job(connection, sentence_id)
        assert (status, attempts) == ("pending", 0)
        assert payload == {"voice": VOICE_XIAO}
    assert _job(connection, rig.sentence_ids[2])[0] == "succeeded"

    assert TaskService(connection).get(rig.task_id).payload.voice_map == {
        "bigbear": VOICE_XIAO,
        "littlebear": VOICE_XIAO,
    }
    audits = _audit(connection, rig.task_id, "task.voice_map")
    assert len(audits) == 1
    assert audits[0].target_type == "task"
    assert audits[0].before["voice_map"] == {"bigbear": VOICE_DA, "littlebear": VOICE_XIAO}
    assert audits[0].after["affected"] == 2
    assert audits[0].after["voice_map"]["bigbear"] == VOICE_XIAO


def test_voice_map_refuses_a_voice_this_machine_does_not_have(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 音色不存在 ⇒ 422 + ``context.available``，**并且什么都没写**。

    这一条必须在**写入之前**报：写进去之后配音会连失败 3 次 ⇒ 每一句都降级成静音
    ⇒ 用户看到"换音色成功"，拿到的却是一支**没人声**的成片。
    """
    rig = _seed(connection, paths)

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"bigbear": "并不存在的音色"}, "confirm": True},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "TTS_VOICE_MISSING"
    assert body["context"]["missing"] == {"bigbear": "并不存在的音色"}
    assert sorted(body["context"]["available"]) == sorted(INSTALLED)

    assert TaskService(connection).get(rig.task_id).payload.voice_map["bigbear"] == VOICE_DA
    assert [row[2] for row in _sentences(connection, rig.task_id)] == ["done"] * 3
    assert _audit(connection, rig.task_id, "task.voice_map") == []


def test_voice_map_leaves_the_untouched_placeholder_alone(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 只改熊大 ⇒ 熊二那条**占位**映射不该把这次提交拦下来（§04.3.7 的两条线）。

    ``TaskPayload.voice_map`` 的默认值是 ``{"bigbear": "bigbear", "littlebear":
    "littlebear"}`` —— 逻辑角色名，不是真音色。校验**整张表**的话，"新建任务第一次换
    音色"必然 422：用户改的是熊大，被拒的理由却是他根本没碰过的熊二（真机演练里撞上
    的就是这一条）。存量值由 ``resolve_voice`` 兜（退回进程音色并标 ``fallback``）。
    """
    rig = _seed(connection, paths, voice_map=None)
    assert TaskService(connection).get(rig.task_id).payload.voice_map == {
        "bigbear": "bigbear",
        "littlebear": "littlebear",
    }

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"bigbear": VOICE_DA}, "confirm": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["affected"] == 2  # 熊大那两句（第 3 句是熊二）
    assert body["requeued"] == 2
    assert body["voice_map"] == {"bigbear": VOICE_DA, "littlebear": "littlebear"}
    assert [row["speaker"] for row in body["changes"]] == ["bigbear"]

    # 提交里**显式**写了一个本机没有的音色 ⇒ 照旧 422（那条线没松）
    rejected = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"littlebear": "并不存在的音色"}, "confirm": True},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "TTS_VOICE_MISSING"


def test_voice_map_refuses_a_speaker_that_is_not_in_the_script(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 角色名写错（``bigBear``）是一个**静默无操作**：请求会成功、映射多一个没人用的键、
    真正要换的那个角色纹丝不动。所以它在写入之前就被拒。
    """
    rig = _seed(connection, paths)

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"bigBear": VOICE_XIAO}, "confirm": True},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["context"]["unknown"] == ["bigBear"]
    assert body["context"]["known"] == ["bigbear", "littlebear"]
    assert TaskService(connection).get(rig.task_id).payload.voice_map == {
        "bigbear": VOICE_DA,
        "littlebear": VOICE_XIAO,
    }


def test_voice_map_reports_the_sentences_it_could_not_touch(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 正在被念的那一句**不动**，但要**报出来**（``busy``）。

    静默跳过就是"换音色成功、成片里那一句还是旧嗓子" —— 面板拿 ``busy`` 才能提示
    "第 1 句等它念完再单独重配"。
    """
    rig = _seed(connection, paths)
    repo = SentenceRepo(connection)
    busy_id = rig.sentence_ids[0]
    row = repo.get(busy_id)
    assert row is not None
    assert repo.reopen(busy_id, expected_version=row.version)
    assert repo.begin(busy_id, engine="counting", voice_id=VOICE_DA)

    response = client.patch(
        f"/api/v1/tasks/{rig.task_id}/voice_map",
        json={"voice_map": {"bigbear": VOICE_XIAO}, "confirm": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["busy"] == [busy_id]
    assert body["affected"] == 1
    assert _sentences(connection, rig.task_id) == [
        (1, "bigbear", "synthesizing", 0),
        (2, "bigbear", "pending", 0),
        (3, "littlebear", "done", 0),
    ]
    # 映射**已经**换了：下一轮收口与之后的重配都按新音色走
    assert body["voice_map"]["bigbear"] == VOICE_XIAO


# ══════════════════════════════════════════════════════════════════════
# 音色试听样本（T2.4）
# ══════════════════════════════════════════════════════════════════════

#: 试听样本要花多久才"生成好"（假引擎是即时的，这个数是**轮询等待的上限**）
PREVIEW_WAIT_SEC = 10.0


class PreviewEngine:
    """试听样本用的假引擎：写一段固定时长的静音，并数得出被念过几次。

    数得出来是这条链路的**主要不变量**：``GET`` 只读盘、绝不合成（T2.9 的硬要求在
    试听这件事上的翻版）。没有这个计数，"点一下刷新面板就念了一句"是验不出来的。
    """

    name = "preview-fake"
    revision = "test"

    def __init__(self, *, duration_ms: int = 900, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.duration_ms = duration_ms
        self.fail = fail

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        del rate
        self.calls.append((text, voice))
        if self.fail is not None:
            raise StudioError(self.fail, code=ErrorCode.TTS_ENGINE_DOWN)
        frames = max(1, round(SAMPLE_RATE * self.duration_ms / 1000))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x00" * frames)


@pytest.fixture
def preview_engine(state: AppState) -> PreviewEngine:
    """把试听服务换成"假引擎"那一个。

    不换的话它会去问常驻推理服务 / 起 PowerShell 列系统音色 —— 用例于是变成
    "这台机器上装了什么"，而不是"这条链路对不对"。
    """
    engine = PreviewEngine()
    state.voice_previews = VoicePreviewService(state.paths, engine_factory=lambda: (engine, VOICE_DA))
    return engine


def _preview_url(voice_id: str) -> str:
    return f"{VOICES_URL}/{voice_id}/preview"


def _wait_for_preview(client: TestClient, voice_id: str, *, want: str) -> dict[str, Any]:
    """轮询到 ``want`` 那一态（真机上一次十几秒，这里假引擎是毫秒级）。"""
    deadline = time.monotonic() + PREVIEW_WAIT_SEC
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get(_preview_url(voice_id)).json()
        if payload["status"] == want:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"等了 {PREVIEW_WAIT_SEC}s 还没到 {want}：{payload}")


def test_the_dropdown_carries_the_preview_state(client: TestClient, preview_engine: PreviewEngine) -> None:
    """下拉框那一行要**一起**带上试听状态。

    分两次请求的话，第一帧上每一行都会显示成"还没生成"（哪怕样本早在盘上），
    而那颗按钮会在这半秒里看起来是坏的。
    """
    voices = client.get(VOICES_URL).json()["voices"]
    # 顺序是 ``usable_voices`` 那份 ``sorted``（不是 INSTALLED 的书写顺序）
    assert [row["id"] for row in voices] == sorted(INSTALLED)
    assert {row["preview_state"] for row in voices} == {"missing"}
    assert all(row["preview_url"] is None for row in voices)


def test_get_never_synthesizes(client: TestClient, preview_engine: PreviewEngine) -> None:
    """★ 查询**不触发合成**：面板每次刷新都会调它，顺带合成就是"每刷新一次念一句"。"""
    payload = client.get(_preview_url(VOICE_DA)).json()
    assert payload["status"] == "missing"
    assert payload["url"] is None
    assert preview_engine.calls == [], "查一下状态就把引擎叫起来了"


def test_generate_then_play(client: TestClient, preview_engine: PreviewEngine, state: AppState) -> None:
    """★ 生成 → 落盘 → 能播：面板上那颗按钮走的就是这条。"""
    started = client.post(_preview_url(VOICE_DA))
    assert started.status_code == 200
    # 立刻返回（真念在后台线程里）——真机上一次十几秒，同步做完就是一个挂住的请求
    assert started.json()["status"] in {"running", "ready"}

    ready = _wait_for_preview(client, VOICE_DA, want="ready")
    assert ready["duration_ms"] == 900
    assert ready["engine"] == "preview-fake"
    assert ready["url"] == f"/api/v1/media/voice_preview/{preview_slug(VOICE_DA)}.wav"
    # 引擎**真的**被叫了一次，而且带的是"试听那一段固定文本 + 这个音色"
    assert preview_engine.calls == [(PREVIEW_TEXT, VOICE_DA)]

    # 盘上真有一份，而且能通过媒资端点取回来（`<audio src>` 走的就是它）
    sample = state.paths.voice_preview_wav(VOICE_DA)
    assert sample.is_file() and sample.stat().st_size > 0
    media = client.get(ready["url"])
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("audio/")
    assert media.content == sample.read_bytes()

    # 下拉框那一行也跟着变（按钮下一次重绘才会显示「试听」）
    rows = {row["id"]: row for row in client.get(VOICES_URL).json()["voices"]}
    assert rows[VOICE_DA]["preview_state"] == "ready"
    assert rows[VOICE_DA]["preview_url"] == ready["url"]


def test_a_second_click_does_not_synthesize_again(client: TestClient, preview_engine: PreviewEngine) -> None:
    """已经有了就**不重念**：重复点一下不该再花十几秒。"""
    client.post(_preview_url(VOICE_DA))
    _wait_for_preview(client, VOICE_DA, want="ready")
    assert len(preview_engine.calls) == 1

    again = client.post(_preview_url(VOICE_DA)).json()
    assert again["status"] == "ready"
    assert len(preview_engine.calls) == 1, "第二次点击又念了一遍"


def test_a_failure_is_reported_not_hidden(client: TestClient, state: AppState) -> None:
    """引擎失败 ⇒ 如实说（原话带出来），而不是永远转圈。

    "永远转圈"是最坏的一种失败：面板上看不出哪里不对，用户只会反复点。
    """
    engine = PreviewEngine(fail="TTS_ENGINE_DOWN: 常驻服务连不上")
    state.voice_previews = VoicePreviewService(state.paths, engine_factory=lambda: (engine, None))

    client.post(_preview_url(VOICE_DA))
    failed = _wait_for_preview(client, VOICE_DA, want="failed")
    assert "TTS_ENGINE_DOWN" in (failed["error"] or "")
    # 半成品**不许**留在盘上（陷阱 #9：半截 wav 被当成样本 = 试听放出来是一声爆音）
    assert not state.paths.voice_preview_wav(VOICE_DA).exists()
    assert list(state.paths.voice_preview_dir.glob("*.partial*")) == []


def test_an_unknown_voice_is_refused_before_anything_is_written(
    client: TestClient, preview_engine: PreviewEngine, state: AppState
) -> None:
    """本机没有的音色 ⇒ 422，**且一个字节都没写**。

    拼错一个字母与"音色没入库"看起来一模一样，所以错误里要把候选列出来。
    """
    response = client.post(_preview_url("ghost-voice"))
    assert response.status_code == 422
    assert response.json()["code"] == "TTS_VOICE_MISSING"
    assert preview_engine.calls == []
    assert not state.paths.voice_preview_dir.exists()


def test_a_voice_the_current_engine_cannot_speak_is_refused(
    client: TestClient, preview_engine: PreviewEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 「本机有」但「这台引擎念不出来」⇒ 另一种错误码。

    这一条与配音**故意不一样**：配音会退回兜底音色（裁定 314），试听**必须**如实失败
    —— 试听的全部意义就是"听听这个嗓子"，换成兜底音色念出来的是**另一个人的声音**，
    而面板上什么都不会说（陷阱 #154 的形状）。
    """
    monkeypatch.setattr("studio.app.routers.voice.speakable_voices", lambda *_a, **_k: (VOICE_XIAO,))

    response = client.post(_preview_url(VOICE_DA))
    assert response.status_code == 503
    assert response.json()["code"] == "TTS_ENGINE_UNAVAILABLE"
    assert preview_engine.calls == []


def test_the_media_endpoint_refuses_anything_but_a_sample_name(client: TestClient) -> None:
    """媒资键**必须**是"一个样本文件名"：``..`` 与子目录在路由那一步就进不来。

    越界的形状有两种，两种都**不是** 404
    ----------------------------------
    ``..%2F..%2Fstudio.db``（编码过的斜杠）在路径参数校验那一步就被拒 ⇒ **422**，
    而不是"盘上没有这个文件"的 404。这两个码在这里不能混：404 说的是"这个名字合法、
    只是没有那一份"，422 说的是"这压根不是一个样本名"。真按 404 处理，排查的人会去
    ``voice_preview/`` 目录里找一个叫 ``../../studio.db`` 的文件。
    ``nope.wav`` 才是真正的 404（形状对、盘上没有）。
    """
    assert client.get("/api/v1/media/voice_preview/..%2F..%2Fstudio.db").status_code == 422
    assert client.get("/api/v1/media/voice_preview/not-a-wav.txt").status_code == 422
    assert client.get("/api/v1/media/voice_preview/nope.wav").status_code == 404


def test_the_preview_dir_is_not_the_sentence_audio_dir(state: AppState) -> None:
    """两个目录**必须**分开：``output/voice`` 有 24 小时 TTL，样本没有。

    混在一起的后果是"第二天所有试听按钮都变回没生成"，而没有任何东西提示
    "是被 GC 清掉的"。
    """
    assert state.paths.voice_preview_dir != state.paths.voice_out_dir
    assert state.paths.voice_out_dir not in state.paths.voice_preview_dir.parents
    assert state.paths.voice_preview_dir in protected_roots(state.paths)
