// 素材库面板状态（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。
//
// 三个菜单，各看一类
// ------------------
// 跑酷 / 音色 / BGM **各是一个菜单**（`stores/ui.ts` 的 `ASSET_PANEL_KINDS`）：三类素材
// 可管理的字段本来就不一样（跑酷有可用区间与 `has_text`，BGM 有 `bpm` / `mood` /
// `loopable`，音色有 `ref_count`），摊在一屏里只能是一张"大半格子是空的"大表。
//
// 一屏只看一页
// ------------
// 一柜子素材迟早会到几百条，列表按页取（服务端切片）。**`stats` 与 `total` 是两个数**：
// 前者是这一类的家底（不受筛选影响），后者是这一页所在的筛选结果 —— 合成一个数，
// 筛出 3 条时面板会说"这一类只有 3 条素材"，用户接着就去补素材了。
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
  deleteAsset,
  fetchAssetPage,
  fetchAssetStats,
  ingestAssets,
  patchAsset,
  uploadAssets,
  uploadVoice,
  type AssetItem,
  type AssetKind,
  type AssetPage,
  type AssetPatchBody,
  type AssetsStats,
  type FlatKind,
  type IngestBody,
  type IngestReport,
  type ScannedAsset,
  type UploadedFile,
  type UploadResult,
} from "@/api/endpoints/assets";
import { fetchCompliance, type ComplianceView } from "@/api/endpoints/publish";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import type { Envelope } from "@/ws/events";

/** 服务端 `system_logs.source`（= `asset_service.LOG_SOURCE`，由契约测试锁死）。 */
export const ASSETS_LOG_SOURCE = "assets";

/** `logs` 通道的合并窗口：一次"全扫"会连着写几行，逐行重拉等于白跑好几趟。 */
export const ASSETS_REFRESH_COALESCE_MS = 300;

/** 搜索框的防抖：逐键重拉一整页，是本地面板上最没必要的一种忙。 */
export const ASSETS_QUERY_DEBOUNCE_MS = 250;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface AssetsApi {
  fetchAssetPage: typeof fetchAssetPage;
  fetchAssetStats: typeof fetchAssetStats;
  ingestAssets: typeof ingestAssets;
  patchAsset: typeof patchAsset;
  /** 上传跑酷 / BGM（落盘 + 入库一次做完）。 */
  uploadAssets: typeof uploadAssets;
  /** 上传一个音色的参考音。 */
  uploadVoice: typeof uploadVoice;
  /** 删掉一条素材（`purge` 决定盘上那份动不动）。 */
  deleteAsset: typeof deleteAsset;
  /** R2 来源登记留档（T5.5）：这一屏与发布面板**共用同一份**（§06.11 要求两处常驻）。 */
  fetchCompliance: typeof fetchCompliance;
}

let api: AssetsApi = {
  fetchAssetPage,
  fetchAssetStats,
  ingestAssets,
  patchAsset,
  uploadAssets,
  uploadVoice,
  deleteAsset,
  fetchCompliance,
};

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

/** 上传逐条结局的中文标签（与后端 `UploadedFileModel.status` 一一对应）。 */
export const UPLOAD_LABELS: Record<string, string> = {
  stored: "已落盘",
  replaced: "已覆盖",
  skipped: "跳过",
};

/** 每页条数的选项（**服务端**另有 200 的硬上限，这里只是给人挑的几档）。 */
export const PAGE_SIZE_OPTIONS: readonly number[] = [20, 50, 100];

export const DEFAULT_PAGE_SIZE = 20;

/** 启用状态筛选：三态（"全部"与"没筛"是同一件事，但界面上要有那一档）。 */
export type EnabledFilter = "all" | "on" | "off";

export const ENABLED_FILTERS: readonly { value: EnabledFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "on", label: "只看启用" },
  { value: "off", label: "只看停用" },
];

/** 三态筛选 → 查询参数（`all` ⇒ `undefined`：不发这个参数，后端就当"全部"）。 */
export function enabledParam(filter: EnabledFilter): boolean | undefined {
  if (filter === "on") return true;
  if (filter === "off") return false;
  return undefined;
}

/**
 * 页码按钮该画哪几个（**纯函数**：页码的算法不该藏在模板里）。
 *
 * 返回 `1 … page-1 page page+1 … pages` 那一段（两端各留一个"跳到首/末页"的锚点），
 * 中间断开的地方由模板画成省略号。页数少（≤ `span`）时全部画出来 —— 3 页的列表上
 * 画两个省略号是最没道理的一种。
 */
export function pageRange(page: number, pages: number, span = 7): number[] {
  if (pages <= span) return Array.from({ length: pages }, (_, index) => index + 1);
  const half = Math.floor((span - 2) / 2);
  let start = Math.max(2, page - half);
  const end = Math.min(pages - 1, start + span - 3);
  start = Math.max(2, end - (span - 3));
  const middle: number[] = [];
  for (let value = start; value <= end; value += 1) middle.push(value);
  return [1, ...middle, pages];
}

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

/** 够不够用那句话（`shortfall` 是后端算的，前端只负责显示）。 */
export function shortfallText(page: AssetPage | null): string | null {
  return page?.shortfall ?? null;
}

/** 降级横幅的文案（`degraded` 由后端判，前端不重算一遍判据）。 */
export function degradedText(stats: AssetsStats | null): string | null {
  if (stats === null || !stats.degraded) return null;
  return stats.note ?? "跑酷素材一条都挑不到（目录是空的或全被停用）⇒ 出片走黑屏降级";
}

/**
 * 一条素材该用什么颜色。
 *
 * 三档而不是两档：**文件不在了是红的**。它比"停用"更该被看见 —— 停用是人自己点的
 * （黄，看得懂），而"行还在、开关还亮着、文件却没了"是出片静默换底片的唯一原因。
 */
export function itemTone(item: AssetItem): StatusTone {
  if (!item.on_disk) return "error";
  if (!item.enabled) return "warn";
  return "ok";
}

/**
 * 这条素材在**出片链路**上的处境（面板上那一列状态）。
 *
 * 这一列回答的是"出片到底会不会用到它"，而不是"库里的开关是什么" —— 两者会不一样：
 * 文件被挪走之后开关还是绿的，而出片再也挑不到它。面板上只画开关，用户就会看着一个
 * 绿点、纳闷片子为什么一直用别的底片。
 */
export function itemStateText(item: AssetItem): string {
  if (!item.on_disk) return item.kind === "voice" ? "目录不在了" : "文件不在了";
  if (!item.enabled) return "已停用";
  return item.kind === "voice" ? "配音会用" : "出片会挑到";
}

/** 扫盘报告里的一条该用什么颜色（`action === null` ⇒ 这次没写库，是红的）。 */
export function scannedTone(item: ScannedAsset): StatusTone {
  if (!item.usable) return "error";
  if (item.action === null) return "error";
  if (item.action === "duplicate") return "warn";
  return "ok";
}

/**
 * 一条上传结局该用什么颜色。
 *
 * `skipped` 是**红的**：它不是"没问题"，而是"这个文件**没有**进素材库"。
 * 画成灰色或黄色，用户就会以为"差不多进去了" —— 而实际上一个字节都没写。
 */
export function uploadTone(row: UploadedFile): StatusTone {
  if (row.status === "skipped") return "error";
  if (row.status === "replaced") return "warn";
  return "ok";
}

/**
 * 文件选择框认哪些后缀（`<input accept>`）。
 *
 * 与后端 `layout.suffixes_for` 是**同一份白名单**。前端这一份只影响选择框的过滤器
 * （用户仍能选"所有文件"），真正的判定在后端 —— 所以这里少一个后缀不会放进坏文件，
 * 只会让人多选一次。
 */
export function acceptFor(kind: AssetKind): string {
  if (kind === "broll") return ".mp4,.mov,.mkv,.webm,video/*";
  return ".mp3,.wav,.m4a,.aac,.flac,.ogg,audio/*";
}

/** 上一次上传的回执，**当且仅当**它属于这一类（其余类别不显示别人的结果）。 */
export function uploadOf(result: UploadResult | null, kind: AssetKind): UploadResult | null {
  if (result === null) return null;
  return result.kind === kind ? result : null;
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
 * 音色没有 `tags` 列（它只有目录里的 `profile.json`），所以如实返回 `false`，不猜。
 */
export function isPlaceholder(item: AssetItem): boolean {
  if (item.kind === "voice") return false;
  return item.tags.includes("placeholder");
}

/** 这是不是素材库自己写的那条日志帧（§04.5.12：面板靠它自己刷新）。 */
export function isAssetsLog(envelope: Envelope): boolean {
  const data = envelope.data;
  return data["kind"] === "log.appended" && data["source"] === ASSETS_LOG_SOURCE;
}

/**
 * 一类素材的**三组数字**（每节标题右边那一行）。
 *
 * 三组而不是一组，是因为它们本来就会对不上，而对不上正是用户要知道的事：
 * - `usable` 出片真能挑到几条（**唯一与出片同口径**的数字，后端算的）；
 * - `disk_total` 盘上符合约定的有几个；
 * - `stats` 库里的家底（入了库几条、其中启用几条）。
 *
 * 合成一个数的后果已经见过一次：盘上 58 条一条没入库时，面板说出的是"0 条"。
 */
export function coverageText(page: AssetPage | null): string {
  if (page === null) return "—";
  const parts = [usableText(page), `盘上 ${page.disk_total} 条`];
  parts.push(`已入库 ${page.stats.total} 条 / 启用 ${page.stats.enabled}`);
  if (page.pending.length > 0) parts.push(`未入库 ${page.pending.length} 条`);
  if (page.stats.enabled > 0) parts.push(formatDuration(page.stats.enabled_duration_ms));
  return parts.join(" · ");
}

/**
 * 「实际能用几条」（`usable` 的人话），**按类别说不同的话**。
 *
 * 跑酷 / BGM 是出片挑素材，音色是配音挑音色 —— 同一条 `usable`（后端按各自链路算的），
 * 但对用户是两件事。说成"出片能挑到 0 个音色"会让人以为音色是拿去当画面的。
 */
export function usableText(page: AssetPage | null): string {
  if (page === null) return "—";
  return page.kind === "voice" ? `配音能用 ${page.usable} 个` : `出片能挑到 ${page.usable} 条`;
}

/**
 * 「盘上有 N 条还没入库」那句话要怎么说（**按类别分叉**）。
 *
 * 分叉的理由是两条链路**真的不同**，不是文案口味：
 * - 跑酷 / BGM：出片挑素材走 `render/assets.py`，它只列目录 ⇒ **未入库照样会被挑到**，
 *   缺的只是留痕（授权、时长、缩略图）；
 * - 音色：配音只认 `voice_profiles` 表 ⇒ 未入库的**挑不了**，得先入库。
 *
 * 这条分叉与后端 `AssetService._usable` 是同一件事（那边也是按 kind 分的）。
 * 写成一句通用的话，就会在音色那一节说一个反过来的谎 —— 而这一屏的可信度正是
 * 这一轮要修的东西。
 */
export function pendingNote(kind: AssetKind): string {
  if (kind === "voice") {
    return "入库后才能被配音用（配音只认库里的音色档案：得先知道参考音有几段、逐字文本对不对）";
  }
  return "出片照样会挑到它们（挑素材只列目录、不读库），缺的只是留痕：授权、时长、缩略图都还没登记";
}

/** 未入库那批的 id（面板上并排显示；太多就只显示前几个）。 */
export function pendingText(page: AssetPage | null, limit = 12): string {
  if (page === null || page.pending.length === 0) return "";
  const ids = page.pending.map((item) => item.id);
  if (ids.length <= limit) return ids.join("、");
  return `${ids.slice(0, limit).join("、")} 等 ${ids.length} 条`;
}

/** 「筛出来几条 / 一共几条」那句话（**两个数分开说**，见 store 头部的说明）。 */
export function totalText(page: AssetPage | null): string {
  if (page === null) return "—";
  const filtered = page.total !== page.stats.total;
  return filtered
    ? `筛出 ${page.total} 条 / 这一类共 ${page.stats.total} 条`
    : `这一类共 ${page.total} 条`;
}

// ══════════════════════════════════════════════════════════════════════
// 逐条编辑：字段清单 + 草稿 + "只交改过的"
// ══════════════════════════════════════════════════════════════════════

/**
 * 一个可编辑字段（面板按这份清单画表单，**不手写三份模板**）。
 *
 * `key` 必须落在后端 `AssetPatchRequest` 的字段里，而且必须在该类的
 * `*_PATCH_FIELDS` 白名单里 —— 否则面板上会有一个"填了也存不进去"的框
 * （后端会回 422「不可改的字段」）。这条约束由单测钉住。
 */
export interface FieldSpec {
  key: PatchKey;
  label: string;
  type: "text" | "number" | "bool" | "tags";
  hint?: string;
  placeholder?: string;
}

/** `AssetPatchBody` 里**逐条编辑**会用到的那些键（`license` / `enabled` 有各自的控件）。 */
export type PatchKey =
  | "has_text"
  | "usable_from_ms"
  | "usable_to_ms"
  | "tags"
  | "mood"
  | "bpm"
  | "loopable"
  | "source_url"
  | "proof_path"
  | "licensed_to";

/**
 * 每一类真正可改的字段。
 *
 * `license` 与 `enabled` **不在**这里：它们在表格行上各有一个控件（改得最勤的两个），
 * 放进编辑器只会变成"同一件事有两个入口"。
 */
export const EDIT_FIELDS: Readonly<Record<AssetKind, readonly FieldSpec[]>> = {
  broll: [
    { key: "has_text", label: "画面自带文字", type: "bool", hint: "素材里本来就有字幕/水印" },
    { key: "usable_from_ms", label: "可用起点 (ms)", type: "number" },
    { key: "usable_to_ms", label: "可用终点 (ms)", type: "number", placeholder: "留空 = 到片尾" },
    { key: "tags", label: "标签", type: "tags", placeholder: "逗号分隔，如：夜景, 备用" },
    { key: "source_url", label: "来源地址", type: "text", hint: "R2 留档：这条素材从哪来的" },
    { key: "proof_path", label: "授权书路径", type: "text" },
    { key: "licensed_to", label: "授权给谁", type: "text" },
  ],
  bgm: [
    { key: "mood", label: "情绪", type: "text", placeholder: "如：轻快 / 沉稳" },
    { key: "bpm", label: "BPM", type: "number" },
    { key: "loopable", label: "可循环", type: "bool" },
    { key: "tags", label: "标签", type: "tags", placeholder: "逗号分隔" },
    { key: "source_url", label: "来源地址", type: "text" },
    { key: "proof_path", label: "授权书路径", type: "text", hint: "R2 留档：曲库授权凭证" },
    { key: "licensed_to", label: "授权给谁", type: "text" },
  ],
  voice: [
    { key: "source_url", label: "来源地址", type: "text" },
    { key: "proof_path", label: "来源登记路径", type: "text", hint: "R2 留档：profile.json 的路径" },
    { key: "licensed_to", label: "授权给谁", type: "text" },
  ],
};

/** 编辑器的草稿：一律是**字符串或布尔**（表单里本来就是这么存的）。 */
export type FieldDraft = Record<string, string | boolean>;

/** 表单里的字符串 → 去掉首尾空白（`""` 与"没填"是同一件事）。 */
function _text(draft: FieldDraft, key: string): string {
  const value = draft[key];
  return typeof value === "string" ? value.trim() : "";
}

/** 标签：逗号（中英文都认）或空白分隔。 */
export function parseTags(raw: string): string[] {
  return raw
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** 从一条素材读出**当前值**（编辑器打开时的初始草稿）。 */
export function fieldDraft(item: AssetItem): FieldDraft {
  const draft: FieldDraft = {};
  for (const field of EDIT_FIELDS[item.kind]) {
    switch (field.key) {
      case "has_text":
        draft[field.key] = item.kind === "broll" ? item.has_text : false;
        break;
      case "usable_from_ms":
        draft[field.key] = item.kind === "broll" ? String(item.usable_from_ms) : "";
        break;
      case "usable_to_ms":
        draft[field.key] =
          item.kind === "broll" && item.usable_to_ms !== null ? String(item.usable_to_ms) : "";
        break;
      case "tags":
        draft[field.key] = item.kind === "voice" ? "" : item.tags.join(", ");
        break;
      case "mood":
        draft[field.key] = item.kind === "bgm" ? (item.mood ?? "") : "";
        break;
      case "bpm":
        draft[field.key] = item.kind === "bgm" && item.bpm !== null ? String(item.bpm) : "";
        break;
      case "loopable":
        draft[field.key] = item.kind === "bgm" ? item.loopable : false;
        break;
      case "source_url":
        draft[field.key] = item.source_url ?? "";
        break;
      case "proof_path":
        draft[field.key] = item.proof_path ?? "";
        break;
      case "licensed_to":
        draft[field.key] = item.licensed_to ?? "";
        break;
      default:
        break;
    }
  }
  return draft;
}

/** 一次「保存」要提交的东西：只交**改过的**字段 + 解析不出来的那些错。 */
export interface FieldChange {
  body: AssetPatchBody;
  errors: string[];
}

/**
 * 草稿 → PATCH 请求体（**只交改过的字段**）。
 *
 * 为什么只交改过的：整行提交会把没动过的字段用"打开编辑器那一刻的旧值"重写一遍 ——
 * 两个标签页同时开着就会互相覆盖，而覆盖是**静默**的。
 *
 * 数字只在**解析不出来**时报错（"这不是个数"）；区间是否合法（`to > from`）**不在这里判**
 * —— 那是后端 `_clean_fields` 的判据，前端再抄一份只会得到"面板放行、保存却 422"。
 */
export function changedFields(item: AssetItem, draft: FieldDraft): FieldChange {
  const body: AssetPatchBody = {};
  const errors: string[] = [];
  for (const field of EDIT_FIELDS[item.kind]) {
    const key = field.key;
    const raw = draft[key];
    if (field.type === "bool") {
      const next = raw === true;
      const current = key === "has_text" ? (item.kind === "broll" ? item.has_text : false) : item.kind === "bgm" ? item.loopable : false;
      if (next !== current) Object.assign(body, { [key]: next });
      continue;
    }
    const text = _text(draft, key);
    if (field.type === "tags") {
      const next = parseTags(text);
      const current = item.kind === "voice" ? [] : item.tags;
      if (next.join("\u0000") !== current.join("\u0000")) body.tags = next;
      continue;
    }
    if (field.type === "number") {
      if (text === "") {
        // 只有"可空"的数字字段允许清空（可用终点 / BPM）；起点为空是没填。
        if (key === "usable_to_ms" && item.kind === "broll" && item.usable_to_ms !== null) {
          body.usable_to_ms = null;
        } else if (key === "bpm" && item.kind === "bgm" && item.bpm !== null) {
          body.bpm = null;
        } else if (key === "usable_from_ms" || key === "bpm") {
          errors.push(`${field.label}不能留空`);
        }
        continue;
      }
      const parsed = Number(text);
      if (!Number.isFinite(parsed)) {
        errors.push(`${field.label}要填数字`);
        continue;
      }
      const current = key === "usable_from_ms" && item.kind === "broll" ? item.usable_from_ms : null;
      if (key === "usable_from_ms") {
        if (item.kind === "broll" && parsed !== current) body.usable_from_ms = Math.trunc(parsed);
      } else if (key === "usable_to_ms") {
        if (item.kind === "broll" && (item.usable_to_ms === null || Math.trunc(parsed) !== item.usable_to_ms)) {
          body.usable_to_ms = Math.trunc(parsed);
        }
      } else if (key === "bpm") {
        if (item.kind === "bgm" && (item.bpm === null || parsed !== item.bpm)) body.bpm = parsed;
      }
      continue;
    }
    // 文本字段：空 ⇒ `null`（**清空**，不是"写一个空串"）
    const current = _currentText(item, key);
    if (text === current) continue;
    Object.assign(body, { [key]: text === "" ? null : text });
  }
  return { body, errors };
}

/** 文本字段的当前值（`null` 与 `""` 在这里是同一件事：都没填）。 */
function _currentText(item: AssetItem, key: PatchKey): string {
  switch (key) {
    case "mood":
      return item.kind === "bgm" ? (item.mood ?? "") : "";
    case "source_url":
      return item.source_url ?? "";
    case "proof_path":
      return item.proof_path ?? "";
    case "licensed_to":
      return item.licensed_to ?? "";
    default:
      return "";
  }
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const useAssetsStore = defineStore("assets", () => {
  const { status: wsStatus, connect, resync } = useWsConnection();

  /** 当前菜单看的是哪一类（`Assets.vue` 挂载时按面板 id 设定）。 */
  const kind = ref<AssetKind>("broll");
  /** 当前这一类的一页（`null` = 还没拉到）。 */
  const page = ref<AssetPage | null>(null);
  /** 三类家底（授权枚举 / 降级判定 / 判据线都在里面，与这一页无关）。 */
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
  /** 上传中（上传可能要几十秒，按钮与文件框都要禁用掉）。 */
  const uploadBusy = ref(false);
  /** 上一次上传的回执（逐条结局 + 这一趟的入库报告）。 */
  const upload = ref<UploadResult | null>(null);
  /** 上传时是否允许替换同名文件。**默认 `false`**：绝不静默盖掉已有素材。 */
  const overwrite = ref(false);
  /** 音色的目录名（也是素材 id）—— 上传参考音时必填。 */
  const voiceId = ref("");
  /** 参考音文字稿：一行对应一段（第 1 行 ↔ `ref_01`）。 */
  const voiceText = ref("");
  /** 面板上"先看看会怎样"的开关（`true` ⇒ 扫盘不写库）。 */
  const dryRun = ref(false);
  /** 正在改的那一条（按 id；`null` = 没有在途的写入）。 */
  const pendingId = ref<string | null>(null);
  /** 正在编辑的那一条（按 id；编辑是**本地状态**，不进 URL）。 */
  const editingId = ref<string | null>(null);
  /** R2 来源登记留档（T5.5）：与发布面板共用同一份。 */
  const compliance = ref<ComplianceView | null>(null);

  // ── 分页与筛选（三个数各自独立：换一个就回第 1 页）────────────────
  const pageIndex = ref(1);
  const pageSize = ref(DEFAULT_PAGE_SIZE);
  const query = ref("");
  const enabledFilter = ref<EnabledFilter>("all");

  const items = computed<AssetItem[]>(() => page.value?.items ?? []);
  const pages = computed(() => page.value?.pages ?? 1);
  const total = computed(() => page.value?.total ?? 0);
  const licenses = computed<string[]>(
    () => stats.value?.licenses ?? ["self_recorded", "authorized", "cc0", "purchased"],
  );
  const degradedNote = computed(() => degradedText(stats.value));
  const fields = computed<readonly FieldSpec[]>(() => EDIT_FIELDS[kind.value]);

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const [nextPage, nextStats] = await Promise.all([
        api.fetchAssetPage({
          kind: kind.value,
          page: pageIndex.value,
          page_size: pageSize.value,
          q: query.value.trim() === "" ? undefined : query.value.trim(),
          enabled: enabledParam(enabledFilter.value),
        }),
        api.fetchAssetStats(),
      ]);
      page.value = nextPage;
      stats.value = nextStats;
      // 页码以**服务端**为准：翻过头（最后一页被筛空了）它给的是最后一页。
      pageIndex.value = nextPage.page;
      loadError.value = null;
    } catch (failure) {
      // 旧快照**留着**：一屏素材不因一次抖动变空白（与总览台同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  /** 切到这个菜单管的那一类（`Assets.vue` 挂载时调一次）。 */
  function open(next: AssetKind): void {
    if (kind.value !== next) {
      kind.value = next;
      pageIndex.value = 1;
      query.value = "";
      enabledFilter.value = "all";
      editingId.value = null;
      upload.value = null;
    }
  }

  function goPage(next: number): void {
    pageIndex.value = Math.max(1, next);
    void refresh();
  }

  function setPageSize(next: number): void {
    pageSize.value = next;
    pageIndex.value = 1;
    void refresh();
  }

  let queryTimer: ReturnType<typeof setTimeout> | null = null;

  /** 搜索框（防抖：逐键重拉一整页是本地面板上最没必要的一种忙）。 */
  function setQuery(next: string): void {
    query.value = next;
    if (queryTimer !== null) clearTimeout(queryTimer);
    queryTimer = setTimeout(() => {
      queryTimer = null;
      pageIndex.value = 1;
      void refresh();
    }, ASSETS_QUERY_DEBOUNCE_MS);
  }

  function setEnabledFilter(next: EnabledFilter): void {
    enabledFilter.value = next;
    pageIndex.value = 1;
    void refresh();
  }

  /**
   * R2 来源登记体检（**只读**）。
   *
   * 放在素材库这一屏的理由：登记就在这些素材身上（音色的 profile.json、BGM 的授权书、
   * 跑酷素材的来源地址）。把它只放在发布面板，等于让"入库时该补什么"和
   * "发布时缺什么"分散在两屏 —— 而人在素材库这一屏才有机会补。
   */
  async function loadCompliance(): Promise<void> {
    try {
      compliance.value = await api.fetchCompliance();
    } catch (failure) {
      // 读不到合规快照**不算这一屏坏了**：素材该看还能看，缺的只是那行提示。
      loadError.value = describeError(failure);
    }
  }

  /** 扫盘（`dry_run`）或入库。两者是**同一条服务端路径**，前端只是一个开关。 */
  async function scan(kindArg?: AssetKind, ids?: string[]): Promise<IngestReport | null> {
    busy.value = true;
    error.value = null;
    try {
      const body: IngestBody = {
        dry_run: dryRun.value,
        license: license.value,
        kind: kindArg ?? kind.value,
      };
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

  /**
   * 上传文件到某一类（跑酷 / BGM）—— 落盘与入库是**同一个请求**。
   *
   * 与 `scan` 分开而不是复用它：上传的对象是"浏览器刚递过来的字节"，扫描的对象是
   * "盘上已经有的东西"。合成一个动作，会让"我到底传没传上去"变成一个要靠报告反推的问题。
   */
  async function uploadFiles(kindArg: FlatKind, files: File[]): Promise<UploadResult | null> {
    if (files.length === 0) return null;
    return runUpload(() =>
      api.uploadAssets({ kind: kindArg, files, license: license.value, overwrite: overwrite.value }),
    );
  }

  /**
   * 上传一个音色的参考音。
   *
   * `voiceId` 空着就直接拦下来：它是目录名，服务端只会回一个 422 `VALIDATION_FAILED`
   * （"请求参数不合法"），而用户该看到的是"先填音色 id"。
   */
  async function uploadVoiceFiles(files: File[]): Promise<UploadResult | null> {
    if (files.length === 0) return null;
    const id = voiceId.value.trim();
    if (id === "") {
      error.value = "先填音色 id（它就是这个音色的目录名，也是素材 id，例如 bear_da）";
      return null;
    }
    return runUpload(() =>
      api.uploadVoice({
        voiceId: id,
        files,
        refText: voiceText.value,
        license: license.value,
        overwrite: overwrite.value,
      }),
    );
  }

  /** 上传的公共收尾：忙位、错误位、回执、重拉（库里变了）。 */
  async function runUpload(call: () => Promise<UploadResult>): Promise<UploadResult | null> {
    uploadBusy.value = true;
    error.value = null;
    try {
      const result = await call();
      upload.value = result;
      // 落盘成功的那些**当场入了库** ⇒ 复用「扫盘报告」那张表：一次上传与一次扫描
      // 在库里干的是同一件事，没有理由画两张不一样的表。
      if (result.report !== null) report.value = result.report;
      await refresh();
      return result;
    } catch (failure) {
      error.value = describeError(failure);
      return null;
    } finally {
      uploadBusy.value = false;
    }
  }

  /** 启用 / 停用（**不动物理文件**）。 */
  async function setEnabled(item: AssetItem, enabled: boolean): Promise<boolean> {
    return applyPatch(item, { enabled });
  }

  /**
   * 删掉一条素材（裁定 369）。
   *
   * `purge` 由调用方给：音色传 `true`（不删盘上目录，下次扫盘它自己就回来了），
   * 跑酷 / BGM 传 `false`（文件留在盘上、出片照样挑得到，删掉的只是留痕）。
   *
   * 与 `setEnabled` 共用 `pendingId`：两件事都改这一行，同时只该有一个在飞。
   */
  async function remove(item: AssetItem, purge: boolean): Promise<boolean> {
    pendingId.value = item.id;
    error.value = null;
    try {
      await api.deleteAsset(item.id, item.kind, purge);
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      pendingId.value = null;
    }
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

  /** 打开 / 关闭某一行的编辑器（同时只有一行在编辑）。 */
  function toggleEditor(item: AssetItem): void {
    editingId.value = editingId.value === item.id ? null : item.id;
  }

  /**
   * 保存编辑器里的改动（**只交改过的字段**）。
   *
   * 解析不出来的输入（"可用起点不是个数字"）在**发请求之前**就报出来：让后端回一句
   * "字段不合法"只会让人去猜是哪一个框。
   */
  async function saveFields(item: AssetItem, draft: FieldDraft): Promise<boolean> {
    const { body, errors } = changedFields(item, draft);
    if (errors.length > 0) {
      error.value = errors.join("；");
      return false;
    }
    if (Object.keys(body).length === 0) {
      error.value = null;
      editingId.value = null;
      return true;
    }
    const ok = await applyPatch(item, body);
    if (ok) editingId.value = null;
    return ok;
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
    void loadCompliance();
  }

  function stop(): void {
    if (coalescer !== null) {
      clearTimeout(coalescer);
      coalescer = null;
    }
    if (queryTimer !== null) {
      clearTimeout(queryTimer);
      queryTimer = null;
    }
  }

  return {
    // 状态
    kind,
    page,
    stats,
    compliance,
    loading,
    loadError,
    error,
    report,
    busy,
    license,
    dryRun,
    pendingId,
    editingId,
    upload,
    uploadBusy,
    overwrite,
    voiceId,
    voiceText,
    pageIndex,
    pageSize,
    query,
    enabledFilter,
    // 派生
    items,
    pages,
    total,
    licenses,
    degradedNote,
    fields,
    wsStatus,
    // 动作
    open,
    refresh,
    goPage,
    setPageSize,
    setQuery,
    setEnabledFilter,
    loadCompliance,
    scan,
    uploadFiles,
    uploadVoiceFiles,
    setEnabled,
    remove,
    applyPatch,
    toggleEditor,
    saveFields,
    resync,
    start,
    stop,
  };
});
