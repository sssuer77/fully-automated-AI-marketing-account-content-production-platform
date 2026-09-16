"""发布前准备服务（T5.1 · §06.3 / §06.4）。

这一层只做"缝合"：从库里捞出标题、从时间轴算抽帧点、把成片路径找出来。
所以用例断言的都是**缝对了没有**：

- 成片路径取的是 ``manifest.json`` 记的那一条（同一个任务可以有多个 ``*_final.mp4``）；
- 抽帧点取第一句的 ``start_ms + 500``，并**钳在片长之内**；
- 封面文案走模型，模型不行 / 命中禁区 ⇒ 退规则兜底（**不抛**）；
- 失败也留痕（``context_json.cover_path = null`` 与"没试过"是两件事）。

合成那一步（ffmpeg）在这里是**注入的假件** —— 真机那一条在
``tests/unit/publish/test_cover.py`` 与真机演练里跑过。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentResult
from studio.core.config import PersonaConfig, PrecheckConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.artifact_repo import ArtifactRepo
from studio.domain.cover import CoverInput, CoverOutput
from studio.domain.enums import TaskKind, TaskStatus
from studio.domain.models import QualityReport
from studio.domain.task_service import TaskService
from studio.publish.cover import CoverResult
from studio.services.publish_service import (
    COVER_KIND,
    CoverRequest,
    PublishService,
    resolve_final_video,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 只在解析路径的用例里当"任意 id"用（那些用例不碰库）。
TASK_ID = "01TASK000000000000000000"

TITLE = "离谱跑酷地图"
HOOK = "今天我们来看一张特别离谱的跑酷地图"
CTA = "点个关注"

TIMELINE: dict[str, Any] = {
    "task_id": TASK_ID,
    "total_ms": 17_482,
    "sentences": [
        {"seq": 1, "start_ms": 0, "end_ms": 4533, "text": HOOK},
        {"seq": 2, "start_ms": 4783, "end_ms": 9261, "text": "开局只有一格方块"},
    ],
}


# ── 夹具 ──────────────────────────────────────────────────────────────


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """隔离路径 + **一份仓库真实的全站禁区词表**。

    词表要拷进来：禁区扫描合并的是 ``persona.forbidden`` 与
    ``prompts/shared/banned_words.yaml``，而隔离路径下没有后者 ——
    不拷的话这些用例测的就成了"文件在不在"，而不是"两份表合没合对"。
    """
    home = tmp_path / "studio"
    resolved = StudioPaths(home=home, data_dir=home / "data")
    resolved.ensure_runtime_dirs()
    shared = resolved.prompts_dir / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    source = REPO_ROOT / "prompts" / "shared" / "banned_words.yaml"
    (shared / "banned_words.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return resolved


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    """一条 ``completed`` 的任务（封面与校验都只对已出片的任务有意义）。

    走**状态机**而不是直接 UPDATE：``tasks.status`` 的迁移必须留下 ``task_events``，
    否则夹具造出来的是一条库里查不到来路的任务 —— 那和真实数据不一样。
    """
    service = TaskService(connection)
    created = service.create(kind=TaskKind.VIDEO, title=TITLE)
    path = (
        TaskStatus.DRAFTING,
        TaskStatus.REVIEWING,
        TaskStatus.QUEUED_VOICE,
        TaskStatus.VOICING,
        TaskStatus.QUEUED_RENDER,
        TaskStatus.RENDERING,
        TaskStatus.COMPLETED,
    )
    for status in path:
        service.transition(created.id, status, actor="test", reason="测试")
    return created.id


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_final(paths: StudioPaths, task_id: str, *, stamp: str = "20260916-130927") -> Path:
    path = paths.final_video(task_id, stamp=stamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mp4")
    return path


def _make_manifest(paths: StudioPaths, task_id: str, final: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "task_id": task_id,
        "final": final.as_posix(),
        "duration_ms": 17_482,
        "watermark": {"enabled": True, "skipped_reason": None},
    }
    payload.update(overrides)
    _write_json(paths.manifest_json(task_id), payload)


class FakeCover:
    """假的合成件：记下参数，按脚本返回成功 / 失败。"""

    def __init__(self, *, ok: bool = True, fallback: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ok = ok
        self.fallback = fallback

    def __call__(self, **kwargs: Any) -> CoverResult:
        self.calls.append(kwargs)
        target: Path = kwargs["target"]
        plan = {"title_text": kwargs["output"].title_text, "frame_at_ms": kwargs["output"].frame_at_ms}
        if not self.ok:
            return CoverResult(path=None, plan=plan, warnings=["假的失败"], fallback_background=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")
        return CoverResult(path=target, plan=plan, fallback_background=self.fallback)


class FakeAgent:
    """假的 Cover Agent（``run`` 是 async 的，与真件同形）。"""

    def __init__(self, result: AgentResult[CoverOutput]) -> None:
        self.result = result
        self.calls: list[CoverInput] = []

    async def run(self, _ctx: Any, payload: CoverInput) -> AgentResult[CoverOutput]:
        self.calls.append(payload)
        return self.result


def _agent_ok(**overrides: Any) -> FakeAgent:
    data: dict[str, Any] = {
        "title_text": "模型写的标题",
        "sub_text": "模型写的次文案",
        "highlight_words": ["标题"],
        "frame_at_ms": 900,
        "banned_checked": True,
    }
    data.update(overrides)
    return FakeAgent(AgentResult[CoverOutput](ok=True, data=CoverOutput.model_validate(data)))


def _agent_failed(**overrides: Any) -> FakeAgent:
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": str(ErrorCode.LLM_UPSTREAM),
        "error_message": "通道都挂了",
    }
    payload.update(overrides)
    return FakeAgent(AgentResult[CoverOutput](**payload))


def _service(
    connection: sqlite3.Connection,
    paths: StudioPaths,
    *,
    agent: Any = None,
    build: Any = None,
    persona: Any = None,
    precheck: PrecheckConfig | None = None,
) -> PublishService:
    return PublishService(
        connection,
        paths=paths,
        publish=type("P", (), {"precheck": precheck or PrecheckConfig()})(),
        persona=persona,
        cover_agent=agent,
        build=build or FakeCover(),
    )


# ── 成片路径 ──────────────────────────────────────────────────────────


class TestResolveFinalVideo:
    def test_manifest_wins(self, paths: StudioPaths, task_id: str) -> None:
        """同一个任务可以有多个 ``*_final.mp4``（重合成一次多一个）—— 以 manifest 为准。"""
        old = _make_final(paths, task_id, stamp="20260101-000000")
        new = _make_final(paths, task_id, stamp="20260916-130927")
        _make_manifest(paths, task_id, old)
        assert resolve_final_video(task_id, paths) == old
        assert new.is_file()  # 另一条也在盘上，只是没被选中

    def test_falls_back_to_the_newest_on_disk(self, paths: StudioPaths, task_id: str) -> None:
        _make_final(paths, task_id, stamp="20260101-000000")
        newest = _make_final(paths, task_id, stamp="20260916-130927")
        assert resolve_final_video(task_id, paths) == newest

    def test_manifest_pointing_at_a_missing_file_falls_back(self, paths: StudioPaths, task_id: str) -> None:
        """manifest 记的那条被挪走了 ⇒ 退回按名字找，而不是报"没有成片"。"""
        _make_manifest(paths, task_id, paths.videos_dir / "gone_final.mp4")
        real = _make_final(paths, task_id)
        assert resolve_final_video(task_id, paths) == real

    def test_nothing_anywhere_is_none(self, paths: StudioPaths, task_id: str) -> None:
        assert resolve_final_video(task_id, paths) is None


# ── 封面 ──────────────────────────────────────────────────────────────


class TestMakeCover:
    async def test_uses_the_agent_text(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        agent = _agent_ok()
        report = await _service(connection, paths, agent=agent, persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert report.ok is True
        assert report.source == "agent"
        assert report.plan["title_text"] == "模型写的标题"

    async def test_cover_lands_in_the_covers_dir(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        report = await _service(connection, paths, agent=_agent_ok(), persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert report.cover_path is not None
        assert report.cover_path.parent == paths.covers_dir
        assert report.cover_path.name.endswith(f"_{task_id}_cover.jpg")
        assert not list(paths.covers_dir.glob("*.partial*"))

    async def test_rule_path_uses_the_first_sentence_of_the_timeline(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """规则兜底时抽帧点取 §06.3 的 ``hook`` 句 ``start_ms + 500``。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), {**TIMELINE, "sentences": [{"start_ms": 1200}]})
        report = await _service(connection, paths, agent=None, persona=None).make_cover(
            CoverRequest(task_id=task_id, use_agent=False)
        )
        assert report.frame_at_ms == 1700

    async def test_agent_may_override_the_default_frame(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """模型挑的时刻**优先于**默认值 —— 默认值只是提示词里的一个建议（§06.3 第 4 条）。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        report = await _service(
            connection, paths, agent=_agent_ok(frame_at_ms=900), persona=_persona()
        ).make_cover(CoverRequest(task_id=task_id))
        assert report.frame_at_ms == 900

    async def test_agent_frame_is_clamped_into_the_video(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """模型挑到片尾之外 ⇒ 钳回片内。``-ss`` 落在片尾会解不出帧 ⇒ 整张封面降级。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final, duration_ms=10_000)
        _write_json(paths.timeline_json(task_id), {**TIMELINE, "total_ms": 10_000})
        report = await _service(
            connection, paths, agent=_agent_ok(frame_at_ms=999_999), persona=_persona()
        ).make_cover(CoverRequest(task_id=task_id))
        assert report.frame_at_ms == 9_999

    async def test_agent_failure_falls_back_to_the_rule_text(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """模型挂了 ⇒ 规则兜底（标题当主文案）。**不抛**：封面不值得把一条能发的片子卡住。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        report = await _service(connection, paths, agent=_agent_failed(), persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert report.ok is True
        assert report.source == "rule"
        assert report.agent_error == "通道都挂了"
        assert any("规则兜底" in item for item in report.warnings)

    async def test_no_agent_at_all_uses_the_rule_text(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        report = await _service(connection, paths, agent=None, persona=None).make_cover(
            CoverRequest(task_id=task_id, use_agent=False)
        )
        assert report.source == "rule"

    async def test_render_failure_is_reported_not_raised(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """§06.3 第二级降级：封面没出来 ⇒ ``ok=False`` + 一句"无封面发布"。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        report = await _service(
            connection, paths, agent=_agent_ok(), build=FakeCover(ok=False), persona=_persona()
        ).make_cover(CoverRequest(task_id=task_id))
        assert report.ok is False
        assert report.cover_path is None
        assert any("无封面发布" in item for item in report.warnings)

    async def test_failure_is_still_recorded_in_context(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """失败也要留痕：``cover_path=null`` 与"没试过"在面板上是两件事。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        await _service(
            connection, paths, agent=_agent_ok(), build=FakeCover(ok=False), persona=_persona()
        ).make_cover(CoverRequest(task_id=task_id))
        task = TaskService(connection).get(task_id)
        assert "cover_path" in task.context
        assert task.context["cover_path"] is None
        assert task.context["cover_plan"]["ok"] is False

    async def test_context_keeps_the_final_path(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """``context_json`` 里躺着互不相干的几件事 ⇒ **合并**写，不能整体覆盖。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        connection.execute(
            "UPDATE tasks SET context_json = ? WHERE id = ?",
            (json.dumps({"final_path": final.as_posix()}), task_id),
        )
        connection.commit()
        await _service(connection, paths, agent=_agent_ok(), persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        context = TaskService(connection).get(task_id).context
        assert context["final_path"] == final.as_posix()
        assert context["cover_path"]

    async def test_artifact_is_recorded(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        report = await _service(connection, paths, agent=_agent_ok(), persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert report.cover_path is not None
        rows = ArtifactRepo(connection, data_dir=paths.data_dir).list_for_task(task_id, kind=COVER_KIND)
        assert len(rows) == 1
        assert rows[0].path == "output/covers/" + report.cover_path.name

    async def test_no_artifact_when_the_cover_failed(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        await _service(
            connection, paths, agent=_agent_ok(), build=FakeCover(ok=False), persona=_persona()
        ).make_cover(CoverRequest(task_id=task_id))
        repo = ArtifactRepo(connection, data_dir=paths.data_dir)
        assert repo.list_for_task(task_id, kind=COVER_KIND) == []

    async def test_unknown_task_raises(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        with pytest.raises(StudioError):
            await _service(connection, paths).make_cover(CoverRequest(task_id="01NOPE"))

    async def test_agent_sees_the_forbidden_terms(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        agent = _agent_ok()
        await _service(connection, paths, agent=agent, persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert "脏话" in agent.calls[0].forbidden
        assert "国家级" in agent.calls[0].forbidden  # 全站词表也合并进来了

    async def test_agent_sees_the_script_summary(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        _write_json(paths.timeline_json(task_id), TIMELINE)
        agent = _agent_ok()
        await _service(connection, paths, agent=agent, persona=_persona()).make_cover(
            CoverRequest(task_id=task_id)
        )
        assert agent.calls[0].task_id == task_id
        assert agent.calls[0].duration_ms == 17_482
        assert agent.calls[0].hook_start_ms == 0


# ── 二次校验 ──────────────────────────────────────────────────────────


class TestPrecheck:
    def test_missing_video_raises_with_a_way_out(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        with pytest.raises(StudioError) as excinfo:
            _service(connection, paths).precheck(task_id)
        assert excinfo.value.remediation

    def test_reads_the_quality_from_the_task(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final, watermark={"enabled": False})
        TaskService(connection).set_quality(
            task_id, QualityReport(lufs=-16.0, true_peak=-1.2, watermark_applied=False)
        )
        report = _service(connection, paths).precheck(task_id, measure=lambda _p: (-16.0, -1.2))
        assert report.passed is False
        assert report.error_code == ErrorCode.PRECHECK_WATERMARK.value

    def test_passes_when_everything_is_in_order(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        TaskService(connection).set_quality(
            task_id, QualityReport(lufs=-16.0, true_peak=-1.2, watermark_applied=True)
        )
        report = _service(connection, paths).precheck(task_id, measure=lambda _p: (-16.0, -1.2))
        assert report.passed is True

    def test_manifest_is_the_watermark_fallback(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        """``quality_json`` 没记水印 ⇒ 读 manifest（同一次渲染自己写的原始记录）。"""
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final, watermark={"enabled": True})
        report = _service(connection, paths).precheck(task_id, measure=lambda _p: (-16.0, -1.2))
        watermark = next(gate for gate in report.gates if gate.name == "watermark")
        assert watermark.passed is True
        assert watermark.context["source"] == "manifest.json"

    def test_forbidden_terms_merge_persona_and_the_shipped_table(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        final = _make_final(paths, task_id)
        _make_manifest(paths, task_id, final)
        TaskService(connection).set_quality(
            task_id, QualityReport(lufs=-16.0, true_peak=-1.2, watermark_applied=True)
        )
        terms = _service(connection, paths, persona=_persona()).forbidden_terms()
        assert terms[0] == "脏话"  # persona 在前：频道自己的规矩先报
        assert "国家级" in terms

    def test_forbidden_terms_are_deduplicated(
        self, paths: StudioPaths, connection: sqlite3.Connection, task_id: str
    ) -> None:
        persona = _persona(forbidden=["国家级", "脏话"])
        terms = _service(connection, paths, persona=persona).forbidden_terms()
        assert list(terms).count("国家级") == 1


def _persona(**overrides: Any) -> PersonaConfig:
    data: dict[str, Any] = {
        "id": "persona_default",
        "name": "熊大熊二·MC跑酷",
        "role_desc": "两只熊的跑酷解说搭档",
        "tone": "嘴碎、互相拆台",
        "audience": "中小学生与怀旧玩家",
        "catchphrases": ["这不科学", "俺寻思"],
        "forbidden": ["脏话", "政治"],
    }
    data.update(overrides)
    return PersonaConfig.model_validate(data)
