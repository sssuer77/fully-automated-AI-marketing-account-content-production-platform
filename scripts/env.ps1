#Requires -Version 5.1
<#
  scripts/env.ps1 —— 环境变量重定向闸门（T1.1 · §02.2）

  用法（必须 dot-source，否则变量不会留在当前会话）：
      . "$PSScriptRoot\..\scripts\env.ps1"

  作用：把 uv / pip / huggingface / modelscope / torch / playwright / TEMP
        全部重定向到 D 盘，防止写爆系统盘（R1：C 盘可用空间常年 < 5 GB）。

  规则：`.env`（若存在）覆盖脚本内置默认值；内置默认值覆盖已存在的进程环境变量。
        唯一例外：已显式指向 D 盘的 UV_CACHE_DIR 会被内置默认值纠正为规范路径。
#>
param(
    # 静默模式：不打印横幅，保证 -Json 输出可被机器直接解析
    [switch]$Quiet
)


$ErrorActionPreference = 'Stop'

$StudioHome = Split-Path -Parent $PSScriptRoot
$StudioDataDir = Join-Path $StudioHome 'data'

# ── 规范值（唯一真相，与 .env.example 保持一致）─────────────────
$StudioEnvDefaults = [ordered]@{
    STUDIO_HOME             = $StudioHome
    STUDIO_DATA_DIR         = $StudioDataDir
    HF_HOME                 = 'D:\ai_models\huggingface_cache'
    MODELSCOPE_CACHE        = 'D:\ai_models\modelscope_cache'
    TORCH_HOME              = 'D:\ai_models\torch'
    UV_CACHE_DIR            = 'D:\ai_models\uv_cache'
    PIP_CACHE_DIR           = 'D:\ai_models\pip_cache'
    NPM_CONFIG_CACHE        = 'D:\ai_models\npm_cache'
    PLAYWRIGHT_BROWSERS_PATH= 'D:\ai_models\playwright'
    TEMP                    = (Join-Path $StudioDataDir 'tmp')
    TMP                     = (Join-Path $StudioDataDir 'tmp')
}

# ── 叠加 .env（存在则覆盖默认值）──────────────────────────────
$StudioEnvFile = Join-Path $StudioHome '.env'
$StudioEnvSource = 'defaults'
if (Test-Path -LiteralPath $StudioEnvFile) {
    foreach ($line in [System.IO.File]::ReadAllLines($StudioEnvFile)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $eq = $trimmed.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $trimmed.Substring(0, $eq).Trim()
        $val = $trimmed.Substring($eq + 1).Trim().Trim('"').Trim("'")
        if ($key -and $val) { $StudioEnvDefaults[$key] = $val }
    }
    $StudioEnvSource = $StudioEnvFile
}

# ── 落盘到当前进程 ────────────────────────────────────────────
foreach ($kv in $StudioEnvDefaults.GetEnumerator()) {
    Set-Item -Path "Env:$($kv.Key)" -Value $kv.Value
}

# ── 目录预建（幂等）───────────────────────────────────────────
$StudioEnvDirs = @(
    $StudioDataDir
    (Join-Path $StudioDataDir 'tmp')
    (Join-Path $StudioDataDir 'hot')
    (Join-Path $StudioDataDir 'hot\archive')
    (Join-Path $StudioDataDir 'feedback')
    # voice_src 只建**父目录**，不建 bigbear / littlebear 这两个具体音色目录。
    # 那两个名字是《熊出没》占位音色，而音色 id 与展现名是解耦的（R2，用户可换），
    # 写进环境闸门 = 每一次 dot-source 都凭空造出两个空目录，面板上就是两条
    # 永远「盘上有、库里没有」的孤儿警告（裁定 384 / 陷阱 219），删了还会长回来。
    (Join-Path $StudioDataDir 'voice_src')
    (Join-Path $StudioDataDir 'assets\mc_parkour')
    (Join-Path $StudioDataDir 'assets\bgm')
    (Join-Path $StudioDataDir 'output\topics')
    (Join-Path $StudioDataDir 'output\voice')
    (Join-Path $StudioDataDir 'output\videos')
    (Join-Path $StudioDataDir 'output\covers')
    (Join-Path $StudioDataDir 'work')
    (Join-Path $StudioDataDir 'cache\tts')
    (Join-Path $StudioDataDir 'backups')
    (Join-Path $StudioDataDir 'logs')
    'D:\ai_models\huggingface_cache'
    'D:\ai_models\modelscope_cache'
    'D:\ai_models\torch'
    'D:\ai_models\uv_cache'
    'D:\ai_models\pip_cache'
    'D:\ai_models\npm_cache'
    'D:\ai_models\playwright'
)
foreach ($d in $StudioEnvDirs) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

if (-not $Quiet) {
    Write-Host "[env] STUDIO_HOME = $StudioHome" -ForegroundColor DarkGray
    Write-Host "[env] source      = $StudioEnvSource" -ForegroundColor DarkGray
}
