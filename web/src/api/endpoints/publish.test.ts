// 发布面板里两个**长动作**的超时口径（T6.4 · 真机坑 2026-09-23）。
//
// 为什么这两个数值得单独钉住
// ------------------------
// 「检测登录态」在真机上被 `http.ts` 那个 10s 的默认超时掐断过：服务端其实探成功了
// （日志里是 info），而面板上是一行红字 `/probe 超时（10000 ms）`。这类 bug 的**代码**
// 全是好的、只有那个数不对 —— 不钉住的话，下次谁把 `timeoutMs` 顺手删掉，症状一模一样。
//
// 判据是"超时**没有**提前触发"：fetch 被换成一个永不落地的 promise，只观察 signal
// 在什么时候被 abort。默认值那 10s 一到就 abort ⇒ 这一条当场红。

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ASSIST_UPLOAD_SEC,
  LOGIN_TIMEOUT_SEC,
  MANUAL_VERIFY_WAIT_SEC,
  PROBE_TIMEOUT_SEC,
  assistPublication,
  loginAccount,
  probeAccount,
} from "./publish";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** 把 fetch 换成"永不落地"，把这次请求的 URL 与 signal 交回来。 */
function stubPendingFetch(): { url: string; signal: AbortSignal | undefined } {
  const seen: { url: string; signal: AbortSignal | undefined } = { url: "", signal: undefined };
  vi.stubGlobal("fetch", (input: string, init: RequestInit) => {
    seen.url = String(input);
    seen.signal = init.signal ?? undefined;
    return new Promise<Response>(() => {});
  });
  return seen;
}

describe("probeAccount", () => {
  it("超时比默认值宽得多 —— 探测要冷启动浏览器再等 SPA 渲染", async () => {
    vi.useFakeTimers();
    const seen = stubPendingFetch();

    void probeAccount("acc_main").catch(() => {});

    expect(seen.url).toBe("/api/v1/publish/accounts/acc_main/probe");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(seen.signal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_SEC * 1000);
    expect(seen.signal?.aborted).toBe(true);
  });
});

describe("loginAccount", () => {
  it("前端比服务端等得久 —— 否则人还在扫、面板已经 abort 掉了", async () => {
    vi.useFakeTimers();
    const seen = stubPendingFetch();

    void loginAccount("acc_main").catch(() => {});

    expect(seen.url).toContain("timeout_sec=" + String(LOGIN_TIMEOUT_SEC));
    await vi.advanceTimersByTimeAsync(LOGIN_TIMEOUT_SEC * 1000);
    expect(seen.signal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(seen.signal?.aborted).toBe(true);
  });
});

describe("assistPublication", () => {
  it("超时盖住「重新上传 + 等人输码」**两段** —— 短一段就是人在输码时面板先放弃", async () => {
    vi.useFakeTimers();
    const seen = stubPendingFetch();

    void assistPublication("pub-1", { actor: "user" }).catch(() => {});

    expect(seen.url).toBe("/api/v1/publish/pub-1/assist");
    // 走到服务端那两段上限的**总和**时还不许 abort：上传一遍成片 + 人掏手机输码，
    // 两件事前后发生，谁都不该被对方的预算挤掉。
    await vi.advanceTimersByTimeAsync((MANUAL_VERIFY_WAIT_SEC + ASSIST_UPLOAD_SEC) * 1000);
    expect(seen.signal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(seen.signal?.aborted).toBe(true);
  });
});
