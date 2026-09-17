"""R2 合规留档（T5.5 · §06.11 / §06.12）。

这一层只回答一个问题：**我现在要发出去的东西，来源登记齐了吗？**

为什么把它做成一个模块而不是一句提示语
--------------------------------------
R2 的风险不是"忘了写一行字"，而是**三样东西各自散在一处**：

1. **音色**：``data/voice_src/<id>/profile.json``（来源 / 录制日期 / 授权说明）
   ⇒ 入库时记进 ``voice_profiles.proof_path``（§04.3.1）；
2. **BGM**：授权书 / 曲库页面 ⇒ ``bgm_tracks.proof_path``；
3. **跑酷素材**：``broll_clips.license``（必填列）+ ``source_url``。

任何一处缺了都不会报错 —— 片子照发，风险照留。所以这里**逐条列出来**，让"缺一份"
变成一个看得见的数字，而不是一句谁都不会细看的提示。

关于音色 ID 与展现名的解耦（R2 的另一半）
-----------------------------------------
「熊大」这个名字不该出现在音色 ID 里：ID 是**存进库、写进 manifest、进交付包**的
那个字符串，展现名是给人看的。两者解耦之后，换掉参考音只需要改目录名 + 在配音面板
把映射指过去，**不用改代码**（T2.4 裁定 286）。所以这里的清单里出现的是 ID，
而"它在剧本里演谁"由 ``tasks.payload_json.voice_map`` 回答 —— 两件事分开报。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.paths import StudioPaths
from studio.db.models import BgmTrackRow, BrollClipRow, VoiceProfileRow
from studio.db.repositories.asset_repo import BgmTrackRepo, BrollClipRepo, VoiceProfileRepo

__all__ = [
    "R2_NOTICE",
    "ComplianceSnapshot",
    "Registration",
    "compliance_snapshot",
]

#: **常驻提示**（发布面板与素材库都显示它，§06.11 的"R2 合规提示常驻"）。
#:
#: 为什么这句话必须常驻而不是放在文档里：它约束的是**每一次发布**，而文档只在
#: "有人想起来去翻"的时候才起作用。用户看到的应当是"这条片子里的声音是谁的"。
R2_NOTICE: Final[str] = (
    "R2 合规提示：复刻他人声音（含动画角色配音）可能同时触及**声音权**与**著作权**。"
    "本系统只做三件事：①音色 ID 与展现名解耦（换音色不改代码）；"
    "②把来源登记（profile.json / 授权书）留在盘上并随交付包一起给出；"
    "③在配音与素材面板如实标出缺哪一份。**是否可商用请自行确认** —— "
    "工具不替你判断授权范围。"
)


@dataclass(frozen=True, slots=True)
class Registration:
    """一件素材的来源登记（一个音色 / 一条 BGM / 一条跑酷素材）。"""

    kind: str
    id: str
    license: str | None
    proof: str | None
    proof_present: bool
    enabled: bool = True
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "license": self.license,
            "proof": self.proof,
            "proof_present": self.proof_present,
            "enabled": self.enabled,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ComplianceSnapshot:
    """一次留档体检的结论（面板直接显示）。"""

    notice: str
    items: tuple[Registration, ...]
    gaps: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """三样都留了档 ⇒ ``True``。**缺了不阻塞发布**（那是人的判断，不是程序的）。"""
        return not self.gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "notice": self.notice,
            "ok": self.ok,
            "items": [item.to_dict() for item in self.items],
            "gaps": list(self.gaps),
        }


def _present(raw: str | None) -> bool:
    """登记文件在不在盘上（``None`` / 空串 ⇒ 没登记）。"""
    return raw is not None and raw != "" and Path(raw).is_file()


def _voice_item(row: VoiceProfileRow) -> Registration:
    proof = row.proof_path
    return Registration(
        kind="voice",
        id=row.id,
        license=row.license,
        proof=proof,
        proof_present=_present(proof),
        enabled=row.enabled,
        note=None if _present(proof) else "没有来源登记（profile.json）⇒ 复刻的出处无从追溯",
    )


def _broll_item(row: BrollClipRow) -> Registration:
    # 跑酷素材的留档是**两样**：授权类型（必填列）与来源地址。
    # ``license`` 永远有值（不入库就进不来），所以这里缺的是来源地址 ——
    # 而它正是"这条素材从哪下的"唯一的答案。
    return Registration(
        kind="broll",
        id=row.id,
        license=row.license,
        proof=row.source_url,
        proof_present=row.source_url is not None and row.source_url != "",
        enabled=True,
        note=None if row.source_url else "没有来源地址（source_url）⇒ 授权范围无从核对",
    )


def _bgm_item(row: BgmTrackRow) -> Registration:
    proof = row.proof_path
    return Registration(
        kind="bgm",
        id=row.id,
        license=row.license,
        proof=proof,
        proof_present=_present(proof),
        enabled=True,
        note=None if _present(proof) else "没有授权书（proof_path）⇒ 曲库授权无从核对",
    )


#: 缺口那句话里的量词（``voice`` ⇒ "音色"）。中文没有单复数，但"一条"与"一个"不同。
_GAP_NOUNS: Final[dict[str, str]] = {"voice": "音色", "broll": "跑酷素材", "bgm": "BGM"}


def compliance_snapshot(connection: sqlite3.Connection, *, paths: StudioPaths) -> ComplianceSnapshot:
    """把三样素材的来源登记扫一遍（**只读，不修任何东西**）。

    ``paths`` 现在没被用到（登记路径都在库列里），仍然收下：留档校验迟早要落到
    "目录里的 ``profile.json`` 与库里的 ``proof_path`` 是不是同一个文件"，
    而那时需要的就是它。留一个参数比日后改签名便宜。
    """
    items: list[Registration] = []
    items.extend(_voice_item(row) for row in VoiceProfileRepo(connection).list_all())
    items.extend(_broll_item(row) for row in BrollClipRepo(connection).list_all())
    items.extend(_bgm_item(row) for row in BgmTrackRepo(connection).list_all())

    gaps: list[str] = []
    for item in items:
        if item.proof_present:
            continue
        noun = _GAP_NOUNS.get(item.kind, item.kind)
        gaps.append(f"{noun} {item.id}：{item.note}")
    return ComplianceSnapshot(notice=R2_NOTICE, items=tuple(items), gaps=tuple(gaps))
