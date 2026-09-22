"""``parse_task_id`` 单测（T5.11）。

成片文件名 ⇒ 任务号这一步只有一条判据，但它有两种坏法，各要一个用例：

* **切错**：任务号自己可以含下划线（``ui-2026…``，以及用户自己起的名），正则一写成
  非贪婪（``.+?``）就会在任务号里遇到第一个 ``_final`` 就断 —— 切出一个不存在的任务号，
  而它在面板上看起来完全正常（只是查不到任务、发不出去）。
* **切出空**：``20260922-152331_final.mp4`` 这种（没有任务号）必须回 ``None``，
  不能切出一个空串去查库。

认不出来是**常态**：``data/output/videos/`` 是人也会往里放东西的地方（手工导出的、
别人发来的），那些东西不该让整个成片库打不开。
"""

from __future__ import annotations

import pytest

from studio.services.library_service import parse_task_id

ULID = "01M2Z9BP1CR70TBW0CJ05FQ12Z"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 出厂形状（§2.4）
        (f"20260922-152331_{ULID}_final.mp4", ULID),
        # 降级档（720P 保底）
        (f"20260922-152331_{ULID}_final_720p.mp4", ULID),
        # 任务号里含下划线：贪婪匹配吃到**最后一个** `_final`
        ("20260922-152331_ui-2026_09_22_final.mp4", "ui-2026_09_22"),
        ("20260922-152331_a_final_b_final.mp4", "a_final_b"),
        # 中文任务号（人自己起的名）也算数
        ("20260922-152331_跑酷第一期_final.mp4", "跑酷第一期"),
    ],
)
def test_parses_the_task_id(name: str, expected: str) -> None:
    assert parse_task_id(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "20260922-152331_final.mp4",  # 没有任务号
        "20260922-152331__final.mp4",  # 任务号是空串
        "跑酷第一期_final.mp4",  # 没有时间戳
        "20260922-152331_任务_final.MP4",  # 后缀大小写不算数（磁盘上是 .mp4）
        "20260922-152331_任务_final.mp4.bak",  # 备份文件
        "20260922-152331_任务.mp4",  # 不是成片（中间产物）
        "手工导出的片子.mp4",
        "readme.txt",
        "",
    ],
)
def test_returns_none_for_anything_else(name: str) -> None:
    assert parse_task_id(name) is None
