"""素材扫盘发现（T4.8.1 · §3.3.14 / §4.3.1）。

这一层只回答"磁盘上有什么"，所以用例全部围绕**命名约定**与 **strays**：
用户最常遇到的失败不是"文件坏了"，而是"我明明放进去了，面板上却没有"。
每个 stray 用例都是一句这种抱怨的可执行版本。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.assets.layout import (
    AUDIO_SUFFIXES,
    VIDEO_SUFFIXES,
    AssetCandidate,
    AssetKind,
    asset_id_for,
    discover,
    discover_all,
    prefix_for,
    root_for,
    suffixes_for,
    voice_profile,
    voice_refs,
    voice_text,
)
from studio.core.paths import StudioPaths


def _write(path: Path, body: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _voice_candidate(root: Path, names: list[str]) -> AssetCandidate:
    return AssetCandidate(
        kind=AssetKind.VOICE, id="probe", path=root, files=tuple(root / name for name in names)
    )


class TestRoots:
    def test_root_for_each_kind(self, tmp_paths: StudioPaths) -> None:
        assert root_for(tmp_paths, AssetKind.BROLL) == tmp_paths.mc_parkour_dir
        assert root_for(tmp_paths, AssetKind.BGM) == tmp_paths.bgm_dir
        assert root_for(tmp_paths, AssetKind.VOICE) == tmp_paths.voice_src_dir

    def test_root_names_match_the_frozen_contract(self, tmp_paths: StudioPaths) -> None:
        assert tmp_paths.mc_parkour_dir.name == "mc_parkour"
        assert tmp_paths.bgm_dir.name == "bgm"
        assert tmp_paths.voice_src_dir.name == "voice_src"

    def test_prefix_per_kind(self) -> None:
        assert prefix_for(AssetKind.BROLL) == "parkour_"
        assert prefix_for(AssetKind.BGM) == "bgm_"
        assert prefix_for(AssetKind.VOICE) == ""

    def test_suffixes_per_kind(self) -> None:
        assert suffixes_for(AssetKind.BROLL) is VIDEO_SUFFIXES
        assert suffixes_for(AssetKind.BGM) is AUDIO_SUFFIXES
        assert suffixes_for(AssetKind.VOICE) is AUDIO_SUFFIXES


class TestAssetId:
    @pytest.mark.parametrize(
        ("kind", "name", "expected"),
        [
            (AssetKind.BROLL, "parkour_017.mp4", "parkour_017"),
            (AssetKind.BGM, "bgm_003.mp3", "bgm_003"),
            (AssetKind.VOICE, "bigbear", "bigbear"),
            (AssetKind.VOICE, "big-bear_2", "big-bear_2"),
            # 前缀不对：不是素材，是别人放进来的东西
            (AssetKind.BROLL, "clip_017.mp4", None),
            (AssetKind.BGM, "parkour_003.mp3", None),
            # 大小写：id 进 SQL / URL / 文件名，两套大小写就是两个 id
            (AssetKind.BROLL, "Parkour_017.mp4", None),
            (AssetKind.VOICE, "BigBear", None),
            # 非 ASCII / 空格 / 前导下划线
            (AssetKind.VOICE, "xiongda", "xiongda"),
            (AssetKind.VOICE, "_hidden", None),
            (AssetKind.VOICE, "-leading", None),
            (AssetKind.BROLL, "parkour_0 17.mp4", None),
            # 长度：1 + 63 = 64 是上限
            (AssetKind.VOICE, "a" * 64, "a" * 64),
            (AssetKind.VOICE, "a" * 65, None),
        ],
    )
    def test_id_whitelist(self, kind: AssetKind, name: str, expected: str | None) -> None:
        assert asset_id_for(kind, Path(name)) == expected

    def test_id_is_the_file_stem_not_a_second_namespace(self) -> None:
        # 不去前缀：面板上显示的、日志里写的、broll_usage 里存的必须一眼对得上
        assert asset_id_for(AssetKind.BROLL, Path("parkour_017.mp4")) == "parkour_017"

    def test_parent_traversal_is_rejected(self) -> None:
        assert asset_id_for(AssetKind.VOICE, Path("..")) is None
        assert asset_id_for(AssetKind.VOICE, Path("a/../..")) is None


class TestDiscoverFlat:
    def test_missing_root_is_not_the_same_as_empty_root(self, tmp_paths: StudioPaths) -> None:
        result = discover(tmp_paths, AssetKind.BROLL)
        assert result.root_missing is True
        assert result.candidates == ()
        assert result.strays == ()
        assert result.root == tmp_paths.mc_parkour_dir

    def test_existing_but_empty_root(self, tmp_paths: StudioPaths) -> None:
        tmp_paths.mc_parkour_dir.mkdir(parents=True, exist_ok=True)
        result = discover(tmp_paths, AssetKind.BROLL)
        assert result.root_missing is False
        assert result.candidates == ()
        assert result.strays == ()

    def test_candidates_are_sorted_by_name(self, tmp_paths: StudioPaths) -> None:
        _write(tmp_paths.mc_parkour_dir / "parkour_002.mp4")
        _write(tmp_paths.mc_parkour_dir / "parkour_001.mov")
        result = discover(tmp_paths, AssetKind.BROLL)
        assert [item.id for item in result.candidates] == ["parkour_001", "parkour_002"]
        assert result.strays == ()

    def test_candidate_carries_kind_and_primary_file(self, tmp_paths: StudioPaths) -> None:
        target = _write(tmp_paths.mc_parkour_dir / "parkour_001.mp4")
        candidate = discover(tmp_paths, AssetKind.BROLL).candidates[0]
        assert candidate.kind is AssetKind.BROLL
        assert candidate.path == target
        assert candidate.files == (target,)
        assert candidate.primary == target

    def test_strays_are_reported_not_silently_dropped(self, tmp_paths: StudioPaths) -> None:
        root = tmp_paths.mc_parkour_dir
        _write(root / "parkour_001.mp4")
        wrong_prefix = _write(root / "clip_001.mp4")
        wrong_suffix = _write(root / "parkour_002.txt")
        nested = root / "archive"
        _write(nested / "parkour_003.mp4")
        result = discover(tmp_paths, AssetKind.BROLL)
        assert [item.id for item in result.candidates] == ["parkour_001"]
        assert set(result.strays) == {wrong_prefix, wrong_suffix, nested}

    def test_uppercase_extension_is_still_this_kind(self, tmp_paths: StudioPaths) -> None:
        _write(tmp_paths.bgm_dir / "bgm_001.MP3")
        result = discover(tmp_paths, AssetKind.BGM)
        assert [item.id for item in result.candidates] == ["bgm_001"]

    def test_bgm_and_broll_do_not_mix(self, tmp_paths: StudioPaths) -> None:
        _write(tmp_paths.mc_parkour_dir / "parkour_001.mp4")
        _write(tmp_paths.bgm_dir / "bgm_001.mp3")
        assert [item.id for item in discover(tmp_paths, AssetKind.BROLL).candidates] == ["parkour_001"]
        assert [item.id for item in discover(tmp_paths, AssetKind.BGM).candidates] == ["bgm_001"]


class TestDiscoverVoice:
    def test_one_directory_per_voice(self, tmp_paths: StudioPaths) -> None:
        root = tmp_paths.voice_src_dir
        _write(root / "bigbear" / "ref_01.wav")
        _write(root / "bigbear" / "ref_02.wav")
        _write(root / "bigbear" / "ref.txt", "第一段\n第二段\n")
        _write(root / "bigbear" / "profile.json", "{}")
        _write(root / "bigbear" / "notes.md", "随手记")
        result = discover(tmp_paths, AssetKind.VOICE)
        assert [item.id for item in result.candidates] == ["bigbear"]
        candidate = result.candidates[0]
        assert candidate.path == root / "bigbear"
        assert candidate.kind is AssetKind.VOICE
        assert len(candidate.files) == 5
        assert voice_text(candidate) == root / "bigbear" / "ref.txt"
        assert voice_profile(candidate) == root / "bigbear" / "profile.json"

    def test_loose_files_and_nested_dirs_are_strays(self, tmp_paths: StudioPaths) -> None:
        root = tmp_paths.voice_src_dir
        loose = _write(root / "ref_01.wav")
        _write(root / "bigbear" / "ref_01.wav")
        nested = root / "bigbear" / "extra"
        _write(nested / "ref_02.wav")
        result = discover(tmp_paths, AssetKind.VOICE)
        assert [item.id for item in result.candidates] == ["bigbear"]
        assert set(result.strays) == {loose, nested}
        # 不递归：嵌套目录里的文件不算这个音色的
        assert [item.name for item in result.candidates[0].files] == ["ref_01.wav"]

    def test_bad_directory_name_is_a_stray(self, tmp_paths: StudioPaths) -> None:
        root = tmp_paths.voice_src_dir
        _write(root / "Big Bear" / "ref_01.wav")
        result = discover(tmp_paths, AssetKind.VOICE)
        assert result.candidates == ()
        assert result.strays == (root / "Big Bear",)

    def test_missing_sidecars_read_as_none(self, tmp_paths: StudioPaths) -> None:
        _write(tmp_paths.voice_src_dir / "bigbear" / "ref_01.wav")
        candidate = discover(tmp_paths, AssetKind.VOICE).candidates[0]
        assert voice_text(candidate) is None
        assert voice_profile(candidate) is None


class TestVoiceRefs:
    def test_sorted_by_name_because_order_is_the_contract(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_02.wav", "ref_01.wav", "ref.txt"])
        assert [item.name for item in voice_refs(candidate)] == ["ref_01.wav", "ref_02.wav"]

    def test_only_two_digit_ref_stems(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(
            tmp_path,
            ["ref_1.wav", "ref_01.wav", "ref_100.wav", "ref_01.mp4", "ref_01.wav.bak"],
        )
        assert [item.name for item in voice_refs(candidate)] == ["ref_01.wav"]

    def test_extension_case_does_not_matter(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.MP3", "ref_02.Flac"])
        assert [item.name for item in voice_refs(candidate)] == ["ref_01.MP3", "ref_02.Flac"]

    def test_primary_falls_back_to_the_directory(self, tmp_path: Path) -> None:
        empty = _voice_candidate(tmp_path, ["ref.txt"])
        assert empty.primary == tmp_path
        with_ref = _voice_candidate(tmp_path, ["ref_02.wav", "ref_01.wav"])
        assert with_ref.primary == tmp_path / "ref_01.wav"


class TestDiscoverAll:
    def test_covers_all_three_kinds_in_enum_order(self, tmp_paths: StudioPaths) -> None:
        results = discover_all(tmp_paths)
        assert [item.kind for item in results] == [
            AssetKind.BROLL,
            AssetKind.VOICE,
            AssetKind.BGM,
        ]
        assert all(item.root_missing for item in results)


class TestSerialization:
    def test_candidate_to_dict(self, tmp_paths: StudioPaths) -> None:
        _write(tmp_paths.bgm_dir / "bgm_001.mp3")
        payload = discover(tmp_paths, AssetKind.BGM).candidates[0].to_dict()
        assert payload["kind"] == "bgm"
        assert payload["id"] == "bgm_001"
        assert payload["files"] == [str(tmp_paths.bgm_dir / "bgm_001.mp3")]

    def test_discovery_to_dict_keeps_strays(self, tmp_paths: StudioPaths) -> None:
        stray = _write(tmp_paths.bgm_dir / "song.mp3")
        payload = discover(tmp_paths, AssetKind.BGM).to_dict()
        assert payload["root"] == str(tmp_paths.bgm_dir)
        assert payload["root_missing"] is False
        assert payload["candidates"] == []
        assert payload["strays"] == [str(stray)]
