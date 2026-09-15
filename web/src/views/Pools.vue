<script setup lang="ts">
// 四池调度控制台（T4.10 · §04.4.5 / §03.4.4）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 四个池各自什么配置、现在什么状态？—— 配置值（pools.yaml）与运行值（DB）
//    **并排**摆出来；
// ② 把并发调一下？—— `−` / `+` 两个键，上下限就是后端那两条硬线；
// ③ 暂停 / 恢复？—— 复用总览台的写入口（同一件事只有一个权威落点 · 裁定 144）；
// ④ 死信重投 + "机器是不是在偷偷降我的并发"？—— 死信逐条可重投，OOM 计数摆在卡片上。
//
// 为什么"只降不升"这句话必须写在面板上
// ------------------------------------
// 自动降并发只**降**：`recover_after_min` 那条自动回升是 T4.11 无人值守编排的活。
// 面板上不写这一句，用户会一直等它自己涨回去 —— 等到发现不会涨，故障已经多烧了一晚上
// （裁定 151 的同一条理由：降不动也要说，说得越具体越好）。
//
// 为什么并发是"按键"而不是输入框
// ------------------------------
// 输入框要处理空串 / 负号 / 小数 / 超范围四种非法态，而这件事的全部语义就是
// "在 1..上限 之间挪一格"。按键把非法态从"要校验"变成"按不动"，顺带让上限
// 一眼可见（顶到上限时 `+` 是灰的，旁边写着为什么）。

import { computed, onMounted } from "vue";

import type { DeadLetter, PoolConsole } from "@/api/endpoints/pools";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import { asPoolName, poolLabel } from "@/stores/overview";
import { canLower, canRaise, needsAttention, poolState, poolTone, usePoolsStore } from "@/stores/pools";

const pools = usePoolsStore();

// 进面板即接线：订阅 WS 的 `pools` 通道 + 一次全量拉取（与选题 / 稿件面板同手法）。
// 退出面板**不退订**：这条通道的回调很便宜，而"回来时补一次全量"由 `start()` 保证。
onMounted(() => {
  pools.start();
});

const cards = computed<PoolConsole[]>(() => pools.pools);

const degradeNote = computed(() =>
  pools.autoDegradeEnabled
    ? `开（连续 ${pools.oomThreshold} 次 TTS_OOM ⇒ 并发 −1，只降不升）`
    : "关（只计数，不自动降并发）",
);

/** 卡片上那句"要不要人管"（空串 ⇒ 不画这一行）。 */
function degradeLine(card: PoolConsole): string {
  if (!needsAttention(card)) return "";
  return `连续 ${card.consecutive_oom} 次 OOM（阈值 ${card.oom_threshold}）：自动降并发只降不升，确认显存够了再手动调回去`;
}

function onRaise(card: PoolConsole): void {
  void pools.setConcurrency(asPoolName(card.pool), card.concurrency + 1);
}

function onLower(card: PoolConsole): void {
  void pools.setConcurrency(asPoolName(card.pool), card.concurrency - 1);
}

function onTogglePause(card: PoolConsole): void {
  void pools.pausePool(asPoolName(card.pool), !card.paused);
}

function onRequeueAll(card: PoolConsole): void {
  void pools.requeueDead(card.dead_letters.map((letter) => letter.job_id));
}

function onRequeueOne(letter: DeadLetter): void {
  void pools.requeueOne(letter);
}
</script>

<template>
  <div class="pools">
    <p v-if="pools.configError" class="alert alert--warn">
      配置读不到：{{ pools.configError }}（配置值那一列不可信；运行值仍然来自数据库）
    </p>

    <p v-if="pools.loadError" class="alert alert--error">
      四池拉取失败：{{ pools.loadError }}（下面显示的是上一次拿到的数字）
    </p>

    <p v-if="pools.error" class="alert alert--error">动作失败：{{ pools.error }}</p>
    <p v-else-if="pools.notice" class="alert alert--ok">{{ pools.notice }}</p>

    <p v-if="pools.degraded.length > 0" class="alert alert--warn">
      自动降并发已触发：
      {{ pools.degraded.map((card) => `${poolLabel(card.pool)} 连续 ${card.consecutive_oom} 次`).join(" · ") }}
      —— 只降不升，确认显存够了再手动调回去。
    </p>

    <PanelCard
      title="四池调度"
      :subtitle="`积压 ${pools.backlogTotal} · 死信 ${pools.deadTotal} · 自动降并发 ${degradeNote} · 配置 ${pools.configPath || '-'}`"
    >
      <template #actions>
        <AppButton size="sm" :loading="pools.loading" @click="pools.refresh()">刷新</AppButton>
      </template>

      <EmptyState
        v-if="cards.length === 0 && !pools.loading"
        title="还没有池状态"
        hint="`pool_settings` 应有 draft / voice / render / publish 四行；跑一次 `studio db migrate` 补齐。"
      />

      <div v-else class="grid">
        <section
          v-for="card in cards"
          :key="card.pool"
          class="pool"
          :class="{ 'pool--paused': card.paused, 'pool--broken': Boolean(card.error) }"
        >
          <header class="pool__head">
            <span class="pool__name">{{ poolLabel(card.pool) }}</span>
            <span class="pool__id mono">{{ card.pool }}</span>
            <span class="pool__spacer" />
            <StatusDot :tone="poolTone(card)" :label="poolState(card)" />
          </header>

          <p v-if="card.error" class="pool__error">{{ card.error }}</p>

          <div class="rows">
            <div class="row">
              <span class="row__key">配置</span>
              <span class="row__val mono">
                {{ card.unit_type ?? "-" }} · 优先级 {{ card.priority ?? "-" }} · 轮询
                {{ card.poll_ms ?? "-" }}ms · 单元超时 {{ card.unit_timeout_sec ?? "-" }}s
              </span>
            </div>
            <div class="row">
              <span class="row__key">运行值</span>
              <span class="row__val mono">
                并发 {{ card.concurrency }}（出厂 {{ card.config_concurrency ?? "-" }}） · 租约
                {{ card.lease_sec }}s · 最多 {{ card.max_attempts }} 次
              </span>
            </div>
            <div class="row">
              <span class="row__key">积压</span>
              <span class="row__val mono">
                待认领 {{ card.pending }} · 等上游 {{ card.blocked }} · 在跑 {{ card.running }} · 死信
                {{ card.dead }} · 成功 {{ card.succeeded }} · 失败 {{ card.failed
                }}<template v-if="card.oldest_pending_age_sec !== null">
                  · 最老 {{ card.oldest_pending_age_sec }}s</template>
              </span>
            </div>
          </div>

          <div class="knob">
            <span class="knob__label">并发</span>
            <AppButton
              size="sm"
              :disabled="!canLower(card) || pools.busy"
              title="调到下限就不能再降了（0 是沉默的暂停，暂停有自己的开关）"
              @click="onLower(card)"
            >
              −
            </AppButton>
            <span class="knob__value mono">{{ card.concurrency }}</span>
            <AppButton
              size="sm"
              :disabled="!canRaise(card) || pools.busy"
              title="到工程上限就按不动了（voice 3 = 8GB 显存的保守值）"
              @click="onRaise(card)"
            >
              +
            </AppButton>
            <span class="knob__range mono">{{ card.concurrency_min }}–{{ card.concurrency_max }}</span>
            <span v-if="card.at_concurrency_ceiling" class="knob__note">已到上限</span>
            <span class="knob__spacer" />
            <AppButton size="sm" :disabled="pools.busy" @click="onTogglePause(card)">
              {{ card.paused ? "恢复" : "暂停" }}
            </AppButton>
          </div>

          <p v-if="degradeLine(card)" class="pool__warn">{{ degradeLine(card) }}</p>

          <div v-if="card.dead_letters.length > 0" class="dead">
            <div class="dead__head">
              <span class="dead__title">
                死信 {{ card.dead }} 条（显示最近 {{ card.dead_letters.length }} 条）
              </span>
              <AppButton size="sm" :disabled="pools.busy" @click="onRequeueAll(card)">
                全部重投
              </AppButton>
            </div>
            <ul class="dead__list">
              <li v-for="letter in card.dead_letters" :key="letter.job_id" class="dead__item">
                <span class="mono dead__id">{{ letter.job_id }}</span>
                <span class="mono dead__ref">{{ letter.unit_type }} · {{ letter.unit_ref }}</span>
                <span class="dead__err">
                  {{ letter.error_code ?? "-" }}：{{ letter.error_message ?? "-" }}
                </span>
                <span class="mono dead__tries">{{ letter.attempts }}/{{ letter.max_attempts }}</span>
                <AppButton size="sm" :disabled="pools.busy" @click="onRequeueOne(letter)">
                  重投
                </AppButton>
              </li>
            </ul>
          </div>
        </section>
      </div>
    </PanelCard>
  </div>
</template>

<style scoped>
.pools {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.alert {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-left-width: 3px;
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  border-left-color: var(--error);
}

.alert--warn {
  color: var(--warn);
  border-left-color: var(--warn);
}

.alert--ok {
  color: var(--ok);
  border-left-color: var(--ok);
}

.grid {
  display: grid;
  gap: var(--space-3);
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
}

.pool {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}

.pool--paused {
  border-color: var(--warn);
}

.pool--broken {
  border-color: var(--error);
}

.pool__head {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.pool__name {
  font-size: var(--text-lg);
  font-weight: 600;
}

.pool__id,
.pool__spacer {
  color: var(--text-muted);
}

.pool__spacer,
.knob__spacer {
  flex: 1;
}

.pool__error {
  color: var(--error);
  font-size: var(--text-sm);
}

.pool__warn {
  color: var(--warn);
  font-size: var(--text-sm);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.row {
  display: flex;
  gap: var(--space-2);
}

.row__key {
  flex: 0 0 48px;
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.row__val {
  color: var(--text-secondary);
  word-break: break-all;
}

.knob {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding-top: var(--space-2);
  border-top: 1px solid var(--border-subtle);
}

.knob__label,
.knob__range,
.knob__note {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.knob__value {
  min-width: 20px;
  color: var(--text-primary);
  text-align: center;
}

.knob__note {
  color: var(--warn);
}

.dead {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border-subtle);
}

.dead__head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  justify-content: space-between;
}

.dead__title {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.dead__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.dead__item {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-panel);
  border-radius: var(--radius-sm);
}

.dead__id {
  color: var(--text-primary);
}

.dead__ref,
.dead__tries {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.dead__err {
  flex: 1;
  min-width: 0;
  color: var(--error);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
