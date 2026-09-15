#Requires -Version 5.1
<#
.SYNOPSIS
    每日数据库备份（T4.12 · §03.7.4）。

.DESCRIPTION
    1. dot-source scripts/env.ps1 —— 缓存/TEMP 全部落在非系统盘（R1）；
    2. 跑 `studio db backup`：`VACUUM INTO` 热备一份**一致快照**到
       `data/backups/studio_YYYYMMDD.db`（WAL 下不阻塞写 ⇒ 不用停进程）；
    3. 顺带轮转：保留 7 份日备 + 4 份周备（周日），其余删除；
    4. 退出码非 0 ⇒ 备份**没产出**，调用方（计划任务）应当报警。

    为什么用 `VACUUM INTO` 而不是 Copy-Item：WAL 模式下库的真身分散在
    `studio.db` + `studio.db-wal` 两个文件里，复制出来的是"两个瞬间的拼盘"。

.PARAMETER Dest
    备份目录（默认 data/backups）。

.PARAMETER KeepDaily
    保留几份日备（默认 7）。

.PARAMETER KeepWeekly
    保留几份周备（默认 4）。

.PARAMETER Overwrite
    今天的备份已存在时覆盖它（默认**拒绝**：那份可能是唯一可用的一份）。

.PARAMETER Json
    以 JSON 输出（计划任务/CI 留痕用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1

.EXAMPLE
    # 计划任务：每天 03:30 跑一次（无人值守）
    schtasks /create /tn "Studio Daily Backup" /tr "powershell -ExecutionPolicy Bypass -File <repo>\scripts\backup_db.ps1" /sc daily /st 03:30
#>
[CmdletBinding()]
param(
    [string]$Dest,
    [int]$KeepDaily = 7,
    [int]$KeepWeekly = 4,
    [switch]$Overwrite,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1') -Quiet:$Json

Push-Location $RepoRoot
try {
    $arguments = @('run', 'studio', 'db', 'backup')
    if ($Dest)       { $arguments += @('--dest', $Dest) }
    if ($KeepDaily)  { $arguments += @('--keep-daily', $KeepDaily) }
    if ($KeepWeekly) { $arguments += @('--keep-weekly', $KeepWeekly) }
    if ($Overwrite)  { $arguments += '--overwrite' }
    if ($Json)       { $arguments += '--json' }

    & uv @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host ''
    Write-Host '[backup] 日备未产出 —— 磁盘上有备份不等于备份是新的，请立即排查。' -ForegroundColor Red
}
exit $code
