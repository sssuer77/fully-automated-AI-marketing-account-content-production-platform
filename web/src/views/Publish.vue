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
// 3. **定时计划与报告都是真接线**（T5.6 / T5.7）：计划能建、能改、能停、能立刻跑一次；
//    报告能看、能采纳建议、能导出，报告周期同样能建能改。报告里的每个数字都来自后端
//    那一条 SQL，面板不做任何估算 —— 画一个看着像真的统计，比空着更糟。
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
import type { Publication, PublishPlatformOption } from "@/api/endpoints/publish";
import {
  MAX_JITTER_MIN,
  SCHEDULE_MODES,
  modeLabel as scheduleModeLabel,
  nextRunText,
  resultLabel as scheduleResultLabel,
  resultTone as scheduleResultTone,
  scheduleSummary,
  stampText as scheduleStampText,
  useSchedulesStore,
} from "@/stores/schedules";
import {
  INCLUDE_KINDS,
  WEEKDAYS,
  confidenceLabel,
  confidenceTone,
  countText,
  includeLabel,
  insightEvidenceText,
  insightIsShaky,
  insightKindLabel,
  nextRunText as reportNextRunText,
  periodLabel,
  resultLabel as reportResultLabel,
  resultTone as reportResultTone,
  scheduleSummary as reportScheduleSummary,
  stampText as reportStampText,
  summaryText,
  triggerLabel,
  useReportsStore,
  windowText,
} from "@/stores/reports";
import {
  BOARD_STATUSES,
  enqueueText,
  formatStamp,
  metricsText,
  missingText,
  optionText,
  plainText,
  platformLabel,
  rowsOf,
  statusLabel,
  statusTone,
  tickText,
  usePublishStore,
} from "@/stores/publish";

const publish = usePublishStore();

// 定时计划单独一个 store：它的轮询节奏与发布看板**不是一回事**（15s vs 2s，见 store 注释）。
const schedules = useSchedulesStore();

// 报告再单独一个（60s）：它看的是「到点那一刻多出一份」，比计划还慢。
const reports = useReportsStore();

onMounted(() => {
  // 进面板即接线：一次拉齐看板 + 待人工 + 合规；有活在跑时 store 自己按
  // `PUBLISH_POLL_MS` 轮询，离开面板必须停 —— 否则切走之后还在每两秒问一次后端。
  publish.start();
  // 定时计划慢轮询（15s）：它看的是**时间**，不是某个进程在忙 —— 到点了才需要重画。
  schedules.start();
  // 报告更慢（60s）：一份报告只可能在到点那一刻多出来，问得再勤也不会早一秒。
  reports.start();
});
onUnmounted(() => {
  publish.stop();
  schedules.stop();
  reports.stop();
});

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
/** 上一轮回收的结论（没跑过 ⇒ 空串，不占屏）。 */
const lastTick = computed(() => tickText(publish.metricsTick));
/** 投递那块的两份读（清单 + 勾选）。 */
const platformItems = computed(() => publish.platformItems);
const defaultTargets = computed(() => publish.defaultTargets);
const enqueueWarning = computed(() => publish.enqueueWarning);
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

function onEnqueueTask(event: Event): void {
  publish.setEnqueueTaskId((event.target as HTMLInputElement).value);
}

/**
 * 一个平台下「投哪几个号」的那行小字（**只有多账号平台才画**）。
 *
 * 全勾时说「全部 N 个号」而不是把 N 个名字再列一遍：勾选框旁边那行 `optionText`
 * 已经写过这一份名单了，同一个平台写两遍名单会让人以为它们在说两件事。
 */
function accountCaption(option: PublishPlatformOption): string {
  const all = option.accounts ?? [];
  const picked = publish.enqueueAccounts(option.code).length;
  return picked === all.length ? `全部 ${all.length} 个号` : `只投 ${picked} / ${all.length} 个号`;
}

function onReason(publicationId: string, event: Event): void {
  publish.setReason(publicationId, (event.target as HTMLInputElement).value);
}

// ── 定时计划（T5.6）────────────────────────────────────────────

const scheduleItems = computed(() => schedules.items);
const scheduleDraft = computed(() => schedules.draft);
const scheduleSubtitle = computed(() => {
  if (schedules.loading && scheduleItems.value.length === 0) return "读取中";
  const counts = schedules.counts;
  return `共 ${counts.total ?? 0} 条 · 启用 ${counts.enabled ?? 0} 条` +
    ((counts.failing ?? 0) > 0 ? ` · 连续失败 ${counts.failing} 条` : "");
});
const scheduleTickSec = computed(() => Math.round(schedules.tickSec));

function scheduleBusy(scheduleId: string): boolean {
  return schedules.busyId === scheduleId;
}

/** 表单里任意一个输入框 -> store（`checkbox` 取 `checked`，数字框转成数字）。 */
function onDraft(event: Event, key: string): void {
  const target = event.target as HTMLInputElement;
  const raw = target.type === "checkbox" ? target.checked : target.value;
  const value = key === "jitterMin" || key === "intervalHours" ? Number(raw) : raw;
  schedules.setDraft({ [key]: value } as never);
}

function onDraftPlatform(event: Event): void {
  schedules.setDraft({ platform: (event.target as HTMLSelectElement).value });
}

// ── 报告（T5.7）────────────────────────────────────────────

const reportItems = computed(() => reports.items);
const reportDetail = computed(() => reports.detail);
const reportInsights = computed(() => reports.insights);
const reportDraft = computed(() => reports.draft);
const reportEditingId = computed(() => reports.editingId);
const reportPeriods = computed(() =>
  reports.list !== null && reports.list.periods.length > 0
    ? reports.list.periods
    : ["daily", "weekly", "monthly"],
);
const reportSubtitle = computed(() => {
  if (reports.loading && reportItems.value.length === 0) return "读取中";
  const counts = reports.counts;
  return `共 ${counts.total ?? 0} 份 · 有建议 ${counts.with_insights ?? 0} 份 · 累计采纳 ${counts.applied ?? 0} 条`;
});
const reportScheduleItems = computed(() => reports.scheduleItems);
const reportScheduleSubtitle = computed(() => {
  const counts = reports.scheduleCounts;
  const failing = counts.failing ?? 0;
  return (
    `共 ${counts.total ?? 0} 条 · 启用 ${counts.enabled ?? 0} 条 · 出厂自带 ${counts.builtin ?? 0} 条` +
    (failing > 0 ? ` · 连续失败 ${failing} 条` : "")
  );
});

/** 报告落盘的那一份（后端可能没写盘 ⇒ 空串，那一行就不占屏）。 */
const reportArtifact = computed<string>(() => reportDetail.value?.artifacts?.[0] ?? "");

function reportBusy(scheduleId: string): boolean {
  return reports.busyId === scheduleId;
}

/** 「现在生成」用哪个周期（与下面表单里那条周期**互不影响**）。 */
function onReportPeriod(event: Event): void {
  reports.period = (event.target as HTMLSelectElement).value;
}

/** 表单里任意一个输入框 -> store（`checkbox` 取 `checked`，数字框转成数字）。 */
function onReportDraft(event: Event, key: string): void {
  const target = event.target as HTMLInputElement;
  const raw = target.type === "checkbox" ? target.checked : target.value;
  const numeric = key === "weekday" || key === "dayOfMonth" || key === "lookbackDays";
  reports.setDraft({ [key]: numeric ? Number(raw) : raw } as never);
}

/** 换周期（回看天数跟着走：weekly 改成 daily 还带着 7 天是错的）。 */
function onReportPeriodPick(event: Event): void {
  reports.setPeriod((event.target as HTMLSelectElement).value);
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

    <!-- ② 投递：这一屏唯一"往外投一条"的动作（T5.10 · §06.5.4） -->
    <PanelCard
      title="投递"
      :subtitle="
        defaultTargets.length > 0
          ? `填任务号 → 勾平台（多账号的可以只勾其中几个号）→ 投进发布池；一个平台都不勾 = 投 ${defaultTargets.map(platformLabel).join(' / ')}`
          : '还没读到平台清单'
      "
    >
      <template #actions>
        <AppButton size="sm" @click="publish.loadPlatforms()">重读选项</AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :loading="publish.enqueueBusy"
          :disabled="!publish.canEnqueue"
          @click="publish.enqueue()"
        >
          投进发布池
        </AppButton>
      </template>

      <div class="form">
        <label class="f f--task">
          <span class="f__key">任务号</span>
          <input
            class="field mono"
            type="text"
            placeholder="出片完成的任务号"
            :value="publish.enqueueTaskId"
            @change="onEnqueueTask"
          />
        </label>
      </div>

      <ul v-if="platformItems.length > 0" class="checks">
        <li v-for="option in platformItems" :key="option.code" class="checks__item">
          <label class="check">
            <input
              type="checkbox"
              :checked="publish.enqueuePick.includes(option.code)"
              :disabled="!option.selectable"
              @change="publish.toggleEnqueuePlatform(option.code)"
            />
            <span>{{ optionText(option) }}</span>
          </label>

          <!-- 账号细分（T5.8）：勾中的平台**真有多个号**时才画。
               勾上平台 = 这个平台下的号**全投**；取消某一个 = 只投剩下的。 -->
          <div
            v-if="publish.enqueuePick.includes(option.code) && (option.accounts ?? []).length > 1"
            class="accounts"
          >
            <span class="accounts__key">{{ accountCaption(option) }}</span>
            <ul class="checks checks--sub">
              <li v-for="account in option.accounts ?? []" :key="account" class="checks__item">
                <label class="check">
                  <input
                    type="checkbox"
                    :checked="publish.enqueueAccounts(option.code).includes(account)"
                    @change="publish.toggleEnqueueAccount(option.code, account)"
                  />
                  <span class="mono">{{ account }}</span>
                </label>
              </li>
            </ul>
          </div>
        </li>
      </ul>
      <p v-else class="hint">
        还没读到平台清单 —— 这一块**不画任何自己猜的平台**（清单来自
        <span class="mono">config/publish.yaml</span>，面板自己列一遍就会漏改）。
      </p>

      <p v-if="enqueueWarning" class="alert alert--warn">{{ plainText(enqueueWarning) }}</p>

      <p v-if="publish.enqueueError" class="alert alert--error">
        {{ publish.enqueueError }}
      </p>
      <p v-else-if="publish.enqueueResult" class="alert alert--ok">
        {{ enqueueText(publish.enqueueResult) }}
      </p>

      <p v-if="publish.enqueueNarrowed > 0" class="hint">
        只投了一部分账号：同一个任务在每个勾中的号上各是一条作业、各留一条发布记录
        （限频、登录态都是按账号各算各的），所以少勾一个号 = 少发一条。
      </p>

      <p class="hint">
        投递只把作业排进发布池（真发布由 publish 池的 worker 干）。出厂
        <span class="mono">publish.enabled=false</span> 时投递照样成功，但真平台那一条
        会在 worker 侧带 PUBLISH_DISABLED <strong>进死信</strong> —— 发布面板上不会出现
        记录（去「四池调度」看），那是 R14 的不可逆防护，不是故障。
        没有真账号时勾「本地演练台」：它发到本机靶页，发完照样能采数、沉淀。
      </p>
    </PanelCard>

    <!-- ③ R2 合规提示：**常驻**（§06.11），不是可关的横幅 -->
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

    <!-- ④ 交付包：预览 → 导出（**先看再给**） -->
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

    <!-- ⑤⑥⑦ 三块状态看板 -->
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
            <span class="row__mono mono">
              {{ platformLabel(row.platform) }} · {{ row.account_id }}
            </span>
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

    <!-- ⑧ 数据回流 -->
    <PanelCard
      title="数据回流"
      :subtitle="`${metricsRows.length} 条有回流信息 · 后台每 60s 自己拍一次`"
    >
      <template #actions>
        <AppButton size="sm" :loading="publish.metricsBusy" @click="publish.tickMetrics()">
          跑一轮
        </AppButton>
      </template>

      <p v-if="lastTick" class="row__sub">上一轮：{{ lastTick }}</p>

      <ul v-if="metricsRows.length > 0" class="rows">
        <li v-for="row in metricsRows" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot tone="ok" :label="row.platform" />
            <span class="row__title">{{ row.title }}</span>
            <span class="row__spacer" />
            <span class="row__mono mono">
              下一次 {{ row.next_metric_at ? formatStamp(row.next_metric_at) : "已停止" }}
              <template v-if="row.metric_attempts > 0"> · 失败 {{ row.metric_attempts }} 次</template>
            </span>
            <AppButton
              size="sm"
              :disabled="publish.metricsBusy"
              @click="publish.collectOne(row)"
            >
              采数
            </AppButton>
            <AppButton
              size="sm"
              :disabled="publish.metricsBusy"
              @click="publish.sinkOne(row)"
            >
              沉淀
            </AppButton>
          </div>
          <p class="row__sub">{{ metricsText(row) }}</p>
        </li>
      </ul>
      <EmptyState
        v-else
        title="还没有可回流的数字"
        hint="回流时刻表在发布成功那一刻就写好了（T+1h 起，依次 1/6/24/72 小时）。到点之后后台自己会去采；想现在就要，按右上角「跑一轮」。"
      />
    </PanelCard>

    <!-- ⑨ 待人工：唯一一个"要求人做决定"的区块 -->
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

    <!-- ⑩ 失败 / 已取消（不在规格的七区块里，但藏起来更糟） -->
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

    <!-- ⑪ 定时计划（T5.6）：能建、能改、能停、能立刻跑一次 -->
    <PanelCard
      title="定时计划"
      :subtitle="`${scheduleSubtitle}；后台每 ${scheduleTickSec} 秒拍一次`"
    >
      <template #actions>
        <AppButton size="sm" :loading="schedules.loading" @click="schedules.refresh()">
          刷新
        </AppButton>
      </template>

      <p v-if="schedules.error" class="alert alert--error">{{ schedules.error }}</p>
      <p v-else-if="schedules.notice" class="alert alert--ok">{{ schedules.notice }}</p>

      <ul v-if="scheduleItems.length > 0" class="rows">
        <li v-for="row in scheduleItems" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot
              :tone="scheduleResultTone(row.last_result)"
              :label="scheduleResultLabel(row.last_result)"
            />
            <span class="row__title">{{ scheduleModeLabel(row.mode) }}</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ row.platforms.join(" / ") }}</span>
            <span v-if="!row.enabled" class="row__note">已停用</span>
          </div>
          <p class="row__sub">{{ scheduleSummary(row) }}</p>
          <p class="row__sub">
            下一次 <span class="mono">{{ nextRunText(row) }}</span> ·
            上次 <span class="mono">{{ scheduleStampText(row.last_run_at) }}</span> ·
            已跑 {{ row.run_count }} 次
            <template v-if="row.fail_streak > 0">
              · <strong>连续失败 {{ row.fail_streak }} 次</strong>
            </template>
          </p>
          <p v-if="row.task_id" class="row__sub">
            钉住任务 <span class="mono">{{ row.task_id }}</span>
          </p>
          <div class="row__actions">
            <AppButton size="sm" :disabled="scheduleBusy(row.id)" @click="schedules.run(row)">
              立即执行一次
            </AppButton>
            <AppButton size="sm" :disabled="scheduleBusy(row.id)" @click="schedules.toggle(row)">
              {{ row.enabled ? "停用" : "启用" }}
            </AppButton>
            <AppButton
              size="sm"
              variant="danger"
              :disabled="scheduleBusy(row.id)"
              @click="schedules.remove(row)"
            >
              删除
            </AppButton>
          </div>
        </li>
      </ul>
      <EmptyState
        v-else
        title="还没有定时计划"
        hint="下面那行就是建一条的地方。到点之后它只做一件事：往发布池投作业 —— 发不发得出去仍由发布池与总开关说了算。"
      />

      <div class="form">
        <label class="f">
          <span class="f__key">平台</span>
          <select class="field" :value="scheduleDraft.platform" @change="onDraftPlatform">
            <option v-for="option in schedules.selectable" :key="option.code" :value="option.code">
              {{ option.code }}{{ option.rehearsal ? "（本地演练台）" : "" }}
            </option>
          </select>
        </label>
        <label class="f">
          <span class="f__key">模式</span>
          <select
            class="field"
            :value="scheduleDraft.mode"
            @change="onDraft($event, 'mode')"
          >
            <option v-for="mode in SCHEDULE_MODES" :key="mode" :value="mode">
              {{ scheduleModeLabel(mode) }}
            </option>
          </select>
        </label>

        <template v-if="scheduleDraft.mode === 'daily_window'">
          <label class="f">
            <span class="f__key">窗口开始</span>
            <input
              class="field mono"
              type="time"
              :value="scheduleDraft.windowStart"
              @change="onDraft($event, 'windowStart')"
            />
          </label>
          <label class="f">
            <span class="f__key">窗口结束</span>
            <input
              class="field mono"
              type="time"
              :value="scheduleDraft.windowEnd"
              @change="onDraft($event, 'windowEnd')"
            />
          </label>
        </template>

        <label v-else-if="scheduleDraft.mode === 'at_time'" class="f">
          <span class="f__key">时刻</span>
          <input
            class="field mono"
            type="datetime-local"
            :value="scheduleDraft.atTime"
            @change="onDraft($event, 'atTime')"
          />
        </label>

        <label v-else class="f">
          <span class="f__key">间隔（小时）</span>
          <input
            class="field mono"
            type="number"
            min="1"
            max="720"
            :value="scheduleDraft.intervalHours"
            @change="onDraft($event, 'intervalHours')"
          />
        </label>

        <label class="f">
          <span class="f__key">抖动（分钟）</span>
          <input
            class="field mono"
            type="number"
            min="0"
            :max="MAX_JITTER_MIN"
            :value="scheduleDraft.jitterMin"
            @change="onDraft($event, 'jitterMin')"
          />
        </label>

        <label class="f f--task">
          <span class="f__key">任务号（可空）</span>
          <input
            class="field mono"
            type="text"
            placeholder="空 = 到点从待发布池挑一条"
            :value="scheduleDraft.taskId"
            @change="onDraft($event, 'taskId')"
          />
        </label>

        <label class="check">
          <input
            type="checkbox"
            :checked="scheduleDraft.enabled"
            @change="onDraft($event, 'enabled')"
          />
          <span>建好就启用</span>
        </label>

        <AppButton
          size="sm"
          variant="primary"
          :loading="schedules.saving"
          :disabled="schedules.selectable.length === 0"
          @click="schedules.create()"
        >
          建这条计划
        </AppButton>
      </div>

      <p class="hint">
        到点只做一件事：<strong>往发布池投作业</strong>（认领 / 重试 / 转人工全走队列那一套）。
        定时**不等于**免限频 —— 到点撞上额度就用 <span class="mono">skipped_ratelimit</span>
        顺延，那不是失败。窗口内取的时刻是 <span class="mono">HMAC(计划号, 日期)</span>
        算出来的：同一天怎么算都是同一个时刻，重启也不会漂。总开关关着时它照拍照记
        <span class="mono">skipped_disabled</span>，好让你确认调度器是活的。
      </p>
    </PanelCard>

    <!-- ⑫ 报告（T5.7）：看 / 采纳 / 导出 / 周期可编辑 -->
    <PanelCard title="报告" :subtitle="reportSubtitle">
      <template #actions>
        <AppButton size="sm" :loading="reports.loading" @click="reports.refresh()">刷新</AppButton>
      </template>

      <p v-if="reports.error" class="alert alert--error">{{ reports.error }}</p>
      <p v-else-if="reports.notice" class="alert alert--ok">{{ reports.notice }}</p>

      <div class="form">
        <label class="f">
          <span class="f__key">周期</span>
          <select class="field" :value="reports.period" @change="onReportPeriod">
            <option v-for="item in reportPeriods" :key="item" :value="item">
              {{ periodLabel(item) }}
            </option>
          </select>
        </label>
        <AppButton
          size="sm"
          variant="primary"
          :loading="reports.generating"
          @click="reports.generate()"
        >
          现在生成一份
        </AppButton>
        <span class="row__note">同一个窗口已经有一份 ⇒ 直接给你那一份，不会重复插入</span>
      </div>

      <ul v-if="reportItems.length > 0" class="rows">
        <li
          v-for="row in reportItems"
          :key="row.id"
          class="row row--stack"
          :class="{ 'row--picked': row.id === reports.selectedId }"
        >
          <div class="row__head">
            <span class="row__title">{{ periodLabel(row.period) }}</span>
            <span class="row__mono mono">{{ windowText(row.start_date, row.end_date) }}</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ triggerLabel(row.trigger) }}</span>
            <span class="row__note">{{ reportStampText(row.generated_at) }}</span>
          </div>
          <p class="row__sub">{{ summaryText(row) }}</p>
          <div class="row__actions">
            <AppButton size="sm" @click="reports.select(row.id)">看这一份</AppButton>
          </div>
        </li>
      </ul>
      <EmptyState
        v-else
        title="还没有报告"
        hint="上面那行就是生成一份的地方。报告只统计**已发布**的记录 —— 还没投出去的任务不会出现在任何数字里，这是故意的：把未发布的算进去，中位数就成了自己编的。"
      />

      <div v-if="reportDetail" class="report">
        <div class="row__head">
          <span class="row__title">
            {{ periodLabel(reportDetail.period) }} ·
            <span class="mono">{{ windowText(reportDetail.start_date, reportDetail.end_date) }}</span>
          </span>
          <span class="row__spacer" />
          <span class="row__mono mono">
            任务 {{ reportDetail.task_count }} · 发布 {{ reportDetail.publish_count }} ·
            中位播放 {{ countText(reportDetail.median_views) }} ·
            建议 {{ reportDetail.insights_count }} 条
          </span>
        </div>
        <div class="row__actions">
          <AppButton
            size="sm"
            :disabled="reportBusy(reportDetail.id)"
            @click="reports.download('md')"
          >
            导出 Markdown
          </AppButton>
          <AppButton
            size="sm"
            :disabled="reportBusy(reportDetail.id)"
            @click="reports.download('csv')"
          >
            导出 CSV
          </AppButton>
          <span v-if="reportArtifact !== ''" class="row__note mono">落盘 {{ reportArtifact }}</span>
        </div>

        <ul v-if="reportInsights.length > 0" class="rows">
          <li v-for="(insight, index) in reportInsights" :key="index" class="row row--stack">
            <div class="row__head">
              <StatusDot
                :tone="confidenceTone(insight.confidence)"
                :label="`置信度${confidenceLabel(insight.confidence)}`"
              />
              <span class="row__title">{{ insightKindLabel(insight.kind) }}</span>
              <span class="row__spacer" />
              <span class="row__mono mono">{{ insightEvidenceText(insight) }}</span>
            </div>
            <p class="row__sub">{{ insight.statement }}</p>
            <p v-if="insightIsShaky(insight)" class="alert alert--warn">
              样本不足，仅供参考 —— 这条结论的证据不到 10 条，别照它改风格。后端也把它写进了上面那句话。
            </p>
            <p class="row__sub">建议动作：{{ insight.suggested_action }}</p>
            <div class="row__actions">
              <AppButton
                size="sm"
                :variant="insight.applied ? 'ghost' : 'primary'"
                :loading="reports.applyingIndex === index"
                :disabled="insight.applied"
                @click="reports.apply(index)"
              >
                {{ insight.applied ? "已采纳（写进人物偏好）" : "采纳 ⇒ 写进人物偏好" }}
              </AppButton>
            </div>
          </li>
        </ul>
        <EmptyState
          v-else
          title="这份报告没有建议"
          hint="只有「有对比且相对差 ≥10%」的维度才会出一条建议。一条建议都没有，说明这个窗口里的数据还看不出差别 —— 那不是坏消息。"
        />

        <details class="more">
          <summary>报告正文（Markdown 原文）</summary>
          <pre class="md mono">{{ reportDetail.summary_md }}</pre>
        </details>
      </div>

      <h4 class="sub">报告周期</h4>
      <p class="row__sub">{{ reportScheduleSubtitle }}</p>

      <ul v-if="reportScheduleItems.length > 0" class="rows">
        <li v-for="row in reportScheduleItems" :key="row.id" class="row row--stack">
          <div class="row__head">
            <StatusDot
              :tone="reportResultTone(row.last_result)"
              :label="reportResultLabel(row.last_result)"
            />
            <span class="row__title">{{ periodLabel(row.period) }}</span>
            <span v-if="row.is_builtin" class="row__note">出厂自带</span>
            <span v-if="!row.enabled" class="row__note">已停用</span>
            <span class="row__spacer" />
            <span class="row__mono mono">{{ row.tz }}</span>
          </div>
          <p class="row__sub">{{ reportScheduleSummary(row) }}</p>
          <p class="row__sub">
            下一次 <span class="mono">{{ reportNextRunText(row) }}</span> ·
            上次 <span class="mono">{{ reportStampText(row.last_run_at) }}</span>
            <template v-if="row.fail_streak > 0">
              · <strong>连续失败 {{ row.fail_streak }} 次</strong>
            </template>
          </p>
          <p class="row__sub">
            正文区块：<span class="mono">{{ row.include.map(includeLabel).join(" / ") }}</span>
          </p>
          <div class="row__actions">
            <AppButton size="sm" :disabled="reportBusy(row.id)" @click="reports.runSchedule(row)">
              立即生成一次
            </AppButton>
            <AppButton size="sm" :disabled="reportBusy(row.id)" @click="reports.openEdit(row)">
              编辑
            </AppButton>
            <AppButton size="sm" :disabled="reportBusy(row.id)" @click="reports.toggleSchedule(row)">
              {{ row.enabled ? "停用" : "启用" }}
            </AppButton>
            <AppButton
              size="sm"
              variant="danger"
              :disabled="reportBusy(row.id) || row.is_builtin"
              @click="reports.removeSchedule(row)"
            >
              删除
            </AppButton>
          </div>
        </li>
      </ul>
      <EmptyState
        v-else
        title="还没有报告周期"
        hint="下面那张表就是建一条的地方。出厂自带的三条（周报 / 月报 / 日报）已经在库里，看不到就点一下「刷新」。"
      />

      <div class="form">
        <label class="f">
          <span class="f__key">周期</span>
          <select class="field" :value="reportDraft.period" @change="onReportPeriodPick">
            <option v-for="item in reportPeriods" :key="item" :value="item">
              {{ periodLabel(item) }}
            </option>
          </select>
        </label>

        <label v-if="reportDraft.period === 'weekly'" class="f">
          <span class="f__key">星期几</span>
          <select
            class="field"
            :value="String(reportDraft.weekday)"
            @change="onReportDraft($event, 'weekday')"
          >
            <option v-for="(day, index) in WEEKDAYS" :key="day" :value="String(index)">
              {{ day }}
            </option>
          </select>
        </label>

        <label v-else-if="reportDraft.period === 'monthly'" class="f">
          <span class="f__key">每月第几天</span>
          <input
            class="field mono"
            type="number"
            min="1"
            max="28"
            :value="reportDraft.dayOfMonth"
            @change="onReportDraft($event, 'dayOfMonth')"
          />
        </label>

        <label class="f">
          <span class="f__key">生成时刻</span>
          <input
            class="field mono"
            type="time"
            :value="reportDraft.atTime"
            @change="onReportDraft($event, 'atTime')"
          />
        </label>

        <label class="f">
          <span class="f__key">回看天数</span>
          <input
            class="field mono"
            type="number"
            min="1"
            max="366"
            :value="reportDraft.lookbackDays"
            @change="onReportDraft($event, 'lookbackDays')"
          />
        </label>

        <label class="check">
          <input
            type="checkbox"
            :checked="reportDraft.enabled"
            @change="onReportDraft($event, 'enabled')"
          />
          <span>{{ reportEditingId === null ? "建好就启用" : "保持启用" }}</span>
        </label>
      </div>

      <ul class="checks">
        <li v-for="kind in INCLUDE_KINDS" :key="kind" class="checks__item">
          <label class="check">
            <input
              type="checkbox"
              :checked="reportDraft.include.includes(kind)"
              @change="reports.toggleInclude(kind)"
            />
            <span>{{ includeLabel(kind) }}<span class="row__mono mono"> · {{ kind }}</span></span>
          </label>
        </li>
      </ul>

      <div class="row__actions">
        <AppButton
          size="sm"
          variant="primary"
          :loading="reports.saving"
          @click="reports.saveDraft()"
        >
          {{ reportEditingId === null ? "建这条周期" : "保存改动" }}
        </AppButton>
        <AppButton v-if="reportEditingId !== null" size="sm" @click="reports.cancelEdit()">
          取消编辑
        </AppButton>
      </div>

      <p class="hint">
        到点只做一件事：<strong>把上一段窗口的数字算成一份报告</strong>。窗口右端是
        <strong>触发日的前一天</strong>（今天还没过完，把它算进去只会让每份报告都偏低），
        左端由「回看天数」倒推。同一个窗口不会生成第二份 —— 想重算就换一个窗口。
        采纳一条建议会写进<strong>人物偏好</strong>（<span class="mono">style_hint</span>），
        下一轮 Planner 就会读到它；<span class="mono">content_directions</span>
        是历史批次，改它对下一轮没有任何影响，所以采纳**不动它**。出厂自带的三条周期
        只能停用、不能删（删掉之后没人知道它们本来是什么）。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
/* 账号细分（T5.8）：挂在平台勾选框下面，缩进一格 —— 它属于**上一个**平台。 */
.accounts {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-left: var(--space-5);
  margin-top: var(--space-1);
}

.accounts__key {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.checks--sub {
  margin-top: 0;
}

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

.checks {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-3);
}

.checks__item {
  list-style: none;
}

.check {
  display: inline-flex;
  gap: var(--space-2);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.check input:disabled + span {
  /* 点不动的那些（二选平台 / 没有账号）灰掉 —— 但它**还在屏上**，
     因为"这个平台存在，只是现在不能投"本身是有用的信息。 */
  color: var(--text-muted);
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

.row--picked {
  border-left: 2px solid var(--ok);
  padding-left: var(--space-2);
}

.report {
  padding: var(--space-3);
  margin-top: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.sub {
  margin-top: var(--space-4);
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.md {
  max-height: 420px;
  padding: var(--space-2) var(--space-3);
  margin-top: var(--space-2);
  overflow: auto;
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.7;
  white-space: pre-wrap;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
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

.alert--warn {
  color: var(--warn);
  background: var(--warn-soft);
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