<script setup lang="ts">
// ① 总览台（T4.1 建外壳 · T4.2 接上四池 / 今日产量 / 资源 / 启动 / 暂停 / 全自动）。
//
// 这一屏只回答六个问题，多一个都不放：
// ① 四个池现在什么情况？—— 池卡片（含 worker 心跳，`stale` 显式标"疑似猝死"）；
// ② 今天出了多少片？—— 今日产量（**本地日**口径，面板上把窗口也写出来）；
// ③ 机器还撑得住吗？—— 资源条 + 磁盘告警条；
// ④ 我能按哪些按钮？—— 启动 / 暂停 / 一键全自动（后两者见各自的二次确认）；
// ⑤ 后端还在不在？—— 服务健康 + WS 连接态（T4.1 保留，压成一条）；
// ⑥ **这台机器还在自己照顾自己吗**（T4.11）—— 自愈次数 / 停手进程 / 门禁水位 /
//    **人工池**。人工池既在顶部有一条告警，也在卡片里逐条列出。
//
// 为什么"一键全自动"要二次确认，而"暂停"不要
// ------------------------------------------
// 暂停是**可逆且局部**的（改一行 DB，在途跑完，点一下就回来）；而"一键全自动"
// 绕过的是全流程**唯一**的人工节点（§04.4.4）：开了之后 A/B 级稿子直接进配音，
// 人不再有机会看。代价不对称 ⇒ 确认的强度也不该对称。
//
// 为什么磁盘告警条要在最上面
// --------------------------
// `free_D < 15GB` 的时候，render 池每跑一片都是在把 C/D 盘往死里逼。这条必须
// 在"启动"按钮**上方**、第一眼就能看到，而不是埋在资源面板的第四行。
//
// 为什么"人工池"也有一条告警
// --------------------------
// 掉进人工池的任务**不会**自己重试 —— 它是流程的终点，只有人能动它（P4：
// 不许静默失败）。所以它必须和磁盘告警一样顶到最上面，而不是只在守护卡片里
// 留一行数字：那行数字会被"今天产量 3 片"这种正面信息盖过去。

import { computed, ref } from "vue";

import type { PoolStatus, WorkerStatus } from "@/api/endpoints/overview";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { StatusTone } from "@/components/tone";
import { HEALTH_POLL_MS, asPoolName, poolLabel, policyLabel, useOverviewStore } from "@/stores/overview";
import { useLogsStore } from "@/stores/logs";

const overview = useOverviewStore();
const logs = useLogsStore();

const recent = computed(() => logs.rows.slice(-6).reverse());
const pollSeconds = HEALTH_POLL_MS / 1000;
const settings = computed(() => overview.settings);
const today = computed(() => overview.today);
const resources = computed(() => overview.resources);
const watchdog = computed(() => overview.watchdog);
const manualPool = computed(() => overview.manualPool);
const watchdogHalted = computed(() => overview.watchdogHalted);

/** 守护那一句状态（**停手最优先**：它表示守护已经放弃这个进程）。 */
const watchdogState = computed<string>(() => {
  const item = watchdog.value;
  if (item === null) return "这套依赖没接守护";
  if (watchdogHalted.value.length > 0) return `已停手 ${watchdogHalted.value.length} 个`;
  if (manualPool.value.length > 0) return `有 ${manualPool.value.length} 条在人工池`;
  if (!item.enabled) return "守护停用（配置读不到）";
  if (!item.runnable) return "守护待命（workers/ 不在）";
  return "守护中";
});

/** 门禁那一句（`low === null` ⇒ **还没采过样**，不是"水位正常"）。 */
function gateLabel(gate: { low?: boolean | null; enabled: boolean } | undefined): string {
  if (gate === undefined) return "-";
  if (!gate.enabled) return "关闭（只告警不暂停）";
  if (gate.low === null || gate.low === undefined) return "还没采样";
  return gate.low ? "水位低（已暂停认领）" : "水位正常";
}

/** 二次确认（只有"一键全自动"走这条路）。 */
const confirmingAuto = ref(false);

const statusBreakdown = computed(() => breakdown(today.value?.by_status));
const gradeBreakdown = computed(() => breakdown(today.value?.by_grade));

function breakdown(counts: Record<string, number> | undefined): string {
  const entries = Object.entries(counts ?? {});
  if (entries.length === 0) return "-";
  return entries.map(([key, value]) => `${key} ${value}`).join(" · ");
}

/** 池卡片的状态灯（`error` 优先：参数缺失时其余数字都不可信）。 */
function poolTone(card: PoolStatus): StatusTone {
  if (card.error) return "error";
  if (card.paused) return "warn";
  if (card.running > 0) return "busy";
  return card.alive_workers > 0 ? "ok" : "idle";
}

function poolState(card: PoolStatus): string {
  if (card.error) return "参数缺失";
  if (card.paused) return card.paused_by ? `已暂停（${card.paused_by}）` : "已暂停";
  if (card.running > 0) return "在跑";
  // "有活没人认领"是**要人管**的一件事，不能和"空闲待命"画成同一个色。
  if (card.pending > 0 && card.alive_workers === 0) return "没人认领";
  return card.alive_workers > 0 ? "待命" : "空闲";
}

/** worker 心跳灯：`dead` 是**已确认**的事实，"超时但没标死"才是疑似。 */
function beatTone(beat: WorkerStatus): StatusTone {
  if (beat.status === "dead") return "error";
  if (beat.stale) return "warn";
  return beat.status === "busy" ? "busy" : "ok";
}

function beatLabel(beat: WorkerStatus): string {
  if (beat.status === "dead") return "已标死";
  if (beat.stale) return "疑似猝死";
  return beat.status === "busy" ? "在跑" : "空闲";
}

function mb(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${value} MB`;
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${value}%`;
}

async function onStart(): Promise<void> {
  await overview.startServices();
}

async function onAutoToggle(): Promise<void> {
  if (!confirmingAuto.value) {
    confirmingAuto.value = true;
    return;
  }
  confirmingAuto.value = false;
  await overview.setAutoPolicy("grade_ab");
}
</script>

<template>
  <div class="overview">
    <p v-if="overview.diskLow" class="alert alert--error">
      磁盘水位低于门禁：{{ resources?.disk_drive }} 可用 {{ resources?.disk_free_gb }} GB &lt;
      {{ resources?.disk_free_d_min_gb }} GB。render / publish 池应暂停认领（`pause_pools_on_low` =
      {{ settings?.pause_pools_on_low }}）。
    </p>

    <p v-if="manualPool.length > 0" class="alert alert--error">
      人工池：{{ manualPool.length }} 个任务连续失败已转人工处置（阈值
      {{ watchdog?.manual_pool_after ?? "-" }} 次）—— 它们**不会**自己重试，详见下面的「无人值守」卡片。
    </p>

    <p v-if="overview.loadError" class="alert alert--error">
      总览拉取失败：{{ overview.loadError }}（下面显示的是**上一次**拿到的数字）
    </p>

    <PanelCard title="四池调度" :subtitle="`worker ${overview.workerAlive}/${overview.workerTotal} 心跳 · 暂停只改一行 DB，在途跑完`">
      <template #actions>
        <AppButton size="sm" :loading="overview.loading" @click="overview.refresh()">刷新</AppButton>
      </template>
      <div v-if="overview.pools.length > 0" class="pools">
        <article v-for="card in overview.pools" :key="card.pool" class="pool">
          <header class="pool__head">
            <span class="pool__name">{{ poolLabel(card.pool) }}</span>
            <StatusDot :tone="poolTone(card)" :label="poolState(card)" />
          </header>
          <p v-if="card.error" class="pool__error">{{ card.error }}</p>
          <dl class="kv kv--tight">
            <dt>待认领</dt>
            <dd class="mono">{{ card.pending }}</dd>
            <dt>依赖未满足</dt>
            <dd class="mono">{{ card.blocked }}</dd>
            <dt>在跑 / 并发</dt>
            <dd class="mono">{{ card.running }} / {{ card.concurrency }}</dd>
            <dt>失败 / 死信</dt>
            <dd class="mono">{{ card.failed }} / {{ card.dead }}</dd>
            <dt>积压</dt>
            <dd class="mono">{{ card.backlog }}</dd>
          </dl>
          <ul v-if="card.workers.length > 0" class="beats">
            <li v-for="beat in card.workers" :key="beat.worker_id" class="beats__item">
              <StatusDot :tone="beatTone(beat)" :label="beatLabel(beat)" />
              <span class="mono beats__id">{{ beat.worker_id }}</span>
              <span class="mono beats__silent">{{ beat.silent_sec }}s</span>
            </li>
          </ul>
          <p v-else class="muted">没有 worker 心跳</p>
          <AppButton
            size="sm"
            :variant="card.paused ? 'ghost' : 'danger'"
            :disabled="overview.busy"
            @click="overview.pausePool(asPoolName(card.pool), !card.paused)"
          >
            {{ card.paused ? "恢复认领" : "暂停认领" }}
          </AppButton>
        </article>
      </div>
      <EmptyState v-else title="还没有池状态" hint="点「刷新」或等 WS 的 `pool.stats` 推过来。" />
    </PanelCard>

    <PanelCard
      title="无人值守"
      :subtitle="`守护 ${watchdog ? `${watchdog.tick_sec}s 一拍` : '未接线'} · 自愈与停手都留痕`"
    >
      <template #actions>
        <AppButton size="sm" :disabled="overview.busy" @click="overview.runWatchdogTick()">
          立即自检
        </AppButton>
      </template>
      <template v-if="watchdog">
        <dl class="kv">
          <dt>状态</dt>
          <dd>
            <StatusDot :tone="overview.watchdogTone" :label="watchdogState" />
          </dd>
          <dt>最近一轮</dt>
          <dd class="mono">{{ watchdog.last_tick_at ?? "还没跑过" }}</dd>
          <dt>自愈次数</dt>
          <dd class="mono">{{ watchdog.restarted_total }}</dd>
          <dt>停手进程</dt>
          <dd class="mono">
            {{ watchdogHalted.length > 0 ? watchdogHalted.join(" / ") : "无（没有进程被放弃）" }}
          </dd>
          <dt>磁盘门禁</dt>
          <dd>
            <StatusDot
              :tone="watchdog.disk_gate.low === true ? 'error' : watchdog.disk_gate.low === null ? 'idle' : 'ok'"
              :label="gateLabel(watchdog.disk_gate)"
            />
          </dd>
          <dt>门禁覆盖</dt>
          <dd class="mono">
            {{ watchdog.disk_gate.overridden.length > 0 ? watchdog.disk_gate.overridden.join(" / ") : "无" }}
          </dd>
          <dt>人工池</dt>
          <dd class="mono">{{ manualPool.length }} 条（阈值 {{ watchdog.manual_pool_after }} 次）</dd>
        </dl>
        <ul class="services">
          <li v-for="guard in watchdog.services" :key="guard.name" class="services__item">
            <StatusDot
              :tone="guard.halted ? 'error' : guard.running ? 'ok' : guard.guarded ? 'warn' : 'idle'"
              :label="guard.halted ? '已停手' : guard.running ? '在跑' : guard.guarded ? '不在' : '不守'"
            />
            <span class="mono">{{ guard.name }}</span>
            <span class="services__detail">
              重启 {{ guard.restarts }} 次{{ guard.detail ? ` · ${guard.detail}` : "" }}
            </span>
          </li>
        </ul>
        <ul v-if="manualPool.length > 0" class="manual">
          <li v-for="item in manualPool" :key="item.task_id" class="manual__item">
            <span class="mono manual__id">{{ item.task_id }}</span>
            <span class="manual__title">{{ item.title }}</span>
            <span class="mono manual__meta">
              失败 {{ item.attempt_count }} 次 · {{ item.retry_from ?? "无断点" }} ·
              {{ item.error_code ?? "-" }}
            </span>
          </li>
        </ul>
        <p v-if="watchdog.detail" class="muted">{{ watchdog.detail }}</p>
      </template>
      <EmptyState
        v-else
        title="这套依赖没接守护"
        hint="守护随 API 进程启动；响应里没有这一段说明它没接线（极简家目录 / 单测）。"
      />
    </PanelCard>

    <PanelCard title="今日产量" subtitle="**本地日**口径（UTC+8），不是 UTC 日">
      <dl class="kv">
        <dt>本地日</dt>
        <dd class="mono">{{ today?.day ?? "-" }}</dd>
        <dt>窗口（UTC）</dt>
        <dd class="mono">{{ today?.window_start ?? "-" }} → {{ today?.window_end ?? "-" }}</dd>
        <dt>新建</dt>
        <dd class="mono">{{ today?.created ?? 0 }}</dd>
        <dt>已完成</dt>
        <dd class="mono">{{ today?.completed ?? 0 }}</dd>
        <dt>已发布</dt>
        <dd class="mono">{{ today?.published ?? 0 }}</dd>
        <dt>失败</dt>
        <dd class="mono">{{ today?.failed ?? 0 }}</dd>
        <dt>按状态</dt>
        <dd class="mono">{{ statusBreakdown }}</dd>
        <dt>按等级</dt>
        <dd class="mono">{{ gradeBreakdown }}</dd>
      </dl>
    </PanelCard>

    <PanelCard title="资源占用" subtitle="采样 0.2 Hz · 与 `studio doctor` 同口径">
      <dl class="kv">
        <dt>CPU</dt>
        <dd class="mono">{{ pct(resources?.cpu_pct) }}</dd>
        <dt>内存</dt>
        <dd class="mono">
          {{ mb(resources?.ram_mb) }} / {{ mb(resources?.ram_total_mb) }}（{{ pct(resources?.ram_pct) }}）
        </dd>
        <dt>显存</dt>
        <dd class="mono">
          {{ pct(resources?.gpu_util) }} · {{ mb(resources?.gpu_mem_mb) }} /
          {{ mb(resources?.gpu_mem_total_mb) }}
        </dd>
        <dt>GPU</dt>
        <dd class="mono">{{ resources?.gpu_name ?? "不可用（CPU 兜底）" }}</dd>
        <dt>磁盘</dt>
        <dd>
          <StatusDot :tone="overview.diskTone" :label="`${resources?.disk_free_gb ?? '-'} GB 可用`" />
        </dd>
        <dt>C 盘可用</dt>
        <dd class="mono">{{ resources?.disk_free_c_gb ?? "-" }} GB</dd>
        <dt>进程 RSS</dt>
        <dd class="mono">{{ mb(resources?.process_rss_mb) }}</dd>
        <dt>采样时刻</dt>
        <dd class="mono">
          {{ resources?.sampled_at ?? "-" }}
          <template v-if="overview.resourcesAgeSec !== null">（{{ overview.resourcesAgeSec }} 秒前）</template>
        </dd>
      </dl>
      <p v-if="!resources" class="muted">还没采过样（API 起来后 5s 内会有一拍）。</p>
    </PanelCard>

    <PanelCard title="六进程" :subtitle="`api / tts / draft / voice / render / publish · 启动要过 doctor 门禁`">
      <template #actions>
        <AppButton size="sm" variant="primary" :loading="overview.busy" @click="onStart()">启动</AppButton>
      </template>
      <ul v-if="overview.services.length > 0" class="services">
        <li v-for="service in overview.services" :key="service.name" class="services__item">
          <StatusDot :tone="service.ready ? 'ok' : 'warn'" :label="service.name" />
          <span class="mono services__readiness">{{ service.readiness }}</span>
          <span class="muted services__detail">{{ service.detail }}</span>
        </li>
      </ul>
      <EmptyState v-else title="没有进程结论" hint="就绪判据见 §04.8.1。" />
      <p class="muted">停止不走这里：在 API 进程里停自己等于自杀，正门是 `停止.bat`。</p>
    </PanelCard>

    <PanelCard title="运行策略" subtitle="`config/app.yaml → approval.auto_approve_policy`">
      <dl class="kv">
        <dt>当前</dt>
        <dd>
          <StatusDot
            :tone="overview.autoApproveOn ? 'warn' : 'ok'"
            :label="policyLabel(overview.autoPolicy)"
          />
        </dd>
        <dt>磁盘门禁</dt>
        <dd class="mono">C ≥ {{ settings?.free_c_min_gb ?? "-" }} GB · D ≥ {{ settings?.free_d_min_gb ?? "-" }} GB</dd>
      </dl>

      <div v-if="confirmingAuto" class="confirm">
        <p class="confirm__text">
          这会绕过全流程**唯一**的人工确认节点：A/B 级稿子直接进配音，你不再有机会看。
          确认开启？
        </p>
        <div class="confirm__actions">
          <AppButton size="sm" variant="danger" :loading="overview.busy" @click="onAutoToggle()">
            确认开启
          </AppButton>
          <AppButton size="sm" @click="confirmingAuto = false">取消</AppButton>
        </div>
      </div>
      <div v-else class="confirm__actions">
        <AppButton
          v-if="!overview.autoApproveOn"
          size="sm"
          variant="primary"
          :disabled="overview.busy"
          @click="onAutoToggle()"
        >
          一键全自动
        </AppButton>
        <AppButton
          v-if="overview.autoApproveOn"
          size="sm"
          :disabled="overview.busy"
          @click="overview.setAutoPolicy('grade_a')"
        >
          回退到 A 级自动
        </AppButton>
        <AppButton
          v-if="overview.autoPolicy !== 'off'"
          size="sm"
          :disabled="overview.busy"
          @click="overview.setAutoPolicy('off')"
        >
          全人工
        </AppButton>
      </div>

      <p v-if="overview.notice" class="notice">{{ overview.notice }}</p>
      <p v-if="overview.error" class="alert alert--error">{{ overview.error }}</p>
    </PanelCard>

    <PanelCard title="服务健康 / 实时通道" subtitle="GET /api/v1/health 轮询 + WS /ws/ui">
      <dl class="kv">
        <dt>ok</dt>
        <dd class="mono">{{ overview.health?.ok ?? "-" }}</dd>
        <dt>spec_version</dt>
        <dd class="mono">{{ overview.health?.spec_version ?? "-" }}</dd>
        <dt>latest_log_id</dt>
        <dd class="mono">{{ overview.health?.latest_log_id ?? "-" }}</dd>
        <dt>轮询</dt>
        <dd class="mono">{{ pollSeconds }}s</dd>
        <dt>WS</dt>
        <dd>
          <StatusDot
            :tone="overview.wsTone"
            :label="overview.wsStatus"
            :pulse="overview.wsStatus === 'open'"
          />
        </dd>
        <dt>seq / since_id</dt>
        <dd class="mono">{{ overview.wsCursor.seq }} / {{ overview.wsCursor.sinceId ?? "-" }}</dd>
        <dt>连接数</dt>
        <dd class="mono">{{ overview.health?.ws.connections ?? "-" }}</dd>
      </dl>
      <template #actions>
        <AppButton size="sm" @click="overview.resync()">请求补齐</AppButton>
      </template>
      <p v-if="overview.healthError" class="alert alert--error">{{ overview.healthError }}</p>
    </PanelCard>

    <PanelCard title="最新日志" :subtitle="`缓冲 ${logs.rows.length} 行 · 其中 error 及以上 ${logs.errorCount} 行`">
      <ul v-if="recent.length > 0" class="recent">
        <li v-for="row in recent" :key="row.id" class="recent__item">
          <StatusDot :tone="row.level === 'info' ? 'idle' : 'warn'" />
          <span class="recent__source mono">{{ row.source }}</span>
          <span class="recent__message">{{ row.message }}</span>
        </li>
      </ul>
      <EmptyState v-else title="还没有日志" hint="日志会随 WS `logs` 通道实时到达。" />
    </PanelCard>
  </div>
</template>

<style scoped>
.overview {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--space-4);
  align-content: start;
}

.kv {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: var(--space-1) var(--space-3);
  margin: 0;
}

.kv--tight {
  grid-template-columns: 92px 1fr;
}

.kv dt {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.kv dd {
  margin: 0;
}

.pools {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-3);
}

.pool {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.pool__head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  justify-content: space-between;
}

.pool__name {
  font-weight: 600;
}

.pool__error {
  color: var(--error);
  font-size: var(--text-xs);
}

.beats {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.beats__item {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: var(--space-2);
  align-items: baseline;
  font-size: var(--text-xs);
}

.beats__id {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.beats__silent {
  color: var(--text-muted);
}

.services {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.services__item {
  display: grid;
  grid-template-columns: 110px 84px 1fr;
  gap: var(--space-2);
  align-items: baseline;
  font-size: var(--text-sm);
}

.services__readiness,
.services__detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.manual {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: var(--space-3) 0 0;
  padding: 0;
  list-style: none;
}

.manual__item {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: var(--space-2);
  padding: var(--space-2);
  font-size: var(--text-sm);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.manual__id,
.manual__meta {
  color: var(--text-muted);
}

.manual__meta {
  grid-column: 2;
  font-size: var(--text-xs);
}

.confirm {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.confirm__text {
  font-size: var(--text-sm);
}

.confirm__actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.alert {
  grid-column: 1 / -1;
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  background: rgba(242, 99, 122, 0.08);
  border: 1px solid var(--error);
}

.notice {
  margin-top: var(--space-3);
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.muted {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.recent {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.recent__item {
  display: grid;
  grid-template-columns: auto 92px 1fr;
  gap: var(--space-2);
  align-items: baseline;
  font-size: var(--text-sm);
}

.recent__source {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent__message {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
