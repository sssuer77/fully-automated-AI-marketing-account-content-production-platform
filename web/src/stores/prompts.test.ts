import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PromptCatalog, PromptEntry, PromptOutcome } from "@/api/endpoints/prompts";
import { ApiError } from "@/api/http";
import {
  configurePromptsApi,
  draftKey,
  overriddenRoles,
  usePromptsStore,
  type PromptsApi,
} from "./prompts";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function entry(name: string, overrides: Partial<PromptEntry> = {}): PromptEntry {
  return {
    name,
    description: `${name} 的用途`,
    version: "1",
    prompt_version: "1+aaaa",
    overridden: false,
    files: [
      { role: "system", path: `${name}/system.md`, text: "你是编剧。\n", overridden: false },
      { role: "user", path: `${name}/user.jinja`, text: "题目：{{topic}}\n", overridden: false },
    ],
    variables: ["topic"],
    ...overrides,
  };
}

function catalog(items: PromptEntry[] = [entry("writer"), entry("ideator")]): PromptCatalog {
  return { prompts: items, override_dir: "D:/repo/data/prompts", count: items.length };
}

function outcome(
  name: string,
  changed: string[],
  overrides: Partial<PromptEntry> = {},
): PromptOutcome {
  return {
    name,
    changed,
    restored: false,
    entry: entry(name, { overridden: changed.length > 0, prompt_version: "1+bbbb", ...overrides }),
  };
}

/** 装一整套假件（**每次都给全** ⇒ 上一个用例的覆盖不会漏到下一个）。 */
function install(overrides: Partial<PromptsApi> = {}): PromptsApi {
  const fakes: PromptsApi = {
    fetchPrompts: vi.fn(async () => catalog()),
    savePrompt: vi.fn(async (name: string) => outcome(name, ["system"])),
    restorePrompt: vi.fn(async (name: string) =>
      outcome(name, ["system"], { overridden: false, prompt_version: "1+aaaa" }),
    ),
    ...overrides,
  };
  configurePromptsApi(fakes);
  return fakes;
}

let fakes: PromptsApi;

beforeEach(() => {
  setActivePinia(createPinia());
  fakes = install();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("草稿键与被覆盖的段", () => {
  it("草稿键由条目名与段组成（一个条目最多两段）", () => {
    expect(draftKey("writer", "system")).toBe("writer/system");
    expect(draftKey("shared.json_contract", "user")).toBe("shared.json_contract/user");
  });

  it("只报真的被覆盖的段", () => {
    const item = entry("writer", {
      files: [
        { role: "system", path: "a", text: "x", overridden: true },
        { role: "user", path: "b", text: "y", overridden: false },
      ],
    });
    expect(overriddenRoles(item)).toEqual(["system"]);
  });
});

// ══════════════════════════════════════════════════════════════════════
// 拉取与选中
// ══════════════════════════════════════════════════════════════════════

describe("拉取", () => {
  it("第一次拉完自动选中第一个条目，并记下覆盖目录", async () => {
    const store = usePromptsStore();
    await store.load();

    expect(store.entries).toHaveLength(2);
    expect(store.selectedName).toBe("writer");
    expect(store.overrideDir).toBe("D:/repo/data/prompts");
    expect(store.loadError).toBeNull();
  });

  it("刷新**不换掉**用户正在看的那条", async () => {
    const store = usePromptsStore();
    await store.load();
    store.select("ideator");
    await store.load();
    expect(store.selectedName).toBe("ideator");
  });

  it("条目被删掉（注册表改了）⇒ 退回第一个，而不是停在空选中上", async () => {
    const store = usePromptsStore();
    await store.load();
    store.select("ideator");

    fakes = install({ fetchPrompts: vi.fn(async () => catalog([entry("writer")])) });
    await store.load();
    expect(store.selectedName).toBe("writer");
  });

  it("拉取失败走 loadError（与动作级 error 分开）", async () => {
    fakes = install({
      fetchPrompts: vi.fn(async () => {
        throw new ApiError("连不上", 0, null);
      }),
    });
    const store = usePromptsStore();
    await store.load();

    expect(store.loadError).toBe("连不上");
    expect(store.error).toBeNull();
  });
});

// ══════════════════════════════════════════════════════════════════════
// 草稿
// ══════════════════════════════════════════════════════════════════════

describe("草稿", () => {
  it("没敲过字 ⇒ 草稿就是生效那一份，不算改动", async () => {
    const store = usePromptsStore();
    await store.load();

    expect(store.segments.map((item) => item.draft)).toEqual(["你是编剧。\n", "题目：{{topic}}\n"]);
    expect(store.dirtyCount).toBe(0);
  });

  it("改一段只让那一段变脏", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "你是编剧，要短。\n");

    expect(store.dirtyCount).toBe(1);
    expect(store.segments[0]?.dirty).toBe(true);
    expect(store.segments[1]?.dirty).toBe(false);
    // 生效那一份没被草稿盖掉
    expect(store.segments[0]?.saved).toBe("你是编剧。\n");
  });

  it("撤销改动 ⇒ 退回生效那一份", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "改了。\n");
    store.dropDraft("writer", "system");

    expect(store.dirtyCount).toBe(0);
    expect(store.segments[0]?.draft).toBe("你是编剧。\n");
  });

  it("切条目再切回来，草稿还在（不必重打）", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "改了。\n");
    store.select("ideator");
    store.select("writer");
    expect(store.segments[0]?.draft).toBe("改了。\n");
  });
});

// ══════════════════════════════════════════════════════════════════════
// 保存 / 还原
// ══════════════════════════════════════════════════════════════════════

describe("保存", () => {
  it("只发真的改了的那一段（另一段一个字都不提）", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "新纪律。\n");

    expect(await store.save("writer")).toBe(true);
    expect(fakes.savePrompt).toHaveBeenCalledWith("writer", { system: "新纪律。\n" });
    expect(store.notice).toContain("1+bbbb");
  });

  it("一段都没改 ⇒ **不发请求**（后端会说「什么都没改」，那只是噪音）", async () => {
    const store = usePromptsStore();
    await store.load();

    expect(await store.save("writer")).toBe(true);
    expect(fakes.savePrompt).not.toHaveBeenCalled();
  });

  it("存完把生效那份换成后端回的那一份，并清掉草稿", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "新纪律。\n");
    await store.save("writer");

    expect(store.dirtyCount).toBe(0);
    expect(store.selected?.overridden).toBe(true);
    expect(store.selected?.prompt_version).toBe("1+bbbb");
  });

  it("校验不过（422）⇒ 报错**且草稿留着**，用户改一个字就能重试", async () => {
    fakes = install({
      savePrompt: vi.fn(async () => {
        throw new ApiError("引用了没人会填的变量：[nobody]", 422, null);
      }),
    });
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "说 {{nobody}}\n");

    expect(await store.save("writer")).toBe(false);
    expect(store.error).toContain("nobody");
    expect(store.dirtyCount).toBe(1);
    expect(store.segments[0]?.draft).toBe("说 {{nobody}}\n");
  });
});

describe("还原", () => {
  it("按段还原，并把生效那份换回仓库那一份", async () => {
    const store = usePromptsStore();
    await store.load();
    store.setDraft("writer", "system", "改了。\n");

    expect(await store.restore("writer", "system")).toBe(true);
    expect(fakes.restorePrompt).toHaveBeenCalledWith("writer", "system");
    expect(store.dirtyCount).toBe(0);
    expect(store.selected?.overridden).toBe(false);
  });

  it("本来就没覆盖 ⇒ 如实说一句，不当成失败", async () => {
    fakes = install({ restorePrompt: vi.fn(async (name: string) => outcome(name, [])) });
    const store = usePromptsStore();
    await store.load();

    expect(await store.restore("writer", "system")).toBe(true);
    expect(store.notice).toBe("这一段本来就没被覆盖");
    expect(store.error).toBeNull();
  });
});
