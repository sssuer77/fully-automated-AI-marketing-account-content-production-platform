"""浏览器上传的落盘（T4.8 的图形化入库入口 · §3.3.14 / §4.3.1）。

这一层最容易被写坏的两件事，用例就围着它们转：

① **路径逃逸**：``../../x.mp4`` 这种文件名不该有任何落点。这里断言的不是"过滤掉了
   ``..``"，而是**落点永远在素材根目录里面** —— 名字根本不参与拼路径，所以它是
   构造上成立的，不靠一条规则去挡；
② **静默改名 / 静默盖掉**：``跑酷 01.MP4`` 变成 ``parkour_01.mp4`` 是可以的，
   不说一声不行；同名文件被后传的覆盖掉更是**绝对不行**（那可能是一份手工剪过的
   片段）。这两条都有对应的用例。
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable

import pytest

from studio.assets.layout import AssetKind
from studio.assets.upload import (
    PARTIAL_SUFFIX,
    asset_id_for_upload,
    check_suffix,
    copy_into_place,
    ref_name,
    safe_basename,
    target_for,
    write_text_into_place,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths


def _stream(body: bytes = b"bytes") -> io.BytesIO:
    return io.BytesIO(body)


def _error(call: Callable[[], object]) -> StudioError:
    """调一次、期望它抛 ``StudioError``，把那个异常还回来（用例据此断言错误码）。"""
    with pytest.raises(StudioError) as caught:
        call()
    return caught.value


class TestAssetId:
    def test_already_legal_name_is_left_alone(self) -> None:
        assert asset_id_for_upload(AssetKind.BROLL, "parkour_001.mp4") == "parkour_001"
        assert asset_id_for_upload(AssetKind.BGM, "bgm_lofi.mp3") == "bgm_lofi"

    def test_prefix_is_added_when_missing(self) -> None:
        assert asset_id_for_upload(AssetKind.BROLL, "clip.mp4") == "parkour_clip"
        assert asset_id_for_upload(AssetKind.BGM, "song.mp3") == "bgm_song"

    def test_chinese_and_spaces_are_normalized(self) -> None:
        # 用户手里真实存在的名字长这样：``跑酷 01.MP4``
        assert asset_id_for_upload(AssetKind.BROLL, "跑酷 01.MP4") == "parkour_01"

    def test_all_non_ascii_falls_back_to_a_stable_hash(self) -> None:
        first = asset_id_for_upload(AssetKind.BROLL, "跑酷.mp4")
        second = asset_id_for_upload(AssetKind.BROLL, "跑酷.mp4")
        assert re.fullmatch(r"parkour_[0-9a-f]{8}", first), first
        # 稳定 = 同一个名字再传一次落到同一条上（否则 `overwrite` 永远撞不上）
        assert first == second
        assert first != asset_id_for_upload(AssetKind.BROLL, "跑酷集锦.mp4")

    def test_result_is_always_a_legal_id(self) -> None:
        for name in ("---.mp4", "a" * 200 + ".mp4", "  .mp4", "A_B-C.mp4"):
            value = asset_id_for_upload(AssetKind.BROLL, name)
            assert re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", value), (name, value)


class TestSafeBasename:
    def test_takes_the_last_segment(self) -> None:
        assert safe_basename("C:\\videos\\a.mp4") == "a.mp4"
        assert safe_basename("/tmp/a.mp4") == "a.mp4"
        assert safe_basename("  a.mp4  ") == "a.mp4"

    def test_empty_and_dot_names_are_refused(self) -> None:
        assert _error(lambda: safe_basename("..")).code is ErrorCode.ASSET_INVALID
        assert _error(lambda: safe_basename("   ")).code is ErrorCode.ASSET_INVALID

    def test_control_characters_are_refused(self) -> None:
        assert _error(lambda: safe_basename("a\nb.mp4")).code is ErrorCode.ASSET_INVALID


class TestSuffix:
    def test_whitelist_per_kind(self) -> None:
        assert check_suffix(AssetKind.BROLL, "a.MP4") == ".mp4"
        assert check_suffix(AssetKind.BGM, "a.mp3") == ".mp3"
        assert check_suffix(AssetKind.VOICE, "a.wav") == ".wav"

    def test_unknown_suffix_says_what_is_allowed(self) -> None:
        error = _error(lambda: check_suffix(AssetKind.BROLL, "notes.txt"))
        assert error.code is ErrorCode.ASSET_INVALID
        assert ".mp4" in str(error.remediation)

    def test_video_is_not_audio(self) -> None:
        assert _error(lambda: check_suffix(AssetKind.BGM, "a.mp4")).code is ErrorCode.ASSET_INVALID


class TestTargetFor:
    def test_target_never_leaves_the_asset_root(self, tmp_paths: StudioPaths) -> None:
        for name in ("../../evil.mp4", "..\\..\\evil.mp4", "/etc/passwd.mp4", "跑酷.mp4"):
            target = target_for(tmp_paths, AssetKind.BROLL, name)
            assert target.parent == tmp_paths.mc_parkour_dir, (name, target)
            assert target.suffix == ".mp4"

    def test_ref_name_is_two_digits_in_filename_order(self) -> None:
        assert ref_name(1, "b.WAV") == "ref_01.wav"
        assert ref_name(12, "x.mp3") == "ref_12.mp3"


class TestCopyIntoPlace:
    def test_writes_atomically_and_leaves_no_partial(self, tmp_paths: StudioPaths) -> None:
        target = tmp_paths.mc_parkour_dir / "parkour_001.mp4"
        assert copy_into_place(_stream(b"hello"), target, overwrite=False) == "stored"
        assert target.read_bytes() == b"hello"
        assert list(tmp_paths.mc_parkour_dir.glob(f"*{PARTIAL_SUFFIX}")) == []

    def test_existing_file_is_never_clobbered_silently(self, tmp_paths: StudioPaths) -> None:
        target = tmp_paths.mc_parkour_dir / "parkour_001.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"hand-edited")

        error = _error(lambda: copy_into_place(_stream(b"new"), target, overwrite=False))
        assert error.code is ErrorCode.ASSET_EXISTS
        assert target.read_bytes() == b"hand-edited"

        assert copy_into_place(_stream(b"new"), target, overwrite=True) == "replaced"
        assert target.read_bytes() == b"new"

    def test_a_failed_write_leaves_nothing_behind(self, tmp_paths: StudioPaths) -> None:
        target = tmp_paths.mc_parkour_dir / "parkour_001.mp4"

        class _Boom(io.BytesIO):
            def read(self, *_: object) -> bytes:
                raise OSError("磁盘满了")

        with pytest.raises(OSError):
            copy_into_place(_Boom(b"x"), target, overwrite=False)
        assert not target.exists()
        assert list(tmp_paths.mc_parkour_dir.iterdir()) == []

    def test_sidecar_text_follows_the_same_policy(self, tmp_paths: StudioPaths) -> None:
        target = tmp_paths.voice_src_dir / "bear_da" / "ref.txt"
        assert write_text_into_place(target, "第一句\n", overwrite=False) == "stored"
        assert target.read_text(encoding="utf-8") == "第一句\n"
        assert _error(lambda: write_text_into_place(target, "改", overwrite=False)).code is (
            ErrorCode.ASSET_EXISTS
        )
