"""句级 TTS 缓存（T2.6 · §04.3.6）—— "同一句话合成第二遍，引擎调用次数必须是 0"。

它解决的是什么
--------------
营销号文案的复用率高得离谱：开场白、口播套话、CTA 在几十条片子里反复出现。
没有缓存时，每重跑一次任务就要把整篇稿子再念一遍 —— 而"重跑"恰恰是日常
（改了第 7 句、换了个底片、上次跑到一半崩了）。缓存把这件事变成"只合成变了的
那一句"，也是 §04.3.6 说的 P2 性能前提。

键为什么是内容哈希，不是"句子编号"
----------------------------------
句子编号（``s007``）是**位置**，不是**内容**：改一句、删一句、插一句，后面所有
句子的编号含义都会平移。按编号缓存的话，"第 7 句"的音频会在稿件改过之后被
当成新第 7 句的产物复用 —— 听起来"命中了"，实际配的是别人的台词。
内容哈希让"命不命中"与位置无关：文本（归一化后）、音色、语速、情感、seed、
采样率、引擎版本任一变化即 miss（§04.3.6 的失效口径）。

键里的字段为什么必须含**归一化后**的文本
----------------------------------------
合成读的就是归一化后的文本（T2.5）。用原文做键会得到"键变了、念出来的字没变"
的假 miss（改个 emoji 就重合成一遍），以及反过来的假命中（词表热改后文本
该念法变了、键却没变 ⇒ 复用了旧读音）。所以键与合成必须吃同一份字符串。

为什么 LRU 上限是 5 GB，且 ``hits >= 2`` 的条目降权
--------------------------------------------------
上限是 §04.3.6 写死的（缓存不能把 D 盘吃光）。降权的理由与"复用率高"是一件事
的两面：**用过两次以上的条目正是那些高频套话**，淘汰它们等于淘汰最值钱的那部分；
而只被用过一次的条目大多是"这一版稿子里的独有句子"，下次改稿就再也用不上了。
淘汰顺序因此是"先 ``hits < 2``、组内先旧后新"，而不是纯 LRU。

元数据为什么用旁挂的 ``.json`` 而不是写进 DB
--------------------------------------------
缓存是**可丢弃**的东西（删掉只损失速度，不损失正确性），而 DB 里每一行都要有
迁移、备份、GC 的账。为一份能随时删掉的索引去动 schema 不划算。元数据读坏 /
写坏一律当"hits=0 的新条目"处理 —— 它最坏的结果只是这条缓存早点被淘汰。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.logging import get_logger

__all__ = [
    "CACHE_LIMIT_BYTES",
    "CacheEntry",
    "CacheStats",
    "TtsCache",
    "tts_cache_key",
]

logger = get_logger("studio.tts.cache")

#: 缓存目录上限（§04.3.6：LRU 上限 5 GB）。超过就淘汰到线下。
CACHE_LIMIT_BYTES: Final[int] = 5 * 1024**3

#: 缓存音频的后缀（与 ``TTSRequest.output_format`` 的默认档一致）
_AUDIO_SUFFIX: Final[str] = ".wav"

#: 旁挂元数据的后缀
_META_SUFFIX: Final[str] = ".json"

#: 键字段之间的分隔符。用 ``\x1f``（Unit Separator）而不是 ``|``：口播文本里
#: 出现一个竖线是完全可能的（"3|5 的比例"），而分隔符与内容撞车会让两组不同的
#: 字段拼出同一个字符串 ⇒ 假命中（复用别人的台词）。控制字符进不了归一化后的
#: 文本（T2.5 的步骤 1 专门删这类字符），所以它不会与内容撞车。
_FIELD_SEP: Final[str] = "\x1f"

#: 键的长度（§04.3.6：``sha256(...) → 32hex``）
_KEY_CHARS: Final[int] = 32

#: "用过两次以上"的门槛（降权判据）
_REUSE_HITS: Final[int] = 2


def tts_cache_key(
    *,
    engine: str,
    engine_revision: str,
    voice_id: str,
    normalized_text: str,
    speed: float,
    emotion: str,
    seed: int | None,
    sample_rate: int,
) -> str:
    """``sha256(engine|engine_revision|voice_id|text|speed|emotion|seed|sr) → 32hex``。

    ``speed`` 用 ``%g`` 格式化：``1.0`` 与 ``1`` 必须是同一个键，否则调用方传
    ``1`` 还是 ``1.0`` 会决定缓存命不命中 —— 这种"看心情"的失效最难查。
    ``seed=None`` 与 ``seed=0`` 必须区分（前者是"引擎自己挑"，后者是"就用 0"），
    所以 ``None`` 编成一个不会与整数撞车的字面量。
    """
    fields = (
        engine,
        engine_revision,
        voice_id,
        normalized_text,
        f"{speed:g}",
        emotion,
        "none" if seed is None else str(seed),
        str(sample_rate),
    )
    raw = _FIELD_SEP.join(fields).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:_KEY_CHARS]


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """缓存里的一个条目（音频 + 它的元数据）。"""

    key: str
    path: Path
    hits: int
    size_bytes: int
    mtime: float


@dataclass(frozen=True, slots=True)
class CacheStats:
    """缓存目录的现状（面板 / 观测用）。"""

    files: int
    bytes: int
    hits: int
    limit_bytes: int = CACHE_LIMIT_BYTES

    @property
    def ratio(self) -> float:
        """占用比例（0.0–1.0）。上限为 0 时按"满"处理，免得除零。"""
        if self.limit_bytes <= 0:
            return 1.0
        return self.bytes / self.limit_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "hits": self.hits,
            "limit_bytes": self.limit_bytes,
            "ratio": round(self.ratio, 4),
        }


class TtsCache:
    """``data/cache/tts/`` 的内容寻址缓存（**进程内一个实例**，跨单元复用）。

    :param root: 缓存目录（``StudioPaths.tts_cache_dir``）
    :param limit_bytes: 淘汰线（测试注入小值，生产用 :data:`CACHE_LIMIT_BYTES`）
    """

    def __init__(self, root: Path, *, limit_bytes: int = CACHE_LIMIT_BYTES) -> None:
        self._root = root
        self._limit = limit_bytes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def limit_bytes(self) -> int:
        return self._limit

    def path_for(self, key: str) -> Path:
        """键 ⇒ 音频路径（**纯函数**：不碰盘，不建目录）。"""
        return self._root / f"{key}{_AUDIO_SUFFIX}"

    def get(self, key: str) -> Path | None:
        """命中 ⇒ 返回缓存里的音频路径并记一次使用；未命中 / 空文件 ⇒ ``None``。

        空文件当未命中：它只可能来自"上次写到一半断电"，而把它当成命中会让
        下游拿到一段 0 秒的音频 —— 那种失败在成片里是"这一句没声音"，比重新
        合成一次贵得多。
        """
        path = self.path_for(key)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        self._bump(key, size_bytes=size)
        return path

    def put(self, key: str, source: Path) -> Path:
        """把刚合成好的音频收进缓存（**原子**：先写 ``.partial`` 再改名）。

        为什么用拷贝而不是硬链接：硬链接让"缓存淘汰"变成"悄悄删掉交付产物的一部分"
        —— 两者必须能各自独立地删。一段句子 WAV 只有几十到几百 KB，拷贝的代价
        远小于一次"为什么这句没声音"的排查。
        """
        target = self.path_for(key)
        if source.resolve() == target.resolve():
            # 已经在缓存里（调用方直接合成到缓存路径）⇒ 只补元数据。
            self._write_meta(key, hits=0, size_bytes=target.stat().st_size)
            return target
        size = self._copy_atomic(source, target)
        if size is None:  # 缓存写失败**不是**合成失败：源音频已经落盘了
            return target
        self._write_meta(key, hits=0, size_bytes=size)
        self.prune()
        return target

    def _copy_atomic(self, source: Path, target: Path) -> int | None:
        """``source`` 原子地拷成 ``target``，返回字节数；失败 / 空文件 ⇒ ``None``。"""
        self._root.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.partial")
        try:
            shutil.copyfile(source, partial)
            size = partial.stat().st_size
            partial.replace(target)
        except OSError as exc:
            logger.warning("tts.cache_put_failed", target=target.name, error=str(exc))
            partial.unlink(missing_ok=True)
            return None
        if size <= 0:
            # 空文件不是"缓存坏了"而是"上游给了一段没声音的音频"：留着它会让下游
            # 把 0 秒当成命中（见 `get` 的注释），所以这里就地撤掉并如实告警。
            logger.warning("tts.cache_source_empty", source=source.as_posix())
            target.unlink(missing_ok=True)
            return None
        return size

    def entries(self) -> list[CacheEntry]:
        """现有条目（按"淘汰优先级"排序：先 ``hits < 2``、组内先旧后新）。"""
        items: list[CacheEntry] = []
        for path in self._audio_files():
            try:
                stat = path.stat()
            except OSError:  # 遍历中途被别的进程删了 ⇒ 跳过
                continue
            meta = self._read_meta(path.stem)
            items.append(
                CacheEntry(
                    key=path.stem,
                    path=path,
                    hits=int(meta.get("hits", 0)),
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        items.sort(key=lambda item: (item.hits >= _REUSE_HITS, item.mtime))
        return items

    def prune(self) -> tuple[str, ...]:
        """淘汰到上限之下，返回被删的键（**从后往前删**：最该走的先走）。"""
        items = self.entries()
        total = sum(item.size_bytes for item in items)
        if total <= self._limit:
            return ()
        evicted: list[str] = []
        # ``entries()`` 已经按"最该走的排最前"排好 ⇒ 顺着删就是淘汰优先级。
        # 反过来删会先把高频套话（hits >= 2）干掉，正好把降权的意义抹掉。
        for item in items:
            if total <= self._limit:
                break
            try:
                item.path.unlink()
            except OSError as exc:  # 删不掉就留着：缓存多占一点不是错误
                logger.warning("tts.cache_evict_failed", key=item.key, error=str(exc))
                continue
            self._meta_path(item.key).unlink(missing_ok=True)
            total -= item.size_bytes
            evicted.append(item.key)
        if evicted:
            logger.info("tts.cache_pruned", evicted=len(evicted), bytes_left=total)
        return tuple(evicted)

    def stats(self) -> CacheStats:
        """缓存现状（文件数 / 字节数 / 累计命中次数）。"""
        items = self.entries()
        return CacheStats(
            files=len(items),
            bytes=sum(item.size_bytes for item in items),
            hits=sum(item.hits for item in items),
            limit_bytes=self._limit,
        )

    # ── 内部 ────────────────────────────────────────────────────────────

    def _audio_files(self) -> Iterator[Path]:
        if not self._root.is_dir():
            return
        yield from self._root.glob(f"*{_AUDIO_SUFFIX}")

    def _meta_path(self, key: str) -> Path:
        return self._root / f"{key}{_META_SUFFIX}"

    def _read_meta(self, key: str) -> dict[str, Any]:
        """读元数据；文件不在 / 读不动 / 不是对象 ⇒ 空字典（**当新条目**）。"""
        try:
            raw = self._meta_path(key).read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_meta(self, key: str, *, hits: int, size_bytes: int) -> None:
        payload = {"hits": hits, "bytes": size_bytes, "created_at": now_iso()}
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._meta_path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n"
            )
        except OSError as exc:
            logger.warning("tts.cache_meta_failed", key=key, error=str(exc))

    def _bump(self, key: str, *, size_bytes: int) -> None:
        """记一次命中（``hits + 1``）并刷新 mtime —— 后者是"组内先旧后新"的依据。"""
        meta = self._read_meta(key)
        hits = int(meta.get("hits", 0)) + 1
        self._write_meta(key, hits=hits, size_bytes=size_bytes)
