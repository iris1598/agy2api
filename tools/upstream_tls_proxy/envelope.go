// 信封构造:上游报文字节的唯一作者。
//
// Python 只传"内层有序值片 + 会话锚点 + 模型",所有结构字节——内层帧、
// labels(九键或十键)、requestId 五段、外层六键信封——由本文件产出。内层值片是
// Python 用与 httpx 相同参数(ensure_ascii=False, separators=(",",":"),
// allow_nan=False)json.dumps 出的原始字节,本文件只做纯拼接,绝不重新
// 序列化,保证最终报文与 Python 直连路径的字节逐字节一致。
//
// 确定性要求:同一输入必须产出同一字节(forward() 在连接重试时会用
// 已缓冲的 body 重发,不能出现第二个 requestId)。时间戳在构造时取一次,
// 之后随 body 复用——与旧行为"wrap 时生成一次"语义一致。
//
// 键序对齐实抓(2.34.0):
//   内层: contents → systemInstruction → tools → toolConfig → labels →
//         generationConfig → sessionId → 其余(顺序由 Python 的有序值片给出)
//   信封: project → requestId → request → model → userAgent → requestType
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// labels 占位键:Python 在值片序列里放 ["labels", null],本文件在原位
// 替换为自己构造的 labels 对象。labels 由代理生成,客户端不可能携带,
// 该键不会与合法值片冲突。
const envelopeLabelsPlaceholder = "labels"

// 实抓键序对照(2.34.0,163547 真实转发会话):
//   labels: cascade_id → [last_execution_id] → last_step_index → model_enum →
//           request_id → root_cascade_id → trajectory_id → used_claude →
//           used_claude_conservative → used_non_gemini_model(字母序;
//           last_execution_id 仅在存在工具执行轮时出现)

// 上游信封协议版本(/__health 的 "protocol" 字段;Python 侧强校验,
// 不匹配则拒绝启动/请求,防止旧二进制静默产出错误报文)。
// v3: anchors 新增 lastExecutionId(labels 可选第十键 last_execution_id,
// 实抓 163547/0074-0085;v2 二进制会静默丢键,必须拒绝)。
const envelopeProtocolVersion = 3

type envelopeInput struct {
	Model        string               `json:"model"`
	RequestType  string               `json:"requestType"`
	Project      string               `json:"project"`
	Anchors      envelopeAnchors      `json:"anchors"`
	InnerEntries []envelopeInnerEntry `json:"innerEntries"`
}

// envelopeAnchors 会话锚点与 labels 值(全部由 Python 内容派生/维护,见
// _derive_upstream_session_ids 与 _build_labels;这里只消费不推导,避免
// used_claude 等判定逻辑在两侧重复实现导致漂移)。字节由本文件产出。
type envelopeAnchors struct {
	CascadeID              string `json:"cascadeId"`
	RootCascadeID          string `json:"rootCascadeId"`
	TrajectoryID           string `json:"trajectoryId"`
	Step                   int    `json:"step"`
	LastStepIndex          string `json:"lastStepIndex"`
	ModelEnum              string `json:"modelEnum"`
	RequestIDLabel         string `json:"requestIdLabel"` // labels.request_id 值(trajectory-seq)
	UsedClaude             string `json:"usedClaude"`
	UsedClaudeConservative string `json:"usedClaudeConservative"`
	UsedNonGeminiModel     string `json:"usedNonGeminiModel"`
	LastExecutionID        string `json:"lastExecutionId"` // labels.last_execution_id 值,空串=不带该键
}

type envelopeInnerEntry struct {
	Key   string          `json:"key"`
	Value json.RawMessage `json:"value"`
}

// UnmarshalJSON 解码内层值片。协议形态: ["key", "<值片 JSON 文本>"]——
// 值片在 wrapper 里是双重编码的 JSON 字符串(Python json.dumps 的产物),
// 解码出文本后紧凑化并校验合法性,再作为原始字节嵌入内层帧。
// "labels" 的占位值是 null → Value=nil,由 buildEnvelope 原位替换。
func (e *envelopeInnerEntry) UnmarshalJSON(data []byte) error {
	var pair []json.RawMessage
	if err := json.Unmarshal(data, &pair); err != nil {
		return fmt.Errorf("inner entry must be [key, valueJsonText]: %w", err)
	}
	if len(pair) != 2 {
		return errors.New("inner entry must be [key, valueJsonText]")
	}
	if err := json.Unmarshal(pair[0], &e.Key); err != nil {
		return fmt.Errorf("inner entry key: %w", err)
	}
	if bytes.Equal(bytes.TrimSpace(pair[1]), []byte("null")) {
		e.Value = nil
		return nil
	}
	var piece string
	if err := json.Unmarshal(pair[1], &piece); err != nil {
		return fmt.Errorf("inner entry %q value must be a JSON text string: %w", e.Key, err)
	}
	var compacted bytes.Buffer
	if err := json.Compact(&compacted, []byte(piece)); err != nil {
		return fmt.Errorf("inner entry %q value is not valid JSON: %w", e.Key, err)
	}
	e.Value = json.RawMessage(compacted.Bytes())
	return nil
}

// envelopeLabels 键序 = 实抓键序(字母序;last_execution_id 仅在有值时
// 出现,位于 cascade_id 之后,实抓 163547/0074-0085 十键形态)。
type envelopeLabels struct {
	CascadeID              string `json:"cascade_id"`
	LastExecutionID        string `json:"last_execution_id,omitempty"`
	LastStepIndex          string `json:"last_step_index"`
	ModelEnum              string `json:"model_enum"`
	RequestID              string `json:"request_id"`
	RootCascadeID          string `json:"root_cascade_id"`
	TrajectoryID           string `json:"trajectory_id"`
	UsedClaude             string `json:"used_claude"`
	UsedClaudeConservative string `json:"used_claude_conservative"`
	UsedNonGeminiModel     string `json:"used_non_gemini_model"`
}

// envelopeFrame 外层六键,字段声明序 = 实抓键序。
type envelopeFrame struct {
	Project     string          `json:"project"`
	RequestID   string          `json:"requestId"`
	Request     json.RawMessage `json:"request"`
	Model       string          `json:"model"`
	UserAgent   string          `json:"userAgent"`
	RequestType string          `json:"requestType"`
}

// marshalCompactJSON 紧凑序列化并关闭 HTML 转义(Go 默认会把 < > & 转成
// \u003c 等,而 Python json.dumps 不会;内层虽然原样拼接,但 labels/
// requestId/信封字段由本文件产出,必须与 Python 参照逐字节一致)。
// json.Encoder.Encode 末尾带换行,这里去掉。
func marshalCompactJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// buildEnvelope 从缓冲的 wrapper 请求体构造最终上游报文字节。
func buildEnvelope(raw []byte) ([]byte, error) {
	var input envelopeInput
	if err := json.Unmarshal(raw, &input); err != nil {
		return nil, fmt.Errorf("envelope input decode: %w", err)
	}
	if input.Model == "" || input.RequestType == "" {
		return nil, errors.New("envelope input missing model/requestType")
	}
	if len(input.InnerEntries) == 0 {
		return nil, errors.New("envelope input has no inner entries")
	}

	// 1. labels 键序对齐实抓(值来自锚点,均为 ASCII:uuid/枚举/"true"/"false"/数字串)。
	labels := envelopeLabels{
		CascadeID:              input.Anchors.CascadeID,
		LastExecutionID:        input.Anchors.LastExecutionID,
		LastStepIndex:          input.Anchors.LastStepIndex,
		ModelEnum:              input.Anchors.ModelEnum,
		RequestID:              input.Anchors.RequestIDLabel,
		RootCascadeID:          input.Anchors.RootCascadeID,
		TrajectoryID:           input.Anchors.TrajectoryID,
		UsedClaude:             input.Anchors.UsedClaude,
		UsedClaudeConservative: input.Anchors.UsedClaudeConservative,
		UsedNonGeminiModel:     input.Anchors.UsedNonGeminiModel,
	}
	labelsBytes, err := marshalCompactJSON(labels)
	if err != nil {
		return nil, fmt.Errorf("labels marshal: %w", err)
	}

	// 2. 内层帧:纯拼接。值片(json.RawMessage)在 json.Unmarshal 时已被
	// encoding/json 校验过是合法 JSON;labels 占位原位替换。
	var inner bytes.Buffer
	inner.WriteByte('{')
	labelsPlaced := false
	for i, entry := range input.InnerEntries {
		if entry.Key == envelopeLabelsPlaceholder {
			if labelsPlaced {
				return nil, errors.New("envelope input duplicated labels placeholder")
			}
			labelsPlaced = true
		} else if len(entry.Value) == 0 {
			return nil, fmt.Errorf("envelope entry %q has empty value", entry.Key)
		}
		if i > 0 {
			inner.WriteByte(',')
		}
		keyJSON, err := marshalCompactJSON(entry.Key)
		if err != nil {
			return nil, fmt.Errorf("entry key marshal: %w", err)
		}
		inner.Write(keyJSON)
		inner.WriteByte(':')
		if entry.Key == envelopeLabelsPlaceholder {
			inner.Write(labelsBytes)
		} else {
			inner.Write(entry.Value)
		}
	}
	inner.WriteByte('}')
	if !labelsPlaced {
		return nil, errors.New("envelope input missing labels placeholder")
	}

	// 3. requestId 五段 agent/{cascadeId}/{unixMs}/{trajectoryId}/{step}。
	// 时间戳在此取一次;body 构造一次后随重试复用,不会出现第二个 requestId。
	requestID := fmt.Sprintf(
		"agent/%s/%d/%s/%d",
		input.Anchors.CascadeID,
		time.Now().UnixMilli(),
		input.Anchors.TrajectoryID,
		input.Anchors.Step,
	)

	// 4. 外层信封六键(struct 声明序)。
	frame := envelopeFrame{
		Project:     input.Project,
		RequestID:   requestID,
		Request:     json.RawMessage(inner.Bytes()),
		Model:       input.Model,
		UserAgent:   "antigravity",
		RequestType: input.RequestType,
	}
	return marshalCompactJSON(frame)
}
