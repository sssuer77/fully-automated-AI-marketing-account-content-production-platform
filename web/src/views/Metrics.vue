<script setup lang="ts">
// 观测面板（T4.12 · §04.5.11）。
//
// 这一屏只回答一个问题：**出事了还有得救吗**。
// 总览台看"现在怎么样"，这里看"兜底的还在不在"：
// ① 备份新不新鲜（不是"有没有备份"）；
// ② 删掉的旧行有没有把盘还回来（不是"我清过垃圾"）；
// ③ 顺手把总览台那四块（资源 / 队列 / 产量 / 进程）摆在一起，省一次来回。
//
// 为什么刷新是**按钮**而不是自动跳
// --------------------------------
// 存储体检要递归数文件，备份新鲜度要 stat 一整个目录。让它们每 10s 跑一遍，是拿磁盘
// 换一个"看起来在实时监控"的假象 —— 而这一屏本来就只在"要不要演练 / 要不要 VACUUM"
// 这两个时刻被打开。
//
// 为什么**没有** GC / 备份 / VACUUM 的按钮
// ---------------------------------------
// 那些都在 CLI 与计划任务里（`studio gc run` / `studio db backup` / `studio db vacuum`）。
// 面板上给一个"删数据"的按钮，就是给手滑多开一条路。

import { onMounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import { cacheShare, formatBytes, freelistShare, useMetricsStore } from "@/stores/metrics";

const metrics = useMetricsStore();

onMounted(() => {
  metrics.start();
});

function pct(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}

/** 最老待跑那一格（`undefined` = 后端没给这一列，`null` = 队列是空的）。 */
function ageSec(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${Math.round(value)}s`;
}
</script>

<template>
  <div class="metrics">
    <p v-if="metrics.loadError" class="alert alert--error">
      观测数据拉取失败：{{ metrics.loadError }}（下面显示的是上一次拿到的数字）
    </p>

    <PanelCard title="备份新鲜度" :subtitle="metrics.backupHint">
      <template #actions>
        <AppButton size="sm" :loading="metrics.loading" @click="metrics.refresh()">刷新</AppButton>
      </template>

      <div class="line">
        <StatusDot
          :tone="metrics.backupTone"
          :label="
            metrics.backups === null
              ? '还没读过'
              : metrics.backups.count === 0
                ? '一份都没有'
                : metrics.backups.stale
                  ? '该查计划任务了'
                  : '新鲜'
          "
        />
      </div>

      <dl class="kv">
        <dt>最新一份</dt>
        <dd class="mono">{{ metrics.backups?.newest ?? '-' }}</dd>
        <dt>距今</dt>
        <dd class="mono">{{ metrics.backupAgeText }}</dd>
        <dt>份数 / 占用</dt>
        <dd class="mono">
          {{ metrics.backups?.count ?? 0 }} 份 ·
          {{ metrics.backups ? formatBytes(metrics.backups.total_bytes) : '-' }}
        </dd>
        <dt>目录</dt>
        <dd class="mono path">{{ metrics.backups?.dir ?? '-' }}</dd>
      </dl>

      <p class="hint">
        报的是"最新一份距今几小时"，不是"有没有备份" —— 备份任务悄悄坏掉一周，磁盘上
        照样有文件。超过 48 小时没新的，先查计划任务，别急着演练。
      </p>
    </PanelCard>

    <PanelCard title="存储体检" :subtitle="metrics.storageHint">
      <div class="line">
        <StatusDot :tone="metrics.storageTone" label="数据库与三个会自己长大的目录" />
      </div>

      <dl class="kv">
        <dt>库文件</dt>
        <dd class="mono">{{ metrics.storage ? formatBytes(metrics.storage.db_bytes) : '-' }}</dd>
        <dt>其中空洞</dt>
        <dd class="mono">
          {{ metrics.storage ? formatBytes(metrics.storage.db_freelist_bytes) : '-' }}
          <span v-if="metrics.storage" class="muted">
            （占 {{ pct(freelistShare(metrics.storage)) }}）
          </span>
        </dd>
        <dt>TTS 缓存</dt>
        <dd class="mono">
          {{ metrics.storage ? formatBytes(metrics.storage.tts_cache_bytes) : '-' }} /
          {{ metrics.storage ? formatBytes(metrics.storage.tts_cache_limit_bytes) : '-' }}
          <span v-if="metrics.storage" class="muted">
            （{{ pct(cacheShare(metrics.storage)) }}）
          </span>
        </dd>
        <dt>热点归档</dt>
        <dd class="mono">
          {{ metrics.storage ? formatBytes(metrics.storage.hot_archive_bytes) : '-' }}
        </dd>
        <dt>临时目录</dt>
        <dd class="mono">{{ metrics.storage ? formatBytes(metrics.storage.tmp_bytes) : '-' }}</dd>
        <dt>合计</dt>
        <dd class="mono">{{ formatBytes(metrics.footprintBytes) }}</dd>
      </dl>

      <p class="hint">
        `DELETE` 删了行**不等于**文件变小：空洞要 `studio db vacuum` 才还回去（要 2 倍
        空间 + 独占写，所以它不会自动跑）。
      </p>
    </PanelCard>

    <PanelCard
      title="四池"
      :subtitle="`worker 就绪 ${metrics.workerAlive}/${metrics.workerTotal}`"
    >
      <EmptyState v-if="metrics.pools.length === 0" title="还没有池状态" hint="等一次刷新。" />
      <table v-else class="table">
        <thead>
          <tr>
            <th>池</th>
            <th class="num">待跑</th>
            <th class="num">在跑</th>
            <th class="num">积压</th>
            <th class="num">死信</th>
            <th>最老待跑</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="card in metrics.pools" :key="card.pool">
            <td>
              {{ metrics.label(card.pool) }}
              <span class="mono muted">{{ card.pool }}</span>
            </td>
            <td class="num mono">{{ card.pending }}</td>
            <td class="num mono">{{ card.running }}</td>
            <td class="num mono">{{ card.backlog }}</td>
            <td class="num mono">{{ card.dead }}</td>
            <td class="mono">{{ ageSec(card.oldest_pending_age_sec) }}</td>
          </tr>
        </tbody>
      </table>
    </PanelCard>

    <PanelCard
      title="今日产量"
      :subtitle="metrics.today ? `本地日 ${metrics.today.day}` : '还没读过'"
    >
      <dl v-if="metrics.today" class="kv kv--tight">
        <dt>新建</dt>
        <dd class="mono">{{ metrics.today.created }}</dd>
        <dt>完成</dt>
        <dd class="mono">{{ metrics.today.completed }}</dd>
        <dt>失败</dt>
        <dd class="mono">{{ metrics.today.failed }}</dd>
        <dt>已发布</dt>
        <dd class="mono">{{ metrics.today.published }}</dd>
        <dt>按等级</dt>
        <dd class="mono">
          {{ Object.entries(metrics.today.by_grade).map(([k, v]) => `${k}:${v}`).join(' · ') || '-' }}
        </dd>
      </dl>
    </PanelCard>

    <PanelCard
      title="资源与进程"
      :subtitle="`采样 ${metrics.resources?.sampled_at ?? '-'} · 生成于 ${metrics.generatedAt ?? '-'}`"
    >
      <div class="line">
        <StatusDot
          :tone="metrics.diskTone"
          :label="`磁盘可用 ${metrics.resources?.disk_free_gb ?? '-'} GB`"
        />
      </div>
      <dl v-if="metrics.resources" class="kv kv--tight">
        <dt>CPU</dt>
        <dd class="mono">{{ metrics.resources.cpu_pct.toFixed(1) }}%</dd>
        <dt>内存</dt>
        <dd class="mono">{{ metrics.resources.ram_pct.toFixed(1) }}%（{{ metrics.resources.ram_mb }} MB）</dd>
        <dt>显存</dt>
        <dd class="mono">
          {{ metrics.resources.gpu_mem_mb ?? '-' }} / {{ metrics.resources.gpu_mem_total_mb ?? '-' }} MB
        </dd>
      </dl>

      <ul class="services">
        <li v-for="service in metrics.services" :key="service.name" class="services__item">
          <StatusDot :tone="service.ready ? 'ok' : 'warn'" :label="service.name" />
          <span class="mono services__readiness">{{ service.readiness }}</span>
          <span class="services__detail">{{ service.detail }}</span>
        </li>
      </ul>
    </PanelCard>
  </div>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: var(--space-4);
  align-content: start;
}

.line {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  margin-bottom: var(--space-2);
}

.kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: var(--space-1) var(--space-3);
  margin: 0;
}

.kv--tight {
  grid-template-columns: 76px 1fr;
}

.kv dt {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.kv dd {
  margin: 0;
}

.path {
  overflow-wrap: anywhere;
}

.muted {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.table th {
  color: var(--text-muted);
  font-weight: 500;
  text-align: left;
}

.table th.num,
.table td.num {
  text-align: right;
}

.table th,
.table td {
  padding: var(--space-1) var(--space-2);
  border-bottom: 1px solid var(--border-subtle);
}

.services {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: var(--space-3) 0 0;
  padding: 0;
  list-style: none;
}

.services__item {
  display: grid;
  grid-template-columns: 150px 110px 1fr;
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
</style>
