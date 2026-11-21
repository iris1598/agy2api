# 交接计划：Antigravity 保真度硬化（agy2api）

> 面向接手本工作的另一个 agent / 工程师。读完这份文档应能独立继续推进，
> 不需要重新抓包或重新推导结论。所有结论都以 `tools/antigravity_capture`
> 的实抓数据为准。

---

## 0. 一句话背景

`agy2api` 是一个把 Google Antigravity 凭证包装成 OpenAI / Anthropic / Gemini
兼容接口的反代。目标不是"能用就行"，而是**出站报文与真实 Antigravity 客户端
尽可能一致；无法证明一致的地方直接报错，不做静默降级**（这条是 owner 的明确要求）。

当前基线：**Antigravity 2.34.0**（2026-11-21 抓包）。

---

## 1. 当前状态总览

| 层 | 状态 |
|---|---|
| TLS / HTTP 出站指纹 | ✅ 已实测一致（Go 转发器，JA3 md5 精确匹配） |
| 信封 / labels / step / 子 agent 规则 | ✅ 已按 2.34.0 实抓对齐 |
| 严格模式（fail-closed） | ✅ 已实现，启动自检已接入 |
| 模型枚举表 | ✅ 39 个模型全部来自实抓 |
| 抓包工具 | ✅ 支持离线、被动、tlsproxy、schema、models、verify、离线合成工具调用/子 agent |
| **真实端到端请求** | ❌ **尚未验证**（唯一没走过的环节） |
| CI | ⚠️ 已写 workflow，**从未在 GitHub 上跑过** |
| 安装/分发 | ⚠️ Docker 已覆盖；源码安装脚本尚未适配"必须有 Go 二进制" |
| 防封剩余项 | ⚠️ 额度预算、指数退避、模型分布约束未做 |

测试基线：`pytest` → **173 passed, 1 skipped**（skip 是依赖旧抓包样本的黄金测试）。
`sniff_cli.py verify --envelope` → **通过**（JA3 + header + 信封 schema）。

---

## 2. 架构与关键文件

```
客户端(OpenAI/Anthropic/Gemini)
        │
        ▼
[Python 控制面]  FastAPI 路由 / 格式转换 / 凭证 / 存储 / 面板
        │  httpx (明文回环)
        ▼
[Go 出站核心] tools/upstream_tls_proxy
        │  Go crypto/tls（与真实 LS 同栈）
        ▼
Google Antigravity API
```

- Python 负责：鉴权、格式转换、凭证管理、严格校验、SSE 解析、日志、面板。
- Go 负责：TLS 握手（ClientHello 与真实客户端一致）、HTTP/1.1 手写 header 顺序、
  连接池、请求体缓冲与"复用连接失效重试"、SSE 逐块 flush。
- 两者接口：Python 把原始 URL 放进 `X-Upstream-URL`，把账号代理放进
  `X-Upstream-Proxy`，发给 `127.0.0.1:17890`；Go 剥离这些头后转发。

### 新增文件

| 文件 | 作用 |
|---|---|
| `src/fidelity.py` | 严格模式：`FidelityError`、字段白名单、thinkingConfig 校验 |
| `src/model_enum.py` | 39 个 `model → MODEL_*` 映射 + fetchAvailableModels 解析/学习 |
| `src/upstream_proxy.py` | Go 转发器生命周期（查找/启动/健康/停止） |
| `src/credential_limiter.py` | 单凭证在途并发上限 |
| `src/thought_signature_cache.py` | thoughtSignature 按轨迹+文本缓存与回注 |
| `tools/upstream_tls_proxy/` | Go 出站核心（main.go/tls.go/build 脚本/README） |
| `tools/antigravity_capture/` | 抓包工具（见第 6 节） |
| `fidelity_schema.json` | 从实抓生成的基准（端点/键序/requestType/模型枚举） |
| `.github/workflows/fidelity.yml` | 三平台 pytest + verify + 交叉编译矩阵 |
| `tests/test_fidelity.py` / `test_upstream_proxy.py` / `test_capture_server.py` | 新增测试 |

### 修改文件

`src/utils.py`（UA）、`src/api/antigravity.py`（labels/step/requestId/盐/签名/错误处理）、
`src/httpx_client.py`（Go 路由、无回退）、`src/request_pacer.py`（`pace_for`）、
`src/converter/antigravity_fix.py`（严格模式不注入 safetySettings）、
`config.py`（新配置项）、`web.py`（启动自检 + 关闭 Go 核心）、`Dockerfile`（多阶段 Go 构建）、
`README.md`、`tests/*`。

---

## 3. 已实测的关键结论（改代码前必须遵守）

1. **UA**：`antigravity/hub/2.34.0 (aidev_client; os_type=windows; arch=amd64; cl=1026637980)`
2. **TLS**：JA3 md5 `03117a8ed39ef02427ebbc39f121275c`；
   ALPN 只给 `http/1.1`；**不要启用 ClientSessionCache**——一开就会多发扩展 35/45，
   JA3 变成 `5e5bffde…`（已踩过）。曲线组顺序必须固定
   `4588-4587-4589-29-23-24-25`（`tools/upstream_tls_proxy/tls.go`）。
3. **header 集合**：`Host → User-Agent → Transfer-Encoding(chunked) → Authorization →
   Content-Type → Accept-Encoding: gzip`。**没有 `Accept`**；httpx 会自动补，已由 Go 丢弃。
4. **信封**：`project → requestId → request → model → userAgent → requestType`；
   内层 `contents → systemInstruction → tools → labels → generationConfig → sessionId`。
5. **labels（2.34.0，9 个键）**：`cascade_id, last_step_index, model_enum,
   request_id, root_cascade_id, trajectory_id, used_claude,
   used_claude_conservative, used_non_gemini_model`。
   **不再有 `last_execution_id`。**
6. **step 规律**：`step = 1 + 2 × (contents 中 role=="model" 的消息数)`；
   `last_step_index = step - 1`。依据：193 条 contents/192 条 model → step 385。
7. **request_id 后缀**：trajectory 内**从 0 开始**逐请求自增。
8. **requestType 三种形态**：
   - `agent`：`agent/{cascade}/{ms}/{trajectory}/{step}`，带 labels；
   - `checkpoint`：`checkpoint/{uuid}`（两段），模型常为 `gemini-3.5-flash-lite`，无 labels；
   - `agent_monitoring`：`agent_monitoring/{uuid}`（两段），无 labels，thinkingLevel 可为 `MINIMAL`。
9. **子 agent**：新 `cascade_id`，`root_cascade_id` = 父 cascade，新 `trajectory_id`，
   `request_id` 从 0 重新计数；父会话继续自己的 step/计数。
10. **工具 part**：`{"functionCall": {"id","name","args"}, "thoughtSignature"}`；
    结果 `{"functionResponse": {"id","name","response":{"output"}}}`，call/response 共享 id。
    合成 functionCall 可以省略 id/签名，客户端会自行补齐。
11. **模型表**：39 个，含 `MODEL_PLACEHOLDER_*`、`MODEL_GOOGLE_GEMINI_*`、
    `MODEL_OPENAI_GPT_OSS_120B_MEDIUM`；`gemini-4-hydrogen-medium → MODEL_PLACEHOLDER_M526`。
12. **端点**：`loadCodeAssist`、`fetchUserInfo`、`fetchAvailableModels`、`listExperiments`、
    `cascadeNuxes`(GET)、`writeTrajectoryAcls`(2.34 新增，body `{"trajectoryId"}`)、
    `retrieveUserQuotaSummary`、`generateContent`、`streamGenerateContent`。

---

## 4. 待办事项（按优先级）

### P0 — 真实端到端验证（必须最先做）

**为什么**：至今没有用真实凭证通过代理发过一条模型请求。严格模式可能误杀
（未知模型、字段白名单、thinkingConfig 冲突），必须尽早暴露。

**做法**：
1. 启动：`.\.venv\Scripts\python.exe web.py`（会自检 Go 核心，失败会拒绝启动）。
2. 用低 token 模型发一条流式请求，确认：200、SSE 逐块到达、`usageMetadata` 可解析、
   日志无 `fidelity_rejected`。
3. 再发：带 tools 的请求、`-low/-high` 后缀模型、一个 Claude 模型。
4. 建议新增 `tools/smoke_real_request.py`（不在文档里，由接手者实现）：
   读配置里的 API_PASSWORD 与模型，发一条 `max_tokens=16` 的流式请求并断言关键字段。

**验收**：三类请求全部 200；无 Go 回退日志；`fidelity_rejected` 出现时能明确说出
reason 并修复（补枚举 / 修转换器 / 加白名单）。

**风险**：会消耗少量额度；建议用最低成本模型、一次一条。

### P1 — 模型参数表与 thinking 归一化

`fetchAvailableModels` 的响应里有 `maxOutputTokens`、`supportsThinking`、
`minThinkingBudget`（样本：`gemini-2.5-flash` maxOutputTokens=65535，
`gemini-4-hydrogen-*` agent 请求 maxOutputTokens=262144）。

**做法**：
- 生成 `model → {maxOutputTokens, supportsThinking}` 表（可内置 + 运行时学习，复用
  `src/model_enum.py` 的学习模式）；
- 严格模式下：客户端给的 `maxOutputTokens` 超过模型上限 → 归一化或报
  `generation_config_mismatch`；
- 补 `MINIMAL` 到合法 thinkingLevel 集合（目前 `src/fidelity.py` 只认 LOW/MEDIUM/HIGH）。

**验收**：新增测试覆盖"超上限归一化/报错"与"MINIMAL 合法"；真实请求 P0 通过。

### P2 — 防封剩余项

1. **每凭证额度预算**：用 `v1internal:retrieveUserQuotaSummary` 定期刷新，
   接近阈值即停用该凭证（不要等 429）。建议在 `get_valid_credential` 前置检查。
2. **全局指数退避 + 抖动**：现在只有固定 `retry_interval`；429/403 后应
   按账号累计失败次数做指数退避。
3. **模型分布约束**：限制单账号高价模型占比；24/7 恒定速率检测。
4. **注意**：`rate_limiter.acquire_turn()` 的串行模式仍是**全局**的（中间件层拿不到凭证），
   按凭证的并发/节流已在 `src/api/antigravity.py` 内部通过
   `credential_limiter` + `request_pacer.pace_for` 实现。若要彻底 per-credential 串行，
   需要把回合锁下移到凭证选择之后。

### P3 — Go 核心收尾

1. **envelope 构造下沉到 Go**：目前 Python 拼 `wrap_cli_request`、Go 只发送。
   目标是上游报文字节只有一个作者（Go 接收"内层请求 + 会话锚点 + 模型"，
   自己生成 labels/requestId/cascade）。
2. **守护与重启**：现在靠"请求前 `ensure_running`"拉起；建议加后台健康检查，
   进程退出后主动重启，并暴露状态。
3. **分发**：
   - 已有多阶段 Docker 构建（`golang:1.27-alpine`）；
   - 源码安装（`install.ps1` / `install.sh` / `termux-install.sh`）**尚未适配**：
     严格模式要求 Go 二进制存在，否则拒绝启动。需要加"下载对应平台 release 二进制 +
     SHA256 校验 + macOS quarantine 处理"，或安装时本地编译；
   - `tools/upstream_tls_proxy/build-all.sh|ps1` 已能产 7 个平台产物。

### P4 — 收尾与卫生

- `fidelity_schema.json` 和 `tools/` 目前是**未跟踪文件**，交接前必须提交
  （`.gitignore` 已放行 `tools/` 源码与 `fidelity_schema.json`，仅忽略 captures/logs/bin）。
- `.zcode/` 建议加入 `.gitignore`。
- CI `fidelity.yml` 需要实际跑一次（见第 7 节风险）。
- 把 `pytest-asyncio` 加入 `requirements-dev`/CI（当前本机 venv 手动装过，
  `pyproject.toml` dev extras 已有，但 CI 是单独 pip 安装）。

---

## 5. 立即可以复现的验证命令

```powershell
# 单元测试（必须用 backend venv）
.\.venv\Scripts\python.exe -m pytest -q

# 闭环指纹 + 信封校验（需要 bin/upstream-proxy.exe 已存在）
.\.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py verify --envelope

# 从最新抓包重新生成基准
.\.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py schema
.\.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py models
```

出站核心编译（仅当需要重新构建时；本机当前已存在 `bin/upstream-proxy.exe`）：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build.ps1
# 或指定 go.exe
powershell -ExecutionPolicy Bypass -File .\tools\upstream_tls_proxy\build.ps1 -Go "C:\Go\bin\go.exe"
```

环境事实：本机 **没有系统级 Go**（PATH 里找不到 `go`），当前二进制是用临时目录里的
portable Go 1.27.4 编译的。临时目录不保证长期存在，接手者需要先安装 Go ≥ 1.26
或使用 `-Go` 指定路径。Docker 构建不受影响。

---

## 6. 抓包工具现状（已升级，勿回退）

`tools/antigravity_capture`：

- `capture_server.py`
  - 模式：默认全转发（经 Go）；`--offline` 只离线模型端点、元数据正常转发；
    `--offline-all` 全部离线；`--offline-script tool-call|subagent` 离线合成一次
    `read_file` / `invoke_subagent(research)`（白名单，绝不合成 run_command）。
  - 支持下游 keep-alive；记录 `framing`、`client_port`、连接内请求序号与到达间隔；
    JSON 响应完整落盘（超长时另存 `.response.full.txt`，避免 JSON 被截断）。
- `sniff_cli.py`
  - `passive` / `tlsproxy` / `analyze` / `report`；
  - `verify [--envelope] [--schema] [--json] [--ja3]`：本地抓自己的 ClientHello，
    逐段比对 JA3/ALPN，并校验上游 header 顺序与信封 schema；
  - `schema`：导出 `fidelity_schema.json`（端点、键序、requestType、模型枚举）；
  - `models`：从 fetchAvailableModels 导出 model→枚举（优先 full.txt，其次 response.bin）。
- TLS 指纹解析在 `tls_fingerprint.py`（含真实 JA3 常量与 `diff_clienthello`）。

抓包使用要点：
- 离线抓工具调用/子 agent 时，`-OfflineScript` 每次运行只触发一次；
- 抓完必须重启 Antigravity 清掉 `CLOUD_CODE_URL`/`HTTPS_PROXY`；
- 抓包目录含 prompt，已 gitignore，用完删除。

---

## 7. 已知坑与风险（接手者务必看）

1. **不要启用 TLS 会话缓存**（见 3.2）。
2. **不要恢复 httpx 回退**：严格模式下 Go 不可用 → 503；启动自检失败 → 拒绝启动。
   仅调试时可用 `STRICT_FIDELITY=false`。
3. **Go 连接池**：复用连接可能已被上游关闭；现在请求体先缓冲、读失败换新连接重发一次。
   改这段注意不要破坏"只重试连接阶段、收到数据后绝不重试"的语义。
4. **httpx 自动补头**：`Accept` 会被 Go 丢弃；若未来换了传输层，需自己保证不发。
5. **抓包响应预览**：`response.txt` 默认 20KB 预览；大响应解析一律走
   `.response.full.txt` 或 `.response.bin`。
6. **CI 未验证**：`fidelity.yml` 里三平台 `verify` 会 import 应用配置（sqlite），
   首次跑可能遇到路径/依赖问题；`fidelity_schema.json` 必须先提交。
7. **严格模式误杀面**：模型枚举、字段白名单、thinkingConfig 冲突都可能拒绝请求；
   P0 验证就是专门用来暴露这些的。
8. **账号盐**：`_derive_upstream_session_ids` 混入 `account_salt`（凭证名），
   同一段内容在不同账号下不会撞 trajectory；改派生逻辑时保持这一点。
9. **不要伪造内容**：systemInstruction / tools / 对话包装 / transcript 一律透传，
   不编造（owner 明确要求）。
10. **转发模式抓包**会真的发请求；不要长期挂着当日常使用。

---

## 8. Owner 已定的决策（不要重新讨论）

- 宁可报错，不静默降级（严格模式默认开启）。
- 出站 TLS 用 Go 自建转发器，不用 tls-client/curl_cffi/httpx 伪造。
- 未知模型不回退成模型名；Go 核心不可用不退回 httpx。
- 不伪造客户端内容；只透传。
- 抓包工具离线合成只允许 `read_file`（临时文件）和 `invoke_subagent(research)`。

---

## 9. 交接完成验收清单

- [ ] 工作区已提交（含 `fidelity_schema.json`、`tools/`；`.zcode/` 处理掉）
- [ ] `pytest -q` 全绿
- [ ] `verify --envelope` 通过
- [ ] P0 真实请求三类场景全部 200，日志无回退/拒绝
- [ ] CI workflow 至少跑通一次
- [ ] 源码安装脚本在无 Go 环境下行为明确（报错或自动获取二进制）
- [ ] README/抓包 README 与实际行为一致
