"""总览台的资源采样与磁盘告警（T4.2 · §04.4.3 `metrics.tick` · §README.7 磁盘门禁）。

三件事，一份真相
----------------
① **采样**：CPU / 内存 / 显存 / 磁盘水位（`psutil` + `nvidia-smi`，与 `doctor` 同口径）；
② **判水位**：`free_D < disk_gate.free_d_min_gb` ⇒ `disk_low`（默认 15 GB）；
③ **告警去重**：只在 **状态迁移** 时写 `DISK_LOW`，不是每拍写一条。

为什么"去重"必须是状态机而不是节流
----------------------------------
`free_D < 15GB` 是**持续**成立的（人不清理就一直在下面），而采样是 5s 一拍。
每拍写一条 ⇒ 一小时 720 条 `DISK_LOW`；而 `system.alert` 在协议里
**永不合并、永不限流、永不丢弃**（§04.4.6）—— 于是前端被同一条消息刷屏，
真正的告警被淹掉，日志表也被刷爆。状态机只写两次：`ok → low`（告警）与
`low → ok`（恢复，走 `info` 日志，**不**升格为告警 —— 恢复不是告警）。

为什么采样器在 `services/` 而不是 `app/`
----------------------------------------
"怎么采、阈值怎么判"是业务口径，`app` 只负责"每 5s 拍一次 + 推给 Hub"。
分层的收益很实际：CLI 排障入口与 WebUI 用**同一份**数字，不会出现
"命令行说 12 GB、网页说 15 GB"这种查不完的悬案。

采样的成本
----------
`psutil.cpu_percent(interval=None)` **非阻塞**（返回"距上次调用"的均值，所以
第一拍是 `0.0` —— 这是 psutil 的既定行为，不是 bug）；`nvidia-smi` 一次约
80–150 ms，0.2 Hz 下可忽略（由 `app/metrics.py` 丢进线程池，不占事件循环）。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from studio.core.clock import format_iso, utc_now
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths, system_drive
from studio.core.proto import AlertCode
from studio.services.log_service import LogService

__all__ = [
    "GPU_QUERY_ARGS",
    "METRICS_SAMPLE_INTERVAL_SEC",
    "MetricsService",
    "ResourceSnapshot",
    "probe_resources",
]

logger = get_logger("studio.services.metrics")

#: 采样周期（§04.4.3 把 `metrics.tick` 的频率上限定为 0.2 Hz ⇒ 5s 一拍）
METRICS_SAMPLE_INTERVAL_SEC: Final[float] = 5.0

#: `nvidia-smi` 的查询字段（与 `doctor._check_gpu` 同口径，另加利用率）
GPU_QUERY_ARGS: Final[tuple[str, ...]] = (
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)

#: `nvidia-smi` 单次超时（它是探测，不是业务；卡住就如实报"不可用"）
GPU_TIMEOUT_SEC: Final[float] = 2.0

_MB: Final[float] = 1024.0 * 1024.0
_GB: Final[float] = 1024.0 * 1024.0 * 1024.0

#: 恢复通知的 `payload_json.code`（**不在** 8 个 `AlertCode` 里 ⇒ 只会是 `log.appended`，
#: 这正是想要的：恢复不是告警，不该占用"永不合并"的那条通道 · 裁定 141）
DISK_RECOVERED_CODE: Final[str] = "DISK_RECOVERED"


def _psutil() -> Any:
    """惰性导入 psutil（与 `doctor` 同手法：core 层不背运行期依赖）。"""
    import psutil  # noqa: PLC0415

    return psutil


def _gb(value: float | int) -> float:
    """字节 → GB（保留两位；面板只显示一位，第二位用于"刚过线"时看得清）。"""
    return round(float(value) / _GB, 2)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """一拍资源占用（`metrics.tick` 的数据体 · §04.4.3）。

    字段名刻意与 §04.4.3 的载荷表**逐字一致**（`cpu_pct` / `ram_mb` /
    `gpu_util` / `gpu_mem_mb` / `disk_free_gb`）：契约测试与前端都按那张表读，
    这里换个名字就等于把"文档里一套、代码里另一套"埋进最容易漂移的一层。
    """

    sampled_at: str
    cpu_pct: float
    ram_used_mb: int
    ram_total_mb: int
    ram_pct: float
    process_rss_mb: int
    disk_free_c_gb: float
    disk_free_d_gb: float
    disk_free_d_min_gb: float
    disk_drive: str
    disk_low: bool
    gpu_name: str | None = None
    gpu_util_pct: float | None = None
    gpu_mem_used_mb: int | None = None
    gpu_mem_total_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """`metrics.tick` 的 `data`（**不含** `kind` —— 那是发布方加的，裁定 140）。"""
        return {
            "sampled_at": self.sampled_at,
            "cpu_pct": self.cpu_pct,
            "ram_mb": self.ram_used_mb,
            "ram_total_mb": self.ram_total_mb,
            "ram_pct": self.ram_pct,
            "gpu_util": self.gpu_util_pct,
            "gpu_mem_mb": self.gpu_mem_used_mb,
            "gpu_mem_total_mb": self.gpu_mem_total_mb,
            "gpu_name": self.gpu_name,
            "disk_free_gb": self.disk_free_d_gb,
            "disk_free_c_gb": self.disk_free_c_gb,
            "disk_free_d_min_gb": self.disk_free_d_min_gb,
            "disk_drive": self.disk_drive,
            "disk_low": self.disk_low,
            "process_rss_mb": self.process_rss_mb,
            # 这两个要等 T2.7 / T3.4 才有真值。**如实给 None**，不编 0 ——
            # 面板上"0 fps"与"还没有渲染过"是两件事。
            "tts_rtf_avg": None,
            "render_fps": None,
        }


def _probe_gpu(*, timeout_sec: float = GPU_TIMEOUT_SEC) -> tuple[str, float, int, int] | None:
    """`nvidia-smi` 一次 ⇒ `(name, util_pct, mem_used_mb, mem_total_mb)`；不可用 ⇒ `None`。

    不可用**不是错误**：CPU 兜底是既定降级路径（§1.7），面板显示"显存不可用"
    比弹一个红叉有用得多。
    """
    try:
        completed = subprocess.run(  # 固定 argv，无 shell，无用户输入
            list(GPU_QUERY_ARGS),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("metrics.gpu_unavailable", error=str(exc))
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    cells = [cell.strip() for cell in completed.stdout.strip().splitlines()[0].split(",")]
    if len(cells) < 4:
        return None
    try:
        return (cells[0], float(cells[1]), int(float(cells[2])), int(float(cells[3])))
    except ValueError:
        logger.debug("metrics.gpu_unparsable", raw=completed.stdout.strip()[:120])
        return None


def probe_resources(
    *,
    paths: StudioPaths,
    free_c_min_gb: float,
    free_d_min_gb: float,
    gpu_timeout_sec: float = GPU_TIMEOUT_SEC,
) -> ResourceSnapshot:
    """采一拍（**阻塞**；调用方负责把它丢进线程池，见 `app/metrics.py`）。

    :raises StudioError: 磁盘探测本身失败（路径不存在 / 权限）。**不吞** ——
        "采不到水位"与"水位很低"必须分得开，否则面板会拿 `0` 当"磁盘空了"。
    """
    psutil = _psutil()
    drive_c = system_drive()
    drive_d = paths.data_dir.drive or drive_c
    try:
        free_c = float(psutil.disk_usage(f"{drive_c}\\").free)
        free_d = float(psutil.disk_usage(f"{drive_d}\\").free)
    except OSError as exc:
        raise StudioError(
            f"磁盘水位探测失败：{exc}",
            code=ErrorCode.DISK_LOW_D,
            context={"drive_c": drive_c, "drive_d": drive_d},
            remediation="确认数据盘已挂载且当前用户可读（`studio doctor` 的磁盘门禁会复检）",
        ) from exc

    memory = psutil.virtual_memory()
    process = psutil.Process()
    gpu = _probe_gpu(timeout_sec=gpu_timeout_sec)
    free_d_gb = _gb(free_d)
    return ResourceSnapshot(
        sampled_at=format_iso(utc_now()),
        cpu_pct=round(float(psutil.cpu_percent(interval=None)), 1),
        ram_used_mb=int(memory.used // _MB),
        ram_total_mb=int(memory.total // _MB),
        ram_pct=float(memory.percent),
        process_rss_mb=int(process.memory_info().rss // _MB),
        disk_free_c_gb=_gb(free_c),
        disk_free_d_gb=free_d_gb,
        disk_free_d_min_gb=float(free_d_min_gb),
        disk_drive=drive_d,
        disk_low=free_d_gb < float(free_d_min_gb),
        gpu_name=None if gpu is None else gpu[0],
        gpu_util_pct=None if gpu is None else gpu[1],
        gpu_mem_used_mb=None if gpu is None else gpu[2],
        gpu_mem_total_mb=None if gpu is None else gpu[3],
    )


class MetricsService:
    """采样 + 磁盘告警去重（**有状态**：`_disk_low` 就是那个状态机）。

    :param log: 日志出口（告警与恢复都从这里走）。
        这里要的是**整个** `LogService` 而不是 `LogSink` 那个可调用面：
        `alert()` 会把 `payload_json` 组装成 Hub 认得的告警形状，
        只拿一个 `append` 就只能把这套约定抄第二遍（而抄漏一次，告警就会
        以 `log.appended` 的身份混进 `logs` 通道 —— 那正是 §04.4.3 分流要防的）。
    :param probe: 采样实现（测试注入假件 ⇒ 不碰真机器）
    """

    def __init__(
        self,
        *,
        paths: StudioPaths,
        log: LogService,
        free_c_min_gb: float = 1.0,
        free_d_min_gb: float = 15.0,
        probe: Callable[..., ResourceSnapshot] = probe_resources,
    ) -> None:
        self._paths = paths
        self._log = log
        self._free_c_min_gb = float(free_c_min_gb)
        self._free_d_min_gb = float(free_d_min_gb)
        self._probe = probe
        self._last: ResourceSnapshot | None = None
        #: `None` = 还没采过。首拍**照常判**（"API 起来时磁盘已经满了"同样要留痕）；
        #: 去重靠的是"状态没变就不写"，不是"头一拍不写"。首拍**健康**则一个字都不写
        #: —— `None → ok` 不是恢复，没有"从哪恢复"这回事。
        self._disk_low: bool | None = None

    @property
    def last(self) -> ResourceSnapshot | None:
        """最近一拍（`metrics` 通道的握手快照就是它；没采过 ⇒ `None`）。"""
        return self._last

    @property
    def disk_low(self) -> bool | None:
        return self._disk_low

    def sample(self) -> ResourceSnapshot:
        """采一拍 + 按状态迁移落告警（**同步**：见 `probe_resources` 的说明）。"""
        snapshot = self._probe(
            paths=self._paths,
            free_c_min_gb=self._free_c_min_gb,
            free_d_min_gb=self._free_d_min_gb,
        )
        self._last = snapshot
        self._emit_disk_transition(snapshot)
        return snapshot

    def _emit_disk_transition(self, snapshot: ResourceSnapshot) -> None:
        previous = self._disk_low
        self._disk_low = snapshot.disk_low
        if previous == snapshot.disk_low:
            return
        if snapshot.disk_low:
            self._log.alert(
                AlertCode.DISK_LOW,
                message=(
                    f"磁盘水位告警：{snapshot.disk_drive} 可用 {snapshot.disk_free_d_gb:.2f} GB "
                    f"< 门禁 {snapshot.disk_free_d_min_gb:.2f} GB"
                ),
                hint="清理 data/ 下的中间产物（`studio gc run`）或迁移输出目录；"
                "低于门禁时 render/publish 池应暂停认领",
                pool="render",
            )
            return
        if previous is None:
            # 首拍就健康 ⇒ **没有**"恢复"这回事：`None` 是"还没采过"，不是"低水位"。
            # 少了这一条，每次启动都会凭空写一条 DISK_RECOVERED；它排在 tail 游标之后，
            # 于是**顶掉日志通道的第一帧** —— T1.7 那两条补发用例就是这么挂的（T4.2 实测）。
            return
        self._log.append(
            level="info",
            source="system",
            message=(
                f"磁盘水位恢复：{snapshot.disk_drive} 可用 {snapshot.disk_free_d_gb:.2f} GB "
                f"≥ 门禁 {snapshot.disk_free_d_min_gb:.2f} GB"
            ),
            payload={
                "code": DISK_RECOVERED_CODE,
                "disk_free_gb": snapshot.disk_free_d_gb,
                "disk_free_d_min_gb": snapshot.disk_free_d_min_gb,
                "disk_drive": snapshot.disk_drive,
            },
        )
