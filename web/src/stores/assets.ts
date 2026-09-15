// 素材库面板状态（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。
//
// 这一屏要回答四个问题
// --------------------
// ① 库里有什么、够不够用？② 盘上还有没入库的、坏在哪？③ 这条启用还是停用？
// ④ 素材长什么样、听着对不对？
//
// 为什么"停用"是唯一的下架方式
// ----------------------------
// 误删一柜子素材是**不可逆**的，而停用随时能点回来。所以面板上只有"启用 / 停用"，
// 没有"删除"——这不是"还没做"，是有意的：真要腾空间，是用户在资源管理器里的决定。
//
// 为什么扫描/启停**不新增 WS 事件**
// ---------------------------------
// §04.4.3 的事件表是**被契约测试解析的契约**，为一次素材扫描新增一种事件不划算：
// 入库与启停本来就会往 `system_logs` 写一行（`source='assets'`）。面板靠 `logs`
// 通道自己刷新，「谁动了素材库」这条信息一条都不少（与 T4.11 / T4.7 同一条裁定）。
// 合并窗口是必须的：一次全扫会写一行，而三类的扫描动作挨得很近。
//
// 为什么"预览 / 试听"不走 store
// ----------------------------
// `<img>` / `<audio>` 直接取 URL（浏览器自己做范围请求与缓存）。把它们塞进 store
// 只会多一份要手动释放的 objectURL，以及一个"刷新时音频被掐断"的坑。

import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

import {
  assetMediaUrl,
  assetThumbUrl,
  fetchAssets,
  fetchAssetStats,
  ingestAssets,
  patchAsset,
  type AssetItem,
  type AssetKind,
  type AssetPatchBody,
  type AssetSection,
  type AssetsLibrary,
  type AssetsStats,
  type IngestBody,
  type IngestReport,
  type ScannedAsset,
} from "@/api/endpoints/assets";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import type { Envelope } from "@/ws/events";

/** 服务端 `system_logs.source`（= `asset_service.LOG_SOURCE`，由契约测试锁死）。 */
export const ASSETS_LOG_SOURCE = "assets";

/** `logs` 通道的合并窗口：一次"全扫"会连着写几行，逐行重拉等于白跑好几趟。 */
export const ASSETS_REFRESH_COALESCE_MS = 300;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface AssetsApi {
  fetchAssets: typeof fetchAssets;
  fetchAssetStats: typeof fetchAssetStats;
  ingestAssets: typeof ingestAssets;
  patchAsset: typeof patchAsset;
}

let api: AssetsApi = { fetchAssets, fetchAssetStats, ingestAssets, patchAsset };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureAssetsApi(overrides: Partial<AssetsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 三类的顺序与中文标签（与后端 `AssetKind` 的枚举顺序一致：跑酷 → 音色 → BGM）。 */
export const ASSET_KINDS: readonly AssetKind[] = ["broll", "voice", "bgm"];

export const KIND_LABELS: Record<AssetKind, string> = {
  broll: "跑酷素材",
  voice: "音色",
  bgm: "BGM",
};

/** 往哪放素材（面板常驻显示，省得用户去翻文档）。 */
export const KIND_HINTS: Record<AssetKind, string> = {
  broll: "data/assets/mc_parkour/parkour_<名字>.mp4",
  voice: "data/voice_src/<音色 id>/ref_01.wav + ref.txt + profile.json（占位音看 profile.json 的 origin）",
  bgm: "data/assets/bgm/bgm_<名字>.mp3",
};

/** 一次入库对某条素材干了什么（`null` = 这次没写库）。 */
export const ACTION_LABELS: Record<string, string> = {
  created: "新入库",
  refreshed: "已刷新",
  unchanged: "无变化",
  duplicate: "内容重复",
};

export const LICENSE_LABELS: Record<string, string> = {
  self_recorded: "自录",
  authorized: "已授权",
  cc0: "CC0",
  purchased: "已购买",
};

/** 毫秒 → 人话（面板上到处是时长）。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds} s`;
  return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
}

/** 按 kind 取一节（缺了返回 `null`：后端少给一节时不该整屏炸掉）。 */
export function sectionOf(library: AssetsLibrary | null, kind: AssetKind): AssetSection | null {
  if (library === null) return null;
  return library.sections.find((section) => section.kind === kind) ?? null;
}

/** 够不够用那句话（`shortfall` 是后端算的，前端只负责显示）。 */
export function shortfallText(section: AssetSection | null): string | null {
  return section?.shortfall ?? null;
}

/** 降级横幅的文案（`degraded` 由后端判，前端不重算一遍判据）。 */
export function degradedText(library: AssetsLibrary | null): string | null {
  if (library === null || !library.degraded) return null;
  return library.note ?? "跑酷素材不足或全部停用 ⇒ 出片会走黑屏降级";
}

/** 一条素材该用什么颜色：拒绝入库的红、停用的黄、可用的绿。 */
export function itemTone(item: AssetItem): StatusTone {
  if (!item.enabled) return "warn";
  return "ok";
}

/** 扫盘报告里的一条该用什么颜色（`action === null` ⇒ 这次没写库，是红的）。 */
export function scannedTone(item: ScannedAsset): StatusTone {
  if (!item.usable) return "error";
  if (item.action === null) return "error";
  if (item.action === "duplicate") return "warn";
  return "ok";
}

/** 可用区间的人话（跑酷素材：`to` 为空 = 到片尾）。 */
export function usableRangeText(item: AssetItem): string {
  if (item.kind !== "broll") return "—";
  const end = item.usable_to_ms === null ? item.duration_ms : item.usable_to_ms;
  return `${item.usable_from_ms} – ${end} ms`;
}

/**
 * 这条素材的缩略图地址。
 *
 * 只有**跑酷素材**有抽帧图：音色是目录、BGM 是音频（`bgm_tracks` 里根本没有
 * `thumb_path` 这一列）。写死"非跑酷一律 null"而不是 `item.thumb_path ?? null`，
 * 是因为后者要靠类型收窄 —— 而"BGM 有没有缩略图"这件事的答案在**表结构**里，
 * 不在请求响应的形状里。
 */
export function thumbUrlOf(item: AssetItem): string | null {
  if (item.kind !== "broll") return null;
  return item.thumb_path === null ? null : assetThumbUrl(item.id);
}

/** 这条素材能不能试听（音色是目录 ⇒ 后端会 422，前端直接不给播放器）。 */
export function mediaUrlOf(item: AssetItem): string | null {
  return item.kind === "voice" ? null : assetMediaUrl(item.id);
}

/**
 * 这是不是**占位素材**（`scripts/seed_placeholder_assets.py` 造出来的）。
 *
 * 判据是 `tags` 里的 `placeholder`：面板据此标黄 —— 一眼能看出"这不是能发出去的
 * 素材"。**绝不假装成正式素材**，否则"这条片子为什么这么难看"会变成一条查不完的悬案。
 *
 * 音色没有 `tags` 列（`voice_profiles` 的 DDL 如此），它的占位标记写在目录里的
 * `profile.json`（`origin: generated`）—— 那个位置面板看不到，所以这里如实返回 false，
 * 而不是拿"授权为空"之类的**别的**信号去猜。
 */
export function isPlaceholder(item: AssetItem): boolean {
  if (item.kind === "voice") return false;
  return item.tags.includes(PLACEHOLDER_TAG);
}

/** 占位标记（与 seed 脚本写进 `tags` 的那个字符串同源，由契约测试锁死）。 */
export const PLACEHOLDER_TAG = "placeholder";

/** 这是不是"素材库改过了"的那条日志帧（§04.5.12：面板靠它自己刷新）。 */
export function isAssetsLog(envelope: Envelope): boolean {
  const data = envelope.data;
  return data["kind"] === "log.appended" && data["source"] === ASSETS_LOG_SOURCE;
}

/** 库里"启用了几条 / 一共几条"的进度（面板顶部那行字）。 */
export function coverageText(section: AssetSection | null): string {
  if (section === null) return "—";
  return `${section.stats.enabled} / ${section.stats.total} 条启用 · ${formatDuration(
    section.stats.enabled_duration_ms,
  )}`;
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const useAssetsStore = defineStore("assets", () => {
  const { status: wsStatus, connect, resync } = useWsConnection();

  const library = ref<AssetsLibrary | null>(null);
  const stats = ref<AssetsStats | null>(null);
  const loading = ref(false);
  /** 拉取失败（与"动作失败"分开：一次拉取失败不该吞掉"我刚点的停用到底成没成"）。 */
  const loadError = ref<string | null>(null);
  const error = ref<string | null>(null);
  /** 上一次扫盘 / 入库的回执（面板据此画"新增 3 / 拒绝 1"）。 */
  const report = ref<IngestReport | null>(null);
  const busy = ref(false);
  /** 新入库素材的授权类型（**只对本次新入库的条目生效**）。 */
  const license = ref<string>("self_recorded");
  /** 面板上"先看看会怎样"的开关（`true` ⇒ 扫盘不写库）。 */
  const dryRun = ref(false);
  /** 正在改的那条（防重复点击；`null` = 没有在途的标记动作）。 */
  const pendingId = ref<string | null>(null);

  const sections = computed<AssetSection[]>(() => library.value?.sections ?? []);
  const degraded = computed(() => library.value?.degraded ?? false);
  const degradedNote = computed(() => degradedText(library.value));
  const licenses = computed<string[]>(
    () => stats.value?.licenses ?? ["self_recorded", "authorized", "cc0", "purchased"],
  );

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const [nextLibrary, nextStats] = await Promise.all([api.fetchAssets(), api.fetchAssetStats()]);
      library.value = nextLibrary;
      stats.value = nextStats;
      loadError.value = null;
    } catch (failure) {
      // 旧快照**留着**：一屏素材不因一次抖动变空白（与总览台同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  /** 扫盘（`dry_run`）或入库。两者是**同一条服务端路径**，前端只是一个开关。 */
  async function scan(kind?: AssetKind, ids?: string[]): Promise<IngestReport | null> {
    busy.value = true;
    error.value = null;
    try {
      const body: IngestBody = {
        dry_run: dryRun.value,
        license: license.value,
      };
      if (kind !== undefined) body.kind = kind;
      if (ids !== undefined) body.ids = ids;
      const result = await api.ingestAssets(body);
      report.value = result;
      // 真入库（非预览）之后库里变了 ⇒ 重拉一次；预览不动库，没必要。
      if (!result.dry_run) await refresh();
      return result;
    } catch (failure) {
      error.value = describeError(failure);
      return null;
    } finally {
      busy.value = false;
    }
  }

  /** 启用 / 停用（**不动物理文件**）。 */
  async function setEnabled(item: AssetItem, enabled: boolean): Promise<boolean> {
    return applyPatch(item, { enabled });
  }

  /** 改几个字段（授权 / 标签 / 可用区间…）；**只交显式给了的字段**。 */
  async function applyPatch(item: AssetItem, changes: AssetPatchBody): Promise<boolean> {
    pendingId.value = item.id;
    error.value = null;
    try {
      await api.patchAsset(item.id, { kind: item.kind, ...changes });
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      pendingId.value = null;
    }
  }

  // ── WS ──────────────────────────────────────────────────────────────

  let coalescer: ReturnType<typeof setTimeout> | null = null;

  function onLogs(envelope: Envelope): void {
    if (!isAssetsLog(envelope)) return;
    if (coalescer !== null) return;
    coalescer = setTimeout(() => {
      coalescer = null;
      void refresh();
    }, ASSETS_REFRESH_COALESCE_MS);
  }

  let wired = false;
  function wire(): void {
    if (wired) return;
    wired = true;
    useChannelStream("logs", onLogs);
    watch(wsStatus, (next, previous) => {
      // 断线期间的事件是**真的丢了** ⇒ 重连成功必须以一次全量重拉收尾。
      if (next === "open" && previous !== "open") void refresh();
    });
  }

  // ── 起停 ────────────────────────────────────────────────────────────

  function start(): void {
    wire();
    connect();
    void refresh();
  }

  function stop(): void {
    if (coalescer !== null) {
      clearTimeout(coalescer);
      coalescer = null;
    }
  }

  return {
    // 状态
    library,
    stats,
    loading,
    loadError,
    error,
    report,
    busy,
    license,
    dryRun,
    pendingId,
    // 派生
    sections,
    degraded,
    degradedNote,
    licenses,
    wsStatus,
    // 动作
    refresh,
    scan,
    setEnabled,
    applyPatch,
    resync,
    start,
    stop,
  };
});
