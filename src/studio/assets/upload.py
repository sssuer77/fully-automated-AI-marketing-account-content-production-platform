"""浏览器上传的落盘（T4.8 · §3.3.14 / §4.3.1）。

这一层只干一件事：**把浏览器递过来的字节，变成一个符合目录约定的文件**。
它不回答"这个素材够不够格"—— 那是 :mod:`studio.assets.validate` 的事：上传完
立刻调一次 ``AssetService.ingest`` 就能拿到**同一份**体检结论（判定只有一份，
不在上传这条路上再抄一遍阈值）。

三条纪律
--------
1. **原始文件名一个字符都不进路径**。浏览器给的名字只用来①取扩展名、②推素材 id；
   真正的落盘路径由 id 拼出来。于是 ``../../x.mp4`` 这种名字**在构造上**就没有落点，
   不需要一条"过滤 ``..``"的规则去挡它。
2. **改名不静默**。``跑酷 01.MP4`` 会变成 ``parkour_01.mp4`` —— 这个名字会出现在
   响应里、出现在面板的逐条结果里。用户看得见，就不叫静默。
3. **先写 ``.partial`` 再原子改名**。半截文件留在素材目录里是最坏的一种状态：
   扫盘会把它当成一条坏素材，而用户以为自己传完了（与 ``voice_preview`` 同一手法）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO, Final, Literal

from studio.assets.layout import (
    ASSET_ID_PATTERN,
    AUDIO_SUFFIXES,
    REF_STEM_PATTERN,
    AssetKind,
    prefix_for,
    root_for,
    suffixes_for,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths

__all__ = [
    "COPY_CHUNK_BYTES",
    "MAX_ID_LENGTH",
    "PARTIAL_SUFFIX",
    "WriteStatus",
    "asset_id_for_upload",
    "check_suffix",
    "copy_into_place",
    "prune_refs",
    "ref_name",
    "safe_basename",
    "suffix_of",
    "target_for",
    "write_text_into_place",
]

#: 一次读多大（上传是"人在等"的动作：1 MB 一块足够快，也足够省内存）
COPY_CHUNK_BYTES: Final[int] = 1 << 20

#: 落盘途中的临时后缀。``.partial`` 不在任何一类素材的后缀白名单里 ——
#: 万一残留，它会作为 stray 被**如实报出来**，而不是被当成一条素材。
PARTIAL_SUFFIX: Final[str] = ".partial"

#: ``ASSET_ID_PATTERN`` 的长度上限（64）。超了要截断，否则 id 直接不合法。
MAX_ID_LENGTH: Final[int] = 64

#: 连续的非 ``[a-z0-9_-]`` 字符（中文、空格、括号、emoji…）一律压成一个 ``_``
_SLUG_RUN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_\-]+")

#: 一次落盘的结局。``skipped`` **不在其中**：没写进去的文件根本走不到这里 ——
#: 它由路由层按"为什么没写"补上（扩展名不对 / 撞名 / 写盘失败）。
type WriteStatus = Literal["stored", "replaced"]


def safe_basename(raw: str) -> str:
    """浏览器给的文件名 ⇒ 最后一段。

    老浏览器会把完整路径塞进 ``filename``（``C:\\x\\y.mp4``），有些客户端用反斜杠。
    取最后一段**不是**为了安全（路径本来就不由它拼），而是为了让报错信息里那句话
    是文件名，而不是一整条路径。
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {"", ".", ".."}:
        raise StudioError(
            f"文件名不合法：{raw!r}",
            code=ErrorCode.ASSET_INVALID,
            context={"filename": raw},
            remediation="换一个正常的文件名再传",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise StudioError(
            f"文件名里有控制字符：{raw!r}",
            code=ErrorCode.ASSET_INVALID,
            context={"filename": raw},
            remediation="把文件名里的换行 / 制表符去掉再传",
        )
    return name


def suffix_of(filename: str) -> str:
    """扩展名（小写，含点）。``A.MP4`` 与 ``a.mp4`` 是同一类。"""
    return Path(safe_basename(filename)).suffix.lower()


def check_suffix(kind: AssetKind, filename: str) -> str:
    """扩展名必须在白名单里 —— 不在就**明说**，不静默忽略。

    "传了 3 个文件、只进去 2 个、没有任何提示"是这类面板最招人恨的一种行为，
    所以这里返回的每一条拒绝都会出现在逐条结果里。
    """
    suffix = suffix_of(filename)
    allowed = suffixes_for(kind)
    if suffix not in allowed:
        raise StudioError(
            f"{safe_basename(filename)} 的扩展名不属于这一类：{suffix or '（没有扩展名）'}",
            code=ErrorCode.ASSET_INVALID,
            context={"filename": filename, "suffix": suffix, "allowed": sorted(allowed)},
            remediation="只认 " + " / ".join(sorted(allowed)),
        )
    return suffix


def asset_id_for_upload(kind: AssetKind, filename: str) -> str:
    """浏览器文件名 ⇒ 合法的素材 id。

    ``跑酷 01.MP4`` ⇒ ``parkour_01``：小写 → 非法字符压成 ``_`` → 补上这一类的前缀。
    全非 ASCII 的名字（``跑酷.mp4``）退化成 ``parkour_<8 位哈希>`` —— 仍然合法、
    而且**稳定**（同一个文件名再传一次落到同一条上，``overwrite`` 才有意义）。

    绝不猜的是"这属于哪一类"：类别由 ``kind`` 决定，名字只用来出 id。
    """
    name = safe_basename(filename)
    slug = _SLUG_RUN.sub("_", Path(name).stem.lower()).strip("_-")
    if slug == "":
        slug = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    prefix = prefix_for(kind)
    if prefix and not slug.startswith(prefix):
        slug = f"{prefix}{slug}"
    candidate = slug[:MAX_ID_LENGTH].rstrip("_-")
    if re.fullmatch(ASSET_ID_PATTERN, candidate) is None:
        raise StudioError(
            f"文件名里挑不出合法的素材 id：{name}",
            code=ErrorCode.ASSET_INVALID,
            context={"filename": name, "candidate": candidate},
            remediation="改成 parkour_xxx.mp4 / bgm_xxx.mp3 这种形状再传",
        )
    return candidate


def target_for(paths: StudioPaths, kind: AssetKind, filename: str) -> Path:
    """平铺素材（跑酷 / BGM）该写到哪 —— **路径完全由 id 拼出来**。"""
    return root_for(paths, kind) / f"{asset_id_for_upload(kind, filename)}{suffix_of(filename)}"


def ref_name(index: int, filename: str) -> str:
    """音色的第 N 段参考音叫什么（``ref_01.wav`` …）。两位数是契约的一部分：
    排序即顺序，而 ``ref.txt`` 的第 N 行对应的就是第 N 段。"""
    return f"ref_{index:02d}{suffix_of(filename)}"


def prune_refs(root: Path, keep: Iterable[str]) -> tuple[str, ...]:
    """覆盖上传之后，把**这次没写到**的 ``ref_NN.*`` 清掉，返回被清掉的文件名。

    为什么覆盖要连带清掉多余的段（裁定 381）
    --------------------------------------
    "覆盖同名"如果只管同名的那几个，结果会是一份**两边都不是**的目录：新传了 2 段、
    旧的 ``ref_03.wav`` 还在盘上 ⇒ 库里报 3 段、引擎把三段拼起来当 prompt，而用户
    以为自己只留了 2 段。用户看到的现象就是**"覆盖没生效"** —— 他说的没错，
    覆盖确实没覆盖完。所以这里把语义定成**镜像**：这次传进来的就是全部，其余的段
    一律清掉，并且**逐条报出来**（静默清理比不清理更难查）。

    三条守卫
    --------
    ① 只认 ``ref_NN`` + 音频扩展名 —— ``ref.txt`` / ``profile.json`` / 用户自己塞的
       其它东西一个都不动（那不是我们建的）；
    ② 只在**本次确实写进去过**的时候才清（由调用方保证）：一次全被跳过 / 全失败的上传
       不该把用户现有的音色清空；
    ③ 目录必须真的存在，且 ``keep`` 是名字而不是路径 —— 拼路径的活只在这一处。
    """
    if not root.is_dir():
        return ()
    kept = set(keep)
    doomed = [
        item
        for item in sorted(root.iterdir(), key=lambda path: path.name)
        if item.is_file()
        and REF_STEM_PATTERN.fullmatch(item.stem) is not None
        and item.suffix.lower() in AUDIO_SUFFIXES
        and item.name not in kept
    ]
    removed: list[str] = []
    for item in doomed:
        item.unlink()
        removed.append(item.name)
    return tuple(removed)


def copy_into_place(source: BinaryIO, target: Path, *, overwrite: bool) -> WriteStatus:
    """把上传流写进 ``target``；返回 ``stored`` / ``replaced``。

    目标已存在且 ``overwrite=False`` ⇒ 抛 ``ASSET_EXISTS``（409）：**绝不静默盖掉
    用户已有的素材** —— 那可能是一份手工剪过的片段，重传一次就没了。
    """
    return _atomic(target, lambda handle: _drain(source, handle), overwrite=overwrite)


def write_text_into_place(target: Path, text: str, *, overwrite: bool) -> WriteStatus:
    """写一个文本旁车文件（``ref.txt``）—— 与素材走**同一套**存在性 / 原子策略。

    ``ref.txt`` 与参考音是两件东西：传参考音时它多半还不存在，所以正常路径是
    ``stored``；第二次传同一批参考音时它会撞上存在性检查，那时该做的事是
    勾「覆盖同名」，而不是被悄悄改掉。
    """

    def _write(handle: BinaryIO) -> None:
        handle.write(text.encode("utf-8"))

    return _atomic(target, _write, overwrite=overwrite)


def _drain(source: BinaryIO, handle: BinaryIO) -> None:
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            return
        handle.write(chunk)


def _atomic(target: Path, write: Callable[[BinaryIO], None], *, overwrite: bool) -> WriteStatus:
    """先写 ``.partial``、写完原子改名（``os.replace`` 同卷上是原子的）。

    失败（磁盘满 / 连接断了 / 中途取消）时把半截文件删掉：素材目录里出现一个
    半个 mp4，比这次上传直接失败难查得多。
    """
    existed = target.exists()
    if existed and not overwrite:
        raise StudioError(
            f"{target.name} 已经在了",
            code=ErrorCode.ASSET_EXISTS,
            context={"target": str(target)},
            remediation="勾上「覆盖同名」再传一次（会替换掉现在这个文件）",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}{PARTIAL_SUFFIX}")
    try:
        with partial.open("wb") as handle:
            write(handle)
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return "replaced" if existed else "stored"
