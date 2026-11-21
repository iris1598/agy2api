# 交接:真实开发会话抓取 → 实抓基准闭环(已完成,2026-11-21 第四轮)

> **本任务已闭环**,全程记录在 `docs/HANDOVER.md` §10.9。此处保留结论摘要,
> 供后续接手者快速了解"抓取 → 闭环"的全貌与剩余缺口。

---

## 1. 结果总览

- owner 以转发模式完成一次真实开发会话抓取:**`captures/20261121-163547`**
  (真实开发 nonebot 项目;57 条 streamGenerateContent,请求+响应全量落盘,
  含全部端点)。
- owner 指正:**该会话是唯一实抓基准**,此前所有离线模拟会话(114847 等)
  不再作为地面真值。
- 最高优先待证 `labels.last_execution_id` 的来源已闭环:客户端本地生成,
  详见 §3。
- 实现与测试全部落地:pytest **225 passed**;`sniff_cli verify --envelope
  --json` 全绿;Go 转发器协议 **2→3** 并已重编译。

## 2. 抓取结论(以 163547 为准,已实现)

1. **step 公式**:`step = user 轮数 + 非(纯 functionResponse)的 model 轮数
   + functionCall part 数`(fr 不计轮,并行调用按 part 数计);**轨迹内叠加
   重发偏移**——contents 未推进的重复请求 = 客户端重发,step +1(0013 请求
   502 失败后 0014 原样重发,此后 31 条请求全部带 +1)。step 不是纯
   contents 函数。
2. **last_execution_id(十键形态)**:客户端本地 uuid4,不出现在任何上游
   响应中;存在"最后一个 user 轮之前的 fr 轮"时携带;同一工具轮在同一
   user 周期内保持同值,新 user 轮开启后更替(0074-0078=X,0080-0085=Z)。
   代理以确定性派生值替代(sha256(trajectory|fr 轮内容) → v4 形态)。
3. **并行工具调用布局**:一轮 N 个调用 = 单 model content N 个 fc part +
   单 model content N 个 fr part;fc/fr/text 是相邻独立 content。
   `openai2gemini` 的合并已改为只合并相邻 user 轮。
4. **子代理 11 键形态:关闭为"不适用"**(owner 2026-11-21 拍板)。实抓证实
   子代理没有独立端点(163547 全部 57 条请求同打 streamGenerateContent),
   11 键只是客户端在自有子代理轨迹上多带 parent_cascade_id/subagent_type
   两个元数据键。反代路径无"这是子代理"的信号字段,无从标注;真实用户
   不开子代理时同样只发 9/10 键,"全顶层 cascade"是合法客户端画像,不模拟。
   键序已存档于 fidelity_schema(11 键 orders),值来源不再追查。残留极小
   不一致:caller 刻意透传 invoke_subagent 工具调用而代理不声明子代理轨迹
   ——常规接入不出现,接受。
5. agent_monitoring / checkpoint 请求为空 labels + uuid 尾 requestId;
   代理不主动发这两类,无需实现。

## 3. last_execution_id 证据链(原待证项的闭环记录)

- 两个 lei 值(5fd09728…/8d00538a…)只出现在请求里,**任何响应文件都不含**
  → 上游不回显,只能是客户端本地状态;
- 两个值均为 uuid v4 形态;
- 出现/更替时点与"新 user 轮开启"严格对应,周期内工具续跑保持同值;
- 结论:客户端本地生成、按 user 周期刷新。代理用确定性派生模拟其稳定性
  模式(不伪造内容——值本身无上游校验)。

## 4. 剩余事项

- captures/20261121-163547 含提示词:**处理完删除**(已 gitignore,勿提交)。
- 两个抓包遗留转发器进程已清理;Go 二进制已重建(协议 3)。
- 既有差距(未修,后续候选):真实客户端 systemInstruction 会随功能激活演化,
  按其派生 trajectory_id 会断裂上游缓存亲和(见 HANDOVER §10.9 第 6 点)。
- 全部运行/测试仍用 `.venv\Scripts\python.exe`;导入规范见 `AGENTS.md`。
