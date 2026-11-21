#Requires -Version 5.1
<#
.SYNOPSIS
    在 Antigravity 桌面版运行时抓取 Hub 发往 Google 的真实 /v1internal:* 请求。

.DESCRIPTION
    通过 CLOUD_CODE_URL 环境变量把 Antigravity 的 Cloud Code 端点指向本机
    捕获服务器，再从同一环境启动 Antigravity。抓包数据只保存在本地。

    默认只保存 generateContent / streamGenerateContent，Authorization 等
    认证头会脱敏。传 -Offline 时只有模型生成请求留在本地（零模型流量），
    启动/元数据请求仍经 Go 转发器取回，客户端可正常显示模型列表；
    传 -OfflineAll 则所有 /v1internal 请求都不转发（可能没有可用模型）。

.EXAMPLE
    .\run_capture.ps1

.EXAMPLE
    .\run_capture.ps1 -Offline

.EXAMPLE
    .\run_capture.ps1 -CaptureAll -NoLaunch

.EXAMPLE
    # 离线抓"工具调用"：发一条消息后，代理会合成一次 read_file 调用
    .\run_capture.ps1 -Offline -CaptureAll -OfflineScript tool-call

.EXAMPLE
    # 离线抓子 agent：合成一次 invoke_subagent(research) 调用
    .\run_capture.ps1 -Offline -CaptureAll -OfflineScript subagent
#>
[CmdletBinding()]
param(
    [int]$Port = 18080,
    [string]$UpstreamUrl = "https://daily-cloudcode-pa.googleapis.com",
    [switch]$Offline,
    [switch]$OfflineAll,
    [ValidateSet("none", "tool-call", "subagent")]
    [string]$OfflineScript = "none",
    [switch]$CaptureAll,
    [switch]$CaptureAuth,
    [switch]$NoLaunch,
    [switch]$NoWait,
    [switch]$ForceClose,
    [string]$AntigravityPath = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$ToolDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ToolDir "..\..")).Path
$ServerScript = Join-Path $ToolDir "capture_server.py"
$CaptureRoot = Join-Path $ToolDir "captures"
$LogRoot = Join-Path $ToolDir "logs"
$ClashDir = Join-Path $env:APPDATA "io.github.clash-verge-rev.clash-verge-rev"
$LsLog = Join-Path $env:APPDATA "Antigravity\logs\language_server.log"

if (-not (Test-Path $ServerScript)) {
    throw "找不到捕获服务器脚本: $ServerScript"
}

function Resolve-Python {
    param([string]$Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) {
            return (Resolve-Path $Explicit).Path
        }
        $command = Get-Command $Explicit -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
        throw "指定的 Python 不存在: $Explicit"
    }

    $repoPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $repoPython) {
        return $repoPython
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "找不到 Python。请用 -Python 指定解释器，例如 -Python D:\path\to\python.exe"
}

function Test-PortListening {
    param([int]$Port)

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen `
        -ErrorAction SilentlyContinue
    return [bool]$connections
}

function Get-ClashTunState {
    $state = [ordered]@{
        VergeConfig = $null
        RuntimeConfig = $null
        VergePath = Join-Path $ClashDir "verge.yaml"
        RuntimePath = Join-Path $ClashDir "clash-verge.yaml"
    }

    if (Test-Path $state.VergePath) {
        $state.VergeConfig = [bool](Select-String -Path $state.VergePath `
                -Pattern '^\s*enable_tun_mode:\s*true\s*$' -Quiet)
    }

    if (Test-Path $state.RuntimePath) {
        $inTun = $false
        foreach ($line in Get-Content $state.RuntimePath) {
            if ($line -match '^tun:\s*$') {
                $inTun = $true
                continue
            }
            if ($inTun -and $line -match '^\S') {
                $inTun = $false
            }
            if ($inTun -and $line -match '^\s+enable:\s*true\s*$') {
                $state.RuntimeConfig = $true
                break
            }
        }
        if ($null -eq $state.RuntimeConfig) {
            $state.RuntimeConfig = $false
        }
    }

    return [pscustomobject]$state
}

function Resolve-Antigravity {
    param([string]$Explicit)

    $candidates = @(
        $Explicit,
        (Join-Path $env:LOCALAPPDATA "Programs\Antigravity\Antigravity.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\antigravity\Antigravity.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    return $null
}

function Stop-AntigravityProcesses {
    $processes = Get-Process -Name Antigravity, language_server -ErrorAction SilentlyContinue
    if ($processes) {
        $processes | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

function Get-LatestSessionDir {
    if (-not (Test-Path $CaptureRoot)) {
        return $null
    }
    $latest = Get-ChildItem -Path $CaptureRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latest) {
        return $latest.FullName
    }
    return $null
}

# ---------------------------------------------------------------------------
# 参数检查
# ---------------------------------------------------------------------------
$PythonExe = Resolve-Python -Explicit $Python
$pythonVersion = & $PythonExe -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])"
if ([int]$pythonVersion -lt 310) {
    throw "需要 Python 3.10 或更高版本，当前: $(& $PythonExe --version)"
}
$AntigravityExe = Resolve-Antigravity -Explicit $AntigravityPath

if (-not $NoLaunch -and -not $AntigravityExe) {
    throw "找不到 Antigravity.exe。请用 -AntigravityPath 指定路径。"
}

if (Test-PortListening -Port $Port) {
    throw "端口 $Port 已有程序监听。请换一个 -Port，或先停止占用该端口的程序。"
}

if ($CaptureAuth) {
    Write-Host "警告: -CaptureAuth 会把 Authorization/Cookie 明文写入磁盘，仅在你明确需要重放请求时使用。" `
        -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# TUN 检查
# ---------------------------------------------------------------------------
$tun = Get-ClashTunState
if ($OfflineAll) {
    Write-Host "Clash TUN: 完全离线模式，不需要检查（token 刷新仍可能需要网络）。" -ForegroundColor DarkGray
}
elseif ($Offline) {
    Write-Host "Clash TUN: -Offline 仍需转发启动/元数据请求，建议保持 TUN 开启。" -ForegroundColor DarkGray
}
else {
    $tunText = "Clash TUN: verge.yaml=$($tun.VergeConfig), 运行配置=$($tun.RuntimeConfig)"
    if ($tun.VergeConfig -eq $true -and $tun.RuntimeConfig -eq $true) {
        Write-Host $tunText -ForegroundColor Green
    }
    elseif ($tun.VergeConfig -eq $null -and $tun.RuntimeConfig -eq $null) {
        Write-Host "Clash TUN: 未找到 Clash Verge 配置，跳过检查。" -ForegroundColor Yellow
    }
    else {
        Write-Host $tunText -ForegroundColor Yellow
        Write-Host "提示: 转发模式需要能访问 Google。建议先在 Clash Verge 打开 TUN 模式。" `
            -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 准备目录
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $CaptureRoot | Out-Null
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdoutLog = Join-Path $LogRoot "capture-$stamp.out.log"
$stderrLog = Join-Path $LogRoot "capture-$stamp.err.log"

# ---------------------------------------------------------------------------
# 启动捕获服务器
# ---------------------------------------------------------------------------
$serverArgs = @(
    "`"$ServerScript`"",
    "--port", "$Port",
    "--upstream-url", "`"$UpstreamUrl`"",
    "--log-dir", "`"$CaptureRoot`""
)
if ($Offline) { $serverArgs += "--offline" }
if ($OfflineAll) { $serverArgs += "--offline-all" }
if ($OfflineScript -ne "none") { $serverArgs += @("--offline-script", $OfflineScript) }
if ($CaptureAll) { $serverArgs += "--capture-all" }
if ($CaptureAuth) { $serverArgs += "--capture-auth" }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

if (-not $Offline) {
    Write-Host "转发模式：请求会经 Go 转发器真实发往 Google（TLS 指纹与真实客户端一致）。" `
        -ForegroundColor Yellow
    Write-Host "  需要先编译 tools/upstream_tls_proxy；不需要真实回复时请加 -Offline。" `
        -ForegroundColor Yellow
}

$serverProc = Start-Process -FilePath $PythonExe `
    -ArgumentList ($serverArgs -join " ") `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog

$ready = $false
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if ($serverProc.HasExited) {
        $errText = ""
        if (Test-Path $stderrLog) {
            $errText = Get-Content -Path $stderrLog -Raw -Encoding utf8
        }
        $outText = ""
        if (Test-Path $stdoutLog) {
            $outText = Get-Content -Path $stdoutLog -Raw -Encoding utf8
        }
        throw "捕获服务器启动失败 (exit $($serverProc.ExitCode)): $errText $outText"
    }
    if (Test-PortListening -Port $Port) {
        $ready = $true
        break
    }
    Start-Sleep -Milliseconds 250
}

if (-not $ready) {
    if (-not $serverProc.HasExited) {
        Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
    }
    throw "捕获服务器在 15 秒内没有开始监听 127.0.0.1:$Port。请查看 $stderrLog"
}

Start-Sleep -Milliseconds 300
$sessionDir = Get-LatestSessionDir
Write-Host ""
Write-Host "捕获服务器已启动: http://127.0.0.1:$Port" -ForegroundColor Green
if ($sessionDir) {
    Write-Host "抓包目录: $sessionDir" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 给当前进程设置 CLOUD_CODE_URL
# ---------------------------------------------------------------------------
$env:CLOUD_CODE_URL = "http://127.0.0.1:$Port"
Write-Host "已设置 CLOUD_CODE_URL=$env:CLOUD_CODE_URL" -ForegroundColor Cyan

if ($NoLaunch) {
    $NoWait = $true
    $launchTarget = if ($AntigravityExe) {
        $AntigravityExe
    }
    else {
        Join-Path $env:LOCALAPPDATA "Programs\Antigravity\Antigravity.exe"
    }
    $runningNoLaunch = Get-Process -Name Antigravity, language_server -ErrorAction SilentlyContinue
    if ($runningNoLaunch) {
        Write-Host "警告: Antigravity 正在运行。必须先完全退出，否则新窗口不会继承 CLOUD_CODE_URL。" `
            -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "未启动 Antigravity（-NoLaunch）。请先完全退出 Antigravity，然后在同一个终端里手动启动:" `
        -ForegroundColor Yellow
    Write-Host "  `$env:CLOUD_CODE_URL = `"http://127.0.0.1:$Port`"" -ForegroundColor Yellow
    Write-Host "  & `"$launchTarget`"" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "捕获服务器 PID: $($serverProc.Id)"
    Write-Host "停止捕获服务器: Stop-Process -Id $($serverProc.Id) -Force"
    return
}

# ---------------------------------------------------------------------------
# 关闭已运行的 Antigravity，确保新进程继承环境变量
# ---------------------------------------------------------------------------
$running = Get-Process -Name Antigravity, language_server -ErrorAction SilentlyContinue
if ($running) {
    if (-not $ForceClose) {
        Write-Host ""
        Write-Host "Antigravity 正在运行，必须重启才能让 CLOUD_CODE_URL 生效。" -ForegroundColor Yellow
        $answer = Read-Host "是否现在关闭 Antigravity 和 language_server？[y/N]"
        if ($answer -notmatch '^(y|yes)$') {
            Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
            Write-Host "已取消。"
            return
        }
    }
    Write-Host "正在关闭 Antigravity..." -ForegroundColor Yellow
    Stop-AntigravityProcesses
    $stillRunning = Get-Process -Name Antigravity, language_server -ErrorAction SilentlyContinue
    if ($stillRunning) {
        Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
        throw "Antigravity 仍在运行，无法自动关闭。请手动退出 Antigravity（含托盘）后重试。"
    }
}

# 轮转旧日志，避免把上一轮的覆盖记录误判成这一轮。
$logRotated = $false
if (Test-Path $LsLog) {
    $backup = "$LsLog.bak-$stamp"
    try {
        Move-Item -Path $LsLog -Destination $backup -Force
        $logRotated = $true
        Write-Host "旧 language_server.log 已备份到: $backup" -ForegroundColor DarkGray
    }
    catch {
        Write-Host "无法轮转 language_server.log（可能仍被占用）: $_" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 启动 Antigravity
# ---------------------------------------------------------------------------
$launchTime = Get-Date
Write-Host "启动 Antigravity: $AntigravityExe" -ForegroundColor Cyan
try {
    $antigravityProc = Start-Process -FilePath $AntigravityExe `
        -WorkingDirectory (Split-Path -Parent $AntigravityExe) `
        -PassThru
}
catch {
    Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
    throw "启动 Antigravity 失败: $_"
}

# ---------------------------------------------------------------------------
# 在 language_server.log 中确认覆盖生效
# ---------------------------------------------------------------------------
Write-Host "等待 language_server.log 确认 CLOUD_CODE_URL 覆盖..." -ForegroundColor DarkGray
$confirmed = $false
$overrideLine = $null
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $LsLog) {
        $file = Get-Item $LsLog
        if ($file.LastWriteTime -ge $launchTime.AddSeconds(-1)) {
            $hits = Select-String -Path $LsLog `
                -Pattern "Overriding CloudCodeServerURL via CLOUD_CODE_URL" `
                -SimpleMatch
            if ($hits) {
                $overrideLine = ($hits | Select-Object -Last 1).Line
                if ($overrideLine -match [regex]::Escape("127.0.0.1:$Port")) {
                    $confirmed = $true
                }
                break
            }
        }
    }
    if ($antigravityProc.HasExited) {
        break
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""
if ($confirmed) {
    Write-Host "已确认: language_server 读取了 CLOUD_CODE_URL，请求会发到本机捕获服务器。" `
        -ForegroundColor Green
    Write-Host "  $overrideLine" -ForegroundColor DarkGray
}
elseif ($overrideLine) {
    Write-Host "日志里出现了覆盖记录，但地址不是 127.0.0.1:${Port}:" -ForegroundColor Yellow
    Write-Host "  $overrideLine"
    Write-Host "可能有其它配置覆盖了 CLOUD_CODE_URL，请检查当前终端和登录 shell 的环境变量。"
}
else {
    Write-Host "未能在日志中确认覆盖。请检查:" -ForegroundColor Yellow
    Write-Host "  $LsLog"
    if (-not $logRotated) {
        Write-Host "注意: 旧日志没有轮转成功，上面的判断可能不可靠。" -ForegroundColor Yellow
    }
    Write-Host "如果日志里没有 Overriding CloudCodeServerURL，说明这个版本的启动链路没有继承该环境变量。"
    Write-Host "此时可以直接从终端启动 Antigravity，或把日志内容发给我继续分析。"
}

Write-Host ""
Write-Host "抓包进行中。现在在 Antigravity 里正常发一条消息即可。" -ForegroundColor Cyan
Write-Host "默认只保存模型生成请求，Authorization 等认证头已脱敏。"
if ($Offline) {
    Write-Host "当前是 -Offline: 只有模型生成请求留在本地（零模型流量），" -ForegroundColor Yellow
    Write-Host "启动/元数据请求仍经 Go 转发器正常取回，模型列表可正常显示；" -ForegroundColor Yellow
    Write-Host "Antigravity 不会给出真实回复，这是预期行为。" -ForegroundColor Yellow
}
if ($OfflineAll) {
    Write-Host "当前是 -OfflineAll: 所有 /v1internal 请求都不转发；可能出现'没有可用模型'。" `
        -ForegroundColor Yellow
}
if ($OfflineScript -ne "none") {
    $scriptTool = if ($OfflineScript -eq "tool-call") { "read_file（只读临时文件）" } else { "invoke_subagent（research，只读无网络）" }
    Write-Host "离线脚本已启用: 你发第一条消息后，代理会合成一次 $scriptTool 调用，仅一次。" `
        -ForegroundColor Cyan
}
Write-Host "关闭 Antigravity 后脚本会自动停止捕获服务器。" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------------------
# 等待退出并收尾
# ---------------------------------------------------------------------------
try {
    if (-not $NoWait) {
        Wait-Process -Id $antigravityProc.Id -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    else {
        Write-Host "捕获服务器保持运行。PID: $($serverProc.Id)"
        Write-Host "手动停止: Stop-Process -Id $($serverProc.Id) -Force"
    }
}
finally {
    if (-not $NoWait -and $serverProc -and -not $serverProc.HasExited) {
        Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
    }

    $latestSession = Get-LatestSessionDir
    Write-Host ""
    if ($latestSession) {
        Write-Host "抓包结果: $latestSession" -ForegroundColor Green
        Write-Host "索引文件: $(Join-Path $latestSession 'index.jsonl')"
    }
    Write-Host "服务器日志: $stdoutLog"
    if ((Test-Path $stderrLog) -and (Get-Item $stderrLog).Length -gt 0) {
        Write-Host "错误日志: $stderrLog" -ForegroundColor Yellow
    }
}
