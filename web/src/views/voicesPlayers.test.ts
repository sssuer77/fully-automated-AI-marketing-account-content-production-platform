// 配音面板「试听」那一列的样式守卫（真机 2026-09-22）。
//
// 真机症状：逐句表里「试听」一列**整列空的** —— 元素在、`audio_url` 也在
// （后端 55 句全给了），只是看不见。
//
// 根因是**两个用途撞了一个类名**：
//   · 逐句那一列的播放器（要看得见）：`<audio class="player" controls>`；
//   · 页面底下那个只用来放音色样本的隐藏播放器：`<audio ref="player" class="player" />`。
// 而后者后来补了一条 `.player { display: none }`，写在**后面** ⇒ 前者一起被藏了。
//
// 为什么不写成组件测试：这个前端没有 jsdom / @vue/test-utils，为了一个类名去引两套依赖
// 不划算。守卫直接读源码 —— 与「前端字段清单现读后端白名单」（裁定 358）同一条思路：
// 断言的是**两处口径一致**，而不是"渲染出来长什么样"。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const source = readFileSync(fileURLToPath(new URL("./Voices.vue", import.meta.url)), "utf-8");

describe("配音面板的播放器类名", () => {
  it("隐藏的那个播放器有自己的类名", () => {
    expect(source).toContain('class="player player--hidden"');
  });

  it("`.player` 不带 display:none（逐句那一列用的就是它）", () => {
    expect(source).not.toMatch(/\.player\s*\{[^}]*display:\s*none/);
  });

  it("`.player--hidden` 才是被藏起来的那个", () => {
    expect(source).toMatch(/\.player--hidden\s*\{[^}]*display:\s*none/);
  });

  it("逐句那一列的播放器有 controls（能点、能拖）", () => {
    expect(source).toMatch(/<audio[\s\S]{0,200}?class="player"[\s\S]{0,200}?controls/);
  });
});
