// 成片库 REST 面（T5.11 · §06.11）。
//
// 两个端点对应面板上的两件事：看（`GET /library`）与**批量投递**（`POST /library/publish`）。
//
// 为什么批量投递是一个端点，而不是让面板循环调单条那个
// ----------------------------------------------------
// 循环调 N 次的代价有三条，都不是理论上的：① 勾 20 条就是 20 个请求，中间断一次
// （刷新、切屏）会留下一半投了一半没投的状态，而面板上没有任何地方记得"刚才投到
// 第几条"；② 每一条的失败要在前端各拼一次文案，于是"这个平台未启用"这句话会出现在
// 前端；③ 上限只能在前端判，而前端判据与后端分叉时，用户看到的是"按钮能点、点了报错"。
//
// 为什么成片库里**没有**"这一版还是那一版"的选择
// ----------------------------------------------
// 发布池认的是任务号，它自己去找成片（`manifest.json` 优先、目录兜底）。面板上让
// 用户选文件，而发布时用的是另一个 —— 那是一句谎话。所以一行 = 一条任务，
// `versions` 只是"它重出过几版"的交代。
//
// 请求体类型不手写
// ----------------
// 与其余端点文件同一条：从 `types.gen.ts` 里取。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type LibraryResponse = OkJson<"/api/v1/library", "get">;
export type LibraryItem = LibraryResponse["items"][number];
/** 一行里的发布记录（与发布面板是**同一个**模型，不另起一个精简版）。 */
export type LibraryPublication = LibraryItem["publications"][number];
export type LibraryPublishBody = BodyJson<"/api/v1/library/publish", "post">;
export type LibraryPublishResponse = OkJson<"/api/v1/library/publish", "post">;
export type LibraryBatchItem = LibraryPublishResponse["items"][number];

const LIBRARY_PATH = "/api/v1/library";

/** 成片库：盘上已出的片子，一条任务一行（新的在前）。 */
export function fetchLibrary(
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<LibraryResponse> {
  return apiGet<LibraryResponse>(LIBRARY_PATH, {
    query: { limit: options.limit ?? null },
    signal: options.signal,
  });
}

/** 批量投递（**立刻返回**：真正发的是 publish 池）。 */
export function publishSelected(
  body: LibraryPublishBody,
  signal?: AbortSignal,
): Promise<LibraryPublishResponse> {
  return apiPost<LibraryPublishResponse>(`${LIBRARY_PATH}/publish`, body, { signal });
}