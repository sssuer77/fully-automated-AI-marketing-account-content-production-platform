"""契约：前端类型**只能**由后端生成（T4.1 · §02.2 / §04.4）。

为什么值得一条静态检查
----------------------
T4.1 的硬约束是"**API 类型由 OpenAPI 自动生成，禁止手写接口类型**"。这类约束靠自觉
必然腐烂：生成物一旦入库就没人记得重新生成，而"漂移"只有在能被断言时才叫漂移。
所以这里做四件事：

1. **逐字比对生成物**：`web/openapi.json` 与 `web/src/ws/events.ts` 必须与
   `studio.app.webcontracts` 的渲染结果完全一致（和 `prompts/manifest.yaml` 的
   sha256 漂移同一条思路：后端一改、生成物没跟着改，门禁就红）。
2. **锁住两侧的日志行形状**：REST 的 `LogRow` 与 WS 的 `SystemLog.to_dict()` 必须
   字段同名同集合 —— 前端只认一种形状，两边分叉就会让"某一路日志字段静默 undefined"。
3. **把 `fetch(` 收敛到一个文件**：整个 `web/src` 只允许 `api/http.ts` 出现 `fetch(`。
   统一超时 / 统一错误信封 / 统一请求头才有唯一落点。
4. **锁住验收命令本身**：`package.json` 的 `typecheck` / `test` / `build` / `size`
   就是 T4.1 的验收动作，被改名或改内容等于把门禁悄悄拆掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from studio.app.main import create_app
from studio.app.schemas.logs import LogRow
from studio.app.webcontracts import (
    EVENTS_TS_RELATIVE,
    OPENAPI_JSON_RELATIVE,
    render_events_ts,
    render_openapi_json,
)
from studio.core.proto import AlertCode
from studio.services.asset_service import LOG_SOURCE as ASSETS_LOG_SOURCE
from studio.services.log_service import SystemLog
from studio.services.outputs_service import LOG_SOURCE as OUTPUTS_LOG_SOURCE

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
WEB_SRC = WEB_DIR / "src"

#: 唯一允许出现 ``fetch(`` 的文件（相对 ``web/``）
FETCH_ALLOWLIST = frozenset({"src/api/http.ts"})

#: 扫描的前端源码后缀（`.vue` 也在内：`<script setup>` 同样是源码）
SOURCE_SUFFIXES = frozenset({".ts", ".vue"})


def _web_text(relative: str) -> str:
    return (WEB_DIR / relative).read_text(encoding="utf-8")


def test_openapi_json_matches_backend() -> None:
    assert _web_text(OPENAPI_JSON_RELATIVE) == render_openapi_json(create_app()), (
        "web/openapi.json 已漂移：跑 `uv run python scripts/dump_web_contracts.py` 重新生成"
    )


def test_events_ts_matches_backend() -> None:
    assert _web_text(EVENTS_TS_RELATIVE) == render_events_ts(), (
        "web/src/ws/events.ts 已漂移：跑 `uv run python scripts/dump_web_contracts.py`"
    )


def test_generated_types_cover_rest_models() -> None:
    """`npm run gen:api` 的产物必须真的含端点模型 —— 否则前端拿到的全是 `unknown`。"""
    text = _web_text("src/api/types.gen.ts")
    for name in ("HealthResponse", "LogPage", "LogRow", "WsStats"):
        assert name in text, f"types.gen.ts 缺少 {name}：跑 `cd web && npm run gen:api`"


def test_events_ts_exposes_alert_codes() -> None:
    """告警码表必须出现在生成物里：前端高亮用的就是服务端分流用的那 8 个值。

    这条不是重复上面那条逐字比对 —— 它防的是"渲染函数被改成不输出告警码"：
    逐字比对会跟着一起变绿，而前端会静默地再也认不出告警。
    """
    text = _web_text(EVENTS_TS_RELATIVE)
    assert "export const ALERT_CODES" in text
    for code in AlertCode:
        assert f'"{code.value}"' in text, f"events.ts 缺少告警码 {code.value}"


def test_outputs_log_source_matches_backend() -> None:
    """合成配置面板靠 `logs` 通道里 `source === "outputs"` 自己刷新 —— 两侧必须同源。

    抄一份的后果很具体：后端改了 `LOG_SOURCE`，前端还在等一个永远不来的字符串，表现为
    "别人改了配置、我这一屏不刷新" —— 这种"少一条推送"是最难查的一类。本条与
    `test_events_ts_exposes_alert_codes` 同一手法：不是逐字比对生成物，而是锁住
    **两侧共同依赖的那个值**。
    """
    text = _web_text("src/stores/outputs.ts")
    assert f'export const OUTPUTS_LOG_SOURCE = "{OUTPUTS_LOG_SOURCE}";' in text, (
        "web/src/stores/outputs.ts 的 OUTPUTS_LOG_SOURCE 与后端 outputs_service.LOG_SOURCE 不一致"
    )


def test_assets_log_source_matches_backend() -> None:
    """素材库面板靠 `logs` 通道里 `source === "assets"` 自己刷新 —— 两侧必须同源。

    与合成配置同一条理由（裁定：扫描 / 启停不新增 WS 事件）：后端改了
    `asset_service.LOG_SOURCE`，前端还在等一个永远不来的字符串，表现为
    "别人入了库、我这一屏不刷新"。
    """
    text = _web_text("src/stores/assets.ts")
    assert f'export const ASSETS_LOG_SOURCE = "{ASSETS_LOG_SOURCE}";' in text, (
        "web/src/stores/assets.ts 的 ASSETS_LOG_SOURCE 与后端 asset_service.LOG_SOURCE 不一致"
    )


def test_log_row_fields_match_ws_row() -> None:
    """REST 的 `LogRow` 与 WS 的 `SystemLog.to_dict()` 必须同字段。"""
    ws_fields = set(SystemLog(id=1, ts="", level="info", source="", message="").to_dict())
    assert set(LogRow.model_fields) == ws_fields, (
        "LogRow 与 SystemLog.to_dict() 字段集不一致："
        f"仅 REST 有 {sorted(set(LogRow.model_fields) - ws_fields)}，"
        f"仅 WS 有 {sorted(ws_fields - set(LogRow.model_fields))}"
    )


def test_only_http_module_calls_fetch() -> None:
    offenders: list[str] = []
    hits = 0
    for path in sorted(WEB_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(WEB_DIR).as_posix()
        count = path.read_text(encoding="utf-8").count("fetch(")
        if count == 0:
            continue
        hits += count
        if relative not in FETCH_ALLOWLIST:
            offenders.append(f"{relative}: {count} 处")

    assert not offenders, "只有 api/http.ts 允许直接 fetch：\n" + "\n".join(offenders)
    # 反向断言：白名单文件被改名/删掉后，上面那条会变成永远通过。
    assert hits > 0, "白名单文件里已经没有 fetch( 了 —— 这条用例会变成永远通过"


def test_package_scripts_cover_acceptance() -> None:
    """T4.1 的验收命令写在 `package.json` 里；被改名/改内容等于拆掉门禁。"""
    scripts = json.loads(_web_text("package.json"))["scripts"]
    assert scripts["typecheck"] == "vue-tsc --noEmit"
    assert scripts["test"] == "vitest run"
    assert scripts["build"] == "vite build"
    assert "check-dist-size.mjs" in scripts["size"]
    assert "typecheck" in scripts["verify"]


def test_dist_size_gate_is_three_megabytes() -> None:
    """产物 < 3 MB 是验收硬指标，门禁脚本里的上限必须真的是 3 MB。"""
    text = _web_text("scripts/check-dist-size.mjs")
    assert "3 * 1024 * 1024" in text, "体积门禁被改过：T4.1 的验收是 < 3 MB"
