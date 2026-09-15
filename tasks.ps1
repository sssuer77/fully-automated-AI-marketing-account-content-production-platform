#Requires -Version 5.1
<#
.SYNOPSIS
    开发任务入口（T1.1）：把常用命令收敛成 `.\tasks.ps1 <task>`。

.EXAMPLE
    .\tasks.ps1 check      # 格式 + lint + 类型 + 单测（提交前跑这个）
    .\tasks.ps1 doctor     # 启动自检门禁
    .\tasks.ps1 test       # 全部测试（不含 gpu/slow/net）
    .\tasks.ps1 web:gen    # 重新生成前端契约（openapi.json / events.ts / types.gen.ts）
    .\tasks.ps1 web:verify # 前端全量验收：typecheck + test + build + 体积门禁
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('check', 'fmt', 'lint', 'type', 'test', 'test-all', 'doctor', 'sync', 'sync-tts', 'clean', 'web', 'web:gen', 'web:check', 'web:verify')]
    [string]$Task = 'check'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
. (Join-Path $RepoRoot 'scripts\env.ps1')

# ── 前端（T4.1）──────────────────────────────────────────────
# 两个坑：
# 1. npm 必须走 `cmd /c`：PowerShell 5.1 会把 npm 的 notice 当成
#    NativeCommandError，在 `$ErrorActionPreference = 'Stop'` 下直接把任务判失败。
# 2. 上面那行 dot-source 是**前置条件**：`scripts\env.ps1` 设的 `NPM_CONFIG_CACHE`
#    决定 npm 缓存落 D 盘还是写爆 C 盘（T4.1 裁定 111）。
function Invoke-WebNpm {
    param([Parameter(Mandatory)][string]$Script)
    $webDir = Join-Path $RepoRoot 'web'
    cmd /c "cd /d `"$webDir`" && set NPM_CONFIG_UPDATE_NOTIFIER=false && npm run $Script"
    if ($LASTEXITCODE -ne 0) { throw "npm run $Script 未通过" }
}

Push-Location $RepoRoot
try {
    switch ($Task) {
        'fmt'      { uv run ruff format src tests workers scripts }
        'lint'     { uv run ruff check src tests workers scripts --fix }
        'type'     { uv run mypy src tests workers scripts }
        'test'     { uv run pytest -m 'not gpu and not slow and not net' -q }
        'test-all' { uv run pytest -q }
        'doctor'   { uv run studio doctor }
        'sync'     { uv sync --extra dev }
        'sync-tts' { uv sync --project tts }
        'clean'    { Get-ChildItem -Path $RepoRoot -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force }
        'web'        { Invoke-WebNpm 'dev' }
        'web:gen'    {
            uv run python scripts/dump_web_contracts.py
            if ($LASTEXITCODE -ne 0) { throw 'dump_web_contracts.py 失败' }
            Invoke-WebNpm 'gen:api'
        }
        'web:check'  { uv run python scripts/dump_web_contracts.py --check }
        'web:verify' { Invoke-WebNpm 'verify' }
        'check' {
            uv run ruff format --check src tests workers scripts
            if ($LASTEXITCODE -ne 0) { throw 'ruff format 未通过（跑 .\tasks.ps1 fmt 修复）' }
            uv run ruff check src tests workers scripts
            if ($LASTEXITCODE -ne 0) { throw 'ruff check 未通过' }
            # Web 契约漂移（T4.1）：前端类型是后端生成的，漂移必须在门禁里看得见。
            uv run python scripts/dump_web_contracts.py --check
            if ($LASTEXITCODE -ne 0) { throw 'Web 契约已漂移（跑 .\tasks.ps1 web:gen 重新生成）' }
            uv run mypy src tests workers scripts
            if ($LASTEXITCODE -ne 0) { throw 'mypy 未通过' }
            uv run pytest -m 'not gpu and not slow and not net' -q
            if ($LASTEXITCODE -ne 0) { throw 'pytest 未通过' }
            Write-Host '[tasks] check 全部通过 ✅' -ForegroundColor Green
        }
    }
} finally {
    Pop-Location
}