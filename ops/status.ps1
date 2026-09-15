#Requires -Version 5.1
<#
.SYNOPSIS
    查看制片台五进程的台账 / 存活 / 端口（T1.12）。

.DESCRIPTION
    薄壳：交给 ``studio service status``。同时列出**未就绪**的服务与修复提示
    （池 handler 未注册 / TTS 常驻服务未落地 ⇒ 降级模式，不是故障）。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $RepoRoot 'scripts\env.ps1')

Push-Location $RepoRoot
try {
    & uv run studio service status
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
