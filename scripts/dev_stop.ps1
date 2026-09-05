# 智能招投标 Agent 平台 vNext Windows 一键停止脚本（只清理本轮启动的进程）
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidDir = Join-Path $Root ".dev\pids"

$pids = @("api.pid","mcp.pid","celery.pid","frontend.pid")
foreach ($name in $pids) {
    $file = Join-Path $PidDir $name
    if (-not (Test-Path $file)) { continue }
    $pidVal = (Get-Content $file -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($pidVal -and $pidVal -match "^\d+$") {
        $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "[dev_stop] 停止 $name (PID $pidVal)"
            if ($name -in @("frontend.pid","celery.pid")) {
                taskkill /PID $pidVal /T /F 2>&1 | Out-Null
            } else {
                Stop-Process -Id ([int]$pidVal) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $file -Force -ErrorAction SilentlyContinue
    }
}

# 兜底：清理本脚本启动过的端口监听进程（仅限 python）
foreach ($port in @(8000, 9001)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $owner = $conn[0].OwningProcess
        $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
        if ($name -like "*python*") {
            Write-Host "[dev_stop] 清理端口 $port 的 python 进程 PID $owner"
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        }
    }
}
Write-Host "[dev_stop] 完成。未删除任何用户数据。"
