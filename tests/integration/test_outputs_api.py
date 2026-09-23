"""合成配置 REST 面集成测试（T4.7 验收 · §04.2.8 / §04.5.9）。

验收六条（todolist T4.7）
------------------------
① 一期表单编辑：合成 profile（分辨率 / 帧率 / CRF / 720P 保底档）；
② 水印（位置 / 边距 / 宽度 / 透明度）；
③ 字幕（字号 / 描边 / 每行字数）；
④ 保存前强制 `validate`（不通过**拒绝保存**）；
⑤ 改动写 `audit_ops` 且 `version +1`；
⑥ 并发编辑 ⇒ `source_sha256` 比对。

五条测试纪律
------------
1. **临时家目录 + 真 `outputs.yaml` 抄一份**：本用例**要写盘**，拿仓库根当 home 会把真配置
   改掉。抄进来的那一份就是生产那一份（含全部注释）。
2. **假探针**：`build_state(metrics_probe=...)`（与人物库 / 四池同一手法）—— `lifespan` 会起
   采样泵，否则 `nvidia-smi` 与真磁盘会进来。
3. **断言也看盘、也看库**：REST 返回 200 只说明"没抛"。文件真写没写、`audit_ops` 真留没留痕，
   一律回盘 / 回库查 —— 「校验不过一个字节都不写」这句承诺，只有比对**改动前后的字节**才算验过。
4. **WS 用真连接**：面板靠 `logs` 通道里 `source === "outputs"` 的那条帧自己刷新
   （§04.4.3 的事件表是契约，本轮不为一次配置保存新增事件）。
5. **手改文件也算一条路径**：人直接编辑 YAML 不经 REST 面 —— 下一次 GET 必须看到新值，
   而拿旧指纹提交必须被拦下（这正是把并发判据放在**文件**上的理由）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.render.sticker import plan_stickers
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

OUTPUTS_URL = "/api/v1/outputs"
WS_URL = "/ws/ui"

DOUYIN = "douyin_1080x1920_30fps_v1"
FALLBACK = "fallback_720x1280_v1"

#: 水印 PNG 的**相对**路径（相对 STUDIO_HOME，与 `config/outputs.yaml` 的注释一致）
WATERMARK_REL = "templates/douyin_9x16_default/assets/images/watermark.png"

#: `hero` 那一层贴图的**相对**路径（同上）
STICKER_HERO_REL = "templates/douyin_9x16_default/assets/images/stickers/hero.png"

#: 一份**语法就是坏的** YAML（`default_profile: [` 让解析器直接炸）
BROKEN_YAML = 'schema_version: "1.0"\ndefault_profile: [\n'


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）。"""

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


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """临时家目录：真 `outputs.yaml` 抄一份 + 放一张（占位）水印 PNG。

    水印 PNG 也造出来，是因为面板要回答的正是「这张图在不在」——**在**与**不在**两条路径
    都得验（D5：缺失 ⇒ 渲染拒绝出片）。
    """
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", root / "config" / "outputs.yaml")
    watermark = root / WATERMARK_REL
    watermark.parent.mkdir(parents=True)
    # 造一张**真** PNG（带 alpha）：水印卡片现在同时报 `exists` 与 `usable`，
    # 写几个假字节会让"在盘上"为真、"渲染贴得上"为假 —— 那是另一条路径的输入，
    # 由下面专门那几条用例负责，不混进基线。
    watermark.write_bytes(_png_bytes())
    return root


@pytest.fixture
def paths(home: Path, tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=home, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    paths.ensure_runtime_dirs()
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


@pytest.fixture(scope="module")
def tuned() -> OutputsConfig:
    """仓库里那一份 `config/outputs.yaml` 的**解析结果**（夹具抄的就是它）。

    为什么断言不写死数字：这份文件是**调参**用的 —— 水印宽占比、贴图高度占比、
    开关状态都跟着真机上试出来的结果变。把 0.25 / 0.45 / enabled=false 写进用例，
    每次调参都会红一片，而红的原因是"我改了配置"，不是"面板坏了"：假红会把真红
    淹掉。所以这里把"文件里是什么"读出来，用例只钉**面板 == 文件**这件事。

    反过来，**面板能改哪些字段**（`*_fields` 那几张表）是契约，照旧写死在用例里：
    加一个字段就该有人来改一行，那是要看见的。
    """
    return load_outputs_config(REPO_ROOT / "config" / "outputs.yaml")


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


def _file(paths: StudioPaths) -> Path:
    return paths.config_dir / "outputs.yaml"


def _read(paths: StudioPaths) -> str:
    return _file(paths).read_text(encoding="utf-8")


def _bytes(paths: StudioPaths) -> bytes:
    return _file(paths).read_bytes()


def _write(paths: StudioPaths, text: str) -> None:
    _file(paths).write_text(text, encoding="utf-8", newline="\n")


def _audit(connection: sqlite3.Connection, action: str = "outputs.update") -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM audit_ops WHERE action = ? ORDER BY id", (action,)).fetchall()


def _outputs_logs(state: AppState) -> list[Any]:
    return [row for row in state.logs.recent(limit=200) if row.source == "outputs"]


def _last_log_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM system_logs").fetchone()
    return int(row[0])


def _changed_lines(before: list[str], after: list[str]) -> list[int]:
    return [index for index, line in enumerate(before) if after[index] != line]


def _png_bytes(*, width: int = 64, height: int = 128, color_type: int = 6) -> bytes:
    """造一张**真的** PNG（只用 stdlib）。

    与 `tests/unit/render/conftest.py::png_bytes` 同一手法，但**故意不跨目录 import**：
    `tests/unit/**` 的 conftest 属于那一层的夹具，从集成用例反向依赖它会让"改一个单测
    夹具"悄悄影响集成面的输入。这里要的只有一件事：一张 `probe_png` 认的图。
    """
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = b"".join(b"\x00" + bytes(channels * width) for _ in range(height))
    return b"".join(
        [
            signature,
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw)),
            chunk(b"IEND", b""),
        ]
    )


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 当前配置
# ══════════════════════════════════════════════════════════════════════


def test_read_returns_profiles_watermark_subtitle_and_provenance(
    client: TestClient, paths: StudioPaths
) -> None:
    """一次拿全：四档 profile + 水印 + 字幕 + 来源（版本 / sha256 / 加载时刻 / 文件）。"""
    body = client.get(OUTPUTS_URL).json()
    assert body["stale"] is False
    assert body["error"] is None
    assert body["version"] == 1
    assert len(body["sha256"]) == 64
    assert body["path"] == str(_file(paths))
    assert body["default_profile"] == DOUYIN

    names = [profile["name"] for profile in body["profiles"]]
    assert names[0] == DOUYIN
    assert len(names) == 4
    assert sum(1 for profile in body["profiles"] if profile["is_default"]) == 1

    douyin = body["profiles"][0]
    assert (douyin["width"], douyin["height"], douyin["fps"]) == (1080, 1920, 30)
    assert douyin["quality_field"] == "crf"
    assert douyin["quality"] == 21
    assert douyin["platforms"] == ["douyin", "kuaishou", "shipinhao"]

    fallback = next(profile for profile in body["profiles"] if profile["name"] == FALLBACK)
    assert (fallback["width"], fallback["height"]) == (720, 1280)
    assert fallback["quality_field"] == "cq", "NVENC 档的质量参数叫 CQ，不是 CRF"
    assert fallback["quality"] == 26


def test_read_returns_the_watermark_with_pixel_width(client: TestClient, tuned: OutputsConfig) -> None:
    """面板下发的水印四项 == 文件里那几行；`width_px` 是**算出来的**那一列。"""
    watermark = client.get(OUTPUTS_URL).json()["watermark"]
    spec = tuned.watermark
    canvas_width = tuned.profiles[tuned.default_profile].width
    assert watermark["path"] == WATERMARK_REL
    assert watermark["position"] == spec.position
    assert (watermark["margin_x"], watermark["margin_y"]) == (spec.margin_x, spec.margin_y)
    assert watermark["width_ratio"] == spec.width_ratio
    assert watermark["width_px"] == spec.width_px_for(canvas_width), "面板与编译器同一口径（T3.2）"
    assert watermark["opacity"] == spec.opacity
    assert watermark["exists"] is True


def test_read_returns_the_subtitle_style(client: TestClient) -> None:
    subtitle = client.get(OUTPUTS_URL).json()["subtitle"]
    assert subtitle["enabled"] is True
    assert subtitle["font_size"] == 64
    assert subtitle["outline"] == 4
    assert subtitle["max_chars_per_line"] == 13
    # 「距底」与「底部安全区」都在这一屏里（T3.5 追加 · 裁定 399）：真正生效的是
    # 两者的 **max**，服务端把它算成 `margin_v` 一起下发 —— 面板不必自己再算一遍，
    # 也就不会出现"面板说 300、成片渲 420"（这正是此前把它锁成只读的那条理由）。
    assert subtitle["margin_bottom"] == 420
    assert subtitle["safe_area_bottom"] == 420
    assert subtitle["margin_v"] == 420


def test_read_ships_form_limits_and_the_position_enum(client: TestClient) -> None:
    """上下限跟着响应下发（唯一真相是配置模型的字段约束 · 裁定 161）。"""
    limits = client.get(OUTPUTS_URL).json()["limits"]
    assert limits["profile"]["width"] == {
        "min": 64.0,
        "max": 7680.0,
        "exclusive_min": False,
        "exclusive_max": False,
    }
    assert limits["watermark"]["width_ratio"] == {
        "min": 0.0,
        "max": 0.25,
        "exclusive_min": True,
        "exclusive_max": False,
    }
    assert limits["watermark"]["positions"] == [
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    ]
    assert limits["subtitle"]["font_size"]["max"] == 200.0
    assert limits["subtitle_fields"] == [
        "font_size",
        "outline",
        "margin_bottom",
        "safe_area_bottom",
        "max_chars_per_line",
    ]
    # 嵌套字段（`subtitle.safe_area.bottom`）的上下限同样**现取**自模型。
    assert limits["subtitle"]["safe_area_bottom"] == {
        "min": 0.0,
        "max": 2000.0,
        "exclusive_min": False,
        "exclusive_max": False,
    }


def test_missing_watermark_png_is_reported_before_render(client: TestClient, paths: StudioPaths) -> None:
    """PNG 不在盘上 ⇒ 渲染会拒绝出片（D5）。面板必须**提前**说出来，不是等点了"开始"才报错。"""
    (paths.home / WATERMARK_REL).unlink()
    body = client.get(OUTPUTS_URL).json()
    assert body["watermark"]["exists"] is False
    assert body["stale"] is False, "水印不在与「配置读不出来」是两件事"


def test_read_returns_the_sticker_layers_in_declaration_order(
    client: TestClient, tuned: OutputsConfig
) -> None:
    """人物贴图是**若干层**：顺序 = YAML 的声明顺序（= 叠放顺序），面板照这个顺序往下列。

    逐层**照着文件比**（`tuned` 就是夹具抄进临时家目录的那一份）。要钉的是"文件里每一层
    的每一个可编辑字段，面板一个不落地如实转述" —— 少转述一个（比如 T6.5 那两个换图
    字段），面板上就有一格是假的，而假的那一格恰恰是用户唯一会来看的地方。
    """
    layers = client.get(OUTPUTS_URL).json()["stickers"]
    assert [layer["name"] for layer in layers] == list(tuned.stickers), "声明顺序 = 叠放顺序"
    canvas_height = tuned.profiles[tuned.default_profile].height
    for layer, spec in zip(layers, tuned.stickers.values(), strict=True):
        assert layer["enabled"] == spec.enabled
        assert layer["path"] == spec.path.as_posix()
        assert layer["speaker"] == spec.speaker
        assert layer["speaking_path"] == (
            None if spec.speaking_path is None else spec.speaking_path.as_posix()
        )
        assert layer["position"] == spec.position
        assert (layer["margin_x"], layer["margin_y"]) == (spec.margin_x, spec.margin_y)
        assert layer["height_ratio"] == spec.height_ratio
        assert layer["height_px"] == spec.height_px_for(canvas_height), "按**画布高**算，不是水印那条宽度占比"
        assert layer["opacity"] == spec.opacity

    hero = layers[0]
    assert hero["path"] == STICKER_HERO_REL, "hero 那一层指的就是这张图"
    assert hero["exists"] is False, "临时家目录里没造这张图 —— 与「贴不贴得上」是两件事"
    assert hero["usable"] is False
    assert hero["problem"] == "文件不存在"


def test_a_sticker_that_exists_but_is_not_a_png_is_reported_as_unusable(
    client: TestClient, paths: StudioPaths
) -> None:
    """★ 「在盘上」与「渲染真的会贴上」是**两件事** —— 面板必须把后者也说出来。

    真机上踩到的就是这一条：`hero.png` / `guest.png` 其实是 WebP / JPEG（改过扩展名），
    于是"开关开着、面板显示在盘上、渲染每一层都跳过"能同时成立，而面板一个字都不说。
    `exists` 为真而 `usable` 为假，正是那句"我开了它、为什么片子上没有"的答案。
    """
    target = paths.home / STICKER_HERO_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"RIFF\xfc\x1d\x00\x00WEBPVP8 ")  # WebP：扩展名叫 .png，字节不是

    hero = client.get(OUTPUTS_URL).json()["stickers"][0]
    assert hero["exists"] is True, "文件确实在盘上（所以只看 exists 的面板会亮绿灯）"
    assert hero["usable"] is False
    assert "不是 PNG" in hero["problem"]


def test_a_sticker_without_alpha_is_unusable_but_a_real_one_is_not(
    client: TestClient, paths: StudioPaths
) -> None:
    """没有透明通道的 PNG 会盖住一块实心画面 ⇒ 渲染跳过；带 alpha 的真 PNG ⇒ 会贴上。"""
    target = paths.home / STICKER_HERO_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_png_bytes(color_type=2))  # 真彩、无 alpha
    assert client.get(OUTPUTS_URL).json()["stickers"][0]["usable"] is False

    target.write_bytes(_png_bytes(color_type=6))  # 真彩 + alpha
    hero = client.get(OUTPUTS_URL).json()["stickers"][0]
    assert hero["usable"] is True
    assert hero["problem"] is None, "usable=True ⇒ 没有原因可说"


@pytest.mark.parametrize(
    "payload",
    [
        None,  # 文件不在
        b"RIFF\xfc\x1d\x00\x00WEBPVP8 ",  # 扩展名叫 .png 的 WebP
        b"\x89PNG\r\n",  # 截断的 PNG
    ],
    ids=["missing", "webp", "truncated"],
)
def test_the_card_agrees_with_the_render_path(
    client: TestClient, paths: StudioPaths, payload: bytes | None
) -> None:
    """★ 面板的 `usable` 与**渲染**的结论必须一致 —— 这是这条改动存在的全部理由。

    只断言"面板报了个 false"是不够的：真正要钉住的是**两处判据同源**。所以这里同时问
    两边 —— REST 卡片与 `plan_stickers`（渲染路径的**唯一**判断点），并断言它们对同一份
    文件给出同一个答案。
    """
    target = paths.home / STICKER_HERO_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(payload)

    # 先像面板那样**真的把这一层打开**（关着的层渲染会走另一条分支："在配置里是关的"）。
    opened = client.post(
        OUTPUTS_URL,
        json={
            "stickers": {"hero": {"enabled": True}},
            "source_sha256": client.get(OUTPUTS_URL).json()["sha256"],
        },
    )
    assert opened.status_code == 200
    body = client.get(OUTPUTS_URL).json()
    hero = body["stickers"][0]
    assert hero["enabled"] is True

    config = load_outputs_config(paths.config_dir / "outputs.yaml")
    plan = plan_stickers(
        config.stickers,
        canvas_width=config.profiles[config.default_profile].width,
        canvas_height=config.profiles[config.default_profile].height,
        home=paths.home,
    )[0]
    assert hero["usable"] is plan.applied, "面板与渲染报的必须是同一件事"
    assert plan.applied is False
    assert hero["problem"] in (plan.skipped_reason or ""), "面板那句话就是渲染跳过的那句话"


def test_read_ships_the_sticker_limits(client: TestClient) -> None:
    """贴图的上下限也现读配置模型 —— 高度占比上限是 **1.0**（水印那条 0.25 只管水印）。"""
    limits = client.get(OUTPUTS_URL).json()["limits"]
    assert limits["sticker_fields"] == [
        "enabled",
        "path",
        "speaker",
        "speaking_path",
        "position",
        "margin_x",
        "margin_y",
        "height_ratio",
        "opacity",
    ]
    assert limits["sticker"]["height_ratio"] == {
        "min": 0.0,
        "max": 1.0,
        "exclusive_min": True,
        "exclusive_max": False,
    }
    assert limits["sticker"]["positions"] == limits["watermark"]["positions"], "位置枚举只有一份"


# ══════════════════════════════════════════════════════════════════════
# ② 写 · 保存表单
# ══════════════════════════════════════════════════════════════════════


def test_save_changes_one_line_and_leaves_a_trace(
    client: TestClient, paths: StudioPaths, state: AppState, connection: sqlite3.Connection
) -> None:
    """★ 保存：只动那一行 + 版本 +1 + `audit_ops` 留痕 + `system_logs` 记一行。"""
    before = _read(paths).splitlines(keepends=True)
    body = client.get(OUTPUTS_URL).json()

    response = client.post(
        OUTPUTS_URL,
        json={
            "subtitle": {"font_size": 72},
            "source_sha256": body["sha256"],
            "reason": "字号偏小",
        },
    )
    assert response.status_code == 200
    outcome = response.json()
    assert outcome["action"] == "outputs.update"
    assert outcome["changed"] is True
    assert outcome["fields"] == ["subtitle.font_size"]
    assert outcome["version"] == body["version"] + 1
    assert outcome["sha256"] != body["sha256"]
    assert outcome["path"] == str(_file(paths))
    assert "只影响后续渲染" in outcome["note"]

    after = _read(paths).splitlines(keepends=True)
    assert len(after) == len(before)
    changed = _changed_lines(before, after)
    assert len(changed) == 1
    assert after[changed[0]].strip() == "font_size: 72"
    assert "# ★ 水印是可选装饰" in _read(paths), "注释必须还在"

    rows = _audit(connection)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "user"
    assert row["source"] == "webui"
    assert row["target_type"] == "outputs"
    assert row["target_id"] == str(_file(paths))
    assert row["reason"] == "字号偏小"
    assert row["result"] == "ok"
    # `before` 是**唯一**能查到"改之前是多少"的地方（这份配置没有备份目录）
    before_trace = json.loads(row["before_json"])
    after_trace = json.loads(row["after_json"])
    assert before_trace["values"] == {"subtitle.font_size": 64}
    assert after_trace["values"] == {"subtitle.font_size": 72}
    assert before_trace["sha256"] == body["sha256"]
    assert before_trace["version"] == body["version"]
    assert after_trace["version"] == outcome["version"]

    logs = _outputs_logs(state)
    assert len(logs) == 1
    assert logs[0].level == "info"
    assert "subtitle.font_size" in logs[0].message
    assert logs[0].payload["action"] == "outputs.update"
    assert logs[0].payload["changed"] is True

    reread = client.get(OUTPUTS_URL).json()
    assert reread["subtitle"]["font_size"] == 72
    assert reread["sha256"] == outcome["sha256"], "面板拿到的必须是最新指纹（否则下次保存必 409）"


def test_save_can_change_a_profile_and_the_default(
    client: TestClient, paths: StudioPaths, tuned: OutputsConfig
) -> None:
    """①② 的合并面：改分辨率 / 帧率 / 质量，并把默认档换成 720P 保底档。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "default_profile": FALLBACK,
            "profiles": {DOUYIN: {"width": 1080, "height": 1920, "fps": 60, "quality": 18}},
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert response.json()["fields"] == [
        "default_profile",
        f"profiles.{DOUYIN}.width",
        f"profiles.{DOUYIN}.height",
        f"profiles.{DOUYIN}.fps",
        f"profiles.{DOUYIN}.quality",
    ]
    text = _read(paths)
    assert "default_profile: " + FALLBACK in text
    assert "fps: 60" in text
    assert "crf: 18" in text
    reread = client.get(OUTPUTS_URL).json()
    assert reread["default_profile"] == FALLBACK
    fallback_width = tuned.profiles[FALLBACK].width
    assert reread["watermark"]["width_px"] == tuned.watermark.width_px_for(fallback_width), (
        "水印像素宽跟着**默认档**的画布宽走：换了默认档 ⇒ 同一个比例换个像素数"
    )


def test_save_updates_the_watermark(client: TestClient, paths: StudioPaths) -> None:
    """② 水印四项：位置 / 边距 / 宽度 / 透明度。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "watermark": {
                "position": "top_left",
                "margin_x": 32,
                "margin_y": 64,
                "width_ratio": 0.18,
                "opacity": 0.6,
            },
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    text = _read(paths)
    assert "position: top_left" in text
    assert "margin_x: 32" in text
    assert "margin_y: 64" in text
    assert "width_ratio: 0.18" in text
    assert "opacity: 0.6" in text
    assert "# top_left | top_right | bottom_left | bottom_right" in text, "行尾注释必须还在"


def test_save_updates_one_sticker_layer(client: TestClient, paths: StudioPaths, tuned: OutputsConfig) -> None:
    """② 贴图一层：开关 / 换图 / 位置 / 边距 / 高度占比 / 透明度 —— **只动这一层**的那几行。"""
    before = _read(paths).splitlines(keepends=True)
    spec = tuned.stickers["hero"]
    desired: dict[str, Any] = {
        "enabled": True,
        "path": "templates/douyin_9x16_default/assets/images/stickers/hero_v2.png",
        "position": "top_left",
        "margin_x": 64,
        "margin_y": 64,
        "height_ratio": 0.6,
        "opacity": 0.9,
    }
    current: dict[str, Any] = {
        "enabled": spec.enabled,
        "path": spec.path.as_posix(),
        "position": spec.position,
        "margin_x": spec.margin_x,
        "margin_y": spec.margin_y,
        "height_ratio": spec.height_ratio,
        "opacity": spec.opacity,
    }
    # `fields` 说的是"这次**按到**了哪几行"（面板提交了几个字段就是几个），而盘上真的
    # 变了的行更少 —— 配置里开关本来就开着，再开一次写出来是同一个字节。两个数分开算：
    # 写死"7 行"的用例会在调参之后为一件没发生的事报警（假红会淹掉真红）。
    reported_fields = [f"stickers.hero.{name}" for name in desired]
    written_lines = sum(1 for name, value in desired.items() if current[name] != value)

    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"stickers": {"hero": desired}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 200
    assert response.json()["fields"] == reported_fields

    text = _read(paths)
    assert "enabled: true" in text
    assert "position: top_left" in text
    assert "margin_x: 64" in text
    assert "height_ratio: 0.6" in text
    assert "opacity: 0.9" in text
    guest_ratio = tuned.stickers["guest"].height_ratio
    assert f"height_ratio: {guest_ratio}" in text, "guest 那一层一个字都不动"
    assert "# 主讲人物。name 只是一个标识，不参与渲染。" in text, "槽位注释必须还在"

    after = _read(paths).splitlines(keepends=True)
    assert len(after) == len(before), "行级替换：行数不变（面板加不了层）"
    assert len(_changed_lines(before, after)) == written_lines, "盘上真的变了的行数"

    layers = client.get(OUTPUTS_URL).json()["stickers"]
    hero = next(layer for layer in layers if layer["name"] == "hero")
    assert hero["enabled"] is True
    assert hero["height_ratio"] == 0.6
    assert hero["height_px"] == 1152, "1920 * 0.6 —— 像素高跟着占比走"


def test_a_sticker_path_is_written_with_forward_slashes(client: TestClient, paths: StudioPaths) -> None:
    """`str(Path)` 在 Windows 上给的是反斜杠 —— 写盘必须转正斜杠。

    不转的话，同一份配置在两种机器上写出两种字节，diff 里看着像"改了路径"，其实只是换了个
    分隔符（`scalar_for_write` 那条规则就是这么来的）。
    """
    body = client.get(OUTPUTS_URL).json()
    native = str(Path("templates/douyin_9x16_default/assets/images/stickers/hero_v2.png"))
    response = client.post(
        OUTPUTS_URL,
        json={"stickers": {"hero": {"path": native}}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 200
    line = next(row for row in _read(paths).splitlines() if "hero_v2.png" in row)
    assert line.strip() == "path: templates/douyin_9x16_default/assets/images/stickers/hero_v2.png"


def test_save_updates_the_subtitle(client: TestClient, paths: StudioPaths) -> None:
    """③ 字幕五项：字号 / 描边 / 每行字数 / 距底 / 底部安全区。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "subtitle": {
                "font_size": 48,
                "outline": 6,
                "max_chars_per_line": 12,
                "margin_bottom": 300,
                "safe_area_bottom": 260,
            },
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    text = _read(paths)
    assert "font_size: 48" in text
    assert "outline: 6" in text
    assert "max_chars_per_line: 12" in text
    assert "margin_bottom: 300" in text
    # 扁平名（`safe_area_bottom`）落到**嵌套**那一行（`subtitle.safe_area.bottom`）——
    # 这一条就是"那份对照表真的接上了"的读数。
    safe_line = next(row for row in text.splitlines() if row.strip().startswith("bottom:"))
    assert safe_line.strip() == "bottom: 260"
    assert "shadow: 2" in text, "没改的字段一个字都不动"
    reread = client.get(OUTPUTS_URL).json()["subtitle"]
    assert (reread["margin_bottom"], reread["safe_area_bottom"]) == (300, 260)
    assert reread["margin_v"] == 300, "两者取大：300 > 260"

    # 再往下挪一点：两个数一起调小，生效值就跟着下来 —— 这是"往下移"唯一的路，
    # 面板上那句提示说的正是它。
    body = client.get(OUTPUTS_URL).json()
    again = client.post(
        OUTPUTS_URL,
        json={
            "subtitle": {"margin_bottom": 200, "safe_area_bottom": 200},
            "source_sha256": body["sha256"],
        },
    )
    assert again.status_code == 200
    assert client.get(OUTPUTS_URL).json()["subtitle"]["margin_v"] == 200


def test_saving_the_same_value_writes_nothing_and_leaves_no_trace(
    client: TestClient,
    paths: StudioPaths,
    state: AppState,
    connection: sqlite3.Connection,
) -> None:
    """`changed=false` 是**如实回答**：不写盘、版本不动、不留痕（否则审计表会被刷屏）。"""
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 64}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 200
    outcome = response.json()
    assert outcome["changed"] is False
    assert outcome["version"] == body["version"]
    assert outcome["sha256"] == body["sha256"]
    assert "没有做任何改动" in outcome["note"]
    assert _bytes(paths) == before
    assert _audit(connection) == []
    assert _outputs_logs(state) == []


# ══════════════════════════════════════════════════════════════════════
# ④ 校验不过 ⇒ 拒绝保存
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"subtitle": {"font_size": 9999}}, "subtitle.font_size"),
        ({"watermark": {"margin_x": 49}}, "watermark.margin_x"),
        ({"watermark": {"width_ratio": 0.0}}, "watermark.width_ratio"),
        ({"profiles": {DOUYIN: {"width": 1081}}}, f"profiles.{DOUYIN}"),
        ({"profiles": {"nope_v1": {"width": 720}}}, "profiles.nope_v1"),
        ({"default_profile": "nope_v1"}, "<root>"),
        ({"stickers": {"hero": {"margin_x": 49}}}, "stickers.hero.margin_x"),
        ({"stickers": {"hero": {"height_ratio": 1.5}}}, "stickers.hero.height_ratio"),
        ({"stickers": {"nope": {"enabled": True}}}, "stickers.nope"),
        # 嵌套字段的报错必须落在**面板认得**的那个框上（`safe_area_bottom`）：
        # 文件里的路径是 `subtitle.safe_area.bottom`，照抄给前端就会出现"保存失败，
        # 但哪一格都没红"（对照表在 `outputs_store._FIELD_ALIASES`）。
        ({"subtitle": {"safe_area_bottom": 5000}}, "subtitle.safe_area_bottom"),
        ({"subtitle": {"margin_bottom": 5000}}, "subtitle.margin_bottom"),
    ],
)
def test_invalid_forms_are_rejected_with_422_and_write_nothing(
    client: TestClient,
    paths: StudioPaths,
    connection: sqlite3.Connection,
    changes: dict[str, Any],
    field: str,
) -> None:
    """④ 保存前强制 validate：不通过 ⇒ 422 + `field_errors`，**盘上原封未动**。"""
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(OUTPUTS_URL, json={**changes, "source_sha256": body["sha256"]})
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "OUTPUTS_INVALID"
    assert [item["field"] for item in payload["context"]["field_errors"]] == [field]
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_an_odd_canvas_is_rejected_with_a_readable_reason(client: TestClient, paths: StudioPaths) -> None:
    """宽高必须是偶数（yuv420p 色度对齐）—— 报错要说得出"为什么"。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"profiles": {DOUYIN: {"height": 1921}}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 422
    error = response.json()["context"]["field_errors"][0]["error"]
    assert "偶数" in error


def test_an_unknown_sticker_layer_is_rejected_with_a_hint(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """★ 面板**加不了层**（层是 YAML 里的命名块）：报错要说清"现有的是哪些"和"该怎么加"。

    静默忽略的代价是：用户以为"第 3 层已经建好了"，而文件里根本没有那一块 —— 出片时那一层
    自然不存在，且没有任何地方说得出来。
    """
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"stickers": {"extra": {"enabled": True}}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "OUTPUTS_INVALID"
    assert payload["context"]["field_errors"] == [
        {"field": "stickers.extra", "error": "没有这一层贴图：extra"}
    ]
    assert "hero" in payload["remediation"] and "guest" in payload["remediation"]
    assert "config/outputs.yaml" in payload["remediation"]
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_the_sticker_body_has_no_width_ratio(client: TestClient, paths: StudioPaths) -> None:
    """贴图**没有** `width_ratio` 这个字段 —— 请求体这一层就拒了，比"写进文件再报错"更早。

    守的是 T6.5 那处刻意的不对称：人物按**高度**定尺寸，水印那套宽度占比不该被顺手带过来
    （1080 × 0.25 = 270px 做不了主体）。
    """
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"stickers": {"hero": {"width_ratio": 0.2}}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert _bytes(paths) == before


# ══════════════════════════════════════════════════════════════════════
# ⑤ 并发编辑 ⇒ source_sha256
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_sha_is_rejected_with_409_and_writes_nothing(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """⑥ 指纹对不上 ⇒ 409 `OUTPUTS_STALE`（状态问题，不是入参问题）。"""
    before = _bytes(paths)
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 72}, "source_sha256": "0" * 64},
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "OUTPUTS_STALE"
    assert payload["context"]["expected_sha256"] == "0" * 64
    assert payload["context"]["actual_sha256"] != "0" * 64
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_a_manual_edit_is_seen_and_then_blocks_a_save_with_the_old_sha(
    client: TestClient, paths: StudioPaths
) -> None:
    """人直接编辑 YAML：下一次 GET 看得到新值，而拿**旧**指纹提交必须被拦下。"""
    stale = client.get(OUTPUTS_URL).json()
    _write(paths, _read(paths).replace("  margin_y: 420", "  margin_y: 196"))

    fresh = client.get(OUTPUTS_URL).json()
    assert fresh["watermark"]["margin_y"] == 196
    assert fresh["sha256"] != stale["sha256"]
    assert fresh["version"] > stale["version"], "热重载要能被看见（版本 +1）"

    before = _bytes(paths)
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 72}, "source_sha256": stale["sha256"]},
    )
    assert response.status_code == 409
    assert _bytes(paths) == before


def test_two_tabs_saving_in_turn_do_not_clobber_each_other(client: TestClient, paths: StudioPaths) -> None:
    """两个标签页各改各的：第一个成功，第二个必须先拿新指纹再提交（顺序保存不丢改动）。"""
    first = client.get(OUTPUTS_URL).json()
    second = client.get(OUTPUTS_URL).json()
    assert first["sha256"] == second["sha256"]

    assert (
        client.post(
            OUTPUTS_URL,
            json={"subtitle": {"font_size": 72}, "source_sha256": first["sha256"]},
        ).status_code
        == 200
    )
    rejected = client.post(
        OUTPUTS_URL,
        json={"watermark": {"opacity": 0.5}, "source_sha256": second["sha256"]},
    )
    assert rejected.status_code == 409

    refreshed = client.get(OUTPUTS_URL).json()
    assert refreshed["subtitle"]["font_size"] == 72, "第一个标签页的改动还在"
    retried = client.post(
        OUTPUTS_URL,
        json={"watermark": {"opacity": 0.5}, "source_sha256": refreshed["sha256"]},
    )
    assert retried.status_code == 200
    assert client.get(OUTPUTS_URL).json()["watermark"]["opacity"] == 0.5


# ══════════════════════════════════════════════════════════════════════
# ⑥ 实时推送（`logs` 通道 · 不新增 WS 事件）
# ══════════════════════════════════════════════════════════════════════


def test_save_pushes_a_log_frame_for_the_console(client: TestClient, connection: sqlite3.Connection) -> None:
    """面板靠这条帧自己刷新 —— 别人（CLI / 另一个标签页）改了配置也走同一条路。"""
    body = client.get(OUTPUTS_URL).json()
    last_id = _last_log_id(connection)
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id={last_id}") as session:
        client.post(
            OUTPUTS_URL,
            json={"subtitle": {"font_size": 72}, "source_sha256": body["sha256"]},
        )
        frame = json.loads(session.receive_text())
    assert frame["type"] == "event"
    assert frame["channel"] == "logs"
    assert frame["data"]["kind"] == "log.appended"
    assert frame["data"]["source"] == "outputs"
    assert frame["data"]["level"] == "info"
    assert "subtitle.font_size" in frame["data"]["message"]


# ══════════════════════════════════════════════════════════════════════
# ⑦ 配置坏了：面板仍要能把它修回来
# ══════════════════════════════════════════════════════════════════════


def test_a_broken_file_still_returns_200_with_limits(client: TestClient, paths: StudioPaths) -> None:
    """读取失败**也返回 200**：这个面板存在的意义就是"配置坏了的时候把它修回来"。"""
    _write(paths, BROKEN_YAML)
    response = client.get(OUTPUTS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is True
    assert body["profiles"] == []
    assert body["watermark"] is None
    assert body["subtitle"] is None
    assert body["error"]
    assert body["limits"]["profile"]["width"]["max"] == 7680.0, "上下限来自模型，照常下发"


def test_saving_into_a_broken_file_is_rejected_and_writes_nothing(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    _write(paths, BROKEN_YAML)
    before = _bytes(paths)
    response = client.post(OUTPUTS_URL, json={"subtitle": {"font_size": 72}})
    assert response.status_code == 422
    assert response.json()["code"] == "OUTPUTS_INVALID"
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_a_missing_file_is_reported_as_not_found(client: TestClient, paths: StudioPaths) -> None:
    _file(paths).unlink()
    response = client.get(OUTPUTS_URL)
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert "不存在" in response.json()["error"]
