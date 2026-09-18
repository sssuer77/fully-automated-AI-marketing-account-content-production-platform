"""契约：``worker_heartbeats`` 表**只允许** ``pools/heartbeat.py`` 写（§04.5.1 · T1.6）。

为什么值得一条静态检查
----------------------
心跳表承载一条不变式：**行存在 ⇔ 该进程应当在跑**。supervisor 靠它判死并重启，
sweeper 靠它做"双保险"（判死 + 租约回收）。只要有一个模块图省事自己
``INSERT/UPDATE worker_heartbeats``：

- 判死会误报（别人替你写活了行），或者漏报（别人把行删了）；
- ``started_at`` 的 ``COALESCE`` 语义被绕过，"重启频率"不再可信；
- 优雅退出的 ``forget`` 不再是唯一删除路径 —— 行会变成孤儿。

这类回退**不会报错**，只在真出事的那个凌晨显形。静态拦住最便宜。

顺带锁住两件事：
1. ``WORKER_STATUSES`` 必须与 DDL 的 ``CHECK`` **逐字一致**（否则运行期才炸）。
2. 判死不升格为 ``system.alert``：§04.5.2 把 ``system.alert.code`` 锁死为 8 个值，
   猝死只能落 ``system_logs`` 的 ``error`` 行 + ``payload_json.code='WORKER_DEAD'``。
"""

from __future__ import annotations

import re
from pathlib import Path

from studio.core.errors import ErrorCode
from studio.core.proto import AlertCode
from studio.pools.heartbeat import WORKER_DEAD_CODE, WORKER_STATUSES

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "studio"

#: 唯一允许写 ``worker_heartbeats`` 的文件（相对 ``src/studio/``）
ALLOWED_WRITERS = frozenset({"pools/heartbeat.py"})

#: 命中即为越权写入（``CREATE TABLE`` 属于 DDL，不在此列）
_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"UPDATE\s+OR\s+\w+\s+worker_heartbeats\s+SET", re.IGNORECASE),
    re.compile(r"UPDATE\s+worker_heartbeats\s+SET", re.IGNORECASE),
    re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+worker_heartbeats\b", re.IGNORECASE),
    re.compile(r"REPLACE\s+INTO\s+worker_heartbeats\b", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+worker_heartbeats\b", re.IGNORECASE),
)


def _python_files() -> list[Path]:
    return sorted(
        path for path in SRC_ROOT.rglob("*.py") if "__pycache__" not in path.parts and path.is_file()
    )


def test_only_heartbeat_store_writes_heartbeat_table() -> None:
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

    assert not offenders, (
        "worker_heartbeats 被越权写入（必须走 pools.heartbeat.HeartbeatStore）：\n" + "\n".join(offenders)
    )


def test_allowlist_is_not_vacuous() -> None:
    """反向断言：白名单文件被删/改名后，上面那条用例会变成永远通过。"""
    writer = SRC_ROOT / "pools" / "heartbeat.py"
    text = writer.read_text(encoding="utf-8")
    assert "INSERT INTO worker_heartbeats" in text
    assert "DELETE FROM worker_heartbeats" in text
    assert "UPDATE worker_heartbeats" in text
    assert "class HeartbeatStore" in text


def test_heartbeat_store_is_the_only_writer_exported() -> None:
    """``pools/__init__.py`` 只许导出 ``HeartbeatStore``，不许导出裸 SQL / 连接。"""
    text = (SRC_ROOT / "pools" / "__init__.py").read_text(encoding="utf-8")
    assert "HeartbeatStore" in text
    for forbidden in ("worker_heartbeats", "def execute", "raw_sql"):
        assert forbidden not in text, f"pools/__init__.py 不该暴露 {forbidden}"


def test_worker_statuses_match_ddl_check() -> None:
    """``WORKER_STATUSES`` 与 DDL ``CHECK`` 必须逐字一致（漂移 = 运行期才炸）。"""
    ddl = (SRC_ROOT / "db" / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    block = ddl.split("CREATE TABLE IF NOT EXISTS worker_heartbeats", 1)[1].split(");", 1)[0]
    match = re.search(r"status\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)", block)
    assert match is not None, "DDL 里找不到 worker_heartbeats.status 的 CHECK 约束"
    allowed = {item.strip().strip("'") for item in match.group(1).split(",") if item.strip()}
    assert allowed == set(WORKER_STATUSES)


def test_worker_dead_is_not_a_system_alert_code() -> None:
    """猝死不升格为告警：``WORKER_DEAD`` 是 ``ErrorCode``，**不**是 ``AlertCode``。

    §04.5.2 把 ``system.alert.code`` 锁成一个**枚举**（T1.5 施工裁定 31）；
    worker 猝死走的是 ``system_logs`` 的 ``error`` 行 + ``payload_json.code``。
    一旦有人把 ``WORKER_DEAD`` 加进 ``AlertCode``，WS 层的"不在枚举内 ⇒ 走 log.append" 分支就会静默失效。
    """
    assert WORKER_DEAD_CODE == "WORKER_DEAD"
    assert ErrorCode.WORKER_DEAD.value == WORKER_DEAD_CODE
    assert WORKER_DEAD_CODE not in {code.value for code in AlertCode}
    # 数量**不进断言**（陷阱 180）：钉死数字的话，加一个与猝死毫无关系的码
    # （T5.6 的 SCHEDULE_FAILING / T5.7 的 REPORT_FAILING）也会让这条用例红，
    # 而它红的原因与它要守的东西（猝死不升格为告警）半点关系都没有。
    assert len(AlertCode) >= 9, "§04.5.2 的告警码枚举塌了（成员名见 proto.AlertCode）"

    heartbeat = (SRC_ROOT / "pools" / "heartbeat.py").read_text(encoding="utf-8")
    assert '"code": WORKER_DEAD_CODE' in heartbeat, "猝死必须落 payload_json.code"
    assert "INSERT INTO system_logs" in heartbeat
    assert "'error'," in heartbeat, "猝死行的 level 必须是 error"
