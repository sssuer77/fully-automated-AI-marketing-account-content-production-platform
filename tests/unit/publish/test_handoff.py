"""交付包（T5.5 · §06.11 / §06.12 · A2）。

这一条钉住四件事
----------------
① **缺件如实**：只有成片必需；其余缺了照打，但每一件都要写清「为什么缺、怎么补」；
② **取哪一份**：发布记录里的那份优先（那才是真送出去过的），它不在盘上了就退回
   「盘上最新的一支」，并在清单里说明这次用的是哪一个；
③ **打包真的落盘**：目录自包含（每件一个文件 + 一份 ``handoff.json``），
   清单里的 sha256 与盘上那份**对得上**（归档三个月后要核对的就是这个）；
④ **适配器名字不认识就抛**（``CONFIG_INVALID``），**不静默退回 local**。

为什么用真盘真库
----------------
``build_package`` 的输入就是「盘上有什么 + 库里记了什么」。换成假件，验的就不再是
「发布记录里的那份被 GC 收走之后还取不取得到」—— 而那正是它唯一容易出错的地方。
发布记录还带外键（``publications.task_id → tasks.id``），所以夹具里真的建一条任务：
拿一个手编的任务号去插记录，会撞在约束上，而不是撞在要验的那件事上。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.files import file_sha256
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.publication_repo import PublicationRepo
from studio.domain.task_service import TaskService
from studio.publish.handoff import (
    DELIVERY_KINDS,
    HANDOFF_MANIFEST_NAME,
    DeliveryItem,
    DeliveryPackage,
    LocalHandoffAdapter,
    build_package,
    handoff_adapter,
    resolve_cover,
)


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    return value


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "studio.db"
    migrate(database)
    value = connect(database)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    """一条**真**任务（发布记录有外键，手编的任务号插不进去）。"""
    return TaskService(connection).create(title="一条测试任务", actor="test").id


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _video(paths: StudioPaths, task_id: str, stamp: str, payload: str) -> Path:
    return _write(paths.videos_dir / f"{stamp}_{task_id}_final.mp4", payload)


def _cover(paths: StudioPaths, task_id: str, stamp: str, payload: str) -> Path:
    return _write(paths.covers_dir / f"{stamp}_{task_id}_cover.jpg", payload)


def _publish(
    connection: sqlite3.Connection,
    task_id: str,
    video: Path,
    *,
    published: bool = False,
) -> str:
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=task_id,
        platform="douyin",
        account_id="acc_main",
        video_path=str(video),
        video_sha256="0" * 64,
        title="一条测试标题",
    )
    if published:
        repo.mark_published(row.id, url="https://example.com/v/1", platform_post_id="p1")
    return row.id


def _item(package: DeliveryPackage, kind: str) -> DeliveryItem:
    return next(item for item in package.items if item.kind == kind)


def test_every_delivery_kind_is_listed(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """六件一个不少，而且**只有成片必需**（缺封面只是不完整，不是打不出包）。"""
    package = build_package(task_id=task_id, paths=paths, connection=connection)

    assert [item.kind for item in package.items] == [kind for kind, _, _ in DELIVERY_KINDS]
    assert [item.kind for item in package.items if item.required] == ["video"]


def test_missing_items_say_how_to_fix(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """什么都没有 ⇒ 不 ready，而且**每一件缺件都写得出怎么补**。

    一句「缺失」对用户没有用：他要的是下一步去哪个面板按哪个按钮。
    """
    package = build_package(task_id=task_id, paths=paths, connection=connection)

    assert package.ready is False
    assert {item.kind for item in package.missing} == {item.kind for item in package.items}
    for item in package.items:
        assert item.note, f"{item.kind} 缺件却没写怎么补"


def test_video_alone_makes_the_package_ready(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """有成片 ⇒ ready（其余缺件不影响「这个包能不能交出去」）。"""
    _video(paths, task_id, "20260917-120000", "片子")

    package = build_package(task_id=task_id, paths=paths, connection=connection)

    assert package.ready is True
    assert "video" not in [item.kind for item in package.missing]


def test_latest_video_on_disk_wins(paths: StudioPaths, connection: sqlite3.Connection, task_id: str) -> None:
    """没有发布记录时取盘上**最新**的那一支（文件名带时间戳 ⇒ 排序即先后）。"""
    _video(paths, task_id, "20260917-100000", "旧的")
    newest = _video(paths, task_id, "20260917-120000", "新的")

    package = build_package(task_id=task_id, paths=paths, connection=connection)

    assert _item(package, "video").source == newest


def test_publication_record_wins_over_disk(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """发出去的那一份优先 —— 发布之后又重渲过一次时，归档要的是前者。"""
    sent = _video(paths, task_id, "20260917-100000", "发出去的那份")
    _video(paths, task_id, "20260917-120000", "后来重渲的那份")
    _publish(connection, task_id, sent, published=True)

    package = build_package(task_id=task_id, paths=paths, connection=connection)

    assert _item(package, "video").source == sent
    assert package.publication is not None
    assert package.publication.status == "published"


def test_gone_publication_file_falls_back_with_a_note(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """发布记录里那份被 GC 收走了 ⇒ 退回盘上最新的，**并说明这次用的是哪一个**。

    不说的话，拿包的人会以为这就是当初发出去的那一支。
    """
    gone = paths.videos_dir / f"20260917-100000_{task_id}_final.mp4"  # 只记了路径，没写盘
    on_disk = _video(paths, task_id, "20260917-120000", "盘上这一份")
    _publish(connection, task_id, gone, published=True)

    package = build_package(task_id=task_id, paths=paths, connection=connection)

    item = _item(package, "video")
    assert item.source == on_disk
    assert item.note is not None and "GC" in item.note


def test_cover_takes_the_latest_and_answers_none_for_unknown_task(paths: StudioPaths, task_id: str) -> None:
    """封面与成片同一条口径：取最后一张；答不出来 ⇒ ``None``（**不抛**）。"""
    _cover(paths, task_id, "20260917-100000", "旧封面")
    newest = _cover(paths, task_id, "20260917-120000", "新封面")

    assert resolve_cover(task_id, paths) == newest
    assert resolve_cover("不存在的任务号", paths) is None


def test_local_adapter_writes_a_selfcontained_dir(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """打包真的落盘：每件一个文件 + 一份清单，且清单里的 sha256 与盘上对得上。"""
    video = _video(paths, task_id, "20260917-120000", "片子内容")
    _write(paths.subtitle_ass(task_id), "[Script Info]")
    package = build_package(task_id=task_id, paths=paths, connection=connection)

    result = LocalHandoffAdapter().push(package, output_dir=paths.home / "data" / "handoff")

    assert result.adapter == "local"
    manifest_path = result.root / HANDOFF_MANIFEST_NAME
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["task_id"] == task_id
    assert manifest["adapter"] == "local"

    for entry in manifest["items"]:
        if entry["file"] is None:
            continue
        copied = result.root / entry["file"]
        assert copied.is_file()
        assert entry["sha256"] == file_sha256(copied), f"{entry['kind']} 的哈希对不上"

    copied_video = next(entry for entry in manifest["items"] if entry["kind"] == "video")
    assert (result.root / copied_video["file"]).read_text(encoding="utf-8") == "片子内容"
    assert copied_video["source"] == video.as_posix()
    assert result.bytes > 0
    assert "cover" in manifest["missing"]


def test_manifest_carries_publication_and_compliance(
    paths: StudioPaths, connection: sqlite3.Connection, task_id: str
) -> None:
    """清单里带上发布记录与 R2 来源登记 —— 拿到包的人**不必回头查我们的库**。"""
    video = _video(paths, task_id, "20260917-120000", "片子")
    _publish(connection, task_id, video, published=True)
    package = build_package(task_id=task_id, paths=paths, connection=connection)

    result = LocalHandoffAdapter().push(package, output_dir=paths.home / "data" / "handoff")
    manifest = json.loads((result.root / HANDOFF_MANIFEST_NAME).read_text(encoding="utf-8"))

    assert manifest["publication"]["status"] == "published"
    assert manifest["publication"]["url"] == "https://example.com/v/1"
    assert manifest["compliance"]["ok"] is True
    assert manifest["compliance"]["gaps"] == []


def test_push_twice_keeps_both(paths: StudioPaths, connection: sqlite3.Connection, task_id: str) -> None:
    """两次打包落在**两个**目录里：覆盖会得到「上一份被悄悄换掉了」，而两边都没记录。"""
    _video(paths, task_id, "20260917-120000", "片子")
    package = build_package(task_id=task_id, paths=paths, connection=connection)
    adapter = LocalHandoffAdapter()
    output_dir = paths.home / "data" / "handoff"

    first = adapter.push(package, output_dir=output_dir)
    # 目录名取到毫秒：这里睡一小下，验的就是「隔一次打包不会被算成同一个目录」。
    time.sleep(0.02)
    second = adapter.push(package, output_dir=output_dir)

    assert first.root != second.root
    assert first.root.is_dir() and second.root.is_dir()
    assert (first.root / HANDOFF_MANIFEST_NAME).is_file()
    assert (second.root / HANDOFF_MANIFEST_NAME).is_file()


def test_unknown_adapter_raises_instead_of_falling_back() -> None:
    """配置里写了个外部适配器却没注册 ⇒ **抛**，不静默落在本地盘上。

    静默退回的后果是「配置写了对接、包一直落在本地」，而两边都不报错 ——
    用户会以为已经对接好了。补救办法要写在 ``remediation`` 里（面板直接显示它）。
    """
    with pytest.raises(StudioError) as caught:
        handoff_adapter("s3-bucket")

    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert "s3-bucket" in str(caught.value)
    assert caught.value.remediation is not None
    assert "local" in caught.value.remediation


def test_local_adapter_is_the_default() -> None:
    """``local`` 是**默认实现**，而且认得出自己的名字（面板上要显示它）。"""
    assert handoff_adapter("local").name == "local"
