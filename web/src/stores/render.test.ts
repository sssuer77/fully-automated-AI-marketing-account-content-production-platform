import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RenderConsole, RenderJob, RenderProfileOption } from "@/api/endpoints/render";
import { ApiError } from "@/api/http";
import {
  MAX_SPEECH_CHARS_FALLBACK,
  RENDER_POLL_MS,
  configureRenderApi,
  defaultTaskId,
  emptyDraft,
  formatDurationMs,
  qualityNotice,
  qualityReading,
  resultVideoName,
  statusLabel,
  statusTone,
  subtitleEffective,
  subtitleNotice,
  toRequest,
  useRenderStore,
  validate,
  watermarkNotice,
} from "./render";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function profile(overrides: Partial<RenderProfileOption> = {}): RenderProfileOption {
  return {
    name: "douyin_1080x1920_30fps_v1",
    width: 1080,
    height: 1920,
    fps: 30,
    vcodec: "libx264",
    quality_field: "crf",
    quality: 21,
    platforms: ["douyin", "kuaishou"],
    is_default: true,
    ...overrides,
  };
}

function job(overrides: Partial<RenderJob> = {}): RenderJob {
  return {
    id: "r0001",
    task_id: "ui-20260915-120000",
    status: "running",
    stage: "voice",
    done: 2,
    total: 5,
    percent: 40,
    note: "配音 2/5",
    created_at: "2026-09-15T12:00:00.000Z",
    started_at: "2026-09-15T12:00:01.000Z",
    finished_at: null,
    error_code: null,
    error_message: null,
    remediation: null,
    result: null,
    logs: ["[job] 任务 ui-20260915-120000 开始"],
    request: {
      task_id: "ui-20260915-120000",
      profile: "douyin_1080x1920_30fps_v1",
      voice: "Microsoft Huihui Desktop",
      reuse_voice: false,
      seed: 7,
      subtitle: null,
      text_preview: "…",
      text_chars: 12,
    },
    ...overrides,
  };
}

function snapshot(overrides: Partial<RenderConsole> = {}): RenderConsole {
  return {
    engine: "sapi",
    engine_ready: true,
    engine_hint: null,
    profiles: [profile()],
    default_profile: "douyin_1080x1920_30fps_v1",
    voices: [
      { name: "Microsoft Huihui Desktop", is_default: true },
      { name: "Microsoft Zira Desktop", is_default: false },
    ],
    running: null,
    jobs: [],
    videos: [],
    max_speech_chars: 5000,
    watermark_enabled: false,
    watermark_hint: "水印文件不存在，跳过：…/watermark.png",
    subtitle_enabled: true,
    subtitle_hint: null,
    stickers_applied: [],
    stickers_hint: null,
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureRenderApi({
    fetchRenderConsole: vi.fn(async () => snapshot()),
    createRenderJob: vi.fn(async () => job()),
    cancelRenderJob: vi.fn(async () => job({ status: "canceled" })),
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("状态词：五个都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(statusLabel("queued")).toBe("排队中");
    expect(statusLabel("running")).toBe("渲染中");
    expect(statusLabel("succeeded")).toBe("完成");
    expect(statusLabel("failed")).toBe("失败");
    expect(statusLabel("canceled")).toBe("已取消");
    expect(statusLabel("weird")).toBe("weird");
  });

  it("状态灯：取消是黄不是红（不是错，但也没成）", () => {
    expect(statusTone("succeeded")).toBe("ok");
    expect(statusTone("failed")).toBe("error");
    expect(statusTone("canceled")).toBe("warn");
    expect(statusTone("running")).toBe("busy");
    expect(statusTone("queued")).toBe("idle");
  });

  it("时长：不到一分钟只说秒，0 与负数说 0 秒", () => {
    expect(formatDurationMs(0)).toBe("0 秒");
    expect(formatDurationMs(-5)).toBe("0 秒");
    expect(formatDurationMs(23_400)).toBe("23 秒");
    expect(formatDurationMs(125_000)).toBe("2 分 5 秒");
  });

  it("默认任务号：一眼看得出是哪一次，且带 ui- 前缀", () => {
    expect(defaultTaskId(new Date(2026, 8, 15, 9, 5, 3))).toBe("ui-20260915-090503");
  });

  it("自检：任务号空 / 太长 / 文案超长 / 种子不是整数 / 线程越界都给一句人话", () => {
    const draft = emptyDraft(new Date(2026, 8, 15, 9, 5, 3));
    expect(validate(draft, 100)).toBeNull();

    expect(validate({ ...draft, taskId: "  " }, 100)).toContain("任务号不能空");
    expect(validate({ ...draft, taskId: "x".repeat(65) }, 100)).toContain("最长 64");
    expect(validate({ ...draft, text: "字".repeat(101) }, 100)).toContain("最长 100 字");
    expect(validate({ ...draft, seed: "abc" }, 100)).toContain("整数");
    expect(validate({ ...draft, threads: "0" }, 100)).toContain("1–64");
    expect(validate({ ...draft, threads: "65" }, 100)).toContain("1–64");
    expect(validate({ ...draft, threads: "8" }, 100)).toBeNull();
  });

  it("请求体：空的可选项**一个都不发**，留空的数字项也不发", () => {
    const draft = emptyDraft(new Date(2026, 8, 15, 9, 5, 3));
    expect(toRequest(draft)).toEqual({
      task_id: "ui-20260915-090503",
      text: "",
      reuse_voice: false,
    });

    const filled = {
      ...draft,
      text: "今天讲一个跑酷的动作",
      profile: "douyin_1080x1920_30fps_v1",
      voice: "Microsoft Huihui Desktop",
      reuseVoice: true,
      seed: "42",
      threads: "8",
    };
    expect(toRequest(filled)).toEqual({
      task_id: "ui-20260915-090503",
      text: "今天讲一个跑酷的动作",
      reuse_voice: true,
      profile: "douyin_1080x1920_30fps_v1",
      voice: "Microsoft Huihui Desktop",
      seed: 42,
      threads: 8,
    });
  });

  it("成片名：从绝对路径取末段；没有结果就是 null（不是空串）", () => {
    expect(resultVideoName(job())).toBeNull();
    expect(
      resultVideoName(
        job({ result: { final: "D:\\repo\\data\\output\\videos\\20260915-120000_ui_final.mp4" } }),
      ),
    ).toBe("20260915-120000_ui_final.mp4");
    expect(resultVideoName(job({ result: { final: 42 } }))).toBeNull();
  });

  it("质检读数：取成片实测那两个数；没有 / 类型不对就是 null（不是 NaN）", () => {
    expect(qualityReading(job())).toBeNull();
    expect(qualityReading(job({ result: { output_loudness: null } }))).toBeNull();
    expect(qualityReading(job({ result: { output_loudness: { input_i: "x" } } }))).toBeNull();
    expect(
      qualityReading(
        job({ result: { output_loudness: { input_i: -16.48, input_tp: -1.12 } } }),
      ),
    ).toBe("-16.48 LUFS / -1.12 dBTP");
  });

  it("降级出片必须说出来：降级**不会失败**，不说就等于悄悄降级", () => {
    // 没跑完 / 失败 ⇒ 没有结论可言
    expect(qualityNotice(job())).toBeNull();
    expect(qualityNotice(job({ status: "failed", result: {} }))).toBeNull();
    // 正常档出片 + 有读数 ⇒ 只报读数
    const normal = qualityNotice(
      job({
        status: "succeeded",
        result: { degraded: false, output_loudness: { input_i: -16.1, input_tp: -1.2 } },
      }),
    );
    expect(normal).toContain("-16.10 LUFS");
    expect(normal).not.toContain("降级出片");
    // 黑屏降级 ⇒ 说清楚是哪一种
    expect(
      qualityNotice(
        job({ status: "succeeded", result: { degraded: true, degrade_reason: "no_broll_assets" } }),
      ),
    ).toContain("纯黑底");
    // 不认识的降级原因原样显示，**不吞掉**（后端加了新原因而前端还没跟上时能看见）
    expect(
      qualityNotice(
        job({ status: "succeeded", result: { degraded: true, degrade_reason: "composite_chunked" } }),
      ),
    ).toContain("分块合成");
    expect(
      qualityNotice(job({ status: "succeeded", result: { degraded: true, degrade_reason: "新原因" } })),
    ).toContain("新原因");
  });

  it("水印那一句：会贴就没什么要说的；跳过必须把原因说出来", () => {
    expect(watermarkNotice(snapshot({ watermark_enabled: true, watermark_hint: null }))).toBeNull();
    expect(watermarkNotice(snapshot())).toContain("watermark.png");
    expect(
      watermarkNotice(snapshot({ watermark_enabled: false, watermark_hint: null })),
    ).toContain("不阻塞出片");
  });

  it("字幕那一句：会烧且没补充就静默；跳过 / 降级都要说出来", () => {
    expect(subtitleNotice(snapshot({ subtitle_enabled: true, subtitle_hint: null }))).toBeNull();
    // 会烧但用了系统字体（合规说明）⇒ 也要说
    expect(
      subtitleNotice(snapshot({ subtitle_enabled: true, subtitle_hint: "用了系统字体目录" })),
    ).toContain("系统字体");
    expect(subtitleNotice(snapshot({ subtitle_enabled: false, subtitle_hint: null }))).toContain(
      "不阻塞出片",
    );
    expect(
      subtitleNotice(snapshot({ subtitle_enabled: false, subtitle_hint: "找不到字体" })),
    ).toBe("找不到字体");
  });

  it("字幕开关是三态：没选过就跟随配置，选过就听用户的", () => {
    const console_ = snapshot({ subtitle_enabled: false });
    // 草稿是 null（没碰过）⇒ 显示配置里的值
    expect(subtitleEffective(emptyDraft(new Date()), console_)).toBe(false);
    // 拿不到首屏时**默认开**（配置默认也是开，不该因为一次网络抖动就静默不烧字幕）
    expect(subtitleEffective(emptyDraft(new Date()), null)).toBe(true);
    // 显式选过 ⇒ 压过配置
    expect(subtitleEffective({ ...emptyDraft(new Date()), subtitle: true }, console_)).toBe(true);
    expect(
      subtitleEffective({ ...emptyDraft(new Date()), subtitle: false }, snapshot({ subtitle_enabled: true })),
    ).toBe(false);
  });

  it("请求体：字幕没选过就一个字段都不发（让后端按配置决定）", () => {
    const draft = emptyDraft(new Date());
    expect(toRequest(draft)).not.toHaveProperty("subtitle");
    expect(toRequest({ ...draft, subtitle: true })).toHaveProperty("subtitle", true);
    expect(toRequest({ ...draft, subtitle: false })).toHaveProperty("subtitle", false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("store", () => {
  it("首屏：一次拿全，并把下拉框填上默认档 / 第一个音色", async () => {
    const store = useRenderStore();
    await store.refresh();

    expect(store.profiles.map((item) => item.name)).toEqual(["douyin_1080x1920_30fps_v1"]);
    expect(store.voices).toHaveLength(2);
    expect(store.draft.profile).toBe("douyin_1080x1920_30fps_v1");
    expect(store.draft.voice).toBe("Microsoft Huihui Desktop");
    expect(store.maxChars).toBe(5000);
  });

  it("默认档**不覆盖**用户已经选过的（重新载入不该把手里的选择吃掉）", async () => {
    const store = useRenderStore();
    await store.refresh();
    store.draft.profile = "bili_1920x1080_30fps_v1";
    await store.refresh();
    expect(store.draft.profile).toBe("bili_1920x1080_30fps_v1");
  });

  it("首屏拉不到时 maxChars 走后备值，且不能提交", async () => {
    configureRenderApi({
      fetchRenderConsole: vi.fn(async () => {
        throw new ApiError("连接被拒绝", 0, null);
      }),
    });
    const store = useRenderStore();
    await store.refresh();

    expect(store.loadError).toBe("连接被拒绝");
    expect(store.maxChars).toBe(MAX_SPEECH_CHARS_FALLBACK);
    expect(store.canSubmit).toBe(false);
  });

  it("拉取失败**保留**上一次那份快照（一屏数字不因一次抖动变空白）", async () => {
    const store = useRenderStore();
    await store.refresh();
    configureRenderApi({
      fetchRenderConsole: vi.fn(async () => {
        throw new ApiError("超时", 0, null);
      }),
    });
    await store.refresh();

    expect(store.loadError).toBe("超时");
    expect(store.profiles).toHaveLength(1);
  });

  it("引擎没有音色时不能提交（宁可按钮灰着，也不让人白等一次失败）", async () => {
    configureRenderApi({
      fetchRenderConsole: vi.fn(async () =>
        snapshot({ engine_ready: false, voices: [], engine_hint: "装一个中文语音包" }),
      ),
    });
    const store = useRenderStore();
    await store.refresh();
    expect(store.engineReady).toBe(false);
    expect(store.canSubmit).toBe(false);
  });

  it("提交：发出去的是表单里的那份，回来立刻盯上这条新任务", async () => {
    const createRenderJob = vi.fn(async () => job({ id: "r0007", status: "queued" }));
    configureRenderApi({ createRenderJob });
    const store = useRenderStore();
    await store.refresh();

    store.draft.taskId = "demo-1";
    store.draft.text = "今天讲一个跑酷的动作";
    expect(await store.submit()).toBe(true);

    expect(createRenderJob).toHaveBeenCalledWith({
      task_id: "demo-1",
      text: "今天讲一个跑酷的动作",
      reuse_voice: false,
      profile: "douyin_1080x1920_30fps_v1",
      voice: "Microsoft Huihui Desktop",
    });
    expect(store.notice).toContain("r0007");
  });

  it("自检不过就**不发请求**（把一句话说在本地，而不是等后端 422）", async () => {
    const createRenderJob = vi.fn(async () => job());
    configureRenderApi({ createRenderJob });
    const store = useRenderStore();
    await store.refresh();

    store.draft.taskId = "   ";
    expect(await store.submit()).toBe(false);
    expect(createRenderJob).not.toHaveBeenCalled();
    expect(store.error).toContain("任务号不能空");
  });

  it("提交失败写 error，不把失败说成成功", async () => {
    configureRenderApi({
      createRenderJob: vi.fn(async () => {
        throw new ApiError("没有可用的素材", 409, null);
      }),
    });
    const store = useRenderStore();
    await store.refresh();
    expect(await store.submit()).toBe(false);
    expect(store.error).toBe("没有可用的素材（HTTP 409）");
  });

  it("取消：措辞只说「已请求取消」—— 协作式取消本来就可能还在跑", async () => {
    const cancelRenderJob = vi.fn(async () => job({ status: "running" }));
    configureRenderApi({
      cancelRenderJob,
      fetchRenderConsole: vi.fn(async () => snapshot({ running: job() })),
    });
    const store = useRenderStore();
    await store.refresh();
    expect(await store.cancel("r0001")).toBe(true);

    expect(cancelRenderJob).toHaveBeenCalledWith("r0001");
    expect(store.notice).toContain("已收到取消请求");
    expect(store.notice).not.toContain("已取消。");
  });

  it("★ 有活在跑才轮询：跑完就停，空闲时一秒一次都没有", async () => {
    vi.useFakeTimers();
    const fetchRenderConsole = vi.fn(async () => snapshot({ running: job() }));
    configureRenderApi({ fetchRenderConsole });
    const store = useRenderStore();

    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchRenderConsole).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS * 2);
    const whileRunning = fetchRenderConsole.mock.calls.length;
    expect(whileRunning).toBeGreaterThanOrEqual(3);

    // 跑完 ⇒ 下一拍之后不再发请求
    fetchRenderConsole.mockImplementation(async () =>
      snapshot({ jobs: [job({ status: "succeeded" })] }),
    );
    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS);
    const settled = fetchRenderConsole.mock.calls.length;
    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS * 5);
    expect(fetchRenderConsole.mock.calls.length).toBe(settled);

    store.stop();
  });

  it("★ 空闲时 start() 只拉一次（不做「看起来在动」的兜底轮询）", async () => {
    vi.useFakeTimers();
    const fetchRenderConsole = vi.fn(async () => snapshot());
    configureRenderApi({ fetchRenderConsole });
    const store = useRenderStore();

    store.start();
    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS * 30);
    expect(fetchRenderConsole).toHaveBeenCalledTimes(1);
  });

  it("stop() 之后不再轮询（离开面板不该还在每秒问一次后端）", async () => {
    vi.useFakeTimers();
    const fetchRenderConsole = vi.fn(async () => snapshot({ running: job() }));
    configureRenderApi({ fetchRenderConsole });
    const store = useRenderStore();

    store.start();
    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS);
    store.stop();
    const stopped = fetchRenderConsole.mock.calls.length;

    await vi.advanceTimersByTimeAsync(RENDER_POLL_MS * 5);
    expect(fetchRenderConsole.mock.calls.length).toBe(stopped);
  });

  it("进度卡：在跑的优先；跑完那条仍留在屏上（不立刻跳走）", async () => {
    const done = job({ id: "r0002", status: "succeeded", percent: 100 });
    configureRenderApi({
      fetchRenderConsole: vi.fn(async () =>
        snapshot({ running: null, jobs: [done, job({ id: "r0001", status: "canceled" })] }),
      ),
    });
    const store = useRenderStore();
    await store.refresh();
    expect(store.focusedJob?.id).toBe("r0002");

    configureRenderApi({
      fetchRenderConsole: vi.fn(async () =>
        snapshot({ running: job({ id: "r0003" }), jobs: [done] }),
      ),
    });
    await store.refresh();
    expect(store.focusedJob?.id).toBe("r0003");
  });

  it("「再来一条」把参数填回表单，但任务号换新的、文案留给用户自己补", async () => {
    configureRenderApi({
      fetchRenderConsole: vi.fn(async () => snapshot({ jobs: [job()] })),
    });
    const store = useRenderStore();
    await store.refresh();

    store.draft.text = "上一版的文案";
    store.reuseJob(job());

    expect(store.draft.taskId).not.toBe("ui-20260915-120000");
    expect(store.draft.profile).toBe("douyin_1080x1920_30fps_v1");
    expect(store.draft.voice).toBe("Microsoft Huihui Desktop");
    expect(store.draft.seed).toBe("7");
    expect(store.draft.text).toBe("上一版的文案");
    expect(store.notice).toContain("文案要自己补");
  });
});