import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AnalyzeResult,
  DirectionItem,
  DirectionList,
  HotImportResult,
  IdeateResult,
  ImportResult,
  ManualTopicResult,
  SelectFailure,
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
  scoreTone,
  sortByScore,
  summarizeImport,
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

/** 装一整套假件（**每次都给全** ⇒ 上一个用例的覆盖不会漏到下一个）。 */
function install(overrides: Partial<TopicsApi> = {}): TopicsApi {
  const fakes: TopicsApi = {
    fetchTopics: vi.fn(async () => pool([topic("tp1"), topic("tp2")])),
    fetchDirections: vi.fn(async () => directions([direction("d1"), direction("d2")])),
    analyzeTopics: vi.fn(async () => analyzed()),
    ideateTopics: vi.fn(async () => ideated()),
    selectTopics: vi.fn(async (ids: readonly string[]) => selected(ids)),
    addManualTopic: vi.fn(async () => manual()),
    importHot: vi.fn(async () => hotResult()),
    submitHot: vi.fn(async () => hotResult()),
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
