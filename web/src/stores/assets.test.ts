import { readFileSync } from "node:fs";

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AssetItem,
  AssetKind,
  AssetPage,
  AssetsStats,
  IngestBody,
  IngestReport,
  ScannedAsset,
  UploadedFile,
  UploadResult,
} from "@/api/endpoints/assets";
import { ApiError } from "@/api/http";
import type { Envelope } from "@/ws/events";
import {
  ACTION_LABELS,
  ASSET_KINDS,
  ASSETS_LOG_SOURCE,
  ASSETS_QUERY_DEBOUNCE_MS,
  DEFAULT_PAGE_SIZE,
  EDIT_FIELDS,
  ENABLED_FILTERS,
  KIND_LABELS,
  LICENSE_LABELS,
  PAGE_SIZE_OPTIONS,
  UPLOAD_LABELS,
  acceptFor,
  changedFields,
  configureAssetsApi,
  coverageText,
  degradedText,
  enabledParam,
  fieldDraft,
  formatDuration,
  isAssetsLog,
  isPlaceholder,
  itemStateText,
  itemTone,
  mediaUrlOf,
  pageRange,
  parseTags,
  pendingNote,
  pendingText,
  scannedTone,
  shortfallText,
  thumbUrlOf,
  totalText,
  uploadOf,
  uploadTone,
  usableRangeText,
  usableText,
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
    on_disk: true,
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
    on_disk: true,
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
    on_disk: true,
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

/**
 * 一类素材的**一页**假件。
 *
 * `usable` 与 `disk_total` 按**出片那条口径**推出来（盘上有文件 + 库里启用着），
 * 而不是照抄 `stats` —— 面板要验的正是"这两组数字会不一样"。
 */
function pageOf(kind: AssetKind, rows: AssetItem[], extra: Partial<AssetPage> = {}): AssetPage {
  const enabled = rows.filter((row) => row.enabled);
  const usable = enabled.filter((row) => row.on_disk).length;
  const pageSize = extra.page_size ?? DEFAULT_PAGE_SIZE;
  const total = extra.total ?? rows.length;
  return {
    kind,
    root: `D:/studio/data/${kind}`,
    root_missing: false,
    items: rows,
    stats: {
      total: rows.length,
      enabled: enabled.length,
      total_duration_ms: rows.length * 30_000,
      enabled_duration_ms: enabled.length * 30_000,
    },
    usable,
    disk_total: usable,
    shortfall: null,
    pending: [],
    strays: [],
    total,
    page: 1,
    page_size: pageSize,
    pages: Math.max(1, Math.ceil(total / pageSize)),
    ...extra,
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

function uploadedFile(overrides: Partial<UploadedFile> = {}): UploadedFile {
  return {
    filename: "parkour_new.mp4",
    asset_id: "parkour_new",
    status: "stored",
    message: "已落盘 parkour_new.mp4",
    ...overrides,
  } as UploadedFile;
}

function uploadResult(overrides: Partial<UploadResult> = {}): UploadResult {
  return {
    kind: "broll",
    root: "D:/studio/data/assets/mc_parkour",
    overwrite: false,
    files: [uploadedFile()],
    stored: 1,
    replaced: 0,
    skipped: 0,
    report: report(),
    ...overrides,
  } as UploadResult;
}

function envelope(data: Record<string, unknown>): Envelope {
  return { v: 1, type: "event", channel: "logs", seq: 1, ts: "2026-09-15T00:00:00Z", data };
}

/** 让挂在 `void refresh()` 上的那几个微任务跑完。 */
function flush(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
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

  it("缺口与降级文案取自后端（前端不重算判据）", () => {
    const page = pageOf("broll", [broll()], { shortfall: "出片能挑到的跑酷素材只有 1 条（建议 ≥ 60）" });
    expect(shortfallText(page)).toContain("建议 ≥ 60");
    expect(shortfallText(null)).toBeNull();
    // ★ 有 1 条能挑到 ⇒ **不降级**（降级只回答"出片会不会真的黑屏"）
    expect(degradedText(stats({ degraded: false, note: null }))).toBeNull();
    expect(
      degradedText(
        stats({ degraded: true, note: "跑酷素材一条都挑不到（目录是空的，或者全被停用了）⇒ 黑屏降级" }),
      ),
    ).toContain("黑屏降级");
    expect(degradedText(null)).toBeNull();
  });

  it("★ 覆盖度报**三组**数字：出片能挑到 / 盘上 / 库里（合成一个数就会说谎）", () => {
    const page = pageOf("broll", [broll(), broll({ id: "parkour_002", enabled: false })]);
    expect(coverageText(page)).toBe("出片能挑到 1 条 · 盘上 1 条 · 已入库 2 条 / 启用 1 · 30 s");
    expect(coverageText(null)).toBe("—");
  });

  it("★ 盘上有、库里没有 ⇒ 未入库那批也进覆盖度（它们照样会被出片挑到）", () => {
    const page = pageOf("broll", [], {
      shortfall: "出片能挑到的跑酷素材只有 58 条（建议 ≥ 60）",
      disk_total: 58,
      usable: 58,
      pending: [
        { kind: "broll", id: "parkour_003", path: "D:/studio/data/assets/mc_parkour/parkour_003.mp4" },
      ],
    });
    expect(coverageText(page)).toContain("未入库 1 条");
    expect(usableText(page)).toBe("出片能挑到 58 条");
    expect(usableText(null)).toBe("—");
  });

  it("★ 未入库那批怎么说：跑酷/BGM 是「出片会挑到」，音色是「入库后才能配音用」", () => {
    // 两条链路真的不同：出片挑素材只列目录，而配音只认 voice_profiles 表。
    // 写成一句通用的话，就会在音色那一节说一个反过来的谎。
    expect(pendingNote("broll")).toContain("出片照样会挑到");
    expect(pendingNote("bgm")).toContain("出片照样会挑到");
    expect(pendingNote("voice")).toContain("入库后才能被配音用");
    expect(pendingNote("voice")).not.toContain("出片照样会挑到");
  });

  it("★ 同一句「能用几条」按类别说不同的话（音色不是拿去当画面的）", () => {
    expect(usableText(pageOf("broll", [], { usable: 3 }))).toBe("出片能挑到 3 条");
    expect(usableText(pageOf("voice", [], { usable: 3 }))).toBe("配音能用 3 个");
    expect(coverageText(pageOf("voice", [], { usable: 2, disk_total: 4 }))).toContain("配音能用 2 个");
  });

  it("★ 「筛出几条」与「这一类一共几条」是两个数（合成一个数会让人去补素材）", () => {
    const all = pageOf("broll", [], { total: 60, stats: { total: 60, enabled: 60, total_duration_ms: 0, enabled_duration_ms: 0 } });
    expect(totalText(all)).toBe("这一类共 60 条");
    const filtered = pageOf("broll", [], {
      total: 3,
      stats: { total: 60, enabled: 60, total_duration_ms: 0, enabled_duration_ms: 0 },
    });
    expect(totalText(filtered)).toBe("筛出 3 条 / 这一类共 60 条");
    expect(totalText(null)).toBe("—");
  });

  it("未入库名单：人看得懂的一行（太多就截断）", () => {
    const pending = Array.from({ length: 15 }, (_, index) => ({
      kind: "broll" as const,
      id: `parkour_${String(index).padStart(3, "0")}`,
      path: `D:/studio/data/assets/mc_parkour/parkour_${String(index).padStart(3, "0")}.mp4`,
    }));
    const page = pageOf("broll", [], { pending, disk_total: 15, usable: 15 });
    expect(pendingText(page)).toContain("等 15 条");
    expect(pendingText(pageOf("broll", []))).toBe("");
  });

  it("★ 一条素材的**三态**：出片会挑到 / 已停用 / 文件不在了", () => {
    expect(itemTone(broll())).toBe("ok");
    expect(itemStateText(broll())).toBe("出片会挑到");

    expect(itemTone(broll({ enabled: false }))).toBe("warn");
    expect(itemStateText(broll({ enabled: false }))).toBe("已停用");

    // 行还在、开关还亮着，而文件被人挪走了 —— 这是最阴的一种，必须红
    expect(itemTone(broll({ on_disk: false }))).toBe("error");
    expect(itemStateText(broll({ on_disk: false }))).toBe("文件不在了");

    // 音色是**目录**，说话方式不一样
    expect(itemStateText(voice())).toBe("配音会用");
    expect(itemStateText(voice({ on_disk: false }))).toBe("目录不在了");
  });

  it("扫盘报告的颜色：拒绝与『这次没写库』都是红的，重复标黄", () => {
    expect(scannedTone(scanned())).toBe("ok");
    expect(scannedTone(scanned({ action: "duplicate" }))).toBe("warn");
    expect(scannedTone(scanned({ action: null, stored: false }))).toBe("error");
    expect(scannedTone(scanned({ usable: false }))).toBe("error");
  });

  it("可用区间：上界为空 = 到片尾；非跑酷素材不适用", () => {
    expect(usableRangeText(broll())).toBe("0 – 30000 ms");
    expect(usableRangeText(broll({ usable_from_ms: 1_500, usable_to_ms: 20_000 }))).toBe(
      "1500 – 20000 ms",
    );
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
// 分页与筛选（纯函数）
// ══════════════════════════════════════════════════════════════════════

describe("分页与筛选", () => {
  it("页码按钮：页数少就全画出来（3 页上画两个省略号是最没道理的一种）", () => {
    expect(pageRange(1, 1)).toEqual([1]);
    expect(pageRange(1, 3)).toEqual([1, 2, 3]);
    expect(pageRange(2, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("页码按钮：页数多时两头各留一个锚点，中间跟着当前页滑", () => {
    expect(pageRange(1, 20)).toEqual([1, 2, 3, 4, 5, 6, 20]);
    expect(pageRange(10, 20)).toEqual([1, 8, 9, 10, 11, 12, 20]);
    expect(pageRange(20, 20)).toEqual([1, 15, 16, 17, 18, 19, 20]);
  });

  it("三态筛选 → 查询参数：`all` 是**不发这个参数**，不是发个空串", () => {
    expect(ENABLED_FILTERS.map((option) => option.value)).toEqual(["all", "on", "off"]);
    expect(enabledParam("all")).toBeUndefined();
    expect(enabledParam("on")).toBe(true);
    expect(enabledParam("off")).toBe(false);
  });

  it("每页条数的几档：默认 20，且**都**不超过服务端 200 的硬上限", () => {
    expect(DEFAULT_PAGE_SIZE).toBe(20);
    expect(PAGE_SIZE_OPTIONS).toContain(DEFAULT_PAGE_SIZE);
    for (const value of PAGE_SIZE_OPTIONS) expect(value).toBeLessThanOrEqual(200);
  });
});

// ══════════════════════════════════════════════════════════════════════
// 逐行编辑器（纯函数）
// ══════════════════════════════════════════════════════════════════════

/**
 * 后端那三个白名单常量的**真值**（现读源码，不抄第二份）。
 *
 * 抄一份到前端当断言，等于"两处一起错"也能通过：字段改名的那天，测试跟着一起改，
 * 而面板上的框仍然填了存不进去。现读源码，改名的当天这里就红。
 */
function backendPatchFields(constant: string): string[] {
  const source = readFileSync(
    new URL("../../../src/studio/db/repositories/asset_repo.py", import.meta.url),
    "utf8",
  );
  const match = new RegExp(
    `${constant}: Final\\[frozenset\\[str\\]\\] = frozenset\\(\\s*\\{([^}]*)\\}`,
  ).exec(source);
  if (match === null) throw new Error(`后端没有 ${constant}（白名单改名了？）`);
  return [...match[1].matchAll(/"([^"]+)"/g)].map((row) => row[1]);
}

describe("逐行编辑器", () => {
  it("★ 编辑器里的字段与后端白名单**一一对应**（否则框填了也存不进去）", () => {
    const constants: Record<AssetKind, string> = {
      broll: "CLIP_PATCH_FIELDS",
      bgm: "TRACK_PATCH_FIELDS",
      voice: "VOICE_PATCH_FIELDS",
    };
    // 行上有自己控件的两个字段不进编辑器（放进去就是"同一件事有两个入口"）
    const rowControls = ["license", "enabled"];
    for (const kind of ASSET_KINDS) {
      const allowed = backendPatchFields(constants[kind]);
      expect(EDIT_FIELDS[kind].map((field) => field.key).sort()).toEqual(
        allowed.filter((key) => !rowControls.includes(key)).sort(),
      );
    }
  });

  it("标签：逗号（中英文都认）或空白分隔", () => {
    expect(parseTags("夜景, 备用")).toEqual(["夜景", "备用"]);
    expect(parseTags("a，b c")).toEqual(["a", "b", "c"]);
    expect(parseTags("   ")).toEqual([]);
  });

  it("打开编辑器时的初始草稿 = 这一行现在的值（按类别只取它真有的字段）", () => {
    const brollDraft = fieldDraft(broll({ tags: ["夜景"], has_text: true }));
    expect(brollDraft).toEqual({
      has_text: true,
      usable_from_ms: "0",
      usable_to_ms: "",
      tags: "夜景",
      source_url: "",
      proof_path: "",
      licensed_to: "",
    });
    const bgmDraft = fieldDraft(bgm({ mood: "轻快", bpm: 120 }));
    expect(bgmDraft.mood).toBe("轻快");
    expect(bgmDraft.bpm).toBe("120");
    expect(bgmDraft.loopable).toBe(true);
    // 音色没有 tags 列 ⇒ 不画那个框
    expect(fieldDraft(voice())).toEqual({ source_url: "", proof_path: "", licensed_to: "" });
  });

  it("★ 没动过的字段**一个都不交**（整行提交会把别人刚改的字段覆盖回去）", () => {
    const item = broll({ tags: ["夜景"] });
    const { body, errors } = changedFields(item, fieldDraft(item));
    expect(errors).toEqual([]);
    expect(body).toEqual({});
  });

  it("★ 只交改过的：标签改了交标签，文字清空交 null（不是空串）", () => {
    const item = broll({ source_url: "https://example.com/a.mp4" });
    const draft = { ...fieldDraft(item), tags: "夜景, 备用", source_url: "" };
    expect(changedFields(item, draft).body).toEqual({ tags: ["夜景", "备用"], source_url: null });
  });

  it("★ 可空数字清空 = 交 null（可用终点「留空 = 到片尾」是能改回去的）", () => {
    const item = broll({ usable_to_ms: 20_000 });
    const draft = { ...fieldDraft(item), usable_to_ms: "" };
    expect(changedFields(item, draft).body).toEqual({ usable_to_ms: null });
  });

  it("★ 数字解析不出来在**发请求之前**就报出来（让后端回一句只会让人去猜是哪个框）", () => {
    const item = broll();
    const { body, errors } = changedFields(item, { ...fieldDraft(item), usable_from_ms: "abc" });
    expect(body).toEqual({});
    expect(errors.join("；")).toContain("可用起点");
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("useAssetsStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    // 每个用例都从一份"什么都能跑通"的基线起步，各自再覆盖要断言的那几项。
    configureAssetsApi({
      fetchAssetPage: vi.fn(async (params: { kind: AssetKind }) => pageOf(params.kind, [])),
      fetchAssetStats: vi.fn(async () => stats()),
      ingestAssets: vi.fn(async () => report()),
      patchAsset: vi.fn(async () => broll()),
      uploadAssets: vi.fn(async () => uploadResult()),
      uploadVoice: vi.fn(async () => uploadResult({ kind: "voice" })),
    });
  });

  it("refresh 一次拉**一类的一页** + 家底", async () => {
    const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
    configureAssetsApi({ fetchAssetPage, fetchAssetStats: vi.fn(async () => stats()) });

    const store = useAssetsStore();
    await store.refresh();

    expect(fetchAssetPage).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "broll", page: 1, page_size: DEFAULT_PAGE_SIZE }),
    );
    expect(store.items.length).toBe(1);
    expect(store.licenses).toEqual(["authorized", "cc0", "purchased", "self_recorded"]);
    expect(store.loadError).toBeNull();
  });

  it("搜索与筛选都**发到服务端**（不是把一页拉回来再自己筛）", async () => {
    const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
    configureAssetsApi({ fetchAssetPage });

    const store = useAssetsStore();
    store.setEnabledFilter("off");
    await flush();
    expect(fetchAssetPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ enabled: false, page: 1 }),
    );
  });

  it("搜索是**防抖**的：连敲几下只发一次，且回第 1 页", async () => {
    vi.useFakeTimers();
    try {
      const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
      configureAssetsApi({ fetchAssetPage });

      const store = useAssetsStore();
      store.setQuery("par");
      store.setQuery("park");
      store.setQuery("parkour");
      expect(fetchAssetPage).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(ASSETS_QUERY_DEBOUNCE_MS);
      expect(fetchAssetPage).toHaveBeenCalledTimes(1);
      expect(fetchAssetPage).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: "parkour", page: 1 }),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("拉取失败**留着旧快照**（一屏素材不因一次抖动变空白）", async () => {
    configureAssetsApi({ fetchAssetPage: vi.fn(async () => pageOf("broll", [broll()])) });
    const store = useAssetsStore();
    await store.refresh();

    configureAssetsApi({
      fetchAssetPage: vi.fn(async () => {
        throw new ApiError("boom", 500, null);
      }),
    });
    await store.refresh();

    expect(store.loadError).toContain("boom");
    expect(store.items.length).toBe(1);
  });

  it("★ 页码以**服务端**为准：翻过头它给最后一页，面板跟着它走", async () => {
    const fetchAssetPage = vi.fn(async () =>
      pageOf("broll", [broll()], { page: 3, page_size: 20, total: 41 }),
    );
    configureAssetsApi({ fetchAssetPage });

    const store = useAssetsStore();
    store.goPage(99);
    await flush();

    expect(fetchAssetPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 99 }));
    expect(store.pageIndex).toBe(3);
    expect(store.pages).toBe(3);
  });

  it("换每页条数 ⇒ 回第 1 页（停在第 5 页上换页长，会落到一段没人想看的位置）", async () => {
    const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
    configureAssetsApi({ fetchAssetPage });

    const store = useAssetsStore();
    store.setPageSize(50);
    await flush();

    expect(store.pageSize).toBe(50);
    expect(store.pageIndex).toBe(1);
    expect(fetchAssetPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, page_size: 50 }),
    );
  });

  it("★ 切菜单：换一类就回第 1 页、清掉搜索与筛选（每页条数是偏好，留着）", async () => {
    const store = useAssetsStore();
    store.setPageSize(50);
    store.pageIndex = 4;
    store.query = "夜景";
    store.enabledFilter = "off";

    store.open("voice");

    expect(store.kind).toBe("voice");
    expect(store.pageIndex).toBe(1);
    expect(store.query).toBe("");
    expect(store.enabledFilter).toBe("all");
    expect(store.pageSize).toBe(50);
  });

  it("切菜单：同一类再点一次**不重置**（否则每次进菜单都把你刚筛的东西清掉）", () => {
    const store = useAssetsStore();
    store.open("broll");
    store.pageIndex = 3;
    store.open("broll");
    expect(store.pageIndex).toBe(3);
  });

  it("预览模式：dry_run 透传，且**不重拉**（没写库就没什么可重拉的）", async () => {
    const ingestAssets = vi.fn(async (body: IngestBody) =>
      report({ dry_run: body.dry_run ?? false }),
    );
    const fetchAssetPage = vi.fn(async () => pageOf("broll", []));
    configureAssetsApi({ ingestAssets, fetchAssetPage });

    const store = useAssetsStore();
    store.dryRun = true;
    store.license = "purchased";
    const result = await store.scan("broll");

    expect(result?.dry_run).toBe(true);
    expect(ingestAssets).toHaveBeenCalledWith(
      expect.objectContaining({ dry_run: true, license: "purchased", kind: "broll" }),
    );
    expect(fetchAssetPage).not.toHaveBeenCalled();
    expect(store.report?.dry_run).toBe(true);
  });

  it("真入库之后重拉一次（库里变了）", async () => {
    const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
    configureAssetsApi({ ingestAssets: vi.fn(async () => report()), fetchAssetPage });

    const store = useAssetsStore();
    store.dryRun = false;
    await store.scan();

    expect(fetchAssetPage).toHaveBeenCalledTimes(1);
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
    configureAssetsApi({ patchAsset });

    const store = useAssetsStore();
    const ok = await store.setEnabled(broll(), false);

    expect(ok).toBe(true);
    expect(patchAsset).toHaveBeenCalledWith("parkour_001", { kind: "broll", enabled: false });
    expect(store.pendingId).toBeNull();
  });

  it("删除：`purge` 由**调用方**给，删完重拉一次（音色删盘 / 跑酷不删盘）", async () => {
    const deleteAsset = vi.fn(async () => ({ kind: "voice", id: "bear_da", purge: true, purged: [] }));
    const fetchAssetPage = vi.fn(async () => pageOf("voice", []));
    configureAssetsApi({ deleteAsset, fetchAssetPage });

    const store = useAssetsStore();
    const ok = await store.remove(voice(), true);

    expect(ok).toBe(true);
    // `purge` 是这一格的**唯一**变量：服务端据此决定动不动盘上那份，
    // 所以它必须原样传下去，不能在这里替它拿主意。
    expect(deleteAsset).toHaveBeenCalledWith("bear_da", "voice", true);
    expect(fetchAssetPage).toHaveBeenCalled();
    expect(store.pendingId).toBeNull();
  });

  it("删除失败 ⇒ 错误位有话说，返回 false（面板据此**不关**二次确认）", async () => {
    configureAssetsApi({
      deleteAsset: vi.fn(async () => {
        throw new ApiError("素材不在库里：broll / parkour_001", 404, null);
      }),
    });

    const store = useAssetsStore();
    const ok = await store.remove(broll(), false);

    expect(ok).toBe(false);
    expect(store.error).toContain("素材不在库里");
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

  it("编辑器：同时只有一行开着；同一行再点一次是关", () => {
    const store = useAssetsStore();
    const first = broll();
    const second = broll({ id: "parkour_002" });

    store.toggleEditor(first);
    expect(store.editingId).toBe("parkour_001");
    store.toggleEditor(second);
    expect(store.editingId).toBe("parkour_002");
    store.toggleEditor(second);
    expect(store.editingId).toBeNull();
  });

  it("★ 保存：只把改过的字段交出去，存完把编辑器关上", async () => {
    const patchAsset = vi.fn(async () => broll({ tags: ["夜景"] }));
    configureAssetsApi({ patchAsset });

    const store = useAssetsStore();
    const item = broll();
    store.toggleEditor(item);
    const ok = await store.saveFields(item, { ...fieldDraft(item), tags: "夜景" });

    expect(ok).toBe(true);
    expect(patchAsset).toHaveBeenCalledWith("parkour_001", { kind: "broll", tags: ["夜景"] });
    expect(store.editingId).toBeNull();
  });

  it("★ 保存：一个字都没改 ⇒ 一个请求都不发，但编辑器照关", async () => {
    const patchAsset = vi.fn(async () => broll());
    configureAssetsApi({ patchAsset });

    const store = useAssetsStore();
    const item = broll();
    store.toggleEditor(item);
    const ok = await store.saveFields(item, fieldDraft(item));

    expect(ok).toBe(true);
    expect(patchAsset).not.toHaveBeenCalled();
    expect(store.editingId).toBeNull();
  });

  it("★ 保存：框里不是数字 ⇒ 拦在发请求之前，且编辑器**不关**（改了半截别丢了）", async () => {
    const patchAsset = vi.fn(async () => broll());
    configureAssetsApi({ patchAsset });

    const store = useAssetsStore();
    const item = broll();
    store.toggleEditor(item);
    const ok = await store.saveFields(item, { ...fieldDraft(item), usable_from_ms: "abc" });

    expect(ok).toBe(false);
    expect(patchAsset).not.toHaveBeenCalled();
    expect(store.error).toContain("可用起点");
    expect(store.editingId).toBe("parkour_001");
  });
});

// ══════════════════════════════════════════════════════════════════════
// 上传（T4.8 的图形化入库入口）
// ══════════════════════════════════════════════════════════════════════

describe("上传", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    configureAssetsApi({
      fetchAssetPage: vi.fn(async (params: { kind: AssetKind }) => pageOf(params.kind, [])),
      fetchAssetStats: vi.fn(async () => stats()),
    });
  });

  it("结局的颜色：跳过是红的（它不是「没问题」，而是「没进去」）", () => {
    expect(uploadTone(uploadedFile())).toBe("ok");
    expect(uploadTone(uploadedFile({ status: "replaced" }))).toBe("warn");
    expect(uploadTone(uploadedFile({ status: "skipped", asset_id: null }))).toBe("error");
    expect(UPLOAD_LABELS.skipped).toBe("跳过");
  });

  it("文件选择框认的后缀按类别分叉（与后端白名单是同一份）", () => {
    expect(acceptFor("broll")).toContain(".mp4");
    expect(acceptFor("broll")).not.toContain(".mp3");
    expect(acceptFor("bgm")).toContain(".mp3");
    expect(acceptFor("voice")).toContain(".wav");
  });

  it("回执只属于它自己那一类（别的类别不显示别人的结果）", () => {
    const result = uploadResult({ kind: "bgm" });
    expect(uploadOf(result, "bgm")).toBe(result);
    expect(uploadOf(result, "broll")).toBeNull();
    expect(uploadOf(null, "bgm")).toBeNull();
  });

  it("上传带上授权与覆盖开关；回执与入库报告都落位，并重拉一次库", async () => {
    const uploadAssets = vi.fn(async () => uploadResult());
    const fetchAssetPage = vi.fn(async () => pageOf("broll", [broll()]));
    configureAssetsApi({ uploadAssets, fetchAssetPage });

    const store = useAssetsStore();
    store.license = "cc0";
    store.overwrite = true;
    const files = [new File(["x"], "parkour_new.mp4")];
    const result = await store.uploadFiles("broll", files);

    expect(result?.stored).toBe(1);
    expect(uploadAssets).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "broll", files, license: "cc0", overwrite: true }),
    );
    expect(store.upload?.kind).toBe("broll");
    // 落盘的那批**当场入库** ⇒ 复用「扫盘报告」那张表
    expect(store.report?.totals.created).toBe(1);
    expect(fetchAssetPage).toHaveBeenCalledTimes(1);
    expect(store.uploadBusy).toBe(false);
  });

  it("没选文件 ⇒ 一个请求都不发", async () => {
    const uploadAssets = vi.fn(async () => uploadResult());
    configureAssetsApi({ uploadAssets });

    const store = useAssetsStore();
    expect(await store.uploadFiles("broll", [])).toBeNull();
    expect(uploadAssets).not.toHaveBeenCalled();
  });

  it("音色：id 空着就拦下来（后端只会回 VALIDATION_FAILED，用户该看到的是「先填 id」）", async () => {
    const uploadVoice = vi.fn(async () => uploadResult({ kind: "voice" }));
    configureAssetsApi({ uploadVoice });

    const store = useAssetsStore();
    const result = await store.uploadVoiceFiles([new File(["a"], "a.wav")]);

    expect(result).toBeNull();
    expect(uploadVoice).not.toHaveBeenCalled();
    expect(store.error).toContain("音色 id");
  });

  it("音色：目录名去掉空白后提交，文字稿一起带上", async () => {
    const uploadVoice = vi.fn(async () => uploadResult({ kind: "voice" }));
    configureAssetsApi({ uploadVoice });

    const store = useAssetsStore();
    store.voiceId = " bear_da ";
    store.voiceText = "第一句\n第二句";
    const files = [new File(["a"], "a.wav")];
    await store.uploadVoiceFiles(files);

    expect(uploadVoice).toHaveBeenCalledWith(
      expect.objectContaining({ voiceId: "bear_da", refText: "第一句\n第二句", files }),
    );
  });

  it("上传失败 ⇒ 错误位有话说、返回 null，且不假装成功", async () => {
    configureAssetsApi({
      uploadAssets: vi.fn(async () => {
        throw new ApiError("授权类型不合法", 422, null);
      }),
    });

    const store = useAssetsStore();
    const result = await store.uploadFiles("broll", [new File(["x"], "a.mp4")]);

    expect(result).toBeNull();
    expect(store.error).toContain("授权类型不合法");
    expect(store.upload).toBeNull();
    expect(store.uploadBusy).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// 授权类型枚举（后端给的那份，前端只负责显示）
// ══════════════════════════════════════════════════════════════════════

describe("授权类型", () => {
  it("每个后端给的枚举值都有人话（少一个就会在面板上露出英文原值）", () => {
    for (const value of ["self_recorded", "authorized", "cc0", "purchased"]) {
      expect(LICENSE_LABELS[value]).toBeTruthy();
    }
  });
});
