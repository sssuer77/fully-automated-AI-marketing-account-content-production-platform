"""TTS 并发实测标定（T2.2 · 裁决 C8 / R4）—— 8 GB 卡上到底能同时念几句。

它回答的问题不是"服务快不快"，而是"**这台机器能扛几路**"
--------------------------------------------------------
原文 §01.6.2 写的是并发 3。R4 说 Turing 8 GB 且桌面常年占 1.5 GB，**必须实测**：
3 路同时推理要么 OOM，要么把 RTF 拖到比串行还慢。这个脚本就是那个判据 ——
它的结论直接回写 ``config/pools.yaml`` 的 ``voice.concurrency``。

**为什么在进程内跑，而不是打 HTTP 接口**
----------------------------------------
常驻服务自己带 GPU 串行信号量（``config/tts.yaml: server.concurrency``）。从外面
发 3 个请求，测到的是**排队**，不是"3 路同时算"。要标定显卡的容量，就得真的同时
调三次引擎 —— 所以这个脚本在 ``tts/.venv`` 里跑，直接驱动
:class:`~studio.tts.cosyvoice.CosyVoiceBackend`。

用法（**必须用推理子环境的解释器**）
------------------------------------
::

    tts/.venv/Scripts/python.exe scripts/bench_tts.py                     # 并发 1,2,3
    tts/.venv/Scripts/python.exe scripts/bench_tts.py --concurrency 1,2
    tts/.venv/Scripts/python.exe scripts/bench_tts.py --sentences 4 --json out.json

退出码
------
0 = 至少跑通了一路（有结论）；2 = 环境不对（没有 torch / 权重不在 / 用法错）。
**"一路都没跑通"算 2 不算 0** —— 一份没有读数的标定报告如果返回成功，
等于给"根本没测"发了一张合格证。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from studio.core.config import load_tts_config  # noqa: E402
from studio.core.errors import ErrorCode, StudioError  # noqa: E402
from studio.core.paths import StudioPaths  # noqa: E402
from studio.tts.cosyvoice import CosyVoiceBackend  # noqa: E402

#: 标定用的固定句子。**不许改**（除非重测一遍）：改了之后这次的读数与上次不可比。
BENCH_SENTENCES: tuple[str, ...] = (
    "这是全自动制片台的第一句并发标定测试。",
    "第二句稍微长一点，用来观察不同长度下的实时率变化。",
    "第三句再长一些，凑够三十来个字看看显存和速度会怎么走。",
    "第四句备用，句子数量可以命令行指定。",
)

#: 吞吐增益门槛：升一档至少要拿到「理想增益」的这个比例，否则不划算（T2.2 裁定 306）。
MIN_GAIN_RATIO: float = 0.4

#: 峰值显存的红线比例：超过它就认为"再往上加并发会 OOM"（留出桌面占用的余量）。
VRAM_HEADROOM: float = 0.85


@dataclass
class LevelResult:
    """一个并发档位的读数。"""

    concurrency: int
    ok: int = 0
    failed: int = 0
    oom: int = 0
    rtfs: list[float] = field(default_factory=list)
    durations_ms: list[int] = field(default_factory=list)
    wall_sec: float = 0.0
    peak_mb: int = 0
    first_error: str | None = None

    @property
    def rtf_median(self) -> float:
        return round(statistics.median(self.rtfs), 3) if self.rtfs else 0.0

    @property
    def audio_sec(self) -> float:
        return round(sum(self.durations_ms) / 1000, 1)


def _fail(message: str) -> int:
    print(f"[x] {message}", file=sys.stderr)
    return 2


def _require_torch() -> None:
    try:
        import torch  # noqa: F401, PLC0415
    except ImportError:
        raise SystemExit(
            "[x] 这个脚本要在推理子环境里跑（torch 只装在那里）：\n"
            "    tts\\.venv\\Scripts\\python.exe scripts/bench_tts.py"
        ) from None


def _run_level(
    backend: CosyVoiceBackend,
    *,
    concurrency: int,
    sentences: int,
    ref_wav: Path,
    ref_text: str,
    out_dir: Path,
) -> LevelResult:
    """跑一个并发档位：``concurrency`` 个线程同时调引擎，共 ``sentences`` 句。"""
    result = LevelResult(concurrency=concurrency)
    _reset_peak()
    started = time.monotonic()

    def one(index: int) -> tuple[int, float] | StudioError:
        text = BENCH_SENTENCES[index % len(BENCH_SENTENCES)]
        target = out_dir / f"c{concurrency}_s{index:02d}.wav"
        try:
            outcome = backend.synthesize(text, target, ref_wav=ref_wav, ref_text=ref_text)
        except StudioError as exc:
            return exc
        return outcome.duration_ms, outcome.rtf

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one, index) for index in range(sentences)]
        for future in futures:
            outcome = future.result()
            if isinstance(outcome, StudioError):
                result.failed += 1
                if outcome.code is ErrorCode.TTS_OOM:
                    result.oom += 1
                if result.first_error is None:
                    result.first_error = f"[{outcome.code}] {outcome.message}"
                continue
            duration_ms, rtf = outcome
            result.ok += 1
            result.durations_ms.append(duration_ms)
            result.rtfs.append(rtf)

    result.wall_sec = round(time.monotonic() - started, 2)
    result.peak_mb = _peak_mb()
    return result


def _reset_peak() -> None:
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def _peak_mb() -> int:
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return 0
    if not torch.cuda.is_available():
        return 0
    return int(torch.cuda.max_memory_allocated() // (1024 * 1024))


def _total_vram_mb() -> int:
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return 0
    if not torch.cuda.is_available():
        return 0
    _free, total = torch.cuda.mem_get_info()
    return int(total // (1024 * 1024))


def _recommend(results: list[LevelResult], total_mb: int) -> tuple[int, str]:
    """给出建议并发与理由（**说清依据**，别只给一个数字）。

    判定顺序（T2.2 裁定 306）：

    1. 先看**最保守的那一档**（并发最小的）能不能全绿、峰值不超红线 —— 不能就别谈往上加；
    2. 再逐档往上试，看的是**吞吐增益**（墙钟），不是单句快慢：升一档至少要拿到
       「理想增益」的 :data:`MIN_GAIN_RATIO`（并发翻倍 = 理想 +100%），拿不到就停在这一档；
    3. 任何一档峰值超红线也停。

    **为什么不按显存判**：8 GB 卡上 2/3 路根本不会 OOM（峰值只从 3643 涨到 4090 MB），
    但墙钟只快 13%，RTF 却涨了 1.6-2.5 倍 —— 显存说「放得下」，吞吐说「不划算」。
    """
    usable = [item for item in results if item.ok > 0 and item.failed == 0]
    if not usable:
        return 1, "没有任何档位全绿 ⇒ 退回默认 1（先看上面的报错）"
    ceiling = total_mb * VRAM_HEADROOM
    ordered = sorted(usable, key=lambda item: item.concurrency)
    base = ordered[0]
    if base.peak_mb > ceiling:
        return base.concurrency, (
            f"连最保守的并发 {base.concurrency} 峰值都有 {base.peak_mb} MB，"
            f"超过 {int(ceiling)} MB 红线（总显存 {total_mb} MB 的 {int(VRAM_HEADROOM * 100)}%）"
            f"⇒ 只能取最保守档"
        )
    best = base
    gain_pct = 0.0
    ideal_pct = 0.0
    stopped: str | None = None
    for item in ordered[1:]:
        if item.peak_mb > ceiling:
            stopped = f"并发 {item.concurrency} 峰值 {item.peak_mb} MB 超过红线，不再往上加"
            break
        ideal = (item.concurrency - best.concurrency) / best.concurrency
        gain = (best.wall_sec - item.wall_sec) / best.wall_sec
        if gain >= ideal * MIN_GAIN_RATIO:
            best = item
            gain_pct = gain * 100
            ideal_pct = ideal * 100
            continue
        stopped = (
            f"并发 {item.concurrency} 全绿、峰值 {item.peak_mb} MB 也没超线，"
            f"但吞吐只快 {gain * 100:.1f}%（理想 {ideal * 100:.0f}%，"
            f"门槛 {MIN_GAIN_RATIO * 100:.0f}%），却把中位 RTF 从 {best.rtf_median} 拖到 "
            f"{item.rtf_median} ⇒ 不值得，停在 {best.concurrency}"
        )
        break
    if best is base:
        if stopped is None:
            return best.concurrency, (
                f"只测了并发 {base.concurrency} 一档，全绿且峰值 {base.peak_mb} MB ≤ {int(ceiling)} MB"
            )
        return best.concurrency, (
            f"最保守档并发 {base.concurrency} 全绿、峰值 {base.peak_mb} MB ≤ {int(ceiling)} MB；"
            f"再往上加不划算 —— {stopped}"
        )
    head = (
        f"并发 {best.concurrency} 全绿、峰值 {best.peak_mb} MB ≤ {int(ceiling)} MB，"
        f"吞吐比 {base.concurrency} 路快 {gain_pct:.1f}%（达到理想 {ideal_pct:.0f}% 的 "
        f"{MIN_GAIN_RATIO * 100:.0f}% 以上）"
    )
    if stopped is not None:
        head += f"；{stopped}"
    return best.concurrency, head


def _render_markdown(
    results: list[LevelResult],
    *,
    recommended: int,
    reason: str,
    total_mb: int,
    stamp: str | None = None,
    voice_id: str | None = None,
    source: Path | None = None,
) -> str:
    now = stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# TTS 并发标定（T2.2 · 裁决 C8）",
        "",
        f"> 由 `scripts/bench_tts.py` 于 **{now}** 真机跑出，**不要手改** —— 重跑覆盖。",
        f"> 显卡总显存 **{total_mb} MB**；红线取 {int(VRAM_HEADROOM * 100)}%（留桌面占用）。",
        "> 判定看**吞吐**（墙钟），不看单句快慢 —— 单句 RTF 只用来判断有没有被换页拖垮。",
    ]
    if voice_id:
        lines.append(f"> 参考音色 **{voice_id}**。")
    lines += [
        "",
        "| 并发 | 成功 | 失败 | 其中 OOM | 音频合计 | 墙钟 | 中位 RTF | 峰值显存 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        lines.append(
            f"| {item.concurrency} | {item.ok} | {item.failed} | {item.oom} | "
            f"{item.audio_sec}s | {item.wall_sec}s | {item.rtf_median} | {item.peak_mb} MB |"
        )
    lines += [
        "",
        f"**结论：`voice.concurrency = {recommended}`** —— {reason}。",
        "",
        "回写位置：`config/pools.yaml → pools.voice.concurrency`，"
        "以及 `config/tts.yaml → server.concurrency`（服务自己的信号量）。",
        "",
        "> RTF = 合成耗时 / 音频时长。**它随并发上升**是正常的（GPU 要分时），",
        "> 关键是别涨到比串行还慢 —— 那说明显存已经开始换页了。",
        "",
        f"> 升档门槛：吞吐增益要达到「理想增益」的 **{int(MIN_GAIN_RATIO * 100)}%** 才算数",
        "> （并发翻倍 = 理想 +100%，即至少要快 40%）；拿不到就停，",
        "> 别拿 RTF 去换那点吞吐 —— 这块卡还要分给 render 池。",
    ]
    for item in results:
        if item.first_error is not None:
            lines += ["", f"> 并发 {item.concurrency} 的首个错误：`{item.first_error}`"]
    if source is not None:
        lines += ["", f"> 本报告由 `{source}` 的读数重出（**没有重跑 GPU**）。"]
    return chr(10).join(lines) + chr(10)


def main() -> int:
    parser = argparse.ArgumentParser(description="TTS 并发实测标定（T2.2）")
    parser.add_argument(
        "--concurrency",
        default="1,2,3",
        help="要测的并发档位，逗号分隔（默认 1,2,3）",
    )
    parser.add_argument("--sentences", type=int, default=3, help="每档跑几句（默认 3）")
    parser.add_argument("--voice", default=None, help="用哪个音色（默认取第一个可用的）")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "runbook" / "tts_concurrency.md"),
        help="标定报告写到哪（默认 docs/runbook/tts_concurrency.md）",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="顺带把原始读数写成 JSON（配 --from-json 时改写重算后的裁定）",
    )
    parser.add_argument(
        "--from-json",
        default=None,
        help="拿上次 --json 的读数重出报告（**不重跑 GPU**；--out 仍然生效）",
    )
    args = parser.parse_args()

    if args.from_json:
        json_out = Path(args.json) if args.json else None
        return _rerender(Path(args.from_json), Path(args.out), json_out)

    _require_torch()
    levels = []
    for raw in args.concurrency.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        if not chunk.isdigit() or int(chunk) < 1:
            return _fail(f"并发档位必须是正整数：{chunk!r}")
        levels.append(int(chunk))
    if not levels:
        return _fail("至少要给一个并发档位")

    paths = StudioPaths.from_env()
    config = load_tts_config(paths)
    voice_id = args.voice or _first_usable_voice(paths)
    if voice_id is None:
        return _fail(
            "没有可用的参考音（data/voice_src/<音色>/ 需要 ref_01.wav + ref.txt）；\n"
            "    没有它就没法零样本克隆，标定无从谈起"
        )
    voice_dir = paths.voice_src_dir / voice_id
    ref_wav = sorted(voice_dir.glob("ref_*.wav"))[0]
    ref_text = _first_line(voice_dir / "ref.txt")
    if ref_text is None:
        return _fail(f"{voice_dir} 里没有 ref.txt")

    print(f"[bench] 音色 {voice_id} · 参考音 {ref_wav.name} · 并发档位 {levels}")
    backend = CosyVoiceBackend(
        config.model.dir,
        revision=config.model.revision,
        source_dir=config.model.source_dir,
        matcha_dir=config.model.matcha_dir,
        fp16=config.model.fp16,
        sample_rate=config.model.sample_rate,
    )
    loaded = backend.load()
    print(
        f"[bench] 模型就绪：加载 {loaded.load_ms} ms · 显存 {loaded.vram_mb} MB · revision {loaded.revision}"
    )

    out_dir = paths.tmp_dir / "bench_tts"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for level in levels:
        print(f"[bench] 并发 {level} …")
        outcome = _run_level(
            backend,
            concurrency=level,
            sentences=max(1, args.sentences),
            ref_wav=ref_wav,
            ref_text=ref_text,
            out_dir=out_dir,
        )
        results.append(outcome)
        print(
            f"        ok={outcome.ok} failed={outcome.failed} oom={outcome.oom} "
            f"RTF中位={outcome.rtf_median} 峰值={outcome.peak_mb} MB 墙钟={outcome.wall_sec}s"
        )
        if outcome.first_error:
            print(f"        首个错误：{outcome.first_error}")

    total_mb = _total_vram_mb()
    recommended, reason = _recommend(results, total_mb)
    report = _render_markdown(
        results,
        recommended=recommended,
        reason=reason,
        total_mb=total_mb,
        voice_id=voice_id,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(out_path, report)
    print(f"[bench] 报告：{out_path}")
    print(f"[bench] 结论：voice.concurrency = {recommended}（{reason}）")

    if args.json:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "voice_id": voice_id,
            "sentences": max(1, args.sentences),
            "total_vram_mb": total_mb,
            "recommended_concurrency": recommended,
            "reason": reason,
            "levels": [
                {
                    "concurrency": item.concurrency,
                    "ok": item.ok,
                    "failed": item.failed,
                    "oom": item.oom,
                    "rtf_median": item.rtf_median,
                    "peak_mb": item.peak_mb,
                    "wall_sec": item.wall_sec,
                    "audio_sec": item.audio_sec,
                    "first_error": item.first_error,
                }
                for item in results
            ],
        }
        _write_text(Path(args.json), json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[bench] JSON：{args.json}")

    backend.unload()
    return 0 if any(item.ok > 0 for item in results) else 2


def _write_text(path: Path, text: str) -> None:
    """按**仓库的行尾约定**落盘（`.gitattributes`: `* text=auto eol=lf`）。

    直接 `Path.write_text()` 在 Windows 上写的是 CRLF —— 每次重跑标定，这两份
    报告都会以「整文件重写」的形式出现在 diff 里（其实一个读数都没变）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=chr(10))


def _rerender(source: Path, out_path: Path, json_out: Path | None = None) -> int:
    """拿上次 ``--json`` 的读数重出报告（**不碰 GPU**）。

    存在的理由：报告里的文字（门槛、结论措辞）会随判定逻辑改，读数不会。
    改一次措辞就重跑一次 40 秒的 GPU 标定，是拿显卡换排版。
    """
    if not source.is_file():
        return _fail(f"找不到读数文件：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail(f"{source} 不是合法 JSON：{exc}")
    raw_levels = payload.get("levels") or []
    if not raw_levels:
        return _fail(f"{source} 里没有 levels 读数")
    results = [
        LevelResult(
            concurrency=int(item["concurrency"]),
            ok=int(item.get("ok", 0)),
            failed=int(item.get("failed", 0)),
            oom=int(item.get("oom", 0)),
            rtfs=[float(item["rtf_median"])] if item.get("rtf_median") else [],
            durations_ms=[int(float(item.get("audio_sec", 0.0)) * 1000)],
            wall_sec=float(item.get("wall_sec", 0.0)),
            peak_mb=int(item.get("peak_mb", 0)),
            first_error=item.get("first_error"),
        )
        for item in raw_levels
    ]
    total_mb = int(payload.get("total_vram_mb") or _total_vram_mb())
    recommended, reason = _recommend(results, total_mb)
    stamp = (payload.get("generated_at") or "").replace("T", " ")[:16] or None
    report = _render_markdown(
        results,
        recommended=recommended,
        reason=reason,
        total_mb=total_mb,
        stamp=stamp,
        voice_id=payload.get("voice_id"),
        source=source,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(out_path, report)
    print(f"[bench] 重出报告：{out_path}")
    print(f"[bench] 结论：voice.concurrency = {recommended}（{reason}）")
    if json_out is not None:
        payload["recommended_concurrency"] = recommended
        payload["reason"] = reason
        payload["rerendered_at"] = datetime.now().isoformat(timespec="seconds")
        json_out.parent.mkdir(parents=True, exist_ok=True)
        _write_text(json_out, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[bench] 读数：{json_out}")
    return 0


def _first_usable_voice(paths: StudioPaths) -> str | None:
    for child in sorted(paths.voice_src_dir.glob("*")):
        if not child.is_dir():
            continue
        if sorted(child.glob("ref_*.wav")) and _first_line(child / "ref.txt") is not None:
            return child.name
    return None


def _first_line(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return line.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
