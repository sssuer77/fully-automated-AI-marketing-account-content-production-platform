import { describe, expect, it, vi } from "vitest";

import { WsClient, type SocketLike, type WsClientOptions } from "./client";
import { WS_CLOSE_NORMAL, WS_CLOSE_PONG_TIMEOUT } from "./reconnect";

/** 假 socket：把"服务端行为"变成测试里可显式驱动的几个方法。 */
class FakeSocket implements SocketLike {
  readyState = 0;
  readonly sent: string[] = [];
  closed: { code?: number; reason?: string } | null = null;
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closed = { code, reason };
    this.readyState = 3;
    this.onclose?.({});
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  deliver(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

function frame(seq: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    v: 1,
    type: "event",
    channel: "tasks",
    seq,
    ts: "2026-09-13T00:00:00Z",
    data: {},
    ...overrides,
  };
}

function harness(overrides: Partial<WsClientOptions> = {}) {
  const sockets: FakeSocket[] = [];
  const gaps: { expected: number; received: number }[] = [];
  const statuses: string[] = [];
  const client = new WsClient({
    origin: "http://127.0.0.1:8787",
    channels: ["tasks", "logs"],
    socketFactory: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    handlers: {
      onGap: (info) => gaps.push(info),
      onStatus: (status) => statuses.push(status),
    },
    ...overrides,
  });
  return { client, sockets, gaps, statuses };
}

describe("WsClient 重连与补发", () => {
  it("断线后按 1s→2s 退避重连，并把 since_id 带上", () => {
    vi.useFakeTimers();
    try {
      const { client, sockets } = harness();
      client.start();
      expect(sockets).toHaveLength(1);
      expect(sockets[0].url).not.toContain("since_id");

      sockets[0].open();
      sockets[0].deliver(
        frame(1, { type: "snapshot", channel: "logs", data: { logs: [{ id: 7 }], truncated: false } }),
      );
      expect(client.cursor.sinceId).toBe(7);

      sockets[0].onclose?.({});
      expect(client.status).toBe("reconnecting");
      vi.advanceTimersByTime(999);
      expect(sockets).toHaveLength(1);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(2);
      expect(sockets[1].url).toContain("since_id=7");

      // 还没连上就又断了 ⇒ 退避升到 2s（不是又从 1s 开始）
      sockets[1].onclose?.({});
      vi.advanceTimersByTime(1999);
      expect(sockets).toHaveLength(2);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(3);

      // 成功连上过一次之后退避重置回 1s（长故障里偶发一次成功，不该继续罚等 15s）
      sockets[2].open();
      sockets[2].onclose?.({});
      vi.advanceTimersByTime(1000);
      expect(sockets).toHaveLength(4);

      client.stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("重连后重置 seq 基线（新连接 seq 从 1 重新开始，不该误判缺口）", () => {
    vi.useFakeTimers();
    try {
      const { client, sockets, gaps } = harness();
      client.start();
      sockets[0].open();
      sockets[0].deliver(frame(1));
      sockets[0].deliver(frame(2));

      sockets[0].onclose?.({});
      vi.advanceTimersByTime(1000);
      sockets[1].open();
      sockets[1].deliver(frame(1));

      expect(gaps).toEqual([]);
      expect(client.cursor.seq).toBe(1);
      client.stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stop() 之后不再重连", () => {
    vi.useFakeTimers();
    try {
      const { client, sockets } = harness();
      client.start();
      sockets[0].open();
      client.stop();
      expect(sockets[0].closed?.code).toBe(WS_CLOSE_NORMAL);
      vi.advanceTimersByTime(60000);
      expect(sockets).toHaveLength(1);
      expect(client.status).toBe("stopped");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("WsClient 缺口检测", () => {
  it("seq 跳号 ⇒ 自动发 resync 并上报 onGap", () => {
    const { client, sockets, gaps } = harness();
    client.start();
    sockets[0].open();
    sockets[0].deliver(
      frame(1, { type: "snapshot", channel: "logs", data: { logs: [], truncated: false } }),
    );
    sockets[0].deliver(frame(2));
    sockets[0].deliver(frame(5));

    expect(gaps).toEqual([{ expected: 3, received: 5 }]);
    expect(sockets[0].sent).toContain('{"type":"resync"}');
    expect(client.cursor.seq).toBe(5);
    client.stop();
  });

  it("补发帧按 seq 续上后不再重复 resync", () => {
    const { client, sockets, gaps } = harness();
    client.start();
    sockets[0].open();
    sockets[0].deliver(frame(1));
    sockets[0].deliver(frame(3));
    sockets[0].deliver(frame(4));
    expect(gaps).toHaveLength(1);
    expect(sockets[0].sent.filter((item) => item.includes("resync"))).toHaveLength(1);
    client.stop();
  });
});

describe("WsClient 心跳", () => {
  it("定时发 ping；一个周期没等到 pong 就主动断开重连", () => {
    vi.useFakeTimers();
    try {
      const { client, sockets } = harness({ pingIntervalMs: 1000 });
      client.start();
      sockets[0].open();

      vi.advanceTimersByTime(1000);
      expect(sockets[0].sent).toEqual(['{"type":"ping"}']);

      vi.advanceTimersByTime(1000); // 这一轮之前没有 pong ⇒ 判定链路已死
      expect(sockets[0].closed?.code).toBe(WS_CLOSE_PONG_TIMEOUT);
      expect(client.status).toBe("reconnecting");

      vi.advanceTimersByTime(1000);
      expect(sockets).toHaveLength(2);
      client.stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("收到 pong 就继续存活，不误判断线", () => {
    vi.useFakeTimers();
    try {
      const { client, sockets } = harness({ pingIntervalMs: 1000 });
      client.start();
      sockets[0].open();

      vi.advanceTimersByTime(1000);
      sockets[0].deliver({ v: 1, type: "pong", channel: "control", seq: 1, ts: "t", data: {} });
      vi.advanceTimersByTime(1000);

      expect(sockets[0].closed).toBeNull();
      expect(sockets[0].sent).toHaveLength(2);
      client.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});
