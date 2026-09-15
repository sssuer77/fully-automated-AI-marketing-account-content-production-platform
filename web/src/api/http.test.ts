import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiGet, apiPatch, apiPost, buildQuery } from "./http";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildQuery", () => {
  it("跳过 null / undefined / 空串（后端把 null 当没传，发了反而撞 422）", () => {
    expect(buildQuery({ a: 1, b: null, c: undefined, d: "", e: "x" })).toBe("?a=1&e=x");
  });

  it("保留 0 与 false（它们是有效取值，不是空值）", () => {
    expect(buildQuery({ since_id: 0, all: false })).toBe("?since_id=0&all=false");
  });

  it("没有可用参数时不产生裸问号", () => {
    expect(buildQuery(undefined)).toBe("");
    expect(buildQuery({})).toBe("");
    expect(buildQuery({ a: null })).toBe("");
  });
});

describe("apiGet", () => {
  it("2xx ⇒ 返回 JSON 体", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    await expect(apiGet("/api/v1/health")).resolves.toEqual({ status: "ok" });
  });

  it("非 2xx ⇒ 抛 ApiError（带 status 与后端 detail）", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify({ detail: "boom" }), {
          status: 500,
          headers: { "content-type": "application/json" },
        }),
    );
    const error = await apiGet("/api/v1/health").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    expect((error as ApiError).detail).toEqual({ detail: "boom" });
  });

  it("网络异常 ⇒ 包成 ApiError（status=0），调用方只需处理一种错误", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("failed to connect");
    });
    const error = await apiGet("/api/v1/health").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });
});

describe("apiPost / apiPatch", () => {
  it("两者只差方法名，走的是同一套超时与错误信封", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: string, init: RequestInit) => {
      calls.push(`${String(init.method)} ${input}`);
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    await apiPost("/api/v1/assets/ingest", { license: "cc0" });
    await apiPatch("/api/v1/assets/parkour_001", { enabled: false });

    expect(calls).toEqual([
      "POST /api/v1/assets/ingest",
      "PATCH /api/v1/assets/parkour_001",
    ]);
  });

  it("请求体省略时发 `{}`（「放行」这种动作本来就可能没有意见）", async () => {
    let body: unknown = null;
    vi.stubGlobal("fetch", async (_input: string, init: RequestInit) => {
      body = init.body;
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });

    await apiPost("/api/v1/approvals/x/approve");
    expect(body).toBe("{}");
  });

  it("PATCH 的非 2xx 也走统一信封（422 的 message 会冒到调用方）", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify({ code: "ASSET_INVALID", message: "授权类型不合法：nope" }), {
          status: 422,
          headers: { "content-type": "application/json" },
        }),
    );
    const error = await apiPatch("/api/v1/assets/parkour_001", { license: "nope" }).catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).message).toContain("授权类型不合法");
  });
});
