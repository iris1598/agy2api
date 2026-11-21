# Antigravity 真实请求抓取工具

这个工具用来抓 **Antigravity 桌面版 Hub**（`language_server.exe`）真正发往 Google 的
`/v1internal:*` 请求与真实 TLS 指纹，方便核对 `agy2api` 的出站报文、header、协议与
TLS 指纹。**所有指纹结论以本目录工具实抓的数据为准，不要引用任何"记忆中的值"。**

它不修改 Antigravity 安装目录，不安装 CA 证书，不需要 MITM。

> 抓包文件里会有你的提示词、代码片段、文件路径等隐私内容。默认会脱敏
> `Authorization` / `Cookie` / `x-goog-api-key`，但请求 body 仍会原样保存。
> 抓包目录已被 `.gitignore` 忽略，用完请及时删除。

---

## 0. 实测结论速览（2026-11-20，language_server 2.33.0）

以下均为本机（Windows，Antigravity 桌面版 44.9.1）实测，不是推测：

1. **`CLOUD_CODE_URL` 环境变量机制实测有效**。设置后
   `language_server.log` 会打印
   `Overriding CloudCodeServerURL via CLOUD_CODE_URL environment variable: "..."`，
   之后 `/v1internal:*` 请求全部发到指定地址。
   静态逆向（2026-11-21，`tools/_rev`）发现等价的**命令行参数**路径：
   Electron 用 `--cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com`
   启动 LS（见 `tools/_rev/README.md`），理论上可从命令行覆盖——未实测，
   重定向仍以 `CLOUD_CODE_URL` 为准。
2. **token 刷新不走 `CLOUD_CODE_URL`**：LS 始终直接 POST
   `https://oauth2.googleapis.com/token`。因此**离线/重定向捕获的前提是 LS 能连上
   Google**（开 Clash TUN，或给 Antigravity 进程注入
   `HTTPS_PROXY=http://127.0.0.1:<Clash混合端口>`）。token 刷不出来时 LS
   一个请求都不会发（会一直重试 `Failed to get OAuth token`）。
3. **真实 User-Agent（2026-11-21，language_server 2.34.0 实测）**：
   `antigravity/hub/2.34.0 (aidev_client; os_type=windows; arch=amd64; cl=1026637980)`
   —— 已同步到 `src/utils.py`（2.33.0 的旧值是 `1023160026`）。
4. **真实 header 集合（HTTP/1.1 视角）**：`User-Agent`、`Authorization`、
   `Content-Type: application/json`（仅 POST）、`Accept-Encoding: gzip`。
   **没有 `Accept`，也没有 `x-goog-api-client`**——README 旧版"真实客户端带
   x-goog-api-client"的说法与实测不符，代理侧不要凭空伪造。
5. **真实 body envelope**：`loadCodeAssist` → `{"metadata": {"ideType": "ANTIGRAVITY"}}`
   （第二变体加 `cloudaicompanionProject`）；`listExperiments` →
   `{"project", "metadata": {"updateChannel": "stable"}}`；
   `fetchUserInfo` / `fetchAvailableModels` → `{"project"}`；
   `writeTrajectoryAcls`（2.34.0 新增）→ `{"trajectoryId": "<uuid>"}`，响应 `{}`。
6. **2.34.0 的 `requestType` 三形态**：`agent`（常规对话，`requestId` 形如
   `agent/<cascade>/<ms>/<trajectory>/<step>`）/ `checkpoint`（`checkpoint/<uuid>`，
   flash-lite，无 labels，contents 只带原始用户文本）/ `agent_monitoring`
   （见 6.2）。**对话请求一律 agent，`chat` 在实抓中 0 次出现**（193455 问候
   抓包复核：纯文本也是 agent + 16 工具声明）。
   `labels` 九键或十键（含 `last_execution_id`，见 6.3）。
6.1 **step 与 request_id 规律（20261121-163547 真实转发会话 33 条 agent 请求
   全序列拟合；推翻本节旧公式 `1 + 2×model 消息数`——那是离线样本的巧合）**：
   - `step = user 轮数 + 非(纯 functionResponse)的 model 轮数
     + functionCall part 数`（fr 不计轮，**并行调用按 part 数计**）；
   - step 不是纯 contents 函数：**contents 未推进的重复请求 = 客户端重发，
     step +1 且此后轨迹永久带该偏移**（163547 的 0013 请求 502 失败 → 0014
     原样重发，step 1→2）；
   - `labels.last_step_index = step - 1`；
   - `labels.request_id` 后缀是 trajectory 内**从 0 开始**的逐请求计数；
   - 工具调用 part 形如
     `{"functionCall": {"id": "<uuid>", "name": "...", "args": {...}},
       "thoughtSignature": "<base64 skip_thought_signature_validator>"}`，
     工具结果 part 形如
     `{"functionResponse": {"id": "<同上>", "name": "...", "response": {"output": "..."}}}`。
     合成的 functionCall 可以不带 id/签名，客户端会自行补齐。
6.2 **子 agent 与 `agent_monitoring`（20261121-114847 实测）**：
   - 子 agent 会开**新 cascade**：`cascade_id` 是新的，`root_cascade_id` 仍是父会话的
     cascade；`trajectory_id` 全新；`request_id` 在新 trajectory 内**从 0 重新计数**；
   - 父会话继续推进：工具轮 step 3 / `request_id` 后缀 1，之后 step 5 / 后缀 2；
   - 第三种 `requestType: "agent_monitoring"`：`requestId` 形如
     `agent_monitoring/<uuid>`（只有两段），模型为 `gemini-3.5-flash-lite` /
     `gemini-4-hydrogen-sonar`，`thinkingLevel` 可以是 `MINIMAL`，且**没有 labels**；
     内层键为 `contents → systemInstruction → generationConfig → sessionId`（无 tools）；
   - `invoke_subagent` 的 functionCall / functionResponse 共享同一个 `id`。
6.3 **labels 十键 `last_execution_id`（163547/193455 实抓闭环）**：
   - 值由**客户端本地生成**（uuid4），不出现在任何上游响应中；
   - 存在"最后一个 user 轮之前的 functionResponse 轮"时携带；同一工具轮在
     同一 user 周期内保持同值，新 user 轮开启后更替；
   - 键序在 `cascade_id` 之后（字母序）；
   - 实现口径见 `src/api/antigravity.py` 的 `_resolve_last_execution_id`。
6.4 **thinkingLevel 形态（193455 实抓）**：hydrogen 系 low/medium/high 为
   字符串 `"LOW"/"MEDIUM"/"HIGH"`；**xhigh（extra high）为数值 `100`**；
   flash-lite 心跳为 `"LOW"`/`"MINIMAL"` 字符串。hydrogen 系
   maxOutputTokens 统一 262144。
6.5 **sessionId 装机级稳定**：163547 与 193455 两个不同会话（不同
   cascade/trajectory）的 sessionId 同为 `-3750763034362895579`——真实客户端
   不按对话换 sessionId。

7. LS→回环捕获服务器走 HTTP/1.1（回环链路产物）；真实上游协议以 ALPN 实测为准，见第 8 条。
8. **真实 TLS 指纹（tlsproxy 实测，2026-11-21 在 2.34.0 上复测未变）**：LS 对 `daily-cloudcode-pa.googleapis.com`
   等全部 API 域名的 ClientHello JA3 完全一致：
   - JA3 md5: `03117a8ed39ef02427ebbc39f121275c`
   - JA3 完整串:
     `771,49195-49199-49196-49200-52393-52392-49161-49171-49162-49172-4865-4866-4867,0-11-65281-23-18-5-10-13-50-16-43-51,4588-4587-4589-29-23-24-25,0`
   - **ALPN 只提供 `http/1.1`**（gstatic/play 等非 API 组件才提供 h2）→ 代理侧对
     Cloud Code API **保持 HTTP/1.1，不要启用 h2**；
   - 密码套件/曲线是 Go 标准库形态（含 ML-KEM 后量子组 4588/4587/4589），
     代理侧用 `tools/upstream_tls_proxy`（Go，显式钉住曲线组顺序）复刻该串，
     实测 JA3 md5 完全一致。

---

## 1. 三种抓取方式与风险矩阵

| 方式 | 命令 | 需管理员 | 改客户端？ | 重启客户端？ | Google 侧可见性 | 能拿到什么 |
| --- | --- | --- | --- | --- | --- | --- |
| **TLS 中继抓取** | `sniff_cli.py tlsproxy --launch` | **否** | 否 | 是 | **无感知**（端到端 TLS 原样转发） | JA3、ALPN、SNI、连接时序 |
| **被动 TUN 抓包** | `Start-Sniff.ps1 passive` | 是 | 否 | 否（默认） | **完全无感知**（纯监听） | JA3/JA4、ALPN、SNI、DNS、全域名清单 |
| **离线重定向** | `run_capture.ps1 -Offline -CaptureAll` | 否 | 否 | 是 | **零模型流量**（仅模型生成端点回合成响应；启动/元数据请求经 Go 转发器正常取回） | 明文 header/body/envelope |
| 完全离线 | `-OfflineAll`（等价 `--offline-all`） | 否 | 否 | 是 | 零流量（所有 `/v1internal:*` 都不转发；可能出现"没有可用模型"） | 同上 |
| 转发重定向 | `run_capture.ps1`（不带 -Offline） | 否 | 否 | 是 | **Go 转发器出站（JA3 与真实客户端一致），但会真的发送模型请求** | 明文报文 + 真实回复 |

推荐流程（各拿各的，互不污染）：

```
1. sniff_cli.py tlsproxy --launch         # TLS 指纹（无需管理员；经 Clash 正常转发）
2. run_capture.ps1 -Offline -CaptureAll   # 明文报文（零模型流量，元数据正常转发）
3. sniff_cli.py report                    # 合并成 fingerprint_report.md + diff src/
4. sniff_cli.py verify                    # 闭环校验代理实际出站指纹（差异时退出码非 0）
# 可选：Start-Sniff.ps1 passive（有管理员时跑一次，补充 JA4/QUIC/全域名清单）
```

---

## 2. 被动 TLS 抓包（tlsproxy / passive 两种模式）

两种模式都是纯被动：Google 看到的 TLS 字节与正常使用完全一致，无感知。

### 2.1 tlsproxy 模式（无需管理员，推荐）

原理：实测 LS 遵守 `HTTPS_PROXY` 环境变量。本模式在本地起一个 CONNECT 中继
（127.0.0.1:7898 → Clash 混合端口 → Google），重启 Antigravity 并注入
`HTTPS_PROXY` 指向中继。LS 通过代理访问 HTTPS 时先发明文 `CONNECT`（暴露目标域名），
TLS ClientHello 是端到端字节穿过隧道——中继**只读不写**这些字节、原样转发，
即可提取 SNI / JA3 / ALPN。Google 看到的 TLS 与正常使用逐字节一致。

```powershell
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py tlsproxy --launch --yes
# 或指定时长 / 端口
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py tlsproxy --duration 180 --launch --yes
```

- 前置：Clash 在跑（上游自动读 `clash-verge.yaml` 的 `mixed-port`，默认 7897）；
- 产物（`captures/tlsproxy-<时间戳>/`）：`connects.jsonl`（所有 CONNECT 目标）、
  `client_hellos.jsonl`（逐条 ClientHello + ServerHello ALPN）、`ja3_summary.json`
  （report 输入）；
- 结束后**关掉 Antigravity 重开一次**（它注入了指向本中继的代理，中继停了就断网）；
- 局限：只覆盖遵守 `HTTPS_PROXY` 的请求（实测 LS 全部 API 流量都遵守）；Chromium 侧
  流量走系统代理，不经过这里（本来也不是指纹重点）。

### 2.2 passive 模式（TUN 网卡 + Wireshark，需管理员）

原理：Clash TUN 开启时，mihomo 会创建一块 TUN 虚拟网卡，客户端进程发出的原始
TLS ClientHello（明文部分：SNI、JA3/JA4 指纹、ALPN）从这块网卡经过。在这块网卡上
**纯监听**，即可拿到真实客户端的 TLS 指纹。

前置条件：

- **管理员权限**（Npcap 抓包要求；`Start-Sniff.ps1` 会自动弹 UAC 提权）；
- Wireshark（tshark/dumpcap）+ Npcap：缺失时脚本会用 winget 自动安装
  （`--no-install` 跳过；无网络时可手动装本机已下载的
  `%LOCALAPPDATA%\Temp\wireshark-setup\Wireshark-4.6.10-x64.msi`）；
- **Clash TUN 必须开启**，且出口节点为非 CN（否则 Google 上游直接 403
  `LOCATION_NOT_ELIGIBLE`，LS 连请求都发不全）；
- 抓**物理网卡没有分析价值**——TUN 关闭时物理网卡上只有到代理节点的加密隧道。

```powershell
# 一键（自动提权 + 自动装 Wireshark + 自动选 TUN 网卡）
powershell -ExecutionPolicy Bypass -File .\tools\antigravity_capture\Start-Sniff.ps1 passive

# 常用变体
.\tools\antigravity_capture\Start-Sniff.ps1 passive --duration 120   # 2 分钟自动停止
.\tools\antigravity_capture\Start-Sniff.ps1 passive --launch         # 顺带重启客户端并注入 SSLKEYLOGFILE
.\tools\antigravity_capture\Start-Sniff.ps1 passive --interface <编号>   # 手动指定 dumpcap 接口
```

流程：启动 dumpcap 抓 TUN 网卡（tcp/udp 443 + udp 53）→ 在 Antigravity 里正常发消息
→ 回车停止（或 `--duration`）→ 自动跑 analyze 生成报告。

关键说明：

- **Go language_server 忽略 `SSLKEYLOGFILE`**（boringcrypto），被动抓包**永远拿不到**
  它的明文 header/body——那部分用离线重定向抓。`--launch` 注入的 keylog 只能解密
  Chromium/Node 侧流量（插件市场、遥测等），对模型流量无效。
- QUIC/HTTP3：抓包过滤包含 udp 443；如果 Google 域名走了 h3，ClientHello 一样能被抓到。
- passive 模式额外提供 JA4、QUIC 观测与全域名清单（tlsproxy 只覆盖走代理的连接）。
- 产物（在 `captures/passive-<时间戳>/`）：`tls_report.md`（人读）、`ja3_summary.json`
  （report 输入）、`connections.csv`、`domains.txt`、可选 `http2_headers.csv`、`keylog.txt`。

---

## 2.5 闭环校验（sniff_cli.py verify）与模型枚举（models）

`verify` 不再依赖人眼读报告：它在本地起一个"只读 ClientHello 就关闭"的监听器
（不完成握手、不需要证书），让代理自己的出站实现去连它，逐段比对
JA3/密码套件/扩展/曲线组/点格式/ALPN，同时抓取转发器写出的上游 header 顺序。
任一不匹配退出码非 0，可直接接 CI。

```powershell
# 需要先编译 Go 转发器（见 tools/upstream_tls_proxy/README.md）
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py verify
# 机器可读 + 期望值覆盖（真客户端升级后无需改代码）
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py verify --json --ja3 "771,..."
```

`schema` 从抓包会话导出仓库根部的 `fidelity_schema.json`（端点 header 顺序/分帧、
信封键序、requestType 取值、labels 键序、每模型的 model_enum 与 generationConfig）。
它不含账号数据，作为运行时严格校验与 CI 的唯一基准：

```powershell
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py schema
# verify --envelope 会用它校验当前实现产出的信封
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py verify --envelope
```

> 抓包 meta 现在额外记录 `framing`（chunked / content-length / none）、`client_port`、
> 同一连接内的请求序号与到达间隔；离线模式支持下游 keep-alive，便于观察真实客户端的
> 连接复用。tlsproxy 汇总额外统计"完整握手 / 恢复握手"次数（扩展 35/41）。

`models` 从 `fetchAvailableModels` 响应导出 `model_id → MODEL_PLACEHOLDER_*` 映射，
可直接贴进 `src/model_enum.py`（离线会话的合成响应带 `synthetic: true`，会被自动跳过）：

```powershell
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py models
```

> 已实测：Go 转发器（`tools/upstream_tls_proxy`，Go 1.27.4，显式钉住曲线组）
> 产出的 ClientHello 与真实客户端 **JA3 md5 完全一致**
> （`03117a8ed39ef02427ebbc39f121275c`），ALPN 只有 `http/1.1`。

## 3. 指纹报告（sniff_cli.py report）

合并"被动会话 + 离线重定向会话"，生成 `captures/fingerprint_report-<时间戳>.md`：

- 真实客户端 TLS 指纹（按域名列出 JA3/JA4/ALPN/版本，含 JA3 完整串）；
- 真实 HTTP 报文（按 endpoint 列 header 顺序与值、body envelope 键序）；
- agy2api 当前指纹（解析 `src/utils.py` / `src/api/antigravity.py`）；
- 差异与建议（UA 版本/CL diff、header 缺失/多出、可直接粘贴的建议常量块）；
- 注意事项。

```powershell
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py report
# 指定会话
.venv\Scripts\python.exe tools\antigravity_capture\sniff_cli.py report --passive-dir <目录> --redirect-dir <目录>
```

报告**不会自动改任何源码**；建议常量块人工核对后自行替换
`src/utils.py` 的 `OFFICIAL_HUB_VERSION` / `OFFICIAL_HUB_CL`。

---

## 4. 离线重定向（run_capture.ps1 -Offline）

原理：Antigravity 的 `language_server.exe` 支持 `CLOUD_CODE_URL` 环境变量（实测确认，
见第 0 节）。把该变量指向本机 `http://127.0.0.1:<port>` 后，Hub 会把 `/v1internal:*`
请求发到本地捕获服务器。`-Offline` 只让 `generateContent` / `streamGenerateContent`
返回合成响应（**零模型流量**）；`loadCodeAssist` / `fetchAvailableModels` /
`listExperiments` 等启动与元数据请求仍经 Go 转发器正常取回——否则客户端会显示
"没有可用模型"。需要完全不联系 Google 时用 `-OfflineAll`（模型列表可能为空）。

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\antigravity_capture\run_capture.ps1 -Offline -CaptureAll
```

脚本会：启动捕获服务器（127.0.0.1，默认 18080）→ 设置 `CLOUD_CODE_URL` →
关闭已运行的 Antigravity → 从当前 PowerShell 重新启动 → 读
`%APPDATA%\Antigravity\logs\language_server.log` 确认出现
`Overriding CloudCodeServerURL via CLOUD_CODE_URL` → 等你发一条消息 →
关闭 Antigravity 后自动停止服务器。

### 4.1 离线抓"工具调用 / 子 agent"（-OfflineScript）

工具调用只存在于模型回复里，离线合成的文本回复不会触发它。用 `-OfflineScript`
让捕获服务器主动合成一次 `functionCall`，客户端就会执行工具并回传
`functionResponse`，从而抓到完整回路——**仍然零模型请求**。

```powershell
# 抓工具调用：合成一次 read_file（只读本会话创建的临时文件）
.\run_capture.ps1 -Offline -CaptureAll -OfflineScript tool-call

# 抓子 agent：合成一次 invoke_subagent(TypeName=research)
.\run_capture.ps1 -Offline -CaptureAll -OfflineScript subagent
```

安全边界：

- 只允许 `read_file`（路径固定为本会话目录下的 `_probe_read_file.txt`，内容 `ok`）
  和 `invoke_subagent`（仅 `research`：只读、无网络、无写权限）；
- **绝不合成** `run_command` / `write_file` / `edit_file` / `search_web` / `swarm` 等有副作用工具；
- 每次运行只触发一次（检测到 contents 已有 functionCall/functionResponse 即停止），
  避免递归或重试风暴；
- 子 agent 的模型回复仍是合成文本，不会向 Google 发模型请求。

参数：`-Port`（默认 18080）、`-Offline`（仅模型端点离线）、`-OfflineAll`、`-OfflineScript`、`-CaptureAll`（默认只存
generateContent/streamGenerateContent）、`-CaptureAuth`（危险：不脱敏认证头）、
`-NoLaunch` / `-NoWait` / `-ForceClose`、`-AntigravityPath`、`-Python`。

### 实测注意事项（2026-11-20）

- **TUN 没开时离线模式也会卡死**：LS 启动必须先连 `oauth2.googleapis.com` 刷 token，
  刷不出来就一个请求都不发。TUN 关闭时手动注入代理后重启客户端即可：
  ```powershell
  taskkill /IM Antigravity.exe /F /T; taskkill /IM language_server.exe /F /T
  $env:CLOUD_CODE_URL = "http://127.0.0.1:18080"
  $env:HTTPS_PROXY = "http://127.0.0.1:7897"   # Clash 混合端口；Go 自动绕过 127.0.0.1
  & "$env:LOCALAPPDATA\Programs\antigravity\Antigravity.exe"
  ```
- 离线模式拿到的是**合成响应**，Antigravity 不会正常回复，这是预期行为；
  抓完建议把 Antigravity 关掉重开一次（离线状态下缓存的账号状态不可信）。
- `CLOUD_CODE_URL` 不是公开 API，Antigravity 升级后可能失效——以日志中的
  `Overriding CloudCodeServerURL` 为准。

---

## 5. 输出文件

`captures\<时间戳>\`（离线重定向，每次运行新建）：

```
├── index.jsonl                                  # 所有 /v1internal:* 请求的路径/状态码/字节数
├── 0001_loadCodeAssist.request.bin              # 原始 body
├── 0001_loadCodeAssist.request.json             # 格式化 JSON（envelope 键序以此为准确）
├── 0001_loadCodeAssist.request.meta.json        # header 原始顺序 + http_version + 哈希
├── 0001_loadCodeAssist.response.bin / .txt / .response.meta.json
```

`captures\passive-<时间戳>\`（被动抓包）：`capture.pcapng`（环形轮转时多个分卷）、
`tls_report.md`、`ja3_summary.json`、`connections.csv`、`domains.txt`、可选
`http2_headers.csv`、`keylog.txt`。

`captures\fingerprint_report-<时间戳>.md`：合并指纹报告。

---

## 6. 原理与限制

- Electron 启动语言服务时使用 `env = { ...process.env, ...loginShellEnv }`，
  所以从设置了环境变量的 PowerShell 启动 Antigravity，子进程就能继承。
- LS 对上游的真实 header/协议与回环捕获有细微差别（回环是 HTTP/1.1，Host 头不同，
  Go 会自动补 Content-Length/Host）；分析 header **集合与取值**时以本工具为准，
  顺序和协议版本以被动抓包为准。
- 转发模式**不再由 Python 建连**：请求交给 `tools/upstream_tls_proxy`（Go，与真实
  客户端同 TLS 栈）出站，JA3/ALPN 与实抓一致；二进制缺失时 capture_server 直接拒绝
  启动，绝不回退 Python TLS。它仍会真的向 Google 发送模型请求，只用于必须拿真实回复
  的场景；分析指纹一律用离线模式 + 被动抓包。
- 转发模式保留客户端的 `Accept-Encoding`（与实抓一致），落盘的 `response.txt`
  由 `preview_text` 自动解压。
- 只处理 `/v1internal:*`；OAuth、遥测、更新检查等请求不经过这里
  （被动抓包能看到它们的 TLS 层）。

---

## 7. 抓包后重点核对

以 `sniff_cli.py report` 的输出为准，重点看：

1. `User-Agent` 的版本与 CL（第 0 节实测值）；
2. header **集合**：真实只有 UA / Authorization / Content-Type(POST) / Accept-Encoding；
   多发 `Accept`、伪造 `x-goog-api-client` 都是差异；
3. `loadCodeAssist.metadata`：实测 `{"ideType": "ANTIGRAVITY"}`；
4. TLS：JA3/JA4 + ALPN（`tls_report.md`），代理侧用 Go 转发器对齐（ALPN 只给 http/1.1，不开 h2）；
5. body envelope 键序（对照 `src/api/antigravity.py:wrap_cli_request`）；
6. 模型请求（generateContent/streamGenerateContent）需要你在离线会话里发一条消息，
   启动时的自动请求抓不到这两个端点。

---

## 8. 隐私与清理

- 捕获服务器只绑定 `127.0.0.1`，不接受外部连接；被动抓包只监听不发送。
- 默认脱敏：`Authorization`、`Cookie`、`Set-Cookie`、`x-goog-api-key`、
  `Proxy-Authorization`；请求 body **不会脱敏**。
- 工具不会上传任何数据；抓包文件用完删除：

```powershell
Remove-Item -Recurse -Force .\tools\antigravity_capture\captures\*
Remove-Item -Recurse -Force .\tools\antigravity_capture\logs\*
```

---

## 9. 常见问题

**端口被占用**

```powershell
Get-NetTCPConnection -LocalPort 18080 -State Listen
```

换端口：`-Port 18081`。

**日志里没有覆盖记录**

- 确认 Antigravity 已完全退出（托盘图标也要退出），再用 `-NoLaunch`
  在同一个 PowerShell 里手动启动；
- 检查 `%APPDATA%\Antigravity\logs\language_server.log` 是否为本次启动新写。

**离线模式一条请求都没进来**

- 大概率是 token 刷新失败（见第 4 节实测注意事项）：LS 日志里反复出现
  `Failed to get OAuth token: ... oauth2.googleapis.com` 即是此因。
  开 TUN 或注入 `HTTPS_PROXY` 后重启客户端。

**被动抓包抓不到 ClientHello**

- 确认 Clash TUN 已开（`Get-NetAdapter` 应能看到 mihomo TUN 网卡）；
- 抓的必须是 TUN 网卡；`dumpcap -D` 无接口说明 Npcap 没装好；
- 确认抓包期间 Antigravity 有网络活动（LS 启动即会发 loadCodeAssist）。

**转发报 502 / 超时**

- 先确认 Clash TUN 已开启，或系统能直接访问 Google；
- 看 `tools\antigravity_capture\logs\capture-*.err.log`。
