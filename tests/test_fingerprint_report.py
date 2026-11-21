"""sniff_cli（tools/antigravity_capture）指纹报告纯函数的单元测试。

覆盖被动抓包 + 指纹报告工具里不涉及网络/子进程的解析与生成逻辑：

- src/utils.py 指纹常量解析、代理 UA 重建、header 顺序解析
- 真实 UA 宽松提取（版本 / CL / 平台）
- 离线重定向会话 meta.json 读取（header 保序、envelope 键序）
- 已知域名分类、endpoint 名提取
- fingerprint_report.md 正文生成（diff 结论 + 建议常量块）

用法::

    .venv\\Scripts\\python.exe -m pytest tests\\test_fingerprint_report.py -v
"""

import hashlib
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "antigravity_capture"))

import pytest  # noqa: E402

import sniff_cli  # noqa: E402
import tls_fingerprint  # noqa: E402


UTILS_SNIPPET = '''
OFFICIAL_HUB_VERSION = "2.28.0"
OFFICIAL_HUB_CL = "1008300195"
_HUB_FINGERPRINT_OS = "windows"
_HUB_FINGERPRINT_ARCH = "amd64"

ANTIGRAVITY_USER_AGENT = (
    f"antigravity/hub/{OFFICIAL_HUB_VERSION} "
    f"(aidev_client; os_type={_HUB_FINGERPRINT_OS}; "
    f"arch={_HUB_FINGERPRINT_ARCH}; cl={OFFICIAL_HUB_CL})"
)
'''

ANTIGRAVITY_SNIPPET = '''
def build_antigravity_headers(access_token, extra_headers=None, model_name=""):
    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    return headers


def compose_python_envelope(parts):
    payload = {
        "project": project_id,
        "requestId": request_id,
        "request": inner,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "image_gen" if is_image_request else ("agent" if is_agent_request else "chat"),
    }
    return payload, request_id


def _align_inner_request_topology(inner):
    key_order = [
        "contents",
        "systemInstruction",
        "tools",
        "toolConfig",
        "labels",
        "generationConfig",
        "sessionId",
    ]
    return inner
'''


ANTIGRAVITY_SNIPPET_WITH_LABELS = '''
def _build_labels(model, trajectory_id, step):
    return {
        "last_step_index": str(step),
        "model_enum": model,
        "request_id": f"{trajectory_id}-{step}",
        "trajectory_id": trajectory_id,
    }
'''


def _make_request_meta(
    seq: int, endpoint: str, headers: list, envelope: dict, tmp_path, http_version="HTTP/1.1"
) -> None:
    """按 capture_server.py 的落盘约定写一对 meta/body 文件。"""
    stem = f"{seq:04d}_{endpoint}"
    meta = {
        "seq": seq,
        "received_at": "2026-11-20T00:00:00+00:00",
        "method": "POST",
        "target": f"/v1internal:{endpoint}?alt=sse",
        "http_version": http_version,
        "headers": headers,
        "body_bytes": 123,
        "body_sha256": "0" * 64,
    }
    (tmp_path / f"{stem}.request.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / f"{stem}.request.json").write_text(
        json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
    )


class TestProxyFingerprintParsing:
    def test_parse_hub_constants(self):
        constants = sniff_cli.parse_hub_constants(UTILS_SNIPPET)
        assert constants["version"] == "2.28.0"
        assert constants["cl"] == "1008300195"
        assert constants["os"] == "windows"
        assert constants["arch"] == "amd64"

    def test_proxy_user_agent(self):
        ua = sniff_cli.proxy_user_agent(sniff_cli.parse_hub_constants(UTILS_SNIPPET))
        assert ua == (
            "antigravity/hub/2.28.0 (aidev_client; os_type=windows; "
            "arch=amd64; cl=1008300195)"
        )

    def test_proxy_user_agent_empty_when_no_version(self):
        assert sniff_cli.proxy_user_agent({"version": ""}) == ""

    def test_parse_proxy_header_order(self):
        order = sniff_cli.parse_proxy_header_order(ANTIGRAVITY_SNIPPET)
        assert order == ["User-Agent", "Authorization", "Content-Type", "Accept", "Accept-Encoding"]

    def test_parse_proxy_header_order_no_match(self):
        assert sniff_cli.parse_proxy_header_order("def other():\n    pass\n") == []

    def test_parse_proxy_envelope_order(self):
        envelope, inner = sniff_cli.parse_proxy_envelope_order(ANTIGRAVITY_SNIPPET)
        assert envelope == ["project", "requestId", "request", "model", "userAgent", "requestType"]
        assert inner == [
            "contents", "systemInstruction", "tools", "toolConfig",
            "labels", "generationConfig", "sessionId",
        ]

    def test_load_proxy_fingerprint_from_repo(self):
        """对真实仓库源码做一次冒烟解析（结构正确即可）。"""
        proxy = sniff_cli.load_proxy_fingerprint(REPO_ROOT)
        assert proxy["constants"]["version"]
        assert proxy["constants"]["cl"]
        assert "User-Agent" in proxy["header_order"]


class TestRealClientEvidence:
    def test_extract_ua_parts_current_shape(self):
        parts = sniff_cli.extract_ua_parts(
            "antigravity/hub/2.33.0 (aidev_client; os_type=windows; arch=amd64; cl=1023160026)"
        )
        assert parts["version"] == "2.33.0"
        assert parts["cl"] == "1023160026"
        assert parts["os"] == "windows"
        assert parts["arch"] == "amd64"

    def test_extract_ua_parts_keeps_raw_when_unknown_shape(self):
        raw = "some-future-client/9.9 (weird)"
        parts = sniff_cli.extract_ua_parts(raw)
        assert parts["raw"] == raw
        assert parts["version"] == ""
        assert parts["cl"] == ""

    def test_endpoint_of(self):
        assert sniff_cli.endpoint_of("/v1internal:streamGenerateContent?alt=sse") == "streamGenerateContent"
        assert sniff_cli.endpoint_of("/v1internal:generateContent") == "generateContent"

    def test_classify_domain(self):
        assert sniff_cli.classify_domain("daily-cloudcode-pa.googleapis.com") == "antigravity"
        assert sniff_cli.classify_domain("aicode.googleapis.com") == "antigravity"
        assert sniff_cli.classify_domain("foo.googleusercontent.com") == "related"
        assert sniff_cli.classify_domain("example.com") == "other"

    def test_load_redirect_meta_preserves_order(self, tmp_path):
        envelope = {
            "project": "p",
            "requestId": "agent/x/1/y/2",
            "request": {"contents": [], "labels": {}, "sessionId": "s"},
            "model": "gemini-3-pro-preview",
            "userAgent": "antigravity",
            "requestType": "agent",
        }
        headers = [
            ["User-Agent", "antigravity/hub/2.33.0 (aidev_client; os_type=windows; arch=amd64; cl=1023160026)"],
            ["Authorization", "<redacted>"],
            ["Content-Type", "application/json"],
            ["x-goog-api-client", "gl-go/1.23 jetski/2.33.0"],
        ]
        _make_request_meta(1, "streamGenerateContent", headers, envelope, tmp_path)

        metas = sniff_cli.load_redirect_meta(tmp_path)
        assert len(metas) == 1
        meta = metas[0]
        assert meta["endpoint"] == "streamGenerateContent"
        assert meta["http_version"] == "HTTP/1.1"
        assert [h[0] for h in meta["headers"]] == [
            "User-Agent", "Authorization", "Content-Type", "x-goog-api-client",
        ]
        assert meta["envelope_keys"] == [
            "project", "requestId", "request", "model", "userAgent", "requestType",
        ]
        assert meta["inner_keys"] == ["contents", "labels", "sessionId"]

    def test_load_redirect_meta_skips_broken_files(self, tmp_path):
        (tmp_path / "0001_x.request.meta.json").write_text("not-json", encoding="utf-8")
        assert sniff_cli.load_redirect_meta(tmp_path) == []


def _sample_ja3_summary() -> dict:
    return {
        "generated_at": "2026-11-20T00:00:00+00:00",
        "pcaps": ["capture.pcapng"],
        "client_hello_count": 3,
        "entries": [
            {
                "sni": "daily-cloudcode-pa.googleapis.com",
                "ja3_md5": "abc123deadbeef",
                "ja4": "t13d1516h2_8daaf6152771_b0da82dd1658",
                "ja3_full": "771,4865-4866-4867,0-23-65281-10-11-35-16-5-13,29-23-24,0",
                "count": 2,
                "transports": ["tcp"],
                "dsts": ["142.250.66.74:443"],
                "alpn": ["h2", "http/1.1"],
                "tls_versions": ["TLS 1.3"],
                "category": "antigravity",
            },
            {
                "sni": "open-vsx.org",
                "ja3_md5": "fff111",
                "ja4": "t13d000000_000000000000_000000000000",
                "ja3_full": "",
                "count": 1,
                "transports": ["tcp"],
                "dsts": ["1.2.3.4:443"],
                "alpn": ["h2"],
                "tls_versions": ["TLS 1.3"],
                "category": "other",
            },
        ],
    }


def _sample_proxy() -> dict:
    constants = sniff_cli.parse_hub_constants(UTILS_SNIPPET)
    envelope, inner = sniff_cli.parse_proxy_envelope_order(ANTIGRAVITY_SNIPPET)
    return {
        "constants": constants,
        "user_agent": sniff_cli.proxy_user_agent(constants),
        "header_order": sniff_cli.parse_proxy_header_order(ANTIGRAVITY_SNIPPET),
        "envelope_order": envelope,
        "inner_order": inner,
    }


def _sample_redirect_metas() -> list:
    return [
        {
            "seq": 1,
            "received_at": "2026-11-20T00:00:00+00:00",
            "method": "POST",
            "target": "/v1internal:streamGenerateContent?alt=sse",
            "endpoint": "streamGenerateContent",
            "http_version": "HTTP/1.1",
            "headers": [
                ["User-Agent", "antigravity/hub/2.33.0 (aidev_client; os_type=windows; arch=amd64; cl=1023160026)"],
                ["x-goog-api-client", "gl-go/1.23 jetski/2.33.0"],
                ["Authorization", "<redacted>"],
                ["Content-Type", "application/json"],
            ],
            "body_bytes": 1024,
            "envelope_keys": ["project", "requestId", "request", "model", "userAgent", "requestType"],
            "inner_keys": ["contents", "labels", "sessionId"],
        }
    ]


class TestBuildFingerprintReport:
    def test_full_report_contains_real_fingerprint_and_suggestion(self):
        report = sniff_cli.build_fingerprint_report(
            _sample_ja3_summary(), _sample_redirect_metas(), _sample_proxy(),
            {"passive": "captures/passive-x", "redirect": "captures/20261120-000000"},
        )
        # 真实 UA / CL 被解析进报告
        assert "1023160026" in report
        assert 'OFFICIAL_HUB_VERSION = "2.33.0"' in report
        assert 'OFFICIAL_HUB_CL = "1023160026"' in report
        # TLS 指纹与代理差异建议
        assert "daily-cloudcode-pa.googleapis.com" in report
        assert "abc123deadbeef" in report
        assert "HTTP/2" in report
        # header 差异
        assert "x-goog-api-client" in report
        assert "代理缺失" in report
        # envelope 键序展示
        assert "project -> requestId -> request -> model -> userAgent -> requestType" in report

    def test_report_without_data(self):
        report = sniff_cli.build_fingerprint_report(None, [], _sample_proxy(), {})
        assert "无被动抓包数据" in report
        assert "无离线重定向数据" in report
        assert "agy2api 当前指纹" in report

    def test_report_tls_section_flags_missing_antigravity_traffic(self):
        summary = _sample_ja3_summary()
        summary["entries"] = [e for e in summary["entries"] if e["category"] == "other"]
        report = sniff_cli.build_fingerprint_report(summary, [], _sample_proxy(), {})
        assert "没有 Antigravity 相关域名的 ClientHello" in report


class TestLatestSession:
    def test_latest_session_picks_newest_matching(self, tmp_path):
        capture_root = tmp_path / "captures"
        (capture_root / "passive-20261120-010000").mkdir(parents=True)
        (capture_root / "passive-20261120-020000").mkdir(parents=True)
        (capture_root / "20261119-120000").mkdir(parents=True)
        (capture_root / "passive-20261120-020000" / "ja3_summary.json").write_text("{}", encoding="utf-8")
        (capture_root / "20261119-120000" / "index.jsonl").write_text("", encoding="utf-8")

        hit = sniff_cli.latest_session(capture_root, r"^passive-", "ja3_summary.json")
        assert hit is not None and hit.name == "passive-20261120-020000"

        miss = sniff_cli.latest_session(capture_root, r"^\d{8}-\d{6}", "index.jsonl")
        assert miss is not None and miss.name == "20261119-120000"

    def test_latest_session_requires_marker_file(self, tmp_path):
        capture_root = tmp_path / "captures"
        (capture_root / "passive-20261120-010000").mkdir(parents=True)
        assert sniff_cli.latest_session(capture_root, r"^passive-", "ja3_summary.json") is None

    def test_latest_session_matches_tlsproxy(self, tmp_path):
        capture_root = tmp_path / "captures"
        (capture_root / "tlsproxy-20261120-030000").mkdir(parents=True)
        (capture_root / "tlsproxy-20261120-030000" / "ja3_summary.json").write_text("{}", encoding="utf-8")
        hit = sniff_cli.latest_session(capture_root, r"^(passive|tlsproxy)-", "ja3_summary.json")
        assert hit is not None and hit.name == "tlsproxy-20261120-030000"


class TestJa3Alignment:
    def _real_info(self, alpn=None):
        parsed = tls_fingerprint.parse_ja3_string(
            tls_fingerprint.REAL_ANTIGRAVITY_JA3_FULL
        )
        ja3_full, ja3_md5 = tls_fingerprint.compute_ja3(parsed)
        parsed.update(
            {
                "ja3_full": ja3_full,
                "ja3_md5": ja3_md5,
                "alpn": list(alpn if alpn is not None else ["http/1.1"]),
            }
        )
        return parsed

    def test_real_constants_match_declared_md5(self):
        _full, md5 = tls_fingerprint.compute_ja3(
            tls_fingerprint.parse_ja3_string(tls_fingerprint.REAL_ANTIGRAVITY_JA3_FULL)
        )
        assert md5 == tls_fingerprint.REAL_ANTIGRAVITY_JA3_MD5

    def test_diff_matches_when_identical(self):
        result = tls_fingerprint.diff_clienthello(self._real_info())
        assert result["ok"] is True
        assert result["md5_match"] is True
        assert result["alpn_match"] is True
        for key in ("ciphers", "extensions", "groups", "formats"):
            assert result[f"{key}_match"] is True
            assert result["segments"][key]["missing"] == []

    def test_diff_flags_alpn_and_groups(self):
        info = self._real_info(alpn=["h2", "http/1.1"])
        info["groups"] = [29, 23, 24, 25]
        result = tls_fingerprint.diff_clienthello(info)
        assert result["ok"] is False
        assert result["alpn_match"] is False
        assert result["groups_match"] is False
        assert set(result["segments"]["groups"]["missing"]) == {4588, 4587, 4589}

    def test_diff_without_clienthello(self):
        result = tls_fingerprint.diff_clienthello(None)
        assert result["ok"] is False
        assert "error" in result


class TestLabelsSemantics:
    def _body(self):
        return {
            "requestId": "agent/cascade-uuid/1795150502265/traj-uuid/8",
            "request": {
                "labels": {
                    "cascade_id": "cascade-uuid",
                    "last_execution_id": "exec-uuid",
                    "last_step_index": "7",
                    "model_enum": "MODEL_PLACEHOLDER_M526",
                    "request_id": "traj-uuid-2",
                    "root_cascade_id": "cascade-uuid",
                    "trajectory_id": "traj-uuid",
                    "used_claude": "false",
                    "used_claude_conservative": "false",
                    "used_non_gemini_model": "false",
                }
            },
        }

    def test_summarize_extracts_keys_and_consistency(self):
        result = sniff_cli.summarize_request_semantics(self._body())
        assert result["labels_keys"][0] == "cascade_id"
        assert result["labels_values"]["model_enum"] == "MODEL_PLACEHOLDER_M526"
        assert result["request_id_segments"][1] == "cascade-uuid"
        assert result["request_id_step"] == 8
        assert result["last_step_index"] == "7"
        assert result["cascade_matches_request_id"] is True
        assert result["trajectory_matches_request_id"] is True

    def test_summarize_handles_missing_labels(self):
        result = sniff_cli.summarize_request_semantics({"requestId": "agent/x/1/y/2"})
        assert result["labels_keys"] == []
        assert result["request_id_step"] == 2
        assert result["cascade_matches_request_id"] is False

    def test_parse_proxy_labels_keys(self):
        keys = sniff_cli.parse_proxy_labels_keys(ANTIGRAVITY_SNIPPET_WITH_LABELS)
        assert keys == ["last_step_index", "model_enum", "request_id", "trajectory_id"]

    def test_redirect_meta_includes_labels(self, tmp_path):
        body = self._body()
        _make_request_meta(3, "streamGenerateContent", [], body, tmp_path)
        metas = sniff_cli.load_redirect_meta(tmp_path)
        assert metas[0]["labels_keys"][3] == "model_enum"
        assert metas[0]["request_id_step"] == 8


class TestModelEnumExtraction:
    def test_parse_dict_shape(self):
        data = {
            "models": {
                "gemini-4-hydrogen-medium": {
                    "displayName": "Gemini 4 Hydrogen",
                    "modelEnum": "MODEL_PLACEHOLDER_M526",
                },
                "claude-sonnet-4-6": {"displayName": "Claude Sonnet 4.6"},
            }
        }
        mapping = sniff_cli.parse_model_enum_map(data)
        assert mapping == {"gemini-4-hydrogen-medium": "MODEL_PLACEHOLDER_M526"}

    def test_parse_list_shape_and_nested(self):
        data = {
            "models": [
                {"id": "m1", "meta": {"enum": "MODEL_PLACEHOLDER_M1"}},
                {"id": "m2", "displayName": "no enum here"},
            ]
        }
        assert sniff_cli.parse_model_enum_map(data) == {"m1": "MODEL_PLACEHOLDER_M1"}

    def test_parse_rejects_non_dict(self):
        assert sniff_cli.parse_model_enum_map([]) == {}
        assert sniff_cli.parse_model_enum_map("x") == {}

    def test_parses_non_placeholder_enum_and_ignores_provider(self):
        """2.34.0 的枚举不止 MODEL_PLACEHOLDER_*（还有 MODEL_GOOGLE_*）。"""
        data = {
            "models": {
                "gemini-2.5-flash": {
                    "model": "MODEL_GOOGLE_GEMINI_2_5_FLASH",
                    "modelProvider": "MODEL_PROVIDER_GOOGLE",
                }
            }
        }
        assert sniff_cli.parse_model_enum_map(data) == {
            "gemini-2.5-flash": "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        }

    def test_load_response_json_handles_sse(self):
        payload = {"models": {"m1": {"modelEnum": "MODEL_PLACEHOLDER_M1"}}}
        assert sniff_cli._load_response_json("data: " + json.dumps(payload)) == payload

    def test_load_model_enum_map_skips_synthetic(self, tmp_path):
        (tmp_path / "0001_fetchAvailableModels.response.txt").write_text(
            json.dumps({"synthetic": True, "models": {"m1": {"modelEnum": "MODEL_PLACEHOLDER_M1"}}}),
            encoding="utf-8",
        )
        (tmp_path / "0002_fetchAvailableModels.response.txt").write_text(
            json.dumps({"models": {"m2": {"modelEnum": "MODEL_PLACEHOLDER_M2"}}}),
            encoding="utf-8",
        )
        assert sniff_cli.load_model_enum_map(tmp_path) == {"m2": "MODEL_PLACEHOLDER_M2"}


class TestHeaderComparison:
    def test_flags_extra_accept(self):
        metas = [
            {
                "endpoint": "loadCodeAssist",
                "headers": [
                    ["Host", "127.0.0.1"],
                    ["User-Agent", "ua"],
                    ["Content-Length", "38"],
                    ["Authorization", "<redacted>"],
                    ["Content-Type", "application/json"],
                    ["Accept-Encoding", "gzip"],
                ],
            }
        ]
        orders = sniff_cli.real_header_orders(metas)
        result = sniff_cli.compare_header_orders(
            ["User-Agent", "Authorization", "Content-Type", "Accept", "Accept-Encoding"],
            orders,
        )
        assert result[0]["ok"] is False
        assert result[0]["extra"] == ["Accept"]

    def test_matching_headers_ok(self):
        metas = [
            {
                "endpoint": "loadCodeAssist",
                "headers": [["User-Agent", "ua"], ["Authorization", "<redacted>"],
                            ["Content-Type", "application/json"], ["Accept-Encoding", "gzip"]],
            }
        ]
        result = sniff_cli.compare_header_orders(
            ["User-Agent", "Authorization", "Content-Type", "Accept-Encoding"],
            sniff_cli.real_header_orders(metas),
        )
        assert result[0]["ok"] is True
        assert result[0]["order_match"] is True


class TestSidecarForwarding:
    """转发模式已改为经 Go 转发器出站（不得回退 Python TLS）。"""

    def test_headers_keep_accept_encoding_and_drop_hop_headers(self):
        import capture_server

        headers = [
            ("Host", "127.0.0.1:18080"),
            ("User-Agent", "antigravity/hub/2.33.0"),
            ("Transfer-Encoding", "chunked"),
            ("Authorization", "Bearer x"),
            ("Content-Type", "application/json"),
            ("Accept-Encoding", "gzip"),
            ("Content-Length", "10"),
        ]
        result = capture_server.build_sidecar_headers(headers)
        assert "Accept-Encoding" in result, "必须保留真实客户端会发的 Accept-Encoding"
        for dropped in ("Host", "Content-Length", "Transfer-Encoding"):
            assert dropped not in result
        assert result["Authorization"] == "Bearer x"

    def test_find_sidecar_binary_env_override(self, monkeypatch, tmp_path):
        import capture_server

        binary = tmp_path / "upstream-proxy.exe"
        binary.write_bytes(b"stub")
        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", str(binary))
        assert capture_server.find_sidecar_binary() == binary

    def test_find_sidecar_binary_missing(self, monkeypatch, tmp_path):
        import capture_server

        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", str(tmp_path / "nope"))
        monkeypatch.setattr(capture_server, "_SIDECAR_CANDIDATES", ())
        assert capture_server.find_sidecar_binary() is None


def _build_client_hello() -> bytes:
    """手工构造一个确定内容的 ClientHello 记录（TLS1.3 形态）。"""
    sni_name = b"example.com"
    sni_entry = b"\x00" + len(sni_name).to_bytes(2, "big") + sni_name
    sni_data = len(sni_entry).to_bytes(2, "big") + sni_entry
    sni_ext = b"\x00\x00" + len(sni_data).to_bytes(2, "big") + sni_data

    groups_data = (4).to_bytes(2, "big") + b"\x00\x1d\x00\x18"
    groups_ext = b"\x00\x0a" + len(groups_data).to_bytes(2, "big") + groups_data

    fmt_data = bytes([2, 0, 1])
    fmt_ext = b"\x00\x0b" + len(fmt_data).to_bytes(2, "big") + fmt_data

    alpn_entries = bytes([2]) + b"h2" + bytes([8]) + b"http/1.1"
    alpn_data = len(alpn_entries).to_bytes(2, "big") + alpn_entries
    alpn_ext = b"\x00\x10" + len(alpn_data).to_bytes(2, "big") + alpn_data

    sv_data = bytes([4]) + b"\x03\x04\x03\x03"
    sv_ext = b"\x00\x2b" + len(sv_data).to_bytes(2, "big") + sv_data

    exts = sni_ext + groups_ext + fmt_ext + alpn_ext + sv_ext
    ext_section = len(exts).to_bytes(2, "big") + exts

    ciphers = (4).to_bytes(2, "big") + b"\x13\x01\x13\x02"
    body = b"\x03\x03" + bytes(32) + b"\x00" + ciphers + b"\x01\x00" + ext_section
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


def _build_server_hello(alpn: bytes) -> bytes:
    alpn_data = len(alpn).to_bytes(2, "big") + alpn
    alpn_ext = b"\x00\x10" + len(alpn_data).to_bytes(2, "big") + alpn_data
    ext_section = len(alpn_ext).to_bytes(2, "big") + alpn_ext
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x13\x01" + b"\x00" + ext_section
    hs = b"\x02" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x03" + len(hs).to_bytes(2, "big") + hs


class TestTlsFingerprint:
    def test_client_hello_ja3_and_fields(self):
        status, info = tls_fingerprint.parse_client_hello(_build_client_hello())
        assert status == tls_fingerprint.OK
        assert info is not None
        assert info["sni"] == "example.com"
        assert info["ciphers"] == [0x1301, 0x1302]
        assert info["extensions"] == [0, 10, 11, 16, 43]
        assert info["groups"] == [0x001D, 0x0018]
        assert info["formats"] == [0, 1]
        assert info["alpn"] == ["h2", "http/1.1"]
        assert info["tls_versions"] == ["TLS 1.3", "TLS 1.2"]
        expected = "771,4865-4866,0-10-11-16-43,29-24,0-1"
        assert info["ja3_full"] == expected
        assert info["ja3_md5"] == hashlib.md5(expected.encode("ascii")).hexdigest()

    def test_client_hello_fragmented_needs_more(self):
        data = _build_client_hello()
        status, info = tls_fingerprint.parse_client_hello(data[:20])
        assert status == tls_fingerprint.NEED_MORE
        assert info is None

    def test_client_hello_non_tls_rejected(self):
        status, info = tls_fingerprint.parse_client_hello(b"GET / HTTP/1.1\r\n\r\n" + bytes(64))
        assert status == tls_fingerprint.BAD

    def test_server_hello_alpn(self):
        status, info = tls_fingerprint.parse_server_hello(_build_server_hello(bytes([2]) + b"h2"))
        assert status == tls_fingerprint.OK
        assert info is not None
        assert info["alpn"] == ["h2"]
        assert info["cipher"] == 0x1301

    def test_sniffer_accumulates_fragments(self):
        """_HelloSniffer 在真实字节流场景下按片段累积，拼出完整 ClientHello。"""
        from sniff_cli import _HelloSniffer

        data = _build_client_hello()
        results = []
        sniffer = _HelloSniffer("client", results.append)
        half = len(data) // 2
        sniffer.feed(data[:half])
        assert sniffer.done is False and not results
        sniffer.feed(data[half:])
        assert sniffer.done is True and len(results) == 1
        assert results[0]["sni"] == "example.com"
