// `GET /api/v1/health`（T1.7 / §04.8.1）—— `api` 服务的就绪判据。

import { apiGet, type OkJson } from "../http";

export type HealthResponse = OkJson<"/api/v1/health", "get">;

export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return apiGet<HealthResponse>("/api/v1/health", { signal });
}
