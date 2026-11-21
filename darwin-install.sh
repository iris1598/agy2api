#!/bin/bash
# macOS 安装脚本 (支持 Intel 和 Apple Silicon)

# 确保 Homebrew 已安装
if ! command -v brew &> /dev/null; then
    echo "未检测到 Homebrew，开始安装..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    
    # 检测 Homebrew 安装路径并设置环境变量
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        # Apple Silicon Mac
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f "/usr/local/bin/brew" ]]; then
        # Intel Mac
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

# 更新 brew 并安装 git
brew update
brew install git

# 安装 uv (Python 环境管理工具)
curl -Ls https://astral.sh/uv/install.sh | sh

# 确保 uv 在 PATH 中
export PATH="$HOME/.local/bin:$PATH"

# 克隆或进入项目目录
if [ -f "./web.py" ]; then
    # 已经在目标目录
    :
elif [ -f "./agy2api/web.py" ]; then
    cd ./agy2api
else
    git clone https://github.com/iris1598/agy2api.git agy2api
    cd ./agy2api
fi

# 拉取最新代码
git pull

# 创建并同步虚拟环境
uv sync

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "❌ 未找到虚拟环境，请检查 uv 是否安装成功"
    exit 1
fi

# ==================== Go 出站核心（严格模式必需） ====================
REPO_SLUG="iris1598/agy2api"
FORWARDER_BIN_DIR="tools/upstream_tls_proxy/bin"
case "$(uname -m)" in
    x86_64|amd64)  FORWARDER_ASSET="upstream-proxy-darwin-amd64" ;;
    aarch64|arm64) FORWARDER_ASSET="upstream-proxy-darwin-arm64" ;;
    *)             FORWARDER_ASSET="" ;;
esac

if [ -f "$FORWARDER_BIN_DIR/upstream-proxy" ]; then
    echo "✅ Go 出站核心已存在: $FORWARDER_BIN_DIR/upstream-proxy"
elif [ -n "$FORWARDER_ASSET" ]; then
    echo "正在从 GitHub Release 下载 Go 出站核心: $FORWARDER_ASSET ..."
    mkdir -p "$FORWARDER_BIN_DIR"
    if curl -fLsS "https://github.com/${REPO_SLUG}/releases/latest/download/${FORWARDER_ASSET}" \
        -o "$FORWARDER_BIN_DIR/$FORWARDER_ASSET"; then
        chmod +x "$FORWARDER_BIN_DIR/$FORWARDER_ASSET"
        # 清除可能的 quarantine 属性（浏览器代理下载场景）
        xattr -d com.apple.quarantine "$FORWARDER_BIN_DIR/$FORWARDER_ASSET" 2>/dev/null || true
        if curl -fLsS "https://github.com/${REPO_SLUG}/releases/latest/download/SHA256SUMS.txt" \
            -o /tmp/agy2api-SHA256SUMS.txt 2>/dev/null; then
            expected=$(awk -v a="$FORWARDER_ASSET" '$2==a {print $1}' /tmp/agy2api-SHA256SUMS.txt)
            actual=$(shasum -a 256 "$FORWARDER_BIN_DIR/$FORWARDER_ASSET" | awk '{print $1}')
            rm -f /tmp/agy2api-SHA256SUMS.txt
            if [ -n "$expected" ] && [ "$expected" != "$actual" ]; then
                echo "❌ SHA256 校验失败（期望 $expected，实际 $actual）"
                rm -f "$FORWARDER_BIN_DIR/$FORWARDER_ASSET"
                exit 1
            fi
            [ -n "$expected" ] && echo "✅ SHA256 校验通过"
        else
            echo "⚠️ 无法下载 SHA256SUMS.txt，跳过校验"
        fi
        echo "✅ Go 出站核心就绪: $FORWARDER_BIN_DIR/$FORWARDER_ASSET"
    else
        rm -f "$FORWARDER_BIN_DIR/$FORWARDER_ASSET"
        echo "❌ 下载失败（可能尚无 Release 或网络不可达），退出安装。"
        echo "   严格模式下 Go 出站核心缺失会导致服务拒绝启动。解决方式："
        echo "   1) 手动下载: https://github.com/${REPO_SLUG}/releases → 放入 ${FORWARDER_BIN_DIR}/"
        echo "   2) 本地编译（brew install go）: bash tools/upstream_tls_proxy/build.sh"
        echo "   3) 关闭严格模式（不推荐）: STRICT_FIDELITY=false"
        exit 1
    fi
else
    echo "❌ 无法识别架构（uname -m = $(uname -m)），请手动下载产物放入 ${FORWARDER_BIN_DIR}/"
    echo "   https://github.com/${REPO_SLUG}/releases"
    exit 1
fi

# 启动项目
python3 web.py
