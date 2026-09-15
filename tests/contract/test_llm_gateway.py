"""契约：LLM 双通道网关（T1.8 · §01.2.4 / §02.1 / §04.1.1）。

为什么值得静态检查
------------------
网关有三类"**不会报错的回退**"，只在真出事的凌晨显形：

1. ``llm_calls`` 被越权写入 —— 预算闸门是按这张表聚合判定的，谁都能写就意味着
   "反复重试绕开预算"重新成立（T1.8 裁定 60）。
2. ``agents/`` 反向 import 上层 —— 分层一破，Agent 就会开始直接写 ``tasks``、
   直接推 WS，最后没人说得清"一次调用到底改了哪些状态"（§02.1）。
3. 密钥入库 —— ``config/llm.yaml`` / ``prompts/`` 一旦写了密钥本体，它会随 git
   历史永久留存；密钥**只**从环境变量读（§README.6）。

另外锁住两处"文档 vs 代码"的逐字一致：``CallStatus`` ↔ DDL ``CHECK``、
``prompts/manifest.yaml`` 的 sha256 ↔ 模板文件内容（漂移 = ``prompt_version``
失真 = P5「同输入同产物」失效）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from studio.agents.cost import CallStatus
from studio.agents.prompts import PromptLibrary

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "studio"
AGENTS_ROOT = SRC_ROOT / "agents"
LLM_YAML = REPO_ROOT / "config" / "llm.yaml"
PROMPTS_ROOT = REPO_ROOT / "prompts"
INIT_SQL = SRC_ROOT / "db" / "migrations" / "0001_init.sql"

#: 唯一允许写 ``llm_calls`` 的文件（相对 ``src/studio/``）
ALLOWED_WRITERS = frozenset({"agents/cost.py"})

#: 命中即为越权写入（``CREATE TABLE`` 属于 DDL，不在此列）
_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"UPDATE\s+OR\s+\w+\s+llm_calls\s+SET", re.IGNORECASE),
    re.compile(r"UPDATE\s+llm_calls\s+SET", re.IGNORECASE),
    re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+llm_calls\b", re.IGNORECASE),
    re.compile(r"REPLACE\s+INTO\s+llm_calls\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+llm_calls\b", re.IGNORECASE),
)

#: ``agents/`` 不得 import 的层（§02.1：只能向下依赖 domain / core）
FORBIDDEN_LAYERS: tuple[str, ...] = (
    "studio.app",
    "studio.services",
    "studio.ws",
    "studio.pipeline",
    "studio.tts",
    "studio.render",
    "studio.publish",
    "studio.pools",
)

#: 环境变量名（大写字母 / 数字 / 下划线）—— ``api_key_env`` 只允许是这个形状
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _python_files() -> list[Path]:
    return sorted(
        path for path in SRC_ROOT.rglob("*.py") if "__pycache__" not in path.parts and path.is_file()
    )


def test_only_cost_store_writes_llm_calls_table() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative in ALLOWED_WRITERS:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _WRITE_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{relative}:{line}: {match.group(0)}")

    assert not offenders, "llm_calls 被越权写入（必须走 agents.cost.LlmCallStore）：\n" + "\n".join(offenders)


def test_allowlist_is_not_vacuous() -> None:
    """反向断言：白名单文件被删/改名后，上面那条用例会变成永远通过。"""
    text = (SRC_ROOT / "agents" / "cost.py").read_text(encoding="utf-8")
    assert "INSERT INTO llm_calls" in text
    assert "class LlmCallStore" in text
    assert "def record" in text


def test_agents_layer_does_not_import_upward() -> None:
    """§02.1：``agents/`` 只能向下依赖，反向 import 会让"谁改了状态"失去边界。"""
    offenders: list[str] = []
    for path in sorted(AGENTS_ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for layer in FORBIDDEN_LAYERS:
            pattern = re.compile(rf"^\s*(?:from|import)\s+{re.escape(layer)}\b", re.MULTILINE)
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"agents/{path.name}:{line}: {layer}")

    assert not offenders, "agents/ 反向 import 了上层（§02.1 依赖方向）：\n" + "\n".join(offenders)


def test_agents_layer_never_imports_fastapi() -> None:
    """§02.1 附加约束：``agents/`` 不 import FastAPI（否则无法在 Worker 里裸跑）。"""
    offenders = [
        f"agents/{path.name}"
        for path in sorted(AGENTS_ROOT.glob("*.py"))
        if re.search(r"^\s*(?:from|import)\s+fastapi\b", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert not offenders, f"agents/ 不该依赖 FastAPI：{offenders}"


def test_only_gateway_reads_the_environment() -> None:
    """密钥只从环境变量读 ⇒ 注入点唯一（``os.environ`` 只该出现在 gateway）。"""
    readers = [
        f"agents/{path.name}"
        for path in sorted(AGENTS_ROOT.glob("*.py"))
        if re.search(r"\bos\.environ\b", path.read_text(encoding="utf-8"))
    ]
    assert readers == ["agents/gateway.py"], f"环境变量读取点应唯一，实际：{readers}"


def test_llm_yaml_carries_no_secret_material() -> None:
    """``config/llm.yaml`` 只允许出现环境变量**名**，绝不写密钥本体。"""
    text = LLM_YAML.read_text(encoding="utf-8")
    assert not re.search(r"^\s*api_key\s*:", text, re.MULTILINE), "只允许 api_key_env（变量名）"
    assert "sk-" not in text
    assert "Bearer " not in text

    raw: dict[str, Any] = yaml.safe_load(text)
    profiles: dict[str, Any] = raw["profiles"]
    assert {"cloud", "local"} <= set(profiles), "云端 + 本地两条通道都要在"

    for name, profile in profiles.items():
        value = profile.get("api_key_env")
        assert value is None or _ENV_NAME.match(str(value)), f"{name}.api_key_env 必须是环境变量名"

    assert profiles["cloud"]["api_key_env"] == "STUDIO_LLM_API_KEY"
    assert profiles["local"]["api_key_env"] is None, "本地通道不需要密钥"


def test_env_example_keeps_the_key_blank() -> None:
    """``.env.example`` 是给人抄的模板：密钥位只能是空占位（占位里塞真钥匙=入库）。"""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^STUDIO_LLM_API_KEY=\s*$", text, re.MULTILINE), "只能留空占位"


def test_call_status_matches_ddl_check() -> None:
    """``CallStatus`` 与 DDL ``CHECK`` 必须逐字一致（漂移 = 运行期才炸）。"""
    ddl = INIT_SQL.read_text(encoding="utf-8")
    block = ddl.split("CREATE TABLE IF NOT EXISTS llm_calls", 1)[1].split(");", 1)[0]
    match = re.search(r"status\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)", block)
    assert match is not None, "DDL 里找不到 llm_calls.status 的 CHECK 约束"
    allowed = {item.strip().strip("'") for item in match.group(1).split(",") if item.strip()}
    assert allowed == {str(status) for status in CallStatus}


def test_prompt_manifest_digests_match_files() -> None:
    """注册表的 sha256 必须与模板文件内容一致 ⇒ ``prompt_version`` 才可信（P5）。"""
    library = PromptLibrary.load(PROMPTS_ROOT)
    assert library.names(), "注册表不能是空的"
    assert library.verify() == [], "提示词内容与 manifest.yaml 漂移（跑 studio prompts verify）"

    for name in library.names():
        version = library.prompt_version(name)
        assert version.count("+") == 1, f"{name}: prompt_version 形状应为 <version>+<sha12>"
        assert len(version.split("+")[1]) == 12, f"{name}: sha 前缀应为 12 位"


def test_shared_prompt_blocks_are_registered() -> None:
    """公共注入块（persona / JSON 纪律）必须登记 —— 否则各 Agent 会各写一份。"""
    library = PromptLibrary.load(PROMPTS_ROOT)
    assert {"shared.persona_block", "shared.json_contract"} <= set(library.names())
