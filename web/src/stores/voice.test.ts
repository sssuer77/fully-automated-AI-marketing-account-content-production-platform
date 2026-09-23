import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PipelineSubmit } from "@/api/endpoints/pipeline";
import type {
  EnqueueVoiceResponse,
  ResynthResponse,
  SentenceProgress,
  SentenceVoice,
  SentenceVoiceList,
  VoiceMapResponse,
  VoiceOptions,
  VoicePreview,
} from "@/api/endpoints/voice";
import { ApiError } from "@/api/http";
import {
  PREVIEW_POLL_MS,
  VOICE_POLL_MS,
  busyNotice,
  busySeqs,
  changeNotice,
  changedSpeakers,
  configureVoiceApi,
  confirmPrompt,
  durationText,
  outstanding,
  previewHint,
  previewLabel,
  progressText,
  skipNotes,
  spanText,
  speakersOf,
  statusLabel,
  startNotice,
  statusTone,
  toVoiceMapRequest,
  useVoiceStore,
  validateTaskId,
  voiceDraft,
  voiceMapRows,
  voiceOptionLabel,
  voiceSourceLabel,
} from "./voice";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const TASK_ID = "ui-20260916-120000";
const HUIHUI = "Microsoft Huihui Desktop";
const ZIRA = "Microsoft Zira Desktop";
const HINT = "重配完成后跑一次配音收口会让时间轴全量重算";

/** 后端「开始配音」那条 hint（面板直接显示它）。 */
const ENQUEUE_HINT = "作业已经排进 voice 池：常驻池在跑就会接着念";

function progress(overrides: Partial<SentenceProgress> = {}): SentenceProgress {
  return {
    total: 3,
    pending: 0,
    synthesizing: 0,
    done: 3,
    skipped: 0,
    failed: 0,
    settled: 3,
    ratio: 1,
    ...overrides,
  };
}

function sentence(overrides: Partial<SentenceVoice> = {}): SentenceVoice {
  return {
    id: "01J000000000000000000000S1",
    seq: 1,
    speaker: "bigbear",
    text: "第一句：公园里有一只大熊。",
    tts_status: "done",
    tts_engine: "sapi",
    tts_voice_id: HUIHUI,
    tts_duration_ms: 3978,
    tts_attempts: 1,
    tts_error: null,
    start_ms: 0,
    end_ms: 3978,
    version: 2,
    degraded: false,
    audio_url: `/api/v1/media/voice/${TASK_ID}/s001.wav`,
    audio_source: "sentence",
    can_resynth: true,
    ...overrides,
  };
}

function snapshot(overrides: Partial<SentenceVoiceList> = {}): SentenceVoiceList {
  return {
    task_id: TASK_ID,
    task_status: "voicing",
    progress: progress(),
    sentences: [
      sentence(),
      sentence({ id: "01J000000000000000000000S2", seq: 2, speaker: "littlebear" }),
      sentence({ id: "01J000000000000000000000S3", seq: 3 }),
    ],
    voice_map: { bigbear: HUIHUI, littlebear: ZIRA },
    timeline_total_ms: 14352,
    timeline_stale: false,
    ...overrides,
  };
}

function options(overrides: Partial<VoiceOptions> = {}): VoiceOptions {
  return {
    voices: [
      { id: HUIHUI, source: "sapi", speakable: true, preview_state: "ready", preview_url: "/p/a.wav" },
      { id: ZIRA, source: "sapi", speakable: true, preview_state: "missing" },
      { id: "ref-voice-01", source: "profile", speakable: false, preview_state: "failed" },
    ],
    task_id: TASK_ID,
    voice_map: { bigbear: HUIHUI, littlebear: ZIRA },
    note: null,
    ...overrides,
  };
}

function resynthReport(overrides: Partial<ResynthResponse> = {}): ResynthResponse {
  return {
    sentence_id: "01J000000000000000000000S1",
    task_id: TASK_ID,
    seq: 1,
    status: "pending",
    job_id: "01J00000000000000000000JOB1",
    job_created: false,
    progress: progress({ done: 2, pending: 1, settled: 2, ratio: 0.67 }),
    timeline_stale: true,
    timeline_total_ms: 14352,
    hint: HINT,
    ...overrides,
  };
}

function mapReport(overrides: Partial<VoiceMapResponse> = {}): VoiceMapResponse {
  return {
    task_id: TASK_ID,
    voice_map: { bigbear: ZIRA, littlebear: ZIRA },
    changes: [
      { speaker: "bigbear", before: HUIHUI, after: ZIRA, affected: 2, sentences: [1, 3] },
    ],
    affected: 2,
    requeued: 2,
    created_jobs: 2,
    busy: [],
    progress: progress({ done: 1, pending: 2, settled: 1, ratio: 0.33 }),
    timeline_stale: true,
    timeline_total_ms: 14352,
    hint: HINT,
    ...overrides,
  };
}

function enqueueReport(overrides: Partial<EnqueueVoiceResponse> = {}): EnqueueVoiceResponse {
  return {
    task_id: TASK_ID,
    status_before: "queued_voice",
    status: "voicing",
    queued: 3,
    outstanding: 3,
    progress: progress({ done: 0, pending: 3, settled: 0, ratio: 0 }),
    timeline_stale: false,
    timeline_total_ms: null,
    hint: ENQUEUE_HINT,
    ...overrides,
  };
}

function pipelineSubmit(overrides: Partial<PipelineSubmit> = {}): PipelineSubmit {
  return {
    job: {
      id: "p0001",
      task_id: TASK_ID,
      until: "completed",
      status: "running",
      stage: "voice",
      done: 0,
      total: 0,
      percent: 0,
      note: "",
      created_at: "2026-09-21T11:00:00Z",
      started_at: "2026-09-21T11:00:00Z",
      finished_at: null,
      steps: [],
      final: null,
      quality: null,
      error_code: null,
      error_message: null,
      remediation: null,
      logs: [],
      request: {},
    },
    deduped: false,
    ...overrides,
  };
}

/** 换音色要"先算代价"的那个 409（后端 `VOICE_MAP_CONFIRM_REQUIRED`）。 */
function confirmError(overrides: Record<string, unknown> = {}): ApiError {
  return new ApiError(`换音色会让 2 句重新配音，确认后再提交`, 409, {
    code: "VOICE_MAP_CONFIRM_REQUIRED",
    message: `换音色会让 2 句重新配音，确认后再提交`,
    context: { task_id: TASK_ID, affected: 2, total: 3, speakers: ["bigbear"], sentences: [1, 3] },
    remediation: "带上 confirm=true 重发一次（面板据此弹二次确认框）",
    type: "StudioError",
    ...overrides,
  });
}

let fetchSentences = vi.fn(async () => snapshot());
let fetchVoiceOptions = vi.fn(async () => options());
let resynthSentence = vi.fn(async () => resynthReport());
let patchVoiceMap = vi.fn(async () => mapReport());
let fetchVoicePreview = vi.fn(async () => previewSample());
let createVoicePreview = vi.fn(async () => previewSample());
let enqueueVoice = vi.fn(async () => enqueueReport());
let createPipelineJob = vi.fn(async () => pipelineSubmit());

beforeEach(() => {
  setActivePinia(createPinia());
  fetchSentences = vi.fn(async () => snapshot());
  fetchVoiceOptions = vi.fn(async () => options());
  resynthSentence = vi.fn(async () => resynthReport());
  patchVoiceMap = vi.fn(async () => mapReport());
  fetchVoicePreview = vi.fn(async () => previewSample());
  createVoicePreview = vi.fn(async () => previewSample());
  enqueueVoice = vi.fn(async () => enqueueReport());
  createPipelineJob = vi.fn(async () => pipelineSubmit());
  configureVoiceApi({
    fetchSentences,
    fetchVoiceOptions,
    resynthSentence,
    patchVoiceMap,
    fetchVoicePreview,
    createVoicePreview,
    enqueueVoice,
    createPipelineJob,
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
    expect(statusLabel("pending")).toBe("待配音");
    expect(statusLabel("synthesizing")).toBe("正在配");
    expect(statusLabel("done")).toBe("已配好");
    expect(statusLabel("skipped")).toBe("已跳过");
    expect(statusLabel("failed")).toBe("失败");
    expect(statusLabel("whatever")).toBe("whatever");
  });

  it("状态灯：跳过是 warn（不是错，但也没声音），正在配是 busy", () => {
    expect(statusTone("done")).toBe("ok");
    expect(statusTone("failed")).toBe("error");
    expect(statusTone("skipped")).toBe("warn");
    expect(statusTone("synthesizing")).toBe("busy");
    expect(statusTone("pending")).toBe("idle");
  });

  it("音色来源翻成人话：下拉框里不出现裸 sapi / profile", () => {
    expect(voiceSourceLabel("sapi")).toBe("系统音色");
    expect(voiceSourceLabel("profile")).toBe("参考音");
    expect(voiceSourceLabel("future")).toBe("future");
    expect(voiceOptionLabel({ id: HUIHUI, source: "sapi", speakable: true, preview_state: "ready" })).toBe(
      `${HUIHUI} · 系统音色`,
    );
  });

  it("念不出来的参考音照旧列出，但那一行必须写明（否则会选中一支没人声的成片）", () => {
    expect(
      voiceOptionLabel({ id: "ref-voice-01", source: "profile", speakable: false, preview_state: "ready" }),
    ).toBe(
      "ref-voice-01 · 参考音 · 当前引擎念不出来",
    );
  });

  it("进度行：零的那几项不出现（写一串 0 只是噪音）", () => {
    expect(progressText(progress())).toBe("已定局 3/3");
    expect(progressText(progress({ done: 1, pending: 2, settled: 1, ratio: 0.33 }))).toBe(
      "已定局 1/3 · 待配 2",
    );
    expect(
      progressText(progress({ done: 1, synthesizing: 1, skipped: 1, settled: 2, ratio: 0.67 })),
    ).toBe("已定局 2/3 · 正在配 1 · 跳过 1");
  });

  it("还有没有会自己变的句子：失败与跳过都是定局，不该让定时器空转", () => {
    expect(outstanding(null)).toBe(0);
    expect(outstanding(progress({ done: 0, failed: 3, settled: 3 }))).toBe(0);
    expect(outstanding(progress({ done: 1, pending: 1, synthesizing: 1, settled: 1 }))).toBe(2);
  });

  it("跳过 / 失败要逐条列出来，原因就写在行上（原因躺在库里等于没有）", () => {
    const rows = [
      sentence(),
      sentence({ id: "S2", seq: 2, tts_status: "skipped", degraded: true, tts_error: "没有装中文语音包" }),
      sentence({ id: "S3", seq: 3, tts_status: "failed", tts_error: "SAPI 超时" }),
      sentence({ id: "S4", seq: 4, tts_status: "skipped", degraded: true, tts_error: null }),
    ];
    expect(skipNotes(rows)).toEqual([
      { seq: 2, speaker: "bigbear", reason: "没有装中文语音包" },
      { seq: 3, speaker: "bigbear", reason: "SAPI 超时" },
      { seq: 4, speaker: "bigbear", reason: "库里没有记原因" },
    ]);
  });

  it("时长：没念过是 `-`，不是 0 秒（0 秒会被读成念了个空的）", () => {
    expect(durationText(null)).toBe("-");
    expect(durationText(undefined)).toBe("-");
    expect(durationText(0)).toBe("-");
    expect(durationText(1500)).toBe("2 秒");
    expect(durationText(65000)).toBe("1 分 5 秒");
  });

  it("时间轴位置：没定局就不编一个数字出来", () => {
    expect(spanText(sentence())).toBe("0 → 3978 ms");
    expect(spanText(sentence({ start_ms: null, end_ms: null }))).toBe("-");
    expect(spanText(sentence({ start_ms: 100, end_ms: null }))).toBe("-");
  });

  it("角色按首次出现顺序去重（面板的顺序要跟稿子一致）", () => {
    const rows = [
      sentence({ speaker: "bigbear" }),
      sentence({ speaker: "littlebear" }),
      sentence({ speaker: "bigbear" }),
      sentence({ speaker: "narrator" }),
    ];
    expect(speakersOf(rows)).toEqual(["bigbear", "littlebear", "narrator"]);
    expect(speakersOf([])).toEqual([]);
  });

  it("音色草稿：库里有的用库里的，没有的落到第一个可用音色", () => {
    expect(voiceDraft({ bigbear: ZIRA }, ["bigbear", "littlebear"], HUIHUI)).toEqual({
      bigbear: ZIRA,
      littlebear: HUIHUI,
    });
  });

  it("音色映射行：库里记的音色本机找不到时要**说出来**，不能让下拉框空着", () => {
    const voices = [{ id: HUIHUI, source: "sapi", speakable: true, preview_state: "ready" }];
    const rows = voiceMapRows(
      ["bigbear", "littlebear"],
      { bigbear: "bigbear", littlebear: HUIHUI },
      { bigbear: "bigbear", littlebear: HUIHUI },
      voices,
    );
    expect(rows).toEqual([
      { speaker: "bigbear", current: "bigbear", was: "bigbear", unknown: true },
      { speaker: "littlebear", current: HUIHUI, was: HUIHUI, unknown: false },
    ]);
    // 没有映射的角色：current 是 fallback，不该被当成"本机找不到"
    const empty = voiceMapRows(["narrator"], { narrator: HUIHUI }, {}, voices);
    expect(empty[0]).toEqual({ speaker: "narrator", current: HUIHUI, was: "", unknown: false });
  });

  it("只提交真正改过的角色", () => {
    const before = { bigbear: HUIHUI, littlebear: ZIRA };
    expect(changedSpeakers(before, { bigbear: HUIHUI, littlebear: ZIRA })).toEqual([]);
    expect(changedSpeakers(before, { bigbear: ZIRA, littlebear: ZIRA })).toEqual(["bigbear"]);
  });

  it("请求体只带**真正改过**的角色：别替用户断言他没碰过的那些行", () => {
    const before = { bigbear: HUIHUI, littlebear: ZIRA };
    // 熊二一个字都没改 ⇒ 它不该出现在请求体里（它的存量值可能本机根本找不到）
    expect(toVoiceMapRequest({ bigbear: ZIRA, littlebear: ZIRA }, before, false)).toEqual({
      voice_map: { bigbear: ZIRA },
      confirm: false,
    });
    expect(toVoiceMapRequest({ bigbear: ZIRA, littlebear: ZIRA }, before, true)).toEqual({
      voice_map: { bigbear: ZIRA },
      confirm: true,
    });
    // 一个字都没改 ⇒ 空的 voice_map（后端 min_length=1 会拒，调用方不该走到这里）
    expect(toVoiceMapRequest(before, before, false).voice_map).toEqual({});
    // 清空的下拉框同样不发（空串在后端是 422）
    expect(toVoiceMapRequest({ bigbear: ZIRA, littlebear: "" }, before, false).voice_map).toEqual({
      bigbear: ZIRA,
    });
  });

  it("二次确认框只认那个错误码：别的 409 不该被弹成换音色确认框", () => {
    expect(confirmPrompt(new Error("boom"))).toBeNull();
    expect(confirmPrompt(new ApiError("池暂停了", 409, { code: "POOL_PAUSED" }))).toBeNull();
    expect(confirmPrompt(new ApiError("池暂停了", 422, { code: "VOICE_MAP_CONFIRM_REQUIRED" }))).toBeNull();
    expect(confirmPrompt(new ApiError("x", 409, { code: "VOICE_MAP_CONFIRM_REQUIRED" }))).toBeNull();

    const prompt = confirmPrompt(confirmError());
    expect(prompt).toEqual({
      message: "换音色会让 2 句重新配音，确认后再提交",
      affected: 2,
      total: 3,
      speakers: ["bigbear"],
      sentences: [1, 3],
    });
  });

  it("busy 的句子 id 要翻成句号：面板上给人看的不是 ULID", () => {
    const rows = [sentence(), sentence({ id: "01J000000000000000000000S2", seq: 2 })];
    expect(busySeqs(rows, ["01J000000000000000000000S2"])).toEqual([2]);
    expect(busySeqs(rows, [])).toEqual([]);
  });

  it("换音色的两种消息分开说：绿底横幅上写警告，人不会当回事", () => {
    const rows = [sentence(), sentence({ id: "01J000000000000000000000S3", seq: 3 })];
    expect(changeNotice(mapReport())).toBe("已重排 2 句（影响 2 句，新建 2 条作业）。");
    expect(changeNotice(mapReport({ affected: 0, requeued: 0, created_jobs: 0 }))).toContain(
      "没有句子需要重配",
    );
    expect(busyNotice(mapReport(), rows)).toBeNull();
    expect(busyNotice(mapReport({ busy: ["01J000000000000000000000S3"] }), rows)).toContain("第 3 句");
  });

  it("任务号自检：空 / 超长 / 带路径分隔符都要拦下来（后端路径契约 §04.3.7）", () => {
    expect(validateTaskId("  ")).toContain("不能空");
    expect(validateTaskId("a".repeat(65))).toContain("最长");
    expect(validateTaskId("a/b")).toContain("只接受");
    expect(validateTaskId("a b")).toContain("只接受");
    expect(validateTaskId(TASK_ID)).toBeNull();
    expect(validateTaskId("01J0000000000000000000000AB")).toBeNull();
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("store", () => {
  it("首屏：两个端点都拉，任务号空着就一个请求都不发", async () => {
    const store = useVoiceStore();
    store.start();
    await Promise.resolve();
    expect(fetchSentences).not.toHaveBeenCalled();

    store.setTaskId(TASK_ID);
    await store.reload();
    expect(fetchSentences).toHaveBeenCalledWith(TASK_ID);
    expect(fetchVoiceOptions).toHaveBeenCalledWith(TASK_ID);
    expect(store.sentences).toHaveLength(3);
    expect(store.speakers).toEqual(["bigbear", "littlebear"]);
  });

  it("草稿：同一个任务号刷新**不覆盖**用户刚选的框，换任务号才重建", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    expect(store.draft["bigbear"]).toBe(HUIHUI);

    store.draft["bigbear"] = ZIRA;
    await store.reload();
    expect(store.draft["bigbear"]).toBe(ZIRA);

    store.setTaskId("ui-20260916-130000");
    await store.reload();
    expect(store.draft["bigbear"]).toBe(HUIHUI);
  });

  it("首屏拉不到就留着上一次的内容，只挂一条人话", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    fetchSentences = vi.fn(async () => {
      throw new ApiError("后端没起来", 0, null);
    });
    configureVoiceApi({ fetchSentences });
    await store.reload();
    expect(store.loadError).toContain("后端没起来");
    expect(store.sentences).toHaveLength(3);
  });

  it("有句子在念时轮询，念完自己停（空闲时不再每秒问一次）", async () => {
    vi.useFakeTimers();
    fetchSentences = vi.fn(async () =>
      snapshot({ progress: progress({ done: 1, pending: 2, settled: 1, ratio: 0.33 }) }),
    );
    configureVoiceApi({ fetchSentences });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchSentences).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(VOICE_POLL_MS);
    expect(fetchSentences).toHaveBeenCalledTimes(2);

    fetchSentences = vi.fn(async () => snapshot());
    configureVoiceApi({ fetchSentences });
    await vi.advanceTimersByTimeAsync(VOICE_POLL_MS);
    expect(fetchSentences).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(VOICE_POLL_MS * 3);
    expect(fetchSentences).toHaveBeenCalledTimes(1);
    store.stop();
  });

  it("念完之后 stop() 一定要停：切走面板还在问后端是白烧电", async () => {
    vi.useFakeTimers();
    fetchSentences = vi.fn(async () =>
      snapshot({ progress: progress({ done: 1, pending: 2, settled: 1, ratio: 0.33 }) }),
    );
    configureVoiceApi({ fetchSentences });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    store.stop();
    await vi.advanceTimersByTimeAsync(VOICE_POLL_MS * 3);
    expect(fetchSentences).toHaveBeenCalledTimes(1);
  });

  it("重配一句：投递成功 ⇒ 回话 + 重新拉一次（念完自己出现）", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    fetchSentences.mockClear();

    const ok = await store.resynth(store.sentences[0]);
    expect(ok).toBe(true);
    expect(resynthSentence).toHaveBeenCalledWith("01J000000000000000000000S1");
    expect(store.notice).toContain("第 1 句已退回待办");
    expect(store.error).toBeNull();
    expect(store.hint).toBe(HINT);
    expect(fetchSentences).toHaveBeenCalledTimes(1);
  });

  it("正被念着的句子不发请求：那一定是 409，白跑一趟还让人以为按钮坏了", async () => {
    fetchSentences = vi.fn(async () =>
      snapshot({ sentences: [sentence({ tts_status: "synthesizing", can_resynth: false })] }),
    );
    configureVoiceApi({ fetchSentences });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const ok = await store.resynth(store.sentences[0]);
    expect(ok).toBe(false);
    expect(resynthSentence).not.toHaveBeenCalled();
    expect(store.error).toContain("正被念着");
  });

  it("重配失败照原样报出来（含 HTTP 状态），不吞", async () => {
    resynthSentence = vi.fn(async () => {
      throw new ApiError("这一句已经被删掉了", 404, null);
    });
    configureVoiceApi({ resynthSentence });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    await store.resynth(store.sentences[0]);
    expect(store.error).toBe("这一句已经被删掉了（HTTP 404）");
  });

  it("换音色第一步：没有改动就不发请求（按了没反应比按钮是灰的更糟）", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const ok = await store.requestVoiceChange();
    expect(ok).toBe(false);
    expect(patchVoiceMap).not.toHaveBeenCalled();
    expect(store.notice).toContain("没有改动");
  });

  it("换音色第一步：409 先算代价 ⇒ 弹确认框，**这一趟什么都没写**", async () => {
    patchVoiceMap = vi.fn(async () => {
      throw confirmError();
    });
    configureVoiceApi({ patchVoiceMap });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    store.draft["bigbear"] = ZIRA;

    const ok = await store.requestVoiceChange();
    expect(ok).toBe(false);
    expect(patchVoiceMap).toHaveBeenCalledWith(TASK_ID, {
      voice_map: { bigbear: ZIRA },
      confirm: false,
    });
    expect(store.confirm?.affected).toBe(2);
    expect(store.error).toBeNull();
    expect(store.voiceMap["bigbear"]).toBe(HUIHUI);
  });

  it("换音色第二步：点头才带 confirm 真写；busy 的句子单独挂黄条", async () => {
    patchVoiceMap = vi
      .fn()
      .mockRejectedValueOnce(confirmError())
      .mockResolvedValueOnce(mapReport({ busy: ["01J000000000000000000000S3"] }));
    configureVoiceApi({ patchVoiceMap });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    store.draft["bigbear"] = ZIRA;

    await store.requestVoiceChange();
    // 后端真的写下去了 ⇒ 再拉一次拿到的是**新的**映射、而且是"还有句子没定局"的状态
    // （这里照实模拟：假件糊弄过去，测的就是假件而不是代码）
    fetchSentences = vi.fn(async () =>
      snapshot({
        voice_map: { bigbear: ZIRA, littlebear: ZIRA },
        progress: progress({ done: 1, pending: 2, settled: 1, ratio: 0.33 }),
        timeline_stale: true,
      }),
    );
    configureVoiceApi({ fetchSentences });
    const ok = await store.confirmVoiceChange();
    expect(ok).toBe(true);
    expect(patchVoiceMap).toHaveBeenLastCalledWith(TASK_ID, {
      voice_map: { bigbear: ZIRA },
      confirm: true,
    });
    expect(store.confirm).toBeNull();
    expect(store.notice).toContain("已重排 2 句");
    expect(store.warn).toContain("第 3 句");
    expect(store.voiceMap["bigbear"]).toBe(ZIRA);
    expect(store.timelineStale).toBe(true);
    store.stop();
  });

  it("确认框上点「算了」：框收起来，什么都不发", async () => {
    patchVoiceMap = vi.fn(async () => {
      throw confirmError();
    });
    configureVoiceApi({ patchVoiceMap });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    store.draft["bigbear"] = ZIRA;
    await store.requestVoiceChange();

    store.cancelVoiceChange();
    expect(store.confirm).toBeNull();
    expect(patchVoiceMap).toHaveBeenCalledTimes(1);
  });

  it("换音色撞上别的 409（不是「要确认」那种）：照常报错，不弹确认框", async () => {
    patchVoiceMap = vi.fn(async () => {
      throw new ApiError("voice 池现在是暂停的", 409, { code: "POOL_PAUSED" });
    });
    configureVoiceApi({ patchVoiceMap });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    store.draft["bigbear"] = ZIRA;

    await store.requestVoiceChange();
    expect(store.confirm).toBeNull();
    expect(store.error).toContain("voice 池现在是暂停的");
  });

  it("还原：下拉框退回库里的映射，确认框一起收起来", async () => {
    patchVoiceMap = vi.fn(async () => {
      throw confirmError();
    });
    configureVoiceApi({ patchVoiceMap });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    store.draft["bigbear"] = ZIRA;
    await store.requestVoiceChange();
    expect(store.changes).toEqual(["bigbear"]);

    store.resetDraft();
    expect(store.changes).toEqual([]);
    expect(store.confirm).toBeNull();
  });
});

// ══════════════════════════════════════════════════════════════════════
// 试听样本（T2.4）
// ══════════════════════════════════════════════════════════════════════

function previewSample(overrides: Partial<VoicePreview> = {}): VoicePreview {
  return {
    voice_id: HUIHUI,
    status: "ready",
    url: "/api/v1/media/voice_preview/huihui_12345678.wav",
    duration_ms: 2_400,
    engine: "cosyvoice2",
    generated_at: "2026-09-19T06:00:00.000Z",
    error: null,
    note: null,
    ...overrides,
  };
}

describe("试听：纯函数", () => {
  it("五态各有各的字：`missing` / `failed` / `stale` 不能都写成「试听」", () => {
    expect(previewLabel("missing")).toBe("生成试听");
    expect(previewLabel("running")).toBe("生成中…");
    expect(previewLabel("ready")).toBe("试听");
    expect(previewLabel("failed")).toBe("重新生成");
    expect(previewLabel("whatever")).toBe("试听");
  });

  it("`stale` 写「重新生成」：盘上那份是上一版参考音念的，点它不该直接播", () => {
    expect(previewLabel("stale")).toBe("重新生成");
    expect(previewHint("stale", "cosyvoice2")).toContain("参考音");
  });

  it("提示里必须写出「这一份是哪台引擎念的」——换个引擎是另一个人的嗓子", () => {
    expect(previewHint("ready", "cosyvoice2")).toContain("cosyvoice2");
    // 引擎名不知道时**不编一个**（只说"播这个音色的样本"）
    expect(previewHint("ready", null)).not.toContain("undefined");
    expect(previewHint("running", null)).toContain("十几秒");
    expect(previewHint("failed", null)).toContain("重试");
    expect(previewHint("missing", null)).toContain("点一下");
  });
});

describe("试听：动作", () => {
  it("已经有样本：只查一次，**不**触发合成（真念一次要十几秒）", async () => {
    fetchVoicePreview = vi.fn(async () => previewSample());
    createVoicePreview = vi.fn(async () => previewSample());
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const url = await store.previewVoice(HUIHUI);
    expect(url).toBe(previewSample().url);
    expect(createVoicePreview).not.toHaveBeenCalled();
    expect(store.previewError).toBeNull();
  });

  it("`stale`（盘上那份是上一版参考音念的）：**要**重新生成，不能拿它当现成的播", async () => {
    // 后端把「同名换了参考音」报成 `stale`（而不是 `ready`）：盘上有文件、能播，
    // 但那是旧嗓子。前端要是照 `ready` 处理，用户点一下就听见一个不对的声音，
    // 而面板上没有任何东西提示「这份是旧的」。
    fetchVoicePreview = vi.fn(async () => previewSample({ status: "stale" }));
    createVoicePreview = vi.fn(async () => previewSample());
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const url = await store.previewVoice(HUIHUI);

    expect(createVoicePreview).toHaveBeenCalledWith(HUIHUI);
    expect(url).toBe(previewSample().url);
  });

  it("没有样本：点生成 → 轮询 → 拿到 url，并把状态写回下拉框那一行", async () => {
    vi.useFakeTimers();
    // 第一次查：盘上没有 ⇒ 面板去点生成；生成完了再查：好了
    fetchVoicePreview = vi
      .fn<() => Promise<VoicePreview>>()
      .mockResolvedValueOnce(previewSample({ status: "missing", url: null }))
      .mockResolvedValueOnce(previewSample());
    createVoicePreview = vi.fn(async () => previewSample({ status: "running", url: null }));
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const pending = store.previewVoice(HUIHUI);
    await vi.advanceTimersByTimeAsync(PREVIEW_POLL_MS * 3);
    const url = await pending;

    expect(createVoicePreview).toHaveBeenCalledWith(HUIHUI);
    expect(url).toBe(previewSample().url);
    // 按钮下一次重绘要变成「试听」——状态不写回的话它会一直显示「生成试听」
    expect(store.previewState(HUIHUI)).toBe("ready");
    expect(store.previewId).toBeNull();
    expect(store.previewBusy).toBe(false);
  });

  it("别人已经在生成了（`running`）：**不再 POST 一次**，直接接着轮询", async () => {
    fetchVoicePreview = vi
      .fn<() => Promise<VoicePreview>>()
      .mockResolvedValueOnce(previewSample({ status: "running", url: null }))
      .mockResolvedValueOnce(previewSample());
    createVoicePreview = vi.fn(async () => previewSample({ status: "running", url: null }));
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const url = await store.previewVoice(HUIHUI);
    expect(url).toBe(previewSample().url);
    expect(createVoicePreview).not.toHaveBeenCalled();
  });

  it("生成失败：把引擎那句话原样带出来，而不是只说「失败了」", async () => {
    fetchVoicePreview = vi.fn(async () => previewSample({ status: "missing", url: null }));
    createVoicePreview = vi.fn(async () =>
      previewSample({ status: "failed", url: null, error: "TTS_ENGINE_DOWN: 服务连不上" }),
    );
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    expect(await store.previewVoice(HUIHUI)).toBeNull();
    expect(store.previewError).toContain("TTS_ENGINE_DOWN");
    expect(store.previewState(HUIHUI)).toBe("failed");
  });

  it("后端拒了（音色本机没有 / 当前引擎念不出来）：照常报错，不假装在生成", async () => {
    fetchVoicePreview = vi.fn(async () => previewSample({ status: "missing", url: null }));
    createVoicePreview = vi.fn(async () => {
      throw new ApiError("本机没有这个音色：ghost", 422, { code: "TTS_VOICE_MISSING" });
    });
    configureVoiceApi({ fetchVoicePreview, createVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    expect(await store.previewVoice("ghost")).toBeNull();
    expect(store.previewError).toContain("本机没有这个音色");
    expect(store.previewBusy).toBe(false);
  });

  it("角色还没选音色：**不发请求**，直接说清楚要做什么", async () => {
    fetchVoicePreview = vi.fn(async () => previewSample());
    configureVoiceApi({ fetchVoicePreview });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    expect(await store.previewVoice("  ")).toBeNull();
    expect(fetchVoicePreview).not.toHaveBeenCalled();
    expect(store.previewError).toContain("还没有选音色");
  });
});

// ══════════════════════════════════════════════════════════════════════
// 「开始配音」：面板上那颗主按钮（T4.5 补的那个入口）
// ══════════════════════════════════════════════════════════════════════

describe("开始配音", () => {
  it("纯函数：投了 0 条说的是幂等，不是「失败了」", () => {
    expect(startNotice(enqueueReport())).toContain("已把 3 句排进 voice 池");
    expect(startNotice(enqueueReport({ queued: 0 }))).toContain("这次新排了 0 条");
    expect(startNotice(enqueueReport({ queued: 0, outstanding: 0 }))).toContain(
      "这条任务没有待投的句子",
    );
  });

  it("★ 一次投递整条任务：调 enqueue_voice + 回话 + 重新拉一次", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    fetchSentences.mockClear();

    const ok = await store.startVoicing();

    expect(ok).toBe(true);
    expect(enqueueVoice).toHaveBeenCalledWith(TASK_ID);
    expect(store.notice).toContain("已把 3 句排进 voice 池");
    expect(store.error).toBeNull();
    expect(store.hint).toBe(ENQUEUE_HINT);
    expect(fetchSentences).toHaveBeenCalledTimes(1);
  });

  it("投了 0 条也要说清是幂等，不是按钮坏了", async () => {
    enqueueVoice = vi.fn(async () => enqueueReport({ queued: 0 }));
    configureVoiceApi({ enqueueVoice });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    await store.startVoicing();

    expect(store.notice).toContain("这次新排了 0 条");
    expect(store.notice).toContain("幂等");
  });

  it("canStart 只看**任务状态**：句子全定局但停在待配音时，那颗按钮还在", async () => {
    fetchSentences = vi.fn(async () =>
      snapshot({ task_status: "queued_voice", progress: progress({ done: 3, settled: 3 }) }),
    );
    configureVoiceApi({ fetchSentences });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    expect(store.canStart).toBe(true);
    expect(store.toEnqueue).toBe(0);
  });

  it("投完之后 canStart 关掉、voiced 打开（念完了 ⇒ 该收口出片了）", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();
    expect(store.canStart).toBe(false);
    expect(store.voiced).toBe(true); // 假快照就是"三句都 done、任务 voicing"

    fetchSentences = vi.fn(async () =>
      snapshot({ task_status: "queued_voice", progress: progress({ done: 0, pending: 3, ratio: 0 }) }),
    );
    configureVoiceApi({ fetchSentences });
    await store.refresh();
    expect(store.canStart).toBe(true);
    expect(store.toEnqueue).toBe(3);
    expect(store.voiced).toBe(false);
  });

  it("失败照原样报出来（含 HTTP 状态），不吞", async () => {
    enqueueVoice = vi.fn(async () => {
      throw new ApiError("任务现在停在 voicing，不在待配音这一步", 409, null);
    });
    configureVoiceApi({ enqueueVoice });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const ok = await store.startVoicing();

    expect(ok).toBe(false);
    expect(store.error).toBe("任务现在停在 voicing，不在待配音这一步（HTTP 409）");
  });
});

describe("收口并出片", () => {
  it("★ 提交一条一键出片（until=completed）—— 母带还没拼，去渲染会失败", async () => {
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const ok = await store.finishToVideo();

    expect(ok).toBe(true);
    expect(createPipelineJob).toHaveBeenCalledWith({ task_id: TASK_ID, until: "completed" });
    expect(store.notice).toContain("p0001");
    expect(store.notice).toContain("final.mp4");
  });

  it("已经有一条在跑 ⇒ 说清是复用的，不是开了第二条", async () => {
    createPipelineJob = vi.fn(async () => pipelineSubmit({ deduped: true }));
    configureVoiceApi({ createPipelineJob });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    await store.finishToVideo();

    expect(store.notice).toContain("已经有一条在跑了");
  });

  it("提交失败照原样报出来，不吞", async () => {
    createPipelineJob = vi.fn(async () => {
      throw new ApiError("任务停在确认闸（awaiting_approval），流水线不代按", 409, null);
    });
    configureVoiceApi({ createPipelineJob });
    const store = useVoiceStore();
    store.setTaskId(TASK_ID);
    await store.reload();

    const ok = await store.finishToVideo();

    expect(ok).toBe(false);
    expect(store.error).toBe("任务停在确认闸（awaiting_approval），流水线不代按（HTTP 409）");
  });
});

