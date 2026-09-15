#Requires -Version 5.1

<#
.SYNOPSIS
    每日媒资回收（T4.12 · §03.7.5）。

.DESCRIPTION
    1. dot-source scripts/env.ps1 —— 缓存/TEMP 全部落在非系统盘（R1）；
    2. 跑 `studio gc run`：
       · DB 行按保留期删（system_logs 30 天 / debug 7 天 / llm_calls 90 天 / task_events 90 天，分批 ≤5000 行）；
       · 任务 completed 后 24h 删句子音频与 voice_master（**成片不在则跳过**）；
       · scenes/*.mp4 按 mtime 7 天；失败任务的 .partial 立即清；
       · TTS 缓存按 LRU 压到 5 GB（use_count >= 2 的条目降权）；
       · 已消费热点**移动**进 data/hot/archive/<YYYYMM>/（移动而非删除）；
    3. 白名单**永不清理**：成片 / 封面 / 稿件 / 时间轴 / manifest / 滤镜图 / 备份 / 素材库 / 热点归档；
    4. 退出码非 0 ⇒ 有候选被守卫拒绝（候选清单写错了，是 bug 不是噪音），调用方应当报警。

    先干跑一遍看清单（不动手）：`.\scripts\gc_media.ps1 -DryRun`

    为什么不做成"删得更狠一点"：GC 无人值守地跑，判据写错的代价不对称 ——
    多留一会儿只是占盘，删错一次是不可逆的。

.PARAMETER DryRun
    只报告，一个字节都不动。

.PARAMETER RowsOnly
    只回收 DB 行（不碰文件）。

.PARAMETER MediaOnly
    只回收文件（不碰 DB 行）。

.PARAMETER Json
    以 JSON 输出（计划任务/CI 留痕用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\gc_media.ps1

.EXAMPLE
    # 计划任务：每天 04:00 跑一次（无人值守；与 config/app.yaml → scheduler.gc_cron 同一时刻）
    schtasks /create /tn "Studio Media GC" /tr "powershell -ExecutionPolicy Bypass -File <repo>\scripts\gc_media.ps1" /sc daily /st 04:00
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$RowsOnly,
    [switch]$MediaOnly,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1') -Quiet:$Json

Push-Location $RepoRoot
try {
    $arguments = @('run', 'studio', 'gc', 'run')

    if ($DryRun)    { $arguments += '--dry-run' }
    if ($RowsOnly)  { $arguments += '--rows-only' }
    if ($MediaOnly) { $arguments += '--media-only' }
    if ($Json)      { $arguments += '--json' }

    & uv @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host ''
    Write-Host '[gc] 有候选被守卫拒绝 —— 白名单里的东西被列进了删除清单，请查 src/studio/gc/media.py 的候选收集。' -ForegroundColor Red
}
exit $code
