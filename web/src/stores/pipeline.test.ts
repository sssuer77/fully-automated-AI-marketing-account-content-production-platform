import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PipelineConsole, PipelineJob, PipelineTask } from "@/api/endpoints/pipeline";
import { ApiError } from "@/api/http";
import {
  PIPELINE_POLL_MS,
  configurePipelineApi,
  emptyDraft,
  finalVideoName,
  formatClock,
  jobStatusLabel,
  jobStatusTone,
  previewReason,
  previewRunnable,
  previewStale,
  stageLabel,
  stageText,
  taskStatusLabel,
  taskStatusTone,
  toRequest,
  usePipelineStore,
  validate,
} from "./pipeline";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const TASK = "01M2M9THZ1M83CT5EJH789ZVZA";

function job(overrides: Partial<PipelineJob> = {}): PipelineJob {
  return {
    id: "p0001",
    task_id: TASK,
    until: "completed",
    status: "running",
    stage: "voice",
    done: 2,
    total: 5,
    percent: 40,
    note: "等 voice 池把句子念完",
    created_at: "2026-09-17T12:00:00.000Z",
    started_at: "2026-09-17T12:00:01.000Z",
    finished_at: null,
    steps: [],
    final: null,
    quality: null,
    error_code: null,
    error_message: null,
    remediation: null,
    logs: [`[job] 任务 ${TASK} → completed`],
    request: {
      task_id: TASK,
      until: "completed",
      voice: "Microsoft Huihui Desktop",
      profile: null,
      seed: 7,
      subtitle: null,
    },
    ...overrides,
  };
}

function preview(overrides: Partial<PipelineTask> = {}): PipelineTask {
  return {
    task_id: TASK,
    title: "跑酷地图",
    status: "reviewing",
    until: "completed",
    runnable: true,
    reason: null,
    note: "从 reviewing 一路推到 completed",
    has_script: true,
    active_job: null,
    ...overrides,
  };
}

function snapshot(overrides: Partial<PipelineConsole> = {}): PipelineConsole {
  return {
    until_options: [
      { value: "reviewing", label: "推到审稿", description: "稿件就绪、进确认闸之前" },
      { value: "queued_voice", label: "投递配音作业", description: "排进 voice 池就停" },
      { value: "voicing", label: "停在配音中", description: "投递后停在 voicing" },
      { value: "queued_render", label: "配音 + 母带", description: "念完、母带拼好，停在待渲染" },
      { value: "completed", label: "一路出成片", description: "默认：配音 → 母带 → 渲染" },
    ],
    default_until: "completed",
    voices: [
      { name: "Microsoft Huihui Desktop", is_default: true },
      { name: "Microsoft Zira Desktop", is_default: false },
    ],
    default_voice: "Microsoft Huihui Desktop",
    engine_ready: true,
    engine_hint: null,
    active: null,
    jobs: [],
    max_log_lines: 200,
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configurePipelineApi({
    fetchPipelineConsole: vi.fn(async () => snapshot()),
    fetchPipelineTask: vi.fn(async () => preview()),
    createPipelineJob: vi.fn(async () => ({ job: job(), deduped: false })),
    fetchPipelineJob: vi.fn(async () => job()),
    cancelPipelineJob: vi.fn(async () => job({ status: "canceled" })),
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("job 状态词：五个都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(jobStatusLabel("queued")).toBe("排队中");
    expect(jobStatusLabel("running")).toBe("跑着");
    expect(jobStatusLabel("succeeded")).toBe("完成");
    expect(jobStatusLabel("failed")).toBe("失败");
    expect(jobStatusLabel("canceled")).toBe("已取消");
    expect(jobStatusLabel("weird")).toBe("weird");
  });

  it("任务状态词：16 态里常用的那些翻成中文，认不出的照原样", () => {
    expect(taskStatusLabel("reviewing")).toBe("审稿中");
    expect(taskStatusLabel("awaiting_approval")).toBe("待人工确认");
    expect(taskStatusLabel("completed")).toBe("已成片");
    expect(taskStatusLabel("manual_pool")).toBe("人工池");
    expect(taskStatusLabel("brand_new_state")).toBe("brand_new_state");
  });

  it("灯色：任务与 job 是两套状态，但「动着」长得一样（都是 busy）", () => {
    expect(jobStatusTone("succeeded")).toBe("ok");
    expect(jobStatusTone("failed")).toBe("error");
    expect(jobStatusTone("canceled")).toBe("warn");
    expect(jobStatusTone("running")).toBe("busy");
    expect(jobStatusTone("queued")).toBe("idle");

    expect(taskStatusTone("completed")).toBe("ok");
    expect(taskStatusTone("failed")).toBe("error");
    expect(taskStatusTone("awaiting_approval")).toBe("warn");
    expect(taskStatusTone("voicing")).toBe("busy");
    expect(taskStatusTone("pending")).toBe("idle");
  });

  it("阶段：翻成中文；分母为 0 时只显示阶段，不编一个分母", () => {
    expect(stageLabel("voice")).toBe("配音");
    expect(stageLabel("render")).toBe("渲染");
    expect(stageLabel("??")).toBe("??");
    expect(stageText(job({ stage: "render", done: 3, total: 12 }))).toBe("渲染 3/12");
    expect(stageText(job({ stage: "starting", done: 0, total: 0 }))).toBe("准备");
    expect(stageText(job({ stage: "", done: 0, total: 0 }))).toBe("-");
  });

  it("时间：ISO 截到 HH:MM:SS；没有 / 太短给 -（不显示半截时间）", () => {
    expect(formatClock("2026-09-17T12:34:56.000Z")).toBe("12:34:56");
    expect(formatClock(null)).toBe("-");
    expect(formatClock("2026-09-17")).toBe("-");
  });

  it("成片名：从绝对路径取末段；没有就是 null（不是空串）", () => {
    expect(finalVideoName(job())).toBeNull();
    expect(
      finalVideoName(job({ final: "D:\\repo\\data\\output\\videos\\20260917-120000_x_final.mp4" })),
    ).toBe("20260917-120000_x_final.mp4");
    expect(finalVideoName(job({ final: "" }))).toBeNull();
  });

  it("自检：空任务号 / 太长 / 带空格都给一句人话，正常的给 null", () => {
    const draft = emptyDraft();
    expect(validate({ ...draft, taskId: TASK })).toBeNull();
    expect(validate(draft)).toContain("任务号不能空");
    expect(validate({ ...draft, taskId: "x".repeat(65) })).toContain("最长 64");
    expect(validate({ ...draft, taskId: "ui 2026" })).toContain("只能用字母、数字");
    expect(validate({ ...draft, taskId: "ui-20260917-120000", seed: "abc" })).toContain("整数");
    expect(validate({ ...draft, taskId: "ui-20260917-120000", seed: "42" })).toBeNull();
  });

  it("请求体：空的可选项一个都不发；subtitle 也不发（这一屏没有那个控件）", () => {
    expect(toRequest({ taskId: `  ${TASK}  `, until: "", voice: "", seed: "" })).toEqual({
      task_id: TASK,
    });
    expect(toRequest({ taskId: TASK, until: "voicing", voice: "Microsoft Huihui Desktop", seed: "42" })).toEqual({
      task_id: TASK,
      until: "voicing",
      voice: "Microsoft Huihui Desktop",
      seed: 42,
    });
  });

  it("预览的话：不能跑说原因；能跑但没稿子也提前说（流水线不写稿）", () => {
    expect(previewReason(null)).toBeNull();
    expect(previewReason(preview())).toBeNull();
    expect(previewReason(preview({ runnable: false, reason: "任务停在确认闸" }))).toBe(
      "任务停在确认闸",
    );
    expect(previewReason(preview({ has_script: false }))).toContain("不写稿");
  });

  it("预览的判据：拿不到预览不拦（真判据在后端）", () => {
    expect(previewRunnable(null)).toBe(true);
    expect(previewRunnable(preview())).toBe(true);
    expect(previewRunnable(preview({ runnable: false, reason: "x" }))).toBe(false);
  });

  it("预览是不是过时了：任务号或落点对不上就算过时", () => {
    const draft = { taskId: TASK, until: "completed", voice: "", seed: "" };
    expect(previewStale(null, draft)).toBe(false);
    expect(previewStale(preview(), draft)).toBe(false);
    expect(previewStale(preview(), { ...draft, taskId: "other" })).toBe(true);
    expect(previewStale(preview(), { ...draft, until: "voicing" })).toBe(true);
    // 落点留空 = 跟随后端默认，那不算"对不上"
    expect(previewStale(preview(), { ...draft, until: "" })).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("usePipelineStore", () => {
  it("首屏：拿回落点 / 音色，并把默认值填进表单（用户选过的不覆盖）", async () => {
    const store = usePipelineStore();
    await store.refresh();

    expect(store.untilOptions).toHaveLength(5);
    expect(store.draft.until).toBe("completed");
    expect(store.draft.voice).toBe("Microsoft Huihui Desktop");
    expect(store.engineReady).toBe(true);

    store.draft.until = "voicing";
    store.draft.voice = "Microsoft Zira Desktop";
    await store.refresh();
    expect(store.draft.until).toBe("voicing");
    expect(store.draft.voice).toBe("Microsoft Zira Desktop");
  });

  it("首屏拉失败：留一句人话，旧快照不因一次抖动变空白", async () => {
    const store = usePipelineStore();
    await store.refresh();
    const kept = store.snapshot;

    configurePipelineApi({
      fetchPipelineConsole: vi.fn(async () => {
        throw new ApiError("后端挂了", 500, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toContain("后端挂了");
    expect(store.snapshot).toBe(kept);
  });

  it("预览：任务号空着就问都不问，且把上一条结论清掉（它说的是别的任务号）", async () => {
    const ask = vi.fn(async () => preview());
    configurePipelineApi({ fetchPipelineTask: ask });

    const store = usePipelineStore();
    await store.refresh();
    store.draft.taskId = TASK;
    await store.ask();
    expect(ask).toHaveBeenCalledWith(TASK, "completed");
    expect(store.preview).not.toBeNull();

    store.draft.taskId = "   ";
    await store.ask();
    expect(store.preview).toBeNull();
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("预览：任务号不存在 ⇒ 只留一句错误，不留半份结论", async () => {
    configurePipelineApi({
      fetchPipelineTask: vi.fn(async () => {
        throw new ApiError("没有这个任务", 404, null);
      }),
    });
    const store = usePipelineStore();
    await store.refresh();
    store.draft.taskId = "nosuchtask";
    await store.ask();

    expect(store.preview).toBeNull();
    expect(store.previewError).toContain("没有这个任务");
  });

  it("预览说不能跑 ⇒ 按钮按不动（原因也摆在旁边）", async () => {
    configurePipelineApi({
      fetchPipelineTask: vi.fn(async () =>
        preview({ runnable: false, reason: "任务停在确认闸（awaiting_approval），流水线不代按" }),
      ),
    });
    const store = usePipelineStore();
    await store.refresh();
    store.draft.taskId = TASK;
    await store.ask();

    expect(store.canSubmit).toBe(false);
    expect(store.problem).toBeNull();
  });

  it("提交：服务端说复用了在跑的那条 ⇒ 面板说清「没开出第二条」", async () => {
    configurePipelineApi({
      createPipelineJob: vi.fn(async () => ({ job: job({ id: "p0007" }), deduped: true })),
    });
    const store = usePipelineStore();
    await store.refresh();
    store.draft.taskId = TASK;
    expect(await store.submit()).toBe(true);
    expect(store.notice).toContain("已经有一条在跑");
    expect(store.notice).toContain("没有重复开第二条");
  });

  it("提交：空任务号当面拒掉，一个请求都不发", async () => {
    const create = vi.fn(async () => ({ job: job(), deduped: false }));
    configurePipelineApi({ createPipelineJob: create });
    const store = usePipelineStore();
    await store.refresh();

    expect(await store.submit()).toBe(false);
    expect(create).not.toHaveBeenCalled();
    expect(store.error).toContain("任务号不能空");
  });

  it("取消：协作式 ⇒ 只说「已请求取消」，不说「已取消」", async () => {
    const store = usePipelineStore();
    await store.refresh();
    await store.cancel("p0001");
    expect(store.notice).toContain("已取消");

    configurePipelineApi({ cancelPipelineJob: vi.fn(async () => job({ status: "running" })) });
    await store.cancel("p0001");
    expect(store.notice).toContain("已收到取消请求");
    expect(store.notice).toContain("下一次进度回调处");
  });

  it("有活才轮询：跑着的时候按秒问，跑完就停", async () => {
    vi.useFakeTimers();
    const fetchConsole = vi.fn(async () => snapshot({ active: job() }));
    configurePipelineApi({ fetchPipelineConsole: fetchConsole });

    const store = usePipelineStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchConsole).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(PIPELINE_POLL_MS + 10);
    expect(fetchConsole).toHaveBeenCalledTimes(2);

    // 跑完 ⇒ 定时器停掉，不再白问后端
    fetchConsole.mockImplementation(async () => snapshot({ active: null }));
    await vi.advanceTimersByTimeAsync(PIPELINE_POLL_MS + 10);
    expect(fetchConsole).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(PIPELINE_POLL_MS * 5);
    expect(fetchConsole).toHaveBeenCalledTimes(3);

    store.stop();
  });

  it("离开面板：stop 之后一拍都不再发", async () => {
    vi.useFakeTimers();
    const fetchConsole = vi.fn(async () => snapshot({ active: job() }));
    configurePipelineApi({ fetchPipelineConsole: fetchConsole });

    const store = usePipelineStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    store.stop();

    await vi.advanceTimersByTimeAsync(PIPELINE_POLL_MS * 5);
    expect(fetchConsole).toHaveBeenCalledTimes(1);
  });

  it("再来一条：把那条的参数填回表单，并顺手重问一次预览", async () => {
    const ask = vi.fn(async () => preview());
    configurePipelineApi({ fetchPipelineTask: ask });
    const store = usePipelineStore();
    await store.refresh();

    store.reuseJob(job({ task_id: TASK, until: "queued_render" }));
    expect(store.draft.taskId).toBe(TASK);
    expect(store.draft.until).toBe("queued_render");
    expect(store.draft.voice).toBe("Microsoft Huihui Desktop");
    expect(store.draft.seed).toBe("7");
    await vi.waitFor(() => expect(ask).toHaveBeenCalledWith(TASK, "queued_render"));
  });

  it("清空表单：草稿与预览一起清掉（留着上一条的结论比没有更糟）", async () => {
    const store = usePipelineStore();
    await store.refresh();
    store.draft.taskId = TASK;
    await store.ask();
    expect(store.preview).not.toBeNull();

    store.resetDraft();
    expect(store.draft.taskId).toBe("");
    expect(store.preview).toBeNull();
    // 落点 / 音色回到默认值（刚拉过首屏，不必再拉一次）
    expect(store.draft.until).toBe("completed");
    expect(store.draft.voice).toBe("Microsoft Huihui Desktop");
  });
});
