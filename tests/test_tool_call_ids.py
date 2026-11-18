"""工具调用 ID 确定性回归测试（issue #450 根因 3）。

覆盖:
- Claude 路径：缺失 id 的 functionCall/functionResponse 按位置+函数名+载荷
  确定性补全，重复调用恒定
- functionCall 与 functionResponse 的配对（queue 机制）保持
- 已有 id 原样保留
- 非 Claude 模型原样透传
- 入参不被修改（copy-on-write）
"""
from src.converter.antigravity_fix import _ensure_tool_call_ids, _deterministic_tool_id

CLAUDE = "claude-sonnet-5-5-medium"
GEMINI = "gemini-4-carbon-medium"


def _contents_with_tools():
    return [
        {"role": "user", "parts": [{"text": "查一下天气"}]},
        {
            "role": "model",
            "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "北京"}}}],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "get_weather", "response": {"temp": 25}}},
            ],
        },
        {"role": "model", "parts": [{"text": "北京 25 度"}]},
    ]


def test_deterministic_id_stable_across_calls():
    c1 = _ensure_tool_call_ids(_contents_with_tools(), CLAUDE)
    c2 = _ensure_tool_call_ids(_contents_with_tools(), CLAUDE)
    assert c1 == c2, "同一段历史重复补全，ID 必须恒定"
    fc = c1[1]["parts"][0]["functionCall"]
    fr = c1[2]["parts"][0]["functionResponse"]
    assert fc["id"] == c2[1]["parts"][0]["functionCall"]["id"]
    assert fr["id"] == c2[2]["parts"][0]["functionResponse"]["id"]
    assert fc["id"].startswith("toolu_")
    assert fr["id"].startswith("toolu_")


def test_fc_fr_pairing_preserved():
    """functionResponse 缺 id 时复用前面 functionCall 的 id（配对）。"""
    c = _ensure_tool_call_ids(_contents_with_tools(), CLAUDE)
    fc_id = c[1]["parts"][0]["functionCall"]["id"]
    fr_id = c[2]["parts"][0]["functionResponse"]["id"]
    assert fc_id == fr_id, "配对的 fc/fr 必须使用相同 id"


def test_repeated_call_same_history_id_unchanged():
    """已补全的历史（含 id）再次经过时不改写既有 id。"""
    c1 = _ensure_tool_call_ids(_contents_with_tools(), CLAUDE)
    c2 = _ensure_tool_call_ids(c1, CLAUDE)
    assert c1 == c2


def test_existing_ids_preserved():
    contents = [
        {
            "role": "model",
            "parts": [{"functionCall": {"id": "toolu_custom", "name": "f", "args": {}}}],
        },
        {
            "role": "user",
            "parts": [{"functionResponse": {"id": "toolu_custom", "name": "f", "response": {}}}],
        },
    ]
    out = _ensure_tool_call_ids(contents, CLAUDE)
    assert out[0]["parts"][0]["functionCall"]["id"] == "toolu_custom"
    assert out[1]["parts"][0]["functionResponse"]["id"] == "toolu_custom"


def test_orphan_fr_deterministic_fallback():
    """无前置 functionCall 的孤立 functionResponse 也确定性补全。"""
    contents = [
        {"role": "user", "parts": [{"text": "hi"}]},
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "ghost_tool", "response": {"x": 1}}},
            ],
        },
    ]
    out1 = _ensure_tool_call_ids(contents, CLAUDE)
    out2 = _ensure_tool_call_ids(contents, CLAUDE)
    id1 = out1[1]["parts"][0]["functionResponse"]["id"]
    id2 = out2[1]["parts"][0]["functionResponse"]["id"]
    assert id1 == id2 and id1.startswith("toolu_")


def test_non_claude_passthrough():
    """非 Claude 模型原样返回（同一对象）。"""
    contents = _contents_with_tools()
    out = _ensure_tool_call_ids(contents, GEMINI)
    assert out is contents


def test_input_not_mutated():
    """不修改入参（copy-on-write）。"""
    original = _contents_with_tools()
    snapshot = [dict(c, parts=list(c["parts"])) for c in original]
    _ensure_tool_call_ids(original, CLAUDE)
    assert original == snapshot


def test_different_position_different_id():
    """相同函数名/参数在不同位置 → 不同 ID（位置参与派生）。"""
    contents = [
        {
            "role": "model",
            "parts": [
                {"functionCall": {"name": "f", "args": {"a": 1}}},
                {"functionCall": {"name": "f", "args": {"a": 1}}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "f", "response": {}}},
                {"functionResponse": {"name": "f", "response": {}}},
            ],
        },
    ]
    out = _ensure_tool_call_ids(contents, CLAUDE)
    fc_ids = [p["functionCall"]["id"] for p in out[0]["parts"]]
    fr_ids = [p["functionResponse"]["id"] for p in out[1]["parts"]]
    assert fc_ids[0] != fc_ids[1]
    # 两个 fr 依次从队列取，与两个 fc 配对
    assert fr_ids[0] == fc_ids[0]
    assert fr_ids[1] == fc_ids[1]


def test_deterministic_tool_id_format():
    tid = _deterministic_tool_id("0:0:get_weather:{\"city\": \"北京\"}")
    assert tid.startswith("toolu_") and len(tid) == len("toolu_") + 32
    assert tid == _deterministic_tool_id("0:0:get_weather:{\"city\": \"北京\"}")
