// 合成配置面板状态（T4.7 · §04.2.8 / §04.5.9）。
//
// 这一屏要回答三个问题
// --------------------
// ① 现在这一档是什么参数？② 水印与字幕长什么样、会不会出问题？③ 改完存哪、会不会把配置写坏？
//
// 为什么状态在**文件**里、不在数据库里
// -----------------------------------
// `config/outputs.yaml` 是唯一真相：消费它的是渲染编译器（T3.x）与发布前校验（T5.1），
// 它们读的都是**文件**。面板写 DB 会让「表说 A、文件说 B」，而真正出片的是文件。所以这一屏
// 没有乐观更新：保存成功后**必须重拉** —— 不复用旧指纹，否则下一次保存必然 409。
//
// 为什么草稿是 store 的一等公民
// ----------------------------
// 表单里的东西**不是**服务端状态：它随时可能是「用户改了一半」。所以它单独放 `draft`，并且：
//   - 只有**改了**的字段才发出去（`dirtyChanges`）—— 两个标签页同时开着，整份表单回写会把
//     对方刚保存的值覆盖成「我打开时的那一份」；
//   - WS 推送 / 刷新**不覆盖**脏草稿（`draftStale` 只如实提示「底稿变了」）。少了这一条，
//     另一台机器上按一次保存就能把正在写的半屏数字抹掉。
//
// 为什么本地也判一次范围
// ----------------------
// 真判定只有一次（服务端 `OutputsConfig.model_validate`），但等一个来回才告诉用户「字号最多
// 200」太慢。所以前端拿响应里的 `limits`（**从模型现取的那一份**）做即时提示，提交时服务端
// 仍然会再判一遍 —— 两者用的是同一组数字，不会打架。
//
// 为什么监听 `logs` 通道、而不是新加一种 WS 事件
// --------------------------------------------
// 这份配置会被 CLI / 另一个标签页改掉，而 §04.4.3 的事件表是**被契约测试解析的**契约：为一次
// 配置保存新增一种事件，等于给前端加一条只在极少数时刻才来的通道。改用已有的 `logs` 通道 +
// `source === "outputs"` 过滤：保存本来就会记一行 `system_logs`，于是「谁改了这份配置」这条
// 信息一条都不少，而 WS 协议一个字节都不用动。

import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

import {
  fetchOutputs,
  updateOutputs as updateOutputsRequest,
  type Bound,
  type OutputsLimits,
  type OutputsOutcome,
  type OutputsProfile,
  type OutputsResponse,
  type OutputsSticker,
  type OutputsUpdateBody,
} from "@/api/endpoints/outputs";
import { ApiError } from "@/api/http";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import type { Envelope } from "@/ws/events";

/** 服务端 `system_logs.source`（= `outputs_service.LOG_SOURCE`，由契约测试锁死）。 */
export const OUTPUTS_LOG_SOURCE = "outputs";

/** 说话人名字的长度上限（与 `core/config.py::StickerConfig.speaker` 的 `max_length` 同源）。 */
export const SPEAKER_MAX_CHARS = 64;

type ProfilesPatch = NonNullable<OutputsUpdateBody["profiles"]>;
type ProfilePatch = ProfilesPatch[string];
type WatermarkPatch = NonNullable<OutputsUpdateBody["watermark"]>;
type StickersPatch = NonNullable<OutputsUpdateBody["stickers"]>;
type StickerPatch = StickersPatch[string];
type SubtitlePatch = NonNullable<OutputsUpdateBody["subtitle"]>;

/** 面板这一侧发出的改动载荷（`source_sha256` / `reason` 由 `save` 现补）。 */
export type OutputsChanges = Omit<OutputsUpdateBody, "source_sha256" | "reason">;

/** 一档 profile 的草稿（`quality_field` 只用于**显示** CRF / CQ，不参与提交）。 */
export interface OutputsProfileDraft {
  width: number;
  height: number;
  fps: number;
  quality: number;
  quality_field: string;
}

export interface OutputsWatermarkDraft {
  position: string;
  margin_x: number;
  margin_y: number;
  width_ratio: number;
  opacity: number;
}

export interface OutputsStickerDraft {
  enabled: boolean;
  path: string;
  /** 这一层代表稿子里的哪个说话人（空 ⇒ 这一层不换图）。 */
  speaker: string;
  /** 讲话时换成的那张图（空 ⇒ 这一层不换图）。 */
  speaking_path: string;
  position: string;
  margin_x: number;
  margin_y: number;
  height_ratio: number;
  opacity: number;
}

export interface OutputsSubtitleDraft {
  font_size: number;
  outline: number;
  /** 我想把字幕放在离底多少像素（与 `safe_area_bottom` 取大者才是实际值）。 */
  margin_bottom: number;
  /** 平台交互区有多高（抖音底部点赞/评论条 ~420px）—— 往下挪字幕要一起调小这个。 */
  safe_area_bottom: number;
  max_chars_per_line: number;
}

/** 表单草稿（一期只做表单编辑；拖拽定位与三层模板树随 C13 延后二期 —— R17）。 */
export interface OutputsDraft {
  default_profile: string;
  profiles: Record<string, OutputsProfileDraft>;
  watermark: OutputsWatermarkDraft | null;
  /** 贴图**每一层**一份草稿，键 = 槽位名（与后端 `stickers.<name>` 同名）。 */
  stickers: Record<string, OutputsStickerDraft>;
  subtitle: OutputsSubtitleDraft | null;
}

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface OutputsApi {
  fetchOutputs: typeof fetchOutputs;
  updateOutputs: typeof updateOutputsRequest;
}

let api: OutputsApi = { fetchOutputs, updateOutputs: updateOutputsRequest };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureOutputsApi(overrides: Partial<OutputsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 浮点相等：`width_ratio` / `opacity` 是小数，"看起来一样"就该算一样。 */
function sameNumber(left: number, right: number): boolean {
  return Math.abs(left - right) < 1e-9;
}

/** 服务端那一份 ⇒ 草稿（打开表单 / 保存成功后回填）。 */
export function draftFrom(response: OutputsResponse): OutputsDraft {
  const profiles: Record<string, OutputsProfileDraft> = {};
  for (const profile of response.profiles) {
    profiles[profile.name] = {
      width: profile.width,
      height: profile.height,
      fps: profile.fps,
      quality: profile.quality,
      quality_field: profile.quality_field,
    };
  }
  const stickers: Record<string, OutputsStickerDraft> = {};
  for (const sticker of response.stickers) {
    stickers[sticker.name] = {
      enabled: sticker.enabled,
      path: sticker.path,
      speaker: sticker.speaker,
      speaking_path: sticker.speaking_path ?? "",
      position: sticker.position,
      margin_x: sticker.margin_x,
      margin_y: sticker.margin_y,
      height_ratio: sticker.height_ratio,
      opacity: sticker.opacity,
    };
  }
  const watermark = response.watermark;
  const subtitle = response.subtitle;
  return {
    default_profile: response.default_profile,
    profiles,
    stickers,
    watermark:
      watermark === null
        ? null
        : {
            position: watermark.position,
            margin_x: watermark.margin_x,
            margin_y: watermark.margin_y,
            width_ratio: watermark.width_ratio,
            opacity: watermark.opacity,
          },
    subtitle:
      subtitle === null
        ? null
        : {
            font_size: subtitle.font_size,
            outline: subtitle.outline,
            margin_bottom: subtitle.margin_bottom,
            safe_area_bottom: subtitle.safe_area_bottom,
            max_chars_per_line: subtitle.max_chars_per_line,
          },
  };
}

/**
 * 草稿 vs 服务端那一份 ⇒ **只含改动**的提交载荷。
 *
 * 不这么做的话，「面板只改了字号、却把整份表单发上来」会把没动过的字段也重写一遍 ——
 * 而重写用的是**提交那一刻的旧值**，两个标签页同时开着就会互相覆盖。
 *
 * 只遍历**服务端有**的 profile：草稿里残留一个已被删掉的档，不该把它重新写回文件。
 */
export function dirtyChanges(draft: OutputsDraft, response: OutputsResponse): OutputsChanges {
  const payload: OutputsChanges = {};
  if (draft.default_profile !== response.default_profile) {
    payload.default_profile = draft.default_profile;
  }

  const profiles: ProfilesPatch = {};
  for (const profile of response.profiles) {
    const edited = draft.profiles[profile.name];
    if (edited === undefined) continue;
    const patch: ProfilePatch = {};
    if (edited.width !== profile.width) patch.width = edited.width;
    if (edited.height !== profile.height) patch.height = edited.height;
    if (edited.fps !== profile.fps) patch.fps = edited.fps;
    if (edited.quality !== profile.quality) patch.quality = edited.quality;
    if (Object.keys(patch).length > 0) profiles[profile.name] = patch;
  }
  if (Object.keys(profiles).length > 0) payload.profiles = profiles;

  const watermark = draft.watermark;
  const current = response.watermark;
  if (watermark !== null && current !== null) {
    const patch: WatermarkPatch = {};
    if (watermark.position !== current.position) patch.position = watermark.position;
    if (watermark.margin_x !== current.margin_x) patch.margin_x = watermark.margin_x;
    if (watermark.margin_y !== current.margin_y) patch.margin_y = watermark.margin_y;
    if (!sameNumber(watermark.width_ratio, current.width_ratio)) {
      patch.width_ratio = watermark.width_ratio;
    }
    if (!sameNumber(watermark.opacity, current.opacity)) patch.opacity = watermark.opacity;
    if (Object.keys(patch).length > 0) payload.watermark = patch;
  }

  const stickers: StickersPatch = {};
  for (const sticker of response.stickers) {
    const edited = draft.stickers[sticker.name];
    if (edited === undefined) continue;
    const patch: StickerPatch = {};
    if (edited.enabled !== sticker.enabled) patch.enabled = edited.enabled;
    if (edited.path !== sticker.path) patch.path = edited.path;
    if (edited.speaker !== sticker.speaker) patch.speaker = edited.speaker;
    // 空串 ⇒ `null`（= 没配）。发空串的话，后端会把它当成一条**要写进文件的改动**，
    // 而写进去的 `speaking_path: ""` 在回读时又是"没配" —— 面板会显示"已保存"，
    // 文件里却多了一行空键。转成 `null` 之后，这条改动与"清空它"是同一件事。
    if (edited.speaking_path !== (sticker.speaking_path ?? "")) {
      patch.speaking_path = edited.speaking_path.trim() === "" ? null : edited.speaking_path;
    }
    if (edited.position !== sticker.position) patch.position = edited.position;
    if (edited.margin_x !== sticker.margin_x) patch.margin_x = edited.margin_x;
    if (edited.margin_y !== sticker.margin_y) patch.margin_y = edited.margin_y;
    if (!sameNumber(edited.height_ratio, sticker.height_ratio)) {
      patch.height_ratio = edited.height_ratio;
    }
    if (!sameNumber(edited.opacity, sticker.opacity)) patch.opacity = edited.opacity;
    if (Object.keys(patch).length > 0) stickers[sticker.name] = patch;
  }
  if (Object.keys(stickers).length > 0) payload.stickers = stickers;

  const subtitle = draft.subtitle;
  const currentSubtitle = response.subtitle;
  if (subtitle !== null && currentSubtitle !== null) {
    const patch: SubtitlePatch = {};
    if (subtitle.font_size !== currentSubtitle.font_size) patch.font_size = subtitle.font_size;
    if (subtitle.outline !== currentSubtitle.outline) patch.outline = subtitle.outline;
    if (subtitle.margin_bottom !== currentSubtitle.margin_bottom) {
      patch.margin_bottom = subtitle.margin_bottom;
    }
    if (subtitle.safe_area_bottom !== currentSubtitle.safe_area_bottom) {
      patch.safe_area_bottom = subtitle.safe_area_bottom;
    }
    if (subtitle.max_chars_per_line !== currentSubtitle.max_chars_per_line) {
      patch.max_chars_per_line = subtitle.max_chars_per_line;
    }
    if (Object.keys(patch).length > 0) payload.subtitle = patch;
  }

  return payload;
}

/** 有改动吗（「保存」按钮亮不亮）。 */
export function isDirty(draft: OutputsDraft, response: OutputsResponse): boolean {
  return Object.keys(dirtyChanges(draft, response)).length > 0;
}

/** 一个数值字段的人话区间（`exclusive` ⇒ 边界本身不合法）。 */
export function describeBound(bound: Bound): string {
  if (bound.min === null && bound.max === null) return "不限";
  const low = bound.min === null ? null : `${bound.exclusive_min ? ">" : "≥"} ${bound.min}`;
  const high = bound.max === null ? null : `${bound.exclusive_max ? "<" : "≤"} ${bound.max}`;
  return [low, high].filter((part) => part !== null).join(" 且 ");
}

function checkBounds(value: number, label: string, bound: Bound): string | null {
  if (!Number.isFinite(value)) return `${label}必须是数字`;
  if (bound.min !== null) {
    if (bound.exclusive_min ? value <= bound.min : value < bound.min) {
      return `${label}必须${bound.exclusive_min ? "大于" : "不小于"} ${bound.min}`;
    }
  }
  if (bound.max !== null) {
    if (bound.exclusive_max ? value >= bound.max : value > bound.max) {
      return `${label}必须${bound.exclusive_max ? "小于" : "不大于"} ${bound.max}`;
    }
  }
  return null;
}

/**
 * 偶数约束（画布宽高按 yuv420p 色度对齐；水印边距避免 overlay 1px 偏移）。
 *
 * 这两条**不在** `limits` 里：它们是模型的 `model_validator`，没有上下限可下发。判错也
 * 只是「提交后 422」，不是「静默写坏」—— 所以这里补一次即时提示是安全的。
 */
function checkEven(value: number, label: string): string | null {
  return value % 2 === 0 ? null : `${label}必须是偶数`;
}

/**
 * 提交前的即时提示（**不是**权威判定：权威在服务端的 `OutputsConfig`）。
 *
 * 返回空对象 ⇒ 可以提交。字段名与后端 `context.field_errors[].field` **同名**，于是「本地
 * 提示」和「服务端打回」画在同一处红字上。跨字段那条（默认档必须在 profile 列表里）用
 * `"<root>"`：后端没有 `loc` 的错误也落在那里，同一个错不该一会儿在顶部、一会儿在下拉框旁。
 */
export function localErrors(draft: OutputsDraft, limits: OutputsLimits): Record<string, string> {
  const errors: Record<string, string> = {};

  if (draft.profiles[draft.default_profile] === undefined) {
    errors["<root>"] = `默认档 ${draft.default_profile} 不在 profile 列表里`;
  }

  for (const name of Object.keys(draft.profiles)) {
    const profile = draft.profiles[name];
    const at = (field: string): string => `profiles.${name}.${field}`;

    const width = checkBounds(profile.width, "宽度", limits.profile.width) ??
      checkEven(profile.width, "宽度");
    if (width !== null) errors[at("width")] = width;
    const height = checkBounds(profile.height, "高度", limits.profile.height) ??
      checkEven(profile.height, "高度");
    if (height !== null) errors[at("height")] = height;

    const fps = checkBounds(profile.fps, "帧率", limits.profile.fps);
    if (fps !== null) errors[at("fps")] = fps;

    const qualityBound = profile.quality_field === "cq" ? limits.profile.cq : limits.profile.crf;
    const quality = checkBounds(profile.quality, qualityLabel(profile), qualityBound);
    if (quality !== null) errors[at("quality")] = quality;
  }

  const watermark = draft.watermark;
  if (watermark !== null) {
    if (!limits.watermark.positions.includes(watermark.position)) {
      errors["watermark.position"] = `位置只能是：${limits.watermark.positions.join(" / ")}`;
    }
    const marginX = checkBounds(watermark.margin_x, "水平边距", limits.watermark.margin_x) ??
      checkEven(watermark.margin_x, "水平边距");
    if (marginX !== null) errors["watermark.margin_x"] = marginX;
    const marginY = checkBounds(watermark.margin_y, "垂直边距", limits.watermark.margin_y) ??
      checkEven(watermark.margin_y, "垂直边距");
    if (marginY !== null) errors["watermark.margin_y"] = marginY;
    const ratio = checkBounds(watermark.width_ratio, "水印宽度占比", limits.watermark.width_ratio);
    if (ratio !== null) errors["watermark.width_ratio"] = ratio;
    const opacity = checkBounds(watermark.opacity, "不透明度", limits.watermark.opacity);
    if (opacity !== null) errors["watermark.opacity"] = opacity;
  }

  for (const [name, sticker] of Object.entries(draft.stickers)) {
    const at = (field: string): string => `stickers.${name}.${field}`;
    if (!limits.sticker.positions.includes(sticker.position)) {
      errors[at("position")] = `位置只能是：${limits.sticker.positions.join(" / ")}`;
    }
    if (sticker.path.trim() === "") errors[at("path")] = "图片路径不能为空";
    // 说话人是稿子里的**角色名**（`script_sentences.speaker`），后端限长 64。
    // 这里挡一道是为了"打了一半的名字"能在本地就红出来 —— 交给后端的话，报回来的是
    // 一条 422，而人在输入框上看到的是一句与这格无关的提示。
    if (sticker.speaker.length > SPEAKER_MAX_CHARS) {
      errors[at("speaker")] = `说话人名字最长 ${SPEAKER_MAX_CHARS} 个字符`;
    }
    const marginX = checkBounds(sticker.margin_x, "水平边距", limits.sticker.margin_x) ??
      checkEven(sticker.margin_x, "水平边距");
    if (marginX !== null) errors[at("margin_x")] = marginX;
    const marginY = checkBounds(sticker.margin_y, "垂直边距", limits.sticker.margin_y) ??
      checkEven(sticker.margin_y, "垂直边距");
    if (marginY !== null) errors[at("margin_y")] = marginY;
    const ratio = checkBounds(sticker.height_ratio, "高度占比", limits.sticker.height_ratio);
    if (ratio !== null) errors[at("height_ratio")] = ratio;
    const opacity = checkBounds(sticker.opacity, "不透明度", limits.sticker.opacity);
    if (opacity !== null) errors[at("opacity")] = opacity;
  }

  const subtitle = draft.subtitle;
  if (subtitle !== null) {
    const size = checkBounds(subtitle.font_size, "字号", limits.subtitle.font_size);
    if (size !== null) errors["subtitle.font_size"] = size;
    const outline = checkBounds(subtitle.outline, "描边", limits.subtitle.outline);
    if (outline !== null) errors["subtitle.outline"] = outline;
    const margin = checkBounds(subtitle.margin_bottom, "距底", limits.subtitle.margin_bottom);
    if (margin !== null) errors["subtitle.margin_bottom"] = margin;
    const safeBottom = checkBounds(
      subtitle.safe_area_bottom,
      "底部安全区",
      limits.subtitle.safe_area_bottom,
    );
    if (safeBottom !== null) errors["subtitle.safe_area_bottom"] = safeBottom;
    const chars = checkBounds(
      subtitle.max_chars_per_line,
      "每行字数",
      limits.subtitle.max_chars_per_line,
    );
    if (chars !== null) errors["subtitle.max_chars_per_line"] = chars;
  }

  return errors;
}

/**
 * 服务端 422 的 `context.field_errors` ⇒ `{字段: 原因}`（能标到输入框上）。
 *
 * 拿不到就返回空对象 —— 调用方会退回显示整条 `message`（至少不丢信息）。
 */
export function fieldErrors(reason: unknown): Record<string, string> {
  if (!(reason instanceof ApiError)) return {};
  const detail = reason.detail;
  if (detail === null || typeof detail !== "object" || !("context" in detail)) return {};
  const context = (detail as { context: unknown }).context;
  if (context === null || typeof context !== "object" || !("field_errors" in context)) return {};
  const raw = (context as { field_errors: unknown }).field_errors;
  if (!Array.isArray(raw)) return {};
  const result: Record<string, string> = {};
  for (const item of raw) {
    if (item !== null && typeof item === "object" && "field" in item && "error" in item) {
      const field = String((item as { field: unknown }).field);
      result[field] = String((item as { error: unknown }).error);
    }
  }
  return result;
}

/** 质量参数在面板上的名字：这一档用的是 libx264（CRF）还是 NVENC（CQ）。 */
export function qualityLabel(profile: { quality_field: string }): string {
  return profile.quality_field === "cq" ? "CQ" : "CRF";
}

/**
 * 水印那一行的灯。判据是 ``usable``（渲染**真的会贴上**），不是 ``exists``。
 *
 * 水印没有 `enabled` 开关（有就贴），所以"贴不上"永远是黄灯而**不是**红灯：
 * 它只是缺一层装饰，出片照常（与贴图那一行不同 —— 那边开着却贴不上是红灯，
 * 因为人**明确表达了**"我要它"）。
 */
export function watermarkTone(usable: boolean): StatusTone {
  return usable ? "ok" : "warn";
}

/** 水印那一行的人话（`width_ratio` 换算成像素宽比小数直观得多）。 */
export function describeWatermark(profile: { width_ratio: number; width_px: number }): string {
  const percent = Math.round(profile.width_ratio * 1000) / 10;
  return `占画布宽 ${percent}%（约 ${profile.width_px} px）`;
}

/** 贴图那一行的人话（按**高度**算 —— 人物是竖长的，宽高比由素材决定）。 */
export function describeSticker(sticker: { height_ratio: number; height_px: number }): string {
  const percent = Math.round(sticker.height_ratio * 1000) / 10;
  return `占画布高 ${percent}%（约 ${sticker.height_px} px 高，宽度按图的比例）`;
}

/**
 * 贴图那一行的灯。判据是 ``usable``（渲染**真的会贴上**），不是 ``exists``（盘上有文件）。
 *
 * ★ 这一格曾经看 ``exists``，于是"扩展名叫 .png 的 WebP / 没 alpha 的 PNG"会点亮绿灯
 * —— 而渲染路径看的是 ``probe_png().usable``，它会把那一层跳过。面板与渲染报的必须
 * 是**同一件事**，否则那句"我开了它、为什么片子上没有"就永远没人回答。
 *
 * `idle` 与 `warn` 必须分开：关掉一层是**故意的**（默认就是关的），而"开着但贴不上"
 * 是**待办**。两者都画成黄色的话，默认状态会显得像出了问题。
 */
export function stickerTone(enabled: boolean, usable: boolean): StatusTone {
  if (!enabled) return "idle";
  return usable ? "ok" : "error";
}

/**
 * 「讲话时换图」那一行的灯（T6.5 追加）。
 *
 * 判据是后端的 `speaking_problem`：`null` = 配置齐了、讲话图也能用。
 *
 * ★ 这盏灯**只说明"配齐了"**，不说明"出片时一定会换"。谁在哪一句讲话要看稿子与
 * 时间轴，而这一屏手上**没有**这两样 —— 所以它判不了"这个人这条片子里有没有讲话
 * 区间"。在面板上编一个"应该有词吧"的结论，就会变成面板与成片各说各话。
 * 真的换没换、按哪份时间换的，写在成片的 `manifest.json` 的
 * `stickers[].speaking` 里（`swapped` / `source` / `note`）。
 */
export function stickerSpeakingTone(
  enabled: boolean,
  usable: boolean,
  speakingProblem: string | null,
): StatusTone {
  if (!enabled) return "idle";
  if (!usable) return "idle";
  return speakingProblem === null ? "ok" : "warn";
}

/** 一次写动作的结论（`changed=false` 也要说清楚「本来就是这样」）。 */
export function describeOutcome(outcome: OutputsOutcome): string {
  return outcome.note;
}

/** `logs` 通道上是不是「这份配置被改过」（这条通道还有全站日志，不能一律重拉）。 */
export function isOutputsLog(envelope: Envelope): boolean {
  const data = envelope.data;
  return data["kind"] === "log.appended" && data["source"] === OUTPUTS_LOG_SOURCE;
}

/** 409 ⇒ 盘上那份已经被别人改过（**本次一个字节都没写**）。 */
export function isStaleConflict(failure: unknown): boolean {
  return failure instanceof ApiError && failure.status === 409;
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useOutputsStore = defineStore("outputs", () => {
  const { status: wsStatus, cursor: wsCursor, connect, resync } = useWsConnection();

  const snapshot = ref<OutputsResponse | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);

  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);
  /** 盘上那份被别处改过（409）—— 必须重拉一次才能继续编辑。 */
  const conflict = ref(false);

  const draft = ref<OutputsDraft | null>(null);
  const serverFieldErrors = ref<Record<string, string>>({});
  const draftStale = ref(false);

  // ── 派生 ───────────────────────────────────────────────────────────

  const profiles = computed<OutputsProfile[]>(() => snapshot.value?.profiles ?? []);
  const watermark = computed(() => snapshot.value?.watermark ?? null);
  /** 贴图**每一层**（顺序 = YAML 声明顺序 = 叠放顺序）。空数组 ⇒ 配置里没有 stickers 段。 */
  const stickers = computed<OutputsSticker[]>(() => snapshot.value?.stickers ?? []);
  const subtitle = computed(() => snapshot.value?.subtitle ?? null);
  const limits = computed<OutputsLimits | null>(() => snapshot.value?.limits ?? null);
  const stale = computed<boolean>(() => snapshot.value?.stale ?? false);
  const configError = computed<string | null>(() => snapshot.value?.error ?? null);
  const configPath = computed<string>(() => snapshot.value?.path ?? "");
  const version = computed<number>(() => snapshot.value?.version ?? 0);
  const sha256 = computed<string>(() => snapshot.value?.sha256 ?? "");
  const loadedAt = computed<string>(() => snapshot.value?.loaded_at ?? "");
  const defaultProfile = computed<string>(() => snapshot.value?.default_profile ?? "");
  /** 水印 PNG 不在盘上 ⇒ 这次出片不带水印（面板要说出来，免得人以为是 bug）。 */
  /**
   * **这次出片不贴水印**（文件不在 / 不是 PNG / 没有透明通道）。
   *
   * 判据是 `usable`（后端与渲染路径同一份 `probe_png` 结论），不是 `exists`：
   * 一个扩展名叫 `.png` 的 WebP 在盘上"存在"，渲染照样跳过它。面板只报前者时，
   * 人看到的是一句"在盘上"和一张没有水印的片子。
   */
  const watermarkBroken = computed<boolean>(
    () => watermark.value !== null && !watermark.value.usable,
  );

  /**
   * **开着、但渲染贴不上**的贴图层（每一层各自一条原因）。
   *
   * 与 `watermarkBroken` 分开报：水印只有一层，"贴不贴得上"就够；贴图是若干层，
   * 人问的是"**哪一层**没贴上、为什么"。这一列是"点了开关没反应"的唯一答案来源 ——
   * 以前它只比 `exists`，于是坏图（WebP 冒充 PNG / 没 alpha）在面板上是绿的、
   * 在渲染里是跳过的，两边都不说话。
   */
  const stickersBroken = computed(() =>
    stickers.value.filter((sticker) => sticker.enabled && !sticker.usable),
  );

  /**
   * **开着、图也贴得上，但这一层的"换图"配不齐**（T6.5 追加）。
   *
   * 判据是后端的 `speaking_problem`（配置齐 + 讲话图可用 ⇒ `null`）。它与
   * `stickersBroken` 是**两件事**：那些层根本贴不上，这些层贴得上、只是不会换图。
   *
   * ★ 这里**不含**"这个人这条片子里没有讲话区间"那一条 —— 面板判不了它（要读稿子与
   * 时间轴）。所以这一列是"你配漏了什么"，不是"出片时一定会换"。
   */
  const stickersNotSwapping = computed(() =>
    stickers.value.filter(
      (sticker) => sticker.enabled && sticker.usable && sticker.speaking_problem !== null,
    ),
  );

  const localFieldErrors = computed<Record<string, string>>(() => {
    if (draft.value === null || limits.value === null) return {};
    return localErrors(draft.value, limits.value);
  });

  const visibleFieldErrors = computed<Record<string, string>>(() => ({
    ...localFieldErrors.value,
    ...serverFieldErrors.value,
  }));

  const dirty = computed<boolean>(() => {
    if (draft.value === null || snapshot.value === null) return false;
    return isDirty(draft.value, snapshot.value);
  });

  /** 能不能提交（有改动 + 本地没意见 + 没有在途动作）。 */
  const canSave = computed<boolean>(
    () => dirty.value && Object.keys(localFieldErrors.value).length === 0 && !busy.value,
  );

  const wsTone = computed<StatusTone>(() => {
    switch (wsStatus.value) {
      case "open":
        return "ok";
      case "connecting":
        return "busy";
      case "reconnecting":
        return "warn";
      case "stopped":
        return "error";
      default:
        return "idle";
    }
  });

  // ── 读 ──────────────────────────────────────────────────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
    serverFieldErrors.value = {};
    conflict.value = false;
  }

  /**
   * 拉一次全量；`resetDraft` 决定要不要用服务端那一份覆盖草稿。
   *
   * **默认不覆盖真的改过的草稿**（与人物库同一条裁定 162）：一条「配置被改过」的推送就能把
   * 用户正在写的半屏数字抹掉。此时只立一个 `draftStale` 旗子如实提示「底稿变了」，决定权留给人。
   *
   * 「真的改过」必须拿**旧**快照比：赋值之后再算，任何「底稿 == 旧服务端状态」都会被误判成
   * 脏草稿 —— 用户一个键都没敲，却被扣上一顶「你手上有改动」的帽子，还看不到别处的改动。
   */
  async function refresh(options: { resetDraft?: boolean } = {}): Promise<void> {
    loading.value = true;
    try {
      const next = await api.fetchOutputs();
      const edited =
        draft.value !== null && snapshot.value !== null && isDirty(draft.value, snapshot.value);
      snapshot.value = next;
      loadError.value = null;
      conflict.value = false;
      // 磁盘那份读不出来 ⇒ **没有可编辑的底稿**（与人物库同一条：`active === null` 时
      // `draft` 也是 `null`）。给一份"空表单"比给 `null` 更糟：用户会以为配置本来就是空的。
      if (next.stale) {
        draft.value = null;
        draftStale.value = false;
        serverFieldErrors.value = {};
      } else if (options.resetDraft === true || draft.value === null || !edited) {
        draft.value = draftFrom(next);
        draftStale.value = false;
        serverFieldErrors.value = {};
      } else {
        draftStale.value = true;
      }
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与总览台同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  /** 放弃编辑，回到服务端那一份（并顺带把「底稿变了」的旗子放下来）。 */
  function resetDraft(): void {
    draft.value =
      snapshot.value === null || snapshot.value.stale ? null : draftFrom(snapshot.value);
    draftStale.value = false;
    serverFieldErrors.value = {};
  }

  // ── WS ──────────────────────────────────────────────────────────────

  let coalescer: ReturnType<typeof setTimeout> | null = null;

  function onLogs(envelope: Envelope): void {
    if (!isOutputsLog(envelope)) return;
    if (coalescer !== null) return;
    coalescer = setTimeout(() => {
      coalescer = null;
      void refresh();
    }, 300);
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

  // ── 动作 ────────────────────────────────────────────────────────────

  /**
   * 保存表单（只发改动；校验不过一个字节都不会写）。
   *
   * 请求体带 `source_sha256` = **我打开时的那一份指纹**。盘上已经变过 ⇒ 409，本次一个字节
   * 都不写（把别人的改动吃掉比报错贵得多）。
   */
  async function save(reason: string | null = null): Promise<boolean> {
    if (draft.value === null || snapshot.value === null) return false;
    const changes = dirtyChanges(draft.value, snapshot.value);
    if (Object.keys(changes).length === 0) {
      notice.value = "没有改动，不需要保存";
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.updateOutputs({
        ...changes,
        source_sha256: snapshot.value.sha256,
        reason,
      });
      notice.value = describeOutcome(outcome);
      // 保存后**强制回填**：这一份的 `sha256` 已经变了，草稿的基线必须跟着换，否则下一次保存
      // 一定撞 409。代价是"在途期间新敲的那几笔"会被回填掉 —— 与人物库同一取舍，换来的是
      // 「保存完还能接着编辑」这件事不会因为一个旧指纹而失效。
      await refresh({ resetDraft: true });
      return true;
    } catch (failure) {
      conflict.value = isStaleConflict(failure);
      const fields = fieldErrors(failure);
      serverFieldErrors.value = fields;
      error.value =
        Object.keys(fields).length > 0
          ? `${describeError(failure)}（${Object.entries(fields)
              .map(([field, why]) => `${field}：${why}`)
              .join("；")}）`
          : describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * 一个**开关**被拨动之后立刻写盘（贴图的 `enabled`）。
   *
   * 为什么开关不走「先改草稿、再往上找保存按钮」那一套：勾选框长得就是**开关** ——
   * 人勾完就去渲染了，而盘上那份还是 `false`。下一次进面板（或刷新）草稿按盘上重建，
   * 勾就"自己跳回去了"；渲染当然也不生效。这不是"保存没生效"，是**根本没保存**，
   * 而面板当时只在另一张卡片里写了一行"有未保存的改动"。
   *
   * 三件事保证它不会比手点保存更危险：
   * - 走的还是 `save()`（sha 守卫、审计、回读全都在），不新增写路径；
   * - 提交的是 `dirtyChanges`（**全部**改动，不只这一格）⇒ 顺手改的占比不会被丢掉；
   * - 本地校验有意见（比如路径空着）时**不提交**，改动留在草稿里、那行红字照旧 ——
   *   面板不该拿一个它自己都判不过的表单去撞后端。
   */
  async function saveToggle(reason: string): Promise<boolean> {
    if (Object.keys(localFieldErrors.value).length > 0) return false;
    return save(reason);
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次订阅、一份合并窗口。 */
  function start(): void {
    connect();
    wire();
    void refresh();
  }

  function stop(): void {
    if (coalescer !== null) {
      clearTimeout(coalescer);
      coalescer = null;
    }
  }

  return {
    // 读
    snapshot,
    profiles,
    watermark,
    stickers,
    subtitle,
    limits,
    stale,
    configError,
    configPath,
    version,
    sha256,
    loadedAt,
    defaultProfile,
    watermarkBroken,
    stickersBroken,
    stickersNotSwapping,
    loading,
    loadError,
    // 表单
    draft,
    dirty,
    canSave,
    draftStale,
    conflict,
    localFieldErrors,
    visibleFieldErrors,
    resetDraft,
    // 动作
    busy,
    error,
    notice,
    save,
    saveToggle,
    // 通道
    wsStatus,
    wsCursor,
    wsTone,
    refresh,
    start,
    stop,
    resync,
  };
});
