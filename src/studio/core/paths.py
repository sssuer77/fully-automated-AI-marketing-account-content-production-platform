"""路径唯一真相（§02.2 目录树 + §2.4 命名约定）。

任何模块**禁止**自行拼路径：一律 ``StudioPaths.from_env().xxx``。
这样"换机 / 换盘 / 跑测试用临时目录"只需改一处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from studio.core.clock import file_stamp

__all__ = [
    "DEFAULT_DATA_DIRNAME",
    "StudioPaths",
    "data_relative",
    "is_on_system_drive",
    "system_drive",
]

DEFAULT_DATA_DIRNAME: Final[str] = "data"


def system_drive() -> str:
    """Windows 系统盘盘符（形如 ``C:``），非 Windows 回退 ``C:``。"""
    # Windows 的环境变量字面量就是 SystemDrive（非全大写），SIM112 在此为误报
    return os.environ.get("SystemDrive", "C:").rstrip("\\/")  # noqa: SIM112


def is_on_system_drive(path: Path | str) -> bool:
    """判断路径是否落在系统盘（R1：C 盘可用空间常年 < 5 GB，写爆即全站停摆）。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return resolved.drive.upper() == system_drive().upper()


def data_relative(path: Path | str, data_dir: Path) -> str:
    """把绝对路径写成**相对 ``data/`` 的 posix 路径**（§04.2.7 / §03.3.11 的写法）。

    为什么不留绝对路径：``timeline.json`` 与 ``artifacts.path`` 是**产物清单**，
    它们的读者是"另一台机器上的面板 / 备份还原之后的下一次运行"。绝对路径会把
    盘符那一截焊死在里面，``data/`` 一搬家（换盘符、换机器、从备份还原）
    整份清单就指着一堆不存在的文件 —— 清单指错地方比没有清单更坏。

    落在 ``data/`` 之外（调用方给错了）⇒ 退回绝对路径而不是抛：写一个"看得见但
    搬不动"的路径，好过在收尾阶段把整条片子拦下来。
    """
    target = Path(path)
    try:
        return target.resolve().relative_to(Path(data_dir).resolve()).as_posix()
    except ValueError:
        return target.resolve().as_posix()


@dataclass(frozen=True, slots=True)
class StudioPaths:
    """全站目录契约。所有字段均为绝对路径。"""

    home: Path
    data_dir: Path
    # ── 可选覆盖（来自 config/app.yaml: paths.*；T1.2）──────────
    # 留 None ⇒ 用 data/ 下的默认值。非 None 时必须已由 config 层校验过
    # （仓库内 + 非系统盘），见 core/config.py::_guard_paths。
    work_dir_override: Path | None = None
    output_dir_override: Path | None = None
    cache_dir_override: Path | None = None
    logs_dir_override: Path | None = None

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StudioPaths:
        """从环境变量解析（``STUDIO_HOME`` / ``STUDIO_DATA_DIR``）。

        未设置时以本文件所在位置回溯三层推断仓库根，保证"零配置也能起"。
        """
        source = env if env is not None else dict(os.environ)
        home_raw = source.get("STUDIO_HOME")
        home = Path(home_raw).resolve() if home_raw else _infer_home()
        data_raw = source.get("STUDIO_DATA_DIR")
        data_dir = Path(data_raw).resolve() if data_raw else home / DEFAULT_DATA_DIRNAME
        return cls(home=home, data_dir=data_dir)

    # ── 输入源（§2.3：完全按原文）──────────────────────────
    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def persona_file(self) -> Path:
        """当前激活人物（人工可编辑，热重载，见 core/persona_store.py）。"""
        return self.config_dir / "persona.yaml"

    @property
    def persona_library_dir(self) -> Path:
        """人物库（多人物备选，`studio persona use <id>` 一键切换）。"""
        return self.config_dir / "personas"

    @property
    def prompts_dir(self) -> Path:
        return self.home / "prompts"

    @property
    def glossary_file(self) -> Path:
        """读音纠正词表（T2.5 · §04.3.6）：`prompts/shared/glossary.yaml`。

        **热重载**：改它不需要重启 TTS 服务（`tts/text_normalize.py` 按 mtime 重载）。
        """
        return self.prompts_dir / "shared" / "glossary.yaml"

    @property
    def schemas_dir(self) -> Path:
        return self.home / "schemas"

    @property
    def templates_dir(self) -> Path:
        return self.home / "templates"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def docs_dir(self) -> Path:
        return self.home / "docs"

    @property
    def web_dist_dir(self) -> Path:
        """前端构建产物（``web/dist``）—— 生产里由 API 直接托管（T4.6）。

        为什么由 API 托管、不另起一个静态服务器：这台子是**单机单进程**的，多一个
        进程就多一份"它没起来 / 端口被占 / 忘了开"的失败面。`npm run build` 的产物
        是纯静态文件，挂上去就完事。
        """
        return self.home / "web" / "dist"

    # ── data 子目录 ────────────────────────────────────────
    @property
    def db_file(self) -> Path:
        return self.data_dir / "studio.db"

    @property
    def logs_dir(self) -> Path:
        return self.logs_dir_override or self.data_dir / "logs"

    @property
    def workers_dir(self) -> Path:
        """进程入口目录（``workers/run_*.py``，§02.2 · T1.12）。"""
        return self.home / "workers"

    def pid_file(self, service: str) -> Path:
        """服务 PID 台账（``data/logs/<service>.pid``）。

        **唯一真相**：Python 启动器与 ``ops/*.ps1`` 都按这条规则找进程，
        两处各写一遍"文件名怎么拼"迟早对不上（裁定 105）。
        """
        return self.logs_dir / f"{service}.pid"

    def stop_flag_file(self, service: str) -> Path:
        """优雅关停标志（``data/logs/<service>.stop``）。

        关停的**主通道**：worker 在 1s 脉冲 tick 里看到这个文件 ⇒ ``request_stop()``。
        与"命令从哪个控制台发出"完全无关（``GenerateConsoleCtrlEvent`` 只作用于
        同控制台的进程组，从另一个窗口发会**静默失效**）—— 裁定 102。
        """
        return self.logs_dir / f"{service}.stop"

    def service_log_file(self, service: str) -> Path:
        """服务 stdout/stderr 的归宿（``data/logs/<service>.log``，裁定 106）。"""
        return self.logs_dir / f"{service}.log"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def tmp_graphs_dir(self) -> Path:
        return self.tmp_dir / "graphs"

    @property
    def tmp_parts_dir(self) -> Path:
        return self.tmp_dir / "parts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def hot_dir(self) -> Path:
        return self.data_dir / "hot"

    @property
    def hot_archive_dir(self) -> Path:
        return self.hot_dir / "archive"

    @property
    def feedback_dir(self) -> Path:
        return self.data_dir / "feedback"

    @property
    def voice_src_dir(self) -> Path:
        return self.data_dir / "voice_src"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def mc_parkour_dir(self) -> Path:
        return self.assets_dir / "mc_parkour"

    @property
    def bgm_dir(self) -> Path:
        return self.assets_dir / "bgm"

    @property
    def thumb_dir(self) -> Path:
        """素材缩略图 ``data/assets/.thumbs/``（**机器生成**的派生物）。

        为什么不放在素材目录里：``data/assets/mc_parkour/`` 是**人往里丢文件**的地方，
        而扫盘约定是"只认 ``parkour_*.<视频扩展名>``，其余一律进 strays"。缩略图落进去
        会立刻变成几十条"不符合约定的文件"，用户看到的是自己没做过的报错。

        也不放 ``data/cache/``：GC 会清缓存，而缩略图是面板的一部分（清掉就是一片
        空白格子）。``data/assets/`` 在 GC 白名单里（§03.7.5），放这儿既不被清、
        又不会被扫盘看见。
        """
        return self.assets_dir / ".thumbs"

    def thumb_file(self, kind: str, asset_id: str) -> Path:
        """某个素材的缩略图路径（``data/assets/.thumbs/<kind>/<id>.jpg``）。"""
        return self.thumb_dir / kind / f"{asset_id}.jpg"

    @property
    def output_dir(self) -> Path:
        return self.output_dir_override or self.data_dir / "output"

    @property
    def topics_dir(self) -> Path:
        return self.output_dir / "topics"

    @property
    def voice_out_dir(self) -> Path:
        return self.output_dir / "voice"

    @property
    def videos_dir(self) -> Path:
        return self.output_dir / "videos"

    @property
    def covers_dir(self) -> Path:
        return self.output_dir / "covers"

    @property
    def work_dir(self) -> Path:
        return self.work_dir_override or self.data_dir / "work"

    @property
    def cache_dir(self) -> Path:
        return self.cache_dir_override or self.data_dir / "cache"

    @property
    def tts_cache_dir(self) -> Path:
        return self.cache_dir / "tts"

    @property
    def phash_cache_dir(self) -> Path:
        return self.cache_dir / "phash"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser_profile"

    # ── 任务级派生路径（§2.3：过程物按 task_id 隔离）────────
    def work_dir_for(self, task_id: str) -> Path:
        """任务过程物根目录 ``data/work/<task_id>/``。"""
        return self.work_dir / task_id

    def tts_work_dir_for(self, task_id: str) -> Path:
        """拼接人声与中间音频 ``data/work/<task_id>/tts/``。"""
        return self.work_dir_for(task_id) / "tts"

    def graphs_dir_for(self, task_id: str) -> Path:
        """滤镜图 ``data/work/<task_id>/graphs/``（永久保留，便于复现）。"""
        return self.work_dir_for(task_id) / "graphs"

    def final_dir_for(self, task_id: str) -> Path:
        """交付级中间产物 ``data/work/<task_id>/final/``（永久保留）。

        目前只有字幕。它**不在** ``data/output/videos/`` 里：那是"给人看的成片"，
        而 ``subtitle.ass`` 是"二次剪辑时重新烧一遍字幕"要用的源文件（§04.2.6），
        两者生命周期不同 —— 成片会被清理策略按天回收，这个不该。
        """
        return self.work_dir_for(task_id) / "final"

    def subtitle_ass(self, task_id: str) -> Path:
        """字幕 ``data/work/<task_id>/final/subtitle.ass``（§04.2.6 的落盘位）。"""
        return self.final_dir_for(task_id) / "subtitle.ass"

    def scenes_dir_for(self, task_id: str) -> Path:
        """场景中间产物（**二期**；一期单遍合成不产出）。"""
        return self.work_dir_for(task_id) / "scenes"

    def publish_evidence_dir(self, task_id: str, platform: str) -> Path:
        """发布取证 ``data/work/<task_id>/publish/<platform>/``（T5.2）。

        为什么按平台再分一层：同一条片子会分发到多个平台（§06.2.4），
        而"抖音失败的那张截图"和"快手失败的那张截图"长得几乎一样 ——
        混在一个目录里，排障时要靠文件名猜。
        """
        return self.work_dir_for(task_id) / "publish" / platform

    def voice_dir_for(self, task_id: str) -> Path:
        """交付级逐句配音 ``data/output/voice/<task_id>/``。"""
        return self.voice_out_dir / task_id

    def sentence_wav(self, task_id: str, seq: int) -> Path:
        """句子音频 ``s%03d.wav``（§2.4：3 位补零保证字典序）。"""
        return self.voice_dir_for(task_id) / f"s{seq:03d}.wav"

    def voice_master(self, task_id: str) -> Path:
        """拼接人声 ``voice_master.wav``（24h TTL）。"""
        return self.tts_work_dir_for(task_id) / "voice_master.wav"

    def script_json(self, task_id: str) -> Path:
        return self.work_dir_for(task_id) / "script.json"

    def timeline_json(self, task_id: str) -> Path:
        return self.work_dir_for(task_id) / "timeline.json"

    def ir_json(self, task_id: str) -> Path:
        return self.work_dir_for(task_id) / "ir.json"

    def manifest_json(self, task_id: str) -> Path:
        return self.work_dir_for(task_id) / "manifest.json"

    def final_video(self, task_id: str, *, degraded: bool = False, stamp: str | None = None) -> Path:
        """成片 ``{yyyymmdd-HHMMSS}_{task_id}_final[_720p].mp4``（§2.4）。"""
        suffix = "_final_720p.mp4" if degraded else "_final.mp4"
        return self.videos_dir / f"{stamp or file_stamp()}_{task_id}{suffix}"

    def cover_image(self, task_id: str, *, stamp: str | None = None) -> Path:
        """封面 ``{yyyymmdd-HHMMSS}_{task_id}_cover.jpg``（§2.4）。"""
        return self.covers_dir / f"{stamp or file_stamp()}_{task_id}_cover.jpg"

    def topic_card(self, task_id: str) -> Path:
        """选题卡 ``data/output/topics/<task_id>.json``。"""
        return self.topics_dir / f"{task_id}.json"

    # ── 目录预建 ────────────────────────────────────────────
    def runtime_dirs(self) -> tuple[Path, ...]:
        """运行期必须存在的目录（``ensure_runtime_dirs`` 依据）。"""
        return (
            self.data_dir,
            self.logs_dir,
            self.tmp_dir,
            self.tmp_graphs_dir,
            self.tmp_parts_dir,
            self.backups_dir,
            self.hot_dir,
            self.hot_archive_dir,
            self.feedback_dir,
            self.voice_src_dir,
            self.assets_dir,
            self.mc_parkour_dir,
            self.bgm_dir,
            self.output_dir,
            self.topics_dir,
            self.voice_out_dir,
            self.videos_dir,
            self.covers_dir,
            self.work_dir,
            self.cache_dir,
            self.tts_cache_dir,
            self.phash_cache_dir,
        )

    def ensure_runtime_dirs(self) -> list[Path]:
        """幂等创建运行期目录，返回本次**新建**的目录列表。"""
        created: list[Path] = []
        for directory in self.runtime_dirs():
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
        return created


def _infer_home() -> Path:
    """``src/studio/core/paths.py`` ⇒ 回溯三层得到仓库根。"""
    return Path(__file__).resolve().parents[3]
