/// <reference types="vite/client" />

// 说明：这里**故意不写** `declare module "*.vue"`。
// `vue-tsc` 原生认识 `.vue` 并能校验 props / emits；补一个返回 `any` 的模块声明会把
// 所有组件的类型退化成 `any` —— 等于把 T4.1 的类型门禁关掉。
