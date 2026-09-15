#Requires -Version 5.1
<#
.SYNOPSIS
    studio 启动自检门禁（T1.1 · §README.6）。

.DESCRIPTION
    1. dot-source scripts/env.ps1 —— 把 uv/pip/HF/ModelScope/torch/Playwright/TEMP
       全部重定向到非系统盘（R1：C 盘写爆 = 全站停摆）；
    2. 跑 `uv run studio doctor`，逐项断言 FFmpeg / 滤镜 / NVENC / 字体 / 磁盘 /
       DB / 环境变量；
    3. 任何**阻塞项**失败 ⇒ 退出码 1，调用方（ops\start_all.ps1 / 启动.bat）必须中止。

.PARAMETER Json
    以 JSON 输出（CI / 日志留痕用）。

.PARAMETER SkipHeavy
    跳过 torch 导入与 NVENC 实编码探测（快速冒烟，约 2s vs 约 20s）。

.PARAMETER NoGate
    仅诊断：即使有阻塞项也返回 0（排障用，禁止用于启动路径）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$SkipHeavy,
    [switch]$NoGate
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'env.ps1') -Quiet:$Json

Push-Location $RepoRoot
try {
    $arguments = @('run', 'studio', 'doctor')
    if ($Json)      { $arguments += '--json' }
    if ($SkipHeavy) { $arguments += '--skip-heavy' }
    if ($NoGate)    { $arguments += '--no-gate' }

    & uv @arguments
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host ''
    Write-Host '[doctor] 门禁未通过 ⇒ 拒绝启动。逐项修复后重跑本脚本。' -ForegroundColor Red
}
exit $code