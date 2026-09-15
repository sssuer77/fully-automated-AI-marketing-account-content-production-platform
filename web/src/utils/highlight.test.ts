import { describe, expect, it } from "vitest";

import { splitHighlight } from "./highlight";

describe("splitHighlight", () => {
  it("空查询 ⇒ 整段不命中", () => {
    expect(splitHighlight("hello", "")).toEqual([{ text: "hello", hit: false }]);
    expect(splitHighlight("hello", "   ")).toEqual([{ text: "hello", hit: false }]);
  });

  it("切出命中段（忽略大小写，但保留原文大小写）", () => {
    expect(splitHighlight("Scene_001 done", "scene")).toEqual([
      { text: "Scene", hit: true },
      { text: "_001 done", hit: false },
    ]);
  });

  it("多次命中全部标出", () => {
    expect(splitHighlight("aXbXc", "X")).toEqual([
      { text: "a", hit: false },
      { text: "X", hit: true },
      { text: "b", hit: false },
      { text: "X", hit: true },
      { text: "c", hit: false },
    ]);
  });

  it("命中在开头 / 结尾时不留空片段", () => {
    expect(splitHighlight("abc", "abc")).toEqual([{ text: "abc", hit: true }]);
    expect(splitHighlight("abc", "c")).toEqual([
      { text: "ab", hit: false },
      { text: "c", hit: true },
    ]);
  });

  it("未命中 ⇒ 原样一段", () => {
    expect(splitHighlight("abc", "z")).toEqual([{ text: "abc", hit: false }]);
  });
});
