import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  LlmKeyOutcome,
  LlmKeyStatus,
  LlmProbe,
  LlmProfile,
  LlmProfileBody,
  LlmProfileOutcome,
  LlmSettings,
} from "@/api/endpoints/settings";
import { ApiError } from "@/api/http";
import {
  FALLBACK_KEY_LIMITS,
  configureSettingsApi,
  keyInputProblem,
  keyTone,
  probeSummary,
  probeTone,
  profileNotice,
  profileTone,
  saveNotice,
  useSettingsStore,
} from "./settings";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const KEY = "sk-abcdefghijklmnopqrstuvwxyz012345";

function keyStatus(overrides: Partial<LlmKeyStatus> = {}): LlmKeyStatus {
  return {
    configured: false,
    masked_key: null,
    source: "none",
    source_label: "尚未配置",
    env_var: "STUDIO_LLM_API_KEY",
    path: "D:/repo/config/secrets.yaml",
    file_exists: false,
    version: 0,
    loaded_at: "2026-09-17T12:00:00.000Z",
    last_error: null,
    env_overrides_file: false,
    ...overrides,
  };
}

function profile(overrides: Partial<LlmProfile> = {}): LlmProfile {
  return {
    name: "cloud",
    engine: "oi_compatible",
    base_url: "https://api.openai.com/v1",
    model: "gpt-5.6-terra",
    api_key_env: "STUDIO_LLM_API_KEY",
    is_default: true,
    needs_key: true,
    usable: false,
    detail: "缺密钥 —— 在下面填一把",
    ...overrides,
  };
}

function settings(overrides: Partial<LlmSettings> = {}): LlmSettings {
  return {
    generated_at: "2026-09-17T12:00:00.000Z",
    default_profile: "cloud",
    profiles: [profile()],
    routing: [{ agent: "writer", profile: "cloud", fallback: "local" }],
    key: keyStatus(),
    limits: { min_len: 8, max_len: 512 },
    notes: [],
    ...overrides,
  };
}

function outcome(overrides: Partial<LlmKeyOutcome> = {}): LlmKeyOutcome {
  return {
    ...settings({ key: keyStatus({ configured: true, source: "file", source_label: "config/secrets.yaml", masked_key: "sk-…2345" }) }),
    changed: true,
    cleared: false,
    reason: null,
    ...overrides,
  };
}

function profileOutcome(overrides: Partial<LlmProfileOutcome> = {}): LlmProfileOutcome {
  return {
    ...settings({ profiles: [profile({ model: "gpt-5.6-sol" })] }),
    changed: true,
    profile: "cloud",
    model: "gpt-5.6-sol",
    base_url: "https://api.openai.com/v1",
    reason: null,
    ...overrides,
  };
}

function probe(overrides: Partial<LlmProbe> = {}): LlmProbe {
  return {
    generated_at: "2026-09-17T12:00:00.000Z",
    rows: [
      {
        profile: "cloud",
        engine: "oi_compatible",
        model: "gpt-5.6-terra",
        base_url: "https://api.openai.com/v1",
        status: "ok",
        detail: "已连接",
      },
    ],
    ok_count: 1,
    ...overrides,
  };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("keyInputProblem", () => {
  const limits = { min_len: 8, max_len: 512 };

  it("空输入不报错（用户正要粘贴）", () => {
    expect(keyInputProblem("", limits)).toBeNull();
    expect(keyInputProblem("   ", limits)).toBeNull();
  });

  it("太短 / 太长各给一句话", () => {
    expect(keyInputProblem("short", limits)).toContain("太短");
    expect(keyInputProblem("a".repeat(513), limits)).toContain("太长");
  });

  it("中间有空白要拦下来（终端复制常把折行带进来）", () => {
    expect(keyInputProblem("sk-abc def ghijkl", limits)).toContain("空格或换行");
  });

  it("正常的一把通过", () => {
    expect(keyInputProblem(KEY, limits)).toBeNull();
  });

  it("判据来自服务端下发的 limits（不是前端写死的）", () => {
    expect(keyInputProblem("abcdefgh", { min_len: 20, max_len: 512 })).toContain("太短");
    expect(keyInputProblem("abcdefgh", FALLBACK_KEY_LIMITS)).toBeNull();
  });
});

describe("色调", () => {
  it("没配是黄的、配好是绿的、读不出来是红的", () => {
    expect(keyTone(null)).toBe("idle");
    expect(keyTone(keyStatus())).toBe("warn");
    expect(keyTone(keyStatus({ configured: true }))).toBe("ok");
    expect(keyTone(keyStatus({ configured: true, last_error: "坏了" }))).toBe("error");
  });

  it("通道能不能用决定绿 / 黄", () => {
    expect(profileTone(profile({ usable: true }))).toBe("ok");
    expect(profileTone(profile({ usable: false }))).toBe("warn");
  });

  it("探测：连上是绿、缺密钥是黄、其余是红", () => {
    expect(probeTone({ ...probe().rows[0], status: "ok" })).toBe("ok");
    expect(probeTone({ ...probe().rows[0], status: "no_key" })).toBe("warn");
    expect(probeTone({ ...probe().rows[0], status: "unreachable" })).toBe("error");
    expect(probeTone({ ...probe().rows[0], status: "http_error" })).toBe("error");
  });
});

describe("probeSummary", () => {
  it("没测过 / 全通 / 部分通各一句话", () => {
    expect(probeSummary(null)).toBe("尚未测试");
    expect(probeSummary(probe())).toContain("全部连通");
    expect(probeSummary(probe({ ok_count: 0 }))).toBe("0 / 1 条通道连通");
  });
});

describe("saveNotice", () => {
  it("没改动就说没改动（不假装做了一次操作）", () => {
    expect(saveNotice(outcome({ changed: false }))).toContain("没有改动");
  });

  it("清除走另一句话", () => {
    expect(saveNotice(outcome({ cleared: true }))).toContain("已清除");
  });

  it("环境变量占着位置时要说清楚「这份暂不生效」", () => {
    const envOutcome = outcome({
      key: keyStatus({ configured: true, source: "env", env_var: "STUDIO_LLM_API_KEY" }),
    });
    const text = saveNotice(envOutcome);
    expect(text).toContain("STUDIO_LLM_API_KEY");
    expect(text).toContain("才会生效");
  });

  it("正常保存要说「存到哪」与「立刻生效」", () => {
    const text = saveNotice(outcome());
    expect(text).toContain("config/secrets.yaml");
    expect(text).toContain("不需要重启");
  });
});

describe("profileNotice", () => {
  it("没改动就说没改动（不假装做了一次操作）", () => {
    expect(profileNotice(profileOutcome({ changed: false }))).toContain("没有改动");
  });

  it("改了要说清**改成了什么**，以及**立刻生效、不用重启**", () => {
    const text = profileNotice(profileOutcome({ changed: true, model: "gpt-5.6-sol" }));
    expect(text).toContain("gpt-5.6-sol");
    expect(text).toContain("立刻生效");
    expect(text).toContain("不需要重启");
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("useSettingsStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    configureSettingsApi({
      fetchLlmSettings: async () => settings(),
      saveLlmKey: async () => outcome(),
      clearLlmKey: async () => outcome(),
      saveLlmProfile: async () => profileOutcome(),
      probeLlm: async () => probe(),
    });
    vi.restoreAllMocks();
  });

  it("load 把首屏装进来，失败时给一句人话", async () => {
    const store = useSettingsStore();
    configureSettingsApi({ fetchLlmSettings: async () => settings() });
    await store.load();
    expect(store.data?.default_profile).toBe("cloud");
    expect(store.error).toBeNull();

    configureSettingsApi({
      fetchLlmSettings: async () => {
        throw new ApiError("请求参数不合法", 422, null);
      },
    });
    await store.load();
    expect(store.error).toContain("HTTP 422");
  });

  it("saveKey 成功 ⇒ 用返回的整屏替换（面板不必再拉一次）", async () => {
    const store = useSettingsStore();
    configureSettingsApi({ saveLlmKey: async () => outcome() });
    const ok = await store.saveKey(`  ${KEY}  `);
    expect(ok).toBe(true);
    expect(store.data?.key.configured).toBe(true);
    expect(store.notice).toContain("已保存");
  });

  it("saveKey 把首尾空白去掉再提交", async () => {
    const spy = vi.fn(async () => outcome());
    configureSettingsApi({ saveLlmKey: spy });
    const store = useSettingsStore();
    await store.saveKey(`  ${KEY}  `);
    expect(spy).toHaveBeenCalledWith(KEY, undefined);
  });

  it("saveKey 失败 ⇒ 保留错误、不写 notice", async () => {
    const store = useSettingsStore();
    configureSettingsApi({
      saveLlmKey: async () => {
        throw new ApiError("API Key 太短（5 个字符，至少 8 个）", 422, null);
      },
    });
    const ok = await store.saveKey("short");
    expect(ok).toBe(false);
    expect(store.error).toContain("太短");
    expect(store.notice).toBeNull();
  });

  it("clearKey 走另一个端点", async () => {
    const spy = vi.fn(async () => outcome({ cleared: true }));
    configureSettingsApi({ clearLlmKey: spy });
    const store = useSettingsStore();
    await store.clearKey("换成环境变量");
    expect(spy).toHaveBeenCalledWith("换成环境变量");
    expect(store.notice).toContain("已清除");
  });

  it("runProbe 把结果装进 probe，失败也不清空旧结果", async () => {
    const store = useSettingsStore();
    configureSettingsApi({ probeLlm: async () => probe() });
    await store.runProbe();
    expect(store.probe?.ok_count).toBe(1);

    configureSettingsApi({
      probeLlm: async () => {
        throw new ApiError("超时", 0, null);
      },
    });
    await store.runProbe();
    expect(store.error).toContain("超时");
    expect(store.probe?.ok_count).toBe(1);
  });

  it("limits 用服务端下发的那一份", async () => {
    const store = useSettingsStore();
    configureSettingsApi({ fetchLlmSettings: async () => settings({ limits: { min_len: 20, max_len: 64 } }) });
    await store.load();
    expect(store.limits.min_len).toBe(20);
  });

  it("没读到数据时 limits 退回兜底值（面板不会因为没数字而画不出来）", () => {
    const store = useSettingsStore();
    expect(store.limits).toEqual(FALLBACK_KEY_LIMITS);
    expect(store.key).toBeNull();
  });

  it("envOverridden 如实转发服务端的判定", async () => {
    const store = useSettingsStore();
    configureSettingsApi({
      fetchLlmSettings: async () => settings({ key: keyStatus({ env_overrides_file: true }) }),
    });
    await store.load();
    expect(store.envOverridden).toBe(true);
  });
});

describe("useSettingsStore · 通道参数", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    configureSettingsApi({ saveLlmProfile: async () => profileOutcome() });
    vi.restoreAllMocks();
  });

  it("只把**填了**的字段发出去（另一个不传，绝不是传空串）", async () => {
    const calls: LlmProfileBody[] = [];
    const store = useSettingsStore();
    configureSettingsApi({
      saveLlmProfile: async (body) => {
        calls.push(body);
        return profileOutcome({ changed: true, model: "new-model" });
      },
    });

    const ok = await store.saveProfile("cloud", "  new-model  ", "");

    expect(ok).toBe(true);
    expect(calls).toEqual([{ profile: "cloud", model: "new-model" }]);
    expect(store.notice).toContain("new-model");
    expect(store.savingProfile).toBeNull();
    expect(store.data?.profiles[0].model).toBe("gpt-5.6-sol");
  });

  it("两个都空 ⇒ 本地拦下、不发请求（省一次 422 来回）", async () => {
    const spy = vi.fn();
    const store = useSettingsStore();
    configureSettingsApi({ saveLlmProfile: spy as never });

    const ok = await store.saveProfile("cloud", "   ", "");

    expect(ok).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    expect(store.error).toContain("至少填一个");
  });

  it("服务端拒绝 ⇒ 错误如实显示（不吞、不假装成功）", async () => {
    const store = useSettingsStore();
    configureSettingsApi({
      saveLlmProfile: async () => {
        throw new ApiError("没有这条通道：nope", 400, null);
      },
    });

    const ok = await store.saveProfile("nope", "x", "");

    expect(ok).toBe(false);
    expect(store.error).toContain("没有这条通道");
  });

  it("base_url 也能单独改", async () => {
    const calls: LlmProfileBody[] = [];
    const store = useSettingsStore();
    configureSettingsApi({
      saveLlmProfile: async (body) => {
        calls.push(body);
        return profileOutcome({ changed: true, base_url: "http://127.0.0.1:9999/v1" });
      },
    });

    await store.saveProfile("local", "", "http://127.0.0.1:9999/v1");

    expect(calls).toEqual([{ profile: "local", base_url: "http://127.0.0.1:9999/v1" }]);
  });
});
