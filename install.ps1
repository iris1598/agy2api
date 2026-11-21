# 检测是否为管理员
$IsElevated = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).
    IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Skip Scoop install if already present to avoid stopping the script
if (Get-Command scoop -ErrorAction SilentlyContinue) {
    Write-Host "Scoop is already installed. Skipping installation."
} else {
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    if ($IsElevated) {
        # 管理员：使用官方一行命令并传入 -RunAsAdmin
        Invoke-Expression "& {$(Invoke-RestMethod get.scoop.sh)} -RunAsAdmin"
    } else {
        # 普通用户安装
        Invoke-WebRequest -useb get.scoop.sh | Invoke-Expression
    }
}

scoop install git uv
if (Test-Path -LiteralPath "./web.py") {
    # Already in target directory; skip clone and cd
}
elseif (Test-Path -LiteralPath "./agy2api/web.py") {
    Set-Location ./agy2api
}
else {
    git clone https://github.com/iris1598/agy2api.git agy2api
    Set-Location ./agy2api
}
# Create relocatable virtual environment to ensure portability
$env:UV_VENV_CLEAR = "1"
uv venv --relocatable
uv sync
.venv/Scripts/activate.ps1

# ==================== Go 出站核心（严格模式必需） ====================
$RepoSlug = "iris1598/agy2api"
$ForwarderBinDir = "tools/upstream_tls_proxy/bin"
switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $ForwarderAsset = "upstream-proxy-windows-amd64.exe" }
    "ARM64" { $ForwarderAsset = "upstream-proxy-windows-arm64.exe" }
    default { $ForwarderAsset = "" }
}

if (Test-Path -LiteralPath "$ForwarderBinDir/upstream-proxy.exe") {
    Write-Host "Go 出站核心已存在: $ForwarderBinDir/upstream-proxy.exe"
}
elseif ($ForwarderAsset) {
    Write-Host "从 GitHub Release 下载 Go 出站核心: $ForwarderAsset ..."
    New-Item -ItemType Directory -Force -Path $ForwarderBinDir | Out-Null
    $assetPath = Join-Path $ForwarderBinDir $ForwarderAsset
    try {
        Invoke-WebRequest -useb "https://github.com/$RepoSlug/releases/latest/download/$ForwarderAsset" -OutFile $assetPath
    } catch {
        Write-Warning "下载失败（可能尚无 Release 或网络不可达）: $_"
    }
    if (Test-Path -LiteralPath $assetPath) {
        try {
            $sums = (Invoke-WebRequest -useb "https://github.com/$RepoSlug/releases/latest/download/SHA256SUMS.txt").Content
            $expected = ($sums -split "`n" |
                Where-Object { $_ -match [regex]::Escape($ForwarderAsset) } |
                ForEach-Object { ($_ -split '\s+')[0] } |
                Select-Object -First 1)
            $actual = (Get-FileHash $assetPath -Algorithm SHA256).Hash.ToLower()
            if ($expected -and $expected.ToLower() -ne $actual) {
                Write-Error "SHA256 校验失败（期望 $expected，实际 $actual）"
                Remove-Item $assetPath
                exit 1
            }
            if ($expected) { Write-Host "SHA256 校验通过" }
        } catch {
            Write-Warning "无法下载 SHA256SUMS.txt，跳过校验"
        }
        Write-Host "Go 出站核心就绪: $assetPath"
    } else {
        Write-Warning "严格模式（STRICT_FIDELITY 默认开启）要求 Go 出站核心，缺失时服务会拒绝启动。"
        Write-Warning "解决方式任选其一:"
        Write-Warning "  1) 手动下载 Release 产物放入 ${ForwarderBinDir}/: https://github.com/$RepoSlug/releases"
        Write-Warning "  2) 安装 Go >= 1.26 后本地编译: powershell -ExecutionPolicy Bypass -File tools/upstream_tls_proxy/build.ps1"
        Write-Warning "  3) 使用 Docker 部署（镜像已内置 Go 出站核心）"
        exit 1
    }
}
else {
    Write-Warning "无法识别 CPU 架构（$env:PROCESSOR_ARCHITECTURE），请手动下载产物放入 $ForwarderBinDir/"
    Write-Warning "https://github.com/$RepoSlug/releases"
    exit 1
}

python web.py