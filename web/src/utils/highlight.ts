// 搜索高亮切分（T4.9）。
//
// 为什么不是 `v-html` + 正则替换：日志的 `message` 是**不可信文本**（里面可能有
// 用户输入、模型输出、stderr 片段）。拼 HTML 就等于给自己开一个 XSS 口子，
// 而且日志面板恰好是最常被"外部内容"填充的地方。切分成片段交给 Vue 渲染则天然安全。

export interface TextSegment {
  text: string;
  hit: boolean;
}

/** 把 `text` 按 `needle`（字面量、忽略大小写）切成 `命中 / 未命中` 的片段。 */
export function splitHighlight(text: string, needle: string): TextSegment[] {
  const trimmed = needle.trim();
  if (!trimmed) return [{ text, hit: false }];
  const haystack = text.toLowerCase();
  const lowered = trimmed.toLowerCase();
  const parts: TextSegment[] = [];
  let cursor = 0;
  for (;;) {
    const found = haystack.indexOf(lowered, cursor);
    if (found < 0) break;
    if (found > cursor) parts.push({ text: text.slice(cursor, found), hit: false });
    parts.push({ text: text.slice(found, found + lowered.length), hit: true });
    cursor = found + lowered.length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false });
  return parts.length > 0 ? parts : [{ text, hit: false }];
}
