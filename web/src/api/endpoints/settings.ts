// 设置面板 REST 面（T6.1）—— LLM 通道与密钥的界面化配置。
//
// 四个端点对应面板上的四件事：看状态、存密钥、清密钥、测试连接。
//
// 为什么「存」与「清」是两个函数、一个端点
// --------------------------------------
// 后端是同一个 PUT（`clear=true` 表示清空）。前端分成两个函数，是因为**调用点的意图
// 不同**：`saveLlmKey("sk-...")` 与 `clearLlmKey()` 在读代码时一眼能分清，而
// `saveLlmKey(null)` 要读者去翻后端才知道 null 是什么意思。
//
// 请求体类型不手写
// ----------------
// 与其余端点文件同一条：从 `types.gen.ts` 里取。手写一份形状，等于在「后端改了字段名」
// 这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, apiPut, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type LlmSettings = OkJson<"/api/v1/settings/llm", "get">;
export type LlmProfile = NonNullable<LlmSettings["profiles"]>[number];
export type LlmKeyStatus = LlmSettings["key"];
export type LlmRoutingRow = NonNullable<LlmSettings["routing"]>[number];
export type LlmKeyBody = BodyJson<"/api/v1/settings/llm", "put">;
export type LlmKeyOutcome = OkJson<"/api/v1/settings/llm", "put">;
export type LlmProfileBody = BodyJson<"/api/v1/settings/llm/profile", "put">;
export type LlmProfileOutcome = OkJson<"/api/v1/settings/llm/profile", "put">;
export type LlmProbe = OkJson<"/api/v1/settings/llm/probe", "post">;
export type LlmProbeRow = NonNullable<LlmProbe["rows"]>[number];

const SETTINGS_PATH = "/api/v1/settings/llm";

/** 通道卡片 + 路由表 + 密钥状态（**只有掩码**）。 */
export function fetchLlmSettings(options: { signal?: AbortSignal } = {}): Promise<LlmSettings> {
  return apiGet<LlmSettings>(SETTINGS_PATH, { signal: options.signal });
}

/**
 * 保存密钥。写盘后**立刻生效**（网关每次调用现取一次，不需要重启任何进程）。
 *
 * `reason` 会进 `audit_ops` —— 换 Key 这件事要留得下"为什么换"。
 */
export function saveLlmKey(apiKey: string, reason?: string): Promise<LlmKeyOutcome> {
  return apiPut<LlmKeyOutcome>(SETTINGS_PATH, { api_key: apiKey, reason: reason ?? null });
}

/**
 * 清除密钥。
 *
 * 必须**显式**走这个函数：后端把「没给 api_key」当成"不知道你想干嘛"（422），
 * 而不是"清空" —— 否则某次少带一个字段就会静默删掉密钥，而症状要到下一次
 * 真跑任务才出现。
 */
export function clearLlmKey(reason?: string): Promise<LlmKeyOutcome> {
  return apiPut<LlmKeyOutcome>(SETTINGS_PATH, { clear: true, reason: reason ?? null });
}

/**
 * 改通道参数（模型名 / base_url）。
 *
 * 与密钥同一个 PUT 家族，但**写的是 `config/llm.yaml`**：模型名会变（换服务商、换档位、
 * 临时降本），而那份文件带着"为什么这么配"的注释 —— 所以后端是按行改写、不整份重写。
 * 生效方式与密钥相同：网关每次取配置先做一次 `stat`，常驻 worker **不需要重启**。
 */
export function saveLlmProfile(body: LlmProfileBody): Promise<LlmProfileOutcome> {
  return apiPut<LlmProfileOutcome>(`${SETTINGS_PATH}/profile`, body);
}

/**
 * 测试连接（**只发只读 GET**，不产生一次计费调用）。
 *
 * 超时给得比默认宽：探测本身的后端超时是 10s，前端若还是 10s 就会先一步掐断，
 * 用户看到的是"超时"而不是后端那句更有信息量的「连不上」。
 */
export function probeLlm(options: { signal?: AbortSignal } = {}): Promise<LlmProbe> {
  return apiPost<LlmProbe>(`${SETTINGS_PATH}/probe`, undefined, {
    signal: options.signal,
    timeoutMs: 20_000,
  });
}
