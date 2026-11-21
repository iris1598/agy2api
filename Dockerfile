# ---- Go 出站 TLS 转发器（与真实客户端同栈，ClientHello/JA3 逐段一致）----
# 需要 Go >= 1.26（SecP256r1MLKEM768 / SecP384r1MLKEM1024 曲线常量）。
FROM golang:1.27-alpine AS gobuild
WORKDIR /src
COPY tools/upstream_tls_proxy/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/upstream-proxy .

# Multi-stage build for agy2api
FROM python:3.13-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# Install tzdata, jemalloc and set timezone
# jemalloc 替代 glibc malloc，大幅减少内存碎片化
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata libjemalloc2 && \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 使用 jemalloc 替代 glibc malloc
# jemalloc 会主动将空闲内存归还给操作系统，避免 RSS 持续增长
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
# jemalloc 配置：启用后台线程回收、脏页衰减时间 5 秒（默认 10 秒）
ENV MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000"

WORKDIR /app

# Copy only requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Go 出站 TLS 转发器（Python 侧找不到二进制时自动回退 httpx）
COPY --from=gobuild /out/upstream-proxy /app/tools/upstream_tls_proxy/bin/upstream-proxy

# Expose port
EXPOSE 7861

# 通过 web.py 启动，使 HOST/PORT 环境变量生效
CMD ["python", "web.py"]
