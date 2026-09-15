// 素材库 REST 面（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。
//
// 六个端点对应面板上的六件事
// ------------------------
// 看（`GET /assets`）、要数字（`GET /assets/stats`）、扫盘入库（`POST /assets/ingest`）、
// 标记（`PATCH /assets/{id}`）、预览（`GET /assets/{id}/thumb`）、试听（`GET /assets/{id}/media`）。
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

import { apiGet, apiPatch, apiPost, type OkJson } from "../http";
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

export type IngestReport = OkJson<"/api/v1/assets/ingest", "post">;
export type IngestSection = IngestReport["sections"][number];
export type ScannedAsset = IngestSection["assets"][number];
export type IngestBody = BodyJson<"/api/v1/assets/ingest", "post">;
export type AssetPatchBody = BodyJson<"/api/v1/assets/{asset_id}", "patch">;

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

/** 缩略图地址（交给 `<img src>`；**没有缩略图时是 404**，组件据此显示占位）。 */
export function assetThumbUrl(assetId: string): string {
  return `${ASSETS_PATH}/${encodeURIComponent(assetId)}/thumb`;
}

/** 原文件地址（交给 `<audio src>` / `<video src>`；音色是目录 ⇒ 该端点是 422）。 */
export function assetMediaUrl(assetId: string): string {
  return `${ASSETS_PATH}/${encodeURIComponent(assetId)}/media`;
}
