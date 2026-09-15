// 通道订阅的组件侧封装（T4.1 · §04.4）。
//
// 组件里用 `useChannelStream("tasks", handler)`：卸载时自动退订（靠 effect scope），
// 不需要每个面板各写一遍 `onUnmounted`。组件外（store / 测试）调用则直接拿退订函数。

import { getCurrentScope, onScopeDispose } from "vue";

import { ensureWsConnection, onChannel } from "./useWsConnection";
import type { Channel, Envelope } from "@/ws/events";

/** 订阅一个通道；返回退订函数（组件内自动退订）。 */
export function useChannelStream(
  channel: Channel,
  listener: (envelope: Envelope) => void,
): () => void {
  ensureWsConnection();
  const unsubscribe = onChannel(channel, listener);
  if (getCurrentScope()) onScopeDispose(unsubscribe);
  return unsubscribe;
}

/** `tasks` 通道的便捷封装（①–⑥ 面板都只看它）。 */
export function useTaskStream(listener: (envelope: Envelope) => void): () => void {
  return useChannelStream("tasks", listener);
}
