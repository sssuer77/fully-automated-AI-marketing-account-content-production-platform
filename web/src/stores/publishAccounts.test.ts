import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PublishAccount,
  PublishAccountHealthOutcome,
  PublishAccountOutcome,
  PublishAccountsView,
  PublishPlatformOption,
} from "@/api/endpoints/publish";
import { ApiError } from "@/api/http";
import {
  accountTitle,
  accountTone,
  configurePublishAccountsApi,
  draftFromAccount,
  draftToBody,
  emptyAccountDraft,
  limitText,
  platformOptionText,
  suggestAccountId,
  suggestProfileDir,
  usePublishAccountsStore,
} from "./publishAccounts";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function account(overrides: Partial<PublishAccount> = {}): PublishAccount {
  return {
    account_id: "acc_main",
    platform: "douyin",
    display_name: "主账号",
    profile_dir: "data/browser_profile/acc_main",
    runtime_profile_dir: "D:/studio/data/browser_profile/acc_main",
    profile_dir_exists: true,
    profile_dir_matches_runtime: true,
    enabled: true,
    daily_limit: 3,
    min_gap_min: 30,
    note: "登录过（会话是否仍有效，要真发一次才知道）",
    ...overrides,
  };
}

function option(overrides: Partial<PublishPlatformOption> = {}): PublishPlatformOption {
  return {
    code: "douyin",
    publisher: "douyin",
    enabled: true,
    rehearsal: false,
    accounts: ["acc_main"],
    selectable: true,
    note: "真平台：要登录态，发出去不可撤销",
    calibration: "calibrated",
    calibration_note: "已真机校准（2026-09-23）",
    ...overrides,
  };
}

function view(overrides: Partial<PublishAccountsView> = {}): PublishAccountsView {
  return {
    generated_at: "2026-09-22T02:00:00.000Z",
    config_path: "D:/studio/config/publish.yaml",
    publish_enabled: false,
    require_confirm: true,
    accounts: [account()],
    platforms: [option(), option({ code: "other", rehearsal: true, note: "本地演练台" })],
    profile_dir_prefix: "data/browser_profile",
    limits: {
      account_id_min: 1,
      account_id_max: 64,
      daily_limit_min: 1,
      daily_limit_max: 100,
      min_gap_min_min: 0,
      min_gap_min_max: 1440,
    },
    notes: ["加完账号要人工扫码一次才会生效"],
    ...overrides,
  };
}

/**
 * 写操作的结论：**刷新后的整屏 + 这一次到底改了什么**。
 *
 * 与 `view()` 分开：`account_id` / `created` / `removed` 也是这张屏的一部分，
 * 少一个就不是一份合法的结论（PUT 与 DELETE 回的是同一个模型）。
 * 假件照契约补全 —— 字段名一改，这里跟着炸，而不是悄悄多出一个 undefined。
 */
function outcome(overrides: Partial<PublishAccountOutcome> = {}): PublishAccountOutcome {
  return {
    ...view(),
    changed: true,
    created: false,
    account_id: "acc_main",
    removed: null,
    reason: null,
    ...overrides,
  };
}

/** 探测 / 扫码登录的结论（比 `outcome` 多一个 `health` + 一句 `note`）。 */
function healthOutcome(overrides: Partial<PublishAccountHealthOutcome> = {}): PublishAccountHealthOutcome {
  return {
    ...outcome(),
    action: "probe",
    health: {
      ready: true,
      logged_in: true,
      last_check_at: "2026-09-22T02:00:00.000Z",
      account_name: "主账号",
      hint: null,
    },
    note: "登录态可用：主账号 —— 这个号可以投递了。",
    waited_sec: 1.5,
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configurePublishAccountsApi({
    fetchAccounts: vi.fn(async () => view()),
    saveAccount: vi.fn(async () => outcome({ created: true, account_id: "acc_second" })),
    deleteAccount: vi.fn(async () => outcome({ removed: "acc_main" })),
    probeAccount: vi.fn(async () => healthOutcome()),
    loginAccount: vi.fn(async () => healthOutcome({ action: "login" })),
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("建议目录：前缀 + id，前缀的尾斜杠不叠成两个", () => {
    expect(suggestProfileDir("data/browser_profile", "acc_second")).toBe(
      "data/browser_profile/acc_second",
    );
    expect(suggestProfileDir("data/browser_profile/", "acc_second")).toBe(
      "data/browser_profile/acc_second",
    );
    expect(suggestProfileDir("", "acc_second")).toBe("");
    expect(suggestProfileDir("data/browser_profile", "  ")).toBe("");
  });

  it("表单初值：默认限频与后端默认值一致，而且是启用的", () => {
    const draft = emptyAccountDraft("douyin");
    expect(draft).toMatchObject({ platform: "douyin", enabled: true, dailyLimit: 3, minGapMin: 30 });
    expect(draft.accountId).toBe("");
    expect(draft.profileDir).toBe("");
  });

  it("改号时把盘上那一条原样带进表单（不替用户改成建议值）", () => {
    const draft = draftFromAccount(account({ display_name: "副号", daily_limit: 2 }));
    expect(draft).toEqual({
      accountId: "acc_main",
      platform: "douyin",
      displayName: "副号",
      profileDir: "data/browser_profile/acc_main",
      enabled: true,
      dailyLimit: 2,
      minGapMin: 30,
    });
  });

  it("请求体：显示名去掉首尾空格；目录留空发 null（由后端补运行期那个路径）", () => {
    const body = draftToBody({ ...emptyAccountDraft("douyin"), accountId: "acc_x", displayName: " 主号 " });
    expect(body).toMatchObject({ platform: "douyin", display_name: "主号", profile_dir: null });
  });

  it("屏上的名字：有显示名用它，没有就用 id（不留空行）", () => {
    expect(accountTitle(account())).toBe("主账号");
    expect(accountTitle(account({ display_name: "  " }))).toBe("acc_main");
  });

  it("限频那一行：两个数字都来自配置", () => {
    expect(limitText(account({ daily_limit: 2, min_gap_min: 60 }))).toBe("≤2 条/天 · 两条间隔 ≥60 分钟");
  });

  it("灯色：停用灰、没登录态黄、目录声明与运行期不一致黄、其余绿", () => {
    expect(accountTone(account({ enabled: false }))).toBe("idle");
    expect(accountTone(account({ profile_dir_exists: false }))).toBe("warn");
    expect(accountTone(account({ profile_dir_matches_runtime: false }))).toBe("warn");
    expect(accountTone(account())).toBe("ok");
  });

  it("平台下拉那一行：平台名 + 账号 + 后端算好的说明", () => {
    expect(platformOptionText(option())).toBe("douyin · acc_main · 真平台：要登录态，发出去不可撤销");
    expect(platformOptionText(option({ code: "other", accounts: [], note: "本地演练台" }))).toBe(
      "本地演练台 · 没有启用的账号 · 本地演练台",
    );
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("账号 store", () => {
  it("刷新一次就够：start() 不排定时器（账号只有在本屏动手才会变）", async () => {
    vi.useFakeTimers();
    const fetchAccounts = vi.fn(async () => view());
    configurePublishAccountsApi({ fetchAccounts });
    const store = usePublishAccountsStore();

    store.start();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(fetchAccounts).toHaveBeenCalledTimes(1);
    expect(store.accounts).toHaveLength(1);
    expect(store.platforms).toHaveLength(2);
    expect(store.notes).toHaveLength(1);
  });

  it("刷新之后表单自动选上第一个平台（否则下拉是空的，用户以为没平台可配）", async () => {
    const store = usePublishAccountsStore();

    await store.refresh();

    expect(store.draft.platform).toBe("douyin");
  });

  it("id 空着不发文：连请求都拼不出来，当场说清楚", async () => {
    const saveAccount = vi.fn(async () => outcome({ created: true, account_id: "acc_second" }));
    configurePublishAccountsApi({ saveAccount });
    const store = usePublishAccountsStore();
    await store.refresh();
    store.beginCreate();
    // 面板会替人填一个建议 id（见下面那条用例）—— 这里**手动清掉**，
    // 验的是那道闸本身：空 id 连 `PUT /accounts/` 都拼不出来。
    store.setDraft({ accountId: "" });

    await store.save();

    expect(saveAccount).not.toHaveBeenCalled();
    expect(store.error).toContain("id");
  });

  it("加号成功：说清楚「还要扫码一次」，并把表单复位、重拉清单", async () => {
    const saveAccount = vi.fn(async () => outcome({ created: true, account_id: "acc_second" }));
    const fetchAccounts = vi.fn(async () => view());
    configurePublishAccountsApi({ saveAccount, fetchAccounts });
    const store = usePublishAccountsStore();
    await store.refresh();
    store.beginCreate();
    store.setDraft({ accountId: "acc_second" });

    await store.save();

    expect(saveAccount).toHaveBeenCalledWith("acc_second", expect.objectContaining({ platform: "douyin" }));
    expect(store.notice).toContain("扫码");
    expect(store.editingId).toBeNull();
    expect(store.draft.accountId).toBe("");
    expect(fetchAccounts).toHaveBeenCalledTimes(2);
  });

  it("同一个值存两次：提示「没变」，而不是假装改成功了", async () => {
    const saveAccount = vi.fn(async () => outcome({ changed: false }));
    configurePublishAccountsApi({ saveAccount });
    const store = usePublishAccountsStore();
    await store.refresh();
    store.beginEdit(account());

    await store.save();

    expect(store.notice).toContain("没变");
  });

  it("后端拒绝（422/400）⇒ 原样把它的那句话显示出来", async () => {
    const saveAccount = vi.fn(async () => {
      throw new ApiError("账号清单不合法（1 处问题）", 400, { code: "CONFIG_INVALID" });
    });
    configurePublishAccountsApi({ saveAccount });
    const store = usePublishAccountsStore();
    await store.refresh();
    store.beginCreate();
    store.setDraft({ accountId: "acc_second" });

    await store.save();

    expect(store.error).toContain("账号清单不合法");
  });

  it("停用/启用：把**整条**账号发上去（PUT 是全量替换，少一个字段就把它清成默认值）", async () => {
    const saveAccount = vi.fn(async () => outcome());
    configurePublishAccountsApi({ saveAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.toggleEnabled(account({ daily_limit: 2, min_gap_min: 60 }));

    expect(saveAccount).toHaveBeenCalledWith(
      "acc_main",
      expect.objectContaining({ enabled: false, daily_limit: 2, min_gap_min: 60, platform: "douyin" }),
    );
    expect(store.notice).toContain("已停用");
  });

  it("删号：带上那句理由，并说清楚「登录态目录没动」", async () => {
    const deleteAccount = vi.fn(async () => outcome({ removed: "acc_main" }));
    configurePublishAccountsApi({ deleteAccount });
    const store = usePublishAccountsStore();
    await store.refresh();
    store.openReason("acc_main");
    store.setReason("acc_main", "  这个号不用了  ");

    await store.remove(account());

    expect(deleteAccount).toHaveBeenCalledWith("acc_main", "这个号不用了");
    expect(store.notice).toContain("登录态目录没动");
    expect(store.reasonFor).toBeNull();
  });

  it("理由空着 ⇒ 发 null（而不是发一个空字符串）", async () => {
    const deleteAccount = vi.fn(async () => outcome({ removed: "acc_main" }));
    configurePublishAccountsApi({ deleteAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.remove(account());

    expect(deleteAccount).toHaveBeenCalledWith("acc_main", null);
  });

  it("正在改的那个号被删掉 ⇒ 表单退回「新建」（否则会对着一个不存在的号点保存）", async () => {
    const store = usePublishAccountsStore();
    await store.refresh();
    store.beginEdit(account());

    await store.remove(account());

    expect(store.editing).toBe(false);
    expect(store.draft.accountId).toBe("");
  });

  it("加号时面板替人起一个没被占用的 id（「账号 id 填什么」是这一屏最容易卡住的地方）", async () => {
    const store = usePublishAccountsStore();
    await store.refresh();

    store.beginCreate();

    // 盘上已经有 acc_main 与 _rehearsal；平台是 douyin ⇒ 起一个 acc_douyin
    expect(store.draft.accountId).toBe("acc_douyin");
    expect(store.draft.platform).toBe("douyin");
  });

  it("换了平台再点加号 ⇒ 建议名跟着新平台走（不是按表单里那个旧的算）", async () => {
    const store = usePublishAccountsStore();
    await store.refresh();
    store.setDraft({ platform: "kuaishou" });
    expect(store.suggestedAccountId).toBe("acc_kuaishou");

    store.beginCreate();

    expect(store.draft.platform).toBe("douyin");
    expect(store.draft.accountId).toBe("acc_douyin");
  });

  it("探测：把后端那句「下一步」原样显示出来，并整屏重画", async () => {
    const probeAccount = vi.fn(async () =>
      healthOutcome({ note: "登录态可用：主账号 —— 这个号可以投递了。" }),
    );
    configurePublishAccountsApi({ probeAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.probe(account());

    expect(probeAccount).toHaveBeenCalledWith("acc_main");
    expect(store.notice).toContain("可以投递");
    expect(store.error).toBe("");
    // 探测回的是**整屏**：面板不必再发一次 GET（那会有"显示旧值"的一帧）
    expect(store.accounts).toHaveLength(1);
  });

  it("探测失败（404）⇒ 把后端那句话显示出来，不假装成功", async () => {
    const probeAccount = vi.fn(async () => {
      throw new ApiError("配置里没有这个账号：acc_main", 404, {
        code: "PUBLISH_ACCOUNT_NOT_FOUND",
      });
    });
    configurePublishAccountsApi({ probeAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.probe(account());

    expect(store.error).toContain("配置里没有这个账号");
    expect(store.notice).toBe("");
  });

  it("扫码登录：等窗口的时候这一行是「等扫码中」，回来之后说清楚结果", async () => {
    const loginAccount = vi.fn(async () =>
      healthOutcome({
        action: "login",
        note: "登录态可用：主账号 —— 这个号可以投递了。",
      }),
    );
    configurePublishAccountsApi({ loginAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    const pending = store.login(account());
    // 请求还没回来：按钮要写"等扫码中…"（人在掏手机，得知道现在轮到他了）
    expect(store.loginId).toBe("acc_main");

    await pending;

    expect(loginAccount).toHaveBeenCalledWith("acc_main");
    expect(store.notice).toContain("可以投递");
    expect(store.loginId).toBeNull();
    expect(store.busyId).toBeNull();
  });

  it("扫码超时 ⇒ 显示后端那句「用哪个 App 扫」，而不是一个红叉", async () => {
    const loginAccount = vi.fn(async () =>
      healthOutcome({
        action: "login",
        health: { ready: false, logged_in: false, last_check_at: "", hint: "等了 180 秒没等到扫码完成" },
        note: "等了 180 秒没等到扫码完成。扫的时候用**手机上对应的那个 App**（抖音 / 快手 / 视频号）里的扫一扫。",
        waited_sec: 180.0,
      }),
    );
    configurePublishAccountsApi({ loginAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.login(account());

    expect(store.notice).toContain("扫一扫");
    expect(store.error).toBe("");
    expect(store.loginId).toBeNull();
  });

  it("扫码登录出错（503 平台没实现）⇒ 走错误那条，而不是「没登录」", async () => {
    const loginAccount = vi.fn(async () => {
      throw new ApiError("平台 bilibili 不支持从面板扫码登录", 503, {
        code: "PUBLISH_NOT_IMPLEMENTED",
      });
    });
    configurePublishAccountsApi({ loginAccount });
    const store = usePublishAccountsStore();
    await store.refresh();

    await store.login(account());

    expect(store.error).toContain("不支持从面板扫码登录");
    expect(store.loginId).toBeNull();
  });
});

describe("suggestAccountId", () => {
  it("没被占用就用 acc_<平台>", () => {
    expect(suggestAccountId([], "douyin")).toBe("acc_douyin");
  });

  it("撞了就往下加序号，直到没被占用", () => {
    expect(suggestAccountId(["acc_douyin"], "douyin")).toBe("acc_douyin_2");
    expect(suggestAccountId(["acc_douyin", "acc_douyin_2"], "douyin")).toBe("acc_douyin_3");
  });

  it("平台为空 ⇒ 退回 acc（不拼出一个以 _ 结尾的名字）", () => {
    expect(suggestAccountId([], "")).toBe("acc");
    expect(suggestAccountId(["acc"], "  ")).toBe("acc_2");
  });
});
