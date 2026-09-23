// 人物贴图面板（T6.5）的**口径守卫**。
//
// 为什么不写成组件测试：这个前端没有 jsdom / @vue/test-utils（与 `voicesPlayers.test.ts`
// 同一条理由），为了几个绑定去引两套依赖不划算。这里断言的是**两处口径一致**，而不是
// "渲染出来长什么样"：
//
// ① 每层字段的错误槽位必须覆盖后端 `STICKER_FIELDS` 的**全部**字段。面板把 422 的
//    `field_errors` 标到输入框上，靠的就是 `stickers.<层名>.<字段>` 这个命名；少一个槽位，
//    那个字段存不进去时**面板上什么都不显示**（只剩一句看不懂的 422）—— 这是这一屏最隐蔽的坏法。
// ② 尺寸绑的是 `height_ratio`，不是水印那套 `width_ratio` —— 人物是竖长的，按宽算会在
//    1080 宽的画布上被锁死在 270px。
// ③ 面板里没有"加一层 / 删一层"的入口：层是 YAML 里的命名块，面板只改字段、不改结构。
// ④ 「开着却贴不上」必须在面板上说出来，而且判据要与渲染路径**同源**（`usable`，
//    不是 `exists`）—— 真机上踩到的就是这一条：两张"PNG"其实是 WebP / JPEG。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const source = readFileSync(fileURLToPath(new URL("./Outputs.vue", import.meta.url)), "utf-8");

/**
 * 后端那张白名单的**真值**（现读源码，不抄第二份）。
 *
 * 抄一份到前端当断言，等于"两处一起错"也能通过：字段改名的那天测试跟着一起改，
 * 而面板上的框仍然填了存不进去。现读源码，改名的当天这里就红。
 */
function backendStickerFields(): string[] {
  const backend = readFileSync(
    fileURLToPath(new URL("../../../src/studio/core/outputs_store.py", import.meta.url)),
    "utf8",
  );
  const match = /STICKER_FIELDS: Final\[tuple\[str, \.\.\.\]\] = \(([\s\S]*?)\)/.exec(backend);
  if (match === null) throw new Error("后端没有 STICKER_FIELDS（白名单改名了？）");
  return [...match[1].matchAll(/"([^"]+)"/g)].map((row) => row[1]);
}

/** 面板上真正挂了错误槽位的字段（从模板里现抠）。 */
function panelErrorFields(): string[] {
  const slots = source.matchAll(/errors\[`stickers\.\$\{row\.name\}\.([a-z_]+)`\]/g);
  return [...new Set([...slots].map((slot) => slot[1]))].sort();
}

describe("人物贴图面板", () => {
  it("★ 错误槽位与后端白名单一一对应（少一个 ⇒ 那个字段存不进去时面板不吭声）", () => {
    // `enabled` 是勾选框：填不出非法值，后端也不会对它报错，所以它没有槽位。
    const allowed = backendStickerFields().filter((field) => field !== "enabled");
    expect(panelErrorFields()).toEqual([...allowed].sort());
  });

  it("逐层渲染：层数由服务端给（面板不改结构）", () => {
    expect(source).toContain('v-for="row in stickerRows"');
    expect(source).toMatch(/const stickerRows = computed\(\(\) =>\s*outputs\.stickers\.map/);
  });

  it("尺寸绑的是高度占比（按宽算会把人物锁死在 270px）", () => {
    expect(source).toContain("onStickerFloat(row.name, 'height_ratio', $event)");
    expect(source).not.toContain("onStickerFloat(row.name, 'width_ratio'");
  });

  it("没有加层 / 删层的入口（层是 YAML 里的命名块）", () => {
    for (const forbidden of ["onStickerAdd", "onStickerRemove", "stickers.push", "stickers.splice"]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("★ 那一行的灯看 `usable`，不看 `exists`（盘上有文件 ≠ 渲染会贴上）", () => {
    expect(source).toContain("stickerTone(row.enabled, row.usable)");
    expect(source).not.toContain("stickerTone(row.enabled, row.exists)");
  });

  it("★ 开着却贴不上 ⇒ 逐层把**原因**写出来（静默跳过是这一屏最贵的坏法）", () => {
    expect(source).toContain("v-if=\"stickersBroken.length > 0\"");
    expect(source).toMatch(/stickersBroken[\s\S]{0,400}?sticker\.problem/);
  });

  it("★ 勾选框当场写盘（开关长得像开关，勾完就该是那个状态）", () => {
    expect(source).toMatch(
      /async function onStickerBool[\s\S]{0,500}?await outputs\.saveToggle\(/,
    );
    expect(source).toContain("outputs.saveToggle");
  });
});
