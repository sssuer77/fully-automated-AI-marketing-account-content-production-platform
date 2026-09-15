// 全局唯一 WS 连接（T4.1 · §04.4）—— 面板只订阅通道，不各自建连。
//
// 为什么是模块级单例而不是 provide/inject：整个操作台只有一条连接（见 `ws/client.ts`
// 的取舍说明），而 store 与 composable 都要用它。用单例就把"谁先挂载"这个顺序问题
// 彻底消掉 —— 后挂载的面板订阅到的只是同一条连接上的回调集合。

import { ref, type Ref } from "vue";

import { WsClient, type WsCursor, type WsStatus } from "@/ws/client";
import { CHANNELS, type Channel, type Envelope } from "@/ws/events";

type ChannelListener = (envelope: Envelope) => void;

const listeners = new Map<Channel, Set<ChannelListener>>();

let client: WsClient | null = null;

/** 连接状态与游标（模块级 ref：任何组件读到的都是同一份）。 */
const status: Ref<WsStatus> = ref<WsStatus>("idle");
const cursor: Ref<WsCursor> = ref<WsCursor>({ seq: 0, sinceId: null });

function dispatch(envelope: Envelope): void {
  const bucket = listeners.get(envelope.channel);
  if (!bucket) return;
  for (const listener of bucket) listener(envelope);
}

/** 拿到（首次调用时创建并启动）全局连接；重复调用返回同一个实例。 */
export function ensureWsConnection(): WsClient {
  if (client) return client;
  client = new WsClient({
    channels: CHANNELS,
    // 订阅下限取 `debug`（= "把库里有的都给我"）：级别过滤一律在客户端做。
    // 若把过滤交给服务端，每换一次级别都要重连 + 重放，而"刚滚过去的那几行"会在
    // 重连窗口里消失 —— 排查时最气人的正是这种。库本身不落 `debug`（§04.5.2），
    // 所以这个下限不会真的多传什么（T4.9 裁定 119）。
    minLevel: "debug",
    handlers: {
      onFrame: dispatch,
      onStatus: (next, current) => {
        status.value = next;
        cursor.value = current;
      },
      onLogId: (logId) => {
        cursor.value = { seq: cursor.value.seq, sinceId: logId };
      },
    },
  });
  client.start();
  return client;
}

/** 订阅某个通道；返回退订函数。 */
export function onChannel(channel: Channel, listener: ChannelListener): () => void {
  let bucket = listeners.get(channel);
  if (!bucket) {
    bucket = new Set<ChannelListener>();
    listeners.set(channel, bucket);
  }
  bucket.add(listener);
  return () => {
    bucket.delete(listener);
  };
}

export interface WsConnection {
  status: Ref<WsStatus>;
  cursor: Ref<WsCursor>;
  connect(): WsClient;
  onChannel(channel: Channel, listener: ChannelListener): () => void;
  resync(): boolean;
  restart(): void;
}

export function useWsConnection(): WsConnection {
  return {
    status,
    cursor,
    connect: ensureWsConnection,
    onChannel,
    resync: () => ensureWsConnection().resync(),
    restart: () => ensureWsConnection().restart(),
  };
}
