// 渲染面板 REST 面（T4.6 · §04.2.8）。
//
// 四个端点对应面板上的四件事：看首屏（`/render/console`）、开一条（`POST /render/jobs`）、
// 看进度（`GET /render/jobs/{id}`）、叫停（`POST /render/jobs/{id}/cancel`）。
// 成片本身不走这里 —— 它由后端返回的 `url` 直接喂 `<video src>`，浏览器自己发范围请求。
//
// 为什么**没有**"重新渲染一遍"的端点
// ----------------------------------
// 那件事等价于"用同样的参数再开一条任务"，而参数已经在上一条的 `request` 里了
// （面板上那颗「再来一条」按钮就是把 `request` 填回表单）。多一个端点等于多一处
// "参数从哪来"的分叉，而分叉的代价是"面板说复现了、出来的却是另一条片子"。
//
// 请求体类型不手写
// ----------------
// 与合成配置 / 素材库同一条：从 `types.gen.ts` 里取。手写一份形状，等于在
// 「后端改了字段名」这件事上自愿放弃编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type RenderConsole = OkJson<"/api/v1/render/console", "get">;
export type RenderProfileOption = RenderConsole["profiles"][number];
export type RenderVoice = RenderConsole["voices"][number];
export type RenderVideo = RenderConsole["videos"][number];
export type RenderJob = OkJson<"/api/v1/render/jobs", "post">;
export type RenderJobRequest = BodyJson<"/api/v1/render/jobs", "post">;

const RENDER_PATH = "/api/v1/render";

/** 面板首屏：可选参数（profile / 音色 / 引擎）+ 在跑的任务 + 最近任务 + 成片列表。 */
export function fetchRenderConsole(signal?: AbortSignal): Promise<RenderConsole> {
  return apiGet<RenderConsole>(`${RENDER_PATH}/console`, { signal });
}

/** 开一条出片任务（**立刻返回**：真正的活在服务端的后台线程里跑）。 */
export function createRenderJob(body: RenderJobRequest, signal?: AbortSignal): Promise<RenderJob> {
  return apiPost<RenderJob>(`${RENDER_PATH}/jobs`, body, { signal });
}

/** 查一条任务的进度 / 结果（面板轮询的就是它）。 */
export function fetchRenderJob(jobId: string, signal?: AbortSignal): Promise<RenderJob> {
  return apiGet<RenderJob>(`${RENDER_PATH}/jobs/${encodeURIComponent(jobId)}`, { signal });
}

/**
 * 叫停一条任务。
 *
 * 后端是**协作式**取消：排队中的立刻作废，已经在跑的会在下一次进度回调处停下 ——
 * 而 ffmpeg 一旦跑起来要等它自己结束。面板上必须把这句话写出来，否则用户按了取消
 * 看见进度条还在动，会以为按钮坏了，然后反复按。
 */
export function cancelRenderJob(jobId: string, signal?: AbortSignal): Promise<RenderJob> {
  return apiPost<RenderJob>(
    `${RENDER_PATH}/jobs/${encodeURIComponent(jobId)}/cancel`,
    {},
    { signal },
  );
}

/** 成片地址（交给 `<video src>` / 下载链接）。`url` 由后端给，这里只做兜底拼接。 */
export function renderVideoUrl(name: string): string {
  return `${RENDER_PATH}/videos/${encodeURIComponent(name)}`;
}