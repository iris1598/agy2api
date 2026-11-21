// upstream_tls_proxy 是 agy2api 的 Go 出站 TLS 转发器。
//
// 为什么用 Go：真实 Antigravity language_server 就是 Go 写的，用 Go 的
// crypto/tls 出站时，ClientHello（密码套件顺序、扩展集合、后量子曲线组
// 4588/4587/4589、ALPN）天然与真实客户端一致，且 Go 原生支持流式读取。
// Python 侧不需要伪造 TLS。
//
// 工作方式（反向代理形态，Python 只改内部 URL 拼接，调用方无感）：
//
//	POST http://127.0.0.1:17890/v1internal:streamGenerateContent?alt=sse
//	X-Upstream-URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
//	X-Upstream-Proxy: http://127.0.0.1:7897     （可选，账号独立出口）
//
// 本进程用 Go crypto/tls 连接目标，手写 HTTP/1.1 请求行与 header 顺序
// （对齐实抓：Host → User-Agent → Transfer-Encoding → Authorization →
// Content-Type → Accept-Encoding），请求体与响应体全程流式转发，
// SSE 逐块 flush，不做任何缓冲。
//
// 安全边界：只监听 127.0.0.1，拒绝非回环客户端；不落盘任何请求内容；
// 不修改客户端、不装 CA。
package main

import (
	"bufio"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	dialTimeout      = 30 * time.Second
	handshakeTimeout = 30 * time.Second
	headerTimeout    = 600 * time.Second
	chunkSize        = 32 * 1024
	// 单请求体上限（缓冲后用于"复用连接失效时重发一次"）
	maxRequestBodyBytes = 64 << 20

	headerUpstreamURL   = "X-Upstream-URL"
	headerUpstreamProxy = "X-Upstream-Proxy"
	headerUpstreamSNI   = "X-Upstream-SNI"
	// 置位时请求体不是最终报文,而是"内层值片 + 锚点"的 wrapper,
	// 由本进程构造信封(envelope.go,上游报文唯一作者)。
	headerUpstreamBuildEnvelope = "X-Upstream-Build-Envelope"
)

// 真实客户端实测的 header 顺序（Host 与 Transfer-Encoding 由本进程生成）。
var fixedHeaderOrder = []string{
	"User-Agent",
	"Transfer-Encoding",
	"Authorization",
	"Content-Type",
	"Accept-Encoding",
}

// 不转发给上游的逐跳/本地头。
var skipHeaders = map[string]bool{
	"host":                true,
	"content-length":      true,
	"transfer-encoding":   true,
	"connection":          true,
	"keep-alive":          true,
	"proxy-connection":    true,
	"proxy-authorization": true,
	"te":                  true,
	"trailer":             true,
	"upgrade":             true,
		"x-upstream-url":      true,
		"x-upstream-proxy":    true,
		"x-upstream-sni":      true,
		"x-upstream-build-envelope": true,
	// httpx 会自动补 Accept: */*，而实测真实客户端不发这个头 —— 这里统一丢弃。
	"accept": true,
}

func main() {
	listen := flag.String("listen", envOr("UPSTREAM_TLS_PROXY_LISTEN", "127.0.0.1:17890"), "监听地址")
	verbose := flag.Bool("v", false, "输出每个请求的概要日志")
	flag.Parse()

	srv := &http.Server{
		Addr:              *listen,
		Handler:           &forwarder{verbose: *verbose},
		ReadHeaderTimeout: 15 * time.Second,
	}
	log.Printf("[upstream-proxy] listening on %s (%s)", *listen, runtimeVersion())
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("[upstream-proxy] serve failed: %v", err)
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type forwarder struct {
	verbose bool
}

func (f *forwarder) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !isLoopback(r.RemoteAddr) {
		http.Error(w, `{"error":"only loopback clients are allowed"}`, http.StatusForbidden)
		return
	}
	if r.URL.Path == "/__health" {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(
			w,
			`{"status":"ok","protocol":%d,"go":%q,"alpn":["http/1.1"],"idle_connections":%d}`,
			envelopeProtocolVersion, runtimeVersion(), idleConnectionCount(),
		)
		return
	}

	rawTarget := r.Header.Get(headerUpstreamURL)
	if rawTarget == "" {
		http.Error(w, `{"error":"missing X-Upstream-URL"}`, http.StatusBadRequest)
		return
	}
	upstreamProxy := r.Header.Get(headerUpstreamProxy)
	upstreamSNI := r.Header.Get(headerUpstreamSNI)

	target, err := url.Parse(rawTarget)
	if err != nil || target.Host == "" {
		http.Error(w, `{"error":"invalid X-Upstream-URL"}`, http.StatusBadRequest)
		return
	}

	started := time.Now()
	status, err := f.forward(w, r, target, upstreamProxy, upstreamSNI)
	if err != nil {
		log.Printf("[upstream-proxy] %s %s failed: %v", r.Method, target.Redacted(), err)
		return
	}
	if f.verbose {
		log.Printf("[upstream-proxy] %s %s -> %d (%s)",
			r.Method, target.Path, status, time.Since(started).Round(time.Millisecond))
	}
}

func (f *forwarder) forward(host http.ResponseWriter, incoming *http.Request, target *url.URL, proxyRaw, sniOverride string) (int, error) {
	hasBody := requestHasBody(incoming)
	// 先缓冲请求体：复用连接可能已被上游关闭，需要在"写失败/读失败"时
	// 用同一条请求在新连接上重试一次。同时限制单请求体大小，避免异常内存占用。
	// 缓冲与信封构造都在取连接之前完成，构造失败不浪费新建的 TLS 连接。
	var bodyBytes []byte
	var bodyErr error
	if hasBody {
		bodyBytes, bodyErr = io.ReadAll(io.LimitReader(incoming.Body, maxRequestBodyBytes))
		if bodyErr != nil {
			http.Error(host, `{"error":"read request body failed"}`, http.StatusBadRequest)
			return 0, bodyErr
		}
	}

	// 信封模式:请求体是"内层值片 + 锚点"wrapper,由本进程构造最终上游报文
	// (唯一作者,见 envelope.go)。构造必须发生在 writeAll 之前——重试路径
	// 重发的是构造后的字节,天然幂等(同一 body 只构造一次,不会出现第二个 requestId)。
	if incoming.Header.Get(headerUpstreamBuildEnvelope) != "" {
		if !hasBody {
			http.Error(host, `{"error":"envelope mode requires a body"}`, http.StatusBadRequest)
			return 0, errors.New("envelope mode without body")
		}
		built, buildErr := buildEnvelope(bodyBytes)
		if buildErr != nil {
			log.Printf("[upstream-proxy] envelope build failed: %v", buildErr)
			http.Error(host, `{"error":"envelope build failed"}`, http.StatusBadRequest)
			return 0, buildErr
		}
		bodyBytes = built
	}

	key := poolKey(target, proxyRaw, sniOverride)
	pooled, reused, err := acquireConn(target, proxyRaw, sniOverride)
	if err != nil {
		http.Error(host, `{"error":"upstream connect failed"}`, http.StatusBadGateway)
		return 0, err
	}

	writeAll := func(pc *pooledConn) error {
		if err := writeRequestHead(pc.conn, incoming, target, hasBody); err != nil {
			return err
		}
		if hasBody {
			return writeChunkedBody(pc.conn, bodyBytes)
		}
		return nil
	}

	if writeErr := writeAll(pooled); writeErr != nil {
		pooled.conn.Close()
		if !reused {
			http.Error(host, `{"error":"upstream write failed"}`, http.StatusBadGateway)
			return 0, fmt.Errorf("write request: %w", writeErr)
		}
		pooled, err = acquireConnFresh(target, proxyRaw, sniOverride)
		if err != nil {
			http.Error(host, `{"error":"upstream connect failed"}`, http.StatusBadGateway)
			return 0, err
		}
		if writeErr = writeAll(pooled); writeErr != nil {
			pooled.conn.Close()
			http.Error(host, `{"error":"upstream write failed"}`, http.StatusBadGateway)
			return 0, fmt.Errorf("write request: %w", writeErr)
		}
	}

	readResponse := func(pc *pooledConn) (*http.Response, error) {
		_ = pc.conn.SetReadDeadline(time.Now().Add(headerTimeout))
		return http.ReadResponse(pc.br, &http.Request{Method: incoming.Method})
	}

	resp, err := readResponse(pooled)
	if err != nil && reused {
		// 复用的空闲连接可能已被对端关闭（写成功但读 EOF）。
		// 丢弃后换新连接，用已缓冲的请求体重发一次。
		pooled.conn.Close()
		fresh, freshErr := acquireConnFresh(target, proxyRaw, sniOverride)
		if freshErr == nil {
			pooled = fresh
			if writeErr := writeAll(pooled); writeErr == nil {
				resp, err = readResponse(pooled)
			}
		}
	}
	if err != nil {
		pooled.conn.Close()
		http.Error(host, `{"error":"upstream response read failed"}`, http.StatusBadGateway)
		return 0, err
	}
	defer resp.Body.Close()
	_ = pooled.conn.SetReadDeadline(time.Time{})

	copyResponseHeaders(host.Header(), resp.Header)
	host.WriteHeader(resp.StatusCode)
	flusher, _ := host.(http.Flusher)
	if flusher != nil {
		flusher.Flush()
	}

	buf := make([]byte, chunkSize)
	reusable := !resp.Close
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, writeErr := host.Write(buf[:n]); writeErr != nil {
				pooled.conn.Close() // 下游断开，无法保证连接干净
				return resp.StatusCode, nil
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			pooled.conn.Close()
			return resp.StatusCode, readErr
		}
	}
	releaseConn(key, pooled, reusable)
	return resp.StatusCode, nil
}

// ---------------------------------------------------------------------------
// 上游连接池：与真实客户端一样复用 TCP/TLS 连接（会话票据也会随之复用），
// 避免"每个请求一次全新握手"这种可观测差异。
// ---------------------------------------------------------------------------

type pooledConn struct {
	conn net.Conn
	br   *bufio.Reader
}

const maxIdlePerKey = 4

var idlePool = struct {
	sync.Mutex
	m map[string][]*pooledConn
}{m: map[string][]*pooledConn{}}

func poolKey(target *url.URL, proxyRaw, sni string) string {
	return target.Scheme + "://" + target.Host + "|" + proxyRaw + "|" + sni
}

func idleConnectionCount() int {
	idlePool.Lock()
	defer idlePool.Unlock()
	total := 0
	for _, list := range idlePool.m {
		total += len(list)
	}
	return total
}

func acquireConn(target *url.URL, proxyRaw, sni string) (*pooledConn, bool, error) {
	key := poolKey(target, proxyRaw, sni)
	idlePool.Lock()
	if list := idlePool.m[key]; len(list) > 0 {
		pc := list[len(list)-1]
		idlePool.m[key] = list[:len(list)-1]
		idlePool.Unlock()
		return pc, true, nil
	}
	idlePool.Unlock()

	pc, err := acquireConnFresh(target, proxyRaw, sni)
	if err != nil {
		return nil, false, err
	}
	return pc, false, nil
}

func acquireConnFresh(target *url.URL, proxyRaw, sni string) (*pooledConn, error) {
	conn, err := dialUpstream(target, proxyRaw, sni)
	if err != nil {
		return nil, err
	}
	return &pooledConn{conn: conn, br: bufio.NewReaderSize(conn, 64*1024)}, nil
}

func releaseConn(key string, pc *pooledConn, reusable bool) {
	if !reusable {
		pc.conn.Close()
		return
	}
	idlePool.Lock()
	defer idlePool.Unlock()
	if len(idlePool.m[key]) >= maxIdlePerKey {
		pc.conn.Close()
		return
	}
	idlePool.m[key] = append(idlePool.m[key], pc)
}

func isLoopback(remoteAddr string) bool {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func requestHasBody(r *http.Request) bool {
	if r.ContentLength > 0 {
		return true
	}
	for _, enc := range r.TransferEncoding {
		if strings.EqualFold(enc, "chunked") {
			return true
		}
	}
	return false
}

// writeRequestHead 按实测顺序手写请求行与 header：
// Host → User-Agent → Transfer-Encoding → Authorization → Content-Type → Accept-Encoding → 其余。
func writeRequestHead(w io.Writer, r *http.Request, target *url.URL, hasBody bool) error {
	var b strings.Builder
	b.WriteString(r.Method)
	b.WriteByte(' ')
	b.WriteString(target.RequestURI())
	b.WriteString(" HTTP/1.1\r\n")
	b.WriteString("Host: ")
	b.WriteString(target.Host)
	b.WriteString("\r\n")

	written := map[string]bool{"host": true, "transfer-encoding": hasBody}
	for _, name := range fixedHeaderOrder {
		lower := strings.ToLower(name)
		if lower == "transfer-encoding" {
			if hasBody {
				b.WriteString("Transfer-Encoding: chunked\r\n")
			}
			continue
		}
		if skipHeaders[lower] {
			continue
		}
		values := r.Header.Values(name)
		for _, v := range values {
			if v == "" {
				continue
			}
			b.WriteString(name)
			b.WriteString(": ")
			b.WriteString(sanitizeHeaderValue(v))
			b.WriteString("\r\n")
		}
		written[lower] = true
	}

	// 其余头按名字排序追加，保证输出稳定。
	rest := make([]string, 0, len(r.Header))
	for name := range r.Header {
		lower := strings.ToLower(name)
		if skipHeaders[lower] || written[lower] {
			continue
		}
		rest = append(rest, name)
	}
	sort.Strings(rest)
	for _, name := range rest {
		for _, v := range r.Header.Values(name) {
			b.WriteString(name)
			b.WriteString(": ")
			b.WriteString(sanitizeHeaderValue(v))
			b.WriteString("\r\n")
		}
	}
	b.WriteString("\r\n")
	_, err := io.WriteString(w, b.String())
	return err
}

// sanitizeHeaderValue 去掉值里的 CR/LF，避免 header 注入（值来自本机 Python）。
func sanitizeHeaderValue(v string) string {
	return strings.NewReplacer("\r", " ", "\n", " ").Replace(v)
}

// writeChunkedBody 以 chunked 形式写出已缓冲的请求体（与实测真实客户端一致）。
func writeChunkedBody(w io.Writer, body []byte) error {
	for offset := 0; offset < len(body); offset += chunkSize {
		end := offset + chunkSize
		if end > len(body) {
			end = len(body)
		}
		piece := body[offset:end]
		if _, err := fmt.Fprintf(w, "%x\r\n", len(piece)); err != nil {
			return err
		}
		if _, err := w.Write(piece); err != nil {
			return err
		}
		if _, err := io.WriteString(w, "\r\n"); err != nil {
			return err
		}
	}
	_, err := io.WriteString(w, "0\r\n\r\n")
	return err
}

func copyResponseHeaders(dst, src http.Header) {
	for name, values := range src {
		lower := strings.ToLower(name)
		if skipHeaders[lower] && lower != "content-type" {
			// 逐跳头与上游 Content-Length 都不透传，交给下游重新分块。
			continue
		}
		for _, v := range values {
			dst.Add(name, v)
		}
	}
}

// dialUpstream 建立到目标站点的 TCP（可经上游代理）+ TLS 连接。
func dialUpstream(target *url.URL, proxyRaw, sniOverride string) (net.Conn, error) {
	host := target.Hostname()
	port := target.Port()
	if port == "" {
		if strings.EqualFold(target.Scheme, "http") {
			port = "80"
		} else {
			port = "443"
		}
	}

	var conn net.Conn
	var err error
	if proxyRaw != "" {
		conn, err = dialViaProxy(proxyRaw, host, port)
	} else {
		conn, err = net.DialTimeout("tcp", net.JoinHostPort(host, port), dialTimeout)
	}
	if err != nil {
		return nil, err
	}

	// 测试/本地调试允许明文 HTTP 目标（verify 用）——此时不套 TLS。
	if strings.EqualFold(target.Scheme, "http") {
		return conn, nil
	}

	serverName := host
	if sniOverride != "" {
		serverName = sniOverride
	}
	tlsConn := tlsClient(conn, serverName)
	_ = tlsConn.SetDeadline(time.Now().Add(handshakeTimeout))
	if err := tlsConn.Handshake(); err != nil {
		conn.Close()
		return nil, fmt.Errorf("tls handshake: %w", err)
	}
	_ = tlsConn.SetDeadline(time.Time{})
	return tlsConn, nil
}

// dialViaProxy 支持 http(s) 代理的 CONNECT 与 socks5/socks5h。
func dialViaProxy(proxyRaw, host, port string) (net.Conn, error) {
	pu, err := url.Parse(proxyRaw)
	if err != nil || pu.Host == "" {
		return nil, fmt.Errorf("invalid proxy url: %q", proxyRaw)
	}
	switch strings.ToLower(pu.Scheme) {
	case "socks5", "socks5h":
		return dialSocks5(pu, host, port)
	case "http", "https", "":
		return dialHTTPProxy(pu, host, port)
	default:
		return nil, fmt.Errorf("unsupported proxy scheme: %s", pu.Scheme)
	}
}

func dialHTTPProxy(pu *url.URL, host, port string) (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", pu.Host, dialTimeout)
	if err != nil {
		return nil, err
	}
	var req strings.Builder
	fmt.Fprintf(&req, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n", net.JoinHostPort(host, port), net.JoinHostPort(host, port))
	if pu.User != nil {
		user := pu.User.Username()
		pass, _ := pu.User.Password()
		cred := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
		fmt.Fprintf(&req, "Proxy-Authorization: Basic %s\r\n", cred)
	}
	req.WriteString("\r\n")
	if _, err := io.WriteString(conn, req.String()); err != nil {
		conn.Close()
		return nil, err
	}

	_ = conn.SetReadDeadline(time.Now().Add(dialTimeout))
	br := bufio.NewReader(conn)
	tp := textproto.NewReader(br)
	line, err := tp.ReadLine()
	if err != nil {
		conn.Close()
		return nil, err
	}
	parts := strings.SplitN(line, " ", 3)
	if len(parts) < 2 || parts[1] != "200" {
		conn.Close()
		return nil, fmt.Errorf("proxy CONNECT rejected: %s", line)
	}
	_ = conn.SetReadDeadline(time.Time{})
	return conn, nil
}

func dialSocks5(pu *url.URL, host, port string) (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", pu.Host, dialTimeout)
	if err != nil {
		return nil, err
	}
	fail := func(e error) (net.Conn, error) { conn.Close(); return nil, e }

	useAuth := pu.User != nil
	methods := []byte{0x00}
	if useAuth {
		methods = []byte{0x00, 0x02}
	}
	greet := append([]byte{0x05, byte(len(methods))}, methods...)
	if _, err := conn.Write(greet); err != nil {
		return fail(err)
	}
	reply := make([]byte, 2)
	if _, err := io.ReadFull(conn, reply); err != nil {
		return fail(err)
	}
	if reply[0] != 0x05 {
		return fail(errors.New("socks5: bad version"))
	}
	if reply[1] == 0x02 {
		user := pu.User.Username()
		pass, _ := pu.User.Password()
		auth := []byte{0x01, byte(len(user))}
		auth = append(auth, user...)
		auth = append(auth, byte(len(pass)))
		auth = append(auth, pass...)
		if _, err := conn.Write(auth); err != nil {
			return fail(err)
		}
		if _, err := io.ReadFull(conn, reply); err != nil {
			return fail(err)
		}
		if reply[1] != 0x00 {
			return fail(errors.New("socks5: auth failed"))
		}
	} else if reply[1] != 0x00 {
		return fail(errors.New("socks5: no acceptable auth method"))
	}

	// CONNECT 请求（域名交给代理解析，兼容 socks5h 语义）
	req := []byte{0x05, 0x01, 0x00, 0x03, byte(len(host))}
	req = append(req, host...)
	p, _ := strconv.Atoi(port)
	req = append(req, byte(p>>8), byte(p&0xff))
	if _, err := conn.Write(req); err != nil {
		return fail(err)
	}

	head := make([]byte, 4)
	if _, err := io.ReadFull(conn, head); err != nil {
		return fail(err)
	}
	if head[0] != 0x05 || head[1] != 0x00 {
		return fail(fmt.Errorf("socks5: connect failed code=%d", head[1]))
	}
	switch head[3] {
	case 0x01:
		if _, err := io.ReadFull(conn, make([]byte, 4+2)); err != nil {
			return fail(err)
		}
	case 0x04:
		if _, err := io.ReadFull(conn, make([]byte, 16+2)); err != nil {
			return fail(err)
		}
	case 0x03:
		ln := make([]byte, 1)
		if _, err := io.ReadFull(conn, ln); err != nil {
			return fail(err)
		}
		if _, err := io.ReadFull(conn, make([]byte, int(ln[0])+2)); err != nil {
			return fail(err)
		}
	}
	_ = conn.SetDeadline(time.Time{})
	return conn, nil
}
