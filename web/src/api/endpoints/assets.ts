// 素材库 REST 面（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。
//
// 十个端点对应面板上的十件事
// ------------------------
// 看（`GET /assets`）、要数字（`GET /assets/stats`）、**看一类的一页**（`GET /assets/list`）、
// 扫盘入库（`POST /assets/ingest`）、标记（`PATCH /assets/{id}`）、预览（`GET /assets/{id}/thumb`）、
// 试听（`GET /assets/{id}/media`）、上传（`POST /assets/upload` / `POST /assets/voice`）、
// 清孤儿（`POST /assets/prune`）。
//
// 为什么"看"有两个端点
// --------------------
// `GET /assets` 一次给三类（每类的**全部**条目）：适合"我就要一眼看全"。
// `GET /assets/list` 给一类的**一页**：素材库按类别分菜单之后，每一屏只需要自己那一类，
// 而一柜子素材迟早会到几百条 —— 一次拖过来既慢又没人看得完。
//
// 为什么"扫盘"和"入库"是同一个端点
// --------------------------------
// `POST /assets/ingest` 带 `dry_run: true` 就是"先看看会怎样"。分成两个端点会让两条
// 路径各写一遍"怎么判定一条素材合格"—— 而它们的差异，恰恰就是"预览说能进、真入库
// 却被拒"的来源。
//
// 为什么预览/试听是 `url` 而不是 `fetch`
// -------------------------------------
// 图片与音频交给 `<img>` / `<audio>` 直接取：浏览器自己会做范围请求、缓存与并发控制。
// 走 `fetch` 再转 blob 等于把这些全丢掉，还要自己管 objectURL 的生命周期。
// 但**路径只从库里取**（`asset_id` 是白名单正则），前端不拼任何文件路径。
//
// 请求体类型不手写
// ----------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份形状，就等于在"后端改了
// 字段名"这件事上自愿放弃了编译期保护。

import { apiDelete, apiGet, apiPatch, apiPost, apiUpload, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type AssetsLibrary = OkJson<"/api/v1/assets", "get">;
export type AssetsStats = OkJson<"/api/v1/assets/stats", "get">;
export type AssetSection = AssetsLibrary["sections"][number];
export type AssetItem = AssetSection["items"][number];
export type AssetBrollItem = Extract<AssetItem, { kind: "broll" }>;
export type AssetBgmItem = Extract<AssetItem, { kind: "bgm" }>;
export type AssetVoiceItem = Extract<AssetItem, { kind: "voice" }>;
export type AssetKind = AssetItem["kind"];
export type AssetThresholds = AssetsStats["thresholds"];

/**
 * 平铺素材（有文件名的那种）。
 *
 * 上传端点**不收** `voice`：音色是一个目录（`ref_NN` + `ref.txt`），它的表单字段
 * （`voice_id` / `ref_text`）与跑酷 / BGM 完全不同，所以走自己的端点。用类型把这条
 * 边界钉住，比在运行时 422 早一步。
 */
export type FlatKind = Exclude<AssetKind, "voice">;

export type UploadResult = OkJson<"/api/v1/assets/upload", "post">;
export type UploadedFile = UploadResult["files"][number];

/** 一次平铺上传（面板把文件与当前设置攒成这个形状）。 */
export interface UploadRequest {
  kind: FlatKind;
  files: File[];
  /** 只对**本次新入库**的条目生效（与 `POST /assets/ingest` 同一条口径）。 */
  license?: string | null;
  /** 默认 `false` ⇒ 撞名逐条跳过，**绝不静默盖掉**已有素材。 */
  overwrite?: boolean;
}

/** 一次音色上传：`voiceId` 是目录名，也**就是**素材 id。 */
export interface VoiceUploadRequest {
  voiceId: string;
  files: File[];
  /** 参考音文字稿，一行对应一段（第 1 行 ↔ `ref_01`）。 */
  refText?: string | null;
  license?: string | null;
  overwrite?: boolean;
}

/** 一类素材的一页（素材库的每个菜单各取自己那一类）。 */
export type AssetPage = OkJson<"/api/v1/assets/list", "get">;
export type AssetPageItem = AssetPage["items"][number];
export type AssetPageStats = AssetPage["stats"];

/** 取一页的参数（`q` / `enabled` 是筛选，`page` 从 1 起）。 */
export interface AssetPageParams {
  kind: AssetKind;
  page?: number;
  page_size?: number;
  /** 按 id 或标签筛（服务端不区分大小写）。 */
  q?: string;
  /** 只看启用 / 只看停用；`undefined` = 全部。 */
  enabled?: boolean;
}

export type IngestReport = OkJson<"/api/v1/assets/ingest", "post">;
export type IngestSection = IngestReport["sections"][number];
export type ScannedAsset = IngestSection["assets"][number];
export type IngestBody = BodyJson<"/api/v1/assets/ingest", "post">;
export type AssetPatchBody = BodyJson<"/api/v1/assets/{asset_id}", "patch">;

/** 一次删除的回执（`purged` 是**真的从盘上删掉的路径**，见后端 `AssetDeleteModel`）。 */
export type AssetDeleteResult = OkJson<"/api/v1/assets/{asset_id}", "delete">;
export type PruneReport = OkJson<"/api/v1/assets/prune", "post">;
export type PrunedOrphan = PruneReport["removed"][number];
export type KeptOrphan = PruneReport["kept"][number];

/** 一个音色的**逐段现状**（`GET /assets/voice/{id}/segments` · 裁定 381）。 */
export type VoiceSegments = OkJson<"/api/v1/assets/voice/{voice_id}/segments", "get">;
export type VoiceSegment = VoiceSegments["segments"][number];

/** 删掉一段参考音的回执（`renamed` 是重编号的流水账，**必须显示**）。 */
export type VoiceSegmentRemoval = OkJson<
  "/api/v1/assets/voice/{voice_id}/segments/{name}",
  "delete"
>;

const ASSETS_PATH = "/api/v1/assets";

/** 一次拿全：三类分节（条目 + 家底 + 缺口）+ 降级判定（面板首屏就这一个请求）。 */
export function fetchAssets(signal?: AbortSignal): Promise<AssetsLibrary> {
  return apiGet<AssetsLibrary>(ASSETS_PATH, { signal });
}

/** 只要数字：三类家底 + 判据线（总览台的小卡片用它，不拖整库）。 */
export function fetchAssetStats(signal?: AbortSignal): Promise<AssetsStats> {
  return apiGet<AssetsStats>(`${ASSETS_PATH}/stats`, { signal });
}

/**
 * 扫盘 / 入库（`dry_run: true` ⇒ **一个字节都不写库**）。
 *
 * `license` 只对本次**新入库**的条目生效；已入库的按行里存的那份走。
 */
export function ingestAssets(body: IngestBody, signal?: AbortSignal): Promise<IngestReport> {
  return apiPost<IngestReport>(`${ASSETS_PATH}/ingest`, body, { signal });
}

/** 改一条素材（启用 / 停用 / 授权 / 标签 / 可用区间…）；**只交显式给了的字段**。 */
export function patchAsset(
  assetId: string,
  body: AssetPatchBody,
  signal?: AbortSignal,
): Promise<AssetItem> {
  return apiPatch<AssetItem>(`${ASSETS_PATH}/${encodeURIComponent(assetId)}`, body, { signal });
}

/**
 * 取一类素材的**一页**（素材库按类别分菜单 + 分页）。
 *
 * `page` 由**服务端钳**：翻过头（比如最后一页被筛空了）回的是最后一页 ——
 * 所以调用方拿到结果后要把 `pageIndex` 同步成响应里的 `page`，不要自己算。
 */
export function fetchAssetPage(params: AssetPageParams, signal?: AbortSignal): Promise<AssetPage> {
  return apiGet<AssetPage>(`${ASSETS_PATH}/list`, {
    query: {
      kind: params.kind,
      page: params.page,
      page_size: params.page_size,
      q: params.q,
      enabled: params.enabled,
    },
    signal,
  });
}

/**
 * 攒一个 multipart 表单。
 *
 * 三个"不发这个字段"的规则，各修一个坑：
 * - `undefined` / `null` / `""` 一律**不发**：发一个空的 `license=` 会被后端当成
 *   "本次授权是空字符串"，而不是"没填"；
 * - `false` 不发（`overwrite` 缺席就是默认的 false，发 `"false"` 只是多几个字节）；
 * - 文件用**同一个字段名**重复 append：FastAPI 侧的 `list[UploadFile]` 收的就是它。
 */
function uploadForm(
  fields: Record<string, string | null | undefined | false>,
  files: File[],
): FormData {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    form.set(key, value);
  }
  for (const file of files) form.append("files", file);
  return form;
}

/**
 * 上传跑酷 / BGM 素材 —— **落盘与入库是一次请求**（§3.3.14）。
 *
 * 为什么不让面板"先传文件、再点一次扫描"：用户点的是"把这个文件放进素材库"，
 * 两步的后果是面板上多出一批"盘上有、库里没有"的条目 —— 而那正是最容易被误读的
 * 一种（"我明明传了啊"）。
 */
export function uploadAssets(request: UploadRequest, signal?: AbortSignal): Promise<UploadResult> {
  return apiUpload<UploadResult>(
    `${ASSETS_PATH}/upload`,
    uploadForm(
      {
        kind: request.kind,
        license: request.license,
        overwrite: request.overwrite === true ? "true" : undefined,
      },
      request.files,
    ),
    { signal },
  );
}

/**
 * 删掉一条素材（裁定 369）。
 *
 * `purge` 决定**盘上那份**动不动：`false` 只删库里的行（重扫一次就回来），
 * `true` 才连文件一起删。音色在面板上默认走 `true` —— 参考音目录留在盘上，
 * 下次扫盘又会变成一条"盘上有、库里没有"，用户刚删掉的东西自己回来了。
 *
 * 为什么两个开关而不是一个：跑酷 / BGM 删了行，文件还在、出片照样挑得到（删的只是
 * 留痕）；音色反过来，配音只认库里的行。把它们写成同一个动作，必然有一半是错的。
 */
export function deleteAsset(
  assetId: string,
  kind: AssetKind,
  purge: boolean,
  signal?: AbortSignal,
): Promise<AssetDeleteResult> {
  return apiDelete<AssetDeleteResult>(`${ASSETS_PATH}/${encodeURIComponent(assetId)}`, {
    query: { kind, purge },
    signal,
  });
}

/**
 * 上传一个音色的参考音（§4.3.1：段数**不设上限**、每段 2–30 秒 —— 裁定 369 / 377），并当场入库。
 *
 * 参考音的**顺序**由服务端按原文件名排序决定（`ref.txt` 第 N 行 ↔ 第 N 段），
 * 面板要把"第 N 段 ← 哪个原文件"逐条显示出来。
 */
export function uploadVoice(request: VoiceUploadRequest, signal?: AbortSignal): Promise<UploadResult> {
  return apiUpload<UploadResult>(
    `${ASSETS_PATH}/voice`,
    uploadForm(
      {
        voice_id: request.voiceId,
        ref_text: request.refText,
        license: request.license,
        overwrite: request.overwrite === true ? "true" : undefined,
      },
      request.files,
    ),
    { signal },
  );
}

/**
 * 一个音色的逐段现状：段号 / 文件名 / 时长 / **同一位置的那行文本**。
 *
 * 位置即对应（`ref.txt` 第 N 行 ↔ 第 N 段）—— 所以 `text` 是「按位置取的那一行」，
 * 不是按文件名里的编号去查。判据与入库同一份（服务端 `check_voice`）。
 */
export function fetchVoiceSegments(
  voiceId: string,
  signal?: AbortSignal,
): Promise<VoiceSegments> {
  return apiGet<VoiceSegments>(
    `${ASSETS_PATH}/voice/${encodeURIComponent(voiceId)}/segments`,
    { signal },
  );
}

/**
 * 删掉一段参考音（**删完重编号 + 同步 `ref.txt`** —— 裁定 381）。
 *
 * 为什么这是删一段、而不是改一段：改一段走的是上传（勾「覆盖同名」）。这里回答的是
 * 「这段我不想要了」—— 比如当年为了凑段数把同一个文件复制了一份。
 */
export function deleteVoiceSegment(
  voiceId: string,
  name: string,
  signal?: AbortSignal,
): Promise<VoiceSegmentRemoval> {
  return apiDelete<VoiceSegmentRemoval>(
    `${ASSETS_PATH}/voice/${encodeURIComponent(voiceId)}/segments/${encodeURIComponent(name)}`,
    { signal },
  );
}

/**
 * 清掉这一类的**孤儿**（裁定 384）：盘上认得出、库里没有、**而且本身就不合格**。
 *
 * 为什么它不能挂在 `deleteAsset` 上：`DELETE /assets/{id}` 的对象是**库里那一行**，
 * 而孤儿**没有行** —— 后端在那条路上手上没有行就直接 404。所以"盘上有、库里没有"
 * 的东西以前在面板上**删不掉**，那句「盘上有 N 条还没入库」于是变成一条永远动不了的
 * 警告。
 *
 * `dryRun: true` ⇒ 只报"会清掉哪些"，一个字节都不动（面板点第一下用它）。
 * 合格的孤儿**不会被清**（它们该入库），后端照实回在 `kept` 里 —— 面板要把那一段
 * 显示出来，否则用户会以为"清了一遍，怎么还剩着"。
 */
export function pruneOrphans(
  kind: AssetKind,
  dryRun: boolean,
  signal?: AbortSignal,
): Promise<PruneReport> {
  return apiPost<PruneReport>(`${ASSETS_PATH}/prune`, { kind, dry_run: dryRun }, { signal });
}

/** 缩略图地址（交给 `<img src>`；**没有缩略图时是 404**，组件据此显示占位）。 */
export function assetThumbUrl(assetId: string): string {
  return `${ASSETS_PATH}/${encodeURIComponent(assetId)}/thumb`;
}

/** 原文件地址（交给 `<audio src>` / `<video src>`；音色是目录 ⇒ 该端点是 422）。 */
export function assetMediaUrl(assetId: string): string {
  return `${ASSETS_PATH}/${encodeURIComponent(assetId)}/media`;
}
