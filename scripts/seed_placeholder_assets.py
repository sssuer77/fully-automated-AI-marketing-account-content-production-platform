"""造一套**占位素材**（T4.8.8 · §3.3.14 / §4.3.1）—— 让整条流水线在"素材还没到位"时也能跑通。

为什么要这个脚本
----------------
原型的验收线是「能播 + 可复现 + 面板如实」。而"跑酷素材还没买 / 还没剪"是**常态**，
不是异常：没有这个脚本，整条链路就卡在"素材库是空的 ⇒ 黑屏降级"，T3 的渲染编译
也就没有真实输入可测。

它造出来的东西**一律带 `placeholder` 标记**（入库时写进 `tags`）：面板上标黄，
一眼能看出"这不是能发出去的素材"。**绝不假装成正式素材** —— 那样的话，
"这条片子为什么这么难看"会变成一条查不完的悬案。

造什么
------
- 跑酷：``data/assets/mc_parkour/parkour_%03d.mp4`` —— ``testsrc2`` 720x1280@30
  （带时间码的彩条：能播、能看出是占位、还能用来核对音画不同步）；
- BGM：``data/assets/bgm/bgm_placeholder_%02d.mp3`` —— 正弦音，3 分钟一条；
- 音色：``data/voice_src/<id>/ref_0N.wav`` + ``ref.txt`` + ``profile.json`` ——
  15 秒正弦音（峰值 −6 dBFS ⇒ 不削波，符合 §4.3.1 的 ≤ −1.0 dBFS）。

用法（必须在**已 dot-source `scripts/env.ps1`** 的会话里跑）::

    uv run python scripts/seed_placeholder_assets.py                    # 造 60 条跑酷 + 3 条 BGM + 2 个音色
    uv run python scripts/seed_placeholder_assets.py --clips 6          # 快速冒烟（6 条）
    uv run python scripts/seed_placeholder_assets.py --force            # 覆盖已有文件
    uv run python scripts/seed_placeholder_assets.py --no-ingest        # 只造文件，不入库

默认 60 条 × 32 秒 = 32 分钟，**刚好过** §T4.8 的判据线（``clips >= 60 且 >= 30min``），
所以造完之后面板上"够不够用"那一行是绿的 —— 否则你会分不清是脚本没生效，
还是素材本来就少。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from studio.assets.layout import AssetKind  # noqa: E402
from studio.core.errors import StudioError  # noqa: E402
from studio.core.media import ffmpeg_binary, run_command  # noqa: E402
from studio.core.paths import StudioPaths  # noqa: E402
from studio.db import connect, migrate  # noqa: E402
from studio.services.asset_service import AssetService  # noqa: E402

#: 占位标记（面板据此标黄；**绝不**当成正式素材）
PLACEHOLDER_TAG = "placeholder"

#: 占位素材的授权类型：自己生成的 ⇒ 自录（R2 留痕仍然要有）
PLACEHOLDER_LICENSE = "self_recorded"

#: 默认规模：60 条 × 32 秒 = 32 分钟（§T4.8 的判据线是 ≥ 60 条且 ≥ 30 分钟）
DEFAULT_CLIPS = 60
DEFAULT_CLIP_SECONDS = 32

#: 默认 BGM：3 条 × 3 分钟
DEFAULT_TRACKS = 3
DEFAULT_TRACK_SECONDS = 180

#: 默认音色：两个（熊大 / 熊二的原型位），每个 2 段 × 15 秒
DEFAULT_VOICES = ("bear_da", "bear_xiong")
VOICE_SEGMENTS = 2
VOICE_SEGMENT_SECONDS = 15

#: 参考音的峰值（−6 dBFS ⇒ 稳稳低于 §4.3.1 的 −1.0 dBFS 上限）
VOICE_PEAK = "-6dB"

#: 时间预算：60 条 32 秒的 testsrc 在这台机器上大约一两分钟
FFMPEG_TIMEOUT_SEC = 600


def _run(argv: list[str]) -> None:
    result = run_command(argv, timeout=FFMPEG_TIMEOUT_SEC)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise StudioError(
            f"ffmpeg 失败（退出码 {result.returncode}）：{' / '.join(tail)}",
            context={"argv": argv},
            remediation="确认 ffmpeg 在 PATH 里（或设 STUDIO_FFMPEG_BIN），再用 --clips 1 冒烟一次",
        )


def _skip(target: Path, *, force: bool) -> bool:
    return target.is_file() and not force


def make_clips(paths: StudioPaths, *, count: int, seconds: int, force: bool) -> list[str]:
    """跑酷占位片（返回生成 / 已存在的 id 列表）。"""
    root = paths.mc_parkour_dir
    root.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for index in range(1, count + 1):
        asset_id = f"parkour_{index:03d}"
        target = root / f"{asset_id}.mp4"
        ids.append(asset_id)
        if _skip(target, force=force):
            continue
        # 每条给一个色相偏移：``testsrc2`` 同参数生成的画面**逐字节相同**，
        # 会被内容去重（sha256）当重复素材拒收 —— 那正好是这条守卫该干的事，
        # 所以是生成侧把每条做得**确实不一样**，而不是去把守卫关掉。
        _run(
            [
                ffmpeg_binary(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=720x1280:rate=30:duration={seconds}",
                "-vf",
                f"hue=h={index * 7}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
    return ids


def make_tracks(paths: StudioPaths, *, count: int, seconds: int, force: bool) -> list[str]:
    """BGM 占位音（正弦音，够长 ⇒ 能验证循环播放）。"""
    root = paths.bgm_dir
    root.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for index in range(1, count + 1):
        asset_id = f"bgm_placeholder_{index:02d}"
        target = root / f"{asset_id}.mp3"
        ids.append(asset_id)
        if _skip(target, force=force):
            continue
        frequency = 180 + 40 * index
        _run(
            [
                ffmpeg_binary(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={seconds}",
                "-af",
                "volume=-18dB",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(target),
            ]
        )
    return ids


def make_voices(paths: StudioPaths, *, voices: tuple[str, ...], force: bool) -> list[str]:
    """零样本参考音占位（目录结构与人提供的**完全一致**）。"""
    ids: list[str] = []
    for voice_id in voices:
        root = paths.voice_src_dir / voice_id
        root.mkdir(parents=True, exist_ok=True)
        ids.append(voice_id)
        for index in range(1, VOICE_SEGMENTS + 1):
            target = root / f"ref_{index:02d}.wav"
            if _skip(target, force=force):
                continue
            frequency = 160 if voice_id.endswith("da") else 220
            _run(
                [
                    ffmpeg_binary(),
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency + 20 * index}:duration={VOICE_SEGMENT_SECONDS}",
                    "-af",
                    f"volume={VOICE_PEAK}",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ]
            )
        text = root / "ref.txt"
        if not _skip(text, force=force):
            text.write_text(
                f"（占位参考音 {voice_id}）这段文字只为了满足目录约定：真实使用时，"
                "这里要换成参考音里**逐字**说的那句话。\n",
                encoding="utf-8",
            )
        profile = root / "profile.json"
        if not _skip(profile, force=force):
            profile.write_text(
                "{\n"
                f'  "id": "{voice_id}",\n'
                '  "origin": "generated",\n'
                '  "note": "占位参考音：由 scripts/seed_placeholder_assets.py 生成，'
                '不可用于正式出片"\n'
                "}\n",
                encoding="utf-8",
            )
    return ids


def ingest(paths: StudioPaths, *, ids: dict[AssetKind, list[str]]) -> str:
    """把刚造出来的素材入库并打上 `placeholder` 标记（**只处理这些 id**）。

    只按 ``ids`` 过滤入库：整库全扫会把用户自己放进去的素材也一起入库，
    而它们可能还没填授权 —— 那会把"我什么都没干"变成一串拒绝记录。
    """
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    try:
        service = AssetService(connection, paths=paths, log=None)
        created = 0
        for kind, wanted in ids.items():
            report = service.ingest(kind=kind, ids=wanted, license=PLACEHOLDER_LICENSE)
            created += report.total_created
            rejected = report.total_rejected
            if rejected > 0:
                print(f"[seed] {kind} 有 {rejected} 条没通过体检（面板的扫盘报告里能看到原因）")
        # 标记放在入库之后：`upsert` 只刷新机器事实，**不覆盖** `tags`（陷阱 #188），
        # 所以这一步不会被下一次重扫抹掉。
        #
        # 音色**没有** `tags` 列（`voice_profiles` 的 DDL 如此，PATCH 白名单里也没有），
        # 它的占位标记写在目录里的 `profile.json`（`origin: generated`）—— 那是它本来就有的
        # 来源登记位，比硬塞一个没有的列诚实。
        tagged = 0
        for kind, wanted in ids.items():
            if kind is AssetKind.VOICE:
                continue
            for asset_id in wanted:
                if service.get(kind, asset_id) is None:
                    continue
                service.patch(
                    kind,
                    asset_id,
                    {"tags": [PLACEHOLDER_TAG]},
                    actor="system",
                    source="cli",
                )
                tagged += 1
        return f"入库 {created} 条，已标记 {tagged} 条为 {PLACEHOLDER_TAG}"
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="造一套占位素材（T4.8.8）")
    parser.add_argument("--clips", type=int, default=DEFAULT_CLIPS, help="跑酷条数")
    parser.add_argument("--clip-seconds", type=int, default=DEFAULT_CLIP_SECONDS, help="单条跑酷秒数")
    parser.add_argument("--tracks", type=int, default=DEFAULT_TRACKS, help="BGM 条数")
    parser.add_argument("--track-seconds", type=int, default=DEFAULT_TRACK_SECONDS, help="单条 BGM 秒数")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    parser.add_argument("--no-ingest", action="store_true", help="只造文件，不入库")
    args = parser.parse_args()

    paths = StudioPaths.from_env()
    paths.ensure_runtime_dirs()

    clips = make_clips(paths, count=args.clips, seconds=args.clip_seconds, force=args.force)
    tracks = make_tracks(paths, count=args.tracks, seconds=args.track_seconds, force=args.force)
    voices = make_voices(paths, voices=DEFAULT_VOICES, force=args.force)

    total_seconds = args.clips * args.clip_seconds
    print(
        f"[seed] 跑酷 {len(clips)} 条 / {total_seconds // 60} 分 {total_seconds % 60} 秒 · "
        f"BGM {len(tracks)} 条 · 音色 {len(voices)} 个"
    )
    print(f"[seed] 目录：{paths.mc_parkour_dir} · {paths.bgm_dir} · {paths.voice_src_dir}")
    if total_seconds < 30 * 60 or len(clips) < 60:
        print("[seed] 提醒：还没到 §T4.8 的判据线（≥ 60 条且 ≥ 30 分钟），面板上会显示缺口。")

    if args.no_ingest:
        print("[seed] 跳过入库（--no-ingest）。下一步：在素材库面板点「扫描并入库」。")
        return 0

    try:
        summary = ingest(
            paths,
            ids={
                AssetKind.BROLL: clips,
                AssetKind.BGM: tracks,
                AssetKind.VOICE: voices,
            },
        )
    except StudioError as exc:
        print(f"[seed] 入库失败：{exc.message}")
        print("[seed] 文件已经造好了，可以稍后在素材库面板里手动入库。")
        return 1
    print(f"[seed] {summary}")
    print("[seed] 下一步：打开素材库面板确认三类都有内容（占位件会标黄）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
