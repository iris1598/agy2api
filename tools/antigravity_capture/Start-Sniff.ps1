#Requires -Version 5.1
<#
.SYNOPSIS
    sniff_cli.py 的入口：自动定位仓库 venv Python，passive 子命令自动 UAC 提权。

.DESCRIPTION
    passive（dumpcap 抓 TUN 网卡）需要管理员权限；本脚本检测到非管理员时会
    通过 Start-Process -Verb RunAs 重新拉起自己（弹 UAC），新窗口运行完毕后
    停留显示结果。analyze / report 不需要提权，直接在当前窗口运行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\antigravity_capture\Start-Sniff.ps1 passive

.EXAMPLE
    .\tools\antigravity_capture\Start-Sniff.ps1 passive --duration 120 --launch

.EXAMPLE
    .\tools\antigravity_capture\Start-Sniff.ps1 analyze .\tools\antigravity_capture\captures\passive-20261120-120000

.EXAMPLE
    .\tools\antigravity_capture\Start-Sniff.ps1 report
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SniffArgs
)

$ErrorActionPreference = "Stop"
$ToolDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ToolDir "..\..")).Path
$CliScript = Join-Path $ToolDir "sniff_cli.py"

if (-not (Test-Path $CliScript)) {
    throw "找不到 CLI 脚本: $CliScript"
}

function Resolve-Python {
    $repoPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $repoPython) {
        return (Resolve-Path $repoPython).Path
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw "找不到 Python。请先创建仓库 venv（uv sync），或把 python 加入 PATH。"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# passive 需要 Npcap/dumpcap 的管理员权限；analyze/report 不需要
$needsAdmin = ($SniffArgs.Count -gt 0 -and $SniffArgs[0] -eq "passive")

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($needsAdmin -and -not $isAdmin -and $env:SNIFF_NO_ELEVATE -ne "1") {
    Write-Host "passive 抓包需要管理员权限（Npcap），即将弹出 UAC 提权重启本脚本..." -ForegroundColor Yellow
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($SniffArgs) {
        $argList += ($SniffArgs | ForEach-Object { $_ })
    }
    $env:SNIFF_ELEVATED_PAUSE = "1"
    $raised = Start-Process powershell -Verb RunAs -ArgumentList ($argList -join " ") -PassThru -Wait
    exit $raised.ExitCode
}

$PythonExe = Resolve-Python
& $PythonExe $CliScript @SniffArgs
$code = $LASTEXITCODE

if ($env:SNIFF_ELEVATED_PAUSE -eq "1") {
    Write-Host ""
    Read-Host "运行结束 (exit $code)，按回车关闭本窗口"
}
exit $code
