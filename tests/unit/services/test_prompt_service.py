"""``PromptService``（T6.2）—— 运行期覆盖层的五条纪律。

为什么单测不用真 LLM、也不用真库
--------------------------------
这一层不碰模型也不碰 ``studio.db``：它只做三件事 —— 读生效那一份、**校验**、
写 ``data/prompts/`` 里的覆盖文件。所以这里用的是**真的**提示词库
（``prompts/`` + ``manifest.yaml``，与生产逐字同源）配一个 tmp 覆盖目录：
校验规则本身（"哪些变量真的会被填上"）就是从这里读出来的，用假库测等于把
被测对象换掉了。

五条纪律
--------
① 覆盖**不改仓库文件**（git 里那份必须等于"入库的那份"）；
② ``prompt_version`` **跟着覆盖走**（P5：同输入同产物靠它复现）；
③ 引用了没人填的变量 / 非法模板语法 ⇒ **一个字节都不落盘**；
④ 提交的内容与生效那份逐字相同 ⇒ 不写盘、不留痕；
⑤ 还原 = 删掉覆盖文件（幂等）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.agents.prompts import PromptLibrary
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import AuditRepo
from studio.services.prompt_service import MAX_PROMPT_CHARS, PromptService

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 一个真的注册过的条目（两段文件齐全，覆盖得了 system 也覆盖得了 user）
NAME = "ideator"

#: 只有一个文件的条目（用来验"这个条目没有 system 段"那条错）
USER_ONLY = "shared.persona_block"


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ 提示词用的是**生产那一份**，覆盖落 tmp。"""
    return StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")


@pytest.fixture
def library(paths: StudioPaths) -> PromptLibrary:
    return PromptLibrary.load(paths.prompts_dir, override_root=paths.prompts_override_dir)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "studio.db"
    migrate(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def service(library: PromptLibrary, connection: sqlite3.Connection) -> PromptService:
    return PromptService(library, audit=AuditRepo(connection))


def role_text(library: PromptLibrary, name: str, role: str) -> str:
    """生效那一份的正文（``library.read`` 走覆盖优先）。"""
    return library.read(getattr(library.entry(name), role))


# ══════════════════════════════════════════════════════════════════════
# ① 读
# ══════════════════════════════════════════════════════════════════════


def test_catalog_covers_every_registered_prompt(service: PromptService, library: PromptLibrary) -> None:
    catalog = service.catalog()
    assert [item.name for item in catalog] == list(library.names())
    assert catalog  # 注册表非空（空了说明 manifest 读错了）
    for item in catalog:
        assert item.prompt_version.startswith(f"{item.version}+")
        assert item.variables  # 每个条目都至少有一个可填的变量
        assert not item.overridden


def test_catalog_reports_the_allowed_variables_not_the_used_ones(
    service: PromptService,
) -> None:
    """``variables`` 是"渲染时拿得到"的集合（含 ``shared.*`` 注入块），不是"这个模板用到的"。"""
    entry = service.view(NAME)
    assert "persona_name" in entry.variables  # 来自 shared.persona_block
    assert "direction_title" in entry.variables  # 自己的模板里用了
    assert "nobody_fills_this" not in entry.variables


# ══════════════════════════════════════════════════════════════════════
# ② 写：覆盖不改仓库文件
# ══════════════════════════════════════════════════════════════════════


def test_saving_an_override_leaves_the_repo_file_alone(
    service: PromptService, library: PromptLibrary
) -> None:
    before_repo = library.read_repo("ideator/system.md")
    before_version = library.prompt_version(NAME)

    outcome = service.save(NAME, system=f"{before_repo}\n额外一句纪律。\n")

    assert outcome.changed == ("system",)
    assert outcome.entry.overridden is True
    # 仓库那一份一个字节都没动（git 里那份 = 入库那份）
    assert library.read_repo("ideator/system.md") == before_repo
    # 生效那一份变了，版本跟着变（落 scripts.prompt_version 的就是它）
    assert "额外一句纪律" in library.read("ideator/system.md")
    assert outcome.entry.prompt_version != before_version


def test_override_is_visible_to_a_freshly_loaded_library(service: PromptService, paths: StudioPaths) -> None:
    """每个进程（API / worker / CLI）都是现 load 一次 ⇒ 覆盖必须对**新库**也生效。"""
    service.save(NAME, user="说 {{direction_title}}，{{per_direction}} 条。\n")
    fresh = PromptLibrary.load(paths.prompts_dir, override_root=paths.prompts_override_dir)
    assert fresh.is_overridden("ideator/user.jinja")
    assert fresh.read("ideator/user.jinja") == "说 {{direction_title}}，{{per_direction}} 条。\n"


def test_a_library_without_an_override_root_refuses_to_write(paths: StudioPaths) -> None:
    """没配覆盖目录 ⇒ 报错，而不是悄悄去改仓库文件（``prompts verify`` 靠它）。"""
    bare = PromptLibrary.load(paths.prompts_dir)
    with pytest.raises(StudioError) as caught:
        PromptService(bare).save(NAME, system="随便写点什么。\n")
    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ══════════════════════════════════════════════════════════════════════
# ③ 校验不过 ⇒ 一个字节都不落盘
# ══════════════════════════════════════════════════════════════════════


def test_an_unknown_variable_is_rejected_before_anything_is_written(
    service: PromptService, library: PromptLibrary
) -> None:
    with pytest.raises(StudioError) as caught:
        service.save(NAME, system="标题：{{nobody_fills_this}}\n")

    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.context["unknown"] == ["nobody_fills_this"]
    assert not library.is_overridden("ideator/system.md")


def test_forbidden_template_syntax_is_rejected(service: PromptService) -> None:
    for text in ("{% for x in y %}z{% endfor %}", "{# 注释 #}"):
        with pytest.raises(StudioError) as caught:
            service.save(NAME, system=text)
        assert caught.value.code == ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize("text", ["   \n  ", ""])
def test_a_blank_segment_is_rejected(service: PromptService, text: str) -> None:
    """清空一整段 = 把那条纪律整个删掉，而它在面板上长得和"没改"几乎一样。"""
    with pytest.raises(StudioError) as caught:
        service.save(NAME, system=text)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_an_overlong_segment_is_rejected(service: PromptService) -> None:
    with pytest.raises(StudioError) as caught:
        service.save(NAME, system="字" * (MAX_PROMPT_CHARS + 1))
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_a_segment_the_entry_does_not_have_is_rejected(service: PromptService) -> None:
    """``shared.persona_block`` 只有 user 段 ⇒ 改它的 system 要报错，不能凭空造一个文件。"""
    with pytest.raises(StudioError) as caught:
        service.save(USER_ONLY, system="凭空一段。\n")
    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert caught.value.context["role"] == "system"


def test_an_unregistered_name_is_rejected(service: PromptService) -> None:
    with pytest.raises(StudioError) as caught:
        service.save("nope.nothing", system="随便写点什么。\n")
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_an_empty_body_is_rejected(service: PromptService) -> None:
    """两段都不给 ⇒ 报错（"我什么都没提交"不该被当成一次成功的操作）。"""
    with pytest.raises(StudioError) as caught:
        service.save(NAME)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


# ══════════════════════════════════════════════════════════════════════
# ④ 没变就不留痕
# ══════════════════════════════════════════════════════════════════════


def test_saving_the_same_text_is_a_no_op(
    service: PromptService, library: PromptLibrary, connection: sqlite3.Connection
) -> None:
    current = role_text(library, NAME, "system")
    outcome = service.save(NAME, system=current)

    assert outcome.changed == ()
    assert outcome.entry.overridden is False
    assert not library.is_overridden("ideator/system.md")
    assert AuditRepo(connection).list_recent(limit=10) == []


def test_saving_the_repo_text_back_clears_the_override(
    service: PromptService, library: PromptLibrary
) -> None:
    """ "改回原样"应当**删掉**覆盖，而不是留一份与仓库逐字相同的文件。

    留着它的话，面板上的「已覆盖」徽标会永远亮着，而它明明等于原样 ——
    下一个人就会以为这里被改过，然后去查一个不存在的差异。
    """
    repo_text = library.read_repo("ideator/system.md")
    service.save(NAME, system=f"{repo_text}\n改一下。\n")
    assert library.is_overridden("ideator/system.md")

    outcome = service.save(NAME, system=repo_text)
    assert outcome.changed == ("system",)
    assert outcome.entry.overridden is False
    assert not library.is_overridden("ideator/system.md")


def test_only_the_named_segment_is_touched(service: PromptService, library: PromptLibrary) -> None:
    service.save(NAME, system="新的一段纪律。\n")
    assert library.is_overridden("ideator/system.md")
    assert not library.is_overridden("ideator/user.jinja")  # user 段一个字节没动


# ══════════════════════════════════════════════════════════════════════
# ⑤ 还原 + 留痕
# ══════════════════════════════════════════════════════════════════════


def test_restore_goes_back_to_the_repo_copy(service: PromptService, library: PromptLibrary) -> None:
    before_version = library.prompt_version(NAME)
    service.save(NAME, system="新的一段纪律。\n")

    outcome = service.restore(NAME, role="system")
    assert outcome.changed == ("system",)
    assert outcome.restored is True
    assert outcome.entry.overridden is False
    assert outcome.entry.prompt_version == before_version
    assert library.read("ideator/system.md") == library.read_repo("ideator/system.md")


def test_restore_is_idempotent(service: PromptService) -> None:
    first = service.restore(NAME, role="system")
    assert first.changed == ()
    assert first.restored is True  # "要求还原"这件事成功了，只是没有东西可删


def test_restore_rejects_an_unknown_role(service: PromptService) -> None:
    with pytest.raises(StudioError) as caught:
        service.restore(NAME, role="nope")
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_audit_records_the_version_on_both_sides(
    service: PromptService, library: PromptLibrary, connection: sqlite3.Connection
) -> None:
    """留痕要能回答"这一版稿子是用哪份提示词写的" ⇒ 前后版本号都得有。"""
    before = library.prompt_version(NAME)
    outcome = service.save(NAME, system="新的一段纪律。\n")
    after = outcome.entry.prompt_version

    ops = AuditRepo(connection).list_recent(limit=10)
    assert [op.action for op in ops] == ["prompt.updated"]
    assert ops[0].target_type == "prompt"
    assert ops[0].target_id == NAME
    assert ops[0].before is not None and ops[0].before["prompt_version"] == before
    assert ops[0].after is not None and ops[0].after["prompt_version"] == after
    assert ops[0].after["overridden"] == ["system"]

    service.restore(NAME, role="system")
    ops = AuditRepo(connection).list_recent(limit=10)
    assert [op.action for op in ops] == ["prompt.restored", "prompt.updated"]
