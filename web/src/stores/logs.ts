// 实时日志面板的状态（T4.1 建骨架 · T4.9 补齐过滤 / 搜索 / 告警 / 补洞 / 导出）。
//
// 这个 store 要同时满足四件互相拉扯的事，先把矛盾摆出来：
//
// 1. **看得快**：WS 是低延迟流，但服务端有 100ms 合并窗口（同 channel + 同实体的
//    日志在窗口内只留最后一条）⇒ **实时流本身是有损的**，这是设计取舍，不是 bug。
// 2. **看得全**：面板必须能说"没丢"。所以收到一个"跳号的 id"就说明中间有洞，
//    立刻用 REST `since_id` 把它补回来（裁定 121）。WS 负责快，REST 负责全。
// 3. **切过滤要即时**：过滤一律在客户端做（服务端订阅取 `min_level=debug`，
//    = "把库里有的都给我"）。若过滤走服务端，每换一次级别就要重连 + 重放，
//    而"刚滚过去的那几行"会在重连窗口里消失 —— 排查时最气人的就是这种。
// 4. **告警永不消失**：`system.alert` 不走 `logs` 通道（Hub 按 `is_alert_code` 分流），
//    所以本 store 必须**同时订阅 `system` 通道**；且告警不受级别过滤影响。
//
// 两个易错点，代码里显式挡掉：
// - WS 事件用 `log_id`，快照行 / REST 用 `id`（陷阱 #59）⇒ 统一走归一函数进缓冲。
// - 游标 `lastId` **只进不退**，且暂停时也推进（否则恢复后补发一屏重复行）。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { exportLogs, fetchLogs } from "@/api/endpoints/logs";
import { ApiError } from "@/api/http";
import { useChannelStream } from "@/composables/useTaskStream";
import { saveTextFile } from "@/utils/download";
import {
  ALERT_CODES,
  DEFAULT_MIN_LEVEL,
  SEVERITY_ORDER,
  type Envelope,
  type LogSnapshot,
  type SystemLogRow,
} from "@/ws/events";

/** 前端环形缓冲上限（服务端每连接只有 200，这里留足滚动回溯）。 */
export const LOG_BUFFER_CAPACITY = 2000;

/** 单次补洞向服务端要多少行。 */
export const GAP_BACKFILL_LIMIT = 500;

/** 补洞冷却：合并窗口每 100ms 就可能造成一次"洞"，不能每来一行就打一次 REST。 */
export const GAP_COOLDOWN_MS = 1000;

/** 「更早」一次翻多少行（REST 分页）。 */
export const EARLIER_PAGE_SIZE = 200;

/** 导出默认行数（与服务端 `DEFAULT_EXPORT` 对齐）。 */
export const EXPORT_LIMIT = 20_000;

/** 级别下限比较（与 `system_logs.level` 的 CHECK 同源）。 */
export function passesLevel(level: string, minimum: string): boolean {
  const value = SEVERITY_ORDER[level] ?? SEVERITY_ORDER["info"];
  const floor = SEVERITY_ORDER[minimum] ?? SEVERITY_ORDER["info"];
  return value >= floor;
}

/** 这一行的 `payload.code` 是否是 §04.5.2 的 8 个告警码之一（与 Hub 的分流条件同源）。 */
export function isAlertPayload(payload: Record<string, unknown>): boolean {
  const code = payload["code"];
  return typeof code === "string" && (ALERT_CODES as readonly string[]).includes(code);
}

/** 搜索匹配：`message` 与 `source` 都搜（与后端 `_filtered` 的语义一致）。 */
export function matchesSearch(row: SystemLogRow, needle: string): boolean {
  const trimmed = needle.trim().toLowerCase();
  if (!trimmed) return true;
  return (
    row.message.toLowerCase().includes(trimmed) || row.source.toLowerCase().includes(trimmed)
  );
}

/**
 * 下一个要补的洞（`(from, to)` 开区间）—— 纯函数，单测直接打它。
 *
 * `pending` 是"收到了、但它前面还缺东西"的 id 集合；`null` ⇒ 连续，不需要补。
 */
export function nextGap(lastId: number, pending: ReadonlySet<number>): { from: number; to: number } | null {
  if (pending.size === 0) return null;
  let smallest = Number.POSITIVE_INFINITY;
  for (const id of pending) smallest = Math.min(smallest, id);
  if (!Number.isFinite(smallest) || smallest <= lastId + 1) return null;
  return { from: lastId, to: smallest };
}

function asText(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asPayload(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function asNullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** `log.appended` 事件载荷 → 缓冲行（事件用的是 `log_id`，且没有 trace/seq_in_task）。 */
export function toLogRow(data: Record<string, unknown>): SystemLogRow | null {
  const id = asNullableNumber(data["log_id"]);
  if (id === null) return null;
  return {
    id,
    ts: asText(data["ts"], ""),
    level: asText(data["level"], "info"),
    source: asText(data["source"], "-"),
    message: asText(data["message"], ""),
    task_id: asNullableText(data["task_id"]),
    job_id: asNullableText(data["job_id"]),
    stage: asNullableText(data["stage"]),
    unit_ref: asNullableText(data["unit_ref"]),
    worker_id: asNullableText(data["worker_id"]),
    trace_id: null,
    duration_ms: asNullableNumber(data["duration_ms"]),
    seq_in_task: null,
    payload: asPayload(data["payload"]),
  };
}

/**
 * `system.alert` 事件载荷 → 缓冲行。
 *
 * 告警帧把 `code` / `hint` 放在**顶层**（不像日志行那样塞在 `payload` 里），
 * 这里把它们折回 `payload`，好让下游只认一种形状。
 */
export function toAlertRow(data: Record<string, unknown>): SystemLogRow | null {
  const id = asNullableNumber(data["log_id"]);
  if (id === null) return null;
  const payload: Record<string, unknown> = {};
  if (typeof data["code"] === "string") payload["code"] = data["code"];
  if (typeof data["hint"] === "string") payload["hint"] = data["hint"];
  return {
    id,
    ts: asText(data["ts"], ""),
    level: asText(data["severity"], "warn"),
    source: asText(data["source"], "system.alert"),
    message: asText(data["message"], ""),
    task_id: asNullableText(data["task_id"]),
    job_id: asNullableText(data["job_id"]),
    stage: null,
    unit_ref: asNullableText(data["code"]),
    worker_id: null,
    trace_id: null,
    duration_ms: null,
    seq_in_task: null,
    payload,
  };
}

/** 快照 / REST 分页里的一行（可选字段补齐成同一形状）。 */
export function toSnapshotRow(raw: unknown): SystemLogRow | null {
  if (typeof raw !== "object" || raw === null) return null;
  const row = raw as Record<string, unknown>;
  const id = asNullableNumber(row["id"]);
  if (id === null) return null;
  return {
    id,
    ts: asText(row["ts"], ""),
    level: asText(row["level"], "info"),
    source: asText(row["source"], "-"),
    message: asText(row["message"], ""),
    task_id: asNullableText(row["task_id"]),
    job_id: asNullableText(row["job_id"]),
    stage: asNullableText(row["stage"]),
    unit_ref: asNullableText(row["unit_ref"]),
    worker_id: asNullableText(row["worker_id"]),
    trace_id: asNullableText(row["trace_id"]),
    duration_ms: asNullableNumber(row["duration_ms"]),
    seq_in_task: asNullableNumber(row["seq_in_task"]),
    payload: asPayload(row["payload"]),
  };
}

export interface LogsStoreOptions {
  /** 注入取数（单测用；生产走真实 REST）。 */
  fetchPage?: typeof fetchLogs;
  fetchExport?: typeof exportLogs;
  /** 注入保存（单测用；生产触发浏览器下载）。 */
  save?: typeof saveTextFile;
  /** 注入时钟（补洞冷却用）。 */
  now?: () => number;
}

export const useLogsStore = defineStore("logs", () => {
  const rows = ref<SystemLogRow[]>([]);
  const lastId = ref(0);
  const alertIds = ref<Set<number>>(new Set());
  const truncated = ref(false);

  const minLevel = ref<string>(DEFAULT_MIN_LEVEL);
  const source = ref<string>("");
  const taskId = ref<string>("");
  const search = ref<string>("");
  const onlyAlerts = ref(false);

  const paused = ref(false);
  const droppedWhilePaused = ref(0);

  const gapCount = ref(0);
  const backfillCount = ref(0);
  const lostIds = ref(0);
  const backfillInFlight = ref(false);
  const loadingEarlier = ref(false);
  const atOldest = ref(false);
  const exportError = ref<string | null>(null);
  const exporting = ref(false);

  // 非响应式内部状态：`pending` 只参与"是否要补洞"的判定，不需要驱动渲染。
  const pending = new Set<number>();
  let lastBackfillAt = Number.NEGATIVE_INFINITY;
  let started = false;

  let options: LogsStoreOptions = {};
  let clock: () => number = () => Date.now();

  /** 单测入口：注入假件（生产不调用）。 */
  function configure(next: LogsStoreOptions): void {
    options = next;
    if (next.now) clock = next.now;
  }

  const visible = computed(() =>
    rows.value.filter((row) => {
      if (onlyAlerts.value && !alertIds.value.has(row.id)) return false;
      // 告警不受**级别**过滤（§04.4.6：告警永不被限流/丢弃）；其余过滤照常生效。
      if (!alertIds.value.has(row.id) && !passesLevel(row.level, minLevel.value)) return false;
      if (source.value && row.source !== source.value) return false;
      if (taskId.value && row.task_id !== taskId.value) return false;
      return matchesSearch(row, search.value);
    }),
  );

  const alertRows = computed(() => rows.value.filter((row) => alertIds.value.has(row.id)));
  const sources = computed(() => [...new Set(rows.value.map((row) => row.source))].sort());
  const errorCount = computed(() => rows.value.filter((row) => passesLevel(row.level, "error")).length);
  const oldestId = computed(() => (rows.value.length > 0 ? rows.value[0].id : null));

  // ── 缓冲 ────────────────────────────────────────────────────────

  function markAlert(id: number): void {
    const next = new Set(alertIds.value);
    next.add(id);
    alertIds.value = next;
  }

  /** 按 id 有序插入（绝大多数情况是"追加到尾部"，所以先走快路径）。 */
  function insertRow(row: SystemLogRow): void {
    const list = rows.value;
    const last = list[list.length - 1];
    if (!last || row.id > last.id) {
      list.push(row);
    } else {
      let low = 0;
      let high = list.length;
      while (low < high) {
        const mid = (low + high) >> 1;
        if (list[mid].id < row.id) low = mid + 1;
        else high = mid;
      }
      list.splice(low, 0, row);
    }
    if (list.length > LOG_BUFFER_CAPACITY) list.splice(0, list.length - LOG_BUFFER_CAPACITY);
    pruneAlerts();
  }

  /** 缓冲裁掉的行不该继续占着 `alertIds`（否则"只看告警"会显示早已被裁掉的行）。 */
  function pruneAlerts(): void {
    if (alertIds.value.size === 0) return;
    const oldest = rows.value[0]?.id ?? 0;
    let dropped = false;
    const next = new Set<number>();
    for (const id of alertIds.value) {
      if (id >= oldest) next.add(id);
      else dropped = true;
    }
    if (dropped) alertIds.value = next;
  }

  /** 推进连续游标：把"已经连上"的悬空 id 逐个吸收。 */
  function absorb(): void {
    while (pending.has(lastId.value + 1)) {
      pending.delete(lastId.value + 1);
      lastId.value += 1;
    }
  }

  /**
   * 记账一行（去重 + 推进游标）；返回是否**首次**收到。
   *
   * 去重只靠 `id <= lastId` 与 `pending`：这两个集合的语义是"已经处理过"，
   * 不需要再维护一份无界的 seen 集合。
   */
  function noteRow(row: SystemLogRow): boolean {
    if (row.id <= lastId.value) return false;
    if (pending.has(row.id)) return false;
    const hadGap = pending.size > 0;
    pending.add(row.id);
    absorb();
    // 只在"洞**新开**"时计数（0 → >0）。按行计数会把补洞回填的那几行也算成新洞，
    // 于是"缺口 N 次"在真正出问题时反而涨得最慢 —— 计数必须对应事件，不能对应行。
    if (!hadGap && pending.size > 0) gapCount.value += 1;
    return true;
  }

  /**
   * 收下一行：去重 → 入缓冲 → 标告警 → 视需要补洞。
   *
   * **暂停时仍然收告警**：暂停的语义是"别滚屏了"，不是"别让我知道出事了"。
   * 其余行只记账不显示（`droppedWhilePaused` 如实计数），游标照样推进。
   */
  function accept(row: SystemLogRow): boolean {
    if (!noteRow(row)) return false;
    const isAlert = isAlertPayload(row.payload);
    if (paused.value && !isAlert) {
      droppedWhilePaused.value += 1;
      void maybeBackfill();
      return true;
    }
    insertRow(row);
    if (isAlert) markAlert(row.id);
    void maybeBackfill();
    return true;
  }

  function append(incoming: readonly SystemLogRow[]): void {
    for (const row of incoming) accept(row);
  }

  // ── 补洞（REST 兜底）────────────────────────────────────────────

  /** 有洞就打一次 REST 把它补上（冷却 + 单飞，避免把服务端当轮询用）。 */
  async function maybeBackfill(): Promise<void> {
    if (backfillInFlight.value) return;
    const gap = nextGap(lastId.value, pending);
    if (!gap) return;
    if (clock() - lastBackfillAt < GAP_COOLDOWN_MS) return;
    lastBackfillAt = clock();
    backfillInFlight.value = true;
    try {
      const page = await (options.fetchPage ?? fetchLogs)({
        since_id: gap.from,
        limit: GAP_BACKFILL_LIMIT,
      });
      backfillCount.value += 1;
      let accepted = 0;
      for (const raw of page.logs) {
        const row = toSnapshotRow(raw);
        if (row && noteRow(row)) {
          insertRow(row);
          if (isAlertPayload(row.payload)) markAlert(row.id);
          accepted += 1;
        }
      }
      if (accepted === 0) giveUpOnGap();
    } catch {
      // 补洞失败**不**影响实时流：下次收到新行时冷却已过，会再试一次。
    } finally {
      backfillInFlight.value = false;
    }
  }

  /**
   * 补不回来（例如日志已被 GC 回收）⇒ 如实跳过并记账。
   *
   * 不跳过的后果是游标永远卡在洞前面：此后每来一行都触发一次注定失败的补洞，
   * 面板看起来"一直在补"却什么都不动。
   */
  function giveUpOnGap(): void {
    const gap = nextGap(lastId.value, pending);
    if (!gap) return;
    lostIds.value += gap.to - gap.from - 1;
    lastId.value = gap.to - 1;
    absorb();
  }

  // ── 入口 ────────────────────────────────────────────────────────

  /**
   * 快照 = **替换**（服务端已按订阅的 `min_level` 过滤过，前端只做展示层二次过滤）。
   *
   * 暂停中也会照常替换：快照只在握手 / `resync` 时来，那意味着连接刚重建过，
   * 用户的滚动位置本来就已经失效了。
   */
  function applySnapshot(payload: LogSnapshot): void {
    const snapshot = [...payload.logs].sort((left, right) => left.id - right.id).slice(-LOG_BUFFER_CAPACITY);
    rows.value = snapshot;
    truncated.value = Boolean(payload.truncated);
    pending.clear();
    alertIds.value = new Set(snapshot.filter((row) => isAlertPayload(row.payload)).map((row) => row.id));
    lastId.value = snapshot.length > 0 ? snapshot[snapshot.length - 1].id : lastId.value;
    droppedWhilePaused.value = 0;
    atOldest.value = false;
  }

  function ingest(envelope: Envelope): void {
    if (envelope.type === "snapshot") {
      const data = envelope.data as unknown as Partial<LogSnapshot>;
      if (!Array.isArray(data.logs)) return;
      applySnapshot({
        logs: data.logs
          .map((raw) => toSnapshotRow(raw))
          .filter((row): row is SystemLogRow => row !== null),
        truncated: Boolean(data.truncated),
      });
      return;
    }
    if (envelope.data["kind"] !== "log.appended") return;
    const row = toLogRow(envelope.data);
    if (row) accept(row);
  }

  /** `system` 通道的告警帧（告警不走 `logs` 通道，必须单独接）。 */
  function ingestAlert(envelope: Envelope): void {
    if (envelope.data["kind"] !== "system.alert") return;
    const row = toAlertRow(envelope.data);
    if (row) accept(row);
  }

  /** 幂等接线；由 `App.vue` 调用，保证缓冲与当前面板无关地一直在攒。 */
  function start(): void {
    if (started) return;
    started = true;
    useChannelStream("logs", ingest);
    useChannelStream("system", ingestAlert);
  }

  /** 「更早」：往前翻一页（REST 分页；WS 只补最近的 2000 条）。 */
  async function loadEarlier(): Promise<number> {
    const before = oldestId.value;
    if (before === null || loadingEarlier.value || atOldest.value) return 0;
    loadingEarlier.value = true;
    try {
      const page = await (options.fetchPage ?? fetchLogs)({
        until_id: before - 1,
        limit: EARLIER_PAGE_SIZE,
      });
      const fetched = page.logs
        .map((raw) => toSnapshotRow(raw))
        .filter((row): row is SystemLogRow => row !== null && row.id < before);
      // 判"到底"要看**服务端返回了多少**，不是去重之后剩多少 —— 否则补洞回填过的那些行
      // 会让"这一页变短"看起来像翻到了头。
      if (fetched.length < EARLIER_PAGE_SIZE) atOldest.value = true;
      // 去重：补洞（REST 回填）与翻历史走的是同一个端点，重叠是常态而不是异常。
      const known = new Set(rows.value.map((row) => row.id));
      const older = fetched.filter((row) => !known.has(row.id));
      if (older.length === 0) return 0;
      const merged = [...older, ...rows.value];
      merged.sort((left, right) => left.id - right.id);
      // 翻历史时从**尾部**裁剪：实时流会把最新几行立刻补回来，而历史只能靠手动翻。
      rows.value = merged.slice(0, LOG_BUFFER_CAPACITY);
      for (const row of older) {
        if (isAlertPayload(row.payload)) markAlert(row.id);
      }
      pruneAlerts();
      return older.length;
    } finally {
      loadingEarlier.value = false;
    }
  }

  /** 导出当前过滤条件下的 NDJSON（服务端流式；行数 == limit ⇒ 可能被截断）。 */
  async function exportToFile(): Promise<{ filename: string; rows: number } | null> {
    exporting.value = true;
    exportError.value = null;
    try {
      const result = await (options.fetchExport ?? exportLogs)({
        level: minLevel.value,
        task_id: taskId.value || null,
        source: source.value || null,
        search: search.value || null,
        limit: EXPORT_LIMIT,
      });
      (options.save ?? saveTextFile)(result.text, result.filename);
      return { filename: result.filename, rows: result.rows };
    } catch (reason) {
      exportError.value =
        reason instanceof ApiError ? `${reason.message}（status=${reason.status}）` : String(reason);
      return null;
    } finally {
      exporting.value = false;
    }
  }

  function setMinLevel(level: string): void {
    minLevel.value = level;
  }

  function setSource(next: string): void {
    source.value = next;
  }

  function setTaskId(next: string): void {
    taskId.value = next;
  }

  function setSearch(next: string): void {
    search.value = next;
  }

  function toggleOnlyAlerts(): void {
    onlyAlerts.value = !onlyAlerts.value;
  }

  function togglePause(): void {
    paused.value = !paused.value;
    if (!paused.value) droppedWhilePaused.value = 0;
  }

  function clear(): void {
    rows.value = [];
    alertIds.value = new Set();
    droppedWhilePaused.value = 0;
  }

  return {
    rows,
    visible,
    alertRows,
    alertIds,
    sources,
    lastId,
    oldestId,
    truncated,
    minLevel,
    source,
    taskId,
    search,
    onlyAlerts,
    paused,
    droppedWhilePaused,
    gapCount,
    backfillCount,
    lostIds,
    backfillInFlight,
    loadingEarlier,
    atOldest,
    exporting,
    exportError,
    errorCount,
    configure,
    append,
    applySnapshot,
    ingest,
    ingestAlert,
    maybeBackfill,
    start,
    loadEarlier,
    exportToFile,
    setMinLevel,
    setSource,
    setTaskId,
    setSearch,
    toggleOnlyAlerts,
    togglePause,
    clear,
  };
});
