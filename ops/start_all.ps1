#Requires -Version 5.1
<#
.SYNOPSIS
    一键拉起制片台（T1.12 · 原文附2 / §7.4）。

.DESCRIPTION
    本脚本是**薄壳**：dot-source ``scripts/env.ps1``（10 项环境变量重定向到非系统盘）
    之后，把活交给 ``studio service start``。

    为什么逻辑不写在这里（T1.12 裁定 104）：关停时序（标志文件 → Ctrl-Break → 强杀
    + 超时 + 升级）、就绪等待、端口探测都是**最需要被验证**的东西 —— 写在 .ps1 里
    既测不了也改不动。真正的实现在 ``studio.services.service_manager``，
    T4.12 的"总览台启停"直接 import 同一份，不必再写第三遍。

.PARAMETER Only
    只拉起指定服务（逗号分隔，排障用）：api,tts,draft,voice,render

.PARAMETER NoBrowser
    不自动打开浏览器。

.PARAMETER NoDoctor
    跳过 doctor 门禁（**仅排障**，正常启动禁止使用）。
#>
[CmdletBinding()]
param(
    [string]$Only = '',
    [switch]$NoBrowser,
    [switch]$NoDoctor
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $RepoRoot 'scripts\env.ps1')

$CliArgs = @('run', 'studio', 'service', 'start')
if ($Only) { $CliArgs += @('--only', $Only) }
if ($NoBrowser) { $CliArgs += '--no-browser' }
if ($NoDoctor) { $CliArgs += '--no-doctor' }

Push-Location $RepoRoot
try {
    & uv @CliArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
