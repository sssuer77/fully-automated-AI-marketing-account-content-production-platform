"""测试用的「出厂」``config/publish.yaml``（T6.4 · 真机 2026-09-23）。

为什么要有这个模块
------------------
``config/publish.yaml`` 是**面板会就地改写**的那一份配置（``write_publish_accounts``
的取舍就是"只动这一个文件、注释全留着"）。于是盘上那份的**账号清单**与**总开关**都是
操作员的运行期状态 —— 真机 2026-09-23 就在面板上删掉了 ``acc_main``、把 ``enabled``
翻成了 ``true``。

而用例要验的是**出厂语义**："出厂有一个 douyin 号" / "出厂不发出去" / "改号时行尾
注释要活着"。把这两件事混在一起，症状是"操作员在面板上删了一个号，一整片用例红在
与它们无关的地方"——那是最难查的一种红（看着像代码坏了，其实是配置变了）。

所以夹具把抄进临时目录的那份**摆回出厂**再交给用例。这里只放账号段与总开关两处：
它们正是面板会改的东西；段外的平台表 / ``precheck`` / 注释照旧是盘上那份。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "FACTORY_ACCOUNTS_YAML",
    "factory_accounts",
    "restore_factory_accounts",
    "restore_factory_publish_config",
    "set_publish_switch",
]

#: 出厂那份 ``accounts:`` 段（逐字抄自 ``config/publish.yaml`` 的 HEAD 版本，**含**段内
#: 注释与行尾注释 —— 有用例正断言"改号时那行注释要活着"，注释被抹掉就一起红了）。
FACTORY_ACCOUNTS_YAML = """\
accounts:
  - account_id: acc_main
    platform: douyin
    display_name: 主账号
    profile_dir: data/browser_profile/acc_main   # 持久化登录态（.gitignore）
    enabled: true
    daily_limit: 3
    min_gap_min: 30

  # 本地演练台的账号（T5.9）。它不是任何平台的账号：登录态目录与真账号**物理隔离**，
  # 发的也是本地靶页。daily_limit 拉满、间隔为 0 —— 演练要能连着跑好几遍。
  - account_id: _rehearsal
    platform: other
    display_name: 本地演练台
    profile_dir: data/browser_profile/_rehearsal
    enabled: true
    daily_limit: 100
    min_gap_min: 0
"""

#: ``accounts:`` 整段：段头 + 其后所有缩进行与空行（下一个顶格键归下一段）。
_ACCOUNTS_BLOCK = re.compile(r"^accounts:\n(?:[ \t].*\n|\n)*", re.MULTILINE)

#: 顶格那一行才是总开关：``handoff`` 与二线平台的 ``enabled`` 都是**缩进**的，
#: 拿不带换行的 ``enabled: false`` 去 ``replace`` 会先撞上 ``handoff`` 那一处
#: （真机上这么写过一次：开关没翻，翻的是 handoff）。
_SWITCH_ON = "\nenabled: true"
_SWITCH_OFF = "\nenabled: false"


def factory_accounts() -> list[dict[str, Any]]:
    """出厂账号清单（dict 形态：给"seed 与 YAML 对齐"那类断言用）。"""
    loaded: Any = yaml.safe_load(FACTORY_ACCOUNTS_YAML)
    accounts: list[dict[str, Any]] = loaded["accounts"]
    return accounts


def restore_factory_accounts(path: Path) -> None:
    """把 ``path`` 的 ``accounts:`` 段换回出厂那两个（段外一个字节不碰）。

    :raises AssertionError: 文件里找不到 ``accounts:`` 段
    """
    text = path.read_text(encoding="utf-8")
    spliced, replaced = _ACCOUNTS_BLOCK.subn(FACTORY_ACCOUNTS_YAML, text, count=1)
    if replaced != 1:
        raise AssertionError(f"{path} 里找不到 accounts: 段")
    # ``newline=""``：盘上那份是 LF，别让 Windows 把整份文件翻成 CRLF
    # （有用例逐字比 ``before/after``，换行符跟着变会让它红得莫名其妙）。
    path.write_text(spliced, encoding="utf-8", newline="")


def restore_factory_publish_config(path: Path) -> None:
    """账号段**与**总开关都摆回出厂（给"验出厂语义"的夹具用）。"""
    restore_factory_accounts(path)
    set_publish_switch(path, enabled=False)


def set_publish_switch(path: Path, *, enabled: bool) -> None:
    """把**顶格**那一行总开关翻成想要的值（缩进的那几处一个字节不动）。

    :raises AssertionError: 顶格那一行既不是 ``true`` 也不是 ``false``
    """
    text = path.read_text(encoding="utf-8")
    wanted, current = (_SWITCH_ON, _SWITCH_OFF) if enabled else (_SWITCH_OFF, _SWITCH_ON)
    if current not in text:
        raise AssertionError(f"{path} 顶格那一行不是预期的值 —— 夹具改了？")
    path.write_text(text.replace(current, wanted, 1), encoding="utf-8", newline="")
