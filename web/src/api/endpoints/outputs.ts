// 合成配置 REST 面（T4.7 · §04.2.8 / §04.5.9）。
//
// 两个端点对应面板上的两件事：看（GET /outputs）、改（POST /outputs）。
//
// 为什么没有「只校验」的端点
// --------------------------
// 保存本身就**先校验后落盘**（`OutputsStore.update`）。再开一个 `/validate`，等于同一套
// 判定跑在两条路径上 —— 两条路径的差异就是「面板说没问题、保存却 422」的来源。
// 表单的即时反馈用响应里的 `limits` 自己判范围即可（后端给的就是那一份上限）。
//
// 为什么保存要带 `source_sha256`
// ------------------------------
// 这份配置是**一个文件**，没有行级锁：两个标签页同时开着，后保存的那一份会静默把前
// 一份的改动吃掉。带上「我打开时它长这样」的指纹，服务端一比就知道盘上那份已经变过
// ⇒ 409，一个字节都不写。
//
// 请求体类型不手写
// ----------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份形状，就等于在「后端改了
// 字段名」这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type OutputsResponse = OkJson<"/api/v1/outputs", "get">;
export type OutputsProfile = OutputsResponse["profiles"][number];
export type OutputsWatermark = NonNullable<OutputsResponse["watermark"]>;
/** 一层人物贴图（T6.5）。`stickers` 是**数组**（顺序 = YAML 声明顺序 = 叠放顺序）。 */
export type OutputsSticker = OutputsResponse["stickers"][number];
export type OutputsSubtitle = NonNullable<OutputsResponse["subtitle"]>;
export type OutputsLimits = OutputsResponse["limits"];
/** 一个数值字段的上下限（`exclusive_min` ⇒ 边界本身不合法）。 */
export type Bound = OutputsLimits["profile"]["width"];
export type OutputsOutcome = OkJson<"/api/v1/outputs", "post">;
export type OutputsUpdateBody = BodyJson<"/api/v1/outputs", "post">;

/** 一次拿全：profile 列表 + 水印 + 字幕 + 表单上下限（面板首屏就这一个请求）。 */
export function fetchOutputs(signal?: AbortSignal): Promise<OutputsResponse> {
  return apiGet<OutputsResponse>("/api/v1/outputs", { signal });
}

/** 保存表单（**校验不过一个字节都不写**）。 */
export function updateOutputs(
  body: OutputsUpdateBody,
  signal?: AbortSignal,
): Promise<OutputsOutcome> {
  return apiPost<OutputsOutcome>("/api/v1/outputs", body, { signal });
}
