# Go 出站 TLS 转发器（upstream_tls_proxy）

agy2api 的 TLS 出站层。真实 Antigravity `language_server` 是 Go 写的，用 Go 的
`crypto/tls` 出站时 ClientHello 与实抓**逐段一致**：

```
JA3 md5 : 03117a8ed39ef02427ebbc39f121275c
JA3 full: 771,49195-49199-49196-49200-52393-52392-49161-49171-49162-49172-4865-4866-4867,0-11-65281-23-18-5-10-13-50-16-43-51,4588-4587-4589-29-23-24-25,0
ALPN    : http/1.1
```

Python 的 httpx/OpenSSL 无法产出这组指纹（尤其后量子曲线组 4588/4587/4589），
所以把"出站 TLS"交给本程序，Python 只负责请求构造、SSE 解析与格式转换。

## 工作方式

本程序在 `127.0.0.1` 起一个**反向代理**，Python 把原始 URL 塞进请求头：

```
POST http://127.0.0.1:17890/v1internal:streamGenerateContent?alt=sse
X-Upstream-URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
X-Upstream-Proxy: http://127.0.0.1:7897      （可选，账号独立出口 / Clash）
X-Upstream-SNI: daily-cloudcode-pa.googleapis.com   （可选，目标为 IP 时补 SNI）
```

然后本程序：

1. 用 Go `crypto/tls` 建连（ALPN 只给 `http/1.1`，曲线组显式钉成实测顺序）；
2. 手写 HTTP/1.1 请求行与 header 顺序
   （`Host → User-Agent → Transfer-Encoding → Authorization → Content-Type → Accept-Encoding → 其余`）；
3. 请求体以 chunked 流式转发，响应体逐块读、逐块 flush（SSE 实时到达，不做缓冲）；
4. 支持 `http(s)://` CONNECT 代理与 `socks5/socks5h` 上游代理。

`X-Upstream-*` 头不会被转发给上游。

## 构建

需要 **Go ≥ 1.26**（`SecP256r1MLKEM768` / `SecP384r1MLKEM1024` 常量）。

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build.ps1
# 产物：tools/upstream_tls_proxy/bin/upstream-proxy.exe
```

```bash
bash tools/upstream_tls_proxy/build.sh
# 产物：tools/upstream_tls_proxy/bin/upstream-proxy
```

Docker 镜像通过多阶段构建自动编译并内置该二进制。

## 与主服务的关系

- 默认开启（`UPSTREAM_TLS_IMPERSONATION=true`）。Python 首次发往 Antigravity API 请求时
  自动拉起本程序（端口取 `UPSTREAM_TLS_PROXY_URL`，默认 `127.0.0.1:17890`）。
- 二进制缺失 / 启动失败 / 端口不可用 → 自动回退 httpx 直连，并只记一条警告，
  不阻塞服务。已收到流数据后绝不重试。
- 应用关闭时会终止由主进程拉起的转发器；你手动启动的实例不会被杀，且会被复用。

## 验证

```powershell
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py verify --transport go
```

会本地起一个只读 ClientHello 的监听器，让本转发器去连它，逐段比对 JA3/ALPN；
同时抓取转发器写出的上游 header 顺序。全部匹配时退出码为 0。

## 安全边界

- 只监听 `127.0.0.1`，拒绝非回环客户端；
- 不落盘任何请求/响应内容；
- 不修改客户端、不安装 CA、不做中间人解密。
