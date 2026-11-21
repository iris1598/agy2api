# 交叉编译全部发布目标（Windows 主机即可完成）。
# 需要 Go >= 1.26；纯静态（CGO_ENABLED=0），可在 musl/Termux 上直接运行。
#
# 用法: powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build-all.ps1
#       powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build-all.ps1 -Go "C:\Go\bin\go.exe"
param(
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
        (Join-Path $env:USERPROFILE "go\bin\go.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    throw "未找到 go 命令。安装 Go >= 1.26（winget install --id GoLang.Go -e），或用 -Go 指定 go.exe 路径。"
}

$goExe = Resolve-GoExe $Go
Write-Output "using go: $goExe"

Push-Location $PSScriptRoot
try {
    New-Item -ItemType Directory -Force -Path bin | Out-Null
    $env:CGO_ENABLED = "0"

    function Build-Target([string]$GoOs, [string]$GoArch, [string]$Suffix, [string]$Extra) {
        $out = "bin/upstream-proxy-$GoOs-$GoArch$Suffix"
        Write-Output "building $out"
        $env:GOOS = $GoOs
        $env:GOARCH = $GoArch
        $env:GOARM = ""
        if ($Extra) { Invoke-Expression $Extra }
        & $goExe build -trimpath -ldflags "-s -w" -o $out .
        if ($LASTEXITCODE -ne 0) { throw "go build 失败: $out (exit $LASTEXITCODE)" }
    }

    Build-Target "windows" "amd64" ".exe" ""
    Build-Target "windows" "arm64" ".exe" ""
    Build-Target "linux" "amd64" "" ""
    Build-Target "linux" "arm64" "" ""
    Build-Target "linux" "arm" "" '$env:GOARM = "7"'
    Build-Target "darwin" "amd64" "" ""
    Build-Target "darwin" "arm64" "" ""

    Write-Output "done"
}
finally {
    Remove-Item Env:GOOS -ErrorAction SilentlyContinue
    Remove-Item Env:GOARCH -ErrorAction SilentlyContinue
    Remove-Item Env:GOARM -ErrorAction SilentlyContinue
    Pop-Location
}
