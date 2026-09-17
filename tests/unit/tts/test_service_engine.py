"""``tts.service_engine``（T2.3 薄片）—— 常驻服务接到引擎缝上的那根管子。

这一层不需要 GPU：它只管「发出去的请求对不对、回来的错怎么翻译」。真正的推理在
``tests/unit/tts/test_server.py`` 那一侧（FakeBackend 注入），两边的接缝就是这里的
HTTP 形状 —— 那边验 ``/synth`` 怎么干活，这边验"我们有没有按那个形状说话"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from studio.core.config import TtsServerConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.tts.sentence import sapi_rate_for
from studio.tts.service_engine import (
    MAX_SPEED,
    MIN_SPEED,
    SYNTH_TIMEOUT_SLACK_SEC,
    ResidentEngine,
    ResidentStatus,
    active_resident,
    base_url_for,
    probe_resident,
    speed_for_rate,
    synth_timeout_for,
)

BASE_URL = "http://127.0.0.1:8788"
REVISION = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"


# ── 假件 ────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    """按剧本回应的假客户端；**记下每一次请求**（断言发出去的东西对不对）。"""

    def __init__(
        self,
        *,
        health: Any = None,
        voices: Any = None,
        synth: Any = None,
        synth_status: int = 200,
        get_error: Exception | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self._health = health
        self._voices = voices
        self._synth = synth
        self._synth_status = synth_status
        self._get_error = get_error
        self._post_error = post_error

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.gets.append(url)
        if self._get_error is not None:
            raise self._get_error
        if url == "/health":
            return FakeResponse(200, self._health)
        return FakeResponse(200, self._voices)

    def post(self, url: str, *, json: Any, timeout: float) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self._post_error is not None:
            raise self._post_error
        return FakeResponse(self._synth_status, self._synth)


def health(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "ready": True,
        "device": "cuda",
        "model_state": "ready",
        "engine": "cosyvoice2",
        "revision": REVISION,
        "sample_rate": 24_000,
    }
    payload.update(overrides)
    return payload


def voices(*usable: str, unusable: tuple[str, ...] = ()) -> dict[str, Any]:
    entries = [{"id": name, "usable": True} for name in usable]
    entries += [{"id": name, "usable": False} for name in unusable]
    return {"ok": True, "voices": entries}


def status(**overrides: Any) -> ResidentStatus:
    payload: dict[str, Any] = {
        "base_url": BASE_URL,
        "engine": "cosyvoice2",
        "revision": REVISION,
        "ready": True,
        "device": "cuda",
        "model_state": "ready",
        "sample_rate": 24_000,
        "voices": ("bigbear", "littlebear"),
    }
    payload.update(overrides)
    return ResidentStatus(**payload)


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    home = tmp_path / "studio"
    data = home / "data"
    data.mkdir(parents=True)
    return StudioPaths(home=home, data_dir=data)


def write_tts_config(paths: StudioPaths, *, port: int = 8788) -> Path:
    """写一份最小的 ``config/tts.yaml``（与 service_manager 的夹具同一形状）。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    path = paths.config_dir / "tts.yaml"
    path.write_text(
        'schema_version: "1.0"\n'
        "model:\n"
        "  dir: D:/somewhere/models\n"
        "  revision: abcdef12\n"
        "server:\n"
        f"  port: {port}\n"
        "  request_timeout_sec: 7\n",
        encoding="utf-8",
    )
    return path


# ── 语速映射 ────────────────────────────────────────────────────────────


class TestSpeedMapping:
    def test_it_is_the_inverse_of_the_sapi_mapping(self) -> None:
        """``rate`` 与 ``speed`` 是两套量纲：把 ``rate=1`` 当 ``speed`` 传过去，
        得到的是"语速 0.5 倍"，慢到没法听，而且**不报错**。"""
        for speed in (0.5, 0.8, 1.0, 1.3, 1.7, 2.0):
            assert speed_for_rate(sapi_rate_for(speed)) == pytest.approx(speed, abs=0.05)

    def test_both_ends_are_clamped_into_the_service_contract(self) -> None:
        """服务端 ``SynthRequest.speed`` 是 0.5–2.0；越界的值会被它 422 掉。"""
        assert speed_for_rate(-10) == MIN_SPEED
        assert speed_for_rate(10) == MAX_SPEED
        assert speed_for_rate(999) == MAX_SPEED
        assert speed_for_rate(-999) == MIN_SPEED


# ── 探测 ────────────────────────────────────────────────────────────────


class TestProbe:
    def test_unreachable_is_none_not_a_fake_status(self) -> None:
        """连不上 ⇒ ``None``（"这个服务没在跑"），而不是一个字段全是默认值的状态。"""
        client = FakeClient(get_error=httpx.ConnectError("refused"))
        assert probe_resident(TtsServerConfig(), client=client) is None

    def test_timeout_is_also_none(self) -> None:
        client = FakeClient(get_error=httpx.ReadTimeout("slow"))
        assert probe_resident(TtsServerConfig(), client=client) is None

    def test_running_but_not_ready_is_reported_as_such(self) -> None:
        """在跑、但模型有问题（子环境不对 ⇒ 每句都报 no module named torch）——
        这与"没在跑"要分开：前者要看 ``data/logs/tts.log``。"""
        client = FakeClient(
            health=health(ready=False, device="unavailable", model_state="error", detail="no torch"),
            voices=voices(),
        )
        found = probe_resident(TtsServerConfig(), client=client)
        assert found is not None
        assert found.ready is False
        assert found.usable is False
        assert found.detail == "no torch"

    def test_only_usable_voices_count(self) -> None:
        client = FakeClient(
            health=health(),
            voices=voices("bigbear", "littlebear", unusable=("broken",)),
        )
        found = probe_resident(TtsServerConfig(), client=client)
        assert found is not None
        assert found.voices == ("bigbear", "littlebear")
        assert found.usable is True

    def test_ready_but_no_voice_is_not_usable(self) -> None:
        """模型在显存里、一个参考音都没有 ⇒ 还是念不出东西来。"""
        client = FakeClient(health=health(), voices=voices())
        found = probe_resident(TtsServerConfig(), client=client)
        assert found is not None
        assert found.ready is True
        assert found.usable is False

    def test_a_broken_voices_endpoint_does_not_take_the_health_down(self) -> None:
        """``/voices`` 挂了 ⇒ 音色为空（不可用），但 ``/health`` 那部分照报。"""

        class HalfBroken(FakeClient):
            def get(self, url: str, *, timeout: float) -> FakeResponse:
                if url == "/voices":
                    raise httpx.ReadTimeout("voices is slow")
                return super().get(url, timeout=timeout)

        found = probe_resident(TtsServerConfig(), client=HalfBroken(health=health()))
        assert found is not None
        assert found.ready is True
        assert found.voices == ()
        assert found.usable is False

    def test_non_json_health_is_treated_as_not_running(self) -> None:
        """对面回的不是 JSON（代理页 / 半截响应）⇒ 当成"没在跑"。
        ``httpx.Response.json()`` 在坏 JSON 上抛的是 ``ValueError`` 那一族。"""
        client = FakeClient(health=ValueError("not json"), voices=voices("bigbear"))
        assert probe_resident(TtsServerConfig(), client=client) is None

    def test_base_url_comes_from_the_config(self) -> None:
        assert base_url_for(TtsServerConfig(host="127.0.0.1", port=8799)) == "http://127.0.0.1:8799"


# ── 共用判据 ────────────────────────────────────────────────────────────


class TestActiveResident:
    def test_no_paths_means_no_probe(self) -> None:
        """调用方没给环境 ⇒ 按最保守的那台引擎算（**不去猜**）。

        这条规则让"忘了传 paths"的表现是"没用上新引擎"（看得见、能查），
        而不是"用了但用了错的"。
        """
        client = FakeClient(health=health(), voices=voices("bigbear"))
        assert active_resident(None, client=client) is None
        assert client.gets == []

    def test_unreadable_config_is_not_a_reason_to_blow_up(self, paths: StudioPaths) -> None:
        """``config/tts.yaml`` 不在 ⇒ 退回 SAPI，而不是让配音整个起不来。"""
        assert active_resident(paths, client=FakeClient(health=health())) is None

    def test_service_down_is_none(self, paths: StudioPaths) -> None:
        write_tts_config(paths)
        client = FakeClient(get_error=httpx.ConnectError("refused"))
        assert active_resident(paths, client=client) is None

    def test_usable_service_is_returned(self, paths: StudioPaths) -> None:
        write_tts_config(paths, port=8799)
        client = FakeClient(health=health(), voices=voices("bigbear", "littlebear"))
        found = active_resident(paths, client=client)
        assert found is not None
        assert found.base_url == "http://127.0.0.1:8799"
        assert found.voices == ("bigbear", "littlebear")


# ── 超时 ────────────────────────────────────────────────────────────────


class TestTimeout:
    def test_client_waits_longer_than_the_service_does(self, paths: StudioPaths) -> None:
        """必须**客户端更宽**：服务超时会回一个带错误码的响应，而客户端先超时
        只会得到一个"连接断了"，把服务想说的话吞掉。"""
        write_tts_config(paths)
        assert synth_timeout_for(paths) == 7 + SYNTH_TIMEOUT_SLACK_SEC

    def test_missing_config_falls_back_instead_of_refusing(self, paths: StudioPaths) -> None:
        assert synth_timeout_for(paths) == 60 + SYNTH_TIMEOUT_SLACK_SEC


# ── 引擎 ────────────────────────────────────────────────────────────────


def make_engine(
    paths: StudioPaths,
    *,
    client: FakeClient,
    found: ResidentStatus | None = None,
) -> ResidentEngine:
    return ResidentEngine(paths=paths, status=found or status(), timeout_sec=7.0, client=client)


class TestResidentEngine:
    def test_identity_comes_from_the_service_not_from_us(self, paths: StudioPaths) -> None:
        """引擎名与版本进缓存键 —— 自己写死一个，服务换了引擎 / 换了权重之后，
        旧缓存会被当成新引擎的产物复用（复用别人的嗓子）。"""
        engine = make_engine(
            paths,
            client=FakeClient(),
            found=status(engine="cosyvoice9", revision="deadbeef"),
        )
        assert engine.name == "cosyvoice9"
        assert engine.revision == "deadbeef"

    def test_it_posts_the_path_relative_to_data(self, paths: StudioPaths) -> None:
        """服务的落点契约是"相对 ``data/`` 的 POSIX 路径"，而池的落点是
        ``data/output/voice/<t>/s001.wav``（不是服务默认的 ``work/<t>/tts/``）。"""
        client = FakeClient(synth={"ok": True})
        target = paths.sentence_wav("t1", 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF")

        make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)

        sent = client.posts[0]
        assert sent["url"] == "/synth"
        assert sent["json"]["out_path"] == "output/voice/t1/s001.wav"
        assert sent["json"]["voice_id"] == "bigbear"
        assert sent["json"]["text"] == "喂"
        assert sent["timeout"] == 7.0

    def test_rate_is_translated_into_speed(self, paths: StudioPaths) -> None:
        client = FakeClient(synth={"ok": True})
        target = paths.sentence_wav("t1", 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF")

        make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=5)

        assert client.posts[0]["json"]["speed"] == pytest.approx(1.5)

    def test_no_voice_is_refused_before_it_reaches_the_service(self, paths: StudioPaths) -> None:
        """没音色就别发 —— 服务那边只会回一个更含糊的错，而这里能说清是哪一句的
        speaker 没解析出音色。"""
        client = FakeClient(synth={"ok": True})
        target = paths.sentence_wav("t1", 1)
        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice=None, rate=0)
        assert caught.value.code is ErrorCode.TTS_VOICE_MISSING
        assert client.posts == []

    def test_outside_data_is_refused_locally(self, paths: StudioPaths, tmp_path: Path) -> None:
        """产物在 ``data/`` 以外 ⇒ 本地就拒（服务也会拒，但那时错误在另一个进程里）。"""
        client = FakeClient(synth={"ok": True})
        outside = tmp_path / "elsewhere" / "s001.wav"
        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", outside, voice="bigbear", rate=0)
        assert caught.value.code is ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS
        assert client.posts == []

    def test_connection_drop_is_engine_down(self, paths: StudioPaths) -> None:
        client = FakeClient(post_error=httpx.ConnectError("refused"))
        target = paths.sentence_wav("t1", 1)
        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)
        assert caught.value.code is ErrorCode.TTS_ENGINE_DOWN

    def test_the_remote_error_code_survives_the_round_trip(self, paths: StudioPaths) -> None:
        """★ 保住码是重点：池的"连续 N 次 ``TTS_OOM`` ⇒ 降并发"认的就是它。
        这里统一成 ``TTS_SENTENCE_FAILED`` 的话，显存炸了会被当成"这一句念不出来"
        —— 并发不会降，下一句接着炸。"""
        client = FakeClient(synth={"code": "TTS_OOM", "message": "CUDA out of memory"}, synth_status=503)
        target = paths.sentence_wav("t1", 1)

        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)

        assert caught.value.code is ErrorCode.TTS_OOM
        assert caught.value.context["http_status"] == 503

    def test_busy_is_kept_apart_from_oom(self, paths: StudioPaths) -> None:
        """429 只是"此刻忙"，退避重试就会好；503 OOM 要**降并发**。两者的修复动作
        完全不同，所以码不能混。"""
        client = FakeClient(synth={"code": "TTS_BUSY", "message": "queue full"}, synth_status=429)
        target = paths.sentence_wav("t1", 1)

        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)

        assert caught.value.code is ErrorCode.TTS_BUSY

    def test_an_unknown_code_degrades_to_sentence_failed(self, paths: StudioPaths) -> None:
        client = FakeClient(synth={"code": "SOMETHING_NEW", "message": "???"}, synth_status=500)
        target = paths.sentence_wav("t1", 1)

        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)

        assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED

    def test_success_but_no_file_on_disk_is_a_failure(self, paths: StudioPaths) -> None:
        """服务回了 200、盘上却没有东西 —— 那是最坏的一种"成功"。"""
        client = FakeClient(synth={"ok": True})
        target = paths.sentence_wav("t1", 1)
        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)
        assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED

    def test_empty_file_is_also_a_failure(self, paths: StudioPaths) -> None:
        client = FakeClient(synth={"ok": True})
        target = paths.sentence_wav("t1", 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        with pytest.raises(StudioError) as caught:
            make_engine(paths, client=client).synthesize("喂", target, voice="bigbear", rate=0)
        assert caught.value.code is ErrorCode.TTS_SENTENCE_FAILED
