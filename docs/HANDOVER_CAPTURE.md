# 交接:真实开发会话抓取 → 实抓基准闭环(下一窗口任务)

> 面向接手的 agent/工程师。任务:owner 将用 Antigravity 做一个**真实开发项目**,
> 借机用**转发模式**抓一次完整开发过程;抓完后解析发现、闭环进实现与测试。
> 当前最高优先待证:`labels.last_execution_id` 的来源(§3)。
> 项目背景与总目录见 `docs/HANDOVER.md`(§10.1–10.8 为上一轮全部记录),此处不重复。

---

## 1. 当前代码库状态(接手即读)

- **12 个本地提交,未推送**(HEAD `a71dff7`);pytest **220 passed, 0 skipped**;
  `sniff_cli.py verify --envelope --json` 通过。owner 决定本轮不 push,CI 验证推迟。
- 信封链路(已完成的架构,改动前必读):
  - `src/api/antigravity.py`:`build_envelope_parts`(值单一来源)/
    `compose_python_envelope`(直连路径参照)/`compose_sidecar_body`(Go 模式)/
    `prepare_cli_request`(模式分派)。
  - `tools/upstream_tls_proxy/envelope.go`:**上游报文字节唯一作者**(protocol 2,
    `/__health` 握手,旧二进制会被拒绝)。
  - `src/upstream_proxy.py`:`REQUIRED_FORWARDER_PROTOCOL = 2`。
- 本机二进制 `tools/upstream_tls_proxy/bin/upstream-proxy.exe` 已是 protocol 2。
  **本机无系统 Go**;portable Go 在 `C:\Users\84637\AppData\Local\Temp\portable-go\go\bin\go.exe`
  (临时目录可能被清理;没了就重新下载 `go1.27.4.windows-amd64.zip` 解压后
  `build.ps1 -Go <路径>`)。改了 Go 代码才需要重建。
- 运行/测试必须用 venv:`.\.venv\Scripts\python.exe`;导入规范见 `AGENTS.md`。

## 2. 已定结论(不要重新推导、不要重新讨论)

以下均有最新实抓证据,记录在 `docs/HANDOVER.md` §10.8:

1. **step = 1 + 2×(model 轮数),functionResponse 不计轮**。证据:轨迹 0f52941f
   (114847/0010/0014/0016):`[user]`→1;`[user, model(fc), model(fr)]`→3;
   `[user, model(fc), model(fr), model(text), user]`→5。
2. **functionResponse 的 role=model**(非 Gemini 公开 API 惯例的 user);转换器已修正,
   `_strip_trailing_model_turns` 已加 fr 豁免(工具续跑请求以 fr 结尾是合法形态)。
3. **labels 三形态**:九键(标准)/ 11 键(子代理,含 `parent_cascade_id`+`subagent_type`,
   0015)/ **10 键(含 `last_execution_id`,0016,出现在上一请求完成工具执行后的下一请求,
   插入位置 cascade_id 之后,字母序)**。
4. **重试风暴样本不作数**:101457(297 条)/101904(193 条)是工具损坏导致的重试异常
   (owner 明确指正)。任何规律只以"工具正常的最新会话"为准。
5. MINIMAL 不属于用户可见档位(agent_monitoring+flash-lite 特有);xhigh 档位存在但
   上游报文字符串未证实,不注入。
6. owner 决策(HANDOVER §8):宁可报错不静默降级;不伪造内容;Go 转发器;不恢复 httpx 回退。
7. 黄金样本 = `captures/20261121-114847/0014_streamGenerateContent.request.bin`
   (九键+工具轮);测试三环结构在 `tests/test_go_envelope_bytes.py`
   (格式环:实抓原始字节≡紧凑重建 / 实抓环:静态内容经 Go ≡ 实抓归一化逐字节 /
   字节等价环:Go ≡ Python)。

## 3. 最高优先待证:last_execution_id 来源

- **现象**:114847/0016 的 labels 带 `last_execution_id = ac34ec93-fd0f-4066-ab11-9b55d028e9c7`;
  该值**未出现在会话任何其它捕获文件中**;0014 的响应仅 354 字节 —— 因为 114847 会话
  是 `--offline-script subagent` 模式抓的,**响应是离线合成的**(响应文本含
  "[antigravity-capture] 离线模式"标记),execution id 不可能出现在合成响应里。
- **推断**:execution id 由真实上游响应返回(或来自客户端内部执行状态),只有
  转发模式(真实响应全量落盘)才能闭环。
- **实现预案**(闭环后执行):
  1. 在新会话的响应文件(`.response.full.txt` / `.response.bin`)里搜 0016 同款
     last_execution_id 值 → 定位字段名与 SSE 事件层;
  2. 从 streamGenerateContent 响应流捕获 execution id,按轨迹存储
     (参照 `thought_signature_cache` 或 `_TRAJECTORY_REQUEST_SEQ` 的轨迹级
     进程内模式);
  3. `_build_labels` 增加该键(有上一执行时插入,位置 cascade_id 之后);
  4. 黄金样本更新为带 10 键 labels 的最新实抓请求;同步更新
     `test_go_envelope_bytes.py` 三环 + `test_wrap_cli_request.py` 断言。
- 若真实响应里也找不到该值:结论改为"客户端本地生成",记录证据后关闭(不凭空实现)。

## 4. 抓取操作(指导 owner 执行;详见 tools/antigravity_capture/README.md)

**模式选择**(已与 owner 讲清,再确认一次):
- **转发模式**(本次用):真实模型响应,能真实开发;请求+响应全量落盘;
  抓包行为对谷歌**不可检测**(capture_server 只连回环 Go 转发器,真实 TLS 由 Go
  以真实客户端同栈完成);内容必然进谷歌——这是产品本质,不是抓包引入的暴露。
- 离线模式(`--offline` / `--offline-all`):内容不进谷歌但模型是假的,无法真实开发;
  仅适合纯行为研究,本次不用。

**步骤**:
1. `run_capture.ps1` / `Start-Sniff.ps1`(默认端口 18080;加 `-CaptureAll` 存全部端点
   响应,便于抓 execution id);脚本自动拉起 Go 转发器。
2. 设环境变量后启动 Antigravity(脚本自动设 `CLOUD_CODE_URL=http://127.0.0.1:18080`;
   token 刷新不走它,需 `HTTPS_PROXY=http://127.0.0.1:7897` 走 Clash,Go 自动绕过回环)。
   日志确认出现 `Overriding CloudCodeServerURL via CLOUD_CODE_URL`。
3. **必须覆盖的场景**(都是待证清单):
   - ≥4 轮普通对话(看 step 随轮次的连续序列);
   - ≥1 次真实工具调用(read_file/run_command)后**继续对话** → 下一请求应带
     last_execution_id;
   - ≥1 次子代理调用(验证 11 键形态与 parent_cascade_id/subagent_type 值来源);
   - 若可行:一次"以工具结果结尾续跑"的请求(请求以 fr 结尾)。
4. 抓完:**重启 Antigravity** 清掉 CLOUD_CODE_URL/HTTPS_PROXY;captures 目录含提示词,
   解析并更新基准后删除;转发模式勿长期挂着当日常使用。

## 5. 抓完后的分析步骤(建议顺序)

1. 枚举新会话全部 `.request.bin`:labels 键集分布(是否出现 10/11 键)、step 序列、
   fr role、requestType 分布;确认与 §2 已定结论一致,不一致以新实抓为准并回报 owner。
2. 在该会话响应文件里搜 last_execution_id 值 → 定位来源字段(§3 预案)。
3. 实现 + 测试(§3);黄金样本换成带 last_execution_id 的最新请求,
   三环测试全部跟着换。
4. 子代理 11 键:parent_cascade_id/subagent_type 的值来源同法排查;
   若涉及子代理转发逻辑(本代理目前不主动发子代理请求),仅记录不实现。
5. 全量 `pytest -q` + `verify --envelope --json` → 本地提交(不推送)。

## 6. 已踩坑(勿重蹈)

- `.ps1` 含中文注释必须 UTF-8 **BOM**(PS 5.1 无 BOM 按 GBK 解析会报语法错误;
  build.ps1/build-all.ps1/install.ps1 已加,新脚本注意)。
- 离线合成响应必须带 `{"response": {...}}` 包装,否则新版客户端解析失败疯狂重试。
- 严格模式 maxOutputTokens 校验豁免代理默认值 65536/64000,否则误杀默认请求
  (gemini-2.5-flash 实抓上限 65535)。
- 逐字节 diff 需要确定性参照:实抓的 uuid/时间戳是动态值,对照时按既有测试的
  归一化方式(requestId/sessionId/labels uuid 值),不要直接比原始字节。
- captures 含提示词:已 gitignore,处理完删;别提交进仓库。
