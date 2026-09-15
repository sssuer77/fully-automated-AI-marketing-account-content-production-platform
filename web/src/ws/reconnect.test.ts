import { describe, expect, it } from "vitest";

import {
  BACKOFF_STEPS_MS,
  MAX_BACKOFF_MS,
  backoffDelay,
  buildWsUrl,
  detectGap,
  isEnvelope,
} from "./reconnect";

describe("backoffDelay", () => {
  it("按 1s→2s→4s→8s→15s 递增，之后恒为 15s（永不放弃重连）", () => {
    expect([1, 2, 3, 4, 5, 6].map(backoffDelay)).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
    expect(backoffDelay(100)).toBe(MAX_BACKOFF_MS);
    expect(BACKOFF_STEPS_MS.at(-1)).toBe(MAX_BACKOFF_MS);
  });

  it("脏输入回落到第一档（绝不返回 NaN 把重连卡死）", () => {
    expect(backoffDelay(0)).toBe(1000);
    expect(backoffDelay(-3)).toBe(1000);
    expect(backoffDelay(Number.NaN)).toBe(1000);
  });
});

describe("detectGap", () => {
  it("新连接的第一帧只建基线，不算缺口", () => {
    expect(detectGap(0, 1)).toBe(false);
    expect(detectGap(0, 17)).toBe(false);
  });

  it("必须严格 +1，否则判缺口", () => {
    expect(detectGap(5, 6)).toBe(false);
    expect(detectGap(5, 7)).toBe(true);
    expect(detectGap(5, 5)).toBe(true);
    expect(detectGap(5, 4)).toBe(true);
  });
});

describe("buildWsUrl", () => {
  it("http→ws，带上通道 / 任务 / 级别 / 游标", () => {
    const url = buildWsUrl({
      origin: "http://127.0.0.1:8787",
      channels: ["tasks", "logs"],
      taskIds: ["t-1"],
      minLevel: "info",
      sinceId: 42,
    });
    expect(url.startsWith("ws://127.0.0.1:8787/ws/ui?")).toBe(true);
    const params = new URLSearchParams(url.slice(url.indexOf("?") + 1));
    expect(params.get("channels")).toBe("tasks,logs");
    expect(params.get("task_ids")).toBe("t-1");
    expect(params.get("min_level")).toBe("info");
    expect(params.get("since_id")).toBe("42");
  });

  it("https→wss；没有查询参数时不产生裸问号", () => {
    expect(buildWsUrl({ origin: "https://box.local:8443" })).toBe("wss://box.local:8443/ws/ui");
  });

  it("since_id=0 必须发出去（0 是有效游标，语义与不传不同）", () => {
    expect(buildWsUrl({ origin: "http://h", sinceId: 0 })).toContain("since_id=0");
    expect(buildWsUrl({ origin: "http://h", sinceId: null })).not.toContain("since_id");
  });
});

describe("isEnvelope", () => {
  it("挡住非对象 / 缺字段的坏帧", () => {
    expect(isEnvelope(null)).toBe(false);
    expect(isEnvelope("{}")).toBe(false);
    expect(isEnvelope({ type: "event", channel: "logs" })).toBe(false);
    expect(isEnvelope({ type: "event", channel: "logs", seq: 1, data: {} })).toBe(true);
  });
});
