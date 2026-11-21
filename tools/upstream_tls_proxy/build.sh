#!/usr/bin/env bash
# 编译 Go 出站 TLS 转发器（需要 Go >= 1.26）
#
# 用法:  bash tools/upstream_tls_proxy/build.sh
#        GO_BIN=/usr/local/go/bin/go bash tools/upstream_tls_proxy/build.sh
# 产物:  tools/upstream_tls_proxy/bin/upstream-proxy
set -euo pipefail

find_go() {
  if [ -n "${GO_BIN:-}" ]; then
    echo "$GO_BIN"
    return
  fi
  if command -v go >/dev/null 2>&1; then
    command -v go
    return
  fi
  for candidate in /usr/local/go/bin/go "$HOME/go/bin/go" /opt/homebrew/bin/go /usr/bin/go; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return
    fi
  done
  cat >&2 <<'EOF'
未找到 go 命令。
  1) 安装 Go >= 1.26（https://golang.google.cn/dl/ 或系统包管理器）
  2) 已有 Go 但不在 PATH：GO_BIN=/path/to/go bash tools/upstream_tls_proxy/build.sh
如果只是要用抓包/反代，仓库里的 bin/upstream-proxy 已经可用，无需重新编译。
EOF
  exit 1
}

GO_EXE=$(find_go)
cd "$(dirname "$0")"
mkdir -p bin
CGO_ENABLED=0 "$GO_EXE" build -trimpath -ldflags "-s -w" -o bin/upstream-proxy .
echo "built: $(pwd)/bin/upstream-proxy"
