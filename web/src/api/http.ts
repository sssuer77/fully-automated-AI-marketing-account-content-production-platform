// HTTP 出口（T4.1）—— **整个前端唯一允许出现 `fetch(` 的地方**。
//
// 为什么收敛到一个文件：`tests/contract/test_web_contracts.py` 会静态扫描
// `web/src/**`，发现第二处 `fetch(` 就报错。这样"统一超时 / 统一错误信封 /
// 统一带请求头"才有唯一落点，而不是靠自觉。

import type { paths } from "./types.gen";
import { filenameFromHeaders } from "@/utils/download";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type QueryValue = string | number | boolean | null | undefined;

/** 一次文件下载的结果（`rows` 供"是否可能被截断"的提示用）。 */
export interface DownloadResult {
  text: string;
  filename: string;
  rows: number;
}

/** 请求选项（`signal` 用于组件卸载时取消在途请求）。 */
export interface RequestOptions {
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 10_000;

/**
 * 从**统一错误信封**里取人话。
 *
 * 后端把业务错误与入参校验错误都翻成了 `StudioError.to_dict()`
 * （`app/errors.py`），所以这里能稳定拿到 `message`。少了这一步，用户看到的是
 * "reject 返回 422"，而真正该看到的是"退回必须写清理由"。
 */
export function errorMessage(payload: unknown, fallback: string): string {
  if (payload !== null && typeof payload === "object" && "message" in payload) {
    const message = (payload as { message: unknown }).message;
    if (typeof message === "string" && message.length > 0) return message;
  }
  return fallback;
}

/** 导出的超时更宽松：它是"生成一个大文件"，不是"问一句状态"。 */
const DOWNLOAD_TIMEOUT_MS = 60_000;

export function buildQuery(query: Record<string, QueryValue> | undefined): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    // null / undefined 一律**不发**这个参数：后端把它们当"没传"，
    // 发 `level=null` 反而会撞上 422。
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/**
 * GET 一个 JSON 端点。
 *
 * `path` 用**完整**路径（含 `/api/v1` 前缀）—— 与 `types.gen.ts` 里的键逐字对应，
 * 于是"改了后端路径但没改前端"会在 `npm run typecheck` 阶段就炸，而不是等到运行时 404。
 */
export async function apiGet<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  try {
    const response = await fetch(`${path}${buildQuery(options.query)}`, {
      method: "GET",
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    const text = await response.text();
    const payload: unknown = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new ApiError(
        errorMessage(payload, `${path} 返回 ${response.status}`),
        response.status,
        payload,
      );
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError(`${path} 超时（${timeoutMs} ms）或被取消`, 0, null);
    }
    throw new ApiError(`${path} 请求失败：${String(error)}`, 0, null);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

/**
 * 发一个带 JSON 体的端点（T4.4 起：确认闸的决断动作；T4.8 起：素材标记）。
 *
 * 与 `apiGet` 共用超时与错误信封。`body` 省略时发 `{}` —— 确认闸的"放行"本来
 * 就可能没有意见，让调用方为了发一个空对象去编一个参数没有意义。
 *
 * 为什么 POST 与 PATCH 走**同一个函数**
 * ------------------------------------
 * 两者的差别只有方法名一个词。各写一遍的后果是"超时 / 错误信封 / 请求头"三件事
 * 要修两处，而这类漂移平时看不出来，只在某次网络异常时表现为"POST 有提示、
 * PATCH 只显示 '请求失败'"。
 */
async function sendJson<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  try {
    const response = await fetch(`${path}${buildQuery(options.query)}`, {
      method,
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal: controller.signal,
    });
    const text = await response.text();
    const payload: unknown = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new ApiError(
        errorMessage(payload, `${path} 返回 ${response.status}`),
        response.status,
        payload,
      );
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError(`${path} 超时（${timeoutMs} ms）或被取消`, 0, null);
    }
    throw new ApiError(`${path} 请求失败：${String(error)}`, 0, null);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

/** POST 一个 JSON 端点。 */
export function apiPost<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return sendJson<T>("POST", path, body, options);
}

/**
 * PUT 一个 JSON 端点（T6.1：LLM 密钥 —— **幂等替换**）。
 *
 * 为什么是 PUT 而不是 POST：写密钥是「同一个值写两次，结果一样」的替换动作
 * （第二次连盘都不碰）。PUT 的语义正好是这个；POST 会让人以为「每点一次就多一把」。
 */
export function apiPut<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return sendJson<T>("PUT", path, body, options);
}


/**
 * PATCH 一个 JSON 端点（T4.8：素材标记 —— 只交**显式给了**的字段）。
 *
 * 为什么是 PATCH 而不是 POST：素材的字段是"各自独立的一小块"（授权 / 标签 /
 * 可用区间 / 启停），PATCH 的语义正是"只改我点到的这几个"。用 POST 会让人
 * 以为"提交的是一整行"，于是前端开始把整行发上来 —— 而整行发上来会静默覆盖
 * 别人刚改过的字段。
 */
export function apiPatch<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return sendJson<T>("PATCH", path, body, options);
}

/**
 * DELETE 一个 JSON 端点（T5.6：删掉一条定时计划）。
 *
 * 为什么不是 `POST .../delete`：删除是 HTTP 本来就有的动词，而 `POST /x/delete`
 * 这种写法会让「谁都没接住的 DELETE」在网关那一层静默变成 405，
 * 排查时看到的是「接口不存在」而不是「方法写错了」。
 * 响应体仍然解析（后端回了 `{deleted, message}`）—— 只回 204 的话，
 * 面板就答不上「它本来就不在」和「刚被我删掉」这两件事的区别。
 */
export function apiDelete<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return sendJson<T>("DELETE", path, undefined, options);
}

/**
 * 上传的默认超时（T4.8：素材上传）。
 *
 * 它搬的是**字节**，不是"问一句状态"：默认那 10 秒在同一个局域网里传一个 200 MB 的
 * 跑酷片段时**必然**超时，而超时的表现是"进度条走完了、什么都没发生"——最难查的一类。
 */
const UPLOAD_TIMEOUT_MS = 300_000;

/**
 * 发一个 multipart 表单（T4.8：素材上传）。
 *
 * **不要自己设 `content-type`**：multipart 的 boundary 是浏览器生成的，手写一个
 * `content-type: multipart/form-data` 会让后端**解不出任何一个字段**，而报错只会说
 * "缺 kind" —— 从"少一个请求头"查回"少一个请求头"要绕一大圈。
 * 把 `FormData` 直接交给 `fetch`，它会自己补上带 boundary 的头。
 *
 * 与 `apiGet` / `sendJson` 共用超时与错误信封：这样"统一超时 / 统一错误信封 /
 * 统一请求头"仍然只有一处需要维护（契约测试锁的就是这一点）。
 */
export async function apiUpload<T>(
  path: string,
  form: FormData,
  options: RequestOptions = {},
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? UPLOAD_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  try {
    const response = await fetch(`${path}${buildQuery(options.query)}`, {
      method: "POST",
      headers: { accept: "application/json" },
      body: form,
      signal: controller.signal,
    });
    const text = await response.text();
    const payload: unknown = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new ApiError(
        errorMessage(payload, `${path} 返回 ${response.status}`),
        response.status,
        payload,
      );
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError(`${path} 超时（${timeoutMs} ms）或被取消`, 0, null);
    }
    throw new ApiError(`${path} 请求失败：${String(error)}`, 0, null);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

/**
 * 下载一个**流式**端点（T4.9 的 NDJSON 导出）。
 *
 * 与 `apiGet` 共用超时 / 错误信封，但返回文本与文件名而不是解析后的 JSON：
 * 导出是"整块交给浏览器落盘"的东西，提前 `JSON.parse` 只会白白吃掉一份内存。
 */
export async function apiDownload(
  path: string,
  options: RequestOptions = {},
): Promise<DownloadResult> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DOWNLOAD_TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  try {
    const response = await fetch(`${path}${buildQuery(options.query)}`, {
      method: "GET",
      headers: { accept: "application/x-ndjson" },
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail: unknown = await response.text();
      throw new ApiError(`${path} 返回 ${response.status}`, response.status, detail);
    }
    const text = await response.text();
    return {
      text,
      filename: filenameFromHeaders(response.headers, "studio-logs.ndjson"),
      rows: text.split("\n").filter((line) => line.length > 0).length,
    };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError(`${path} 超时（${timeoutMs} ms）或被取消`, 0, null);
    }
    throw new ApiError(`${path} 请求失败：${String(error)}`, 0, null);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

/** 便捷别名：某个操作的 200 JSON 响应体类型（改后端 ⇒ 这里会跟着变）。 */
export type OkJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : never;
