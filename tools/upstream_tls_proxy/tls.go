package main

import (
	"crypto/tls"
	"net"
	"runtime"
)

// 注意：不要启用 ClientSessionCache。
// 实测真实客户端全部 API 连接（8 次握手）的 ClientHello 都没有扩展 35（session_ticket）
// 与 45（psk_key_exchange_modes）；而 Go 只要设置了 ClientSessionCache 就会带上这两个
// 扩展，JA3 立刻偏离（md5 从 03117a8e… 变成 5e5bffde…）。因此保持"每次完整握手"。
// 连接池（见 main.go）仍会复用已建立的 TCP/TLS 连接，只是不做会话恢复。

// curvePreferences 显式钉住实测真实客户端的曲线组顺序：
// 4588-4587-4589-29-23-24-25（X25519MLKEM768 / SecP256r1MLKEM768 /
// SecP384r1MLKEM1024 / X25519 / P-256 / P-384 / P-521）。
//
// Go 1.26 默认还会多带 MLKEM1024(514)，与实测不一致，所以这里显式覆盖。
// 需要 Go >= 1.26（SecP256r1MLKEM768 / SecP384r1MLKEM1024 常量在该版本引入）。
var curvePreferences = []tls.CurveID{
	tls.X25519MLKEM768,
	tls.SecP256r1MLKEM768,
	tls.SecP384r1MLKEM1024,
	tls.X25519,
	tls.CurveP256,
	tls.CurveP384,
	tls.CurveP521,
}

// tlsClient 用 Go 原生 crypto/tls 建连：与真实 language_server 同栈，
// ClientHello（密码套件顺序、扩展集合、后量子曲线组）天然一致。
// ALPN 只提供 http/1.1 —— 实测真实客户端对 API 域名不开 h2。
//
// serverName 为空表示目标是 IP，此时 Go 不发 SNI（与真实客户端访问 IP 时一致）。
func tlsClient(conn net.Conn, serverName string) *tls.Conn {
	return tls.Client(conn, &tls.Config{
		ServerName:       serverName,
		NextProtos:       []string{"http/1.1"},
		MinVersion:       tls.VersionTLS12,
		CurvePreferences: curvePreferences,
	})
}

func runtimeVersion() string {
	return runtime.Version()
}
