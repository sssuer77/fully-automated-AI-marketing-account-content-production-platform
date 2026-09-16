import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Bound,
  OutputsLimits,
  OutputsOutcome,
  OutputsProfile,
  OutputsResponse,
  OutputsSubtitle,
  OutputsUpdateBody,
  OutputsWatermark,
} from "@/api/endpoints/outputs";
import { ApiError } from "@/api/http";
import type { Envelope } from "@/ws/events";
import {
  configureOutputsApi,
  describeBound,
  describeWatermark,
  dirtyChanges,
  draftFrom,
  fieldErrors,
  isDirty,
  isOutputsLog,
  isStaleConflict,
  localErrors,
  qualityLabel,
  useOutputsStore,
  watermarkTone,
} from "./outputs";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function profile(overrides: Partial<OutputsProfile> = {}): OutputsProfile {
  return {
    name: "douyin_1080x1920_30fps_v1",
    width: 1080,
    height: 1920,
    fps: 30,
    vcodec: "libx264",
    quality_field: "crf",
    quality: 21,
    platforms: ["douyin", "kuaishou", "shipinhao"],
    is_default: true,
    ...overrides,
  };
}

function watermark(overrides: Partial<OutputsWatermark> = {}): OutputsWatermark {
  return {
    path: "templates/douyin_9x16_default/assets/images/watermark.png",
    position: "bottom_right",
    margin_x: 48,
    margin_y: 96,
    width_ratio: 0.22,
    width_px: 238,
    opacity: 0.85,
    exists: true,
    ...overrides,
  };
}

function subtitle(overrides: Partial<OutputsSubtitle> = {}): OutputsSubtitle {
  return {
    enabled: true,
    font_name: "Source Han Sans SC",
    font_size: 64,
    outline: 4,
    shadow: 2,
    margin_bottom: 260,
    max_chars_per_line: 16,
    max_lines: 2,
    ...overrides,
  };
}

function bounds(
  min: number | null,
  max: number | null,
  exclusiveMin = false,
  exclusiveMax = false,
): Bound {
  return { min, max, exclusive_min: exclusiveMin, exclusive_max: exclusiveMax };
}

function limits(overrides: Partial<OutputsLimits> = {}): OutputsLimits {
  return {
    scalar_fields: ["default_profile"],
    profile_fields: ["width", "height", "fps", "quality"],
    watermark_fields: ["position", "margin_x", "margin_y", "width_ratio", "opacity"],
    subtitle_fields: ["font_size", "outline", "max_chars_per_line"],
    default_profile: bounds(null, null),
    profile: {
      width: bounds(64, 7680),
      height: bounds(64, 7680),
      fps: bounds(1, 120),
      crf: bounds(0, 51),
      cq: bounds(0, 51),
    },
    watermark: {
      positions: ["top_left", "top_right", "bottom_left", "bottom_right", "center"],
      margin_x: bounds(0, 2000),
      margin_y: bounds(0, 2000),
      width_ratio: bounds(0, 0.25, true, false),
      opacity: bounds(0, 1),
    },
    subtitle: {
      font_size: bounds(16, 200),
      outline: bounds(0, 20),
      max_chars_per_line: bounds(4, 60),
    },
    ...overrides,
  };
}

function response(overrides: Partial<OutputsResponse> = {}): OutputsResponse {
  return {
    generated_at: "2026-09-14T04:00:01.000Z",
    version: 1,
    sha256: "a".repeat(64),
    loaded_at: "2026-09-14T04:00:00.000Z",
    path: "D:/studio/config/outputs.yaml",
    stale: false,
    error: null,
    default_profile: "douyin_1080x1920_30fps_v1",
    profiles: [
      profile(),
      profile({
        name: "fallback_720x1280_v1",
        width: 720,
        height: 1280,
        vcodec: "h264_nvenc",
        quality_field: "cq",
        quality: 26,
        platforms: [],
        is_default: false,
      }),
    ],
    watermark: watermark(),
    subtitle: subtitle(),
    limits: limits(),
    ...overrides,
  };
}

function outcome(overrides: Partial<OutputsOutcome> = {}): OutputsOutcome {
  return {
    action: "outputs.update",
    changed: true,
    version: 2,
    sha256: "d".repeat(64),
    path: "D:/studio/config/outputs.yaml",
    fields: ["subtitle.font_size"],
    note: "已保存合成配置（1 项：subtitle.font_size）；**只影响后续渲染**",
    ...overrides,
  };
}

/** 一个带 `field_errors` 的 422（形状与 `app/errors.py` 的 `StudioError.to_dict` 一致）。 */
function invalidForm(field: string, why: string): ApiError {
  return new ApiError("合成配置校验不通过（1 处问题）", 422, {
    code: "OUTPUTS_INVALID",
    message: "合成配置校验不通过（1 处问题）",
    context: { field_errors: [{ field, error: why }] },
    remediation: "按 field_errors 逐条修正后重新提交；**本次没有写入任何内容**",
    type: "ConfigError",
  });
}

/** 盘上那份被别人改过（`OUTPUTS_STALE`）。 */
function staleConflict(): ApiError {
  return new ApiError("合成配置已被别处修改", 409, {
    code: "OUTPUTS_STALE",
    message: "合成配置已被别处修改（source_sha256 不匹配）",
    context: { expected: "a".repeat(64), actual: "e".repeat(64) },
    remediation: "重新载入后再提交",
    type: "ConfigError",
  });
}

function envelope(data: Record<string, unknown>): Envelope {
  return {
    v: 1,
    type: "event",
    channel: "logs",
    seq: 1,
    ts: "2026-09-14T04:00:01.000Z",
    data,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureOutputsApi({
    fetchOutputs: vi.fn(async () => response()),
    updateOutputs: vi.fn(async () => outcome()),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("草稿是**拷贝**：改草稿不会碰到服务端那一份", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.profiles["douyin_1080x1920_30fps_v1"].width = 720;
    draft.watermark!.opacity = 0.5;
    draft.subtitle!.font_size = 16;
    expect(source.profiles[0].width).toBe(1080);
    expect(source.watermark?.opacity).toBe(0.85);
    expect(source.subtitle?.font_size).toBe(64);
  });

  it("dirtyChanges：只发改动过的字段（两个标签页同时开着不该互相覆盖）", () => {
    const source = response();
    const draft = draftFrom(source);
    expect(dirtyChanges(draft, source)).toEqual({});

    draft.subtitle!.font_size = 72;
    expect(dirtyChanges(draft, source)).toEqual({ subtitle: { font_size: 72 } });
    expect(isDirty(draft, source)).toBe(true);
  });

  it("dirtyChanges：小数按**值**比（0.22 与 0.2200000001 是同一个数）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.watermark!.width_ratio = 0.22 + 1e-12;
    expect(dirtyChanges(draft, source)).toEqual({});
  });

  it("dirtyChanges：`quality_field` 只用于显示，**不**出现在提交载荷里", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.profiles["fallback_720x1280_v1"].quality = 30;
    const payload = dirtyChanges(draft, source);
    expect(payload.profiles).toEqual({ fallback_720x1280_v1: { quality: 30 } });
  });

  it("dirtyChanges：草稿里残留一个已被删掉的档 ⇒ 不把它写回文件", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.profiles["gone_v1"] = {
      width: 640,
      height: 480,
      fps: 24,
      quality: 30,
      quality_field: "crf",
    };
    expect(dirtyChanges(draft, source)).toEqual({});
  });

  it("dirtyChanges：默认档改名 ⇒ 只有这一个字段", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.default_profile = "fallback_720x1280_v1";
    expect(dirtyChanges(draft, source)).toEqual({ default_profile: "fallback_720x1280_v1" });
  });

  it("localErrors：宽高必须偶数（yuv420p 色度对齐）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.profiles["douyin_1080x1920_30fps_v1"].height = 1921;
    expect(localErrors(draft, source.limits)["profiles.douyin_1080x1920_30fps_v1.height"]).toBe(
      "高度必须是偶数",
    );
  });

  it("localErrors：超范围按上下限说人话", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.subtitle!.font_size = 400;
    draft.profiles["douyin_1080x1920_30fps_v1"].fps = 240;
    const errors = localErrors(draft, source.limits);
    expect(errors["subtitle.font_size"]).toBe("字号必须不大于 200");
    expect(errors["profiles.douyin_1080x1920_30fps_v1.fps"]).toBe("帧率必须不大于 120");
  });

  it("localErrors：`width_ratio` 的 0 是**排他**下界（0 一定 422）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.watermark!.width_ratio = 0;
    expect(localErrors(draft, source.limits)["watermark.width_ratio"]).toBe(
      "水印宽度占比必须大于 0",
    );
  });

  it("localErrors：边距必须是偶数（奇数边距会让 overlay 落半像素）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.watermark!.margin_y = 97;
    expect(localErrors(draft, source.limits)["watermark.margin_y"]).toBe("垂直边距必须是偶数");
  });

  it("localErrors：位置必须在枚举里（下拉框的值来自服务端）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.watermark!.position = "middle";
    expect(localErrors(draft, source.limits)["watermark.position"]).toContain("bottom_right");
  });

  it("localErrors：默认档不在 profile 列表里 ⇒ 落 `\u003croot\u003e`（与后端同一条红字位置）", () => {
    const source = response();
    const draft = draftFrom(source);
    draft.default_profile = "nope_v1";
    expect(localErrors(draft, source.limits)["\u003croot\u003e"]).toContain("nope_v1");
  });

  it("fieldErrors：从 422 的 context.field_errors 里取（能标到输入框上）", () => {
    expect(fieldErrors(invalidForm("subtitle.font_size", "Input should be <= 200"))).toEqual({
      "subtitle.font_size": "Input should be <= 200",
    });
  });

  it("fieldErrors：拿不到就返回空对象（调用方退回显示整条 message，不丢信息）", () => {
    expect(fieldErrors(new Error("boom"))).toEqual({});
    expect(fieldErrors(new ApiError("x", 500, { code: "X" }))).toEqual({});
  });

  it("qualityLabel：标签跟着这一档的编码器走（libx264 ⇒ CRF / nvenc ⇒ CQ）", () => {
    expect(qualityLabel({ quality_field: "crf" })).toBe("CRF");
    expect(qualityLabel({ quality_field: "cq" })).toBe("CQ");
  });

  it("describeWatermark：占比换算成人看得懂的百分比与像素宽", () => {
    expect(describeWatermark({ width_ratio: 0.22, width_px: 238 })).toBe("占画布宽 22%（约 238 px）");
  });

  it("describeBound：排他边界要能看出来（`> 0` 不是 `≥ 0`）", () => {
    expect(describeBound(bounds(0, 0.25, true, false))).toBe("> 0 且 ≤ 0.25");
    expect(describeBound(bounds(null, null))).toBe("不限");
  });

  it("watermarkTone：PNG 不在盘上是**黄灯**不是红灯（不贴水印也照样出片）", () => {
    expect(watermarkTone(true)).toBe("ok");
    expect(watermarkTone(false)).toBe("warn");
  });

  it("只有 source === outputs 的日志才触发重拉（这条通道还有全站日志）", () => {
    expect(isOutputsLog(envelope({ kind: "log.appended", source: "outputs" }))).toBe(true);
    expect(isOutputsLog(envelope({ kind: "log.appended", source: "render" }))).toBe(false);
    expect(isOutputsLog(envelope({ kind: "system.alert", source: "outputs" }))).toBe(false);
  });

  it("isStaleConflict：只认 409（422 是入参问题，不是并发问题）", () => {
    expect(isStaleConflict(staleConflict())).toBe(true);
    expect(isStaleConflict(invalidForm("subtitle.font_size", "x"))).toBe(false);
    expect(isStaleConflict(new Error("boom"))).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("useOutputsStore", () => {
  it("refresh 成功 ⇒ profile / 水印 / 字幕 / 上下限就位，草稿跟着回填", async () => {
    const store = useOutputsStore();
    await store.refresh();
    expect(store.loadError).toBeNull();
    expect(store.profiles).toHaveLength(2);
    expect(store.defaultProfile).toBe("douyin_1080x1920_30fps_v1");
    expect(store.watermark?.width_px).toBe(238);
    expect(store.subtitle?.max_chars_per_line).toBe(16);
    expect(store.limits?.watermark.positions).toContain("bottom_right");
    expect(store.draft?.subtitle?.font_size).toBe(64);
    expect(store.dirty).toBe(false);
    expect(store.canSave).toBe(false);
    expect(store.draftStale).toBe(false);
  });

  it("配置坏了（stale）⇒ 上下限照常下发，面板仍能画出表单骨架", async () => {
    configureOutputsApi({
      fetchOutputs: vi.fn(async () =>
        response({
          stale: true,
          error: "配置文件语法错误：line 12",
          profiles: [],
          watermark: null,
          subtitle: null,
        }),
      ),
    });
    const store = useOutputsStore();
    await store.refresh();
    expect(store.stale).toBe(true);
    expect(store.configError).toContain("line 12");
    expect(store.profiles).toEqual([]);
    expect(store.draft).toBeNull();
    expect(store.limits?.profile.width.max).toBe(7680);
  });

  it("refresh 失败 ⇒ loadError 就位，但**旧快照留着**（一屏数字不因一次抖动变空白）", async () => {
    const store = useOutputsStore();
    await store.refresh();
    configureOutputsApi({
      fetchOutputs: vi.fn(async () => {
        throw new ApiError("网络断了", 0, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toContain("网络断了");
    expect(store.profiles).toHaveLength(2);
  });

  it("★ 刷新**不覆盖**脏草稿：只立一个 draftStale 旗子", async () => {
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 72;
    await store.refresh();
    expect(store.draft?.subtitle?.font_size).toBe(72);
    expect(store.draftStale).toBe(true);
  });

  it("草稿不脏 ⇒ 刷新用服务端那一份覆盖（并清掉 draftStale）", async () => {
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 72;
    await store.refresh();
    configureOutputsApi({
      fetchOutputs: vi.fn(async () => response({ subtitle: subtitle({ font_size: 80 }) })),
    });
    store.resetDraft();
    expect(store.draftStale).toBe(false);
    await store.refresh();
    expect(store.draft?.subtitle?.font_size).toBe(80);
  });

  it("save：请求体只有改动过的字段 + 打开时那一份的 source_sha256", async () => {
    const update = vi.fn(async (_body: OutputsUpdateBody) => outcome());
    configureOutputsApi({ updateOutputs: update });
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 72;
    store.draft!.watermark!.opacity = 0.7;
    expect(await store.save()).toBe(true);
    expect(update).toHaveBeenCalledTimes(1);
    const body = update.mock.calls[0][0];
    expect(body.source_sha256).toBe("a".repeat(64));
    expect(body.subtitle).toEqual({ font_size: 72 });
    expect(body.watermark).toEqual({ opacity: 0.7 });
    expect(body.profiles).toBeUndefined();
    expect(body.default_profile).toBeUndefined();
    expect(store.notice).toContain("只影响后续渲染");
  });

  it("save：没有改动 ⇒ 不发请求（如实说「没改动」，不假装保存了一次）", async () => {
    const update = vi.fn(async (_body: OutputsUpdateBody) => outcome());
    configureOutputsApi({ updateOutputs: update });
    const store = useOutputsStore();
    await store.refresh();
    expect(await store.save()).toBe(false);
    expect(update).not.toHaveBeenCalled();
    expect(store.notice).toBe("没有改动，不需要保存");
  });

  it("★ save 撞 422：逐字段红字上桌，error 里带原因（不是一句干巴巴的「失败」）", async () => {
    configureOutputsApi({
      updateOutputs: vi.fn(async () => {
        throw invalidForm("subtitle.font_size", "Input should be <= 200");
      }),
    });
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 9999;
    expect(await store.save()).toBe(false);
    expect(store.visibleFieldErrors["subtitle.font_size"]).toBe("Input should be <= 200");
    expect(store.error).toContain("Input should be <= 200");
    expect(store.conflict).toBe(false);
  });

  it("★ save 撞 409：conflict 上桌（本次一个字节都没写，必须重新载入）", async () => {
    configureOutputsApi({
      updateOutputs: vi.fn(async () => {
        throw staleConflict();
      }),
    });
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 72;
    expect(await store.save()).toBe(false);
    expect(store.conflict).toBe(true);
    expect(store.error).toContain("别处修改");
  });

  it("本地就能看出来的问题 ⇒ canSave 直接是 false（少一次来回）", async () => {
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 400;
    expect(store.dirty).toBe(true);
    expect(store.canSave).toBe(false);
    expect(store.localFieldErrors["subtitle.font_size"]).toBe("字号必须不大于 200");
  });

  it("保存成功 ⇒ 服务端红字被清掉（上一轮的错误不该跟着下一轮）", async () => {
    configureOutputsApi({
      updateOutputs: vi.fn(async (_body: OutputsUpdateBody) => {
        throw invalidForm("subtitle.font_size", "Input should be <= 200");
      }),
    });
    const store = useOutputsStore();
    await store.refresh();
    store.draft!.subtitle!.font_size = 9999;
    await store.save();
    expect(store.visibleFieldErrors["subtitle.font_size"]).toBe("Input should be <= 200");

    configureOutputsApi({ updateOutputs: vi.fn(async (_body: OutputsUpdateBody) => outcome()) });
    store.draft!.subtitle!.font_size = 72;
    await store.save();
    expect(store.visibleFieldErrors["subtitle.font_size"]).toBeUndefined();
  });

  it("watermarkMissing：PNG 不在盘上 ⇒ 面板要说出来（免得人以为是 bug）", async () => {
    configureOutputsApi({
      fetchOutputs: vi.fn(async () => response({ watermark: watermark({ exists: false }) })),
    });
    const store = useOutputsStore();
    await store.refresh();
    expect(store.watermarkMissing).toBe(true);
  });
});
