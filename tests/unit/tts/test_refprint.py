"""参考音指纹（``tts/refprint.py`` · 2026-09-23）。

为什么单独钉这一个模块
----------------------
它的输出**进缓存键**，也是试听样本判 ``stale`` 的唯一判据。算错的后果分两种，
而两种都不报错：

- **该变没变**（换了参考音却算出同一个指纹）⇒ 配音一句不落地复用旧音频、
  试听继续放旧嗓子 —— 用户报的就是这个；
- **不该变却变了**（没换参考音却算出新指纹）⇒ 每一次都重念、每一个样本都作废，
  慢得莫名其妙，而且没人知道为什么。
"""

from __future__ import annotations

from pathlib import Path

from studio.tts.refprint import ref_fingerprint, voice_fingerprint


def _voice(root: Path, *, texts: tuple[str, ...], blobs: tuple[bytes, ...]) -> Path:
    """造一个音色目录：``ref_NN.wav`` 各写一段字节 + ``ref.txt`` 每行一段文本。"""
    root.mkdir(parents=True, exist_ok=True)
    for index, blob in enumerate(blobs, start=1):
        (root / f"ref_{index:02d}.wav").write_bytes(blob)
    (root / "ref.txt").write_text("\n".join(texts) + "\n", encoding="utf-8")
    return root


def test_the_same_audio_is_the_same_fingerprint(tmp_path: Path) -> None:
    root = _voice(tmp_path / "v", texts=("第一段。",), blobs=(b"aaaa",))
    assert ref_fingerprint(root) == ref_fingerprint(root)
    assert len(ref_fingerprint(root)) == 12


def test_new_audio_under_the_same_directory_is_a_new_fingerprint(tmp_path: Path) -> None:
    """★ 同名换参考音 ⇒ 指纹必须变（这条不成立，用户那条 bug 就还在）。"""
    root = _voice(tmp_path / "v", texts=("第一段。",), blobs=(b"aaaa",))
    before = ref_fingerprint(root)
    _voice(root, texts=("第一段。",), blobs=(b"bbbbbb",))
    assert ref_fingerprint(root) != before


def test_a_changed_transcript_is_a_new_fingerprint(tmp_path: Path) -> None:
    """逐字文本进 prompt（§04.3.1），所以它变了就是另一份参考音。"""
    root = _voice(tmp_path / "v", texts=("第一段。",), blobs=(b"aaaa",))
    before = ref_fingerprint(root)
    (root / "ref.txt").write_text("改了的那一行。\n", encoding="utf-8")
    assert ref_fingerprint(root) != before


def test_segments_without_text_do_not_count(tmp_path: Path) -> None:
    """只算「配得上文本的段」：多丢一段没有文本的进来**不算换了嗓子**。

    与 ``VoiceRegistry.resolve`` 同一条取舍 —— 那些段压根进不了引擎，把它们算进
    指纹会让"多丢一个文件"看起来像换了音色（键变、整篇重念，而声音一模一样）。
    """
    root = _voice(tmp_path / "v", texts=("第一段。",), blobs=(b"aaaa",))
    before = ref_fingerprint(root)
    (root / "ref_02.wav").write_bytes("多出来的一段".encode())
    assert ref_fingerprint(root) == before


def test_a_second_segment_with_its_own_line_counts(tmp_path: Path) -> None:
    """配上文本的第 2 段**要**算进去 —— 它真的会进 prompt。"""
    root = _voice(tmp_path / "v", texts=("第一段。",), blobs=(b"aaaa",))
    before = ref_fingerprint(root)
    _voice(root, texts=("第一段。", "第二段。"), blobs=(b"aaaa", b"bbbb"))
    assert ref_fingerprint(root) != before


def test_no_reference_audio_is_an_empty_fingerprint(tmp_path: Path) -> None:
    """算不出来就是空串：目录不在 / 没有 wav / 没有文本 —— 三种都不猜。"""
    assert ref_fingerprint(tmp_path / "没有这个目录") == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ref_fingerprint(empty) == ""
    no_text = tmp_path / "no_text"
    no_text.mkdir()
    (no_text / "ref_01.wav").write_bytes(b"aaaa")
    assert ref_fingerprint(no_text) == ""


def test_voice_fingerprint_reads_the_directory_named_after_the_id(tmp_path: Path) -> None:
    _voice(tmp_path / "bigbear", texts=("熊大。",), blobs=(b"aaaa",))
    assert voice_fingerprint(tmp_path, "bigbear") == ref_fingerprint(tmp_path / "bigbear")


def test_voice_fingerprint_refuses_ids_that_are_paths(tmp_path: Path) -> None:
    """带分隔符的 id 一律当「没有这个音色」：它会拼进路径，一个 ``..`` 就够越界。"""
    _voice(tmp_path / "bigbear", texts=("熊大。",), blobs=(b"aaaa",))
    assert voice_fingerprint(tmp_path, "../bigbear") == ""
    assert voice_fingerprint(tmp_path, "sub\\bigbear") == ""
    assert voice_fingerprint(tmp_path, "") == ""
