# 编译 Go 出站 TLS 转发器（需要 Go >= 1.26）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build.ps1
#   powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build.ps1 -Go "C:\Go\bin\go.exe"
# 产物: tools/upstream_tls_proxy/bin/upstream-proxy.exe
#
# 说明：仓库里已带可用的 bin/upstream-proxy.exe 时，日常使用不需要重新编译。
param(
    [string]$Out = "",
    [string]$Go = ""
)

$ErrorActionPreference = "Stop"

function Resolve-GoExe([string]$Explicit) {
    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        throw "指定的 Go 不存在: $Explicit"
    }
    $cmd = Get-Command go -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $env:ProgramFiles "Go\bin\go.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Go\bin\go.exe"),
        "C:\Go\bin\go.exe",
        (Join-Path $env:USERPROFILE "go\bin\go.exe"),
        (Join-Path $env:USERPROFILE "scoop\apps\go\current\bin\go.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Go\bin\go.exe")
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    throw @"
未找到 go 命令。

请任选一种方式：
  1) winget install --id GoLang.Go -e
  2) 从 https://golang.google.cn/dl/ 下载 go1.2x.windows-amd64.zip，解压后用
     -Go "<解压目录>\go\bin\go.exe" 指定
  3) 已有 Go 但不在 PATH：直接用 -Go 指定 go.exe 完整路径

如果只是要用抓包/反代，仓库里的 bin/upstream-proxy.exe 已经可用，无需重新编译。
"@
}

$goExe = Resolve-GoExe $Go

Push-Location $PSScriptRoot
try {
    if (-not $Out) {
        $Out = Join-Path $PSScriptRoot "bin\upstream-proxy.exe"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
    $env:CGO_ENABLED = "0"
    Write-Output "using go: $goExe"
    & $goExe build -trimpath -ldflags "-s -w" -o $Out .
    if ($LASTEXITCODE -ne 0) { throw "go build 失败 (exit $LASTEXITCODE)" }
    Write-Output "built: $Out"
}
finally {
    Pop-Location
}
