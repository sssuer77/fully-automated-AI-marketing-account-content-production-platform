"""选题池仓储层（T1.9 · §02.1 / §03.3）—— 幂等与查询口径。

``db/repositories/`` 是全站**唯一**允许出现 SQL 的地方（§02.1），所以这里测的是
"SQL 写对没有"：幂等键是否真的幂等、``ORDER BY`` 是否与索引对齐、
``NULL`` 语义是否被误解成空串。

用真实库（临时目录 + 迁移）而不是 mock 连接：这些断言的价值全在 SQL 本身，
把 SQL 换掉就等于什么都没测。

**id 由仓储生成**：``insert_batch`` / ``insert_many`` 自己发 ULID 并返回，
调用方传进来的 ``id`` 会被忽略（它们只收"已经翻译好的纯数据"）。所以下面
一律用返回值，而不是自己编 id —— 编了也不会生效。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.models import DirectionRow, FeedbackItemRow, HotItemRow, TopicRow
from studio.db.repositories import DirectionRepo, FeedbackItemRepo, HotItemRepo, TopicRepo

# ══════════════════════════════════════════════════════════════════════
# 夹具与构造器
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """迁移好的临时库（**用完关掉**，否则 Windows 上临时目录删不干净）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


def _hot(
    *,
    item_id: str,
    line_no: int = 1,
    title: str = "MC 跑酷新版本",
    heat: str | None = "爆",
    platform: str | None = "douyin",
    raw_line: str = "MC 跑酷新版本|爆|douyin",
    parse_ok: bool = True,
    source_file: str = "hot.md",
) -> HotItemRow:
    return HotItemRow(
        id=item_id,
        source_file=source_file,
        line_no=line_no,
        title=title,
        heat=heat,
        platform=platform,
        raw_line=raw_line,
        parse_ok=parse_ok,
    )


def _feedback(
    *,
    item_id: str,
    content: str = "想看跑酷合集",
    sentiment: str | None = None,
    source_file: str = "fb.md",
    platform: str | None = "douyin",
) -> FeedbackItemRow:
    return FeedbackItemRow(
        id=item_id,
        source_file=source_file,
        platform=platform,
        occurred_on="2026-09-01",
        content=content,
        sentiment=sentiment,
    )


def _direction(*, batch_id: str, seq: int, title: str = "方向") -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "seq": seq,
        "title": title,
        "rationale": "理由",
        "grounded_on": [{"type": "persona", "quote": "账号定位"}],
        "priority": 100,
        "risk_flags": [],
    }


def _topic(*, direction_id: str, seq: int, title: str = "选题", score: float = 8.0) -> dict[str, Any]:
    return {
        "direction_id": direction_id,
        "seq": seq,
        "title": title,
        "hook_type": "悬念",
        "angle": "角度",
        "exec_feasible": True,
        "score": score,
        "reason": "理由",
        "dedup_hash": f"hash-{seq}",
        "similar_to": [],
        "status": "candidate",
    }


def _seed_direction(connection: sqlite3.Connection, *, batch_id: str = "b1", seq: int = 1) -> str:
    """插一个方向并返回**仓储发出来的** id。"""
    return DirectionRepo(connection).insert_batch(
        batch_id=batch_id, directions=[_direction(batch_id=batch_id, seq=seq)]
    )[0]


# ══════════════════════════════════════════════════════════════════════
# hot_items
# ══════════════════════════════════════════════════════════════════════


class TestHotItemRepo:
    def test_insert_is_idempotent_on_source_line_raw(self, connection: sqlite3.Connection) -> None:
        repo = HotItemRepo(connection)
        rows = [_hot(item_id="h1"), _hot(item_id="h2", line_no=2, raw_line="第二条|高|douyin")]
        assert repo.insert_many(rows) == 2
        # 同样的三列再导一次：换成新 id 也插不进去（幂等键不是主键）
        again = [_hot(item_id="h3"), _hot(item_id="h4", line_no=2, raw_line="第二条|高|douyin")]
        assert repo.insert_many(again) == 0
        assert repo.count_unconsumed() == 2

    def test_same_line_in_different_files_is_not_a_duplicate(self, connection: sqlite3.Connection) -> None:
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1")])
        assert repo.insert_many([_hot(item_id="h2", source_file="other.md")]) == 1

    def test_bad_lines_are_stored_but_never_selected(self, connection: sqlite3.Connection) -> None:
        """裁定 67：坏行照入库留痕，但 ``list_unconsumed`` 不选它。"""
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1"), _hot(item_id="h2", line_no=2, parse_ok=False)])
        assert [row.id for row in repo.list_unconsumed()] == ["h1"]
        assert repo.count_unconsumed() == 1

    def test_mark_consumed_records_the_referencing_direction(self, connection: sqlite3.Connection) -> None:
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1"), _hot(item_id="h2", line_no=2, raw_line="第二条|高|douyin")])
        stamp = "2026-09-13T00:00:00.000Z"
        assert repo.mark_consumed(item_ids=["h1"], direction_id="d1", consumed_at=stamp) == 1
        assert repo.count_unconsumed() == 1
        row = connection.execute("SELECT * FROM hot_items WHERE id = 'h1'").fetchone()
        assert row["used_by_direction_id"] == "d1"
        assert row["consumed_at"] == stamp

    def test_mark_consumed_without_direction_keeps_link_null(self, connection: sqlite3.Connection) -> None:
        """裁定 72：读过但没被任何方向引用 ⇒ 只写时间，不编造一个 direction_id。"""
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1")])
        assert repo.mark_consumed(item_ids=["h1"]) == 1
        row = connection.execute("SELECT * FROM hot_items WHERE id = 'h1'").fetchone()
        assert row["used_by_direction_id"] is None
        assert row["consumed_at"] is not None

    def test_mark_consumed_is_not_double_counted(self, connection: sqlite3.Connection) -> None:
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1")])
        repo.mark_consumed(item_ids=["h1"], direction_id="d1")
        assert repo.mark_consumed(item_ids=["h1"], direction_id="d2") == 0

    def test_mark_consumed_with_empty_ids_is_a_noop(self, connection: sqlite3.Connection) -> None:
        assert HotItemRepo(connection).mark_consumed(item_ids=[]) == 0

    def test_unconsumed_sources_lists_files_with_pending_rows(self, connection: sqlite3.Connection) -> None:
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1"), _hot(item_id="h2", source_file="other.md")])
        repo.mark_consumed(item_ids=["h1"])
        assert repo.list_unconsumed_sources() == ["other.md"]

    def test_bad_lines_do_not_block_archiving(self, connection: sqlite3.Connection) -> None:
        """坏行永远消费不掉 ⇒ 它不该让整个文件"永远归档不了"（施工期发现的缺陷）。"""
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1"), _hot(item_id="h2", line_no=2, parse_ok=False)])
        assert repo.list_unconsumed_sources() == ["hot.md"]
        repo.mark_consumed(item_ids=["h1"])
        assert repo.list_unconsumed_sources() == []
        assert repo.list_consumed_sources() == ["hot.md"]

    def test_consumed_sources_ignores_files_never_imported(self, connection: sqlite3.Connection) -> None:
        """归档只看库里的事实：一行都没有的文件不算"已消费"（否则会被误归档）。"""
        repo = HotItemRepo(connection)
        repo.insert_many([_hot(item_id="h1")])
        assert repo.list_consumed_sources() == []


# ══════════════════════════════════════════════════════════════════════
# feedback_items
# ══════════════════════════════════════════════════════════════════════


class TestFeedbackItemRepo:
    def test_insert_is_idempotent_on_source_and_content(self, connection: sqlite3.Connection) -> None:
        """幂等键是 ``(source_file, content)``：DDL 没有 ``line_no`` 列（§03.3.3）。"""
        repo = FeedbackItemRepo(connection)
        assert repo.insert_many([_feedback(item_id="f1")]) == 1
        assert repo.insert_many([_feedback(item_id="f2")]) == 0
        assert repo.count() == 1

    def test_same_content_in_another_file_is_not_a_duplicate(self, connection: sqlite3.Connection) -> None:
        repo = FeedbackItemRepo(connection)
        repo.insert_many([_feedback(item_id="f1")])
        assert repo.insert_many([_feedback(item_id="f2", source_file="fb2.md")]) == 1

    def test_unclassified_only_filters_by_null_sentiment(self, connection: sqlite3.Connection) -> None:
        repo = FeedbackItemRepo(connection)
        repo.insert_many(
            [
                _feedback(item_id="f1", content="a", sentiment="positive"),
                _feedback(item_id="f2", content="b"),
            ]
        )
        assert [row.id for row in repo.list_recent(unclassified_only=True)] == ["f2"]
        assert len(repo.list_recent()) == 2

    def test_apply_classification_writes_ddl_vocabulary(self, connection: sqlite3.Connection) -> None:
        repo = FeedbackItemRepo(connection)
        repo.insert_many([_feedback(item_id="f1")])
        assert repo.apply_classification(item_id="f1", sentiment="negative", wants=["a"], complaints=["b"])
        row = repo.list_recent()[0]
        assert row.sentiment == "negative"
        assert row.wants == ["a"]
        assert row.complaints == ["b"]

    def test_apply_classification_reports_unknown_id(self, connection: sqlite3.Connection) -> None:
        assert not FeedbackItemRepo(connection).apply_classification(
            item_id="missing", sentiment="unknown", wants=[], complaints=[]
        )


# ══════════════════════════════════════════════════════════════════════
# content_directions
# ══════════════════════════════════════════════════════════════════════


class TestDirectionRepo:
    def test_insert_batch_keeps_seq_order_and_returns_ids(self, connection: sqlite3.Connection) -> None:
        repo = DirectionRepo(connection)
        ids = repo.insert_batch(
            batch_id="b1",
            directions=[
                _direction(batch_id="b1", seq=1, title="甲"),
                _direction(batch_id="b1", seq=2, title="乙"),
            ],
            llm_model="cloud-model",
            prompt_version="1+abc",
        )
        assert len(ids) == len(set(ids)) == 2
        rows = repo.list_batch("b1")
        assert [row.title for row in rows] == ["甲", "乙"]
        assert [row.seq for row in rows] == [1, 2]
        assert rows[0].llm_model == "cloud-model"
        assert rows[0].prompt_version == "1+abc"
        assert rows[0].grounded_on == [{"type": "persona", "quote": "账号定位"}]

    def test_insert_empty_batch_writes_nothing(self, connection: sqlite3.Connection) -> None:
        repo = DirectionRepo(connection)
        assert repo.insert_batch(batch_id="b1", directions=[]) == []
        assert repo.count() == 0

    def test_latest_batch_id_follows_creation_order(self, connection: sqlite3.Connection) -> None:
        repo = DirectionRepo(connection)
        assert repo.latest_batch_id() is None
        repo.insert_batch(batch_id="b1", directions=[_direction(batch_id="b1", seq=1)])
        repo.insert_batch(batch_id="b2", directions=[_direction(batch_id="b2", seq=1)])
        assert repo.latest_batch_id() == "b2"

    def test_set_status_and_count(self, connection: sqlite3.Connection) -> None:
        repo = DirectionRepo(connection)
        first, second = repo.insert_batch(
            batch_id="b1",
            directions=[_direction(batch_id="b1", seq=1), _direction(batch_id="b1", seq=2)],
        )
        assert repo.set_status(direction_id=first, status="selected") is True
        assert repo.set_status(direction_id="missing", status="selected") is False
        assert repo.count(status="open") == 1
        assert repo.count() == 2
        fetched = repo.get(first)
        assert fetched is not None
        assert fetched.status == "selected"
        assert repo.get(second) is not None

    def test_set_status_rejects_unknown_status(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="非法方向状态"):
            DirectionRepo(connection).set_status(direction_id="x", status="unknown")

    def test_new_batch_id_is_unique(self) -> None:
        assert DirectionRepo.new_batch_id() != DirectionRepo.new_batch_id()


# ══════════════════════════════════════════════════════════════════════
# topic_candidates
# ══════════════════════════════════════════════════════════════════════


class TestTopicRepo:
    def test_insert_many_returns_ids_and_reads_back(self, connection: sqlite3.Connection) -> None:
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        ids = repo.insert_many(
            [_topic(direction_id=direction_id, seq=1), _topic(direction_id=direction_id, seq=2)]
        )
        assert len(ids) == 2
        rows = repo.list_by_direction(direction_id)
        assert [row.seq for row in rows] == [1, 2]
        assert rows[0].hook_type == "悬念"
        assert rows[0].status == "candidate"

    def test_list_pool_orders_by_score_desc(self, connection: sqlite3.Connection) -> None:
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        repo.insert_many(
            [
                _topic(direction_id=direction_id, seq=1, title="低分", score=5.0),
                _topic(direction_id=direction_id, seq=2, title="高分", score=9.0),
            ]
        )
        assert [row.title for row in repo.list_pool()] == ["高分", "低分"]

    def test_list_pool_filters_by_status(self, connection: sqlite3.Connection) -> None:
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        ids = repo.insert_many([_topic(direction_id=direction_id, seq=1)])
        repo.set_status(topic_id=ids[0], status="queued")
        assert repo.list_pool(status="candidate") == []
        assert len(repo.list_pool(status="queued")) == 1

    def test_list_for_dedup_covers_every_status(self, connection: sqlite3.Connection) -> None:
        """R15 要防的是"跟已经发过的撞车"，而那些早就是 ``queued``/``selected`` 了。"""
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        ids = repo.insert_many(
            [
                _topic(direction_id=direction_id, seq=1, title="甲"),
                _topic(direction_id=direction_id, seq=2, title="乙"),
            ]
        )
        repo.set_status(topic_id=ids[0], status="queued", selected_by="user")
        assert {row.title for row in repo.list_for_dedup()} == {"甲", "乙"}

    def test_selected_at_is_only_written_for_selected(self, connection: sqlite3.Connection) -> None:
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        ids = repo.insert_many([_topic(direction_id=direction_id, seq=1)])
        repo.set_status(topic_id=ids[0], status="rejected")
        rejected = repo.get(ids[0])
        assert rejected is not None
        assert rejected.selected_at is None
        repo.set_status(topic_id=ids[0], status="selected", selected_by="user")
        selected = repo.get(ids[0])
        assert selected is not None
        assert selected.selected_at is not None
        assert selected.selected_by == "user"

    def test_set_status_rejects_unknown_status(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="非法选题状态"):
            TopicRepo(connection).set_status(topic_id="x", status="unknown")

    def test_count_by_status(self, connection: sqlite3.Connection) -> None:
        direction_id = _seed_direction(connection)
        repo = TopicRepo(connection)
        ids = repo.insert_many(
            [_topic(direction_id=direction_id, seq=1), _topic(direction_id=direction_id, seq=2)]
        )
        repo.set_status(topic_id=ids[1], status="rejected")
        assert repo.count_by_status() == {"candidate": 1, "rejected": 1}


# ══════════════════════════════════════════════════════════════════════
# 行结构的容错边界
# ══════════════════════════════════════════════════════════════════════


def test_direction_row_parses_json_columns() -> None:
    row = DirectionRow.from_row(
        {
            "id": "d1",
            "batch_id": "b1",
            "seq": 3,
            "title": "t",
            "rationale": "r",
            "grounded_on_json": '[{"type": "hot", "ref_id": "h1"}]',
            "priority": 100,
            "risk_flags_json": '["low_grounding"]',
            "status": "open",
        }
    )
    assert row.seq == 3
    assert row.grounded_on == [{"type": "hot", "ref_id": "h1"}]
    assert row.risk_flags == ["low_grounding"]


def test_topic_row_tolerates_broken_json() -> None:
    """坏 JSON **不抛异常**：读路径上炸掉只会让"面板打不开"。"""
    row = TopicRow.from_row(
        {
            "id": "t1",
            "direction_id": "d1",
            "seq": 1,
            "title": "标题",
            "angle": "角度",
            "exec_feasible": 1,
            "similar_to_json": "{ 这不是 JSON",
            "status": "candidate",
        }
    )
    assert row.similar_to == []
    assert row.exec_feasible is True
    assert row.score is None
