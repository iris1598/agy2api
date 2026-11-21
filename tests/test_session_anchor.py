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
    """同一会话追加历史后，从首条请求起 key 恒定（§10.14 粘性绑定）。

    粘性键基底取首条消息（追加历史/模型回复不影响）；绑定的摘要值在
    首见时按纯函数全量锚点（model + sys + 前两条消息）定格。
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


# ==================== 粘性锚点(§10.14:sys 会话内演化不断键) ====================


@pytest.fixture(autouse=True)
def _isolated_sticky_memo():
    from src.session_cache import _reset_sticky_anchor_memo

    _reset_sticky_anchor_memo()
    yield
    _reset_sticky_anchor_memo()


def test_sticky_key_survives_system_evolution():
    """systemInstruction 会话内演化(实验旗标/功能激活)→ key 不变(§10.9.6 修复)。

    纯函数口径下 0013↔0080 的 sys 段落差异会换 trajectory/凭证亲和;
    粘性绑定后首见即定格,演化不再断键。
    """
    sys_a = {"parts": [{"text": "You are Antigravity... <terminal_sandbox> 段"}]}
    sys_b = {"parts": [{"text": "You are Antigravity... <artifacts> 段"}]}
    msgs = [
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的"}]},
        {"role": "user", "parts": [{"text": "继续"}]},
    ]
    k_birth = derive_session_key("gemini-4-carbon-medium", _req(msgs))
    evolved = _req(msgs)
    evolved["systemInstruction"] = sys_b
    k_after = derive_session_key("gemini-4-carbon-medium", evolved)
    assert k_birth == k_after, "sys 演化不得更换会话 key"


def test_sticky_key_differs_without_sticky_hit():
    """不同首条消息仍是不同 key(粘性只影响同基底会话)。"""
    ka = derive_session_key(
        "gemini-4-carbon-medium",
        _req([{"role": "user", "parts": [{"text": "分析数据库慢查询"}]}]),
    )
    kb = derive_session_key(
        "gemini-4-carbon-medium",
        _req([{"role": "user", "parts": [{"text": "给前端写个组件"}]}]),
    )
    assert ka != kb


def test_pure_anchor_remains_sys_sensitive():
    """纯函数 `derive_conversation_anchor` 保持 sys 敏感(两层设计:粘性在消费方)。"""
    msgs = [{"role": "user", "parts": [{"text": "继续"}]}]
    r1 = _req(msgs)
    r2 = _req(msgs)
    r2["systemInstruction"] = {"parts": [{"text": "另一个 sys"}]}
    d1 = derive_conversation_anchor("m", r1)
    d2 = derive_conversation_anchor("m", r2)
    assert d1 != d2
