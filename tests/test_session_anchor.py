"""会话锚点与会话 key 派生回归测试（issue #450 根因 2）。

覆盖:
- 同一会话跨轮次（历史追加）→ key 恒定
- 共用相同 systemInstruction 的不同会话 → key 隔离（不再短路撞车）
- 不同模型 → key 隔离
- 无 systemInstruction 时回落消息特征
- 联合锚点摘要确定性（wrap_cli_request 与 derive_session_key 同口径）
"""
import pytest

from src.session_cache import derive_conversation_anchor, derive_session_key

SYSTEM_PARTS = [{"text": "You are a helpful coding assistant."}]


def _req(contents, system=True, model="gemini-4-carbon-medium"):
    req = {"contents": contents}
    if system:
        req["systemInstruction"] = {"parts": SYSTEM_PARTS}
    return req


def test_same_session_stable_across_turns():
    """同一会话追加历史后，从第二条消息出现起 key 必须恒定。

    锚点取前两条消息：第 1 轮只有一条消息，第 2 轮起前两条固定为
    [首条 user, 首条 model 回复]（append-only 历史），此后所有轮次恒定。
    """
    turn2 = _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的，我先看一下……"}]},
        {"role": "user", "parts": [{"text": "先从 session_cache 开始"}]},
    ])
    turn3 = _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的，我先看一下……"}]},
        {"role": "user", "parts": [{"text": "先从 session_cache 开始"}]},
        {"role": "model", "parts": [{"text": "已完成。"}]},
        {"role": "user", "parts": [{"text": "再补几个测试"}]},
    ])
    turn4 = _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的，我先看一下……"}]},
        {"role": "user", "parts": [{"text": "先从 session_cache 开始"}]},
        {"role": "model", "parts": [{"text": "已完成。"}]},
        {"role": "user", "parts": [{"text": "再补几个测试"}]},
        {"role": "model", "parts": [{"text": "测试也补完了。"}]},
        {"role": "user", "parts": [{"text": "收尾"}]},
    ])
    k2 = derive_session_key("gemini-4-carbon-medium", turn2)
    k3 = derive_session_key("gemini-4-carbon-medium", turn3)
    k4 = derive_session_key("gemini-4-carbon-medium", turn4)
    assert k2 == k3 == k4
    assert k2 and k2.startswith("auto:")


def test_shared_system_instruction_sessions_isolated():
    """共用相同 systemInstruction 的两个不同会话，key 必须不同（短路撞车回归）。"""
    session_a = _req([
        {"role": "user", "parts": [{"text": "分析数据库慢查询"}]},
    ])
    session_b = _req([
        {"role": "user", "parts": [{"text": "给前端写个组件"}]},
    ])
    key_a = derive_session_key("gemini-4-carbon-medium", session_a)
    key_b = derive_session_key("gemini-4-carbon-medium", session_b)
    assert key_a and key_b
    assert key_a != key_b


def test_different_model_isolated():
    """不同模型 → key 不同。"""
    session = _req([
        {"role": "user", "parts": [{"text": "分析数据库慢查询"}]},
    ])
    key_gemini = derive_session_key("gemini-4-carbon-medium", session)
    key_claude = derive_session_key("claude-sonnet-5-5-medium", session)
    assert key_gemini != key_claude


def test_first_function_call_feature_distinguishes():
    """首条消息以 functionCall 开头时（Agent 子会话），其 name+args 参与锚点。"""
    session_a = _req([
        {
            "role": "user",
            "parts": [{"functionCall": {"name": "search_code", "args": {"q": "session"}}}],
        },
    ])
    session_b = _req([
        {
            "role": "user",
            "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}],
        },
    ])
    assert (
        derive_session_key("gemini-4-carbon-medium", session_a)
        != derive_session_key("gemini-4-carbon-medium", session_b)
    )


def test_args_key_order_irrelevant():
    """同 functionCall，args 键序不同 → 相同特征（稳定序列化）。"""
    session_a = _req([
        {"role": "user", "parts": [{"functionCall": {"name": "f", "args": {"a": 1, "b": 2}}}]},
    ])
    session_b = _req([
        {"role": "user", "parts": [{"functionCall": {"name": "f", "args": {"b": 2, "a": 1}}}]},
    ])
    assert (
        derive_session_key("gemini-4-carbon-medium", session_a)
        == derive_session_key("gemini-4-carbon-medium", session_b)
    )


def test_no_system_no_contents_returns_none():
    assert derive_session_key("m", {}) is None
    assert derive_conversation_anchor("m", {"contents": []}) is None


def test_no_system_instruction_falls_back_to_messages():
    """无 systemInstruction 时，消息特征仍然派生 key。"""
    session = _req(
        [{"role": "user", "parts": [{"text": "hello"}]}], system=False
    )
    key = derive_session_key("gemini-4-carbon-medium", session)
    assert key and key.startswith("auto:")


def test_anchor_digest_stability():
    """联合锚点摘要跨调用恒定，且为 32 字节。"""
    session = _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的"}]},
        {"role": "user", "parts": [{"text": "继续"}]},
    ])
    d1 = derive_conversation_anchor("gemini-4-carbon-medium", session)
    d2 = derive_conversation_anchor("gemini-4-carbon-medium", session)
    assert d1 == d2 and len(d1) == 32


def test_first_two_messages_only():
    """锚点仅取前两条消息：第三条消息变化不影响锚点（长对话追加历史不换 key）。"""
    head = [
        {"role": "user", "parts": [{"text": "任务 A 开始"}]},
        {"role": "model", "parts": [{"text": "收到"}]},
    ]
    short = _req(head)
    long = _req(head + [
        {"role": "user", "parts": [{"text": "第 3 条新消息"}]},
        {"role": "model", "parts": [{"text": "第 4 条新消息"}]},
    ])
    assert derive_session_key("m", short) == derive_session_key("m", long)
