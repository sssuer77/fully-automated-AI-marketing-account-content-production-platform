#Requires -Version 5.1
<#
.SYNOPSIS
    恢复演练（T4.12 · §03.7.4「每月一次」）。

.DESCRIPTION
    还原一份备份到**临时库** ⇒ `studio db check` ⇒ 抽查 3 张表行数
    （`tasks` / `jobs` / `system_logs`）。

    两处刻意的保守：
      · **拒绝还原到活库**：本脚本的存在意义是"验证备份可用"，不是"就地回滚"。
        真要回滚，先停全部进程再手工替换 `studio.db`（见
        `docs/runbook/restore_drill.md` 的步骤与回滚检查单）；
      · 目标文件已存在 ⇒ 默认**拒绝**，要覆盖请显式 `-Force`（覆盖前会清掉
        目标旁边的 `-wal` / `-shm`：旧 WAL + 新主文件 = "能打开但对不上"）。

    演练结果应当写进 `docs/runbook/restore_drill.md` 的记录表（谁、什么时候、
    哪一份备份、三张表行数）—— 没有记录的演练等于没做。

.PARAMETER From
    备份文件（默认取 `data/backups/` 里**最新**的一份）。

.PARAMETER To
    还原到哪个库（默认 `data/backups/restore_drill/studio.db`）。

.PARAMETER Force
    目标已存在时覆盖它。

.PARAMETER Json
    以 JSON 输出。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\restore_db.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\restore_db.ps1 -From data\backups\studio_20260901.db
#>
[CmdletBinding()]
param(
    [string]$From,
    [string]$To,
    [switch]$Force,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1') -Quiet:$Json

if (-not $To) {
    $To = Join-Path $RepoRoot 'data\backups\restore_drill\studio.db'
}

# 默认取最新的一份：演练要验的是"昨晚那份能不能用"，不是随便挑一份。
if (-not $From) {
    $latest = Get-ChildItem -Path (Join-Path $RepoRoot 'data\backups') -Filter 'studio_*.db' -File -ErrorAction SilentlyContinue |
        Sort-Object -Property Name -Descending |
        Select-Object -First 1
    if (-not $latest) {
        Write-Host '[restore] data/backups 里没有备份 —— 先跑 scripts\backup_db.ps1。' -ForegroundColor Red
        exit 1
    }
    $From = $latest.FullName
}

Push-Location $RepoRoot
try {
    $arguments = @('run', 'studio', 'db', 'restore', '--from', $From, '--to', $To)
    if ($Force) { $arguments += '--force' }
    if ($Json)  { $arguments += '--json' }

    & uv @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host ''
    Write-Host '[restore] 恢复演练未通过 —— 备份**不可用**，这是最高优先级的故障。' -ForegroundColor Red
    Write-Host '[restore] 排查：换更早的一份备份重试，确认是"这一份坏了"还是"备份一直没产出"。' -ForegroundColor Yellow
} else {
    Write-Host ''
    Write-Host '[restore] 演练通过。把本次结果记进 docs\runbook\restore_drill.md（谁 / 何时 / 哪一份 / 行数）。' -ForegroundColor Green
}
exit $code
