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
from studio.core.config import (
    AccountConfig,
    PersonaConfig,
    PlatformConfig,
    PrecheckConfig,
    PublishConfig,
)
from studio.core.errors import ErrorCode, PublishError, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.artifact_repo import ArtifactRepo
from studio.domain.cover import CoverInput, CoverOutput
from studio.domain.enums import TaskKind, TaskStatus
from studio.domain.models import QualityReport
from studio.domain.task_service import TaskService
from studio.publish.base import PublishHealth
from studio.publish.cover import CoverResult
from studio.publish.platforms.douyin import DouyinPublisher
from studio.publish.selectors import SelectorPack
from studio.services import publish_service
from studio.services.publish_service import (
    CALIBRATED,
    CALIBRATION_BROKEN,
    CALIBRATION_NA,
    COVER_KIND,
    UNCALIBRATED,
    CoverRequest,
    PublishService,
    default_platforms,
    health_payload,
    platform_options,
    publisher_for,
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


class TestDefaultPlatforms:
    """投递的默认目标平台（T5.9 起要**跳过演练台**）。"""

    @staticmethod
    def _config() -> PublishConfig:
        return PublishConfig(
            accounts=[
                AccountConfig(
                    account_id="acc_main",
                    platform="douyin",
                    profile_dir=Path("data/browser_profile/acc_main"),
                ),
                AccountConfig(
                    account_id="_rehearsal",
                    platform="other",
                    profile_dir=Path("data/browser_profile/_rehearsal"),
                ),
            ],
            platforms={
                "douyin": PlatformConfig(
                    publisher="douyin",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=True,
                    title_max=55,
                    caption_max=1000,
                ),
                "other": PlatformConfig(
                    publisher="fixture",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=True,
                    title_max=55,
                    caption_max=1000,
                ),
            },
        )

    def test_rehearsal_target_is_not_a_default(self) -> None:
        """演练台**不在**默认目标里：否则每条任务投递完都会顺手多出一条"演练发布"。

        那条记录在面板上与真发布长得一样（只差平台代号），而"我没让它发，它自己发了
        一条"是最难解释的一类问题。要演练就显式点名 ``platforms=("other",)``。
        """
        assert default_platforms(self._config()) == ("douyin",)


class TestPlatformOptions:
    """投递面板的选项清单（T5.10 · §06.5.4）。

    面板不许自己列平台：那样等于把 ``config/publish.yaml`` 抄第二遍，加一个平台要改
    两处，而漏改的那一处表现为"这个平台在面板上不存在"。这里验的就是"清单与判据都
    从配置来"，外加两条最容易做错的：
    **演练台必须列出来**（没真账号时的唯一通路）、**点不动的要写清为什么**。
    """

    def _config(self) -> PublishConfig:
        return PublishConfig(
            enabled=True,
            accounts=[
                AccountConfig(
                    account_id="acc_main",
                    platform="douyin",
                    profile_dir=Path("data/browser_profile/acc_main"),
                ),
                AccountConfig(
                    account_id="_rehearsal",
                    platform="other",
                    profile_dir=Path("data/browser_profile/_rehearsal"),
                ),
            ],
            platforms={
                "douyin": PlatformConfig(
                    publisher="douyin",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=True,
                    title_max=55,
                    caption_max=1000,
                ),
                "other": PlatformConfig(
                    publisher="fixture",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=True,
                    title_max=55,
                    caption_max=1000,
                ),
                "xiaohongshu": PlatformConfig(
                    publisher="xiaohongshu",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=False,
                    title_max=20,
                    caption_max=1000,
                ),
            },
        )

    def test_order_follows_the_config_file(self) -> None:
        """顺序 = ``platforms`` 的书写顺序（面板上按这个顺序排）。"""
        codes = [option.code for option in platform_options(self._config())]
        assert codes == ["douyin", "other", "xiaohongshu"]

    def test_rehearsal_is_listed_and_marked(self) -> None:
        """演练台**列出来**且带上记号：它是没有真账号时的唯一通路。"""
        by_code = {option.code: option for option in platform_options(self._config())}
        rehearsal = by_code["other"]
        assert rehearsal.rehearsal is True
        assert rehearsal.selectable is True
        assert rehearsal.accounts == ("_rehearsal",)
        assert "靶页" in rehearsal.note

    def test_disabled_platform_is_listed_but_not_selectable(self) -> None:
        """二线平台（``enabled=false`` · Q9）照旧列出来，但**点不动**，且说清为什么。"""
        by_code = {option.code: option for option in platform_options(self._config())}
        second = by_code["xiaohongshu"]
        assert second.selectable is False
        assert second.enabled is False
        assert "未启用" in second.note

    def test_enabled_platform_without_account_is_not_selectable(self) -> None:
        """平台启用、但没有启用的账号 ⇒ 也点不动。

        这一条是**投递期**的真实行为（``resolve_account`` 会拒），面板显示"点得动"
        而投出去被跳过就是骗人 —— 所以判据必须同一套。
        """
        config = self._config()
        config.platforms["kuaishou"] = PlatformConfig(
            publisher="kuaishou",
            profile="douyin_1080x1920_30fps_v1",
            enabled=True,
            title_max=60,
            caption_max=1000,
        )
        by_code = {option.code: option for option in platform_options(config)}
        assert by_code["kuaishou"].selectable is False
        assert by_code["kuaishou"].accounts == ()
        assert "账号" in by_code["kuaishou"].note

    def test_real_platform_says_it_is_irreversible(self) -> None:
        """开关**打开**时，真平台那一句必须说"不可撤销"（R14）。"""
        by_code = {option.code: option for option in platform_options(self._config())}
        assert by_code["douyin"].rehearsal is False
        assert by_code["douyin"].selectable is True
        assert "不可撤销" in by_code["douyin"].note

    def test_real_platform_says_dead_letter_while_the_switch_is_off(self) -> None:
        """开关**关着**（出厂）时，真平台那一条必须说"直接死信、面板上不会出现记录"。

        这一条是**用户第一眼会撞上的那一档**：出厂 ``enabled: false``，操作员在面板上
        勾了 douyin、按了投递 ⇒ 作业在 worker 侧被守卫挡下，``publications`` 那一行
        根本不会建 ⇒ 发布面板上"什么都没发生"。说成"转人工"会让人去「待人工」区块里
        找一个永远不会出现的记录（陷阱：三处文案写的是转人工，代码做的是死信）。
        """
        # 配置是**冻结**的（`_Base`）⇒ 用 `model_copy` 换一个字段，而不是就地改
        config = self._config().model_copy(update={"enabled": False})
        by_code = {option.code: option for option in platform_options(config)}
        note = by_code["douyin"].note
        assert "死信" in note
        assert "四池调度" in note
        assert "转人工" not in note
        # 演练台**不受开关影响**（它发的是本地靶页）⇒ 那句提示不该出现
        assert "死信" not in by_code["other"].note

    def test_real_platforms_keep_their_order(self) -> None:
        """真平台照旧：按 ``accounts`` 的书写顺序，去重。"""
        config = self._config()
        config.accounts.append(
            AccountConfig(
                account_id="acc_kuaishou",
                platform="kuaishou",
                profile_dir=Path("data/browser_profile/acc_kuaishou"),
            )
        )
        config.accounts.append(
            AccountConfig(
                account_id="acc_main_2",
                platform="douyin",
                profile_dir=Path("data/browser_profile/acc_main_2"),
            )
        )
        config.platforms["kuaishou"] = PlatformConfig(
            publisher="kuaishou",
            profile="douyin_1080x1920_30fps_v1",
            enabled=True,
            title_max=60,
            caption_max=1000,
        )
        assert default_platforms(config) == ("douyin", "kuaishou")

    # ── 校准那一列（T5.14）─────────────────────────────────────────────

    def test_calibration_state_is_reported_for_every_platform(self) -> None:
        """★ "这个平台的选择器验过没有"要能从面板那一行读出来。

        抖音是七份 pack 里**唯一**验过的；演练台不是平台（"真机校准"对它没有意义）。
        """
        by_code = {option.code: option for option in platform_options(self._config())}
        assert by_code["douyin"].calibration == CALIBRATED
        assert "已真机校准" in by_code["douyin"].calibration_note
        assert by_code["xiaohongshu"].calibration == UNCALIBRATED
        assert "calibrate" in by_code["xiaohongshu"].calibration_note
        assert by_code["other"].calibration == CALIBRATION_NA

    def test_douyin_reports_the_gaps_calibration_cannot_fix(self) -> None:
        """校准回答"这些选择器对不对"；``known_gaps`` 回答"还有没有一段流程压根没写"。

        只显示前者会让人以为"校准完就能发了" —— 抖音的数据回收那一组就还没验过。
        """
        by_code = {option.code: option for option in platform_options(self._config())}
        assert any("数据回收" in gap for gap in by_code["douyin"].known_gaps)
        assert by_code["douyin"].to_dict()["known_gaps"] == list(by_code["douyin"].known_gaps)

    def test_a_selectable_but_uncalibrated_platform_warns(self) -> None:
        """★ 平台能投、开关开着、账号也有，但选择器**没验过** ⇒ 那一行必须带警告。

        不加这一句，操作员看到的就是一行和抖音长得一模一样的平台 —— 而它投出去大概率
        发不出去（或者更糟：发出去一半、结果判不出来，见陷阱 #228）。
        """
        config = self._config()
        config.platforms["kuaishou"] = PlatformConfig(
            publisher="kuaishou",
            profile="douyin_1080x1920_30fps_v1",
            enabled=True,
            title_max=60,
            caption_max=1000,
        )
        config.accounts.append(
            AccountConfig(
                account_id="acc_kuaishou",
                platform="kuaishou",
                profile_dir=Path("data/browser_profile/acc_kuaishou"),
            )
        )
        option = {item.code: item for item in platform_options(config)}["kuaishou"]
        assert option.selectable is True
        assert option.calibration == UNCALIBRATED
        assert "⚠️" in option.note
        assert "calibrate" in option.note

    def test_a_pack_that_cannot_be_loaded_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 读不到 pack ⇒ 报 ``broken``，**不抛**。

        这一列画在**投递面板**上：为"某个平台的选择器包装不起来"把整个面板打成 500，
        等于把一个平台的问题变成"面板坏了"（其余六个平台也跟着看不见）。
        """

        def boom(code: str) -> SelectorPack:
            raise PublishError("选择器文件不合法", code=ErrorCode.PUBLISH_SELECTOR_MISS)

        monkeypatch.setattr(publish_service, "load_selector_pack", boom)
        by_code = {option.code: option for option in platform_options(self._config())}
        assert by_code["douyin"].calibration == CALIBRATION_BROKEN
        assert "装不起来" in by_code["douyin"].calibration_note
        # 演练台不走读盘那一条 ⇒ 读不到 pack 这件事与它无关
        assert by_code["other"].calibration == CALIBRATION_NA


class TestPublisherFor:
    """按账号装配发布器（T6.4）：面板上的「检测登录态 / 扫码登录」走的就是这一条。"""

    def test_builds_one_publisher_per_account(self, paths: StudioPaths) -> None:
        config = PublishConfig(
            accounts=[
                AccountConfig(
                    account_id="acc_main",
                    platform="douyin",
                    profile_dir=Path("data/browser_profile/acc_main"),
                )
            ],
            platforms={
                "douyin": PlatformConfig(
                    publisher="douyin",
                    profile="douyin_1080x1920_30fps_v1",
                    enabled=True,
                    title_max=55,
                    caption_max=1000,
                )
            },
        )

        publisher = publisher_for(paths, config, config.accounts[0], headless=False)

        assert isinstance(publisher, DouyinPublisher)
        # 一个账号一份登录态：profile 目录从**账号**推出来，不从配置那一列读
        assert publisher.context.profile_dir == paths.browser_profile_dir / "acc_main"
        # 扫码登录必须可见 —— 无头窗口里没有人能扫那个码
        assert publisher.context.headless is False

    def test_unknown_platform_is_refused(self, paths: StudioPaths) -> None:
        """平台代号在 ``platforms`` 里没有 ⇒ 抛（**不猜**），与投递期同一条判据。

        ``model_construct`` 是**故意**的：``PublishConfig`` 的校验器本来就拦住了
        "账号挂在一个没定义的平台上"，所以这一格正常配不出来。这里绕开它，验的是
        **函数自己**的契约 —— 哪天有人给配置加一条绕过校验的构造路径，
        这一层仍然要拒绝，而不是拿一个空配置去猜。
        """
        account = AccountConfig(
            account_id="acc_x",
            platform="douyin",
            profile_dir=Path("data/browser_profile/acc_x"),
        )
        broken = PublishConfig.model_construct(accounts=[account], platforms={})
        with pytest.raises(StudioError) as info:
            publisher_for(paths, broken, account)
        assert info.value.code == ErrorCode.VALIDATION_FAILED


class TestHealthPayload:
    def test_carries_the_five_fields_the_panel_shows(self) -> None:
        """面板与 CLI ``--json`` 说的是**同一份**形状（各拼一份迟早分叉）。"""
        payload = health_payload(
            PublishHealth(ready=False, logged_in=False, last_check_at="now", hint="需人工扫码登录")
        )
        assert set(payload) == {"ready", "logged_in", "account_name", "hint", "last_check_at"}
        assert payload["hint"] == "需人工扫码登录"
