"""参考音指纹 —— 音色的身份不止是**名字**，还有那几段原声的**内容**。

为什么需要它
------------
``data/voice_src/<id>/`` 里的参考音是**用户可以随时换掉**的（删掉重传、勾「覆盖同名」
重传，见裁定 381），而换的时候目录名通常**不变** —— 人就是想把这个位置换成另一段
原声，不是想多出一个音色。于是凡是"**按音色名记住一个结论**"的地方，都会在换了
参考音之后继续交出**旧的**结论：

- **试听样本**：文件名 = ``f(名字)`` ⇒ 面板说"样本好了"，放出来还是旧嗓子；
- **配音缓存**：键 = ``f(名字, 文本, 引擎, …)`` ⇒ 命中旧音频，一句都不重念。

两处的症状一模一样，而且**都不报错**。修法是同一个：把参考音的内容算成一个指纹，
让它进这两处的身份 —— 名字没变、内容变了 ⇒ 指纹变 ⇒ 键变 / 结论失效。

为什么是内容而不是 mtime / 大小
--------------------------------
指纹要回答的是"**这声音还是不是同一个人**"，而 mtime 回答的是"这个文件动过没有"
（`touch` 一下就会变，而嗓子一点没变）。所以指纹按**内容**算；mtime 只用来做
**记忆化**的钥匙（同一个文件不必重复哈希），见 :func:`_fingerprint`。

为什么只算"配得上文本的那几段"
------------------------------
与 :meth:`~studio.tts.server.VoiceRegistry.resolve` 用的是**同一条**取舍：prompt 文本
必须与 prompt 音频逐字对应，多出来的段**进不了引擎**。把没进引擎的段也算进指纹，
会让"多丢一段进来"看起来像换了嗓子（键变了、整篇重念一遍），而声音一模一样。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Final

__all__ = ["ref_fingerprint", "voice_fingerprint"]

#: 指纹的长度（12 hex）。它只需要在"同一台机器上的几十个音色"之间区分，不需要抗
#: 碰撞攻击 —— 与 ``preview_slug`` 末尾那段 sha1 是同一个量级。
_CHARS: Final[int] = 12

#: 段的读法（与 ``VoiceRegistry.resolve`` 同一条：``ref_*.wav`` 按名字升序）。
_REF_GLOB: Final[str] = "ref_*.wav"

#: 逐字文本的文件名（§04.3.1）。
_TEXT_NAME: Final[str] = "ref.txt"

#: 哈希的分块大小。参考音可以几十 MB，整份读进内存没有意义。
_CHUNK: Final[int] = 1024 * 1024

#: 字段分隔符。与 ``tts.cache`` 同一条理由：用控制字符，避免内容里的可见字符
#: （竖线、空格）把两组不同的段拼成同一个串 ⇒ 假指纹（换了嗓子却判成没换）。
_SEP: Final[str] = "\x1f"


def _digest(path: Path) -> str:
    """文件内容的 ``sha256``（分块读）。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _ref_texts(path: Path) -> tuple[str, ...]:
    """``ref.txt`` 的**全部非空行**（第 N 行 ↔ 第 N 段参考音）。

    与 ``tts/server.py::_ref_lines`` 逐字同一条规则。为什么不共用一个函数：那个在
    tts 子进程的入口模块里，而这个模块要被 API 进程（试听）与池进程（配音）一起
    导入 —— 让池进程去 import 服务入口会把它拖进一堆推理依赖里。
    """
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _signature(root: Path) -> tuple[tuple[str, int, int], ...] | None:
    """记忆化的钥匙：参与指纹的那些文件的 ``(名字, 大小, mtime_ns)``。

    没有可用的参考音（目录不在 / 没有 wav / 没有文本）⇒ ``None``。**这就是"算不出
    指纹"的那一种情况**：调用方拿到空串，按"没有内容可认"处理，而不是猜一个。
    """
    wavs = sorted(root.glob(_REF_GLOB)) if root.is_dir() else []
    lines = _ref_texts(root / _TEXT_NAME)
    usable = min(len(wavs), len(lines))
    if usable == 0:
        return None
    parts: list[tuple[str, int, int]] = []
    for path in (*wavs[:usable], root / _TEXT_NAME):
        try:
            stat = path.stat()
        except OSError:
            return None
        parts.append((path.name, stat.st_size, stat.st_mtime_ns))
    return tuple(parts)


@lru_cache(maxsize=256)
def _fingerprint(root_str: str, signature: tuple[tuple[str, int, int], ...]) -> str:
    """真算一次，之后按 :func:`_signature` 记忆化。

    **钥匙是 (大小, mtime)**：任何真的改动都会动这两个数之一，于是下次重新哈希。
    代价写在明处：把文件换成**另一份大小与 mtime 都完全相同**的（``copy2`` 保留了
    时间戳），这里会认成没换 —— 而参考音的换法是"重传/覆盖"，走的是新文件的
    mtime，不会撞上这一条。
    """
    root = Path(root_str)
    wavs = sorted(root.glob(_REF_GLOB))
    lines = _ref_texts(root / _TEXT_NAME)
    usable = min(len(wavs), len(lines))
    fields: list[str] = []
    for index in range(usable):
        fields.append(wavs[index].name)
        fields.append(_digest(wavs[index]))
        fields.append(lines[index])
    return hashlib.sha256(_SEP.join(fields).encode("utf-8")).hexdigest()[:_CHARS]


def ref_fingerprint(root: Path) -> str:
    """一个音色目录 ⇒ 参考音指纹（没有可用的参考音 ⇒ ``""``）。"""
    root = Path(root)
    signature = _signature(root)
    if signature is None:
        return ""
    return _fingerprint(str(root), signature)


def voice_fingerprint(voice_src_dir: Path, voice_id: str) -> str:
    """``(data/voice_src, 音色 id)`` ⇒ 参考音指纹。

    系统音色（SAPI）**没有**参考音目录 ⇒ 空串：它的身份就是那个名字，而名字背后的
    东西由 ``engine_revision`` 守着（§04.3.6 的缓存键里已经有它）。

    ``voice_id`` 里带路径分隔符的一律当"没有这个音色"（空串）：它会被拼进路径，而
    这个函数的调用方拿的是用户填的值 —— 一个 ``..`` 就能让它去哈希别人的目录。
    """
    if not voice_id or "/" in voice_id or "\\" in voice_id:
        return ""
    return ref_fingerprint(Path(voice_src_dir) / voice_id)
