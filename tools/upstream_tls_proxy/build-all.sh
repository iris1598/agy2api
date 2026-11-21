#!/usr/bin/env bash
# 交叉编译全部发布目标（产物在 bin/，文件名带平台后缀）。
# 需要 Go >= 1.26；纯静态（CGO_ENABLED=0），可在 musl/Termux 上直接运行。
set -euo pipefail

find_go() {
  if [ -n "${GO_BIN:-}" ]; then
    echo "$GO_BIN"; return
  fi
  if command -v go >/dev/null 2>&1; then
    command -v go; return
  fi
  for candidate in /usr/local/go/bin/go "$HOME/go/bin/go" /opt/homebrew/bin/go /usr/bin/go; do
    if [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
  echo "未找到 go 命令。安装 Go >= 1.26，或用 GO_BIN=/path/to/go 指定。" >&2
  exit 1
}

GO_EXE=$(find_go)
cd "$(dirname "$0")"
mkdir -p bin

build() {
  local goos=$1 goarch=$2 suffix=$3 extra=${4:-}
  local out="bin/upstream-proxy-${goos}-${goarch}${suffix}"
  echo "building ${out}"
  # extra 必须经 env 传递：$extra 展开出的 "GOARM=7" 不经 env 时会被 bash
  # 当成命令名（解析期只认字面 NAME=value 词为赋值前缀），CI 实测报
  # "GOARM=7: command not found"。
  # shellcheck disable=SC2086
  env CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" $extra \
    "$GO_EXE" build -trimpath -ldflags "-s -w" -o "$out" .
}

build windows amd64 .exe
build windows arm64 .exe
build linux amd64 ""
build linux arm64 ""
build linux arm "" "GOARM=7"
build darwin amd64 ""
build darwin arm64 ""

echo "done"
