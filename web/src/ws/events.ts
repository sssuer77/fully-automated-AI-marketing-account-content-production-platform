// ⚠️ 本文件由 `tasks.ps1 web:gen` 自动生成，**禁止手改**。
// 真相源：`src/studio/ws/protocol.py` + `src/studio/services/log_service.py`；
// 漂移由 `tests/contract/test_web_contracts.py` 拦截。

export const WS_PROTOCOL_VERSION = 1;
export const WS_PATH = "/ws/ui";

export const CHANNELS = [
  "tasks",
  "logs",
  "pools",
  "metrics",
  "system",
  "control",
  "topics",
  "publish",
] as const;
export type Channel = (typeof CHANNELS)[number];

export const FRAME_TYPES = [
  "snapshot",
  "event",
  "ack",
  "error",
  "pong",
] as const;
export type FrameType = (typeof FRAME_TYPES)[number];

export const EVENT_KINDS = [
  "task.created",
  "task.updated",
  "task.transition",
  "task.progress_detail",
  "task.completed",
  "task.failed",
  "sentence.updated",
  "review.scored",
  "script.editing",
  "degraded",
  "approval.requested",
  "approval.decided",
  "direction.batch_ready",
  "topic.batch_ready",
  "topic.selected",
  "topic.dedup_warn",
  "publish.queued",
  "publish.progress",
  "publish.done",
  "publish.failed",
  "publish.manual_required",
  "metrics.updated",
  "log.appended",
  "pool.stats",
  "pool.worker_status",
  "metrics.tick",
  "system.alert",
  "system.doctor",
  "system.persona_changed",
  "command.result",
] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

export const ALERT_CODES = [
  "DISK_LOW",
  "DISK_CRITICAL",
  "TTS_CIRCUIT_OPEN",
  "JOB_DEAD",
  "POOL_AUTODEGRADED",
  "PUBLISH_LOGIN_EXPIRED",
  "DUP_AUDIT_WARN",
  "BROLL_EMPTY",
] as const;
export type AlertCode = (typeof ALERT_CODES)[number];

/** 事件种类 → 归属通道（前端按它决定一条事件该进哪个面板）。 */
export const KIND_CHANNEL: Record<EventKind, Channel> = {
  "task.created": "tasks",
  "task.updated": "tasks",
  "task.transition": "tasks",
  "task.progress_detail": "tasks",
  "task.completed": "tasks",
  "task.failed": "tasks",
  "sentence.updated": "tasks",
  "review.scored": "tasks",
  "script.editing": "tasks",
  "degraded": "tasks",
  "approval.requested": "tasks",
  "approval.decided": "tasks",
  "direction.batch_ready": "topics",
  "topic.batch_ready": "topics",
  "topic.selected": "topics",
  "topic.dedup_warn": "topics",
  "publish.queued": "publish",
  "publish.progress": "publish",
  "publish.done": "publish",
  "publish.failed": "publish",
  "publish.manual_required": "publish",
  "metrics.updated": "publish",
  "log.appended": "logs",
  "pool.stats": "pools",
  "pool.worker_status": "pools",
  "metrics.tick": "metrics",
  "system.alert": "system",
  "system.doctor": "system",
  "system.persona_changed": "system",
  "command.result": "control",
};

/** 级别序（只用于比较；与 `system_logs.level` 的 CHECK 同源）。 */
export const SEVERITY_ORDER: Record<string, number> = {
  "debug": 10,
  "info": 20,
  "warn": 30,
  "error": 40,
  "fatal": 50,
};
export const DEFAULT_MIN_LEVEL = "info";

/** 服务端背压参数（前端据此理解 `WS_CLIENT_SLOW` 断开的含义）。 */
export const COALESCE_WINDOW_MS = 100;
export const RING_CAPACITY = 200;
export const SLOW_SUSTAIN_SEC = 10.0;
export const REPLAY_LIMIT = 2000;

/** 统一报文信封（§04.4.2）。`seq` 是**每连接**单调，用于缺口检测。 */
export interface Envelope {
  v: number;
  type: FrameType;
  channel: Channel;
  seq: number;
  ts: string;
  data: Record<string, unknown>;
}

/** `system_logs` 一行（= `SystemLog.to_dict()`；快照与日志事件共用）。 */
export interface SystemLogRow {
  id: number;
  ts: string;
  level: string;
  source: string;
  message: string;
  task_id: string | null;
  job_id: string | null;
  stage: string | null;
  unit_ref: string | null;
  worker_id: string | null;
  trace_id: string | null;
  duration_ms: number | null;
  seq_in_task: number | null;
  payload: Record<string, unknown>;
}

/** `logs` 通道快照载荷（§04.4.6）。 */
export interface LogSnapshot {
  logs: SystemLogRow[];
  truncated: boolean;
  hint?: string;
}
