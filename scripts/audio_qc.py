"""响度 / 真峰值 QC（T3.7 · §06.4 门禁 2）—— 量**盘上那个文件**，不是量"应该多响"。

它回答一个问题
--------------
"这支片子现在到底多响，过不过发布门禁？" 判据来自 ``config/publish.yaml → precheck``
（``lufs ∈ [lufs_min, lufs_max]``、``true_peak ≤ true_peak_max_dbtp``），**不在这里再写
一份阈值** —— 阈值写两处，改了一处就会出现"脚本说过了、发布门禁说没过"。

三种用法
--------
::

    python scripts/audio_qc.py --task <task_id>                    # 读 manifest.json 找成片
    python scripts/audio_qc.py --mix final.mp4                     # 直接指一个文件
    python scripts/audio_qc.py --in voice_master.wav --mix f.mp4   # 顺带量人声母带

``--in`` 量的是**人声母带**（配音量得对不对），``--mix`` 量的是**成片**（发布门禁读的
就是它）。两个数摆在一起看，才能分清"人声本身就轻"和"混音 / 编码把它压下去了"。

退出码
------
0 = 在门禁内；1 = 超标；2 = 用法 / 环境错（文件找不到、配置没通过校验、量不出来）。
**"量不出来"算 2 不算 0** —— 一份量不出响度的 QC 报告如果返回成功，
等于给"根本没检查"发了一张合格证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from studio.core.config import PrecheckConfig, load_config  # noqa: E402
from studio.core.errors import StudioError  # noqa: E402
from studio.core.paths import StudioPaths  # noqa: E402
from studio.render.mixdown import MixSettings, measure_file  # noqa: E402


def _fail(message: str) -> int:
    print(f"[x] {message}", file=sys.stderr)
    return 2


def _measure(path: Path, settings: MixSettings, label: str) -> tuple[float, float] | None:
    """量一个文件；返回 ``(lufs, true_peak)``；量不出来 ⇒ ``None``（并说明为什么）。"""
    if not path.is_file():
        print(f"[x] 找不到{label}：{path}", file=sys.stderr)
        return None
    measured = measure_file(path, settings=settings)
    if measured is None:
        print(f"[x] {label}量不出响度（ffmpeg 没给出 JSON 报告）：{path}", file=sys.stderr)
        return None
    return measured.input_i, measured.input_tp


def _resolve_mix(args: argparse.Namespace, paths: StudioPaths) -> tuple[Path | None, dict[str, Any]]:
    """定位要量的成片；返回 ``(路径, manifest 内容)``（直接给 ``--mix`` ⇒ manifest 为空）。"""
    if args.mix is not None:
        return args.mix, {}

    manifest = paths.manifest_json(args.task)
    if not manifest.is_file():
        print(
            f"[x] 任务 {args.task} 还没有 manifest：{manifest}（先跑 `studio render make`）",
            file=sys.stderr,
        )
        return None, {}
    payload: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))
    recorded = payload.get("final")
    if not isinstance(recorded, str) or not Path(recorded).is_file():
        print(f"[x] manifest 里记的成片不在盘上：{recorded}", file=sys.stderr)
        return None, {}
    return Path(recorded), payload


def _verdict(lufs: float, true_peak: float, precheck: PrecheckConfig) -> tuple[bool, bool, bool]:
    loudness_ok = precheck.lufs_min <= lufs <= precheck.lufs_max
    peak_ok = true_peak <= precheck.true_peak_max_dbtp
    return loudness_ok, peak_ok, loudness_ok and peak_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="响度 / 真峰值 QC（发布门禁 2）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", help="任务 id：从 data/work/<task>/manifest.json 读成片路径")
    source.add_argument("--mix", type=Path, help="直接指定成片")
    parser.add_argument("--in", dest="voice", type=Path, help="人声母带（可选，顺带量一下）")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON")
    args = parser.parse_args(argv)

    try:
        loaded = load_config()
    except StudioError as error:
        return _fail(f"配置没通过校验：{error.message}")

    settings = MixSettings.from_config(loaded.bundle.outputs.audio)
    precheck = loaded.bundle.publish.precheck
    mix, manifest = _resolve_mix(args, loaded.paths)
    if mix is None:
        return 2

    voice_reading = _measure(args.voice, settings, "人声母带") if args.voice else None
    mix_reading = _measure(mix, settings, "成片")
    if mix_reading is None:
        return 2

    lufs, true_peak = mix_reading
    loudness_ok, peak_ok, passed = _verdict(lufs, true_peak, precheck)

    if args.json:
        print(
            json.dumps(
                {
                    "mix": str(mix),
                    "lufs": round(lufs, 2),
                    "true_peak": round(true_peak, 2),
                    "lufs_min": precheck.lufs_min,
                    "lufs_max": precheck.lufs_max,
                    "true_peak_max_dbtp": precheck.true_peak_max_dbtp,
                    "loudness_ok": loudness_ok,
                    "true_peak_ok": peak_ok,
                    "passed": passed,
                    "voice": (
                        {"lufs": round(voice_reading[0], 2), "true_peak": round(voice_reading[1], 2)}
                        if voice_reading
                        else None
                    ),
                    "degraded": manifest.get("degraded"),
                    "degrade_reason": manifest.get("degrade_reason"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if passed else 1

    print(f"成片      {mix}")
    if voice_reading is not None:
        print(f"人声母带  {args.voice}  {voice_reading[0]:.2f} LUFS / {voice_reading[1]:.2f} dBTP")
    print(
        f"成片读数  {lufs:.2f} LUFS / {true_peak:.2f} dBTP"
        f"（门禁 {precheck.lufs_min:g} ≤ LUFS ≤ {precheck.lufs_max:g}，"
        f"dBTP ≤ {precheck.true_peak_max_dbtp:g}）"
    )
    if manifest.get("degraded"):
        print(f"降级      {manifest.get('degrade_reason')}（画质降级不影响响度门禁）")
    if passed:
        print("[v] 通过")
        return 0
    if not loudness_ok:
        print(f"[x] 响度不在门禁内：{lufs:.2f} LUFS")
    if not peak_ok:
        print(f"[x] 真峰值超标：{true_peak:.2f} dBTP > {precheck.true_peak_max_dbtp:g}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
