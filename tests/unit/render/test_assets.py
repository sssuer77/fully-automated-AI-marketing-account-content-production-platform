"""素材选取：列目录 + 随机挑一个（T3.3 前置）。

口径刻意粗糙：**只挑，不校验**。所以用例的重点不是"挑得对不对"，
而是三件事：只认该认的扩展名、挑不到时如实报错、随机性可复现（排障要用）。

第四件事在 T4.8 补上：**停用要生效**（``exclude=``）。它同样不是校验 ——
校验问的是"这条素材够不够格"，而 ``exclude`` 问的是"人有没有把它关掉"。
"""

from __future__ import annotations

import random
from pathlib import Path

from studio.core.paths import StudioPaths
from studio.render.assets import (
    BGM_SUFFIXES,
    CLIP_SUFFIXES,
    list_files,
    pick_bgm,
    pick_file,
    pick_parkour_clip,
)


def _paths(tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=tmp_path, data_dir=tmp_path / "data")


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ══════════════════════════════════════════════════════════════════════
# 列目录
# ══════════════════════════════════════════════════════════════════════


def test_lists_only_matching_suffixes(tmp_path: Path) -> None:
    _touch(tmp_path / "a.mp4")
    _touch(tmp_path / "b.txt")
    _touch(tmp_path / "c.mov")
    found = [item.name for item in list_files(tmp_path, CLIP_SUFFIXES)]
    assert found == ["a.mp4", "c.mov"]


def test_suffix_match_is_case_insensitive(tmp_path: Path) -> None:
    """素材是人手收集的，``.MP4`` 与 ``.mp4`` 都得认。"""
    _touch(tmp_path / "SHOUT.MP4")
    assert [item.name for item in list_files(tmp_path, CLIP_SUFFIXES)] == ["SHOUT.MP4"]


def test_directories_are_not_picked(tmp_path: Path) -> None:
    """名字叫 ``.mp4`` 的**目录**不算素材。"""
    (tmp_path / "sneaky.mp4").mkdir(parents=True)
    assert list_files(tmp_path, CLIP_SUFFIXES) == []


def test_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert list_files(tmp_path / "nope", CLIP_SUFFIXES) == []


def test_result_is_sorted_for_reproducible_picking(tmp_path: Path) -> None:
    """排序保证"同样的种子 ⇒ 同样的选择"，排障时能复现某一条片子。"""
    for name in ("c.mp4", "a.mp4", "b.mp4"):
        _touch(tmp_path / name)
    assert [item.name for item in list_files(tmp_path, CLIP_SUFFIXES)] == [
        "a.mp4",
        "b.mp4",
        "c.mp4",
    ]


def test_directory_names_with_glob_metacharacters_are_fine(tmp_path: Path) -> None:
    """目录名带 ``[`` ``]`` 时 glob 会当成字符集 —— 列目录不走 glob，所以没事。"""
    weird = tmp_path / "broll[1]"
    _touch(weird / "a.mp4")
    assert [item.name for item in list_files(weird, CLIP_SUFFIXES)] == ["a.mp4"]


# ══════════════════════════════════════════════════════════════════════
# 随机挑
# ══════════════════════════════════════════════════════════════════════


def test_same_seed_picks_the_same_clip(tmp_path: Path) -> None:
    for name in ("a.mp4", "b.mp4", "c.mp4", "d.mp4"):
        _touch(tmp_path / name)
    first = pick_file(tmp_path, CLIP_SUFFIXES, rng=random.Random(7))
    second = pick_file(tmp_path, CLIP_SUFFIXES, rng=random.Random(7))
    assert first == second


def test_pick_returns_none_when_empty(tmp_path: Path) -> None:
    assert pick_file(tmp_path, CLIP_SUFFIXES) is None


def test_pick_parkour_clip_returns_none_when_the_library_is_empty(tmp_path: Path) -> None:
    """★ 挑不到底片 ⇒ ``None``，由调用方走**黑屏降级**，**不是**失败（T3.7 · §04.2.8.6）。

    这条断言在 T3.7 反过来了：原先要求抛 ``RENDER_BROLL_MISSING``（"没有底片就没有
    画面"）。改口径的理由是**无人值守** —— 素材是人工收集的、随时可能被清空或全部
    禁用，而"今天没有底片"不该等于"今天出不了片"。纯黑底配字幕仍是一条能发的片子。
    """
    assert pick_parkour_clip(_paths(tmp_path)) is None


def test_pick_parkour_clip_finds_a_clip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    picked = pick_parkour_clip(paths)
    assert picked is not None and picked.name == "parkour_001.mp4"


def test_pick_bgm_returns_none_when_empty(tmp_path: Path) -> None:
    """没有 BGM ⇒ ``None``（跳过背景音乐），**不是**失败 —— 与底片的口径不同。"""
    assert pick_bgm(_paths(tmp_path)) is None


def test_pick_bgm_finds_a_track(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _touch(paths.bgm_dir / "track.mp3")
    picked = pick_bgm(paths)
    assert picked is not None
    assert picked.name == "track.mp3"


def test_bgm_suffixes_match_the_config_glob() -> None:
    """与 `config/outputs.yaml` 的 ``bgm.glob`` 同一批扩展名，别各写一份。"""
    assert set(BGM_SUFFIXES) == {".mp3", ".m4a", ".wav", ".flac"}


# ══════════════════════════════════════════════════════════════════════
# 停用（T4.8 验收：禁用 ⇒ 随机化不再选中）
# ══════════════════════════════════════════════════════════════════════
#
# 这一组是"面板与出片看到同一个真相"的另一半：面板上点了「停用」，出片就不该再挑到
# 它。落地点在本模块的 ``exclude=`` —— 调用方从库里查出来、以**文件名集合**传进来，
# 这里只做一次集合减法（本模块不认识素材 id，也不碰数据库）。


def test_excluded_clip_is_never_picked(tmp_path: Path) -> None:
    """★ 被停用的那条一次都不该被挑到（多试几个种子，别让随机性掩盖问题）。"""
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    _touch(paths.mc_parkour_dir / "parkour_002.mp4")
    for seed in range(20):
        picked = pick_parkour_clip(paths, rng=random.Random(seed), exclude={"parkour_001.mp4"})
        assert picked is not None and picked.name == "parkour_002.mp4"


def test_all_clips_excluded_falls_back_to_black(tmp_path: Path) -> None:
    """★ 全部停用 ⇒ ``None`` ⇒ 黑屏降级。

    这是对的：「我把底片都关了」应当看到纯黑底，而不是"停用没生效、照样随机挑一条"。
    """
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    assert pick_parkour_clip(paths, exclude={"parkour_001.mp4"}) is None


def test_exclude_matches_the_file_name_not_the_path(tmp_path: Path) -> None:
    """排除名单是**文件名**：库里存的是绝对路径，换台机器前缀就不同，只有文件名能对上。"""
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    assert pick_parkour_clip(paths, exclude={"D:/other/machine/parkour_001.mp4"}) is not None
    assert pick_parkour_clip(paths, exclude={"parkour_001.mp4"}) is None


def test_bgm_honours_exclude(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _touch(paths.bgm_dir / "bgm_a.mp3")
    _touch(paths.bgm_dir / "bgm_b.mp3")
    picked = pick_bgm(paths, exclude={"bgm_a.mp3"})
    assert picked is not None and picked.name == "bgm_b.mp3"
    assert pick_bgm(paths, exclude={"bgm_a.mp3", "bgm_b.mp3"}) is None


def test_empty_exclude_is_the_same_as_none(tmp_path: Path) -> None:
    """空集合与 ``None`` 同义（调用方给空名单时不该改变行为）。"""
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    assert pick_parkour_clip(paths, rng=random.Random(3), exclude=frozenset()) is not None
    assert pick_parkour_clip(paths, rng=random.Random(3)) is not None


def test_excluding_a_name_that_is_not_there_changes_nothing(tmp_path: Path) -> None:
    """停用的行指向一个已经不在盘上的文件 ⇒ 名单里有个对不上的名字，不该有副作用。"""
    paths = _paths(tmp_path)
    _touch(paths.mc_parkour_dir / "parkour_001.mp4")
    picked = pick_parkour_clip(paths, exclude={"parkour_999.mp4"})
    assert picked is not None and picked.name == "parkour_001.mp4"
