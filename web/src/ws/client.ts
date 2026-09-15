// WS 客户端（T4.1 · §04.4）—— 单连接多路复用 + 断线重连 + 游标补发。
//
// 设计取舍：**一条连接订阅全部通道**（`?channels=` 一次给全），而不是每个面板各开一条。
// 服务端的合并/限流是 Hub 侧全局算的（协议不变量 4），连接数与面板数脱钩才能让
// "慢客户端"判定（RING_CAPACITY / SLOW_SUSTAIN_SEC）保持可解释；前端侧也就只需要
// 维护**一份** seq 与 since_id 游标。
//
// 纯策略（退避 / 缺口 / URL）住在 `reconnect.ts`，本文件只负责 socket 生命周期。

import { DEFAULT_MIN_LEVEL, type Channel, type Envelope } from "./events";
import {
  PING_INTERVAL_MS,
  WS_CLOSE_NORMAL,
  WS_CLOSE_PONG_TIMEOUT,
  asLogId,
  backoffDelay,
  buildWsUrl,
  detectGap,
  isEnvelope,
  type WsStatus,
} from "./reconnect";

export * from "./reconnect";

/** 浏览器 `WebSocket` 的最小结构面（单测注入假实现即可，无需 jsdom）。 */
export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

/** `WebSocket.OPEN`（写成常量，免得在 node 环境里碰 `WebSocket` 全局）。 */
export const SOCKET_OPEN = 1;

export interface WsCursor {
  /** 本连接最后收到的 `seq`（0 = 还没收到过帧）。 */
  seq: number;
  /** 最后一条日志 id（= 重连时的 `since_id`）。 */
  sinceId: number | null;
}

export interface WsClientHandlers {
  onFrame?(envelope: Envelope): void;
  onStatus?(status: WsStatus, cursor: WsCursor): void;
  /** `seq` 缺口（诊断用；客户端已自动发 `resync`）。 */
  onGap?(info: { expected: number; received: number }): void;
  /** 日志游标推进（= 后端"先落库再广播"在前端的落点）。 */
  onLogId?(logId: number): void;
}

export interface WsClientOptions {
  /** 默认取 `window.location.origin`。 */
  origin?: string;
  channels?: readonly Channel[];
  taskIds?: readonly string[];
  minLevel?: string;
  token?: string | null;
  pingIntervalMs?: number;
  socketFactory?: SocketFactory;
  handlers?: WsClientHandlers;
  /** 定时器注入（单测用假定时器；生产用全局 `setTimeout`）。 */
  setTimer?: typeof setTimeout;
  clearTimer?: typeof clearTimeout;
}

const defaultFactory: SocketFactory = (url) => new WebSocket(url) as unknown as SocketLike;

function defaultOrigin(): string {
  if (typeof window !== "undefined" && window.location) return window.location.origin;
  return "http://127.0.0.1:8787";
}

export class WsClient {
  private readonly origin: string | undefined;
  private readonly channels: readonly Channel[];
  private readonly taskIds: readonly string[];
  private readonly minLevel: string;
  private readonly token: string | null;
  private readonly pingIntervalMs: number;
  private readonly factory: SocketFactory;
  private readonly setTimer: typeof setTimeout;
  private readonly clearTimer: typeof clearTimeout;

  private handlers: WsClientHandlers;
  private socket: SocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private lastSeq = 0;
  private lastLogId: number | null = null;
  private awaitingPong = false;
  private stopped = true;
  private currentUrl: string | null = null;
  private state: WsStatus = "idle";

  constructor(options: WsClientOptions = {}) {
    this.origin = options.origin;
    this.channels = options.channels ?? [];
    this.taskIds = options.taskIds ?? [];
    this.minLevel = options.minLevel ?? DEFAULT_MIN_LEVEL;
    this.token = options.token ?? null;
    this.pingIntervalMs = options.pingIntervalMs ?? PING_INTERVAL_MS;
    this.factory = options.socketFactory ?? defaultFactory;
    this.setTimer = options.setTimer ?? setTimeout;
    this.clearTimer = options.clearTimer ?? clearTimeout;
    this.handlers = options.handlers ?? {};
  }

  get status(): WsStatus {
    return this.state;
  }

  get url(): string | null {
    return this.currentUrl;
  }

  get cursor(): WsCursor {
    return { seq: this.lastSeq, sinceId: this.lastLogId };
  }

  setHandlers(handlers: WsClientHandlers): void {
    this.handlers = handlers;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.connect();
  }

  stop(): void {
    // 先置位再 close：否则 `onclose` 会顺手排一次重连。
    this.stopped = true;
    this.clearReconnectTimer();
    this.clearPingTimer();
    const socket = this.socket;
    this.socket = null;
    this.emitStatus("stopped");
    if (socket) socket.close(WS_CLOSE_NORMAL, "client stop");
  }

  /** "重新同步"按钮：断开重连一次（会带上当前 `since_id`）。 */
  restart(): void {
    this.stop();
    this.start();
  }

  /** 主动请求重放（`seq` 缺口 / 用户点"补齐"）。 */
  resync(): boolean {
    return this.send({ type: "resync" });
  }

  /** 发一条上行报文；未连接 ⇒ `false`（调用方自己决定要不要记日志）。 */
  send(payload: Record<string, unknown>): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== SOCKET_OPEN) return false;
    socket.send(JSON.stringify(payload));
    return true;
  }

  /**
   * 推进日志游标（快照与事件共用；**只进不退**）。
   *
   * 这是"重连补齐"的输入：`since_id` 一旦回退，补发就会重复或漏 —— 而两者都
   * 只在真断线时才暴露，所以宁可在这里挡住。
   */
  noteLogId(logId: number | null | undefined): void {
    if (logId === null || logId === undefined) return;
    if (this.lastLogId !== null && logId <= this.lastLogId) return;
    this.lastLogId = logId;
    this.handlers.onLogId?.(logId);
  }

  private connect(): void {
    const url = buildWsUrl({
      origin: this.origin ?? defaultOrigin(),
      channels: this.channels,
      taskIds: this.taskIds,
      minLevel: this.minLevel,
      sinceId: this.lastLogId,
      token: this.token,
    });
    this.currentUrl = url;
    this.emitStatus("connecting");
    const socket = this.factory(url);
    this.socket = socket;
    socket.onopen = () => {
      this.handleOpen();
    };
    socket.onmessage = (event) => {
      this.handleMessage(event.data);
    };
    socket.onclose = () => {
      this.handleClose();
    };
    socket.onerror = () => {
      // `onclose` 一定会跟着来；这里再排一次重连就会变成两条重连链。
    };
  }

  private handleOpen(): void {
    this.attempt = 0;
    this.awaitingPong = false;
    // 新连接的 `seq` 从 1 重新开始（每连接单调）⇒ 基线必须重置，否则第一帧会被
    // 误判成缺口，每重连一次就白跑一轮 resync。
    this.lastSeq = 0;
    this.emitStatus("open");
    this.schedulePing();
  }

  private handleMessage(raw: unknown): void {
    if (typeof raw !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isEnvelope(parsed)) return;
    if (parsed.type === "pong") this.awaitingPong = false;
    this.trackLogCursor(parsed);
    if (detectGap(this.lastSeq, parsed.seq)) {
      this.handlers.onGap?.({ expected: this.lastSeq + 1, received: parsed.seq });
      this.resync();
    }
    this.lastSeq = parsed.seq;
    this.handlers.onFrame?.(parsed);
  }

  /** 从快照行 / `log.appended` 事件里推进 `since_id`。 */
  private trackLogCursor(envelope: Envelope): void {
    if (envelope.data["kind"] === "log.appended") {
      this.noteLogId(asLogId(envelope.data["log_id"]));
      return;
    }
    if (envelope.type !== "snapshot" || envelope.channel !== "logs") return;
    const rows = envelope.data["logs"];
    if (!Array.isArray(rows)) return;
    for (const row of rows as unknown[]) {
      if (typeof row === "object" && row !== null) {
        this.noteLogId(asLogId((row as { id?: unknown }).id));
      }
    }
  }

  private handleClose(): void {
    this.socket = null;
    this.clearPingTimer();
    if (this.stopped) return;
    this.attempt += 1;
    const delay = backoffDelay(this.attempt);
    this.emitStatus("reconnecting");
    this.reconnectTimer = this.setTimer(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.connect();
    }, delay);
  }

  private schedulePing(): void {
    this.clearPingTimer();
    this.pingTimer = this.setTimer(() => {
      this.pingTimer = null;
      const socket = this.socket;
      if (this.stopped || !socket) return;
      if (this.awaitingPong) {
        // 上行链路已死（TCP 半开）——`onclose` 不会自己来，必须主动踢。
        socket.close(WS_CLOSE_PONG_TIMEOUT, "pong timeout");
        return;
      }
      this.awaitingPong = true;
      this.send({ type: "ping" });
      this.schedulePing();
    }, this.pingIntervalMs);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      this.clearTimer(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearPingTimer(): void {
    if (this.pingTimer !== null) {
      this.clearTimer(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private emitStatus(status: WsStatus): void {
    this.state = status;
    this.handlers.onStatus?.(status, this.cursor);
  }
}
