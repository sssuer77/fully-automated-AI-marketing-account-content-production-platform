"""跨面板共用的请求契约零件（T4.10 · 裁定 155）。

这里只放**两个以上面板真的共用**的东西。一旦它开始长业务字段，就会变成
「什么都往里塞」的口袋，``schemas/<面板>.py`` 的边界也就没了。

目前只有一样：``reason``（操作理由）
------------------------------------
总览台的「暂停 / 恢复 / 一键全自动」与四池控制台的「调并发 / 重投死信」写的是
**同一种**理由字段：给人看的附注、长度有上限、空串按「没写」处理。

它必须只有一份上限。两处各写一个数，迟早出现「暂停能写 200 字、调并发只能写
80 字」这种没人解释得清的差异 —— 而理由是**留痕**的一部分，留痕字段的宽度
不一致，等于审计表里同一列有两套语义。
"""

from __future__ import annotations

from typing import Final

__all__ = ["MAX_REASON", "clean_reason"]

#: 操作理由的长度上限（留痕是给人看的，不是给人写论文的）
MAX_REASON: Final[int] = 200


def clean_reason(reason: str | None) -> str | None:
    """理由：去空白、截断到 :data:`MAX_REASON`、空串按「没写」处理。

    截断而不是拒绝：理由只是给人看的附注，为了多打几个字把整个「暂停」打回
    是本末倒置（与确认闸「退回必须写理由」不同 —— 那个理由会被模型读进去）。
    """
    if reason is None:
        return None
    text = reason.strip()
    return text[:MAX_REASON] or None
