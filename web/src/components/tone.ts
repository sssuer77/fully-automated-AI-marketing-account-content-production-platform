// 状态色板（T4.1）：把"语义"和"颜色"分开，组件只认语义。
// 颜色本身仍然只来自 `styles/tokens.css` —— 这里不出现任何色值。

export type StatusTone = "ok" | "warn" | "error" | "idle" | "busy";
