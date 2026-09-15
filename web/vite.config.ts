import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// `defineConfig` 从 `vitest/config` 取（同一份实现 + `test` 段有类型），
// 否则 `test.include` 会在 `npm run typecheck` 里报 TS2769。
//
// 开发期把 /api 与 /ws 代理到本机 API 进程（T1.12 的 api 服务，8787）。
// 生产由 `studio serve` 直接托管 dist（T4.12），不走代理。
const API_TARGET = process.env.STUDIO_API_TARGET ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/ws": { target: API_TARGET, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
