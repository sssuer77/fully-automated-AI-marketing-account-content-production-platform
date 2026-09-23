"""参考音入库（T2.4 · §04.3.1 / R2）—— 把「我录的那几段」变成「配音能用的音色」。

这个脚本回答一个问题：**我录的音，到底能不能用？**
它做三件事，一件都不多做：扫目录 → 按 §4.3.1 的阈值体检 → 通过的写进 ``voice_profiles``。

判定**不在这里重写**
-------------------
阈值（段数 / 时长 / 采样率 / 峰值）与旁车文件的检查全在
:mod:`studio.assets.validate` 的 ``check_voice`` 里，入库走
:meth:`studio.services.asset_service.AssetService.ingest` —— 与素材库面板点
「扫描并入库」是**同一条代码路径**。所以"命令行说能进、面板说不能"这件事
结构上不会发生；本脚本只负责把它打印成人话。

放什么、放哪儿
--------------
```
data/voice_src/<音色 id>/
    ref_01.wav  ref_02.wav  ...   段数**不设上限**（越多越稳），每段 2–30 秒
    ref.txt                       与 wav **一一对应**的逐字文本（每行一段，顺序同文件名）
    profile.json                  来源登记（URL / 录制日期 / 授权说明）⇒ R2 合规留档
```

音色 id **随便起**（小写字母 / 数字 / 下划线），它和"剧本里谁在说话"是两件事 ——
对应关系在**配音面板**上设（``voice_map``：逻辑角色 → 音色 id）。所以换一个音色
不需要改代码，只需要：改目录名 + 在面板上把映射指过去。

用法（在**已 dot-source `scripts/env.ps1`** 的会话里）::

    uv run python scripts/ingest_voice_src.py                    # 扫 data/voice_src/ 全部
    uv run python scripts/ingest_voice_src.py --voice my_voice   # 只处理一个
    uv run python scripts/ingest_voice_src.py --dry-run          # 只看能不能进，一个字节都不写库
    uv run python scripts/ingest_voice_src.py --voice a --voice b --license authorized

退出码：全部通过 ⇒ 0；**有任何一个被拒** ⇒ 1（方便挂进批处理）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from studio.assets.layout import AssetKind  # noqa: E402
from studio.assets.validate import (  # noqa: E402
    VOICE_MIN_SAMPLE_RATE,
    VOICE_PEAK_CEILING_DB,
    VOICE_SEGMENT_MAX_MS,
    VOICE_SEGMENT_MIN_MS,
)
from studio.core.paths import StudioPaths  # noqa: E402
from studio.db import connect, migrate  # noqa: E402
from studio.db.repositories import IngestAction  # noqa: E402
from studio.services.asset_service import LICENSES, AssetService, ScannedAsset, ScanReport  # noqa: E402

#: 入库动作 ⇒ 打印的那句话（``None`` = 这次没写库，是**失败**不是"无变化"）
_ACTION_TEXT: Final[dict[IngestAction, str]] = {
    IngestAction.CREATED: "已入库",
    IngestAction.REFRESHED: "已刷新",
    IngestAction.UNCHANGED: "无变化",
    IngestAction.DUPLICATE: "内容重复，未入库",
}


def _requirements() -> str:
    """参考音的硬要求（**拒绝入库**的那三条）+ 旁车文件的作用。

    只在"没扫到东西"或"有东西被拒"时打印：那正是用户需要照着改的时候。
    每次跑都打一遍，会把成功那一次的结论淹掉。
    """
    seconds_min = VOICE_SEGMENT_MIN_MS // 1000
    seconds_max = VOICE_SEGMENT_MAX_MS // 1000
    return (
        "参考音要求（§4.3.1，前三条不达标**拒绝入库**）：\n"
        "  段数     **不设上限**（越多越稳：合成时会把各段拼成一段 prompt），至少 1 段；\n"
        "           文件名 ref_01.wav 这种（两位数是排序，ref.txt 第 N 行 ↔ 第 N 段）\n"
        f"  单段时长 {seconds_min}–{seconds_max} 秒\n"
        f"  采样率   ≥ {VOICE_MIN_SAMPLE_RATE // 1000} kHz\n"
        f"  峰值     ≤ {VOICE_PEAK_CEILING_DB} dBFS（削波会被一起复刻）\n"
        "  旁车文件 ref.txt（逐字文本）/ profile.json（来源登记）缺了只提醒，不拦\n"
        "\n"
        "目录：data/voice_src/<音色 id>/ref_01.wav ...\n"
        "音色 id 随便起；它对应剧本里的谁，在**配音面板**上设（不用改代码）。"
    )


def _segment_summary(asset: ScannedAsset) -> str:
    """一条音色的体检摘要（段数 / 总时长 / 采样率 / 峰值）—— 只看得到的东西。"""
    segments = asset.check.segments
    if not segments:
        return "读不出参考音"
    total_ms = sum(item.duration_ms or 0 for item in segments)
    rate = segments[0].sample_rate
    peaks = [value for value in asset.check.peaks if value is not None]
    parts = [f"{len(segments)} 段", f"{total_ms / 1000:.1f} 秒"]
    if rate is not None:
        parts.append(f"{rate} Hz")
    if peaks:
        parts.append(f"峰值 {max(peaks):.1f} dBFS")
    return " · ".join(parts)


def _print_asset(asset: ScannedAsset, *, dry_run: bool) -> None:
    mark = "✅" if asset.check.ok else "❌"
    if asset.check.ok and asset.check.warnings:
        mark = "⚠️"
    if not asset.check.ok:
        action = "不能入库"
    elif dry_run:
        action = "能入库（预览）"
    elif asset.action is None:
        # 体检过了、库却没写成（``_store`` 里抛了）—— 这是**失败**，不是"无变化"
        action = "入库失败"
    else:
        action = _ACTION_TEXT.get(asset.action, "已入库")
    print(f"  {mark} {asset.id:<20} {_segment_summary(asset)}  → {action}")
    for problem in asset.check.problems:
        print(f"       ✗ {problem.message}")
    for warning in asset.check.warnings:
        print(f"       ! {warning.message}")
    if asset.action is None and not dry_run and asset.check.ok:
        print(f"       ✗ {asset.note or '入库没写成，原因没记下来'}")


def _print_report(report: ScanReport, *, dry_run: bool) -> int:
    section = report.section(AssetKind.VOICE)
    if not section.assets:
        print("[参考音] 没扫到任何音色目录。")
        if section.root_missing:
            print(f"[参考音] 目录还不存在：{section.root}")
        elif section.strays:
            print(f"[参考音] 目录里有 {len(section.strays)} 个东西不合规（没被当成音色）：")
            for stray in section.strays:
                print(f"         {stray}")
        print()
        print(_requirements())
        return 1

    for asset in section.assets:
        _print_asset(asset, dry_run=dry_run)

    totals = report.to_dict()["totals"]
    assert isinstance(totals, dict)
    rejected = [item for item in section.assets if not item.check.ok]
    ok = len(section.assets) - len(rejected)
    print()
    if dry_run:
        print(f"[参考音] 预览（**一个字节都没写库**）：{ok} 个能进 / {len(rejected)} 个不能进")
        print("[参考音] 去掉 --dry-run 就会真的入库。")
    else:
        print(
            "[参考音] 合计：新增 {created} / 刷新 {refreshed} / 未变 {unchanged}"
            " / 重复 {duplicate} / 未入库 {rejected}".format(**totals)
        )

    if rejected:
        print()
        print(_requirements())
        return 1

    if not dry_run:
        print("[参考音] 下一步：打开配音面板，把角色指到这个音色上（或者直接看它已经在列表里）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="参考音入库（T2.4 · §04.3.1 / R2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python scripts/ingest_voice_src.py --dry-run\n"
            "  python scripts/ingest_voice_src.py --voice my_voice\n"
        ),
    )
    parser.add_argument(
        "--voice",
        action="append",
        metavar="ID",
        help="只处理这个音色 id（可重复给多个）；不给 ⇒ 扫 data/voice_src/ 下全部",
    )
    parser.add_argument(
        "--license",
        choices=sorted(LICENSES),
        default=None,
        help="授权类型（**只对本次新入库的**生效；已入库的按它自己那份走）。"
        "音色可以不给 —— 它的合规前置是目录里的 profile.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只体检、不写库（先看看能不能用）",
    )
    args = parser.parse_args()

    paths = StudioPaths.from_env()
    paths.ensure_runtime_dirs()
    print(f"[参考音] 目录：{paths.voice_src_dir}")
    print(f"[参考音] 库：{paths.db_file}")

    migrate(paths.db_file)
    connection = connect(paths.db_file)
    try:
        service = AssetService(connection, paths=paths, log=None)
        ids = None if args.voice is None else tuple(args.voice)
        if args.dry_run:
            report = service.scan(kind=AssetKind.VOICE, ids=ids, license=args.license)
        else:
            report = service.ingest(kind=AssetKind.VOICE, ids=ids, license=args.license)
    finally:
        connection.close()

    return _print_report(report, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
