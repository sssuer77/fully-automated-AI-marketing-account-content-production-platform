"""文件层原语（``core/files.py``）—— 指纹与变更探测键。

这一层之所以值得单测，是因为它服务的是**两个失败语义相反的调用方**：
热重载要"读不出来也别炸"（软失败 ⇒ 空串），素材入库要"读不出来就报错"
（没有指纹就没有幂等键）。两条路各测一条，别让它们悄悄长成一条。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from studio.core.files import HASH_CHUNK_BYTES, file_sha256, stat_key, stream_sha256


class TestFileSha256:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"hello")
        assert file_sha256(target) == hashlib.sha256(b"hello").hexdigest()

    def test_missing_file_is_an_empty_string(self, tmp_path: Path) -> None:
        assert file_sha256(tmp_path / "nope.txt") == ""


class TestStreamSha256:
    def test_matches_the_one_shot_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "clip.bin"
        payload = bytes(range(256)) * 5_000  # ~1.25 MB ⇒ 跨过多个分块
        target.write_bytes(payload)
        assert stream_sha256(target) == hashlib.sha256(payload).hexdigest()

    def test_chunk_size_does_not_change_the_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "clip.bin"
        target.write_bytes(b"x" * (HASH_CHUNK_BYTES + 7))
        assert stream_sha256(target, chunk_size=16) == stream_sha256(target)

    def test_empty_file_has_the_empty_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.bin"
        target.write_bytes(b"")
        assert stream_sha256(target) == hashlib.sha256(b"").hexdigest()

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        # 与 file_sha256 的软失败**故意不同**：入库没有指纹就等于没有幂等键
        with pytest.raises(OSError):
            stream_sha256(tmp_path / "nope.bin")


class TestStatKey:
    def test_changes_with_content(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one", encoding="utf-8")
        first = stat_key(target)
        target.write_text("two-longer", encoding="utf-8")
        assert stat_key(target) != first

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert stat_key(tmp_path / "nope.txt") is None
