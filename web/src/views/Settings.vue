<script setup lang="ts">
// 设置面板（T6.1）—— LLM 通道与密钥的界面化配置。
//
// 这一屏回答四个问题，多一个都不放：
// ① 现在配了没有、哪来的？—— 密钥状态（掩码 / 来源 / 文件路径 / 是否被环境变量盖住）；
// ② 有哪几条通道、哪条能用、缺什么？—— 通道卡片（缺密钥的**直接写缺什么**）；
// ③ 每个 Agent 走哪条？—— 路由表（只读：改它属于 `config/llm.yaml`）；
// ④ 填完真的通吗？—— 「测试连接」（只发只读 GET，不产生一次计费调用）；
// ⑤ 用哪个模型？—— 通道卡片上的模型名 / base_url（写回 `config/llm.yaml`）。
//
// 为什么模型名要能在面板上改
// --------------------------
// "这个月用哪个模型"是会变的（换服务商、换档位、临时降本），而 `llm.yaml` 是**入库**的
// 基线配置。不能改的话，用户只能手改 YAML —— 而那份文件的注释正是"为什么这么配"的
// 唯一记录，手改迟早改坏。后端按行改写，段外的 routing / budget / 注释一个字节都不碰。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **保存即生效**，不需要重启 API 与 4 个 worker（网关每次调用现取一次密钥）。
//    不说这一句，用户会去重启服务，然后发现"重启完还得再填一次"（其实不用）。
// 2. **环境变量优先**。环境变量占着位置时，这里存的这份**不生效** —— 面板标黄并说明
//    去哪儿改，否则用户会以为这个面板坏了。
// 3. **明文不回显**。面板永远只显示掩码；要看完整的就去环境变量 / 文件里看。
//    这是刻意做的取舍：能显示明文的面板，等于给"任何能打开页面的人"发了一把钥匙。
//
// 为什么输入框是 `:value` + `@input` 而不是 `v-model`
// ------------------------------------------------
// 与人物库面板同一条：提交成功后要**主动清空**输入框（那把 Key 已经存进去了，
// 留在输入框里既没必要、又会随截图 / 录屏一起漏出去）。显式事件更好控制这件事。

import { computed, onMounted, ref, watch } from "vue";

import type { LlmProfile } from "@/api/endpoints/settings";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import {
  keyInputProblem,
  keyTone,
  probeSummary,
  probeTone,
  profileTone,
  useSettingsStore,
} from "@/stores/settings";

const settings = useSettingsStore();

onMounted(() => {
  void settings.load();
});

const draft = ref("");
const reason = ref("");

const localProblem = computed(() => keyInputProblem(draft.value, settings.limits));
const canSave = computed(
  () => draft.value.trim() !== "" && localProblem.value === null && !settings.saving,
);

function onInput(event: Event): void {
  draft.value = (event.target as HTMLInputElement).value;
}

function onReason(event: Event): void {
  reason.value = (event.target as HTMLInputElement).value;
}

async function onSave(): Promise<void> {
  const ok = await settings.saveKey(draft.value, reason.value.trim() || undefined);
  // 只有真存进去了才清空输入框：失败时留在那儿，用户改一个字就能重试。
  if (ok) {
    draft.value = "";
    reason.value = "";
  }
}

async function onClear(): Promise<void> {
  const ok = await settings.clearKey(reason.value.trim() || undefined);
  if (ok) {
    draft.value = "";
    reason.value = "";
  }
}

// ── 通道参数（模型名 / base_url）──────────────────────────────────────
//
// 草稿按通道名存：**用户正在敲的那一份不能被一次刷新冲掉**。所以只在"这条通道还没有
// 草稿"时才用服务端值初始化，其余情况以草稿为准（还原按钮显式覆盖）。

interface ProfileDraft {
  model: string;
  baseUrl: string;
}

const drafts = ref<Record<string, ProfileDraft>>({});

watch(
  () => settings.data?.profiles.map((item) => item.name).join("|") ?? "",
  () => {
    const next: Record<string, ProfileDraft> = { ...drafts.value };
    for (const item of settings.data?.profiles ?? []) {
      next[item.name] ??= { model: item.model, baseUrl: item.base_url };
    }
    drafts.value = next;
  },
  { immediate: true },
);

function draftOf(name: string): ProfileDraft {
  return drafts.value[name] ?? { model: "", baseUrl: "" };
}

function onProfileInput(name: string, key: keyof ProfileDraft, event: Event): void {
  const value = (event.target as HTMLInputElement).value;
  drafts.value = { ...drafts.value, [name]: { ...draftOf(name), [key]: value } };
}

/** 草稿与服务端当前值不一致 ⇒ 这行有未保存的改动（按钮据此点亮）。 */
function profileDirty(profile: LlmProfile): boolean {
  const draft = draftOf(profile.name);
  return draft.model.trim() !== profile.model || draft.baseUrl.trim() !== profile.base_url;
}

function profileReady(profile: LlmProfile): boolean {
  const draft = draftOf(profile.name);
  const filled = draft.model.trim() !== "" || draft.baseUrl.trim() !== "";
  return filled && profileDirty(profile) && settings.savingProfile === null;
}

/** 把草稿还原成服务端当前值（放弃这一行的改动）。 */
function restoreProfile(name: string): void {
  const item = settings.data?.profiles.find((candidate) => candidate.name === name);
  if (item === undefined) return;
  drafts.value = { ...drafts.value, [name]: { model: item.model, baseUrl: item.base_url } };
}

async function onSaveProfile(profile: LlmProfile): Promise<void> {
  const draft = draftOf(profile.name);
  const ok = await settings.saveProfile(profile.name, draft.model, draft.baseUrl);
  if (ok) restoreProfile(profile.name);
}
</script>

<template>
  <div class="settings">
    <PanelCard
      title="LLM 密钥"
      subtitle="填完立刻生效 —— 不需要重启 API 或 worker"
    >
      <template #actions>
        <AppButton size="sm" :loading="settings.loading" @click="settings.load()">
          刷新
        </AppButton>
      </template>

      <div class="key">
        <div class="key__status">
          <StatusDot :tone="keyTone(settings.key)" :label="settings.key?.source_label ?? '未知'" />
          <span class="mono key__masked">{{ settings.key?.masked_key ?? "（尚未配置）" }}</span>
          <span v-if="settings.key" class="key__path mono">{{ settings.key.path }}</span>
        </div>

        <p v-if="settings.envOverridden" class="key__warn">
          环境变量 <span class="mono">{{ settings.key?.env_var }}</span> 已设置，它优先于本文件；
          在这里保存的密钥要等那个环境变量清掉之后才生效。
        </p>

        <div class="key__form">
          <input
            class="key__input mono"
            type="password"
            autocomplete="off"
            spellcheck="false"
            placeholder="粘贴 API Key（形如 sk-...）"
            :value="draft"
            @input="onInput"
          />
          <AppButton variant="primary" :disabled="!canSave" :loading="settings.saving" @click="onSave">
            保存
          </AppButton>
          <AppButton
            variant="danger"
            :disabled="settings.key?.configured !== true || settings.saving"
            @click="onClear"
          >
            清除密钥
          </AppButton>
        </div>

        <p v-if="localProblem !== null" class="key__problem">{{ localProblem }}</p>

        <input
          class="key__reason"
          type="text"
          placeholder="换 Key 的理由（会进审计留痕，可留空）"
          :value="reason"
          @input="onReason"
        />

        <p v-if="settings.error !== null" class="key__error">{{ settings.error }}</p>
        <p v-else-if="settings.notice !== null" class="key__notice">{{ settings.notice }}</p>

        <ul v-if="(settings.data?.notes.length ?? 0) > 0" class="key__notes">
          <li v-for="note in settings.data?.notes ?? []" :key="note">{{ note }}</li>
        </ul>
      </div>
    </PanelCard>

    <PanelCard
      title="通道"
      :subtitle="`默认通道：${settings.data?.default_profile ?? '-'} · 改完立刻生效，不需要重启`"
    >
      <template #actions>
        <AppButton size="sm" :loading="settings.probing" @click="settings.runProbe()">
          测试连接
        </AppButton>
      </template>

      <EmptyState
        v-if="settings.data === null"
        title="还没有读到配置"
        hint="点右上角「刷新」重试；若是首次启动，先确认 config/llm.yaml 在位。"
      />
      <ul v-else class="profiles">
        <li v-for="profile in settings.data.profiles" :key="profile.name" class="profile">
          <div class="profile__head">
            <StatusDot :tone="profileTone(profile)" :label="profile.name" />
            <span class="profile__model mono">{{ profile.engine }} · {{ profile.model }}</span>
            <span v-if="profile.is_default" class="profile__badge">默认</span>
            <span class="profile__detail">{{ profile.detail }}</span>
          </div>
          <div class="profile__form">
            <input
              class="profile__field mono"
              type="text"
              spellcheck="false"
              placeholder="模型名"
              :value="draftOf(profile.name).model"
              @input="onProfileInput(profile.name, 'model', $event)"
            />
            <input
              class="profile__field profile__field--url mono"
              type="text"
              spellcheck="false"
              placeholder="base_url"
              :value="draftOf(profile.name).baseUrl"
              @input="onProfileInput(profile.name, 'baseUrl', $event)"
            />
            <AppButton
              size="sm"
              :disabled="!profileReady(profile)"
              :loading="settings.savingProfile === profile.name"
              @click="onSaveProfile(profile)"
            >
              保存
            </AppButton>
            <AppButton size="sm" :disabled="!profileDirty(profile)" @click="restoreProfile(profile.name)">
              还原
            </AppButton>
          </div>
        </li>
      </ul>
    </PanelCard>

    <PanelCard v-if="settings.probe !== null" title="探测结果" :subtitle="probeSummary(settings.probe)">
      <ul class="probes">
        <li v-for="row in settings.probe.rows" :key="row.profile" class="probe">
          <StatusDot :tone="probeTone(row)" :label="row.profile" />
          <span class="probe__status mono">{{ row.status }}</span>
          <span class="probe__detail">{{ row.detail }}</span>
        </li>
      </ul>
    </PanelCard>

    <PanelCard
      title="Agent → 通道路由"
      subtitle="只读 —— 改它属于 config/llm.yaml（改完热重载）"
    >
      <table v-if="settings.data !== null" class="routing">
        <thead>
          <tr>
            <th>Agent</th>
            <th>主通道</th>
            <th>兜底</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in settings.data.routing" :key="row.agent">
            <td class="mono">{{ row.agent }}</td>
            <td class="mono">{{ row.profile }}</td>
            <td class="mono">{{ row.fallback ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </PanelCard>
  </div>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.key {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.key__status {
  display: flex;
  gap: var(--space-3);
  align-items: center;
}

.key__masked {
  font-size: var(--text-md);
}

.key__path {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.key__warn {
  padding: var(--space-2) var(--space-3);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  background: var(--bg-raised);
  border-left: 2px solid var(--warn, var(--accent));
  border-radius: var(--radius-sm);
}

.key__form {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.key__input {
  flex: 1;
  min-width: 0;
  height: 28px;
  padding: 0 var(--space-3);
  color: var(--text-primary);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.key__reason {
  height: 28px;
  padding: 0 var(--space-3);
  color: var(--text-primary);
  font-size: var(--text-sm);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.key__problem,
.key__error {
  color: var(--error);
  font-size: var(--text-xs);
}

.key__notice {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.key__notes {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-left: var(--space-4);
  color: var(--text-muted);
  font-size: var(--text-xs);
  list-style: disc;
}

.profiles,
.probes {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.profile,
.probe {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
}

/* 通道卡片是**两行**的：上面一行说"现在是什么"，下面一行才是能改的输入框。
   挤成一行的话，长模型名会把输入框压到看不见。 */
.profile {
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
}

.profile__head {
  display: flex;
  gap: var(--space-3);
  align-items: center;
}

.profile__form {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.profile__field {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 var(--space-3);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-base, var(--bg-raised));
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.profile__field--url {
  flex: 1.4;
}

.profile__model {
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.profile__badge {
  padding: 0 var(--space-2);
  color: var(--accent);
  font-size: var(--text-xs);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
}

.profile__detail {
  flex: 1;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.profile__url {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.probe__status {
  font-size: var(--text-xs);
}

.probe__detail {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.routing {
  width: 100%;
  font-size: var(--text-sm);
  border-collapse: collapse;
}

.routing th {
  padding: var(--space-2);
  color: var(--text-muted);
  font-size: var(--text-xs);
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
}

.routing td {
  padding: var(--space-2);
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border-subtle);
}
</style>
