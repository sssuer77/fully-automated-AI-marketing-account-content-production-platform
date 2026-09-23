"""句级 TTS 缓存（T2.6 · §04.3.6）。

断言分三层，各管一件事
----------------------
1. **键**：同一份输入必须得到同一个键，任一字段变化必须换键。键错了不会报错，
   只会"悄悄复用别人的台词"或"每次都 miss 白花引擎调用"，所以这一层要逐字段钉。
2. **命中**：命中必须**不碰引擎**（这是 P2 性能前提的全部内容），未命中必须
   如实返回 ``None``（空文件不算命中）。
3. **淘汰**：先走 ``hits < 2``、组内先旧后新；**正在用的条目不能被自己淘汰掉**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

from studio.tts.cache import CACHE_LIMIT_BYTES, TtsCache, tts_cache_key


class KeyArgs(TypedDict):
    """``tts_cache_key`` 的一组参数。

    写成 ``TypedDict`` 而不是普通 ``dict``：逐字段钉的那几条用例全靠展开它，
    而 ``**dict[str, object]`` 会把"哪个字段该是什么类型"这件事整个抹掉 ——
    键的字段类型错了不会报错，只会静默换键。
    """

    engine: str
    engine_revision: str
    voice_id: str
    voice_fingerprint: str
    normalized_text: str
    speed: float
    emotion: str
    seed: int | None
    sample_rate: int


#: 一组基准字段（逐用例只改其中一项 ⇒ 差异必须体现在键上）。
BASE: KeyArgs = {
    "engine": "sapi",
    "engine_revision": "1",
    "voice_id": "Microsoft Huihui Desktop",
    "voice_fingerprint": "",
    "normalized_text": "今天聊三件事",
    "speed": 1.0,
    "emotion": "neutral",
    "seed": None,
    "sample_rate": 48_000,
}


def _key(**overrides: object) -> str:
    """基准字段 + 覆盖项 ⇒ 键。

    逐字段那几条用例全靠"只改一项"，而 ``tts_cache_key(**BASE, speed=1)`` 这种写法
    mypy 会判成"同一个关键字给了两次"（``**TypedDict`` 在它看来可能已经含 ``speed``）。
    所以覆盖项统一从这条窄缝进，``cast`` 是这里唯一的类型逃生门 —— 字段名写错
    由用例自己兜底（每个字段都有一条断言）。
    """
    return tts_cache_key(**cast(KeyArgs, {**BASE, **overrides}))


def _wav(path: Path, *, size: int = 64) -> Path:
    """造一个"像音频"的文件（缓存只关心字节数，不解析内容）。``size=0`` ⇒ 空文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"" if size == 0 else b"RIFF" + b"\x00" * (size - 4))
    return path


# ══════════════════════════════════════════════════════════════════════
# ① 键
# ══════════════════════════════════════════════════════════════════════


def test_key_is_stable_and_32_hex() -> None:
    key = _key()
    assert key == _key()
    assert len(key) == 32
    assert all(char in "0123456789abcdef" for char in key)


def test_key_treats_1_and_1_0_as_the_same_speed() -> None:
    """``1`` 与 ``1.0`` 是同一个语速 —— 否则"调用方怎么写这个数"会决定命不命中。"""
    assert _key(speed=1) == _key(speed=1.0)


def test_key_distinguishes_none_seed_from_zero_seed() -> None:
    """``None`` = 引擎自己挑，``0`` = 就用 0。两者编出来的音频不同，键必须不同。"""
    assert _key(seed=None) != _key(seed=0)


def test_every_field_changes_the_key() -> None:
    """逐字段钉：漏掉任何一个字段都会变成"改了它却复用旧音频"（§04.3.6 的失效口径）。"""
    baseline = _key()
    changes: dict[str, object] = {
        "engine": "cosyvoice",
        "engine_revision": "2",
        "voice_id": "Microsoft Yaoyao",
        "voice_fingerprint": "ab12cd34ef56",
        "normalized_text": "今天聊四件事",
        "speed": 1.1,
        "emotion": "开心",
        "seed": 7,
        "sample_rate": 24_000,
    }
    assert len(changes) == len(BASE) == 9, "键的字段少一个，这里就得少一条断言"
    for field, value in changes.items():
        assert _key(**{field: value}) != baseline, field


def test_the_same_name_with_new_reference_audio_is_a_new_key() -> None:
    """★ 同一个音色名、换了参考音 ⇒ 必须换键（2026-09-23 · 用户原话）。

    用户换参考音的做法是「删掉重传 / 勾覆盖重传」，而**目录名不变** —— 只按名字
    记结论的话，换了嗓子之后每一句都命中旧音频，而面板上一切正常（配音台写着
    「换音色成功」）。指纹是唯一能把这两次分开的东西。
    """
    assert _key(voice_fingerprint="aaaa1111bbbb") != _key(voice_fingerprint="cccc2222dddd")


def test_separator_cannot_be_forged_by_content() -> None:
    """分隔符撞车 = 假命中：``voice_id="a" + text="b"`` 不能与 ``voice_id="a|b"`` 同键。"""
    left = _key(voice_id="a", normalized_text="b")
    right = _key(voice_id="a|b", normalized_text="")
    assert left != right


# ══════════════════════════════════════════════════════════════════════
# ② 命中 / 未命中
# ══════════════════════════════════════════════════════════════════════


def test_miss_then_put_then_hit(tmp_path: Path) -> None:
    cache = TtsCache(tmp_path / "tts")
    source = _wav(tmp_path / "s001.wav", size=128)

    assert cache.get("deadbeef") is None

    stored = cache.put("deadbeef", source)
    assert stored == cache.path_for("deadbeef")
    assert stored.read_bytes() == source.read_bytes()

    hit = cache.get("deadbeef")
    assert hit == stored


def test_empty_file_is_not_a_hit(tmp_path: Path) -> None:
    """0 字节音频当命中的后果是"这一句在成片里没声音"，比重合成一次贵得多。"""
    cache = TtsCache(tmp_path / "tts")
    cache.path_for("empty").parent.mkdir(parents=True, exist_ok=True)
    cache.path_for("empty").write_bytes(b"")
    assert cache.get("empty") is None


def test_put_refuses_empty_source(tmp_path: Path) -> None:
    cache = TtsCache(tmp_path / "tts")
    empty = _wav(tmp_path / "s001.wav", size=0)
    cache.put("empty", empty)
    assert cache.get("empty") is None
    assert not cache.path_for("empty").exists()


def test_hits_are_persisted_and_accumulate(tmp_path: Path) -> None:
    cache = TtsCache(tmp_path / "tts")
    cache.put("k1", _wav(tmp_path / "s001.wav"))
    cache.get("k1")
    cache.get("k1")
    # 换一个实例（新进程）读同一份元数据 ⇒ 计数不能只活在内存里
    assert TtsCache(tmp_path / "tts").stats().hits == 2


def test_corrupt_meta_degrades_to_zero_hits(tmp_path: Path) -> None:
    """元数据读坏最坏只让这条缓存早点被淘汰 —— 不该让一次命中变成异常。"""
    cache = TtsCache(tmp_path / "tts")
    cache.put("k1", _wav(tmp_path / "s001.wav"))
    (tmp_path / "tts" / "k1.json").write_text("{ 这不是 JSON", encoding="utf-8")
    assert cache.get("k1") is not None
    assert json.loads((tmp_path / "tts" / "k1.json").read_text(encoding="utf-8"))["hits"] == 1


# ══════════════════════════════════════════════════════════════════════
# ③ 淘汰
# ══════════════════════════════════════════════════════════════════════


def test_prune_evicts_oldest_single_use_first(tmp_path: Path) -> None:
    """只被用过一次的条目先走（"这一版稿子里的独有句子"，改稿后再也用不上）。

    淘汰发生在 ``put`` 里（写入即收敛），所以这里断言的是**写完之后的盘面**，
    而不是某次 ``prune()`` 的返回值。
    """
    root = tmp_path / "tts"
    cache = TtsCache(root, limit_bytes=250)
    for index in range(3):
        cache.put(f"k{index}", _wav(tmp_path / f"s{index}.wav", size=100))

    assert not (root / "k0.wav").exists(), "300 字节超了 250 的线 ⇒ 最旧的那条走"
    assert not (root / "k0.json").exists(), "音频删了，元数据也得跟着走"
    assert (root / "k1.wav").is_file()
    assert (root / "k2.wav").is_file()
    assert cache.prune() == (), "已经在线下，再淘汰一次不该动任何东西"


def test_prune_spares_reused_entries(tmp_path: Path) -> None:
    """用过两次以上的条目是高频套话 ⇒ 淘汰时降权（§04.3.6）。"""
    root = tmp_path / "tts"
    cache = TtsCache(root, limit_bytes=250)
    cache.put("hot", _wav(tmp_path / "hot.wav", size=100))
    cache.get("hot")
    cache.get("hot")  # hits = 2 ⇒ 进"降权"那一档
    cache.put("cold", _wav(tmp_path / "cold.wav", size=100))
    cache.get("cold")  # hits = 1 ⇒ 该先走
    cache.put("newest", _wav(tmp_path / "newest.wav", size=100))

    assert not (root / "cold.wav").exists(), "hits=1 的冷条目先走"
    assert (root / "hot.wav").is_file(), "hits=2 的高频套话留下"
    assert (root / "newest.wav").is_file(), "刚写的条目不该被自己淘汰掉"


def test_prune_keeps_the_entry_it_just_wrote(tmp_path: Path) -> None:
    """刚写进来的那条 hits=0，但它是最新的 ⇒ 不能"刚存完就把自己淘汰掉"。"""
    root = tmp_path / "tts"
    cache = TtsCache(root, limit_bytes=100)
    cache.put("fresh", _wav(tmp_path / "fresh.wav", size=100))
    assert cache.prune() == ()
    assert (root / "fresh.wav").is_file()


def test_stats_reports_usage_against_the_limit(tmp_path: Path) -> None:
    cache = TtsCache(tmp_path / "tts", limit_bytes=1000)
    cache.put("k1", _wav(tmp_path / "s001.wav", size=100))
    stats = cache.stats()
    assert (stats.files, stats.bytes, stats.hits) == (1, 100, 0)
    assert stats.ratio == 0.1
    assert stats.to_dict()["limit_bytes"] == 1000


def test_default_limit_is_five_gib() -> None:
    """上限是 §04.3.6 写死的数（缓存不能把 D 盘吃光），不该被随手改小。"""
    assert CACHE_LIMIT_BYTES == 5 * 1024**3
    assert TtsCache(Path("unused")).limit_bytes == CACHE_LIMIT_BYTES


def test_put_into_missing_root_creates_it(tmp_path: Path) -> None:
    """缓存目录可能被人工清掉（data/tmp 被删过不止一次）⇒ 写之前自己建。"""
    root = tmp_path / "cache" / "tts"
    assert not root.exists()
    TtsCache(root).put("k1", _wav(tmp_path / "s001.wav"))
    assert (root / "k1.wav").is_file()
