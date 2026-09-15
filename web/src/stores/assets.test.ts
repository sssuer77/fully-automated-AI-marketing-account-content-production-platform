import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AssetItem,
  AssetKind,
  AssetsLibrary,
  AssetsStats,
  IngestBody,
  IngestReport,
  ScannedAsset,
} from "@/api/endpoints/assets";
import { ApiError } from "@/api/http";
import type { Envelope } from "@/ws/events";
import {
  ACTION_LABELS,
  ASSET_KINDS,
  ASSETS_LOG_SOURCE,
  KIND_LABELS,
  configureAssetsApi,
  coverageText,
  degradedText,
  formatDuration,
  isAssetsLog,
  isPlaceholder,
  itemTone,
  mediaUrlOf,
  scannedTone,
  sectionOf,
  shortfallText,
  thumbUrlOf,
  usableRangeText,
  useAssetsStore,
} from "./assets";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function broll(overrides: Partial<AssetItem> = {}): AssetItem {
  return {
    kind: "broll",
    id: "parkour_001",
    path: "D:/studio/data/assets/mc_parkour/parkour_001.mp4",
    duration_ms: 30_000,
    usable_from_ms: 0,
    usable_to_ms: null,
    width: 1920,
    height: 1080,
    fps: 30,
    has_text: false,
    tags: [],
    license: "cc0",
    thumb_path: "D:/studio/data/assets/.thumbs/broll/parkour_001.jpg",
    source_url: null,
    proof_path: null,
    licensed_to: null,
    use_count: 0,
    last_used_at: null,
    enabled: true,
    created_at: "2026-09-15T00:00:00Z",
    ...overrides,
  } as AssetItem;
}

function voice(overrides: Partial<AssetItem> = {}): AssetItem {
  return {
    kind: "voice",
    id: "bear_da",
    path: "D:/studio/data/voice_src/bear_da",
    ref_count: 2,
    total_duration_ms: 30_000,
    sample_rate: 24_000,
    peak_db: -3.0,
    text_path: "D:/studio/data/voice_src/bear_da/ref.txt",
    proof_path: null,
    license: null,
    source_url: null,
    licensed_to: null,
    use_count: 0,
    last_used_at: null,
    enabled: true,
    created_at: "2026-09-15T00:00:00Z",
    ...overrides,
  } as AssetItem;
}

function bgm(overrides: Partial<AssetItem> = {}): AssetItem {
  return {
    kind: "bgm",
    id: "bgm_001",
    path: "D:/studio/data/assets/bgm/bgm_001.mp3",
    duration_ms: 120_000,
    sample_rate: 44_100,
    channels: 2,
    bitrate_kbps: 320,
    loudness_lufs: -14,
    bpm: null,
    mood: null,
    tags: [],
    loopable: true,
    license: "cc0",
    source_url: null,
    proof_path: null,
    licensed_to: null,
    use_count: 0,
    last_used_at: null,
    enabled: true,
    created_at: "2026-09-15T00:00:00Z",
    ...overrides,
  } as AssetItem;
}

function stats(overrides: Partial<AssetsStats> = {}): AssetsStats {
  return {
    sections: [],
    degraded: true,
    note: "跑酷素材不够 ⇒ 出片会走黑屏降级",
    thresholds: {
      broll_min_clips: 60,
      broll_min_duration_ms: 1_800_000,
      bgm_min_duration_ms: 15_000,
      voice_min_segments: 2,
      voice_max_segments: 3,
      voice_segment_min_ms: 10_000,
      voice_segment_max_ms: 30_000,
    },
    licenses: ["authorized", "cc0", "purchased", "self_recorded"],
    ...overrides,
  };
}

function library(items: AssetItem[], overrides: Partial<AssetsLibrary> = {}): AssetsLibrary {
  const section = (kind: AssetKind, rows: AssetItem[], shortfall: string | null = null) => ({
    kind,
    root: `D:/studio/data/${kind}`,
    stats: {
      total: rows.length,
      enabled: rows.filter((row) => row.enabled).length,
      total_duration_ms: rows.length * 30_000,
      enabled_duration_ms: rows.filter((row) => row.enabled).length * 30_000,
    },
    items: rows,
    shortfall,
  });
  return {
    degraded: true,
    note: "跑酷素材不够 ⇒ 出片会走黑屏降级",
    sections: [
      section("broll", items.filter((item) => item.kind === "broll"), "启用的跑酷素材只有 0 条（建议 ≥ 60）"),
      section("voice", items.filter((item) => item.kind === "voice")),
      section("bgm", items.filter((item) => item.kind === "bgm")),
    ],
    ...overrides,
  };
}

function scanned(overrides: Partial<ScannedAsset> = {}): ScannedAsset {
  return {
    kind: "broll",
    id: "parkour_001",
    path: "D:/studio/data/assets/mc_parkour/parkour_001.mp4",
    stored: true,
    enabled: true,
    usable: true,
    action: "created",
    note: null,
    thumb_path: null,
    check: {
      kind: "broll",
      id: "parkour_001",
      ok: true,
      problems: [],
      warnings: [],
      info: null,
      segments: [],
      peaks: [],
    },
    ...overrides,
  } as ScannedAsset;
}

function report(overrides: Partial<IngestReport> = {}): IngestReport {
  const empty = {
    kind: "voice",
    root: "D:/studio/data/voice_src",
    root_missing: false,
    assets: [],
    strays: [],
    missing: [],
    stats: { total: 0, enabled: 0, total_duration_ms: 0, enabled_duration_ms: 0 },
    counts: {
      found: 0,
      created: 0,
      refreshed: 0,
      unchanged: 0,
      duplicate: 0,
      rejected: 0,
      strays: 0,
      missing: 0,
    },
  };
  return {
    dry_run: false,
    license: "cc0",
    sections: [
      {
        ...empty,
        kind: "broll",
        assets: [scanned()],
        counts: { ...empty.counts, found: 1, created: 1 },
      },
      empty,
      { ...empty, kind: "bgm" },
    ],
    totals: {
      created: 1,
      refreshed: 0,
      unchanged: 0,
      duplicate: 0,
      rejected: 0,
      missing: 0,
      strays: 0,
    },
    ...overrides,
  };
}

function envelope(data: Record<string, unknown>): Envelope {
  return { v: 1, type: "event", channel: "logs", seq: 1, ts: "2026-09-15T00:00:00Z", data };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("素材库面板的纯函数", () => {
  it("三类的顺序与中文标签与后端枚举一致", () => {
    expect(ASSET_KINDS).toEqual(["broll", "voice", "bgm"]);
    expect(KIND_LABELS.broll).toBe("跑酷素材");
    expect(ACTION_LABELS.duplicate).toBe("内容重复");
  });

  it("时长翻成人话", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(500)).toBe("500 ms");
    expect(formatDuration(30_000)).toBe("30 s");
    expect(formatDuration(90_000)).toBe("1 分 30 秒");
    expect(formatDuration(1_800_000)).toBe("30 分 00 秒");
  });

  it("按 kind 取一节；缺了返回 null（后端少给一节不该整屏炸）", () => {
    const data = library([broll()]);
    expect(sectionOf(data, "broll")?.items.length).toBe(1);
    expect(sectionOf({ ...data, sections: [] }, "bgm")).toBeNull();
    expect(sectionOf(null, "broll")).toBeNull();
  });

  it("缺口与降级文案取自后端（前端不重算判据）", () => {
    const section = sectionOf(library([broll()]), "broll");
    expect(shortfallText(section)).toContain("建议 ≥ 60");
    expect(degradedText(library([broll()]))).toContain("黑屏降级");
    expect(degradedText(library([broll()], { degraded: false, note: null }))).toBeNull();
    expect(degradedText(null)).toBeNull();
  });

  it("覆盖度把启用数与总时长摆在一起", () => {
    const section = sectionOf(library([broll(), broll({ id: "parkour_002", enabled: false })]), "broll");
    expect(coverageText(section)).toBe("1 / 2 条启用 · 30 s");
    expect(coverageText(null)).toBe("—");
  });

  it("一条素材的颜色：停用标黄、启用标绿", () => {
    expect(itemTone(broll())).toBe("ok");
    expect(itemTone(broll({ enabled: false }))).toBe("warn");
  });

  it("扫盘报告的颜色：拒绝与『这次没写库』都是红的，重复标黄", () => {
    expect(scannedTone(scanned())).toBe("ok");
    expect(scannedTone(scanned({ action: "duplicate" }))).toBe("warn");
    expect(scannedTone(scanned({ action: null, stored: false }))).toBe("error");
    expect(scannedTone(scanned({ usable: false }))).toBe("error");
  });

  it("可用区间：上界为空 = 到片尾；非跑酷素材不适用", () => {
    expect(usableRangeText(broll())).toBe("0 – 30000 ms");
    expect(usableRangeText(broll({ usable_from_ms: 1_500, usable_to_ms: 20_000 }))).toBe("1500 – 20000 ms");
    expect(usableRangeText(voice())).toBe("—");
  });

  it("缩略图 / 试听地址：没有缩略图给 null；音色是目录 ⇒ 不给播放器", () => {
    expect(thumbUrlOf(broll())).toBe("/api/v1/assets/parkour_001/thumb");
    expect(thumbUrlOf(broll({ thumb_path: null }))).toBeNull();
    expect(thumbUrlOf(voice())).toBeNull();
    expect(thumbUrlOf(bgm())).toBeNull();
    expect(mediaUrlOf(broll())).toBe("/api/v1/assets/parkour_001/media");
    expect(mediaUrlOf(voice())).toBeNull();
  });

  it("占位素材按 tags 标黄（音色没有 tags 列 ⇒ 如实返回 false，不猜）", () => {
    expect(isPlaceholder(broll())).toBe(false);
    expect(isPlaceholder(broll({ tags: ["placeholder"] }))).toBe(true);
    expect(isPlaceholder(bgm({ tags: ["placeholder"] }))).toBe(true);
    expect(isPlaceholder(voice())).toBe(false);
  });

  it("只认素材库自己写的那条日志帧（§04.5.12：不新增 WS 事件）", () => {
    expect(isAssetsLog(envelope({ kind: "log.appended", source: ASSETS_LOG_SOURCE }))).toBe(true);
    expect(isAssetsLog(envelope({ kind: "log.appended", source: "outputs" }))).toBe(false);
    expect(isAssetsLog(envelope({ kind: "asset.changed", source: "assets" }))).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("useAssetsStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("refresh 一次拉全：库 + 家底", async () => {
    const fetchAssets = vi.fn(async () => library([broll()]));
    const fetchAssetStats = vi.fn(async () => stats());
    configureAssetsApi({ fetchAssets, fetchAssetStats });

    const store = useAssetsStore();
    await store.refresh();

    expect(store.sections.length).toBe(3);
    expect(store.degraded).toBe(true);
    expect(store.licenses).toEqual(["authorized", "cc0", "purchased", "self_recorded"]);
    expect(store.loadError).toBeNull();
  });

  it("拉取失败**留着旧快照**（一屏素材不因一次抖动变空白）", async () => {
    configureAssetsApi({
      fetchAssets: vi.fn(async () => library([broll()])),
      fetchAssetStats: vi.fn(async () => stats()),
    });
    const store = useAssetsStore();
    await store.refresh();

    configureAssetsApi({
      fetchAssets: vi.fn(async () => {
        throw new ApiError("boom", 500, null);
      }),
    });
    await store.refresh();

    expect(store.loadError).toContain("boom");
    expect(store.sections.length).toBe(3);
  });

  it("预览模式：dry_run 透传，且**不重拉**（没写库就没什么可重拉的）", async () => {
    const ingestAssets = vi.fn(async (body: IngestBody) => report({ dry_run: body.dry_run ?? false }));
    const fetchAssets = vi.fn(async () => library([broll()]));
    configureAssetsApi({ ingestAssets, fetchAssets, fetchAssetStats: vi.fn(async () => stats()) });

    const store = useAssetsStore();
    store.dryRun = true;
    store.license = "purchased";
    const result = await store.scan("broll");

    expect(result?.dry_run).toBe(true);
    expect(ingestAssets).toHaveBeenCalledWith(
      expect.objectContaining({ dry_run: true, license: "purchased", kind: "broll" }),
    );
    expect(fetchAssets).not.toHaveBeenCalled();
    expect(store.report?.dry_run).toBe(true);
  });

  it("真入库之后重拉一次（库里变了）", async () => {
    const fetchAssets = vi.fn(async () => library([broll()]));
    configureAssetsApi({
      ingestAssets: vi.fn(async () => report()),
      fetchAssets,
      fetchAssetStats: vi.fn(async () => stats()),
    });

    const store = useAssetsStore();
    store.dryRun = false;
    await store.scan();

    expect(fetchAssets).toHaveBeenCalledTimes(1);
    expect(store.report?.totals.created).toBe(1);
  });

  it("入库失败 ⇒ 错误位有话说，且不会假装成功", async () => {
    configureAssetsApi({
      ingestAssets: vi.fn(async () => {
        throw new ApiError("授权类型不合法", 422, null);
      }),
    });

    const store = useAssetsStore();
    const result = await store.scan();

    expect(result).toBeNull();
    expect(store.error).toContain("授权类型不合法");
  });

  it("停用只交 enabled + kind（**不整行发上去**：整行会覆盖别人刚改的字段）", async () => {
    const patchAsset = vi.fn(async () => broll({ enabled: false }));
    configureAssetsApi({
      patchAsset,
      fetchAssets: vi.fn(async () => library([broll({ enabled: false })])),
      fetchAssetStats: vi.fn(async () => stats()),
    });

    const store = useAssetsStore();
    const ok = await store.setEnabled(broll(), false);

    expect(ok).toBe(true);
    expect(patchAsset).toHaveBeenCalledWith("parkour_001", { kind: "broll", enabled: false });
    expect(store.pendingId).toBeNull();
  });

  it("改字段失败 ⇒ 错误位有话说，返回 false", async () => {
    configureAssetsApi({
      patchAsset: vi.fn(async () => {
        throw new ApiError("可用区间为空", 422, null);
      }),
    });

    const store = useAssetsStore();
    const ok = await store.applyPatch(broll(), { usable_from_ms: 20_000, usable_to_ms: 1_000 });

    expect(ok).toBe(false);
    expect(store.error).toContain("可用区间为空");
    expect(store.pendingId).toBeNull();
  });
});
