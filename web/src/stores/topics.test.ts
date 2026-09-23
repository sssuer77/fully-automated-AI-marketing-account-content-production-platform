import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AnalyzeResult,
  ClearTopicsResult,
  DirectionDeleteResult,
  DirectionEditResult,
  DirectionItem,
  DirectionList,
  DraftReviewResult,
  HotImportResult,
  IdeateResult,
  ImportResult,
  ManualDirectionBody,
  ManualDirectionResult,
  ManualTopicResult,
  NewsPullResult,
  OutlineItem,
  OutlineResult,
  OutlineView,
  SelectFailure,
  TopicDeleteResult,
  TopicEditResult,
  SelectResult,
  TopicItem,
  TopicList,
} from "@/api/endpoints/topics";
import { ApiError } from "@/api/http";
import {
  configureTopicsApi,
  describeError,
  groupByDirection,
  hookLabel,
  isTopicEvent,
  newsFacts,
  scoreTone,
  sortByScore,
  summarizeClear,
  summarizeEdit,
  summarizeImport,
  summarizeNews,
  summarizeSelect,
  topicStatusTone,
  useTopicsStore,
  type TopicsApi,
} from "./topics";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function topic(id: string, overrides: Partial<TopicItem> = {}): TopicItem {
  return {
    id,
    direction_id: "d1",
    seq: 1,
    title: `选题 ${id}`,
    angle: "",
    hook_type: null,
    exec_feasible: true,
    score: null,
    reason: null,
    dedup_hash: null,
    similar_to: [],
    status: "candidate",
    task_id: null,
    selected_at: null,
    selected_by: null,
    created_at: null,
    ...overrides,
  };
}

function direction(id: string, overrides: Partial<DirectionItem> = {}): DirectionItem {
  return {
    id,
    batch_id: "b1",
    seq: 1,
    title: `方向 ${id}`,
    rationale: "因为所以",
    grounded_on: [],
    priority: 100,
    risk_flags: [],
    status: "open",
    created_at: null,
    topic_count: 0,
    selected_count: 0,
    ...overrides,
  };
}

function directionAdded(
  item: DirectionItem,
  overrides: Partial<ManualDirectionResult> = {},
): ManualDirectionResult {
  return { direction: item, ...overrides };
}

function directionEdited(
  item: DirectionItem,
  changed: string[],
): DirectionEditResult {
  return { direction: item, changed };
}

function directionDeleted(
  overrides: Partial<DirectionDeleteResult> = {},
): DirectionDeleteResult {
  return {
    direction_id: "d1",
    title: "方向 d1",
    deleted: true,
    cascaded_topics: 3,
    detached_task_count: 0,
    ...overrides,
  };
}

function drafted(overrides: Partial<DraftReviewResult> = {}): DraftReviewResult {
  return {
    ok: true,
    topic_id: "tp1",
    task_id: "t-tp1",
    script_id: "sc1",
    title: "熊大跑酷翻车那一下，问题出在起跳前",
    sentence_count: 20,
    word_count: 700,
    task_status: "reviewing",
    reused: false,
    warnings: [],
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

function pool(items: TopicItem[]): TopicList {
  return { status: "candidate", topics: items, counts: { candidate: items.length }, limit: 200 };
}

function directions(items: DirectionItem[], batches: string[] = ["b1"]): DirectionList {
  return { batch_id: batches[0] ?? null, batches, directions: items, counts: {} };
}

function selected(ids: readonly string[], failed: SelectFailure[] = []): SelectResult {
  return {
    requested: ids.length,
    selected: ids.map((id) => ({
      topic_id: id,
      task_id: `t-${id}`,
      title: `选题 ${id}`,
      created_task: true,
      task_status: "pending",
      topic_status: "queued",
      selected_by: "user",
      draft: null,
    })),
    failed,
    drafted: 0,
    draft_failed: 0,
  };
}

function analyzed(overrides: Partial<AnalyzeResult> = {}): AnalyzeResult {
  return {
    ok: true,
    batch_id: "b1",
    direction_count: 2,
    directions: [
      {
        id: "d1",
        batch_id: "b1",
        seq: 1,
        title: "方向 1",
        rationale: "因为所以",
        grounded_on: [],
        priority: 100,
        risk_flags: [],
        status: "open",
        created_at: null,
      },
    ],
    rule_report: null,
    hot_imported: 3,
    hot_bad: 0,
    feedback_imported: 0,
    feedback_classified: 0,
    hot_consumed: 0,
    archived: [],
    warnings: [],
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

function ideated(overrides: Partial<IdeateResult> = {}): IdeateResult {
  return {
    ok: true,
    batch_id: "b1",
    direction_count: 1,
    inserted: 4,
    dropped: 1,
    demoted: 0,
    outcomes: [
      {
        direction_id: "d1",
        title: "方向 1",
        ok: true,
        inserted: 4,
        dropped: 1,
        demoted: 0,
        error_code: null,
        error_message: null,
      },
    ],
    ...overrides,
  };
}

function manual(overrides: Partial<ManualTopicResult> = {}): ManualTopicResult {
  return {
    topic_id: "tp-manual",
    direction_id: "manual",
    title: "我加的选题",
    hook_type: null,
    score: null,
    similar_to: [],
    warnings: [],
    ...overrides,
  };
}

function imported(overrides: Partial<ImportResult> = {}): ImportResult {
  return {
    kind: "hot",
    files: ["a.md"],
    parsed: 3,
    bad: 0,
    inserted: 3,
    skipped: 0,
    issues: [],
    ...overrides,
  };
}

function hotResult(overrides: Partial<HotImportResult> = {}): HotImportResult {
  return { hot: imported(), feedback: null, ...overrides };
}

function deleted(overrides: Partial<TopicDeleteResult> = {}): TopicDeleteResult {
  return {
    topic_id: "tp1",
    title: "选题 tp1",
    deleted: true,
    detached_task_id: null,
    ...overrides,
  };
}

function edited(overrides: Partial<TopicEditResult> = {}): TopicEditResult {
  return {
    topic: topic("tp1", { title: "改之后" }),
    changed: ["title"],
    warnings: [],
    ...overrides,
  };
}

function outlineItem(overrides: Partial<OutlineItem> = {}): OutlineItem {
  return {
    topic_id: "tp1",
    title: "熊大跑酷翻车那一下，问题出在起跳前",
    core_argument: "新手翻车不是因为手速，是因为起跳前没看落脚点",
    llm_model: "gpt-5.6-terra",
    prompt_version: "1+abc",
    updated_at: null,
    ...overrides,
  };
}

function outlineView(outline: OutlineItem | null = outlineItem()): OutlineView {
  return { topic_id: "tp1", outline };
}

function outlineResult(overrides: Partial<OutlineResult> = {}): OutlineResult {
  return {
    ok: true,
    topic_id: "tp1",
    title: "熊大跑酷翻车那一下，问题出在起跳前",
    core_argument: "新手翻车不是因为手速，是因为起跳前没看落脚点",
    llm_model: "gpt-5.6-terra",
    prompt_version: "1+abc",
    generated: true,
    changed: ["title", "core_argument"],
    warnings: [],
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

function clearResult(overrides: Partial<ClearTopicsResult> = {}): ClearTopicsResult {
  return {
    dry_run: true,
    directions: 4,
    topics: 13,
    detached_task_count: 1,
    ...overrides,
  };
}

function newsResult(overrides: Partial<NewsPullResult> = {}): NewsPullResult {
  return {
    ok: true,
    source: "今日头条热榜",
    fetched: 50,
    evaluated: 25,
    batch_id: "b1",
    kept_count: 1,
    kept: [direction("d9", { title: "新闻挑出来的方向" })],
    skipped: [{ title: "某条没写稿价值的新闻", reason: "没有冲突，写不出钩子" }],
    warnings: [],
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

/** 装一整套假件（**每次都给全** ⇒ 上一个用例的覆盖不会漏到下一个）。 */
function install(overrides: Partial<TopicsApi> = {}): TopicsApi {
  const fakes: TopicsApi = {
    fetchTopics: vi.fn(async () => pool([topic("tp1"), topic("tp2")])),
    fetchDirections: vi.fn(async () => directions([direction("d1"), direction("d2")])),
    // 回一个**带着提交内容**的方向：面板显示的是后端回的那份，不是自己拼的
    createDirection: vi.fn(async (body: ManualDirectionBody) =>
      directionAdded(direction("d3", { title: body.title, rationale: body.rationale })),
    ),
    updateDirection: vi.fn(async () =>
      directionEdited(direction("d1", { title: "改之后" }), ["title"]),
    ),
    deleteDirection: vi.fn(async () => directionDeleted()),
    draftTopicForReview: vi.fn(async () => drafted()),
    analyzeTopics: vi.fn(async () => analyzed()),
    ideateTopics: vi.fn(async () => ideated()),
    selectTopics: vi.fn(async (ids: readonly string[]) => selected(ids)),
    addManualTopic: vi.fn(async () => manual()),
    fetchTopicOutline: vi.fn(async () => outlineView()),
    generateTopicOutline: vi.fn(async () => outlineResult()),
    saveTopicOutline: vi.fn(async () => outlineResult()),
    clearTopicOutline: vi.fn(async () => outlineResult()),
    updateTopic: vi.fn(async () => edited()),
    deleteTopic: vi.fn(async () => deleted()),
    importHot: vi.fn(async () => hotResult()),
    submitHot: vi.fn(async () => hotResult()),
    pullTodayNews: vi.fn(async () => newsResult()),
    clearTopics: vi.fn(async () => clearResult()),
    ...overrides,
  };
  configureTopicsApi(fakes);
  return fakes;
}

let fakes: TopicsApi;

beforeEach(() => {
  setActivePinia(createPinia());
  fakes = install();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态）
// ══════════════════════════════════════════════════════════════════════

describe("瀑布流排序", () => {
  it("分高的在前，同分按 seq（与后端同一条比较器）", () => {
    const sorted = sortByScore([
      topic("a", { score: 6.0, seq: 2 }),
      topic("b", { score: 9.1, seq: 5 }),
      topic("c", { score: 6.0, seq: 1 }),
    ]);
    expect(sorted.map((item) => item.id)).toEqual(["b", "c", "a"]);
  });

  it("没打分的排最后（不是当成 0 分）", () => {
    const sorted = sortByScore([topic("a", { score: null }), topic("b", { score: 1.0 })]);
    expect(sorted.map((item) => item.id)).toEqual(["b", "a"]);
  });

  it("不改原数组（调用点拿到的还是它自己那份）", () => {
    const source = [topic("a", { score: 1 }), topic("b", { score: 9 })];
    sortByScore(source);
    expect(source.map((item) => item.id)).toEqual(["a", "b"]);
  });
});

describe("按方向分组", () => {
  it("组内保持原序，空方向也保留（「这个方向一条都没出」本身就是信息）", () => {
    const groups = groupByDirection(
      [direction("d1"), direction("d2")],
      [topic("t1", { direction_id: "d1" }), topic("t2", { direction_id: "d1" })],
    );
    expect(groups).toHaveLength(2);
    expect(groups[0]?.topics.map((item) => item.id)).toEqual(["t1", "t2"]);
    expect(groups[1]?.topics).toEqual([]);
  });

  it("归不进当前批次的选题不丢，收进末尾（人工加选题挂在固定批次 manual 上）", () => {
    const groups = groupByDirection([direction("d1")], [topic("t1", { direction_id: "manual" })]);
    expect(groups).toHaveLength(2);
    expect(groups[1]?.direction).toBeNull();
    expect(groups[1]?.topics.map((item) => item.id)).toEqual(["t1"]);
  });

  it("没有孤儿时不占位", () => {
    const groups = groupByDirection([direction("d1")], [topic("t1", { direction_id: "d1" })]);
    expect(groups).toHaveLength(1);
  });
});

describe("标签与色调", () => {
  it("钩子没标注就是没标注，不编一个", () => {
    expect(hookLabel(null)).toBe("未标注");
    expect(hookLabel("")).toBe("未标注");
    expect(hookLabel("conflict")).toBe("冲突");
    expect(hookLabel("unknown-kind")).toBe("unknown-kind");
  });

  it("评分色调：8 以上绿 / 6 以上黄 / 其余红 / 没打分灰", () => {
    expect(scoreTone(9)).toBe("ok");
    expect(scoreTone(6.5)).toBe("warn");
    expect(scoreTone(3)).toBe("error");
    expect(scoreTone(null)).toBe("idle");
  });

  it("状态色调：已入队绿 / 已选忙 / 淘汰红 / 过期黄 / 其余灰", () => {
    expect(topicStatusTone("queued")).toBe("ok");
    expect(topicStatusTone("selected")).toBe("busy");
    expect(topicStatusTone("rejected")).toBe("error");
    expect(topicStatusTone("expired")).toBe("warn");
    expect(topicStatusTone("candidate")).toBe("idle");
  });
});

describe("WS 事件过滤", () => {
  it("只认 topics 通道自己的四类", () => {
    for (const kind of [
      "direction.batch_ready",
      "topic.batch_ready",
      "topic.selected",
      "topic.dedup_warn",
    ]) {
      expect(isTopicEvent(kind)).toBe(true);
    }
  });

  it("`task.transition` 不算（那是稿件面板的事，为它重拉选题池纯属噪音）", () => {
    expect(isTopicEvent("task.transition")).toBe(false);
    expect(isTopicEvent(undefined)).toBe(false);
    expect(isTopicEvent(42)).toBe(false);
  });
});

describe("结果小结", () => {
  it("导入小结把热点与反馈分开说", () => {
    expect(summarizeImport(imported(), null)).toBe("热点 +3（坏行 0）");
    expect(summarizeImport(imported(), imported({ kind: "feedback", inserted: 2, bad: 1 }))).toBe(
      "热点 +3（坏行 0） · 反馈 +2（坏行 1）",
    );
    expect(summarizeImport(null, null)).toBe("没有可导入的输入源");
  });

  it("入队小结成功与失败都要出现（只说「部分失败」等于让人去猜）", () => {
    expect(summarizeSelect(3, 0, 0)).toBe("已入队 3 条");
    expect(summarizeSelect(3, 0, 2)).toBe("已入队 3 条 · 其中 2 条已顺手写稿");
    expect(summarizeSelect(3, 1, 0)).toBe("已入队 3 条 · 失败 1 条（逐条原因见下）");
  });

  it("新闻小结把「评测了几条」与「留下几个方向」分开说（后者才是用户关心的）", () => {
    expect(summarizeNews(newsResult({ skipped: [] }))).toBe(
      "今日头条热榜：评测 25 条 · 留下 1 个方向",
    );
    expect(summarizeNews(newsResult({ kept_count: 0, kept: [], skipped: [] }))).toBe(
      "今日头条热榜：评测 25 条 · 一条都没挑中",
    );
    expect(summarizeNews(newsResult({ source: "" }))).toBe(
      "今日新闻：评测 25 条 · 留下 1 个方向 · 跳过 1 条",
    );
  });

  it("「事件」只取新闻那一条依据（别的依据是出处，不是事实）", () => {
    expect(
      newsFacts([
        { type: "hot", kind: "news", quote: "一家三口一氧化碳中毒身亡。" },
      ]),
    ).toBe("一家三口一氧化碳中毒身亡。");
    // 账号定位 / 热点池 / 历史反馈都是**出处**，画在「事件」那一行会误导人
    expect(
      newsFacts([
        { type: "persona", quote: "账号定位就是跑酷" },
        { type: "hot", ref_id: "h1", quote: "某条热点" },
        { type: "feedback", kind: "want", quote: "想看更多跑酷" },
      ]),
    ).toBe("");
    // 多条事实之间换行（后端 direction_facts 也是这么拼给提示词的）
    expect(
      newsFacts([
        { type: "hot", kind: "news", quote: "第一句" },
        { type: "hot", kind: "news", quote: "第二句" },
      ]),
    ).toBe("第一句\n第二句");
    expect(newsFacts([])).toBe("");
  });

  it("请求失败翻成人话：带状态码，status=0 是超时/取消", () => {
    expect(describeError(new ApiError("选题池满了", 409, null))).toBe("选题池满了（HTTP 409）");
    expect(describeError(new ApiError("超时", 0, null))).toBe("超时");
    expect(describeError("boom")).toBe("boom");
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("重拉", () => {
  it("同时拉选题池与方向卡片，并把分数排好", async () => {
    fakes = install({
      fetchTopics: vi.fn(async () => pool([topic("a", { score: 5 }), topic("b", { score: 9 })])),
    });
    const store = useTopicsStore();
    await store.refresh();
    expect(store.topics.map((item) => item.id)).toEqual(["b", "a"]);
    expect(store.directions.map((item) => item.id)).toEqual(["d1", "d2"]);
    expect(store.batches).toEqual(["b1"]);
    expect(store.batchId).toBe("b1");
    expect(store.error).toBeNull();
  });

  it("把当前过滤与批次原样带给后端（切了批次再刷新不该跳回最近一批）", async () => {
    // 假件要**回显**请求的批次：真实后端会把解析后的 batch_id 回填，前端据此记住选择。
    fakes = install({
      fetchDirections: vi.fn(async (batchId?: string | null) =>
        directions([direction("d1")], [batchId ?? "b1"]),
      ),
    });
    const store = useTopicsStore();
    await store.setStatus("queued");
    await store.setBatch("b0");
    await store.refresh();
    expect(fakes.fetchTopics).toHaveBeenLastCalledWith("queued", 200);
    expect(fakes.fetchDirections).toHaveBeenLastCalledWith("b0");
    expect(store.batchId).toBe("b0");
  });

  it("拉取失败走 loadError（**不**占用动作级的 error），且不清空上一份数据", async () => {
    const store = useTopicsStore();
    await store.refresh();
    fakes = install({
      fetchTopics: vi.fn(async () => {
        throw new ApiError("数据库锁住了", 500, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toBe("数据库锁住了（HTTP 500）");
    expect(store.error).toBeNull();
    expect(store.topics).toHaveLength(2);
  });

  it("下一次拉成功就把 loadError 收回（不留下过期的红字）", async () => {
    const store = useTopicsStore();
    fakes = install({
      fetchTopics: vi.fn(async () => {
        throw new ApiError("数据库锁住了", 500, null);
      }),
    });
    await store.refresh();
    fakes = install();
    await store.refresh();
    expect(store.loadError).toBeNull();
  });

  it("动作级结论不会被紧跟着的成功刷新抹掉（这正是两条信道分开的原因）", async () => {
    fakes = install({
      analyzeTopics: vi.fn(async () => analyzed({ ok: false, error_message: "没有可用输入源" })),
    });
    const store = useTopicsStore();
    await store.analyze();
    expect(store.error).toBe("没有可用输入源");
    expect(store.loadError).toBeNull();
  });
});

describe("过滤与勾选", () => {
  it("换状态会清空勾选（跨状态勾选等于选了看不见的东西）", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    await store.setStatus("queued");
    expect(store.checked).toEqual([]);
    expect(store.pendingSelect).toBeNull();
  });

  it("换批次同样清空勾选", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    await store.setBatch("b0");
    expect(store.checked).toEqual([]);
  });

  it("勾选可加可撤", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.toggleChecked("tp2");
    store.toggleChecked("tp1");
    expect(store.checked).toEqual(["tp2"]);
    expect(store.hasSelection).toBe(true);
  });
});

describe("勾选入队（两步确认）", () => {
  it("第一步只摊开清单，**不发请求**", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp2");
    store.requestSelect();
    expect(store.pendingSelect?.map((item) => item.id)).toEqual(["tp2"]);
    expect(fakes.selectTopics).not.toHaveBeenCalled();
  });

  it("一条都没勾时只报错，不摊清单", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.requestSelect();
    expect(store.pendingSelect).toBeNull();
    expect(store.error).toBe("先勾选要入队的选题");
  });

  it("清单按瀑布流顺序，而不是勾选顺序（人对得上号）", async () => {
    fakes = install({
      fetchTopics: vi.fn(async () => pool([topic("a", { score: 9 }), topic("b", { score: 5 })])),
    });
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("b");
    store.toggleChecked("a");
    store.requestSelect();
    expect(store.pendingSelect?.map((item) => item.id)).toEqual(["a", "b"]);
  });

  it("第二步才发请求，成功后清空勾选并重拉", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.requestSelect();
    await store.confirmSelect();
    expect(fakes.selectTopics).toHaveBeenCalledWith(["tp1"], false);
    expect(store.checked).toEqual([]);
    expect(store.pendingSelect).toBeNull();
    expect(store.notice).toBe("已入队 1 条");
    expect(fakes.fetchTopics).toHaveBeenCalledTimes(2);
  });

  it("顺手写稿要显式勾上（默认不跑写稿）", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.setDraftNow(true);
    store.requestSelect();
    await store.confirmSelect();
    expect(fakes.selectTopics).toHaveBeenCalledWith(["tp1"], true);
  });

  it("部分失败要逐条留下 code 与补救建议，成功的那几条照旧算数", async () => {
    fakes = install({
      selectTopics: vi.fn(async (ids: readonly string[]) =>
        selected(
          ids.filter((id) => id !== "tp2"),
          [{ topic_id: "tp2", code: "TOPIC_SELECT_INVALID", message: "已入队", remediation: "去稿件面板看" }],
        ),
      ),
    });
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.toggleChecked("tp2");
    store.requestSelect();
    await store.confirmSelect();
    expect(store.notice).toBe("已入队 1 条 · 失败 1 条（逐条原因见下）");
    expect(store.failures.map((item) => item.code)).toEqual(["TOPIC_SELECT_INVALID"]);
    expect(store.failures[0]?.remediation).toBe("去稿件面板看");
  });

  it("没摊清单就确认 = 什么都不做（不会发一次空请求）", async () => {
    const store = useTopicsStore();
    await store.refresh();
    await store.confirmSelect();
    expect(fakes.selectTopics).not.toHaveBeenCalled();
  });

  it("入队被守卫拦下（409）时报人话，勾选不清空", async () => {
    fakes = install({
      selectTopics: vi.fn(async () => {
        throw new ApiError("方向分析正在运行，请等它跑完", 409, null);
      }),
    });
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.requestSelect();
    await store.confirmSelect();
    expect(store.error).toBe("方向分析正在运行，请等它跑完（HTTP 409）");
    expect(store.checked).toEqual(["tp1"]);
    expect(store.pendingSelect).not.toBeNull();
  });
});

describe("长任务", () => {
  it("分析成功时报方向数", async () => {
    const store = useTopicsStore();
    await store.analyze();
    expect(store.lastAnalyze?.direction_count).toBe(2);
    expect(store.notice).toBe("出了 2 个方向（热点 +3 条）");
    expect(store.error).toBeNull();
  });

  it("分析 ok=false 也是 200 ⇒ 看体内，用后端给的原因", async () => {
    fakes = install({
      analyzeTopics: vi.fn(async () =>
        analyzed({ ok: false, error_code: "HOT_TEXT_EMPTY", error_message: "没有可用输入源" }),
      ),
    });
    const store = useTopicsStore();
    await store.analyze();
    expect(store.error).toBe("没有可用输入源");
  });

  it("生成选题失败时逐方向的原因留在 lastIdeate 里（失败只影响自己）", async () => {
    fakes = install({
      ideateTopics: vi.fn(async () =>
        ideated({
          ok: false,
          outcomes: [
            {
              direction_id: "d2",
              title: "方向 2",
              ok: false,
              inserted: 0,
              dropped: 0,
              demoted: 0,
              error_code: "LLM_TIMEOUT",
              error_message: "网关超时",
            },
          ],
        }),
      ),
    });
    const store = useTopicsStore();
    await store.ideate();
    expect(store.error).toBe("选题生成没产出（逐方向原因见下）");
    expect(store.lastIdeate?.outcomes[0]?.error_code).toBe("LLM_TIMEOUT");
  });

  it("被单飞守卫拦下时保留后端那句话（409 不是「忙」能糊过去的）", async () => {
    fakes = install({
      analyzeTopics: vi.fn(async () => {
        throw new ApiError("选题生成正在运行，请等它跑完", 409, null);
      }),
    });
    const store = useTopicsStore();
    await store.analyze();
    expect(store.error).toBe("选题生成正在运行，请等它跑完（HTTP 409）");
  });
});

describe("人工加选题", () => {
  it("相似只提示、不拦（仍然算加成功）", async () => {
    fakes = install({
      addManualTopic: vi.fn(async () => manual({ similar_to: [{ title: "很像的一条", score: 8.2 }] })),
    });
    const store = useTopicsStore();
    const ok = await store.addManual({ title: "我加的选题", angle: "", score: null, reason: null });
    expect(ok).toBe(true);
    expect(store.notice).toBe("已入库；库里有 1 条很像的（只提示，不拦）");
    expect(store.manual?.similar_to).toHaveLength(1);
  });

  it("没有相似项时只说已入库", async () => {
    const store = useTopicsStore();
    await store.addManual({ title: "我加的选题", angle: "", score: null, reason: null });
    expect(store.notice).toBe("已入库");
  });
});

describe("输入源", () => {
  it("扫盘导入报热点与反馈两段", async () => {
    fakes = install({
      importHot: vi.fn(async () => hotResult({ hot: imported(), feedback: imported({ kind: "feedback" }) })),
    });
    const store = useTopicsStore();
    await store.scanHot();
    expect(store.notice).toBe("热点 +3（坏行 0） · 反馈 +3（坏行 0）");
  });

  it("空文本不发请求（省一次注定 422 的往返）", async () => {
    const store = useTopicsStore();
    const ok = await store.submitHotText("   \n  ", "hot");
    expect(ok).toBe(false);
    expect(store.error).toBe("先粘几行再提交");
    expect(fakes.submitHot).not.toHaveBeenCalled();
  });

  it("提交成功把原文交给后端（文件名服务端生成，客户端不参与）", async () => {
    const store = useTopicsStore();
    const ok = await store.submitHotText("某热点|9800|抖音", "hot");
    expect(ok).toBe(true);
    expect(fakes.submitHot).toHaveBeenCalledWith({ text: "某热点|9800|抖音", kind: "hot" });
    expect(store.notice).toBe("热点 +3（坏行 0）");
  });
});

describe("一键拉取今日新闻", () => {
  it("成功 ⇒ 小结当 notice、重拉一次（挑中的方向要当场出现在左栏）", async () => {
    const store = useTopicsStore();
    await store.pullNews();

    expect(store.newsBusy).toBe(false);
    expect(store.error).toBeNull();
    expect(store.notice).toBe("今日头条热榜：评测 25 条 · 留下 1 个方向 · 跳过 1 条");
    expect(store.lastNews?.kept_count).toBe(1);
    expect(fakes.fetchDirections).toHaveBeenCalled();
  });

  it("评测没跑起来 ⇒ error 用后端那句话，notice 不冒充小结", async () => {
    fakes = install({
      pullTodayNews: vi.fn(async () =>
        newsResult({
          ok: false,
          evaluated: 0,
          kept_count: 0,
          kept: [],
          skipped: [],
          error_message: "写作通道没配",
        }),
      ),
    });
    const store = useTopicsStore();
    await store.pullNews();

    expect(store.error).toBe("写作通道没配");
    expect(store.notice).toBeNull();
    expect(store.lastNews?.ok).toBe(false);
  });

  it("抓取失败（503）⇒ describeError 文案，闸要复位", async () => {
    fakes = install({
      pullTodayNews: vi.fn(async () => {
        throw new ApiError("今天的新闻一条都没抓到", 503, null);
      }),
    });
    const store = useTopicsStore();
    await store.pullNews();

    expect(store.error).toBe("今天的新闻一条都没抓到（HTTP 503）");
    expect(store.newsBusy).toBe(false);
  });

  it("它自己一把闸：跑新闻时不把别的长任务按钮冻住", async () => {
    const store = useTopicsStore();
    const running = store.pullNews();

    expect(store.newsBusy).toBe(true);
    expect(store.busy).toBe(false);
    await running;
    expect(store.newsBusy).toBe(false);
  });
});

describe("改选题 / 删选题", () => {
  it("小结把列名说成人话，认不出的列照原样显示", () => {
    expect(summarizeEdit(edited({ changed: ["hook_type", "weird"] }))).toBe("已改 钩子、weird");
  });

  it("改完重拉一次，并把改了哪几列说成人话", async () => {
    fakes = install({ updateTopic: vi.fn(async () => edited({ changed: ["score", "title"] })) });
    const store = useTopicsStore();
    await store.refresh();

    const ok = await store.editTopic("tp1", { title: "改之后", score: 7.5 });

    expect(ok).toBe(true);
    expect(store.notice).toBe("已改 分数、标题");
    expect(fakes.fetchTopics).toHaveBeenCalledTimes(2); // 初拉 + 改完重拉
    expect(store.saving).toBe(false);
  });

  it("一个字节都没动时说清楚是「没改动」，不是「改好了」", async () => {
    fakes = install({ updateTopic: vi.fn(async () => edited({ changed: [] })) });
    const store = useTopicsStore();
    await store.editTopic("tp1", { title: "改之后" });
    expect(store.notice).toBe("没有改动（值跟原来一样）");
  });

  it("改标题撞库 ⇒ 把相似提示带出来（仍然算改成功）", async () => {
    fakes = install({
      updateTopic: vi.fn(async () => edited({ warnings: ["与库内 1 条选题相似（最高 1.00）：老标题"] })),
    });
    const store = useTopicsStore();
    const ok = await store.editTopic("tp1", { title: "老标题" });
    expect(ok).toBe(true);
    expect(store.notice).toBe("已改 标题 · 与库内 1 条选题相似（最高 1.00）：老标题");
  });

  it("派生过任务也照删，小结里把那条任务号说清楚（删的是想法，不是活）", async () => {
    fakes = install({
      deleteTopic: vi.fn(async () => deleted({ detached_task_id: "t-tp1" })),
    });
    const store = useTopicsStore();

    expect(await store.removeTopic("tp1")).toBe(true);
    expect(store.error).toBeNull();
    expect(store.notice).toBe("已删除《选题 tp1》（任务 t-tp1 照跑，不受影响）");
  });

  it("删掉之后把它的勾选一并摘掉（不留一个指向已删行的 id）", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");

    const ok = await store.removeTopic("tp1");

    expect(ok).toBe(true);
    expect(store.notice).toBe("已删除《选题 tp1》");
    expect(store.checked).toEqual([]);
  });
});

describe("二级产物：视频标题 + 核心论点", () => {
  it("展开才拉（按需），拿到 null 也照样记下来", async () => {
    fakes = install({ fetchTopicOutline: vi.fn(async () => outlineView(null)) });
    const store = useTopicsStore();

    await store.loadOutline("tp1");

    expect(fakes.fetchTopicOutline).toHaveBeenCalledWith("tp1");
    expect(store.outlines["tp1"]).toBeNull(); // 拉过了、确实还没有 —— 不是 undefined
  });

  it("模型产出之后直接落进按选题索引的那张表", async () => {
    const store = useTopicsStore();

    const ok = await store.generateOutline("tp1");

    expect(ok).toBe(true);
    expect(store.outlines["tp1"]?.title).toBe("熊大跑酷翻车那一下，问题出在起跳前");
    expect(store.notice).toBe("二级已生成：《熊大跑酷翻车那一下，问题出在起跳前》");
    expect(store.outlineBusy).toBeNull();
  });

  it("产出失败（ok=false 也是 200）⇒ 报体内原因，不写空行", async () => {
    fakes = install({
      generateTopicOutline: vi.fn(async () =>
        outlineResult({ ok: false, title: "", core_argument: "", error_message: "Key 没配" }),
      ),
    });
    const store = useTopicsStore();

    const ok = await store.generateOutline("tp1");

    expect(ok).toBe(false);
    expect(store.error).toBe("Key 没配");
    expect(store.outlines["tp1"]).toBeUndefined();
  });

  it("手写定稿不调 LLM，并把定稿值写回本地", async () => {
    const store = useTopicsStore();

    const ok = await store.saveOutline("tp1", { title: "手写的标题", core_argument: "手写的论点" });

    expect(ok).toBe(true);
    expect(fakes.generateTopicOutline).not.toHaveBeenCalled();
    expect(store.outlines["tp1"]?.title).toBe("熊大跑酷翻车那一下，问题出在起跳前");
  });

  it("清空幂等：本来就没有时说「本来就没定」，不假装删掉了什么", async () => {
    fakes = install({
      clearTopicOutline: vi.fn(async () => outlineResult({ title: "", core_argument: "", changed: [] })),
    });
    const store = useTopicsStore();

    const ok = await store.clearOutline("tp1");

    expect(ok).toBe(true);
    expect(store.outlines["tp1"]).toBeNull();
    expect(store.notice).toBe("这一级本来就没定");
  });

  it("清空成功 ⇒ 说明三级会退回自由发挥（这一级是可选的）", async () => {
    const store = useTopicsStore();
    await store.clearOutline("tp1");
    expect(store.notice).toBe("二级已清空（三级会按选题自由发挥）");
  });
});


// ══════════════════════════════════════════════════════════════════════
// 方向 · 手写 / 改 / 删 / 只跑这一个（左列那一栏）
// ══════════════════════════════════════════════════════════════════════

describe("方向 · 手写", () => {
  it("落进当前批次并如实报出排到第几位", async () => {
    const store = useTopicsStore();
    const ok = await store.addDirection({
      title: "手写的方向",
      rationale: "先占位",
      priority: 100,
    });

    expect(ok).toBe(true);
    expect(fakes.createDirection).toHaveBeenCalledWith({
      title: "手写的方向",
      rationale: "先占位",
      priority: 100,
    });
    expect(store.notice).toContain("手写的方向");
    expect(store.directionBusy).toBeNull();
    expect(fakes.fetchDirections).toHaveBeenCalled(); // 左列重拉了
  });

  it("失败时把原因摆在 error 上，不写 notice", async () => {
    fakes = install({
      createDirection: vi.fn(async () => {
        throw new ApiError("标题太长", 422, null);
      }),
    });
    const store = useTopicsStore();

    expect(await store.addDirection({ title: "x", rationale: "", priority: 100 })).toBe(false);
    expect(store.error).toContain("422");
    expect(store.notice).toBeNull();
  });
});

describe("方向 · 改", () => {
  it("原样转发要改的字段（哪些字段变了由调用点判）", async () => {
    const store = useTopicsStore();
    expect(await store.editDirection("d1", { title: "改之后" })).toBe(true);

    expect(fakes.updateDirection).toHaveBeenCalledWith("d1", { title: "改之后" });
    expect(store.notice).toContain("改之后");
  });

  it("changed 为空 ⇒ 说「什么都没改」，不假装改了一次", async () => {
    fakes = install({
      updateDirection: vi.fn(async () => directionEdited(direction("d1"), [])),
    });
    const store = useTopicsStore();

    await store.editDirection("d1", { title: "方向 d1" });
    expect(store.notice).toBe("什么都没改");
  });
});

describe("方向 · 删（级联）", () => {
  it("把被一起删掉的候选条数如实说出来", async () => {
    const store = useTopicsStore();
    expect(await store.removeDirection("d1")).toBe(true);

    expect(fakes.deleteDirection).toHaveBeenCalledWith("d1");
    expect(store.notice).toContain("3 条候选");
    expect(store.lastDirection).toEqual(directionDeleted());
  });

  it("勾选里已经不存在的 id 要清掉（否则下一次入队会报一串「选题不存在」）", async () => {
    const store = useTopicsStore();
    await store.refresh();
    store.toggleChecked("tp1");
    store.toggleChecked("tp-gone");

    await store.removeDirection("d1");

    // 假件的选题池只有 tp1 / tp2 ⇒ 被级联删掉的那条从勾选里消失
    expect(store.checked).toEqual(["tp1"]);
  });

  it("候选上已经派生过任务 ⇒ 照删，并在小结里说清那些任务照跑", async () => {
    fakes = install({
      deleteDirection: vi.fn(async () => directionDeleted({ detached_task_count: 2 })),
    });
    const store = useTopicsStore();

    expect(await store.removeDirection("d1")).toBe(true);
    expect(store.error).toBeNull();
    expect(store.notice).toBe(
      "已删除方向《方向 d1》（一并删掉 3 条候选） · 其中 2 条已有任务，照跑",
    );
  });

  it("没有任务的小结不提任务（说了等于给一个不存在的东西留位置）", async () => {
    const store = useTopicsStore();
    await store.removeDirection("d1");
    expect(store.notice).toBe("已删除方向《方向 d1》（一并删掉 3 条候选）");
  });
});

describe("方向 · 只跑这一个", () => {
  it("只把这一个方向交给 Ideator，条数用面板上选的那个", async () => {
    const store = useTopicsStore();
    store.setPerDirection(8);
    await store.ideateOne("d1", "方向 d1");

    expect(fakes.ideateTopics).toHaveBeenCalledWith({
      per_direction: 8,
      direction_ids: ["d1"],
    });
    expect(store.notice).toContain("方向 d1");
    expect(store.busy).toBe(false);
    expect(store.directionBusy).toBeNull();
  });

  it("每方向条数不接受非法值（NaN / 0 都当没听见）", () => {
    const store = useTopicsStore();
    store.setPerDirection(6);
    store.setPerDirection(Number.NaN);
    store.setPerDirection(0);
    expect(store.perDirection).toBe(6);
  });
});

describe("一键清除所有选题", () => {
  it("预览（dryRun=true）⇒ 只报三个数，**不重拉**（库里一个字节都没动）", async () => {
    const store = useTopicsStore();
    await store.refresh();

    const preview = await store.clearAll(true);

    expect(fakes.clearTopics).toHaveBeenCalledWith(true);
    expect(preview?.dry_run).toBe(true);
    expect(store.notice).toBe("会清掉：4 个方向 · 13 条选题（其中 1 条已派生任务，删了也照跑）");
    expect(store.clearBusy).toBe(false);
    // 预览不动数据 ⇒ 重拉一次只是白跑一趟（还会把面板闪一下）
    expect(fakes.fetchTopics).toHaveBeenCalledTimes(1);
  });

  it("真删 ⇒ 用同一份读数说结果、重拉一次、**把勾选清空**", async () => {
    const store = useTopicsStore();
    await store.refresh();
    fakes = install({ clearTopics: vi.fn(async () => clearResult({ dry_run: false })) });
    store.toggleChecked("tp1");

    const result = await store.clearAll(false);

    expect(result?.dry_run).toBe(false);
    expect(store.notice).toBe("已清空：4 个方向 · 13 条选题（其中 1 条已派生任务，任务照跑）");
    expect(fakes.fetchTopics).toHaveBeenCalled();
    // 被删掉的那些可能正被勾着：留着已不存在的 id，下一次"入队"就会报一串"选题不存在"
    expect(store.checked).toEqual([]);
  });

  it("面板本来就空 ⇒ 不谎称「清掉了 0 个」（预览与真删两句不同）", async () => {
    // 假件要**跟着请求回 dry_run**（与真端点同一条）：写死 true 的话，"真删"那一次
    // 会被面板当成又一次预览，而那个 bug 只会在真机上出现。
    fakes = install({
      clearTopics: vi.fn(async (dryRun = true) =>
        clearResult({ dry_run: dryRun, directions: 0, topics: 0, detached_task_count: 0 }),
      ),
    });
    const store = useTopicsStore();

    await store.clearAll(true);
    expect(store.notice).toBe("面板上已经没有可清的东西");

    await store.clearAll(false);
    expect(store.notice).toBe("面板本来就是空的");
  });

  it("没有派生过任务的选题 ⇒ 不提任务（说了等于给一个不存在的东西留位置）", () => {
    expect(summarizeClear(clearResult({ detached_task_count: 0, dry_run: false }))).toBe(
      "已清空：4 个方向 · 13 条选题",
    );
  });

  it("取消 ⇒ 预览收起来（下一次点还是先预览）", async () => {
    const store = useTopicsStore();
    await store.clearAll(true);
    expect(store.lastClear).not.toBeNull();

    store.dismissClear();
    expect(store.lastClear).toBeNull();
  });

  it("被长任务守卫拦下（409）⇒ error 带后端那句话，闸要复位", async () => {
    fakes = install({
      clearTopics: vi.fn(async () => {
        throw new ApiError("选题生成正在运行，请等它跑完", 409, null);
      }),
    });
    const store = useTopicsStore();

    expect(await store.clearAll(false)).toBeNull();
    expect(store.error).toBe("选题生成正在运行，请等它跑完（HTTP 409）");
    expect(store.clearBusy).toBe(false);
  });
});

describe("候选 · 生成文案并送审", () => {
  it("成功 ⇒ 返回任务号、切到「已入队」、如实报字数与句数", async () => {
    const store = useTopicsStore();
    const taskId = await store.draftForReview("tp1");

    expect(taskId).toBe("t-tp1");
    expect(store.notice).toContain("700 字 / 20 句");
    expect(store.draftBusy).toBeNull();
    // 这一步会把选题推出候选视图：留在原地它当场就消失了
    expect(store.status).toBe("queued");
    expect(fakes.fetchTopics).toHaveBeenLastCalledWith("queued", 200);
  });

  it("已有生效稿件 ⇒ 说清楚「没重跑」，不谎称又写了一遍", async () => {
    fakes = install({ draftTopicForReview: vi.fn(async () => drafted({ reused: true })) });
    const store = useTopicsStore();

    await store.draftForReview("tp1");
    expect(store.notice).toContain("已有生效稿件");
  });

  it("失败 ⇒ 返回 null、error 带后端那句话，不改过滤条件", async () => {
    fakes = install({
      draftTopicForReview: vi.fn(async () =>
        drafted({
          ok: false,
          script_id: null,
          task_status: "failed",
          error_message: "Writer 没返回成稿",
        }),
      ),
    });
    const store = useTopicsStore();

    expect(await store.draftForReview("tp1")).toBeNull();
    expect(store.error).toBe("Writer 没返回成稿");
    expect(store.status).toBe("candidate");
  });
});
