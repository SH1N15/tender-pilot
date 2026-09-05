# 智能招投标 Agent 平台 vNext Windows 一键开发启动脚本
# 用法: powershell -ExecutionPolicy Bypass -File scripts\dev_start.ps1 [-SkipFrontend] [-SkipInfra]
# Redis 优先使用本机 Memurai Windows Service（官方 Redis 兼容，端口 6379）。
# 注意：Memurai Developer Edition 仅用于开发/测试，每 10 天需重启服务。
param([switch]$SkipFrontend, [switch]$SkipInfra)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PidDir = Join-Path $Root ".dev\pids"
$LogDir = Join-Path $Root ".dev\logs"
New-Item -ItemType Directory -Force -Path $PidDir, $LogDir | Out-Null

function Write-Step($msg) { Write-Host "[dev_start] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[dev_start] $msg" -ForegroundColor Green }
function Write-Warn($msg){ Write-Host "[dev_start] $msg" -ForegroundColor Yellow }

Write-Step "投标智航 / TenderPilot vNext 启动"

# 1) 依赖检查
if (-not (Test-Path $Py)) {
    Write-Host "[dev_start] 未找到虚拟环境 .venv，请先执行:" -ForegroundColor Red
    Write-Host '    .venv\Scripts\python.exe -m pip install -e ".[dev]"' -ForegroundColor Red
    exit 1
}
& $Py -c "import services.main" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[dev_start] import services.main 失败，请检查依赖安装" -ForegroundColor Red
    exit 1
}
Write-Ok "依赖 OK (import services.main)"

# 2) 基础设施（PostgreSQL/Redis/MinIO）
#    优先使用本机 Memurai Windows Service（官方 Redis 兼容，端口 6379）；
#    若未安装 Memurai 再尝试 docker compose；均不可用则降级。
#    注意：Memurai Developer Edition 仅用于开发/测试，每 10 天需重启服务。
if (-not $SkipInfra) {
    $memurai = Get-Service -Name Memurai -ErrorAction SilentlyContinue
    if ($memurai) {
        if ($memurai.Status -ne 'Running') {
            Write-Step "Memurai Windows Service 存在但未运行，尝试启动 ..."
            try {
                Start-Service -Name Memurai -ErrorAction Stop
                Start-Sleep -Seconds 2
                $memurai.Refresh()
            } catch {
                Write-Warn "Memurai 服务启动失败: $($_.Exception.Message)"
            }
        }
        if ($memurai.Status -eq 'Running') {
            Write-Ok "Memurai Windows Service 已运行 (Redis 兼容，端口 6379)"
        } else {
            Write-Warn "Memurai 服务存在但未运行，后端 Redis 可能不可用"
        }
        $redisListen = Get-NetTCPConnection -LocalPort 6379 -State Listen -ErrorAction SilentlyContinue
        if ($redisListen) {
            Write-Ok "localhost:6379 监听中（Redis 可用）"
        } else {
            Write-Warn "localhost:6379 尚未监听，请稍后检查 Memurai 日志"
        }
    } else {
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if ($docker) {
            Write-Step "未检测到 Memurai，尝试 docker compose 启动 postgres/redis/minio ..."
            try {
                docker compose -f (Join-Path $Root "docker\docker-compose.yml") up -d postgres redis minio 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "docker compose 已启动基础设施"
                } else {
                    Write-Warn "docker compose 启动失败，后端将降级运行（DB/Redis 不可用）"
                }
            } catch {
                Write-Warn "docker 不可用，后端将降级运行（DB/Redis 不可用），GUI 仍可打开设置/诊断页"
            }
        } else {
            Write-Warn "未检测到 Memurai / Docker，跳过基础设施；后端降级运行"
        }
    }
}

# 3) 数据库初始化（create_all + seed 管理员；失败不阻断——后端有降级）
Write-Step "初始化数据库 schema + 默认管理员（若 DB 可用）..."
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$pgReady = Get-Command pg_isready -ErrorAction SilentlyContinue
if ($pgReady) {
    & pg_isready -h localhost -p 5432 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "PostgreSQL 未就绪（localhost:5432）。将继续以开发模式启动（admin@dev.local / dev123）。"
    } else {
        & $Py -c "import asyncio; from services.database import init_db; asyncio.run(init_db())" 2>&1 | Out-Null
        Write-Ok "数据库初始化完成（已建表 + 默认管理员）"
    }
} else {
    & $Py -c "import asyncio; from services.database import init_db; asyncio.run(init_db())" 2>&1 | Out-Null
    Write-Ok "数据库初始化尝试完成（若 PostgreSQL 未运行则会降级开发模式）"
}
$ErrorActionPreference = $oldEAP

# 4) 启动 FastAPI 后端
$apiPid = Join-Path $PidDir "api.pid"
if (Test-Path $apiPid) {
    $old = Get-Content $apiPid
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { Write-Ok "后端已在运行 (PID $old)，跳过" } else { Remove-Item $apiPid -Force }
}
if (-not (Test-Path $apiPid)) {
    Write-Step "启动 FastAPI (uvicorn :8000) ..."
    $p = Start-Process -FilePath $Py -ArgumentList @("-m","uvicorn","services.main:app","--host","0.0.0.0","--port","8000","--log-level","info") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "api.out.log") -RedirectStandardError (Join-Path $LogDir "api.err.log") -PassThru
    $p.Id | Set-Content $apiPid
    Write-Ok "后端启动 PID=$($p.Id)  日志: .dev/logs/api.out.log"
}

# 5) 启动 MCP HTTP（streamable-http :9001，独立进程）
$mcpPid = Join-Path $PidDir "mcp.pid"
if (Test-Path $mcpPid) {
    $old = Get-Content $mcpPid
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { Write-Ok "MCP 已在运行 (PID $old)，跳过" } else { Remove-Item $mcpPid -Force }
}
if (-not (Test-Path $mcpPid)) {
    Write-Step "启动 MCP (streamable-http :9001) ..."
    $p = Start-Process -FilePath $Py -ArgumentList @("-m","services.mcp.server","--transport","streamable-http","--host","127.0.0.1","--port","9001") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "mcp.out.log") -RedirectStandardError (Join-Path $LogDir "mcp.err.log") -PassThru
    $p.Id | Set-Content $mcpPid
    Write-Ok "MCP 启动 PID=$($p.Id)"
}

# 5.5) 启动 Celery Worker（Windows 下 --pool=solo；PID 落 .dev/pids/celery.pid）
$celeryPid = Join-Path $PidDir "celery.pid"
if (Test-Path $celeryPid) {
    $old = Get-Content $celeryPid
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { Write-Ok "Celery worker 已在运行 (PID $old)，跳过" } else { Remove-Item $celeryPid -Force }
}
if (-not (Test-Path $celeryPid)) {
    Write-Step "启动 Celery worker (pool=solo) ..."
    $p = Start-Process -FilePath $Py -ArgumentList @("-m","celery","-A","services.celery_app.celery_app","worker","--pool=solo","--loglevel=info","--concurrency=1") -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "celery.out.log") -RedirectStandardError (Join-Path $LogDir "celery.err.log") -PassThru
    $p.Id | Set-Content $celeryPid
    Write-Ok "Celery worker 启动 PID=$($p.Id)  日志: .dev/logs/celery.out.log"
}

# 6) 前端（Vite + Electron）
if (-not $SkipFrontend) {
    $fePid = Join-Path $PidDir "frontend.pid"
    if (Test-Path $fePid) {
        $old = Get-Content $fePid
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) { Write-Ok "前端已在运行 (PID $old)，跳过" } else { Remove-Item $fePid -Force }
    }
    if (-not (Test-Path $fePid)) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
        if (-not $npm) { Write-Warn "未找到 npm，跳过前端；可手动执行 cd packages/desktop && npm run dev" }
        else {
            Write-Step "启动前端 (Vite + Electron) ..."
            $env:NODE_ENV = "development"  # 子进程继承，Electron 走 dev 分支 loadURL
            $npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
            if (-not $npmCmd) { $npmCmd = "C:\Program Files\nodejs\npm.cmd" }
            $p = Start-Process -FilePath $npmCmd -ArgumentList @("run","electron:dev") -WorkingDirectory (Join-Path $Root "packages\desktop") -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "frontend.out.log") -RedirectStandardError (Join-Path $LogDir "frontend.err.log") -PassThru
            $p.Id | Set-Content $fePid
            Write-Ok "前端启动 PID=$($p.Id)  (Vite http://localhost:5173)"
        }
    }
}

Write-Ok "全部启动完成。"
Write-Host "  后端:    http://localhost:8000/api/health"
Write-Host "  MCP:     http://localhost:9001/mcp"
Write-Host "  Worker:  celery (pool=solo), 日志 .dev/logs/celery.out.log"
Write-Host "  前端:    http://localhost:5173"
Write-Host "停止:    powershell -ExecutionPolicy Bypass -File scripts\dev_stop.ps1"

exit 0
