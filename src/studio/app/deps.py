"""应用层依赖组装（T1.7 · §02.2）。

`create_app()` 拿到的所有东西都在这里造出来：连接、`LogService`、`Hub`、快照 provider。
这样测试可以直接 `build_state(paths=tmp)` 造一套隔离环境，不需要起真服务。

两个"为什么"
------------
1. **为什么连接是"按线程取"**：应用进程里事件循环线程（Hub tail / async 路由）与
   Starlette 线程池（同步 `def` 路由）都会碰库，而 `sqlite3` 连接线程亲和。
   统一走 `ThreadLocalConnections`，每线程一条（T1.5 裁定 27）。
2. **为什么快照 provider 在这里而不是 `ws/`**：`ws/` 只读且不碰数据库（契约测试锁死），
   真正的 SQL 属于应用层。分层方向 `app → ws → services → db`，反向依赖会被契约测试拦下。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from studio.agents.cover import CoverAgent
from studio.agents.director import DirectorAgent
from studio.agents.feedback_classifier import FeedbackClassifierAgent
from studio.agents.gateway import LogSink
from studio.agents.gateway_factory import build_gateway
from studio.agents.ideator import IdeatorAgent
from studio.agents.news_scout import NewsScoutAgent
from studio.agents.outliner import OutlinerAgent
from studio.agents.planner import PlannerAgent
from studio.agents.prompts import PromptLibrary
from studio.agents.writer import WriterAgent
from studio.app.metrics import MetricsPump
from studio.app.persona_events import PersonaBroadcaster
from studio.app.recycle import MetricsRecyclePump, SchedulePump
from studio.app.watchdog import WatchdogPump
from studio.core.config import (
    HandoffConfig,
    PersonaConfig,
    PoolsConfig,
    PublishConfig,
    RuntimeSettings,
    llm_config_provider,
    load_app_config,
    load_config,
    load_pools_config,
    load_publish_config,
    load_runtime_settings,
)
from studio.core.errors import StudioError
from studio.core.files import stream_sha256
from studio.core.logging import get_logger
from studio.core.media import analyze_volume, extract_thumbnail, measure_loudness, probe_media
from studio.core.outputs_store import OutputsStore
from studio.core.paths import StudioPaths
from studio.core.persona_store import PersonaStore, get_persona_store
from studio.core.proto import Severity
from studio.core.secret_store import SecretStore
from studio.db import JobStore, ThreadLocalConnections
from studio.db.models import DirectionRow, TopicRow
from studio.db.repositories import AuditRepo, DirectionRepo, TopicRepo
from studio.domain.enums import AutoApprovePolicy
from studio.pools.heartbeat import HeartbeatStore
from studio.pools.runner import POOL_NAMES
from studio.services import news_service
from studio.services.asset_service import AssetService
from studio.services.log_service import LogService
from studio.services.metrics_service import (
    METRICS_SAMPLE_INTERVAL_SEC,
    MetricsService,
    ResourceSnapshot,
    probe_resources,
)
from studio.services.observability_service import ObservabilityService
from studio.services.outputs_service import OutputsService
from studio.services.overview_service import OverviewService
from studio.services.persona_service import PersonaService
from studio.services.pipeline_job_service import PipelineJobService
from studio.services.pool_service import PoolService
from studio.services.prompt_service import PromptService
from studio.services.publish_accounts_service import PublishAccountsService, PublisherBuilder
from studio.services.publish_assist_service import PublishAssistService
from studio.services.publish_service import PublishService
from studio.services.render_job_service import RenderJobService
from studio.services.review_service import ReviewService
from studio.services.script_service import ScriptService
from studio.services.service_manager import ServiceManager
from studio.services.settings_service import SettingsService
from studio.services.topic_service import TopicService
from studio.services.voice_preview import VoicePreviewService
from studio.services.watchdog_service import WatchdogService
from studio.ws.hub import Hub, HubSettings
from studio.ws.snapshots import TASK_SNAPSHOT_ROWS, SnapshotFn, SnapshotRegistry

__all__ = [
    "TOPIC_SNAPSHOT_ROWS",
    "AppState",
    "AssetTools",
    "active_persona",
    "asset_service_for",
    "build_state",
    "handoff_config_for",
    "outputs_service_for",
    "overview_service_for",
    "persona_service_for",
    "pool_service_for",
    "publish_accounts_service_for",
    "publish_config_for",
    "publish_cover_service_for",
    "review_service_for",
    "script_service_for",
    "settings_service_for",
    "task_snapshot_sql",
    "topic_service_for",
]

#: 握手快照里的选题池条数（瀑布流首屏够画就行；更多走 REST 分页）
TOPIC_SNAPSHOT_ROWS: int = 100

logger = get_logger("studio.app.deps")

#: 任务快照的字段（§04.4.1："tasks:[…20 条…]"）
_TASK_COLUMNS = "id, title, status, pool, priority, progress, stage_detail, created_at, updated_at"


@dataclass(frozen=True, slots=True)
class AssetTools:
    """素材库用的外部工具（T4.8）。

    与 `build_state(metrics_probe=...)` 同一条理由：扫盘要跑 ffprobe / ffmpeg 抽帧 /
    响度分析 / 分块指纹。测试注入假件之后，"入库时探不出来怎么办"这类用例不再
    以"这台机器装没装 ffmpeg"为前提 —— 那是最难查的一类 flaky。

    生产里全部是默认实现（真 ffmpeg），所以这个类**只是注入点**，不是配置项。
    """

    probe: Any = probe_media
    volume: Any = analyze_volume
    thumbnail: Any = extract_thumbnail
    loudness: Any = measure_loudness
    digest: Any = stream_sha256


@dataclass(slots=True)
class AppState:
    """一套运行期依赖（进程内单例；测试里每个用例一套）。"""

    paths: StudioPaths
    connections: ThreadLocalConnections
    logs: LogService
    hub: Hub
    #: 人物库（T4.13）。**由 `paths` 现造**，不是 `get_persona_store()` 那个进程单例：
    #: `build_state(paths=tmp)` 必须自洽，否则"在测试里改人物"会改到真仓库的
    #: `config/persona.yaml`（§04.5.8 裁定 158）。生产两者指向同一个文件。
    persona: PersonaStore
    #: 人物变更 → `system.persona_changed`（订阅上面那个 store；`lifespan` 起停）
    persona_events: PersonaBroadcaster
    #: 合成配置（T4.7）：`config/outputs.yaml` 的热重载仓库。与 `persona` 同一条裁定
    #: （158）——**由 `paths` 现造**，否则 `build_state(paths=tmp)` 里点一次「保存」
    #: 会写进真仓库的那一份。生产里只此一份，没有进程级单例。
    outputs: OutputsStore
    #: 密钥（设置面板）：`config/secrets.yaml` 的热重载仓库。同样**由 `paths` 现造**。
    #: 它必须是**网关用的那一个**实例 —— 面板写完、网关读旧的，就是「填了没生效」。
    secrets: SecretStore
    #: 运行期可改的少量配置（T4.2 起）：`/overview` 的"一键全自动"改的就是它。
    #: **进程内单例**：必须与 `OverviewService` 共享同一个引用，否则改完这一份、
    #: 下一次请求又拿到旧值，面板上就是"改了但没变"。
    settings: RuntimeSettings
    metrics: MetricsService
    pump: MetricsPump
    #: 数据回收的周期循环（T5.4 · §06.6）。**进程内单例**：它持有 asyncio 任务，
    #: 每次请求现造一份等于每来一个请求就多起一条循环。`runnable=False` ⇒ 不起跳。
    metrics_recycle: MetricsRecyclePump
    #: 定时发布的周期循环（T5.6 · §04.6.5.1）。**进程内单例**，与 `metrics_recycle`
    #: 同一条理由。两个泵的节奏不同（60s 采数 / 30s 排期），所以分开起 —— 见
    #: `SchedulePump` 的注释。
    schedule_pump: SchedulePump
    #: 无人值守守护（T4.11）。**进程内单例**：它的重启计数是内存状态，现造一份
    #: 等于每次请求都从零开始 ⇒ "超限停止重启"永远触发不了。REST 面读的也是它。
    watchdog: WatchdogService
    #: 守护的周期循环（`lifespan` 起停；`runnable=False` ⇒ 不起跳）
    watchdog_pump: WatchdogPump
    #: 出片任务登记表（T4.6）。**进程内单例**：任务表与工作线程都是内存状态，
    #: 每次请求现造一份等于"刚提交的任务下一次请求就查不到了"。`lifespan` 起停它的
    #: 工作线程。代价写在 `render_job_service` 的模块注释里（重启即丢，成片不受影响）。
    render_jobs: RenderJobService
    #: 一键出片登记表（T4.14 延伸）。**进程内单例**，与 `render_jobs` 同一条理由：
    #: 任务表与工作线程都是内存状态。两者并存是因为管的**不是同一件事** ——
    #: `render_jobs` 管渲染那一步（给它文案或任务号，它出片），这里管整条链路
    #: （从任务当前状态出发，该投配音就投、该拼母带就拼、该渲染就渲染）。
    #: `lifespan` 起停它的工作线程。
    pipeline_jobs: PipelineJobService
    #: 音色试听样本（T2.4）。**进程内单例**：它的状态就是"哪几个音色正在生成"，
    #: 每次请求现造一份等于"刚点完生成、下一次刷新就查不到了"，面板会一直转圈。
    #: 生成跑在它自己的后台线程里（与 `render_jobs` 同一条：重启即丢，产物不受影响）。
    voice_previews: VoicePreviewService
    snapshots: SnapshotRegistry = field(default_factory=SnapshotRegistry)
    #: 素材库的外部工具（T4.8）。**由 `build_state` 注入**：REST 面每次现造服务，
    #: 注入点只有这一处，测试换一次假件就够（不必去 patch 路由模块的内部名字）。
    asset_tools: AssetTools = field(default_factory=AssetTools)
    #: 发布器的装配器（T6.4）：账号面板的「检测登录态 / 扫码登录」要按账号造一个
    #: Publisher。``None`` ⇒ 真的那个（会起浏览器）。与 `asset_tools` 同一条 ——
    #: 换一次假件，单测就不必以"这台机器装没装 Chromium"为前提。
    publish_publishers: PublisherBuilder | None = None

    def close(self) -> None:
        """关连接（Hub 由 `lifespan` 先停）。"""
        self.connections.close_all()


def build_state(
    *,
    paths: StudioPaths,
    connections: ThreadLocalConnections | None = None,
    hub_settings: HubSettings | None = None,
    metrics_probe: Callable[..., ResourceSnapshot] | None = None,
    asset_tools: AssetTools | None = None,
    publish_publishers: PublisherBuilder | None = None,
) -> AppState:
    """组装运行期依赖（`connections=None` ⇒ 按 `paths.db_file` 现造）。

    :param metrics_probe: 资源采样实现（``None`` ⇒ 真探针）。测试注入假件之后，
        ``pump.tick()`` 不再碰 `nvidia-smi` 与真磁盘 —— 否则单测会随开发机的
        显存占用与剩余空间飘（那是最难查的一类 flaky）。
    :param asset_tools: 素材库的外部工具（``None`` ⇒ 真 ffmpeg / ffprobe）。
    :param publish_publishers: 发布器装配器（``None`` ⇒ 真 Playwright）。
    """
    pool = connections if connections is not None else ThreadLocalConnections(paths.db_file)
    settings = load_runtime_settings(paths)
    snapshots = SnapshotRegistry()
    logs = LogService(pool.get)
    metrics = MetricsService(
        paths=paths,
        log=logs,
        free_c_min_gb=settings.free_c_min_gb,
        free_d_min_gb=settings.free_d_min_gb,
        probe=metrics_probe or probe_resources,
    )
    snapshots.register("tasks", _tasks_provider(pool))
    snapshots.register("pools", _pools_provider(pool))
    snapshots.register("topics", _topics_provider(pool))
    snapshots.register("metrics", _metrics_provider(metrics))
    hub = Hub(logs=logs, snapshots=snapshots, settings=hub_settings)
    logs.attach_waker(hub.wake)
    pump = MetricsPump(hub=hub, connections=pool, metrics=metrics)
    # 配置**每拍现读**：`metrics_schedule_hours` 是 §06.6 写着"可配"的那一项，
    # 在这里存一份快照就等于"改完要重启 API"（见 `MetricsRecyclePump` 的注释）。
    metrics_recycle = MetricsRecyclePump(
        connections=pool,
        paths=paths,
        config_provider=lambda: load_config(paths).bundle.publish,
        log=logs,
    )
    # 排期与采数分开起（见 `SchedulePump` 的注释）；两者都**每拍现读**配置。
    schedule_pump = SchedulePump(
        connections=pool,
        paths=paths,
        config_provider=lambda: load_config(paths).bundle.publish,
        log=logs,
    )
    persona = PersonaStore(paths)
    outputs = OutputsStore(paths)
    watchdog = WatchdogService(
        paths=paths,
        connection_factory=pool.get,
        manager=ServiceManager(paths),
        log=logs,
        metrics=metrics,
        settings=settings,
        pools_config=_pools_config_or_none(paths),
        # 守护跑在 API 进程里 ⇒ 它**守不了自己**。写清 self_name 比假装守得住有用：
        # 总览台上那一条会显示"由 ops/start_all.ps1 守护"（P4）。
        self_name="api",
    )
    logger.info("app.state_built", db=str(paths.db_file), channels=list(snapshots.channels()))
    return AppState(
        paths=paths,
        connections=pool,
        logs=logs,
        hub=hub,
        persona=persona,
        persona_events=PersonaBroadcaster(store=persona, hub=hub),
        outputs=outputs,
        # 密钥与网关共用同一个实例：面板写完、网关下一次调用就取到新的。
        secrets=SecretStore(paths),
        settings=settings,
        metrics=metrics,
        pump=pump,
        metrics_recycle=metrics_recycle,
        schedule_pump=schedule_pump,
        watchdog=watchdog,
        watchdog_pump=WatchdogPump(watchdog=watchdog),
        # `outputs=None` 是**故意的**：出片每次现读 `config/outputs.yaml`，于是
        # "在合成配置面板改完档位、下一次出片就用新档"这句话是真的。在这里存一份
        # 配置快照，长跑的 API 进程就会一直用启动那一刻的档位 —— 面板显示新值、
        # 实际按旧值编码，这类漂移没有任何地方会报错。
        render_jobs=RenderJobService(paths=paths, connection_factory=pool.get),
        # `outputs=None` 与上面同一条：出片每次现读 `config/outputs.yaml`，于是
        # "在合成配置面板改完档位、下一次出片就用新档"这句话在整条链路上也成立。
        pipeline_jobs=PipelineJobService(paths=paths, connection_factory=pool.get),
        # 音色试听（T2.4）：**与任务无关**，所以不进队列（`jobs.task_id` 有外键，
        # 硬塞一条作业就得先造一条假任务）。它只读 `paths` —— 引擎判据自己现问，
        # 于是"服务起来了没有"这件事在每一次生成时都是最新的。
        voice_previews=VoicePreviewService(paths=paths),
        snapshots=snapshots,
        asset_tools=asset_tools or AssetTools(),
        publish_publishers=publish_publishers,
    )


def _pools_config_or_none(paths: StudioPaths) -> PoolsConfig | None:
    """读 `config/pools.yaml`；**读不到就退回 `None`（守护整体停用）**。

    为什么容错而不抛：`build_state(paths=tmp)` 在集成测试里会拿到"只有 config/ 的
    最小家目录"，甚至什么都没有。为了几个阈值让**整个 API 起不来**是不划算的 ——
    真正的硬门禁在 `doctor` 与 `run_server`（它们该拒绝启动时绝不含糊）。
    与 `load_runtime_settings` 同一取舍（T4.2 裁定）。
    """
    try:
        return load_pools_config(paths)
    except StudioError as exc:
        logger.warning("app.pools_config_fallback", error=str(exc))
        return None


def review_service_for(state: AppState) -> ReviewService:
    """按**当前线程**的连接造一个确认闸服务（REST 面只用决断 / 捞回两个能力）。

    为什么每次现造而不是放进 :class:`AppState`：``ReviewService`` 持有
    ``sqlite3.Connection``，而连接是**线程亲和**的（``ThreadLocalConnections``）
    —— 同步路由跑在 Starlette 的线程池里，每次可能是不同线程。服务本身很轻
    （几个仓储包装），现造没有成本。

    Agent 依赖刻意不注入：``decide_approval`` / ``rescue_task`` 是纯业务规则，
    不需要 LLM 网关。**日志出口必须注入** —— `_emit` 的 payload 里捎带着
    ``approval.requested`` / ``approval.decided`` 的事件声明（裁定 122），
    少了它，事件只会进控制台、不会进表，前端永远收不到。
    """
    # 策略从**共享的** `RuntimeSettings` 现取：面板上点过"一键全自动"之后，
    # 走 REST 的那条审稿路径要立刻用新策略（写稿池 worker 的下一单元同理）。
    return ReviewService(
        state.connections.get(),
        policy=AutoApprovePolicy(state.settings.auto_approve_policy),
        log=state.logs.append,
    )


def overview_service_for(state: AppState) -> OverviewService:
    """按**当前线程**的连接造一个总览台服务（与 `review_service_for` 同一手法）。

    `settings` / `metrics` / `watchdog` 来自 `AppState`（**共享引用**）：前者是
    "一键全自动"改的那份配置，中者持有最近一拍资源与磁盘告警去重状态，后者持有
    自愈计数 —— 三者都必须跨请求存活，现造一份就等于每次刷新都从零开始
    （自愈计数归零 ⇒ "超限停手"永远触发不了）。
    """
    return OverviewService(
        state.connections.get(),
        paths=state.paths,
        settings=state.settings,
        metrics=state.metrics,
        watchdog=state.watchdog,
        log=state.logs.append,
    )


def observability_service_for(state: AppState) -> ObservabilityService:
    """按**当前线程**的连接造一个观测面板服务（与 `overview_service_for` 同一手法）。

    内部复用 `overview_service_for(state)`：资源 / 队列 / 产量 / 进程四块必须是
    **同一份**数字，现造第二个 `OverviewService` 就等于让两屏各自采样一次。
    """
    return ObservabilityService(
        state.connections.get(),
        paths=state.paths,
        overview=overview_service_for(state),
    )


def pool_service_for(state: AppState) -> PoolService:
    """按**当前线程**的连接造一个四池控制台服务（与 `overview_service_for` 同一手法）。

    配置读失败 ⇒ ``config=None``（卡片带 ``error`` 上桌），**不**让整个请求 500：
    ``pools.yaml`` 被改坏时，用户最需要看到的恰恰是「配置读不到，去修这一份」。

    读的是 ``pools.yaml`` **这一份**（:func:`load_pools_config`），不走 `load_config`
    —— 否则 `llm.yaml` 里少一个 key 会让四池面板打不开（裁定 150）。
    """
    config_path = str(state.paths.config_dir / "pools.yaml")
    config = None
    try:
        config = load_pools_config(state.paths)
    except StudioError as exc:
        logger.warning("app.pools_config_failed", path=config_path, error=str(exc))
    return PoolService(
        state.connections.get(),
        config=config,
        config_path=config_path,
        log=state.logs.append,
    )


def publish_config_for(state: AppState) -> PublishConfig:
    """读 ``config/publish.yaml``（**只碰这一份**）。

    与 :func:`pool_service_for` 同一条理由：发布面板要知道"有哪些平台、哪些账号、
    开关状态"，而 ``llm.yaml`` 里少一个 key 不该让发布面板打不开（裁定 150）。
    读失败**不吞**：这里不做 ``config=None`` 那套降级 —— 投递一条发布需要**确切**的
    平台与账号清单，拿着半个配置去投会把"配置坏了"变成"发到错的账号上"。
    面板的只读区块走的是各自的仓储，不经过本函数。
    """
    return load_publish_config(state.paths)


def handoff_config_for(state: AppState) -> HandoffConfig:
    """读 ``config/app.yaml`` 的 ``handoff`` 段（交付包的开关 / 适配器 / 落点）。

    为什么读 ``app.yaml`` 而不是 ``publish.yaml``：交付包是**外部制片台对接**
    （§04.6.3 · A2）的那条缝，它不只在发布那一屏用得上（审片、归档、别人来取片都
    可能用）。``publish.yaml → handoff`` 只管"发布这条链路要不要顺手交付一份"，
    两处的开关**刻意不合并**：合并之后"我只想让审片台取片、但不想发布"就配不出来了。

    读失败**不吞**（与 :func:`publish_config_for` 同一条）：落点目录是"包往哪儿写"，
    拿着一个猜的路径去写等于把文件散到别处。
    """
    return load_app_config(state.paths).handoff


def asset_service_for(state: AppState) -> AssetService:
    """按**当前线程**的连接造一个素材库服务（与 `pool_service_for` 同一手法）。

    素材是**文件 + 表**两处一起才算数：扫盘看的是 ``data/assets/`` 与
    ``data/voice_src/`` 下的文件，入库写的是 ``broll_clips`` / ``bgm_tracks`` /
    ``voice_profiles``。服务层每次现造，注入的探测器/缩略图/响度/指纹就是真 ffmpeg
    （测试里换假件即可，不必把这台机器装没装 ffmpeg 变成用例前提）。
    """
    return AssetService(
        state.connections.get(),
        paths=state.paths,
        log=state.logs.append,
        probe=state.asset_tools.probe,
        volume=state.asset_tools.volume,
        thumbnail=state.asset_tools.thumbnail,
        loudness=state.asset_tools.loudness,
        digest=state.asset_tools.digest,
    )


def persona_service_for(state: AppState) -> PersonaService:
    """按**当前线程**的连接造一个人物库服务（与 `pool_service_for` 同一手法）。

    传进去的 store 是 :attr:`AppState.persona` —— 与 `PersonaBroadcaster` 订阅的
    **是同一个对象**。若这里现造一份，面板改了人物、事件却发不出来（订阅在另一份上）。
    """
    return PersonaService(
        state.persona,
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
    )


def outputs_service_for(state: AppState) -> OutputsService:
    """按**当前线程**的连接造一个合成配置服务（与 `persona_service_for` 同一手法）。

    传进去的 store 是 :attr:`AppState.outputs` —— 与 `OutputsStore` 的热重载状态
    **是同一个对象**。若这里现造一份，面板每次保存都会拿到"自己刚写的那份"之外
    的旧快照（`version` 不动、`source_sha256` 对不上），表现为"改完一次就 409"。

    ``home`` 必须给：水印的 ``path`` 是**相对家目录**写的（``templates/...``），
    面板要把它解析成绝对路径才能回答"这张 PNG 到底在不在"（D5 的必做项判据）。
    """

    return OutputsService(
        state.outputs,
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
        home=state.paths.home,
    )


def active_persona() -> PersonaConfig:
    """当前激活人物（热重载单例；`studio persona use` 切的就是它）。

    REST 面与 CLI **共用同一次热重载**：面板上点"生成选题"时用的定位，
    与命令行跑 `studio topics analyze` 时用的是同一份（否则"网页上跑出来
    的方向跟 CLI 不一样"会成为一条查不完的悬案）。
    """
    return get_persona_store().current().config


def settings_service_for(state: AppState) -> SettingsService:
    """装配设置面板服务（密钥仓库**必须**是 `state.secrets` 那一个实例）。

    为什么不现造一个：网关用的是 `state.secrets`。现造一份意味着「面板写进 A、
    网关读 B」—— 症状是「填了 Key 却还说没配」，而两边的代码单看都没错。
    """
    return SettingsService(
        state.paths,
        state.secrets,
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
    )


def publish_accounts_service_for(state: AppState) -> PublishAccountsService:
    """装配发布账号服务（面板加号 / 改号 / 停用 / 删号 —— 写回 ``config/publish.yaml``）。

    与 :func:`settings_service_for` 同一条：审计与日志**必须**是应用持有的那两个
    （现造一份 ⇒ 面板上做的改动不进 ``audit_ops``、不进日志面板）。

    路径由 ``state.paths`` 给：``config/publish.yaml`` 与 ``data/browser_profile``
    都从它派生 —— 测试用临时家目录时不会写到真仓库里。
    """
    return PublishAccountsService(
        state.paths,
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
        publisher_builder=state.publish_publishers,
    )


def prompt_service_for(state: AppState) -> PromptService:
    """装配提示词面板服务（**必须**带上覆盖目录 `data/prompts`）。

    不带覆盖目录的话，`write_override` 会直接报错 —— 那是故意的：没有覆盖目录的
    入口不该悄悄去改仓库里那份入库文件（`prompts verify` 逐字校验它）。
    """
    return PromptService(
        PromptLibrary.load(state.paths.prompts_dir, override_root=state.paths.prompts_override_dir),
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
    )


def publish_assist_service_for(state: AppState) -> PublishAssistService:
    """装配「人工过验证」服务（T6.4 · 真机 2026-09-23）。

    与 :func:`publish_accounts_service_for` 同一条：审计与日志**必须**是应用持有的
    那两个 —— 现造一份的话，面板上点的那一下不进 ``audit_ops``、不进日志面板，
    而这条动作恰恰是"人做了什么决定"里最该留痕的一类（它真的往平台上发东西）。

    发布器装配器同样从 ``state.publish_publishers`` 来：测试注入假件之后，
    这个端点不会去起真浏览器。
    """
    return PublishAssistService(
        state.paths,
        connection=state.connections.get(),
        audit=AuditRepo(state.connections.get()),
        log=state.logs.append,
        publisher_builder=state.publish_publishers,
    )


def publish_cover_service_for(state: AppState) -> PublishService:
    """装配「出封面」服务（T5.1 追加 · §06.3）。

    与 CLI 的 ``_build_publish_service`` 同一条，**但合成配置的来源不同**：这里读的是
    ``state.outputs``（热重载仓库）—— 面板刚在「合成配置」里改完贴图，发布页点出封面
    就该用**新改的那一份**。CLI 那边没有这个仓库，所以现读文件。

    每次现造（与 :func:`topic_service_for` 同一条）：网关持有熔断器与预算状态，
    而 ``sqlite3.Connection`` 是线程亲和的。
    """
    connection = state.connections.get()
    loaded = load_config(state.paths)
    prompts = PromptLibrary.load(state.paths.prompts_dir, override_root=state.paths.prompts_override_dir)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        config_provider=llm_config_provider(state.paths),
        paths=state.paths,
        log=_gateway_log_sink(state.logs),
        secrets=state.secrets.lookup,
    )
    return PublishService(
        connection,
        paths=state.paths,
        publish=loaded.bundle.publish,
        persona=active_persona(),
        outputs=state.outputs.current().config,
        cover_agent=CoverAgent(gateway, prompts),
    )


def topic_service_for(state: AppState) -> TopicService:
    """装配选题服务（配置 → 提示词 → 网关 → 三个 Agent）。

    与 CLI 的 ``_build_topic_service`` 同一手法：**装配是入口的责任**，
    服务层只认 Protocol。这里不缓存实例：网关持有熔断器与预算状态，而
    ``sqlite3.Connection`` 是线程亲和的 —— 每次现造最省心，代价只是几个包装对象。
    """
    connection = state.connections.get()
    loaded = load_config(state.paths)
    prompts = PromptLibrary.load(state.paths.prompts_dir, override_root=state.paths.prompts_override_dir)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        config_provider=llm_config_provider(state.paths),
        paths=state.paths,
        log=_gateway_log_sink(state.logs),
        secrets=state.secrets.lookup,
    )
    return TopicService(
        connection,
        planner=PlannerAgent(gateway, prompts),
        ideator=IdeatorAgent(gateway, prompts),
        classifier=FeedbackClassifierAgent(gateway, prompts),
        scout=NewsScoutAgent(gateway, prompts),
        # 走**模块属性**而不是 import 进来的那个名字：测试把 `news_service.fetch_news`
        # 换成假件即可，不必连网关一起换。
        news_fetcher=news_service.fetch_news,
        paths=state.paths,
        log=state.logs.append,
        include_auto_feedback=loaded.bundle.llm.planner.include_auto_feedback,
    )


def script_service_for(state: AppState, *, with_agents: bool = True) -> ScriptService:
    """装配写稿服务。

    ``with_agents=False`` ⇒ **不读配置、不建网关**：勾选入队（``enqueue``）是纯业务
    规则，不该因为"LLM Key 还没配"就点不动（T4.3 裁定 133）。真跑写稿时才需要
    Director/Writer —— 那时缺配置会在 ``_require_agents`` 处报一条能看懂的错。
    """
    connection = state.connections.get()
    if not with_agents:
        return ScriptService(connection, paths=state.paths, log=state.logs.append)
    loaded = load_config(state.paths)
    prompts = PromptLibrary.load(state.paths.prompts_dir, override_root=state.paths.prompts_override_dir)
    gateway = build_gateway(
        connection=connection,
        llm=loaded.bundle.llm,
        config_provider=llm_config_provider(state.paths),
        paths=state.paths,
        log=_gateway_log_sink(state.logs),
        secrets=state.secrets.lookup,
    )
    return ScriptService(
        connection,
        director=DirectorAgent(gateway, prompts),
        writer=WriterAgent(gateway, prompts),
        outliner=OutlinerAgent(gateway, prompts),
        paths=state.paths,
        log=state.logs.append,
    )


def _gateway_log_sink(log: LogService) -> LogSink:
    """``LogService.append`` → 网关的日志出口。

    为什么要一层瘦适配：``agents.gateway.LogSink`` 声明返回 ``None``，而
    ``append`` 返回落库的行 —— 直接把方法当回调传，mypy 会因为返回类型不一致拒绝
    （与 ``cli._gateway_log_sink`` 同一条理由；两侧都只丢返回值，语义无损失）。
    """

    def sink(
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        log.append(level=level, source=source, message=message, task_id=task_id, payload=payload)

    return sink


def task_snapshot_sql() -> str:
    """任务快照的 SQL（契约测试引用，避免"文档里一套、代码里另一套"）。"""
    return f"SELECT {_TASK_COLUMNS} FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?"


def _tasks_provider(connections: ThreadLocalConnections) -> SnapshotFn:
    def provider(task_ids: frozenset[str] | None) -> Mapping[str, Any]:
        rows = connections.get().execute(task_snapshot_sql(), (TASK_SNAPSHOT_ROWS,)).fetchall()
        tasks = [dict(row) for row in rows]
        if task_ids is not None:
            tasks = [task for task in tasks if task.get("id") in task_ids]
        return {"tasks": tasks, "limit": TASK_SNAPSHOT_ROWS}

    return provider


def _topics_provider(connections: ThreadLocalConnections) -> SnapshotFn:
    """选题池快照（§04.4.1 的 `topics` 通道 · T4.3 补上）。

    为什么值得在握手时就给：面板一打开要同时画"方向卡片 + 瀑布流"，而这两块
    分属两张表。让前端先收快照再补一次 REST，会出现"卡片画好了、瀑布流空着"
    的一帧 —— 而那一帧恰好是用户第一眼看到的。
    """

    def provider(task_ids: frozenset[str] | None) -> Mapping[str, Any]:
        del task_ids  # 选题池与"看哪几个任务"无关
        connection: sqlite3.Connection = connections.get()
        directions = DirectionRepo(connection)
        topics = TopicRepo(connection)
        batch_id = directions.latest_batch_id()
        return {
            "batch_id": batch_id,
            "directions": [
                _direction_snapshot(row) for row in (directions.list_batch(batch_id) if batch_id else [])
            ],
            "topics": [_topic_snapshot(row) for row in topics.list_pool(limit=TOPIC_SNAPSHOT_ROWS)],
            "counts": topics.count_by_status(),
            "limit": TOPIC_SNAPSHOT_ROWS,
        }

    return provider


def _direction_snapshot(row: DirectionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "seq": row.seq,
        "title": row.title,
        "rationale": row.rationale,
        "priority": row.priority,
        "risk_flags": list(row.risk_flags),
        "status": row.status,
    }


def _topic_snapshot(row: TopicRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "direction_id": row.direction_id,
        "seq": row.seq,
        "title": row.title,
        "hook_type": row.hook_type,
        "angle": row.angle,
        "exec_feasible": row.exec_feasible,
        "score": row.score,
        "reason": row.reason,
        "status": row.status,
        "task_id": row.task_id,
        "selected_by": row.selected_by,
    }


def _metrics_provider(metrics: MetricsService) -> SnapshotFn:
    """资源占用快照（§04.4.1 的 `metrics` 通道 · T4.2 补上）。

    为什么值得在握手时就给：采样是 5s 一拍，而面板可能在两拍之间打开 ——
    没有快照的话，用户第一眼看到的是"CPU - / 内存 - / 磁盘 -"，
    而这几块恰恰是"我现在能不能开跑"的判据。给最近一拍（并带上
    `resources_age_sec` 说明它有多旧）比给空的诚实得多。

    还没采过 ⇒ `metrics: null`（**不**发一个全 0 的假快照：0% CPU 与"没采到"
    在面板上是两件事，前者会让人以为机器闲着）。
    """

    def provider(task_ids: frozenset[str] | None) -> Mapping[str, Any]:
        del task_ids  # 资源与"看哪几个任务"无关
        snapshot = metrics.last
        return {
            "metrics": None if snapshot is None else snapshot.to_dict(),
            "interval_sec": METRICS_SAMPLE_INTERVAL_SEC,
        }

    return provider


def _pools_provider(connections: ThreadLocalConnections) -> SnapshotFn:
    def provider(task_ids: frozenset[str] | None) -> Mapping[str, Any]:
        del task_ids  # 池状态与任务无关
        connection: sqlite3.Connection = connections.get()
        store = JobStore(connection)
        beats = HeartbeatStore(connection)
        pools: dict[str, Any] = {}
        for name in POOL_NAMES:
            try:
                pools[name] = store.stats(pool=name).to_dict()
            except Exception as exc:  # 池配置缺失 / 表结构漂移都不该让握手失败
                logger.warning("app.pool_snapshot_failed", pool=name, error=str(exc))
                pools[name] = {"pool": name, "error": str(exc)}
        return {"pools": pools, "workers": [asdict(beat) for beat in beats.list_workers()]}

    return provider
