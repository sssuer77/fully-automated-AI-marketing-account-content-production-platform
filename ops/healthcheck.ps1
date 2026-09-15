#Requires -Version 5.1
<#
.SYNOPSIS
    探活 API 与 TTS 子服务（CI / 运维用）。

.DESCRIPTION
    退出码：0 = API 健康；1 = API 不可达或返回非 ok。
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8787'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $RepoRoot 'scripts\env.ps1')

$url = "$BaseUrl/api/v1/health"
try {
    $response = Invoke-RestMethod -Uri $url -TimeoutSec 5
} catch {
    Write-Host "[health] API 不可达：$url" -ForegroundColor Red
    exit 1
}

Write-Host "[health] $url ⇒ ok" -ForegroundColor Green
$response | ConvertTo-Json -Depth 6
exit 0