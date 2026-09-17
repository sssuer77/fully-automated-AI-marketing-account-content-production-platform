<script setup lang="ts">
// ⑨ 发布面板（T5.5 · §06.11 / §06.12）。
//
// 这一屏按规格的**七区块**排（待发布 / 发布中 / 已发布 / 数据回流 / 待人工 / 定时计划 / 报告），
// 外加两块：
//   - **失败 / 已取消**：它们不在七区块里，但藏起来更糟 —— 失败中的记录还在自动重试，
//     看不见它就会以为"这条根本没投出去过"；
//   - **交付包**：规格与验收都点名了 `HandoffAdapter`，而它是这一屏唯一"往外送东西"的动作。
//
// 三件必须写在面板上的事
// ----------------------
// 1. **R2 提示是常驻的**，不是可关的横幅：复刻他人声音可能同时触及声音权与著作权，
//    而"是否可商用"只有人能判断 —— 工具只负责把"缺哪一份"摆出来。
// 2. **待人工计数在顶部**：那是这一屏唯一"要求人做决定"的地方，其余都是"看"。
// 3. **定时计划与报告还没施工**：后端没有对应端点，所以这两块**不画任何数字**。
//    画一个看着像真的时刻表，比空着更糟 —— 用户会按它去安排发布。
//
// 为什么交付包要"先预览再导出"
// ----------------------------
// 不看一眼包里缺什么就交出去，收到的是一份自己都不知道少了什么的包。预览与打包在
// 后端走的是**同一个函数**，所以预览说六件齐，打出来就真是六件齐。

import { computed, onMounted, onUnmounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { Publication } from "@/api/endpoints/publish";
import {
  BOARD_STATUSES,
  formatStamp,
  metricsText,
  missingText,
  plainText,
  rowsOf,
  statusLabel,
  statusTone,
  usePublishStore,
} from "@/stores/publish";

const publish = usePublishStore();

onMounted(() => {
  // 进面板即接线：一次拉齐看板 + 待人工 + 合规；有活在跑时 store 自己按
  // `PUBLISH_POLL_MS` 轮询，离开面板必须停 —— 否则切走之后还在每两秒问一次后端。
  publish.start();
});
onUnmounted(() => publish.stop());

/** 一块看板（区块名 + 那几条 + 空态那句话）。 */
interface Block {
  key: string;
  title: string;
  rows: Publication[];
  empty: string;
}

const counts = computed(() => publish.counts);
const board = computed(() => publish.board);

/** 三块按状态的看板（顺序就是屏上的顺序）。 */
const statusBlocks = computed<Block[]>(() => {
  const empties: Record<string, string> = {
    queued: "没有排队中的发布",
    uploading: "没有正在上传的发布",
    published: "还没有发出去过",
  };
  return BOARD_STATUSES.map((status) => ({
    key: status,
    title: statusLabel(status),
    rows: rowsOf(board.value, status),
    empty: empties[status] ?? "没有",
  }));
});

const endedRows = computed(() => publish.endedRows);
const manualRows = computed(() => publish.manualRows);
const metricsRows = computed(() => publish.metrics);
const compliance = computed(() => publish.compliance);
const preview = computed(() => publish.handoffPreview);
const result = computed(() => publish.handoffResult);

/** 顶部那一行数字（六个状态一个不少 —— 少了哪个都会让人以为"这类不存在"）。 */
const summary = computed(() => {
  const order = ["queued", "uploading", "published", "failed", "manual_required", "canceled"];
  return order.map((status) => ({
    status,
    label: statusLabel(status),
    value: counts.value[status] ?? 0,
  }));
});

function onHandoffTask(event: Event): void {
  publish.setHandoffTaskId((event.target as HTMLInputElement).value);
}

function onReason(publicationId: string, event: Event): void {
  publish.setReason(publicationId, (event.target as HTMLInputElement).value);
}

function onCancelReason(publication: Publication): void {
  // 取消不强制写理由（后端也不要求），但面板给一句默认的 —— 留痕里空白最难查。
  void publish.cancel(publication, "人工取消");
}
</script>

<template>
  <div class="publish">
    <!-- ① 顶部：一眼看清有什么、以及**有几条在等人** -->
    <PanelCard title="发布台" subtitle="投递 → 上传 → 平台确认 → 数据回流；出事的那几条在「待人工」里等人做决定">
      <template #actions>
        <StatusDot
          v-if="publish.manual > 0"
          tone="warn"
          :label="`待人工 ${publish.manual}`"
          pulse
        />
        <StatusDot
          :tone="publish.active ? 'busy' : 'idle'"
          :label="publish.active ? '有活在跑' : '空闲'"
        />
        <StatusDot
          :tone="publish.complianceOk ? 'ok' : 'warn'"
          :label="publish.complianceOk ? '来源登记齐' : `来源登记缺 ${compliance?.gaps?.length ?? 0} 份`"
        />
        <AppButton size="sm" :loading="publish.loading" @click="publish.refreshAll()">
          刷新
        </AppButton>
      </template>

      <ul class="tally">
        <li v-for="entry in summary" :key="entry.status" class="tally__item">
          <StatusDot :tone="statusTone(entry.status)" :label="entry.label" />
          <span class="tally__value mono">{{ entry.value }}</span>
        </li>
      </ul>

      <p v-if="publish.error" class="alert alert--error">动作失败：{{ publish.error }}</p>
      <p v-else-if="publish.notice" class="alert alert--ok">{{ publish.notice }}</p>
      <p v-if="publish.loadError" class="alert alert--error">读不到看板：{{ publish.loadError }}</p>
      <p v-if="board?.hint" class="hint">{{ board.hint }}</p>

      <p class="hint">
        有活在跑（排队中 / 上传中）时这一屏每 2 秒自己刷一次；空闲时**不轮询** ——
        代价是别的进程刚转人工的那一条不会自己冒出来，按「刷新」即可。
        真发布由 publish 池的 worker 干，这一屏只投作业、看结果、做人工处置。
      </p>
    </PanelCard>

    <!-- ② R2 合规提示：**常驻**（§06.11），不是可关的横幅 -->
    <PanelCard
      title="R2 来源登记"
      :subtitle="
        compliance === null
          ? '还没读到'
          : compliance.ok
            ? '音色 / BGM / 跑酷素材的来源登记都齐'
            : `缺 ${compliance.gaps?.length ?? 0} 份登记（不阻塞发布，但请自行确认授权范围）`
      "
    >
      <template #actions>
        <AppButton size="sm" @click="publish.refreshCompliance()">重新体检</AppButton>
      </template>

      <p v-if="compliance" class="notice">{{ plainText(compliance.notice) }}</p>
      <p v-else class="hint">还没读到合规快照。</p>

      <ul v-if="compliance && (compliance.gaps?.length ?? 0) > 0" class="gaps">
        <li v-for="gap in compliance.gaps ?? []" :key="gap" class="gaps__item">{{ gap }}</li>
      </ul>

      <details v-if="compliance && (compliance.items?.length ?? 0) > 0" class="more">
        <summary>逐条登记（{{ compliance.items?.length ?? 0 }} 件）</summary>
        <ul class="rows">
          <li v-for="item in compliance.items ?? []" :key="`${item.kind}:${item.id}`" class="row">
            <StatusDot :tone="item.proof_present ? 'ok' : 'warn'" :label="item.kind" />
            <span class="mono">{{ item.id }}</span>
            <span class="row__val">{{ item.proof ?? "（没有登记）" }}</span>
            <span v-if="item.note" class="row__note">{{ item.note }}</span>
          </li>
        </ul>
      </details>
    </PanelCard>

    <!-- ③ 交付包：预览 → 导出（**先看再给**） -->
    <PanelCard
      title="交付包"
      subtitle="把一支成片连同封面 / 字幕 / 稿件 / 时间轴 / 渲染留痕打成一个自包含目录（含 handoff.json）"
    >
      <template #actions>
        <AppButton
          size="sm"
          :loading="publish.handoffBusy"
          :disabled="!publish.canPreviewHandoff"
          @click="publish.previewHandoff()"
        >
          预览
        </AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :loading="publish.handoffBusy"
          :disabled="!publish.canPreviewHandoff || preview === null"
          @click="publish.exportHandoff()"
        >
          导出
        </AppButton>
      </template>

      <div class="form">
        <label class="f f--task">
          <span class="f__key">任务号</span>
          <input
            class="field mono"
            type="text"
            placeholder="ULID 或你自己起的名字"
            :value="publish.handoffTaskId"
            @change="onHandoffTask"
          />
        </label>
      </div>

      <p v-if="publish.handoffError" class="alert alert--error">{{ publish.handoffError }}</p>

      <div v-if="preview" class="preview">
        <div class="preview__head">
          <StatusDot
            :tone="preview.ready ? 'ok' : 'warn'"
            :label="preview.ready ? '可以交出去' : '还不完整'"
          />
          <span class="preview__title">{{ missingText(preview) }}</span>
          <span class="preview__stale mono">适配器 {{ preview.adapter }}</span>
        </div>
        <p v-if="preview.note" class="preview__note">{{ preview.note }}</p>
        <ul class="rows">
          <li v-for="item in preview.items" :key="item.kind" class="row">
            <StatusDot :tone="item.present ? 'ok' : 'idle'" :label="item.label" />
            <span class="row__val">{{ item.present ? "在" : "缺" }}</span>
            <span v-if="!item.present && item.note" class="row__note">{{ item.note }}</span>
          </li>
        </ul>
        <p class="hint">
          导出会复制到 <span class="mono">{{ preview.output_dir }}</span>；
          {{ preview.auto_on_publish ? "发布时会自动顺手带一份。" : "发布时不会自动带一份（手动导出不受这个开关影响）。" }}
        </p>
      </div>

      <div v-if="result" class="preview">
        <p class="preview__title">已导出到 <span class="mono">{{ result.root }}</span></p>
        <p class="preview__note">
          清单 <span class="mono">{{ result.manifest }}</span> · {{ result.bytes }} 字节 ·
          缺 {{ result.missing?.length ?? 0 }} 件
        </p>
      </div>

      <p class="hint">
        预览**一个字节都不写**；导出会写 <span class="mono">audit_ops</span> 一行（交付包是离开
        我们掌控的东西）。两次导出不会互相覆盖 —— 每次落在一个带时间戳的新目录里。
      </p>
    </PanelCard>

    <!-- ④⑤⑥ 三块状态看板 -->
    <PanelCard
      v-for="block in statusBlocks"
      :key="block.key"
      :title="block.title"
      :subtitle="`${block.rows.length} 条`"
    >
      <ul v-if="block.rows.length > 0" class="rows">
        <li v-for="row in block.rows" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot :tone="statusTone(row.status)" :label="statusLabel(row.status)" />
            <span class="row__title">{{ row.title }}</span>
            <span v-if="row.dry_run" class="tag">演练</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ row.platform }} · {{ row.account_id }}</span>
          </div>
          <p class="row__sub">
            任务 <span class="mono">{{ row.task_id }}</span> · 尝试 {{ row.attempt_count }}/{{ row.max_attempts }}
            <template v-if="row.published_at"> · 发布于 {{ formatStamp(row.published_at) }}</template>
            <template v-else> · 登记于 {{ formatStamp(row.created_at) }}</template>
          </p>
          <p v-if="row.url" class="row__sub">
            <a :href="row.url" target="_blank" rel="noreferrer">{{ row.url }}</a>
          </p>
        </li>
      </ul>
      <EmptyState v-else :title="block.empty" hint="这里只显示最近 50 条；更早的在数据库里。" />
    </PanelCard>

    <!-- ⑦ 数据回流 -->
    <PanelCard
      title="数据回流"
      :subtitle="`${metricsRows.length} 条有回流信息`"
    >
      <ul v-if="metricsRows.length > 0" class="rows">
        <li v-for="row in metricsRows" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot tone="ok" :label="row.platform" />
            <span class="row__title">{{ row.title }}</span>
          </div>
          <p class="row__sub">{{ metricsText(row) }}</p>
        </li>
      </ul>
      <EmptyState
        v-else
        title="还没有可回流的数字"
        hint="回流时刻表在发布成功那一刻就写好了（T+1h 起）；真正去平台上取数字的作业还没施工，所以这里目前只有时刻表，没有数字。"
      />
    </PanelCard>

    <!-- ⑧ 待人工：唯一一个"要求人做决定"的区块 -->
    <PanelCard
      title="待人工"
      :subtitle="
        manualRows.length > 0
          ? `${manualRows.length} 条等你处理：重试 / 取消 / 标记已处理（三者都写 audit_ops）`
          : '自动这条路走完了，现在没有需要你做决定的发布'
      "
    >
      <template #actions>
        <StatusDot v-if="publish.manual > 0" tone="warn" :label="`${publish.manual}`" />
        <AppButton size="sm" :loading="publish.busy" @click="publish.refreshQueue()">刷新队列</AppButton>
      </template>

      <ul v-if="manualRows.length > 0" class="rows">
        <li v-for="row in manualRows" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot tone="warn" :label="statusLabel(row.status)" />
            <span class="row__title">{{ row.title }}</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ row.platform }} · {{ row.account_id }}</span>
          </div>
          <p class="row__sub">
            任务 <span class="mono">{{ row.task_id }}</span> · 尝试 {{ row.attempt_count }}/{{ row.max_attempts }} ·
            最后动于 {{ formatStamp(row.updated_at) }}
          </p>
          <p v-if="row.error_code" class="alert alert--error">
            {{ row.error_code }}：{{ row.error_message ?? "（没有更多说明）" }}
          </p>
          <p v-if="Object.keys(row.evidence ?? {}).length > 0" class="row__sub">
            取证：<span class="mono">{{ Object.keys(row.evidence ?? {}).join(" / ") }}</span>
          </p>

          <div class="row__actions">
            <AppButton
              size="sm"
              :disabled="!row.can_retry || publish.busy"
              :title="row.can_retry ? '把这条重新排进发布池（尝试次数归零）' : '这条已经结束，不能重试'"
              @click="publish.retry(row)"
            >
              重试
            </AppButton>
            <AppButton
              size="sm"
              variant="danger"
              :disabled="!row.can_cancel || publish.busy"
              :title="row.can_cancel ? '作废这条记录（平台上没有它）' : '已发布的不能取消 —— 平台上已经有那条作品了'"
              @click="onCancelReason(row)"
            >
              取消
            </AppButton>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="!row.can_mark_done || publish.busy"
              title="我在平台上手工处理完了，或者决定放弃这条（必须写理由）"
              @click="publish.openReason(row.id)"
            >
              标记已处理
            </AppButton>
          </div>

          <div v-if="publish.reasonFor === row.id" class="reason">
            <input
              class="field"
              type="text"
              placeholder="为什么算处理完了（必填，写进 audit_ops）"
              :value="publish.reasonDraft[row.id] ?? ''"
              @input="onReason(row.id, $event)"
            />
            <AppButton size="sm" variant="primary" :loading="publish.busy" @click="publish.markDone(row)">
              确认
            </AppButton>
            <AppButton size="sm" @click="publish.closeReason()">算了</AppButton>
          </div>
        </li>
      </ul>
      <EmptyState
        v-else
        title="待人工队列是空的"
        hint="没有需要你处理的发布 —— 这是常态，不是没接线。"
      />
    </PanelCard>

    <!-- ⑨ 失败 / 已取消（不在规格的七区块里，但藏起来更糟） -->
    <PanelCard title="失败 / 已取消" :subtitle="`${endedRows.length} 条`">
      <ul v-if="endedRows.length > 0" class="rows">
        <li v-for="row in endedRows" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot :tone="statusTone(row.status)" :label="statusLabel(row.status)" />
            <span class="row__title">{{ row.title }}</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ row.platform }} · {{ row.account_id }}</span>
          </div>
          <p class="row__sub">
            任务 <span class="mono">{{ row.task_id }}</span> · 尝试 {{ row.attempt_count }}/{{ row.max_attempts }}
          </p>
          <p v-if="row.error_code" class="row__sub">
            <span class="mono">{{ row.error_code }}</span>：{{ row.error_message ?? "（没有更多说明）" }}
          </p>
        </li>
      </ul>
      <EmptyState
        v-else
        title="没有失败或取消的发布"
        hint="失败到上限的会自动进「待人工」，所以这里通常只有还在自动重试的那几条。"
      />
    </PanelCard>

    <!-- ⑩⑪ 两块**尚未施工**：后端没有端点，所以一个数字都不画 -->
    <PanelCard title="定时计划" subtitle="T5.6 · 尚未施工">
      <EmptyState
        title="这块还没接线"
        hint="后端目前没有定时计划相关的端点（tests/integration/test_scheduler.py 也不存在）。投递接口上虽然留了 scheduled_at 字段，但没有东西会去读它 —— 所以这里不画任何时刻表：画一个看着像真的表，比空着更糟。"
      />
    </PanelCard>

    <PanelCard title="报告" subtitle="T5.7 · 尚未施工">
      <EmptyState
        title="这块还没接线"
        hint="后端目前没有报告相关的端点（tests/integration/test_reports.py 也不存在）。上面「数据回流」里的数字是这条链路目前唯一能看到的统计，它来自发布记录本身，不是报告模块。"
      />
    </PanelCard>
  </div>
</template>

<style scoped>
.publish {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}

.tally {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin-bottom: var(--space-2);
}

.tally__item {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.tally__value {
  color: var(--text-primary);
  font-size: var(--text-lg);
}

.notice {
  color: var(--text-secondary);
  font-size: var(--text-sm);
  line-height: 1.7;
}

.gaps {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.gaps__item {
  color: var(--warn);
  font-size: var(--text-xs);
}

.more {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.more summary {
  cursor: pointer;
}

.form {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: flex-end;
}

.f {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.f--task {
  flex: 0 0 320px;
}

.f__key {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.field {
  height: 24px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.preview {
  padding: var(--space-2) var(--space-3);
  margin-top: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.preview__head {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.preview__title {
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.preview__stale {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.preview__note {
  margin-top: var(--space-1);
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.row {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  font-size: var(--text-sm);
}

.row--stack {
  flex-direction: column;
  gap: var(--space-1);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border-subtle);
}

.row__head {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  width: 100%;
}

.row__title {
  color: var(--text-primary);
}

.row__spacer {
  flex: 1;
}

.row__mono {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.row__val {
  min-width: 0;
  color: var(--text-secondary);
  word-break: break-all;
}

.row__note {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.row__sub {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.row__actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.reason {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  margin-top: var(--space-1);
}

.reason .field {
  flex: 1 1 320px;
}

.tag {
  padding: 0 var(--space-1);
  color: var(--text-inverse);
  font-size: var(--text-xs);
  background: var(--warn);
  border-radius: var(--radius-sm);
}

.alert {
  padding: var(--space-2) var(--space-3);
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  border-color: var(--error);
}

.alert--ok {
  color: var(--ok);
  border-color: var(--ok);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
  line-height: 1.7;
}
</style>