"""人物变更 → ``system.persona_changed``（T4.13 · §04.4.3）。

为什么挂在 store 的订阅上，而不是"写入口顺手声明一条事件"
----------------------------------------------------------
本项目里业务事件有两条既有做法：① 写一行 `system_logs` 并在 payload 里声明 ``kind``，
Hub 的 tail 读到就发（确认闸、池暂停走这条）；② 直接往 Hub 投递（采样泵走这条）。

人物这里两条都不合适：改人物的路径有**三条** —— 面板保存 / CLI ``studio persona use``
/ 人直接在编辑器里改 YAML —— 而只有第一条会经过我们的写入口。声明式写法会让后两条
**悄无声息**，可它们恰恰是"我在编辑器里改了口吻，面板为什么没反应"的答案。挂在
:class:`~studio.core.persona_store.PersonaStore` 的订阅上，三条路径都会经过同一个
``_load_locked``（前两条主动调它，第三条在下一次 ``current()`` 时发现），一条都漏不掉。

为什么在 `app/` 而不是 `core/`
-----------------------------
分层方向 ``app → ws → services → db``：``core/persona_store.py`` 去 import ``ws/``
会成环（陷阱 #64）。所以 store 只负责"通知订阅者"，"把通知翻成 WS 帧"留在这里。

线程安全
--------
订阅回调可能在**任何**线程里被触发（worker 线程读到文件变了同样会 reload）。
``Hub.publish`` 是跨线程安全的（内部走 ``wake()`` → ``call_soon_threadsafe``，
陷阱 #77），所以这里不需要额外的锁，也不需要 ``to_thread``。
"""

from __future__ import annotations

from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.logging import get_logger
from studio.core.persona_store import PersonaChange, PersonaStore
from studio.core.proto import EventKind
from studio.ws.hub import Hub
from studio.ws.protocol import Channel, Envelope, FrameType

__all__ = ["PersonaBroadcaster"]

logger = get_logger("studio.app.persona_events")

#: 不值得广播的变更原因。
#:
#: ``initial`` = "本进程第一次把人物读进内存"。那一刻**什么都没变** —— 事件叫
#: ``persona_changed``，而"我刚学会你长什么样"不是变更。面板拿当前人物走
#: ``GET /api/v1/persona``（§04.5.8），不靠这条事件当快照用。
_SILENT_REASONS: Final[frozenset[str]] = frozenset({"initial"})


class PersonaBroadcaster:
    """把 ``PersonaStore`` 的变更通知翻成 ``system.persona_changed``（`lifespan` 起停）。

    :param store: 热重载仓库（**必须与面板用的那一份是同一个对象**，否则改了人物
        没有事件 —— 订阅的是它，读的也是它）
    :param hub: 推送中枢
    """

    def __init__(self, *, store: PersonaStore, hub: Hub) -> None:
        self._store = store
        self._hub = hub
        self._subscribed = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        """订阅（幂等：重复 start 只会挂一个监听器）。"""
        if self._subscribed:
            return
        self._store.subscribe(self._on_change)
        self._subscribed = True

    def stop(self) -> None:
        """退订。

        不摘掉的话，反复 `TestClient(...)`（每个用例一套 app）会把监听器叠成 N 份，
        于是一条人物变更广播 N 次 —— 而且 N 会随跑过的用例数增长。
        """
        if not self._subscribed:
            return
        self._store.unsubscribe(self._on_change)
        self._subscribed = False

    # ── 订阅回调（任意线程）─────────────────────────────────────────────

    def _on_change(self, change: PersonaChange) -> None:
        if change.reason in _SILENT_REASONS:
            return
        payload = self.payload_of(change)
        try:
            self._hub.publish(
                Envelope(type=FrameType.EVENT, channel=Channel.SYSTEM, ts=now_iso(), data=payload)
            )
        except Exception:  # 广播失败不能反过来把"人物加载成功"这件事搅黄
            logger.exception("persona.broadcast_failed", persona_id=payload["persona_id"])

    @staticmethod
    def payload_of(change: PersonaChange) -> dict[str, Any]:
        """§04.4.3 的载荷字段 + 三个"面板要判断该不该重画"的补充字段。

        ``source`` 取快照的来源（``active`` / ``library``），``error`` 只在
        ``failed`` 时有值 —— 规格里这两个字段本来就这么定的。
        """
        previous = change.previous
        return {
            "kind": EventKind.SYSTEM_PERSONA_CHANGED.value,
            "persona_id": change.new.persona_id,
            "name": change.new.config.name,
            "version": change.version,
            "sha256": change.new.sha256,
            "source": change.new.source,
            "error": change.error,
            # 补充字段：面板据此决定"要不要把整屏重拉一遍"
            "failed": change.failed,
            "reason": change.reason,
            "previous_persona_id": None if previous is None else previous.persona_id,
            "previous_version": None if previous is None else previous.version,
            "changed": not change.failed and (previous is None or previous.sha256 != change.new.sha256),
        }
