<script setup lang="ts">
// 人物库面板（T4.13 · §02.4 / §04.1.6 / §04.5.8）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 现在用的是谁、哪来的、坏没坏？—— id / 名称 / 版本 / sha256 / 加载时刻 / 来源；
// ② 库里还有哪些人、哪个不能用？—— 无效条目**直接写原因**，不从列表里消失；
// ③ 改完怎么存、改错了怎么退？—— 表单（保存前强制校验）+ 备份列表（一键回滚）；
// ④ 换个人上？—— 一键切换（旧版自动备份）。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **只影响后续稿件**：已入队 / 在跑的任务不中断、不重写。不写这一句，用户会以为
//    切完人物"当前那条稿子也变了"，然后盯着一条永远不会变的稿子等。
// 2. **多进程无 IPC**：面板显示的是**本进程**（api）已发现的状态。切换之后 worker
//    要等下一次 `current()` 才发现（每个单元开始前取一次）—— 这是 T1.2 的既定设计，
//    不是故障。不说清楚，"我切了人物，在跑的那条还是旧口吻"会被当成 bug 查半天。
// 3. **禁区词会经规则通道全量扫描**（E7 唯一人工必填项）：改禁区等于改一道**硬门禁**，
//    命中即 `block`、不重写、不进评分。
//
// 为什么"另存为"和"保存"是两个按钮
// --------------------------------
// 保存改的是**当前激活的那一份**；另存为是把当前内容**存进人物库**。把两者合成一个
// 按钮，就会出现"我只是想存一份，结果把线上人物的口吻也改了"。
//
// 为什么表单是 `:value` + `@input` 而不是 `v-model`
// ------------------------------------------------
// `draft` 可能是 `null`（激活文件坏了、还没有底稿）。`v-model` 在可空对象上要么报
// 类型错、要么被迫写一堆 `!`；显式的事件处理把"没有草稿就什么都不做"写成一行判断。

import { computed, onMounted, ref } from "vue";

import type { PersonaBackup, PersonaLibraryItem } from "@/api/endpoints/persona";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import {
  backupTone,
  describeBackup,
  libraryState,
  libraryTone,
  usePersonaStore,
} from "@/stores/persona";

const personas = usePersonaStore();

// 进面板即接线：订阅 WS 的 `system` 通道（只认 `system.persona_changed`）+ 一次全量拉取。
onMounted(() => {
  personas.start();
});

const saveAsId = ref("");
const saveAsOverwrite = ref(false);

type TextKey = "name" | "role_desc" | "tone" | "audience" | "style_hint";
type ListKey = "catchphrases" | "forbidden";
type NumberKey = "target_chars_min" | "target_chars_max" | "max_duration_ms";

const form = computed(() => personas.draft);
const errors = computed(() => personas.visibleFieldErrors);

/** 时长上限在表单上按**秒**显示（人不会用毫秒想事情），存回去仍是毫秒。 */
const durationSec = computed(() =>
  form.value === null ? 0 : Math.round(form.value.max_duration_ms / 1000),
);

function onText(key: TextKey, event: Event): void {
  if (form.value === null) return;
  form.value[key] = (event.target as HTMLInputElement | HTMLTextAreaElement).value;
}

function onNumber(key: NumberKey, event: Event): void {
  if (form.value === null) return;
  const raw = (event.target as HTMLInputElement).value.trim();
  const parsed = Number.parseInt(raw, 10);
  // 输入框清空时**保留原值**：让"空"变成 NaN 存进草稿，只会在提交时给出一条
  // 看不懂的 422，而用户真正想做的往往是"全选重打"。
  if (Number.isNaN(parsed)) return;
  form.value[key] = parsed;
}

function onDurationSec(event: Event): void {
  if (form.value === null) return;
  const raw = (event.target as HTMLInputElement).value.trim();
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed)) return;
  form.value.max_duration_ms = parsed * 1000;
}

function onLines(key: ListKey, event: Event): void {
  if (form.value === null) return;
  const lines = (event.target as HTMLTextAreaElement).value.split("\n").map((line) => line.trim());
  // 只去掉**尾部**空行（"打完一行顺手回车"不该报错）；中间的空行留着，它确实该被修掉。
  while (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  form.value[key] = lines;
}

function linesText(items: readonly string[]): string {
  return items.join("\n");
}

function onActivate(item: PersonaLibraryItem): void {
  if (item.active) return;
  void personas.activate(item.persona_id);
}

function onRollback(backup: PersonaBackup): void {
  void personas.rollback(backup.name);
}

function onSaveAs(): void {
  const id = saveAsId.value.trim();
  if (id === "") return;
  void personas.saveAs(id, saveAsOverwrite.value).then((ok) => {
    if (ok) {
      saveAsId.value = "";
      saveAsOverwrite.value = false;
    }
  });
}
</script>

<template>
  <div class="personas">
    <p v-if="personas.activeError" class="alert alert--error">
      当前人物读不出来：{{ personas.activeError }}（人物库与备份照常可用 —— 用「切换」或「回滚」修回来）
    </p>

    <p v-if="personas.stale" class="alert alert--warn">
      `config/persona.yaml` 现在是坏的，下面是**上一份可用的人物**：
      {{ personas.active?.last_error }}。5 个进程仍在用这一份跑，改好文件或回滚即可。
    </p>

    <p v-if="personas.draftStale" class="alert alert--warn">
      底稿在你编辑期间变过（有人改了人物，或别处切了一次）。你手里的改动**没有被丢掉**，
      保存时会覆盖对应字段；想放弃改动就点「放弃编辑」。
    </p>

    <p v-if="personas.loadError" class="alert alert--error">
      人物库拉取失败：{{ personas.loadError }}（下面显示的是上一次拿到的内容）
    </p>

    <p v-if="personas.error" class="alert alert--error">动作失败：{{ personas.error }}</p>
    <p v-else-if="personas.notice" class="alert alert--ok">{{ personas.notice }}</p>

    <PanelCard
      title="当前人物"
      :subtitle="personas.active ? `${personas.active.persona_id} · v${personas.active.version}` : '还没有可用的人物'"
    >
      <template #actions>
        <AppButton size="sm" :loading="personas.loading" @click="personas.refresh()">刷新</AppButton>
      </template>

      <EmptyState
        v-if="!personas.active"
        title="没有可用的人物"
        hint="`config/persona.yaml` 是唯一人工必填项（§02.4）。从人物库切一份过来，或把文件改好。"
      />

      <div v-else class="rows">
        <div class="row">
          <span class="row__key">名称</span>
          <span class="row__val">
            {{ personas.active.name }}
            <span class="mono muted">{{ personas.active.persona_id }}</span>
          </span>
        </div>
        <div class="row">
          <span class="row__key">来源</span>
          <span class="row__val mono">
            {{ personas.active.source }} · {{ personas.active.path }}
          </span>
        </div>
        <div class="row">
          <span class="row__key">版本</span>
          <span class="row__val mono">
            v{{ personas.active.version }} · sha256 {{ personas.active.sha256.slice(0, 12) }} ·
            加载于 {{ personas.active.loaded_at }}
          </span>
        </div>
        <div class="row">
          <span class="row__key">状态</span>
          <span class="row__val">
            <StatusDot
              :tone="personas.stale ? 'error' : 'ok'"
              :label="personas.stale ? '磁盘上那份已损坏（在用上一份好的）' : '正常'"
            />
          </span>
        </div>
      </div>

      <p class="hint">
        改完 / 切完**只影响后续稿件**：已入队、正在跑的任务不中断、不重写。
        面板显示的是**本进程**（api）已发现的状态；worker 会在下一个单元开始前取到新的那一份。
      </p>
    </PanelCard>

    <PanelCard
      title="编辑"
      :subtitle="
        form === null
          ? '没有可编辑的底稿'
          : personas.dirty
            ? '有未保存的改动'
            : '与服务端一致'
      "
    >
      <template #actions>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="!personas.canSave"
          :loading="personas.busy"
          @click="personas.save()"
        >
          保存
        </AppButton>
        <AppButton size="sm" :disabled="!personas.dirty" @click="personas.resetDraft()">
          放弃编辑
        </AppButton>
      </template>

      <EmptyState
        v-if="form === null"
        title="没有可编辑的人物"
        hint="先把 `config/persona.yaml` 修好，或从人物库切一份过来。"
      />

      <div v-else class="form">
        <label class="f f--wide">
          <span class="f__key">名称</span>
          <input class="field" :value="form.name" @input="onText('name', $event)" />
          <span v-if="errors.name" class="f__err">{{ errors.name }}</span>
        </label>

        <label class="f f--wide">
          <span class="f__key">口吻</span>
          <input class="field" :value="form.tone" @input="onText('tone', $event)" />
          <span v-if="errors.tone" class="f__err">{{ errors.tone }}</span>
        </label>

        <label class="f f--wide">
          <span class="f__key">受众</span>
          <input class="field" :value="form.audience" @input="onText('audience', $event)" />
          <span v-if="errors.audience" class="f__err">{{ errors.audience }}</span>
        </label>

        <label class="f f--full">
          <span class="f__key">人设</span>
          <textarea class="area" rows="4" :value="form.role_desc" @input="onText('role_desc', $event)" />
          <span v-if="errors.role_desc" class="f__err">{{ errors.role_desc }}</span>
        </label>

        <label class="f">
          <span class="f__key">口癖（一行一条）</span>
          <textarea
            class="area"
            rows="6"
            :value="linesText(form.catchphrases)"
            @input="onLines('catchphrases', $event)"
          />
          <span v-if="errors.catchphrases" class="f__err">{{ errors.catchphrases }}</span>
        </label>

        <label class="f">
          <span class="f__key">禁区（一行一条）</span>
          <textarea
            class="area"
            rows="6"
            :value="linesText(form.forbidden)"
            @input="onLines('forbidden', $event)"
          />
          <span v-if="errors.forbidden" class="f__err">{{ errors.forbidden }}</span>
        </label>

        <label class="f f--full">
          <span class="f__key">风格提示（选填）</span>
          <textarea class="area" rows="3" :value="form.style_hint" @input="onText('style_hint', $event)" />
        </label>

        <div class="f f--full f--row">
          <label class="f f--num">
            <span class="f__key">篇幅下限（字）</span>
            <input
              class="field mono"
              type="number"
              :value="form.target_chars_min"
              @input="onNumber('target_chars_min', $event)"
            />
            <span v-if="errors.target_chars_min" class="f__err">{{ errors.target_chars_min }}</span>
          </label>

          <label class="f f--num">
            <span class="f__key">篇幅上限（字）</span>
            <input
              class="field mono"
              type="number"
              :value="form.target_chars_max"
              @input="onNumber('target_chars_max', $event)"
            />
            <span v-if="errors.target_chars_max" class="f__err">{{ errors.target_chars_max }}</span>
          </label>

          <label class="f f--num">
            <span class="f__key">时长上限（秒）</span>
            <input
              class="field mono"
              type="number"
              :value="durationSec"
              @input="onDurationSec($event)"
            />
            <span v-if="errors.max_duration_ms" class="f__err">{{ errors.max_duration_ms }}</span>
          </label>
        </div>
      </div>

      <p class="hint">
        **禁区词会经规则通道全量扫描**（忽略空白差异）：命中即拦稿，不重写、不进评分。
        篇幅与时长上限是写稿/配音的**硬阈值**，改了立刻对后续稿件生效。
      </p>

      <div class="saveas">
        <input
          v-model="saveAsId"
          class="field field--wide mono"
          placeholder="另存为人物库 id（小写字母 / 数字 / 下划线）"
        />
        <label class="check">
          <input v-model="saveAsOverwrite" type="checkbox" />
          <span>同名则覆盖</span>
        </label>
        <AppButton size="sm" :disabled="saveAsId.trim() === '' || personas.busy" @click="onSaveAs">
          另存为
        </AppButton>
      </div>
    </PanelCard>

    <PanelCard title="人物库" :subtitle="personas.libraryDir">
      <EmptyState
        v-if="personas.library.length === 0"
        title="人物库是空的"
        hint="`config/personas/<id>.yaml` 放备选人物；把当前这份「另存为」进去就有了。"
      />

      <ul v-else class="list">
        <li v-for="item in personas.library" :key="item.persona_id" class="item">
          <div class="item__main">
            <span class="item__title">{{ item.name || item.persona_id }}</span>
            <span class="mono muted">{{ item.persona_id }}</span>
            <span v-if="item.error" class="item__err">{{ item.error }}</span>
            <span v-else class="item__sub">{{ item.tone }} · {{ item.audience }}</span>
          </div>
          <StatusDot :tone="libraryTone(item)" :label="libraryState(item)" />
          <AppButton
            size="sm"
            :disabled="item.active || !item.valid || personas.busy"
            :title="item.active ? '已经是当前人物' : item.valid ? '切换并自动备份旧版' : '这条无效，先修文件'"
            @click="onActivate(item)"
          >
            切换
          </AppButton>
        </li>
      </ul>
    </PanelCard>

    <PanelCard
      title="备份与回滚"
      :subtitle="`${personas.backupDir}（最多显示最近 ${personas.snapshot?.backup_page ?? 0} 份）`"
    >
      <EmptyState
        v-if="personas.backups.length === 0"
        title="还没有备份"
        hint="每次保存 / 切换 / 回滚前，旧版都会自动备份到这里 —— 这就是「改错了能退回来」的那条退路。"
      />

      <ul v-else class="list">
        <li v-for="backup in personas.backups" :key="backup.name" class="item">
          <div class="item__main">
            <span class="item__title mono">{{ backup.name }}</span>
            <span class="item__sub">{{ describeBackup(backup) }}</span>
            <span v-if="backup.error" class="item__err">{{ backup.error }}</span>
          </div>
          <StatusDot :tone="backupTone(backup)" :label="backup.valid ? '可回滚' : '已损坏'" />
          <AppButton
            size="sm"
            :disabled="!backup.valid || personas.busy"
            :title="backup.valid ? '回滚到这一份（当前这份也会先备份）' : '坏备份不能覆盖现在这份好的'"
            @click="onRollback(backup)"
          >
            回滚
          </AppButton>
        </li>
      </ul>

      <p class="hint">
        回滚前会把**当前这一份**也备份一次 ⇒ 回滚错了可以再滚回来。
        坏备份照样列在这里（不会被静默跳过），但它不能覆盖现在这份好的。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.personas {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.alert {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  border-color: var(--error);
}

.alert--warn {
  color: var(--warn);
  border-color: var(--warn);
}

.alert--ok {
  color: var(--ok);
  border-color: var(--ok);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.row {
  display: flex;
  gap: var(--space-3);
  align-items: baseline;
  font-size: var(--text-sm);
}

.row__key {
  flex: 0 0 64px;
  color: var(--text-muted);
}

.row__val {
  min-width: 0;
  color: var(--text-secondary);
  word-break: break-all;
}

.form {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.f {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.f--wide {
  flex: 1 1 220px;
}

.f--full {
  flex: 1 1 100%;
}

.f--row {
  flex-direction: row;
  gap: var(--space-4);
}

.f--num {
  flex: 0 0 140px;
}

.f__key {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.f__err {
  color: var(--error);
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

.field--wide {
  flex: 1;
  min-width: 200px;
}

.area {
  width: 100%;
  padding: var(--space-2);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-xs);
  resize: vertical;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.saveas {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  margin-top: var(--space-3);
}

.check {
  display: inline-flex;
  gap: var(--space-1);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: 0;
  margin: 0;
  list-style: none;
}

.item {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.item__main {
  display: flex;
  flex: 1;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
  min-width: 0;
}

.item__title {
  color: var(--text-primary);
  font-size: var(--text-md);
}

.item__sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.item__err {
  color: var(--error);
  font-size: var(--text-xs);
}

.muted {
  color: var(--text-muted);
}
</style>
