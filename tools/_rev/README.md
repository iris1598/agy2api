# Antigravity 客户端静态逆向工具（tools/_rev）

对 `tools/antigravity/`（Antigravity 桌面版客户端副本，未入库）做**只读静态
逆向**的脚本集。不执行客户端二进制，只解析字节；产物（strings dump、asar
展开、proto schema 文本）均在本目录，已被 `.gitignore` 忽略，只提交 `.py` 脚本。

## 脚本

| 脚本 | 用途 |
|---|---|
| `extract_asar.py` | 解包 Electron `resources/app.asar` → `asar_out/`（主进程 JS 可读） |
| `dump_strings.py` | 二进制 printable strings dump（Go 二进制字符串表连成片，适合粗筛） |
| `ctx.py` | 按关键字定位并列出二进制上下文（非打印字节转换行）——日常主力 |
| `parse_descriptors.py` | 解析嵌入的 protobuf FileDescriptorProto，打印消息/字段/枚举/服务全 schema |
| `extract_textblobs.py` | 提取长文本块（定位提示词模板等） |
| `session_hash_probe.py` | 对给定输入枚举常见 64 位哈希公式，命中目标 int64 时打印 |

用法统一走 backend venv + `-I`（读取不可信副本数据）：

```bash
.venv/Scripts/python.exe -I tools/_rev/ctx.py tools/antigravity/resources/bin/language_server.exe "v1internal:"
.venv/Scripts/python.exe -I tools/_rev/parse_descriptors.py tools/antigravity/resources/bin/language_server.exe "google/internal/cloud/code/v1internal/prediction_service.proto"
```

## 客户端结构速记（Antigravity 44.9.1 / language_server 2.34.0）

- `resources/app.asar`：Electron 主进程 JS。LS 启动参数在 `dist/lsDefaults.js`
  （`--standalone --override_ide_name antigravity --subclient_type hub
  --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com ...`），
  spawn 逻辑在 `dist/languageServer.js`（`--stamp` 打印 `Built at CL: %d`）。
- `resources/bin/language_server.exe`：Go 1.28 boringcrypto，真实出站请求的作者。
  内嵌 1000+ 个 proto 文件描述符——API 全 schema 可静态还原。
- 系统提示词是 Go text/template（含 `{{if .ExperimentEnabled "generic-system-prompt"}}`
  等实验门控），渲染时随实验开关/上下文演化——这就是实抓中
  systemInstruction 会话内变化的根源（HANDOVER §10.9.6）。

## 已落地的结论

- **sessionId 常量**：真实 LS 的 sessionId = FNV-1a64 **空输入**哈希 → offset
  basis `0xCBF29CE484222325` = int64 `-3750763034362895579`，全体客户端恒等
  （非机器/凭证派生）。已实现为 `src/api/antigravity.py` 的 `OFFICIAL_SESSION_ID`。
- 外层信封 proto（`GenerateContentRequest`）字段全集：project/requestId/request/
  model/**userPromptId(5)**/userAgent/requestType/**enabledCreditTypes(8)**；
  实抓证实真实客户端 agent 请求从不设置后两者——代理保持不注入。
- ThinkingConfig.ThinkingLevel 完整枚举：LOW=1/MEDIUM=2/HIGH=3/MINIMAL=4/
  EXTRA_HIGH=5/MAX=6/XHIGH=7（xhigh 实抓 JSON 值 100 为客户端自定义变换，保留实抓口径）。
- 63 个 `/v1internal:*` 端点全集、3 个 OAuth Client ID、ANTIGRAVITY_*/AGY_*
  环境变量全集——详见 `docs/HANDOVER.md` §10.12。
