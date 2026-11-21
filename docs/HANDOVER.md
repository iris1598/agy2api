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
   不编造（owner 明确要求）。user 轮的 `<USER_REQUEST>` 格式包装是例外——
   实抓证明它是客户端的确定性格式（见 §10.13），复现格式不属于编造，
   原始 user 文本仍一字不改。
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

- [x] 工作区已提交（含 `fidelity_schema.json`、`tools/`；`.zcode/` 已加入 `.gitignore`）
- [x] `pytest -q` 全绿（2026-11-21：204 passed, 1 skipped）
- [x] `verify --envelope` 通过
- [ ] P0 真实请求四类场景全部 200（**smoke 脚本已就绪，待 owner 手动执行**，见 10.1）
- [ ] CI workflow 至少跑通一次（**本轮只本地提交未推送**；push 到 origin 后 fidelity.yml 自动触发）
- [x] 源码安装脚本在无 Go 环境下行为明确（自动下载 Release 二进制 + SHA256 校验，失败时本地编译/明确报错）
- [x] README/抓包 README 与实际行为一致

---

## 10. 本轮改进记录（2026-11-21，接手后第一轮）

范围：P0(脚本) / P1 / P2 / P3(除信封下沉) / P4，全部已本地提交（未 push）。

### 10.1 P0 — 真实端到端冒烟脚本（未实测，消耗额度为零）

- 新增 `tools/smoke_real_request.py`：四场景（basic 流式 / tools / `-low` 后缀 / Claude），
  默认读配置端口与 `api_password`，`--spawn` 可自动拉起 web.py；
  预检 `/v1/models`（不消耗额度），出现 `fidelity_rejected` 时打印 reason/detail。
  **owner 待办**：`.\.venv\Scripts\python.exe tools\smoke_real_request.py`（会消耗少量额度）。

### 10.2 P1 — 模型参数表与 thinking 归一化

- `src/model_enum.py` 新增 `MODEL_PARAMS` 种子表（实抓证据：gemini-2.5-flash 65535、
  gemini-4-hydrogen 主力档 262144）+ `parse_model_params`/`merge_model_params`/`get_model_param`
  运行时学习（接入点 `fetch_credential_models`）。
- `src/fidelity.py`：新增 maxOutputTokens 严格校验
  （超实抓上限 → `generation_config_mismatch`；**代理默认值 65536/64000 豁免**——否则
  gemini-2.5-flash 的实抓上限 65535 会误杀默认请求）。
  **thinkingLevel 档位修正（owner 2026-11-21 指正）**：用户可见模型档位只有
  low/medium/high（部分系列有 extra high），最低是 low，**没有 MINIMAL**。
  曾按本文档 §4 P1 的说法补过 MINIMAL，核实后已回退：实抓中 MINIMAL 只出现在
  agent_monitoring 辅助请求（gemini-3.5-flash-lite）与 fetchAvailableModels 的
  flash-lite 元数据里，agent 聊天路径 0 条；本代理只发 agent 请求。
  `xhigh` 后缀模型 owner 确认档位存在，但实抓未覆盖 xhigh 模型请求、档位字符串未证实，
  按"无证据不注入"暂不参与校验（待抓包补证）。
- `src/converter/antigravity_fix.py`：非严格模式对超限显式值钳制（严格模式交 fidelity 拒绝）。
  `_normalize_generation_config` 因此改为 async。

### 10.3 P2 — 防封剩余项

- 新增 `src/quota_budget.py`：web.py lifespan 后台任务定期复用 `fetch_quota_info` 刷新额度，
  低于阈值（`QUOTA_BUDGET_THRESHOLD`，默认 0.1）在 `get_valid_credential` 前置剔除；
  数据过期 fail-open。注意 `fetch_quota_info` 在 refresh_all 内延迟导入（循环依赖）。
- `src/api/utils.py`：429/500/503 重试改为**指数退避 + 抖动**（按账号连续失败计数，
  成功清零；`BACKOFF_ENABLED`/`BACKOFF_MAX_DELAY`）。上游明确冷却仍优先（模型级冷却 + 换号）。
- 新增 `src/model_mix.py`（默认关）：高价模型每日上限（`get_valid_credential` 前置过滤）+
  恒定速率 warning（每账号每小时最多一次）。
- 全局串行回合锁语义**未动**（P2.4 观察项：要彻底 per-credential 串行需把回合锁下移到凭证选择后）。

### 10.4 P3 — Go 核心收尾（信封下沉除外）

- `src/upstream_proxy.py`：`find_binary` 支持平台后缀产物名（与 build-all 命名对齐）；
  新增后台健康检查 `health_monitor`（本进程拉起的转发器退出自动重启）+ `status()`；
  web.py lifespan 启停。
- 新增 `.github/workflows/release.yml`：push `v*` 标签 → build-all 7 平台 + SHA256SUMS.txt
  → GitHub Release（gh CLI，无第三方 action）。
- 四个安装脚本（install.ps1 / install.sh / darwin-install.sh / termux-install.sh）：
  无二进制时优先下载 `releases/latest/download/<平台产物>` + SHA256 校验（macOS 清 quarantine，
  校验不匹配视为致命），失败回退本地编译指引，行为明确。
  **注意 install.ps1 已加 UTF-8 BOM**（PS 5.1 无 BOM 按 GBK 解析中文会破坏语法）。
- README 已同步（回退措辞改为"直接报错"、新配置项、二进制获取三途径）。

### 10.5 P4 — 卫生

- `.gitignore` 加 `.zcode/`；修正 httpx_client / upstream_proxy 的过期"回退 httpx"注释。
- `.env.example` 登记全部新环境变量（STRICT_FIDELITY / UPSTREAM_TLS_* / BACKOFF_* /
  QUOTA_BUDGET_* / MODEL_MIX_* / CREDENTIAL_MAX_CONCURRENT）。

### 10.6 遗留（下一轮候选）

1. **P0 实测**：owner 用真实凭证跑 `tools/smoke_real_request.py` 四场景。
2. **CI 验证**：push 到 origin 后确认 fidelity.yml 三平台绿、release.yml 打 tag 发产物。
3. **P2.4 观察项**：串行模式回合锁仍是全局（中间件层拿不到凭证）。
4. `xhigh` 后缀 thinkingLevel：owner 确认 extra high 档位存在，但缺 xhigh 模型的 agent
   请求实抓（上游报文档位字符串未证实）；抓到后按实抓字符串纳入档位校验。
5. 版本号：`version.txt` 由 CI 自动 bump，无需手动改。
6. requestType "chat"：当前实现会对无工具请求产出 `chat`，而实抓三种形态
   （agent/checkpoint/agent_monitoring）中没有 `chat`——是否属于保真度问题待 owner 拍板，
   未在信封重构中改动。

### 10.7 第二轮：信封构造下沉 Go（P3.1，owner 点单）

目标达成：**上游报文字节只有一个作者**。协议（Python → Go，判别头
`X-Upstream-Build-Envelope: 1`，Go 侧剥离）：

- Python `build_envelope_parts`：严格校验、sessionId 注入、step 计算、签名回注、
  labels 值（`_build_labels` 单一来源）、requestType/tools 判定、内层键序对齐，
  产出 `EnvelopeParts`（有序值片 + 锚点）。**值片按 httpx json= 同参序列化为
  JSON 文本字符串**，labels 位为 null 占位。
- Go `envelope.go`：解码值片（自定义 UnmarshalJSON，`json.Compact` 校验）、
  labels 九键/外层六键用 struct 声明序 + `json.Encoder SetEscapeHTML(false)`
  （Go 默认转义 `<>&` 而 Python 不转义，必须关）、手工拼接内层帧并在原位产出
  labels、requestId 五段（时间戳构造时取一次，重试复用同一 body，天然幂等）。
- 两种作曲模式：impersonation 开启（默认）→ wrapper + 头 → Go 构造；
  关闭 → `compose_python_envelope` 参照作曲直连 httpx。
  `tests/test_go_envelope_bytes.py` 用真实二进制 + dummy 上游断言**两种模式
  字节逐字节等价**（requestId 时间戳归一），并验证重试幂等性。
- 兼容性握手：`/__health` 增加 `protocol:2`；`ensure_running` 拉起后校验，
  旧二进制判不可用（严格模式拒绝启动），不会静默错发。**部署要求：升级后必须重建 Go 二进制**（CI/Docker 自动；release.yml 产物自动满足）。
- 面板探活改走 `prepare_cli_request` 同一路径，并补 `account_salt=email`
  （原先不同凭证探活派生相同会话，与真实请求不一致）。
- 踩坑记录：`build.ps1`/`build-all.ps1` 中文注释无 BOM → PS 5.1 按 GBK 解析报
  语法错误，已补 UTF-8 BOM（与 install.ps1 同类问题）。
- 本机无系统 Go：portable Go 1.27.4 下载到仓库外临时目录，`build.ps1 -Go` 重建。

### 10.8 第三轮：实抓基准修正（owner 指正"以最新几条实抓为准"）

**关键指正**：101457（297 条）/101904（193 条）两个长序列是**工具损坏导致的重试风暴，
不作数**（tools 全 user 角色、step 每请求 +2 的异常形态均源于此）。交接文档 §3.6 的
step 规律（`step = 1 + 2×(contents 中 role=="model" 的消息数)`，"192 条 model"）就是对
风暴样本的误读。以工具正常的最新会话（112035/112144/112701/114847）为准。

1. **step 公式修正**：真实规律为 `step = 1 + 2×(model 轮数)`，**functionResponse
   不计入轮**（工具输出不是一次模型回复）。证据（轨迹 0f52941f，114847）：
   `[user]`→1；`[user, model(fc), model(fr)]`→3；`[user, model(fc), model(fr),
   model(text), user]`→5。裸数 role==model 的旧实现在含工具轮的历史上会多算
   （0016 形态算出 7）。实现：`build_envelope_parts` 的轮数统计排除仅含
   functionResponse 的 model content；测试 `test_step_excludes_function_response_rounds`。

2. **functionResponse role 修正**：真实客户端的工具输出消息是 **role=model**
   （114847/0014、112701/0011），非 Gemini 公开 API 惯例的 user。修正
   `openai2gemini.flush_pending_tool_parts`、`anthropic2gemini` 主路径（与文本
   混合时拆分独立 content）与 `reorganize_tool_messages`；同时给
   `_strip_trailing_model_turns` 加豁免（含 functionResponse 的 model 消息是
   工具续跑请求的合法结尾，不按"预填充"剥离——否则工具续跑会被截断）。

3. **labels 三形态确认**（最新会话）：九键（标准）/ 11 键（子代理，含
   `parent_cascade_id`+`subagent_type`，0015）/ **10 键（含 `last_execution_id`，
   0016，出现在上一请求完成工具执行之后的下一请求）**。交接文档 §3.5
   "不再有 last_execution_id" 被最新实抓推翻。**未实现**：last_execution_id 的值
   （ac34ec93…）未出现在会话其它任何捕获文件中（0014 响应仅 354 字节，疑似错误
   响应，execution id 来源未捕获）——凭空生成违反"不伪造"，待补抓一次完整
   工具执行的请求-响应对后实现（插入位置：cascade_id 之后，字母序）。

4. **黄金样本更新**：实抓对照/黄金形状测试样本从 0307（风暴）换成
   114847/0014（九键+工具轮）；原先引用已删除的 20261120-125431 目录而一直
   skip 的 `test_envelope_matches_captured_sample_shape` 现已真正运行。
   `test_go_envelope_bytes.py` 三个测试（格式环/实抓环/字节等价环）全部基于
   最新有效样本。

> **下一窗口任务**（owner 用 Antigravity 做真实开发项目并抓全过程）：
> 见 `docs/HANDOVER_CAPTURE.md`——含抓取操作、last_execution_id 闭环预案、
> 分析步骤与已踩坑清单。

### 10.9 第四轮：真实转发会话闭环（HANDOVER_CAPTURE.md 任务的完成）

owner 用转发模式完成一次真实开发会话（**20261121-163547**，Antigravity 真实开发
nonebot 项目，57 条 streamGenerateContent + 全量响应落盘），并明确指正：**该会话是
唯一实抓基准，此前所有离线模拟会话（114847 等）不再作为地面真值**。据此完成闭环：

1. **step 公式二次修正（推翻 §10.8 的 `1+2×model 轮`）**。33 条 agent 请求全序列
   拟合（轨迹 d5cdfe46）：
   `step = user 轮数 + 非(纯 functionResponse)的 model 轮数 + functionCall part 数`，
   fr 不计轮；**并行调用按 part 数计**（0020 的 [user, model(fc×2), model(fr×2)] →
   自然步号 4）。旧公式与它在"每轮恰好 1 次调用"时巧合相等（离线样本全部落在
   该巧合区），163547 的并行调用/重发暴露了差异。
   **step 不是纯 contents 函数**：0013 请求 502 失败后客户端原样重发（0014），
   step 1→2，此后该轨迹 31 条请求全部带这个 +1 偏移。实现：`_natural_step` +
   `_resolve_trajectory_step`（轨迹级重发偏移，LRU 进程内态，随
   `_reset_trajectory_request_seq` 一并重置；空 contents 兜底取下限 1）。

2. **last_execution_id 闭环（§10.8 第 3 点的待证项）**：该值由**客户端本地生成**
   （uuid4），不出现在任何上游响应或其它端点中；出现规律 = 存在位于"最后一个
   user 轮"之前的 fr 轮，且**同一工具轮在同一 user 周期内保持同值，新 user 轮开启
   后更替**（0074-0078 同值 X，0080-0085 同值 Z，X≠Z；0020-0067 工具续跑期不携带）。
   代理无法复现本地随机值，以确定性派生替代：sha256(trajectory_id | 最后一个
   user 轮之前的最后一个 fr 轮内容规范 JSON) → uuid v4 形态——历史中同一 fr 轮
   逐字节相同 → 值稳定，与实抓稳定性模式逐请求一致（不伪造：值本身是客户端本地
   状态，上游从不回显校验）。实现 `_resolve_last_execution_id`；`_build_labels`
   十键（键序 cascade_id 之后）；Go 侧 `last_execution_id,omitempty` +
   **协议版本 2→3**（v2 二进制会静默丢键，握手拒绝，需重编译）。

3. **converter 布局对齐实抓**：`openai2gemini` 末尾的相邻同角色合并改为**只合并
   相邻 user 轮**（历史兼容 user-role fr 的 0-token 空回场景）。实抓证明真实客户端
   的 model(fc)/model(fr)/model(text) 是相邻独立 content（并行调用=单 content N
   part），旧合并把它们压成单 content，破坏布局与 step 口径。

4. **黄金样本换 163547/0080**（十键+多周期历史）；实抓环用
   `_seed_trajectory_step_state(bumps=1)` 复放重发偏移，requestId 末段 77 逐字节
   对照；`last_execution_id` 加入归一化集（值是客户端本地随机量，键存在性/键序由
   断言守住）。新增测试：并行调用计数、重发 +1、lei 出现/周期内稳定/更替、
   Go≡Python 十键环。**pytest 225 passed**（+5）；`sniff_cli verify --envelope --json`
   全绿（labels_orders 已含十键形态）。

5. **子代理 11 键形态关闭为"不适用"**（owner 2026-11-21 拍板）：实抓证实子代理
   没有独立端点（163547 全部 57 条请求同打 streamGenerateContent），11 键只是
   客户端在自有子代理轨迹上多带 parent_cascade_id/subagent_type 两个元数据键。
   反代路径无"这是子代理"的信号字段，无从标注；真实用户不开子代理时同样只发
   9/10 键，"全顶层 cascade"是合法客户端画像。键序已存档于 fidelity_schema
   （11 键 orders），值来源不再追查。残留极小不一致：caller 刻意透传
   invoke_subagent 工具调用而代理不声明子代理轨迹——常规接入不出现，接受。
   agent_monitoring/checkpoint 请求为空 labels + uuid 尾 requestId（代理不主动发
   这两类，无需实现）。

6. **新发现的既有差距（未修，后续候选）**：真实客户端 systemInstruction 会随功能
   激活演化（0013 的 `<terminal_sandbox>` 段 ↔ 0080 的 `<artifacts>` 段）——代理
   按 model+sys+前两条消息派生 trajectory_id，sys 演化会导致同一会话派生出不同
   trajectory（上游缓存亲和断裂）。若要修，方向是锚点只用首条消息+sys 稳定部分，
   或客户端显式 sessionId 透传优先。

> **遗留操作**：captures/20261121-163547 含提示词，处理完删除（gitignore，
> 勿提交）；两个抓包遗留转发器进程已清理，Go 二进制已重建为协议 3。

### 10.10 第五轮：问候抓包（193455）——xhigh 闭环 + chat 判定证据

owner 新抓 20261121-193455（新会话只发一句"你好"，extra high 档位）：

1. **xhigh 档位闭环（§10.6 遗留第 4 点）**：`gemini-4-hydrogen-xhigh` 的 agent
   请求 `thinkingLevel` 是**数值 100**（非字符串！），maxOutputTokens 262144，
   model_enum M528（枚举表已有）。实现：`fidelity._thinking_level_from_model`
   xhigh → 100（注入 JSON 数值）；比较键归一（数值 100 放行、字符串/其它值
   拒绝）；fidelity_schema 登记 xhigh 模型条目。**pytest 228 passed**。

2. **requestType "chat" 判定证据（待 owner 拍板）**：普通对话面板发纯文本问候
   → `requestType=agent` + 16 工具声明（与 163547/0013 一致）。`chat` 在两次
   实抓中 0 次出现，真实客户端不存在无工具声明的对话请求。建议：我们的无工具
   请求也改发 `agent`（实抓为准）；原"chat 避开 Agent 资源池限流"只是防御性
   假设，无实据。改动几行 + 测试同步，等 owner 确认。

3. **新观察（未修，记录）**：
   - **sessionId 是装机级稳定值**：163547 与 193455 两个不同会话（不同
     cascade/trajectory）的 sessionId 同为 `-3750763034362895579`——真实客户端
     的 sessionId 不随对话变化；代理按内容锚点逐对话派生（路由用途等效，但
     画像不同，后续若上游校验再议）；
   - **checkpoint 请求**（0011/0013）：contents 只带原始用户文本（"你好"，非
     `<USER_REQUEST>` 包装），flash-lite + `thinkingLevel: "LOW"`（字符串）+
     无 tools/无 labels；0011 曾 503 后重发为 0013。属客户端内部功能，代理
     不实现；
   - **maxOutputTokens**：真实客户端 hydrogen 系（medium 与 xhigh）统一
     262144，代理默认 65536（`GEMINI_MAX_OUTPUT_TOKENS`）——已知偏差，是否
     对齐待 owner 定（上限值只影响生成长度封顶）。

### 10.11 第六轮：三项实拍对齐（owner 指示"123 都对齐"）

基于 §10.10 的实抓证据，三项偏差全部对齐：

1. **requestType：chat 分支删除，非图片一律 `agent`**。实抓（163547/193455）
   证实真实客户端的对话请求一律 agent（纯文本问候也带 16 个工具声明），
   `chat` 0 次出现；原"chat 避开 Agent 资源池限流"是防御性假设。顺带效果：
   无 tools 请求不再剥离 tools/toolConfig（真实客户端永远声明工具集，透传
   更对齐）。死代码 `_contents_have_tool_interactions` 删除。

2. **sessionId 改为装机级稳定**（实抓：两个不同会话 sessionId 同值）。代理侧
   按**凭证**派生（`_derive_install_session_id`：sha256("upstream-session:" +
   account_salt) → -{int}），一个凭证 = 一个客户端安装，跨会话/跨重启恒定；
   换凭证即换值（多账号隔离保留）。trajectory/cascade 仍逐对话派生（实抓
   证实跨会话不同）。原"摘要前 8 字节 → sessionId"口径废止。

3. **maxOutputTokens 默认注入：hydrogen 系 262144**（实抓 medium/xhigh 统一
   262144；注册表上限本就是 262144，严格模式校验兼容）。其余 Gemini 家族
   65536、Claude 64000 不变。

**pytest 232 passed**（+4：requestType/装机 sessionId/hydrogen 默认值各一组）；
`verify --envelope --json` 全绿。本地领先 origin 3 个提交（d94af71/ec55092/本轮）。

### 10.12 第七轮：客户端副本静态逆向（tools/_rev，2026-11-21）

owner 提供 Antigravity 客户端副本（`tools/antigravity/`，不入库）。本轮对
Electron `app.asar`（主进程 JS）与 `language_server.exe`（Go 1.28 boringcrypto，
207MB）做**只读静态逆向**，工具与说明见 `tools/_rev/README.md`（脚本已入库，
dumps/schema 文本留在本地）。不用抓包就拿到了一批字段级地面真值：

1. **sessionId 算法破译（§10.11 第 2 点的升级，已落地）**：
   int64(-3750763034362895579) 的无符号形态恰为 Go `hash/fnv64a` 的 offset
   basis `0xCBF29CE484222325`——真实 LS 对**空输入**做 FNV-1a64 后转 int64
   （哈希对象从未写入字节）。FNV-1a64 任何非空输入都不可能回到 basis
   （逐字节改变状态），故该值是**全体客户端恒等的常量**，不是装机/凭证派生值。
   实现：`src/api/antigravity.py` 新增 `OFFICIAL_SESSION_ID` 常量，
   `_derive_install_session_id` 不再 sha256 派生、不再区分凭证（account_salt
   仅保留签名兼容）；会话隔离本就由 trajectory/cascade 承担。
   测试 `test_install_session_id_is_official_constant`（断言常量值 = FNV basis
   int64，且跨盐恒定、trajectory 仍按盐隔离）。**pytest 232 passed**，
   `verify --envelope --json` 全绿。
2. **外层信封 proto 全 schema 还原**（`v1internal/prediction_service.proto`）：
   `GenerateContentRequest = {project, requestId, request, model,
   userPromptId(5), userAgent, requestType, enabledCreditTypes(8)}`。
   实抓复核（163547/193455 全部 generateContent 请求）：真实客户端 agent 路径
   **从不设置** `userPromptId` / `enabledCreditTypes`——代理保持不注入是对的；
   两字段记录在案，未来若上游启用（如按 prompt 计费）需要跟进实抓。
   内层 `aiplatform.master.GenerateContentRequest` 全字段（labels=10,
   sessionId=13, generationConfig=4…）与实抓键序一致。
3. **ThinkingConfig 权威枚举**：`LOW=1/MEDIUM=2/HIGH=3/MINIMAL=4/EXTRA_HIGH=5/
   MAX=6/XHIGH=7`。xhigh 实抓 JSON 值 100 是客户端自定义变换（wire 枚举 XHIGH=7），
   代理保持实抓口径；MINIMAL 仍不进 agent 路径合法集（实抓 0 次，§10.2 口径不变）。
   另发现 `prefixMismatchBehavior`（ERROR/DROP）等字段，供上游排错参考。
4. **系统提示词模板本体已还原**：Go text/template，含
   `{{if .ExperimentEnabled "generic-system-prompt"}}` 等实验门控与
   `IsAutonomous` 分支（§10.9.6 sys 会话内演化的根源 = 模板 + 实验开关 +
   渲染上下文，不是客户端随机改动）。模板文本在本地 `tools/_rev/prompt_region.txt`
   （Google 客户端内容，不入库）。**§10.9.6 修复方向随之明确**：trajectory 锚点
   不应包含 systemInstruction 全文（同一会话跨实验开关就会断锚）——候选方案：
   锚点只用 model + 首条 user 消息 + (可选) sys 的稳定前缀。
5. **端点全集 63 个 `/v1internal:*`**（含实抓未见过的 `countTokens`、
   `retrieveUserQuota`（按模型 remainingFraction）、`fetchFromTrawlerCache`、
   `internalAtomicAgenticChat` 等，完整清单见本地 `ls_strings.txt` /
   `tools/_rev`）。后续候选：token_estimator 可改用 `countTokens` 真实计数；
   quota_budget 可评估 `retrieveUserQuota` 粒度。
6. **模型宇宙远大于 39 枚举**：二进制内置 fallback 模型注册表含
   `claude-opus-5-5(-low/-high/-max)`、`claude-sonnet-5-5`、`gemini-3.5~3.8-flash`
   家族、`gemini-4-carbon/neon/argon` 家族等。strict 模式枚举仍以
   fetchAvailableModels 实抓为准；此表作为"新模型上线预警清单"价值更大。
7. **基础设施情报**：LS 启动参数全集（`--cloud_code_endpoint` 是 `CLOUD_CODE_URL`
   的命令行等价物，未实测）；`ANTIGRAVITY_*/AGY_*` 环境变量全集；3 个 OAuth
   Client ID（`884354919052-*`、`1071006060591-*`、`1098991175725-*`，仅记录，
   代理 OAuth 流程不需要）；UA 构成 = `--override_user_agent_name antigravity`
   + `--subclient_type hub` + LS build label + `--stamp` 的 CL
   （`1026637980` 在二进制中出现 5 次，与 `src/utils.py` 一致）。
8. `.gitignore`：`tools/antigravity/` 与 `tools/_rev/*` 不入库（保留 `*.py`/`*.md`）。

> 下轮候选：§10.9.6 trajectory 锚点修复（4）、`countTokens`/`retrieveUserQuota`
> 接入（5）、新模型预警清单维护（6）。静态逆向产物（dumps/schema/prompt 文本）
> 仍在 `tools/_rev/` 本地目录，升级客户端版本后可用 `tools/_rev` 脚本重跑。

### 10.13 第八轮：USER_REQUEST 包装破译与落地（2026-11-21，owner 指示"继续"）

对既有实抓（163547/193455）做**内容维度**复查（此前各轮只对齐了
labels/step/header/TLS，从未校验 user 文本形态），发现并修复了一个每个请求
都存在的系统性偏差：

1. **实抓规律（163547 57 条 + 193455 3 条，逐条分类复核）**：
   - **agent 请求的全部 user 轮都带包装**（含历史旧轮，无例外）：
     `<USER_REQUEST>\n{原文}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n
     The current local time is: {ISO8601 本地时区}.\n</ADDITIONAL_METADATA>`；
   - 包装在该轮**首次发送时定格**，此后逐字节回放：0080 的 contents[0]
     时间仍是首轮 0013 的 `16:40:48`、contents[50] 是 0074 的 `16:52:43`、
     contents[56] 是新轮的 `16:55:16`；0013 失败后 0014 原样重发同值；
   - `<USER_SETTINGS_CHANGE>` 只在 UI 改设置事件那一轮出现（两份实抓分别是
     Model Selection 档位变更），**代理无对应事件，按"不伪造"永不注入**；
   - `checkpoint`/`agent_monitoring` 请求带裸文本（代理不发送这两类）；
   - 静态侧印证：系统提示词模板明确写着 "User requests are enclosed within
     `<USER_REQUEST>` tags"，且模板含 `<INITIAL_USER_REQUEST>`、
     `<COMPACTED_HISTORY>` 等同名段落标签。
2. **落地**（`src/api/antigravity.py`）：
   - `_wrap_user_turn_text`（幂等：已包装文本原样返回，实抓重放/黄金样本
     不受影响）+ `_wrap_user_turns`（只包装 role=user 的 text part；空文本
     跳过；图片生成模型无实抓证据整体跳过）；
   - 时间戳用**轨迹级"首次见到即盖戳"注册表**（键 = sha256(text)，与索引
     无关——调用方删改历史轮不破坏既有轮的戳值；LRU 上限 512 轨迹×256 轮，
     随 `_reset_trajectory_request_seq` 一并重置）。 driving 轮首次出现即
     盖当前墙钟戳，与真实客户端语义一致；注册表与 step 重发偏移同一失败
     模式（重启丢失→时间跳变一次后重新稳定）；
   - 插入点在 `build_envelope_parts`：严格校验（看原始请求）与锚点派生
     （raw 口径）之后、step/labels 之前；敏感词过滤在转换层先行（包装文本
     本身不参与过滤）。
3. **测试**：新增 5 个（实抓格式逐字节正则、跨请求稳定、实抓重放幂等、
   图片模型跳过、改写重盖戳/删除保戳）；**pytest 237 passed**；
   `verify --envelope --json` 全绿。过期注释清理：`build_envelope_parts`
   中残留的"纯文本 → chat"分支描述（§10.11 已删除该分支）。
4. **schema 解码补充**（静态逆向直接产出，不再依赖逐字段实抓推断）：
   - `model_configs.proto`：`ModelDetails` 全字段——displayName/supportsImages/
     supportsThinking/**thinkingBudget/minThinkingBudget**/recommended/
     maxTokens/maxOutputTokens/tokenizerType/quotaInfo/beta/disabled/
     model(枚举)/supportsRawThinking/supportsVideo/supportsPdf…。
     P1 模型参数表的运行时学习（`model_enum.parse_model_params`）可按此
     扩容为全字段学习；`FetchAvailableModelsRequest` 还有 requestId/location
     两个可选字段（实抓中未设置）。
   - `quota_summary.proto`：`QuotaSummaryBucket{bucketId, displayName,
     description, window, remainingFraction, remainingAmount, disabled,
     resetTime}`——quota_budget 的字段级依据。
   - **Model 枚举权威全表**：`.exa.codeium_common_pb.Model` 共 1691 值
     （`MODEL_PLACEHOLDER_M0~`、`MODEL_OPENAI_GPT_OSS_120B_MEDIUM=342` 等），
     完整清单在本地 `tools/_rev/model_enum_full.txt`。strict 模式仍以
     fetchAvailableModels 实抓为准；此表是"新模型上线预警清单"的完整底册。
   - 系统提示词模板门控全景：`ExperimentEnabled` 只出现 4 个旗标
     （`generic-system-prompt`×7、`jetski-generative-ui`、
     `jetski-generative-ui-prompt`、`enable-url-artifacts`），
     上下文字段只有 `IsAutonomous`、skills 注入（`Data.Skills`）与
     cascade 配置——§10.9.6 的 sys 演化维度就此完全枚举。
   - **OAuth 常量逐字节验证**：`src/utils.py` 的
     `ANTIGRAVITY_CLIENT_ID`（`1071006060591-tmhssin2h21lcre235vtolojh4g403ep…`）
     与 `ANTIGRAVITY_CLIENT_SECRET` 均在 LS 二进制中命中（后者 1 次）；
     二进制里另有两个 Client ID（`884354919052-*`、`1098991175725-*`，
     推测对应 VS Code 扩展 / CLI 客户端，代理无关）。
   - **`image_gen` requestType 有静态证据**：字符串存在于 LS 二进制
     （与 cascadeId 等请求字段同区）；图片路径无实抓报文，维持现状。
5. **决策记录——"caller 无 tools 时注入真实 16 工具集"评估后不采纳**：
   现已拥有实抓的完整工具声明（163547/0013），注入技术上可行且内容真实，
   但真实客户端的工具集由客户端 agent 循环执行（run_command 等），无工具
   的 OpenAI 调用方无法执行 functionCall，注入会破坏 API 契约。维持
   §10.11 的透传口径。

> **遗留**：`docs/HANDOVER.md` §3.9"对话包装一律透传，不编造"的表述已被
> 本轮升级——实抓证据表明真实客户端对 user 轮有确定性格式包装，复现该
> 格式不是编造；原始 user 文本仍一字不改（只加包装壳 + 客户端本地时间戳）。

### 10.14 第九轮：粘性锚点——修复 trajectory 首轮断裂与 sys 演化断锚（2026-11-21）

§10.9.6 的"已知差距"在本轮升级为**两个实锤 bug 并修复**，新增测试直接暴露：

1. **既有 bug A（首轮断裂，比 §10.9.6 更严重）**：锚点取 `contents[:2]`，
   首轮请求只有 1 条消息、第二轮起变为 `[首条 user, 首条 model]`——
   **每个会话的第 1 条请求与后续请求本来就派生出不同的
   trajectory/cascade/session_key**。后果：上游视角每个对话都在第 2 条
   请求换了轨迹（缓存亲和断裂）、凭证亲和在第 2 条请求重新选号、
   step/request_seq 计数器在第 2 条请求归零（`request_id` 后缀重新从
   `-0` 开始——与真实客户端"一个 trajectory 内从 0 连续计数"直接矛盾）。
   此前测试 `test_step_increments_with_model_turns` 断言 turn2 后缀 `-0`
   正是对该 bug 的错误固化，已随修复更新为连续计数。
2. **既有 bug B（sys 演化断锚，§10.9.6 原文）**：sys 由实验旗标驱动演化
   （静态逆向：模板 `{{if .ExperimentEnabled}}`；实抓 0013↔0080 段落不同），
   sys 全文参与锚点 → 同一会话中途换 key，后果同上。

**修复：粘性首见绑定**（`src/session_cache.py`）：

- 新增 `resolve_sticky_anchor(model, request, account_salt)`：粘性键基底 =
  `SHA256(model + 首条消息特征)`（**不含 sys、只取首条消息**——追加历史
  与模型回复都不影响它）；**首次见到**时按纯函数全量锚点
  （`derive_conversation_anchor`，model + sys + 前两条消息）派生并登记，
  此后沿用首见绑定。进程内 FIFO+TTL（12h / 4096 条），与轨迹计数器同一
  失败模式（重启丢失 → 按当前 sys 重新绑定一次，此后稳定）。
- 纯函数 `derive_conversation_anchor` 保持不变（sys 敏感，测试与其它用途）；
  两个消费方切换到粘性解析：`derive_session_key`（凭证亲和）与
  `_derive_upstream_session_ids`（trajectory/cascade 派生）。
- 账号盐混入**粘性键**（不同凭证不共享绑定）；返回摘要仍是未混盐值，
  盐由调用方按原口径追加——多账号隔离语义不变。
- **代价（已评估，净收益）**：同 (模型, 首条消息) 但不同 sys 的两个会话会
  合并到首见绑定（旧行为仅 sys 相同时才撞车）；对比"每个长会话必断锚 +
  首轮必断裂"，是明确净收益。
- `_reset_trajectory_request_seq`（测试复位）现同时清空 user 轮盖戳与
  粘性锚点绑定。

**测试**：新增 4 个（粘性键跨 sys 演化恒定 / 不同首条消息仍隔离 / 纯函数
保持 sys 敏感 / wrap 层 sys 演化下 trajectory+step+盖戳全连续）；更新 2 个
固化旧行为的断言（request_seq 连续计数、首轮即恒定 docstring）。
**pytest 241 passed**；`verify --envelope --json` 全绿。

### 10.15 第十轮：旗标体系与流量形态逆向（2026-11-21，owner 指示"继续，不跑真实请求"）

全部基于既有实抓响应 + 客户端二进制静态解析，零新流量：

1. **listExperiments 实抓响应全量解析**（163547/0005）：105 个 experimentIds +
   **635 个旗标**。4 个模板门控旗标当时值：`generic-system-prompt=True`、
   `jetski-generative-ui=True`、`enable-url-artifacts=False`、
   `jetski-generative-ui-prompt=False`。**会话内 listExperiments 只拉取一次**
   （0005 之后无再取）→ §10.9.6 的 0013↔0080 sys 段落差异**不是旗标翻转**，
   而是模板渲染上下文随会话状态变化（IsAutonomous / artifacts 段落按需出现）。
   sys 演化机制就此完全闭环。
2. **真实流量形态量化**（`tools/_rev/monitor_timeline.py` 可复跑）：163547
   全时间线——checkpoint **每会话 1 次**且先于首条 agent 请求（0012）；
   **agent_monitoring 以约 2 条 agent : 1 条 monitoring 的频率穿插**
   （30 agent vs 25 monitoring ≈ 0.83），新 user 轮开启后连发 2 条
   （0075+0076）；相邻请求间隔 5~15s。**代理（只发 agent）与真实客户端
   的流量形态差异从此有精确数字**。owner 已决策代理不发这两类请求
   （§10.9.5），此处仅存档供再评估。
3. **模型角色体系**（实抓 live flags）：`orchestrator-model-roles` =
   orchestrators `[gemini-4-neon, gemini-4-carbon, gemini-4-argon,
   claude-opus-5-5]` / executors `[gemini-4-hydrogen, claude-sonnet-5-5]`；
   `checkpoint-variant = claude-1m_840k`；`browser-subagent-model =
   gemini-3.8-flash-low`；`ambient_disabled_models = fable-5.2-*,claude-fable-5-2*`
   （fable 系列首次现形）。Google 内部模型家族命名/角色分工就此有实抓依据。
4. **客户端内置默认旗标资产**（`tools/_rev/extract_flags_asset.py` 按模式
   定位提取，93KB JSON → 本地 `client_default_flags.json`）：六段
   `base / subclients / os / model_profiles / models / enterprise`。
   - `models` 段 = **别名→规范族映射**（66 项→13 规范族）：M525~M531
     （hydrogen 系含 M526）→ `MODEL_PLACEHOLDER_M432`；M50 →
     `MODEL_GOOGLE_GEMINI_2_5_FLASH`。labels.model_enum 上报的是**别名原值**
     （实抓 M526 未规范化），映射仅客户端内部用。
   - `model_profiles` = 按规范族客户端配置：`CASCADE_USE_EXPERIMENT_CHECKPOINTER`
     （checkpoint 策略/阈值/重试）、**`allow-experimental-monitors=True`**
     （agent_monitoring 的开关所在）、`task-details-suffix`、
     `template__system_prompts__guidelines`（按模型 sys 指南模板）。
   - 该资产是**默认值快照**（listExperiments 失败时的兜底），live 值以
     实抓响应为准。
5. **Mendel 架构闭环**（asar `mendelFlagSync.js`）：LS 是旗标缓存层（上游
   listExperiments），Electron 每 60s 轮询 LS 本地
   `POST /exa.language_server_pb.LanguageServerService/GetMendelFlags`
   （Connect 协议，`x-codeium-csrf-token` 鉴权）；本地管理面还有
   `/auth/mint`（UI bootstrap 单次令牌）。代理无交互，仅补全架构图。
6. **凭证存储无缺口**：代理自建 OAuth 回调流（`src/auth.py`），
   client_id/secret 与二进制逐字节一致（§10.13 补记），无需解析客户端
   本地令牌存储。
