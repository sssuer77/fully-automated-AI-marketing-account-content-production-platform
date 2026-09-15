import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PersonaBackup,
  PersonaConfig,
  PersonaLibraryItem,
  PersonaLimits,
  PersonaOutcome,
  PersonaResponse,
} from "@/api/endpoints/persona";
import { ApiError } from "@/api/http";
import {
  backupTone,
  configurePersonaApi,
  describeBackup,
  dirtyChanges,
  draftFrom,
  fieldErrors,
  isDirty,
  isPersonaEvent,
  libraryState,
  libraryTone,
  localErrors,
  usePersonaStore,
} from "./persona";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function config(overrides: Partial<PersonaConfig> = {}): PersonaConfig {
  return {
    schema_version: "1.0",
    id: "persona_default",
    name: "熊大熊二·MC跑酷",
    is_active: true,
    role_desc: "熊大负责讲解，熊二负责提问、吐槽、造笑点。",
    tone: "东北话、兄弟互怼",
    audience: "12-30 岁男性，游戏 / 动漫 / 沙雕搞笑",
    catchphrases: ["熊大你听我说", "俺寻思着吧"],
    forbidden: ["政治", "涉黄"],
    style_hint: "开头 3 秒内必须抛钩子",
    target_chars_min: 600,
    target_chars_max: 800,
    max_duration_ms: 180000,
    ...overrides,
  };
}

function limits(overrides: Partial<PersonaLimits> = {}): PersonaLimits {
  return {
    editable: ["name", "tone"],
    name: { min: 1, max: 128 },
    role_desc: { min: 4, max: null },
    tone: { min: 2, max: null },
    audience: { min: 2, max: null },
    catchphrases: { min: 2, max: 20 },
    forbidden: { min: 1, max: 200 },
    target_chars_min: { min: 100, max: 5000 },
    target_chars_max: { min: 100, max: 8000 },
    max_duration_ms: { min: 10000, max: 1800000 },
    ...overrides,
  };
}

function libraryItem(overrides: Partial<PersonaLibraryItem> = {}): PersonaLibraryItem {
  return {
    persona_id: "solo_commentary",
    name: "快嘴单人解说",
    tone: "京腔、语速快",
    audience: "14-28 岁男性",
    valid: true,
    error: "",
    sha256: "b".repeat(64),
    path: "D:/studio/config/personas/solo_commentary.yaml",
    active: false,
    ...overrides,
  };
}

function backup(overrides: Partial<PersonaBackup> = {}): PersonaBackup {
  return {
    name: "20260914-120000_persona_default.yaml",
    path: "D:/studio/data/backups/persona/20260914-120000_persona_default.yaml",
    persona_id: "persona_default",
    created_at: "2026-09-14T04:00:00.000Z",
    sha256: "c".repeat(64),
    size: 1024,
    valid: true,
    error: "",
    ...overrides,
  };
}

function snapshot(overrides: Partial<PersonaResponse> = {}): PersonaResponse {
  return {
    generated_at: "2026-09-14T04:00:01.000Z",
    active: {
      persona_id: "persona_default",
      name: "熊大熊二·MC跑酷",
      version: 1,
      sha256: "a".repeat(64),
      source: "active",
      path: "D:/studio/config/persona.yaml",
      loaded_at: "2026-09-14T04:00:00.000Z",
      last_error: null,
      stale: false,
      config: config(),
    },
    active_error: null,
    library: [libraryItem()],
    backups: [backup()],
    limits: limits(),
    active_path: "D:/studio/config/persona.yaml",
    library_dir: "D:/studio/config/personas",
    backup_dir: "D:/studio/data/backups/persona",
    backup_page: 20,
    ...overrides,
  };
}

function outcome(overrides: Partial<PersonaOutcome> = {}): PersonaOutcome {
  return {
    action: "persona.update",
    persona_id: "persona_default",
    name: "熊大熊二·MC跑酷",
    version: 2,
    sha256: "d".repeat(64),
    changed: true,
    previous_persona_id: "persona_default",
    backup: "20260914-120001_persona_default.yaml",
    note: "已保存「熊大熊二·MC跑酷」（v2）",
    ...overrides,
  };
}

/** 一个带 `field_errors` 的 422（形状与 `app/errors.py` 的 `StudioError.to_dict` 一致）。 */
function invalidForm(field: string, why: string): ApiError {
  return new ApiError("人物表单校验不通过（1 处问题）", 422, {
    code: "PERSONA_INVALID",
    message: "人物表单校验不通过（1 处问题）",
    context: { field_errors: [{ field, error: why }] },
    remediation: "按 field_errors 逐条修正后重新提交；**本次没有写入任何内容**",
    type: "ConfigError",
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  configurePersonaApi({
    fetchPersona: vi.fn(async () => snapshot()),
    updatePersona: vi.fn(async () => outcome()),
    activatePersona: vi.fn(async () => outcome({ action: "persona.activate", persona_id: "solo_commentary" })),
    saveAsPersona: vi.fn(async () => outcome({ action: "persona.save_as", changed: false, backup: null })),
    rollbackPersona: vi.fn(async () => outcome({ action: "persona.rollback" })),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("草稿是**拷贝**：改草稿不会碰到服务端那一份", () => {
    const source = config();
    const draft = draftFrom(source);
    expect(draft.name).toBe(source.name);
    expect(draft.catchphrases).toEqual(source.catchphrases);
    draft.catchphrases.push("新口癖");
    expect(source.catchphrases).toHaveLength(2);
  });

  it("dirtyChanges：只发改动过的字段（两个标签页同时开着不该互相覆盖）", () => {
    const source = config();
    const draft = draftFrom(source);
    expect(dirtyChanges(draft, source)).toEqual({});

    draft.tone = "京腔";
    expect(dirtyChanges(draft, source)).toEqual({ tone: "京腔" });

    draft.catchphrases = ["只要一条"];
    expect(dirtyChanges(draft, source)).toEqual({ tone: "京腔", catchphrases: ["只要一条"] });

    // 值改回去 ⇒ 那一项消失（"改了又改回来"不该被当成改动）
    draft.tone = source.tone;
    expect(dirtyChanges(draft, source)).toEqual({ catchphrases: ["只要一条"] });
  });

  it("isDirty 跟着 dirtyChanges 走", () => {
    const source = config();
    const draft = draftFrom(source);
    expect(isDirty(draft, source)).toBe(false);
    draft.max_duration_ms = 120000;
    expect(isDirty(draft, source)).toBe(true);
  });

  it("localErrors：长度、条数、区间自洽、时长上限都要判", () => {
    const source = config();
    const draft = draftFrom(source);
    expect(localErrors(draft, limits())).toEqual({});

    draft.name = "";
    expect(localErrors(draft, limits()).name).toContain("至少 1 个字");

    draft.name = source.name;
    draft.tone = "x";
    expect(localErrors(draft, limits()).tone).toContain("至少 2 个字");

    draft.tone = source.tone;
    draft.catchphrases = ["只有一条"];
    expect(localErrors(draft, limits()).catchphrases).toContain("至少 2 条");

    draft.catchphrases = source.catchphrases;
    draft.forbidden = [];
    expect(localErrors(draft, limits()).forbidden).toContain("至少 1 条");

    draft.forbidden = source.forbidden;
    draft.target_chars_min = 900;
    draft.target_chars_max = 800;
    expect(localErrors(draft, limits()).target_chars_max).toBe("篇幅上限必须大于下限");

    draft.target_chars_min = source.target_chars_min;
    draft.target_chars_max = source.target_chars_max;
    draft.max_duration_ms = 5_000;
    expect(localErrors(draft, limits()).max_duration_ms).toContain("10000–1800000");
  });

  it("localErrors：中文按**字符**数（不是 UTF-16 码元）", () => {
    const source = config({ name: "熊" });
    const draft = draftFrom(source);
    expect(localErrors(draft, limits()).name).toBeUndefined();
  });

  it("fieldErrors：从 422 的 context.field_errors 里取（能标到输入框上）", () => {
    expect(fieldErrors(invalidForm("name", "String should have at least 1 character"))).toEqual({
      name: "String should have at least 1 character",
    });
  });

  it("fieldErrors：拿不到就返回空对象（调用方退回显示整条 message，不丢信息）", () => {
    expect(fieldErrors(new Error("网络断了"))).toEqual({});
    expect(fieldErrors(new ApiError("库锁住了", 503, null))).toEqual({});
    expect(fieldErrors(new ApiError("怪东西", 500, { context: { field_errors: "不是数组" } }))).toEqual({});
    expect(fieldErrors(new ApiError("怪东西", 500, { context: { field_errors: [{ 缺: "字段" }] } }))).toEqual({});
  });

  it("库条目的灯与文案：无效 > 激活 > 可切换", () => {
    expect(libraryTone(libraryItem())).toBe("idle");
    expect(libraryTone(libraryItem({ active: true }))).toBe("ok");
    expect(libraryTone(libraryItem({ valid: false, error: "文件内 id 不一致" }))).toBe("error");
    expect(libraryState(libraryItem())).toBe("可切换");
    expect(libraryState(libraryItem({ active: true }))).toBe("当前激活");
    expect(libraryState(libraryItem({ valid: false }))).toBe("不可用");
  });

  it("备份的灯与文案：坏备份照样列出来，但一眼看得出不能用", () => {
    expect(backupTone(backup())).toBe("idle");
    expect(backupTone(backup({ valid: false }))).toBe("error");
    expect(describeBackup(backup())).toContain("persona_default");
    expect(describeBackup(backup({ valid: false }))).toContain("已损坏");
  });

  it("只有 system.persona_changed 才触发重拉（这条通道还有日志与告警）", () => {
    expect(isPersonaEvent("system.persona_changed")).toBe(true);
    expect(isPersonaEvent("log.appended")).toBe(false);
    expect(isPersonaEvent("system.alert")).toBe(false);
    expect(isPersonaEvent(undefined)).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("usePersonaStore", () => {
  it("refresh 成功 ⇒ 激活人物 / 人物库 / 备份 / 上下限就位，草稿跟着回填", async () => {
    const store = usePersonaStore();
    await store.refresh();
    expect(store.loadError).toBeNull();
    expect(store.active?.persona_id).toBe("persona_default");
    expect(store.library.map((item) => item.persona_id)).toEqual(["solo_commentary"]);
    expect(store.backups).toHaveLength(1);
    expect(store.limits?.target_chars_max).toEqual({ min: 100, max: 8000 });
    expect(store.draft?.tone).toBe("熊大熊二·MC跑酷".length > 0 ? config().tone : "");
    expect(store.dirty).toBe(false);
    expect(store.draftStale).toBe(false);
  });

  it("激活文件坏了 ⇒ activeError / stale 上桌，但库里那份照样能切（修回来的路不能断）", async () => {
    configurePersonaApi({
      fetchPersona: vi.fn(async () =>
        snapshot({
          active_error: "配置文件不存在：config/persona.yaml",
          active: null,
          library: [libraryItem()],
        }),
      ),
    });
    const store = usePersonaStore();
    await store.refresh();
    expect(store.activeError).toContain("不存在");
    expect(store.active).toBeNull();
    expect(store.draft).toBeNull();
    expect(store.library).toHaveLength(1);
  });

  it("stale（有可用快照但磁盘那份坏了）如实透传", async () => {
    configurePersonaApi({
      fetchPersona: vi.fn(async () =>
        snapshot({
          active: {
            ...snapshot().active!,
            stale: true,
            last_error: "YAML 语法错误",
          },
        }),
      ),
    });
    const store = usePersonaStore();
    await store.refresh();
    expect(store.stale).toBe(true);
    expect(store.active?.last_error).toBe("YAML 语法错误");
  });

  it("★ 刷新**不覆盖**脏草稿：只立一个 draftStale 旗子", async () => {
    const store = usePersonaStore();
    await store.refresh();
    store.draft!.tone = "我正在写的东西";

    configurePersonaApi({ fetchPersona: vi.fn(async () => snapshot({ generated_at: "更晚" })) });
    await store.refresh();

    expect(store.draft?.tone).toBe("我正在写的东西");
    expect(store.dirty).toBe(true);
    expect(store.draftStale).toBe(true);
  });

  it("刷新时草稿不脏 ⇒ 用服务端那一份覆盖（并清掉 draftStale）", async () => {
    const store = usePersonaStore();
    await store.refresh();
    store.draftStale = true;
    await store.refresh();
    expect(store.draftStale).toBe(false);
    expect(store.dirty).toBe(false);
  });

  it("resetDraft：放弃编辑回到服务端那一份", async () => {
    const store = usePersonaStore();
    await store.refresh();
    store.draft!.name = "改了但不要了";
    store.resetDraft();
    expect(store.draft?.name).toBe(config().name);
    expect(store.dirty).toBe(false);
  });

  it("save：请求体只有改动过的字段，成功后重拉并回填草稿", async () => {
    const updatePersona = vi.fn(async () => outcome());
    const fetchPersona = vi.fn(async () => snapshot());
    configurePersonaApi({ updatePersona, fetchPersona });
    const store = usePersonaStore();
    await store.refresh();
    fetchPersona.mockClear();

    store.draft!.tone = "京腔、快嘴";
    expect(await store.save("换口吻试试")).toBe(true);
    expect(updatePersona).toHaveBeenCalledWith({ tone: "京腔、快嘴", reason: "换口吻试试" });
    expect(store.notice).toContain("已保存");
    expect(store.error).toBeNull();
    expect(fetchPersona).toHaveBeenCalledTimes(1);
  });

  it("save：没有改动 ⇒ 不发请求（如实说「没改动」，不假装保存了一次）", async () => {
    const updatePersona = vi.fn(async () => outcome());
    configurePersonaApi({ updatePersona });
    const store = usePersonaStore();
    await store.refresh();
    expect(await store.save()).toBe(false);
    expect(updatePersona).not.toHaveBeenCalled();
    expect(store.notice).toBe("没有改动，不需要保存");
  });

  it("★ save 撞 422：逐字段红字上桌，error 里带原因（不是一句干巴巴的「失败」）", async () => {
    configurePersonaApi({
      updatePersona: vi.fn(async () => {
        throw invalidForm("name", "String should have at least 1 character");
      }),
    });
    const store = usePersonaStore();
    await store.refresh();
    store.draft!.name = "";

    expect(await store.save()).toBe(false);
    expect(store.visibleFieldErrors.name).toBe("String should have at least 1 character");
    expect(store.error).toContain("name：String should have at least 1 character");
    expect(store.error).toContain("HTTP 422");
  });

  it("本地就能看出来的问题 ⇒ canSave 直接是 false（少一次来回）", async () => {
    const store = usePersonaStore();
    await store.refresh();
    expect(store.canSave).toBe(false);
    store.draft!.name = "";
    expect(store.localFieldErrors.name).toBeTruthy();
    expect(store.canSave).toBe(false);
  });

  it("保存成功 ⇒ 服务端红字被清掉（上一轮的错误不该跟着下一轮）", async () => {
    const store = usePersonaStore();
    await store.refresh();
    store.draft!.tone = "京腔";
    await store.save();
    expect(store.visibleFieldErrors).toEqual({});
  });

  it("activate：请求体显式带 backup:true（面板这一侧永远备份）", async () => {
    const activatePersona = vi.fn(async () => outcome({ action: "persona.activate" }));
    configurePersonaApi({ activatePersona });
    const store = usePersonaStore();
    await store.refresh();

    expect(await store.activate("solo_commentary")).toBe(true);
    expect(activatePersona).toHaveBeenCalledWith({
      persona_id: "solo_commentary",
      backup: true,
      reason: null,
    });
    expect(store.notice).toContain("已保存");
  });

  it("activate 失败 ⇒ error 就位，notice 不残留", async () => {
    configurePersonaApi({
      activatePersona: vi.fn(async () => {
        throw new ApiError("人物库中不存在：ghost", 404, { code: "PERSONA_NOT_FOUND" });
      }),
    });
    const store = usePersonaStore();
    await store.refresh();
    expect(await store.activate("ghost")).toBe(false);
    expect(store.error).toBe("人物库中不存在：ghost（HTTP 404）");
    expect(store.notice).toBeNull();
  });

  it("saveAs：带上 overwrite，notice 用后端那句（含「当前激活人物未变」）", async () => {
    const saveAsPersona = vi.fn(async () =>
      outcome({
        action: "persona.save_as",
        changed: false,
        backup: null,
        note: "已存进人物库：baseline_v2（当前激活人物未变，仍是「熊大熊二·MC跑酷」）",
      }),
    );
    configurePersonaApi({ saveAsPersona });
    const store = usePersonaStore();
    await store.refresh();

    expect(await store.saveAs("baseline_v2", true)).toBe(true);
    expect(saveAsPersona).toHaveBeenCalledWith({
      persona_id: "baseline_v2",
      overwrite: true,
      reason: null,
    });
    expect(store.notice).toContain("当前激活人物未变");
  });

  it("rollback：请求体带 backup:true（回滚前也备份 ⇒ 回滚错了还能滚回来）", async () => {
    const rollbackPersona = vi.fn(async () => outcome({ action: "persona.rollback" }));
    configurePersonaApi({ rollbackPersona });
    const store = usePersonaStore();
    await store.refresh();

    expect(await store.rollback("20260914-120000_persona_default.yaml")).toBe(true);
    expect(rollbackPersona).toHaveBeenCalledWith({
      name: "20260914-120000_persona_default.yaml",
      backup: true,
      reason: null,
    });
  });

  it("refresh 失败 ⇒ loadError 就位，但**旧快照留着**（一屏数字不因一次抖动变空白）", async () => {
    const store = usePersonaStore();
    await store.refresh();
    configurePersonaApi({
      fetchPersona: vi.fn(async () => {
        throw new ApiError("库锁住了", 503, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toBe("库锁住了（HTTP 503）");
    expect(store.snapshot).not.toBeNull();
    expect(store.active?.persona_id).toBe("persona_default");
  });
});
