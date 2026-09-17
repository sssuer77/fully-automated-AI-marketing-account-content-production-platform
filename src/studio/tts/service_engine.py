"""把常驻推理服务接到引擎缝上（T2.3 薄片 · §04.3.5）。

这根管子为什么现在才接
----------------------
T2.2 把模型跑在了 ``127.0.0.1:8788`` 上，可**没有任何业务代码调它**：配音池装配的
还是 :class:`~studio.tts.sentence.SapiEngine`，于是成片里的人声一直是系统语音包 ——
模型能跑，片子里的声音却一个字都没变。这一层就是那根管子：一个
:class:`~studio.tts.sentence.SentenceEngine` 实现，把一句话 POST 给常驻服务，
拿回一段 WAV。

判据只此一份（:func:`active_resident`）
--------------------------------------
「服务能不能用」= 连得上 **且** ``/health`` 说 ``ready`` **且** 至少有一个可克隆
音色。三条缺一就退回 SAPI（今天的默认档）：宁可这一遍是系统音，也不要整段配音
哑掉（§1.7 降级模式）。

它必须是一个**共用函数**，而不是池里那句 ``try: 服务 / except: SAPI`` —— 因为同一个
判据有两处要用：配音池装配（决定用哪个引擎）与
:func:`~studio.services.voice_service.speakable_voices`（决定面板上哪个音色真的念得
出来）。两处各写一遍，就会出现「面板说 bigbear 能念、池子却把它交给 SAPI」——
那正是陷阱 #154 的形状：选了个音色、成片没人声，而库里写着换音色成功。

引擎名与版本**从服务自己报的**来
--------------------------------
``/health`` 里有 ``engine`` 与 ``revision``，它们进缓存键（``tts_cache_key``）。
客户端自己写死一个 ``cosyvoice2`` 的话，服务换了引擎 / 换了权重版本，旧缓存会
被当成新引擎的产物复用 —— 复用的是**别人的嗓子**。所以这里不写死，照抄服务的自述；
连不上时那两个字面量只是"还没问过服务"的占位，不会进任何键。

为什么 ``out_path`` 由调用方给、而不是让服务按 ``task_id + seq`` 推
--------------------------------------------------------------------
服务的默认落点是 ``data/work/<task_id>/tts/s00N.wav``，而配音池的契约落点是
``data/output/voice/<task_id>/s00N.wav``（§04.3.3 不变量 1）。两者**不是同一个目录**：
后者是交付级中间产物（拼接与重配都要读它），前者是过程物。所以这里把池算好的路径
转成相对 ``data/`` 的 POSIX 路径显式传过去 —— 服务只允许写 ``data/`` 以内，越界它会拒
（``CONFIG_PATH_OUT_OF_BOUNDS``）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from studio.core.config import TtsServerConfig, load_tts_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths

__all__ = [
    "FALLBACK_ENGINE_NAME",
    "MAX_SPEED",
    "MIN_SPEED",
    "PROBE_TIMEOUT_SEC",
    "SYNTH_TIMEOUT_SLACK_SEC",
    "ResidentEngine",
    "ResidentStatus",
    "active_resident",
    "base_url_for",
    "probe_resident",
    "resident_status",
    "speed_for_rate",
    "synth_timeout_for",
]

logger = get_logger("studio.tts.service_engine")

#: 问一次 ``/health`` 的超时（秒）。**故意很短**：面板每打开一次都要问，
#: 而"服务没起来"是最常见的情形 —— 回环上被拒是瞬时的，这里防的是端口被
#: 防火墙静默丢包（那种情况会一直等到超时）。
PROBE_TIMEOUT_SEC: Final[float] = 1.0

#: 合成请求比服务自己的 ``request_timeout_sec`` 多给的时间（秒）。
#: 必须**客户端更宽**：服务超时会回一个带错误码的响应（``TTS_ENGINE_DOWN`` 之类），
#: 而客户端先超时只会得到一个"连接断了"，把服务想说的话吞掉。
SYNTH_TIMEOUT_SLACK_SEC: Final[float] = 5.0

#: ``/health`` 里没有 ``engine`` 字段时的占位（只在"问过了但对面没报"时出现）
FALLBACK_ENGINE_NAME: Final[str] = "cosyvoice2"

#: ``speed`` 的上下界（与服务端 ``SynthRequest.speed`` 的 Field 约束一致）
MIN_SPEED: Final[float] = 0.5
MAX_SPEED: Final[float] = 2.0


class _Response(Protocol):
    """``httpx.Response`` 里我们真正用到的部分（测试拿假件顶它）。"""

    status_code: int

    def json(self) -> Any: ...


class _Client(Protocol):
    """``httpx.Client`` 里我们真正用到的部分。"""

    def get(self, url: str, *, timeout: float) -> _Response: ...

    def post(self, url: str, *, json: Any, timeout: float) -> _Response: ...


@dataclass(frozen=True, slots=True)
class ResidentStatus:
    """常驻服务的一次体检结论（探不到就是 ``None``，不是这个对象）。"""

    base_url: str
    engine: str
    revision: str
    ready: bool
    device: str
    model_state: str
    sample_rate: int
    voices: tuple[str, ...]
    detail: str | None = None

    @property
    def usable(self) -> bool:
        """能不能拿它念句子：模型在显存里 **且** 至少有一个可克隆音色。"""
        return self.ready and bool(self.voices)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "engine": self.engine,
            "revision": self.revision,
            "ready": self.ready,
            "device": self.device,
            "model_state": self.model_state,
            "sample_rate": self.sample_rate,
            "voices": list(self.voices),
            "usable": self.usable,
            "detail": self.detail,
        }


def base_url_for(config: TtsServerConfig) -> str:
    """``config/tts.yaml`` 的 ``server`` 段 ⇒ 基址。"""
    return f"http://{config.host}:{config.port}"


def speed_for_rate(rate: int) -> float:
    """SAPI 的 ``rate``（−10…10）⇒ 服务的 ``speed``（0.5…2.0）。

    :func:`~studio.tts.sentence.sapi_rate_for` 的**逆映射**。为什么要在引擎缝上转
    一次：``SentenceEngine.synthesize`` 收的是 ``rate``（缝是先给 SAPI 定的），
    而服务收的是 ``speed``（0.5–2.0）。两个量纲不同 —— 把 ``rate=1`` 直接当
    ``speed`` 传过去，得到的是"语速 0.5 倍"，慢到没法听，而且**不报错**。
    """
    return max(MIN_SPEED, min(MAX_SPEED, 1.0 + rate / 10))


@contextmanager
def _client_for(base_url: str, client: _Client | None) -> Iterator[_Client]:
    """给一段连接：注入的假件原样借出，没注入就自己开一条、用完关掉。

    写成上下文管理器而不是"自己建的记得 close"：上面那两处（探测 / 合成）都要走
    同一条路，而"某一条分支忘了关"的表现是连接慢慢漏光 —— 那是几天后才发作的。
    """
    if client is not None:
        yield client
        return
    with httpx.Client(base_url=base_url) as owned:
        yield owned


def _json_or_none(response: _Response) -> dict[str, Any] | None:
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:  # 对面回的不是 JSON（代理页 / 半截响应）
        return None
    return payload if isinstance(payload, dict) else None


def probe_resident(
    config: TtsServerConfig,
    *,
    client: _Client | None = None,
    timeout: float = PROBE_TIMEOUT_SEC,
) -> ResidentStatus | None:
    """问一次常驻服务；**连不上 ⇒ ``None``**（连得上但没就绪 ⇒ 一个 ``ready=False`` 的状态）。

    两种"不行"要分开：``None`` 是"这个服务没在跑"，``ready=False`` 是"在跑，但模型
    有问题"（比如子环境不对，每句都报 ``no module named torch``）。前者是没启动，
    后者要去看 ``data/logs/tts.log`` —— 混成一种，排障时第一步就分岔了。
    """
    base_url = base_url_for(config)
    try:
        with _client_for(base_url, client) as http:
            health = _json_or_none(http.get("/health", timeout=timeout))
            if health is None:
                logger.info("tts.resident_bad_health", base_url=base_url)
                return None
            voices = _usable_voices(http, timeout=timeout, base_url=base_url)
    except httpx.HTTPError as exc:
        logger.info("tts.resident_unreachable", base_url=base_url, error=f"{type(exc).__name__}: {exc}")
        return None

    status = ResidentStatus(
        base_url=base_url,
        engine=str(health.get("engine") or FALLBACK_ENGINE_NAME),
        revision=str(health.get("revision") or ""),
        ready=bool(health.get("ready")),
        device=str(health.get("device") or "unavailable"),
        model_state=str(health.get("model_state") or "unknown"),
        sample_rate=int(health.get("sample_rate") or 0),
        voices=voices,
        detail=health.get("detail"),
    )
    if not status.usable:
        logger.warning("tts.resident_not_usable", **status.to_dict())
    return status


def synth_timeout_for(paths: StudioPaths, *, fallback_sec: float = 60.0) -> float:
    """合成请求该等多久：服务自己的 ``request_timeout_sec`` + :data:`SYNTH_TIMEOUT_SLACK_SEC`。

    读不出配置时用 ``fallback_sec``：这一层不该因为"配置文件坏了"而拒绝合成 ——
    那件事由 ``doctor`` 去报，配音该做的是"尽量把这一句念出来"。
    """
    try:
        config = load_tts_config(paths)
    except StudioError:
        return fallback_sec + SYNTH_TIMEOUT_SLACK_SEC
    return float(config.server.request_timeout_sec) + SYNTH_TIMEOUT_SLACK_SEC


def _usable_voices(http: _Client, *, timeout: float, base_url: str) -> tuple[str, ...]:
    """``/voices`` 里**这台引擎念得出来**的那些 id（读不到就当没有，不抛）。"""
    try:
        listing = _json_or_none(http.get("/voices", timeout=timeout))
    except httpx.HTTPError as exc:
        logger.info("tts.resident_voices_failed", base_url=base_url, error=f"{type(exc).__name__}: {exc}")
        return ()
    if listing is None:
        return ()
    return tuple(
        str(item["id"])
        for item in listing.get("voices") or []
        if isinstance(item, dict) and item.get("usable") and item.get("id")
    )


def resident_status(
    paths: StudioPaths | None,
    *,
    client: _Client | None = None,
) -> ResidentStatus | None:
    """按 ``paths`` 问一次常驻服务，**把原始结论原样带回**（不替调用方下结论）。

    与 :func:`active_resident` 的差别只在最后一步：那个只肯回"能用"的状态，这个连
    "起着但没就绪"也回。面板要的正是后者 —— 「为什么这次不是那个音色」的答案就在
    ``ready`` / ``detail`` 里；只拿到 ``None`` 的话，说得出的话只有"服务没在跑"。

    ``paths is None`` ⇒ ``None``：调用方没给环境（比如只拿到一个 ``connection`` 的
    旧调用点），就按**最保守的那台引擎**算，而不是去猜。
    """
    if paths is None:
        return None
    try:
        config = load_tts_config(paths)
    except StudioError as exc:  # 配置读不出来 ⇒ 这不是配音该炸的地方
        logger.warning("tts.resident_config_unreadable", error=str(exc))
        return None
    return probe_resident(config.server, client=client)


def active_resident(
    paths: StudioPaths | None,
    *,
    client: _Client | None = None,
) -> ResidentStatus | None:
    """**共用的那一份判据**：服务可用 ⇒ 状态，否则 ``None``（调用方退回 SAPI）。

    ``paths is None`` ⇒ 直接 ``None``：调用方没给环境（比如只拿到一个 ``connection``
    的旧调用点），就按**最保守的那台引擎**算，而不是去猜。这条规则让"忘了传 paths"
    的表现是"没有用上新引擎"（看得见、能查），而不是"用了但用了错的"。
    """
    status = resident_status(paths, client=client)
    if status is None or not status.usable:
        return None
    logger.info("tts.resident_active", base_url=status.base_url, voices=list(status.voices))
    return status


class ResidentEngine:
    """:class:`~studio.tts.sentence.SentenceEngine` 的常驻服务实现。

    :param paths: 用来把产物路径转成"相对 ``data/``"（服务只肯写 data/ 以内）
    :param status: 装配期探到的服务状态（引擎名 / 版本 / 采样率从它来）
    :param timeout_sec: 缺省按 ``config/tts.yaml`` 现算（见 :func:`synth_timeout_for`）
    :param client: 注入点（测试用假件；缺省自己建一条连接）
    """

    name: str
    revision: str

    def __init__(
        self,
        *,
        paths: StudioPaths,
        status: ResidentStatus,
        timeout_sec: float | None = None,
        client: _Client | None = None,
    ) -> None:
        self._paths = paths
        self._base_url = status.base_url
        self._timeout = synth_timeout_for(paths) if timeout_sec is None else timeout_sec
        self._client = client
        self.name = status.engine
        self.revision = status.revision
        self.sample_rate = status.sample_rate

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        """念一句：POST ``/synth``，服务**直接写到** ``out_path``。

        :raises StudioError: 音色没给 / 服务连不上 / 服务回错 / 服务说成功但盘上没有
        """
        if voice is None:
            raise StudioError(
                "没有给音色，常驻服务不知道该用谁的声音",
                code=ErrorCode.TTS_VOICE_MISSING,
                context={"engine": self.name, "out_path": out_path.as_posix()},
                remediation=(
                    "这一句的 speaker 没解析出音色（voice_map 里没有它）⇒ "
                    "检查 tasks.payload_json.voice_map，或在配音面板上给这个角色选一个音色"
                ),
            )
        payload = {
            "text": text,
            "voice_id": voice,
            "out_path": self._relative_to_data(out_path),
            "speed": speed_for_rate(rate),
        }
        try:
            with _client_for(self._base_url, self._client) as http:
                response = http.post("/synth", json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise StudioError(
                f"常驻推理服务连不上（{self._base_url}）：{type(exc).__name__}: {exc}",
                code=ErrorCode.TTS_ENGINE_DOWN,
                context={"base_url": self._base_url, "voice": voice, "text": text[:80]},
                remediation=(
                    "`studio service status` 看 tts 进程在不在；"
                    "没起来就 `启动.bat`（或 `studio service start tts`）"
                ),
            ) from exc

        if response.status_code != 200:
            raise _from_error_response(response, base_url=self._base_url, voice=voice)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise StudioError(
                f"服务回了成功，但产物不在盘上：{out_path}",
                code=ErrorCode.TTS_SENTENCE_FAILED,
                context={"out_path": out_path.as_posix(), "base_url": self._base_url},
                remediation="看 data/logs/tts.log：多半是服务写了别的地方（out_path 越界会被拒，这里是没写）",
            )

    def _relative_to_data(self, out_path: Path) -> str:
        """``data/output/voice/<t>/s001.wav`` ⇒ ``output/voice/<t>/s001.wav``。"""
        data_root = self._paths.data_dir.resolve()
        try:
            relative = out_path.resolve().relative_to(data_root)
        except ValueError as exc:
            raise StudioError(
                f"产物路径不在 data/ 下，常驻服务写不了：{out_path}",
                code=ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
                context={"out_path": out_path.as_posix(), "data_dir": str(data_root)},
                remediation="配音的落点必须是 data/ 以内（§04.3.3 不变量 1）",
            ) from exc
        return relative.as_posix()


def _from_error_response(response: _Response, *, base_url: str, voice: str) -> StudioError:
    """把服务回的错误体还原成一个 ``StudioError``，**保住原来的错误码**。

    保住码是重点：池的降级与"连续 N 次 ``TTS_OOM`` 就降并发"（§01.4.4）认的都是码。
    这里统一成 ``TTS_SENTENCE_FAILED`` 的话，显存炸了会被当成"这一句念不出来" ——
    于是并发不会降，下一句接着炸。
    """
    payload = _safe_json(response)
    raw_code = str(payload.get("code") or "")
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        code = ErrorCode.TTS_SENTENCE_FAILED
    message = str(payload.get("message") or f"常驻服务回了 {response.status_code}")
    return StudioError(
        message,
        code=code,
        context={
            "base_url": base_url,
            "voice": voice,
            "http_status": response.status_code,
            "remote": payload,
        },
        remediation=str(payload.get("remediation") or "看 data/logs/tts.log（服务侧的同一次失败也在那里）"),
    )


def _safe_json(response: _Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
