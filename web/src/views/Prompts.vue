<script setup lang="ts">
// 提示词面板（T6.2）—— 提示词的界面化编辑。
//
// 这一屏回答四个问题，多一个都不放：
// ① 有哪些提示词？—— 左列（名字 + 用途 + 是否被覆盖 + 当前版本）；
// ② 现在生效的是哪一份？—— 每个条目两段（system / user）的正文 + 「覆盖生效中」徽标；
// ③ 这段读起来不对，改成这样 —— textarea + 保存（**先校验、后落盘**）；
// ④ 还是退回仓库那份吧 —— 每段的「还原」。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **改的是覆盖，不是仓库文件**：写的每一份落 `data/prompts/<相对路径>`，
//    `prompts/` 里那份入库文件一个字节都不动（`prompts verify` 逐字校验它）。
//    不说这一句，用户会以为自己在改 git 里那份，然后 `git diff` 里什么都看不到。
// 2. **保存即生效**：API 与四个 worker 都读生效那一份 —— 不需要重启任何进程。
// 3. **版本跟着覆盖走**：`prompt_version`（落 `scripts.prompt_version` 的那个号）
//    会随覆盖变化，所以"这一版稿子是用哪份提示词写的"永远答得出来。
//
// 为什么"整段清空"会被拦
// ----------------------
// 清空一段 = 把那条纪律整个删掉，而它在面板上长得和"没改"几乎一样（一个空白框）。
// 后端回 422；这里在保存按钮上就先把它标出来，不必等一次往返。

import { computed, onMounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import { usePromptsStore } from "@/stores/prompts";
import type { PromptRole } from "@/api/endpoints/prompts";

const prompts = usePromptsStore();

/** 模板语法的示例（写成常量：直接塞进插值里，`}}` 会把插值提前关掉）。 */
const VAR_SYNTAX = "{{变量}}";

const subtitle = computed(() => {
  const parts = [`${prompts.entries.length} 个条目`];
  if (prompts.selected !== null) parts.push(`当前 ${prompts.selected.name}`);
  if (prompts.dirtyCount > 0) parts.push(`未保存 ${prompts.dirtyCount} 段`);
  return parts.join(" · ");
});

const canSave = computed(
  () => prompts.dirtyCount > 0 && prompts.saving === null && prompts.selected !== null,
);

function onInput(role: PromptRole, event: Event): void {
  const name = prompts.selected?.name;
  if (name === undefined) return;
  prompts.setDraft(name, role, (event.target as HTMLTextAreaElement).value);
}

async function onSave(): Promise<void> {
  const name = prompts.selected?.name;
  if (name === undefined) return;
  await prompts.save(name);
}

function onDrop(role: PromptRole): void {
  const name = prompts.selected?.name;
  if (name === undefined) return;
  prompts.dropDraft(name, role);
}

async function onRestore(role: PromptRole): Promise<void> {
  const name = prompts.selected?.name;
  if (name === undefined) return;
  await prompts.restore(name, role);
}

onMounted(() => {
  void prompts.load();
});
</script>

<template>
  <PanelCard fill dense title="提示词 · 运行期覆盖" :subtitle="subtitle">
    <template #actions>
      <AppButton size="sm" :loading="prompts.loading" @click="prompts.load()">刷新</AppButton>
    </template>

    <div class="wrap">
      <p v-if="prompts.error ?? prompts.loadError" class="notice notice--error">
        {{ prompts.error ?? prompts.loadError }}
      </p>
      <p v-else-if="prompts.notice" class="notice">{{ prompts.notice }}</p>
      <p v-else class="notice notice--muted">
        改的是覆盖文件（<span class="mono">{{ prompts.overrideDir ?? "data/prompts" }}</span>），
        <span class="mono">prompts/</span> 里那份入库文件一个字节都不动。保存即生效 ——
        API 与四个 worker 都读生效那一份，不需要重启；版本号跟着覆盖走。
      </p>

      <div class="split">
        <section class="list">
          <EmptyState
            v-if="prompts.entries.length === 0"
            title="还没拉到提示词"
            hint="点右上角「刷新」；条目登记在 prompts/manifest.yaml 里。"
          />
          <ul v-else class="list__items">
            <li
              v-for="entry in prompts.entries"
              :key="entry.name"
              class="item"
              :class="{ 'item--active': entry.name === prompts.selectedName }"
              @click="prompts.select(entry.name)"
            >
              <span class="item__row">
                <span class="item__name mono">{{ entry.name }}</span>
                <span v-if="entry.overridden" class="badge">已覆盖</span>
              </span>
              <span class="item__desc">{{ entry.description }}</span>
              <span class="item__ver mono">{{ entry.prompt_version }}</span>
            </li>
          </ul>
        </section>

        <section class="editor">
          <EmptyState
            v-if="prompts.selected === null"
            title="左边选一个提示词"
            hint="每个条目最多两段：system 是纪律，user 是这一次的输入模板。"
          />
          <template v-else>
            <header class="editor__head">
              <span class="editor__name mono">{{ prompts.selected.name }}</span>
              <span class="editor__ver mono">
                v{{ prompts.selected.version }} · {{ prompts.selected.prompt_version }}
              </span>
              <span class="editor__spacer" />
              <AppButton
                variant="primary"
                size="sm"
                :disabled="!canSave"
                :loading="prompts.saving === prompts.selected.name"
                @click="onSave()"
              >
                保存改动（{{ prompts.dirtyCount }}）
              </AppButton>
            </header>

            <p class="editor__vars mono">
              可用变量：{{ prompts.selected.variables.join(" · ") }}
            </p>
            <p class="editor__hint">
              只支持 <span class="mono">{{ VAR_SYNTAX }}</span> 替换；引用了清单之外的变量会在
              保存时被拦下（那种错只会在下一次生成时炸，而那时人早忘了自己改过什么）。
            </p>

            <article v-for="seg in prompts.segments" :key="seg.role" class="seg">
              <div class="seg__head">
                <span class="seg__role mono">{{ seg.role }}</span>
                <span class="seg__path mono">{{ seg.path }}</span>
                <span v-if="seg.overridden" class="badge">覆盖生效中</span>
                <span v-if="seg.dirty" class="seg__dirty">未保存</span>
                <span class="seg__spacer" />
                <AppButton size="sm" :disabled="!seg.dirty" @click="onDrop(seg.role)">
                  撤销改动
                </AppButton>
                <AppButton
                  variant="danger"
                  size="sm"
                  :disabled="!seg.overridden || prompts.restoring !== null"
                  :loading="prompts.restoring === `${prompts.selected.name}/${seg.role}`"
                  @click="onRestore(seg.role)"
                >
                  还原
                </AppButton>
              </div>
              <textarea
                class="seg__text mono"
                spellcheck="false"
                :value="seg.draft"
                @input="onInput(seg.role, $event)"
              />
            </article>
          </template>
        </section>
      </div>
    </div>
  </PanelCard>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  height: 100%;
  min-height: 0;
}

.notice {
  margin: 0;
  font-size: var(--text-xs);
}

.notice--error {
  color: var(--error);
}

.notice--muted {
  color: var(--text-muted);
}

.split {
  display: grid;
  flex: 1;
  grid-template-columns: minmax(200px, 260px) 1fr;
  gap: var(--space-2);
  min-height: 0;
}

.list,
.editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-height: 0;
  overflow-y: auto;
}

.list {
  padding-right: var(--space-2);
  border-right: 1px solid var(--border-subtle);
}

.list__items {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  cursor: pointer;
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.item:hover {
  background: var(--bg-hover);
}

.item--active {
  border-color: var(--accent);
}

.item__row {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.item__name {
  font-size: var(--text-sm);
}

.item__desc {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.item__ver {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.badge {
  padding: 0 4px;
  color: var(--warn);
  font-size: var(--text-xs);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.editor__head,
.seg__head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.editor__name {
  font-size: var(--text-md);
}

.editor__ver,
.seg__path,
.editor__vars,
.editor__hint {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.editor__spacer,
.seg__spacer {
  flex: 1;
}

.seg {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.seg__role {
  color: var(--accent);
  font-size: var(--text-xs);
}

.seg__dirty {
  color: var(--warn);
  font-size: var(--text-xs);
}

.seg__text {
  width: 100%;
  min-height: 220px;
  padding: var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-sm);
  line-height: 1.6;
  resize: vertical;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
</style>
