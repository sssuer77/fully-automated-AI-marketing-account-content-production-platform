// 应用入口（T4.1）。样式顺序固定：先令牌后基础样式（base.css 用 var()）。

import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import "./styles/tokens.css";
import "./styles/base.css";

createApp(App).use(createPinia()).mount("#app");
