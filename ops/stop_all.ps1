#Requires -Version 5.1
<#
.SYNOPSIS
    优雅关停制片台（T1.12 · §04.5.1）。

.DESCRIPTION
    薄壳：交给 ``studio service stop``。关停时序是
    **标志文件 → Ctrl-Break → terminate → kill**，每一级都留时间给 worker 跑完
    当前单元（``draining``）；只有跑不完才升级，并且升级结果会**如实**列进
    ``forced`` —— 那意味着它可能丢了当前单元，不假装优雅。

    为什么主通道是标志文件而不是信号（裁定 102）：``GenerateConsoleCtrlEvent``
    只能作用于**同控制台**的进程组。双击 ``停止.bat`` 是另起一个控制台，
    从那里发的 Ctrl-Break 对启动器拉起的子进程**静默无效**，
    最后只能硬杀 ⇒ 丢进度。

.PARAMETER TimeoutSec
    优雅关停的宽限期（秒），默认 10（验收口径）。
#>
[CmdletBinding()]
param(
    [int]$TimeoutSec = 10
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $RepoRoot 'scripts\env.ps1')

Push-Location $RepoRoot
try {
    & uv run studio service stop --timeout-sec $TimeoutSec
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
