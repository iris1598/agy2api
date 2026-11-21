#!/bin/bash
set -e  # Exit on error
set -u  # Exit on undefined variable
set -o pipefail  # Exit on pipe failure

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_debug() {
    echo -e "${BLUE}[DEBUG]${NC} $1"
}

# Cleanup function for error handling
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        log_error "Installation failed with exit code $exit_code"
    fi
    exit $exit_code
}

trap cleanup EXIT

# Detect OS and distribution
detect_os() {
    log_info "Detecting operating system..."
    
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS_NAME=$ID
            OS_VERSION=$VERSION_ID
            log_info "Detected: $NAME $VERSION_ID"
        elif [ -f /etc/lsb-release ]; then
            . /etc/lsb-release
            OS_NAME=$DISTRIB_ID
            OS_VERSION=$DISTRIB_RELEASE
            log_info "Detected: $DISTRIB_ID $DISTRIB_RELEASE"
        else
            OS_NAME="linux"
            OS_VERSION="unknown"
            log_warn "Could not determine specific Linux distribution"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS_NAME="macos"
        OS_VERSION=$(sw_vers -productVersion)
        log_info "Detected: macOS $OS_VERSION"
    elif [[ "$OSTYPE" == "freebsd"* ]]; then
        OS_NAME="freebsd"
        OS_VERSION=$(freebsd-version)
        log_info "Detected: FreeBSD $OS_VERSION"
    else
        log_error "Unsupported operating system: $OSTYPE"
        exit 1
    fi
}

# Check for root privileges (only for Linux package managers that need it)
check_root_if_needed() {
    if [[ "$OS_NAME" == "ubuntu" ]] || [[ "$OS_NAME" == "debian" ]] || [[ "$OS_NAME" == "linuxmint" ]] || [[ "$OS_NAME" == "kali" ]]; then
        if [ "$EUID" -ne 0 ]; then
            log_error "This script requires root privileges for apt. Please run with sudo."
            exit 1
        fi
    elif [[ "$OS_NAME" == "fedora" ]] || [[ "$OS_NAME" == "rhel" ]] || [[ "$OS_NAME" == "centos" ]] || [[ "$OS_NAME" == "rocky" ]] || [[ "$OS_NAME" == "almalinux" ]]; then
        if [ "$EUID" -ne 0 ]; then
            log_error "This script requires root privileges for dnf/yum. Please run with sudo."
            exit 1
        fi
    elif [[ "$OS_NAME" == "arch" ]] || [[ "$OS_NAME" == "manjaro" ]]; then
        if [ "$EUID" -ne 0 ]; then
            log_error "This script requires root privileges for pacman. Please run with sudo."
            exit 1
        fi
    fi
}

# Update package manager
update_packages() {
    log_info "Updating package manager..."
    
    case "$OS_NAME" in
        ubuntu|debian|linuxmint|kali|pop)
            if ! apt update; then
                log_error "Failed to update apt package lists"
                exit 1
            fi
            ;;
        fedora|rhel|centos|rocky|almalinux)
            if command -v dnf &> /dev/null; then
                if ! dnf check-update; then
                    # dnf check-update returns 100 if updates are available, which is not an error
                    if [ $? -ne 100 ]; then
                        log_warn "dnf check-update returned non-standard exit code"
                    fi
                fi
            else
                if ! yum check-update; then
                    if [ $? -ne 100 ]; then
                        log_warn "yum check-update returned non-standard exit code"
                    fi
                fi
            fi
            ;;
        arch|manjaro)
            if ! pacman -Syu; then
                log_error "Failed to update pacman database"
                exit 1
            fi
            ;;
        macos)
            if command -v brew &> /dev/null; then
                log_info "Updating Homebrew..."
                brew update
            else
                log_warn "Homebrew not installed. Skipping package manager update."
            fi
            ;;
        *)
            log_warn "Unknown package manager for $OS_NAME. Skipping update."
            ;;
    esac
}

# Install git based on OS
install_git() {
    if ! command -v git &> /dev/null; then
        log_info "Installing git..."
        
        case "$OS_NAME" in
            ubuntu|debian|linuxmint|kali|pop)
                if ! apt install git -y; then
                    log_error "Failed to install git"
                    exit 1
                fi
                ;;
            fedora|rhel|centos|rocky|almalinux)
                if command -v dnf &> /dev/null; then
                    if ! dnf install git -y; then
                        log_error "Failed to install git"
                        exit 1
                    fi
                else
                    if ! yum install git -y; then
                        log_error "Failed to install git"
                        exit 1
                    fi
                fi
                ;;
            arch|manjaro)
                if ! pacman -S git --noconfirm; then
                    log_error "Failed to install git"
                    exit 1
                fi
                ;;
            macos)
                if command -v brew &> /dev/null; then
                    if ! brew install git; then
                        log_error "Failed to install git"
                        exit 1
                    fi
                else
                    log_error "Homebrew is required for macOS. Install from https://brew.sh/"
                    exit 1
                fi
                ;;
            *)
                log_error "Don't know how to install git on $OS_NAME"
                exit 1
                ;;
        esac
    else
        log_info "Git is already installed ($(git --version))"
    fi
}

# Detect OS first
detect_os

# Check root if needed
check_root_if_needed

log_info "Starting installation process..."

# Update package lists
update_packages

# Install git
install_git

# Install uv if not present
if ! command -v uv &> /dev/null; then
    log_info "Installing uv package manager..."
    if ! curl -Ls https://astral.sh/uv/install.sh | sh; then
        log_error "Failed to install uv"
        exit 1
    fi
    
    # Source environment
    if [ -f "$HOME/.local/bin/env" ]; then
        source "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        source "$HOME/.cargo/env"
    fi
    
    # Verify uv installation
    if ! command -v uv &> /dev/null; then
        log_error "uv installation failed - command not found after install"
        exit 1
    fi
else
    log_info "uv is already installed"
fi

# Determine working directory
log_info "Checking project directory..."
if [ -f "./web.py" ]; then
    log_info "Already in target directory"
elif [ -f "./agy2api/web.py" ]; then
    log_info "Changing to agy2api directory"
    cd ./agy2api || exit 1
else
    log_info "Cloning repository..."
    if [ -d "./agy2api" ]; then
        log_warn "agy2api directory exists but web.py not found. Removing and re-cloning..."
        rm -rf ./agy2api
    fi
    
    if ! git clone https://github.com/iris1598/agy2api.git agy2api; then
        log_error "Failed to clone repository"
        exit 1
    fi
    
    cd ./agy2api || exit 1
fi

# Update repository if it's a git repo
if [ -d ".git" ]; then
    log_info "Updating repository..."
    if ! git pull; then
        log_warn "Git pull failed, continuing anyway..."
    fi
else
    log_warn "Not a git repository, skipping update"
fi

# Create relocatable virtual environment to ensure portability
log_info "Creating relocatable virtual environment..."
export UV_VENV_CLEAR=1
if ! uv venv --relocatable; then
    log_error "Failed to create virtual environment"
    exit 1
fi

# Sync dependencies
log_info "Syncing dependencies with uv..."
if ! uv sync; then
    log_error "Failed to sync dependencies"
    exit 1
fi

# Activate virtual environment
log_info "Activating virtual environment..."
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    log_error "Virtual environment not found at .venv/bin/activate"
    exit 1
fi

# Verify Python is available
if ! command -v python3 &> /dev/null; then
    log_error "python3 not found in virtual environment"
    exit 1
fi

# Check if web.py exists
if [ ! -f "web.py" ]; then
    log_error "web.py not found in current directory"
    exit 1
fi

# ==================== Go 出站核心（严格模式必需） ====================
REPO_SLUG="iris1598/agy2api"
FORWARDER_BIN_DIR="tools/upstream_tls_proxy/bin"

forwarder_hint() {
    log_warn "严格模式（STRICT_FIDELITY 默认开启）要求 Go 出站核心，缺失时服务会拒绝启动。"
    log_warn "解决方式任选其一:"
    log_warn "  1) 手动下载 Release 产物放入 ${FORWARDER_BIN_DIR}/: https://github.com/${REPO_SLUG}/releases"
    log_warn "  2) 安装 Go >= 1.26 后本地编译: bash tools/upstream_tls_proxy/build.sh"
    log_warn "  3) 使用 Docker 部署（镜像已内置 Go 出站核心）"
}

detect_forwarder_asset() {
    case "$(uname -s)" in
        Linux)  local goos="linux" ;;
        Darwin) local goos="darwin" ;;
        *) echo ""; return ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64)   local arch="amd64" ;;
        aarch64|arm64)  local arch="arm64" ;;
        armv7*|armv6*)  local arch="arm" ;;
        *) echo ""; return ;;
    esac
    echo "upstream-proxy-${goos}-${arch}"
}

download_forwarder() {
    local asset="$1"
    local url="https://github.com/${REPO_SLUG}/releases/latest/download/${asset}"
    log_info "从 GitHub Release 下载 Go 出站核心: $asset ..."
    mkdir -p "$FORWARDER_BIN_DIR"
    if ! curl -fLsS "$url" -o "$FORWARDER_BIN_DIR/$asset"; then
        log_warn "下载失败（可能尚无 Release 或网络不可达）"
        rm -f "$FORWARDER_BIN_DIR/$asset"
        return 1
    fi
    chmod +x "$FORWARDER_BIN_DIR/$asset"

    # macOS: 清除可能的 quarantine 属性（浏览器代理下载场景）
    if [ "$(uname -s)" = "Darwin" ] && command -v xattr &> /dev/null; then
        xattr -d com.apple.quarantine "$FORWARDER_BIN_DIR/$asset" 2>/dev/null || true
    fi

    # SHA256 校验（Release 附带 SHA256SUMS.txt；不匹配视为致命）
    if curl -fLsS "https://github.com/${REPO_SLUG}/releases/latest/download/SHA256SUMS.txt" \
        -o /tmp/agy2api-SHA256SUMS.txt 2>/dev/null; then
        local expected actual
        expected=$(awk -v a="$asset" '$2==a {print $1}' /tmp/agy2api-SHA256SUMS.txt)
        if command -v sha256sum &> /dev/null; then
            actual=$(sha256sum "$FORWARDER_BIN_DIR/$asset" | awk '{print $1}')
        else
            actual=$(shasum -a 256 "$FORWARDER_BIN_DIR/$asset" | awk '{print $1}')
        fi
        rm -f /tmp/agy2api-SHA256SUMS.txt
        if [ -z "$expected" ]; then
            log_warn "SHA256SUMS.txt 中没有 $asset 的记录，跳过校验"
        elif [ "$expected" != "$actual" ]; then
            log_error "SHA256 校验失败（期望 $expected，实际 $actual）"
            rm -f "$FORWARDER_BIN_DIR/$asset"
            exit 1
        else
            log_info "SHA256 校验通过"
        fi
    else
        log_warn "无法下载 SHA256SUMS.txt，跳过校验"
    fi
    log_info "Go 出站核心就绪: $FORWARDER_BIN_DIR/$asset"
    return 0
}

ensure_forwarder_binary() {
    local plain="$FORWARDER_BIN_DIR/upstream-proxy"
    if [ -f "$plain" ]; then
        log_info "Go 出站核心已存在: $plain"
        return 0
    fi
    local asset
    asset=$(detect_forwarder_asset)
    if [ -n "$asset" ] && download_forwarder "$asset"; then
        return 0
    fi
    # 下载失败时尝试本地编译（有 Go 环境就顺手补上）
    if command -v go &> /dev/null || [ -x /usr/local/go/bin/go ]; then
        log_info "尝试本地编译 Go 出站核心..."
        if bash tools/upstream_tls_proxy/build.sh; then
            return 0
        fi
        log_warn "本地编译失败"
    fi
    forwarder_hint
    log_error "未获得 Go 出站核心，退出安装。"
    exit 1
}

ensure_forwarder_binary

# Start the application
log_info "Starting application..."
python3 web.py