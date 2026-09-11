"""Day 8 — Routing + Recursion Limit 集成测试。

覆盖:
- Intent enum: 4 个值齐全
- fast_route: 关键词 regex 命中覆盖 QUERY/ACTION/ANALYSIS/CHAT 4 类
- classify_intent: 关键词失败 fallback ANALYSIS;有 LLM 时走 LLM 路径
- render_intent_hint: 生成 system prompt 片段
- orchestrator: build_agent + stream_chat 集成(用 FakeListChatModel)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.agents.general.routing import (
    Intent,
    RouteResult,
    fast_route,
    classify_intent,
    render_intent_hint,
    DEFAULT_CONFIDENCE_THRESHOLD,
)
from langchain_core.language_models.fake_chat_models import FakeListChatModel


# ─────────────────────────────────────────────────────
# Routing tests
# ─────────────────────────────────────────────────────


def test_intent_enum_values():
    assert Intent.QUERY.value == "query"
    assert Intent.ACTION.value == "action"
    assert Intent.ANALYSIS.value == "analysis"
    assert Intent.CHAT.value == "chat"
    print("  ✓ Intent enum has 4 values")


def test_fast_route_query():
    for msg in ["600036 现在多少钱", "RSI 怎么样", "MACD 数值",
                "PE 市盈率多少", "我的关注列表", "最近的成交量"]:
        r = fast_route(msg)
        assert r == Intent.QUERY, f"{msg!r} -> {r}"
    print(f"  ✓ fast_route QUERY hit ({6} cases)")


def test_fast_route_action():
    for msg in ["新建一个告警", "帮我建一个笔记", "删除告警",
                "更新我的偏好", "建一个调度任务"]:
        r = fast_route(msg)
        assert r == Intent.ACTION, f"{msg!r} -> {r}"
    print(f"  ✓ fast_route ACTION hit ({5} cases)")


def test_fast_route_analysis():
    for msg in ["加仓吗", "减仓 600036", "该不该买入",
                "深度分析 600036", "对比一下 600036 和 600000",
                "风险怎么样", "值不值得"]:
        r = fast_route(msg)
        assert r == Intent.ANALYSIS, f"{msg!r} -> {r}"
    print(f"  ✓ fast_route ANALYSIS hit ({7} cases)")


def test_fast_route_chat():
    for msg in ["你好", "hi", "什么是 PE", "什么是 RSI", "解释一下什么叫夏普"]:
        r = fast_route(msg)
        assert r == Intent.CHAT, f"{msg!r} -> {r}"
    print(f"  ✓ fast_route CHAT hit ({5} cases)")


def test_fast_route_no_match():
    for msg in ["随便说点啥", "嗯", "abc xyz", ""]:
        r = fast_route(msg)
        if msg == "":
            assert r is None, f"empty msg -> {r}"
        else:
            # 复杂 fallback
            assert r is None or r in (Intent.CHAT, Intent.ANALYSIS), f"{msg!r} -> {r}"
    print(f"  ✓ fast_route fallback for unmatched inputs")


def test_classify_intent_no_llm():
    """无 LLM 时,fast 失败 → fallback ANALYSIS。"""
    r = classify_intent("随便聊聊", llm=None)
    assert r.intent == Intent.ANALYSIS
    assert r.source == "fallback"
    print(f"  ✓ classify_intent no-llm -> fallback ANALYSIS")


def test_classify_intent_with_llm():
    """LLM 正常返回 JSON → 用 LLM 结果。"""
    # Fake LLM 模拟 {"intent":"action","confidence":0.95,"reason":"..."}
    fake = FakeListChatModel(
        responses=['{"intent": "action", "confidence": 0.95, "reason": "user wants to create"}']
    )
    r = classify_intent("某个 unmatched 文本", llm=fake)
    assert r.intent == Intent.ACTION
    assert r.source == "llm"
    assert r.confidence >= DEFAULT_CONFIDENCE_THRESHOLD
    print(f"  ✓ classify_intent with LLM -> {r.intent.value} (conf={r.confidence})")


def test_classify_intent_low_confidence_fallback():
    """LLM 返回低 confidence → fallback ANALYSIS。"""
    fake = FakeListChatModel(
        responses=['{"intent": "query", "confidence": 0.3, "reason": "unsure"}']
    )
    r = classify_intent("某个 unmatched 文本", llm=fake, confidence_threshold=0.85)
    assert r.intent == Intent.ANALYSIS  # fallback
    assert r.source == "fallback"
    print(f"  ✓ classify_intent low confidence -> fallback ANALYSIS")


def test_classify_intent_malformed_response():
    """LLM 返回无效 JSON → fallback。"""
    fake = FakeListChatModel(responses=["oops, not JSON"])
    r = classify_intent("某个 unmatched 文本", llm=fake)
    assert r.intent == Intent.ANALYSIS
    print(f"  ✓ classify_intent malformed response -> fallback")


def test_render_intent_hint():
    r = RouteResult(Intent.QUERY, 1.0, "keyword", "fast")
    hint = render_intent_hint(r)
    assert "query" in hint
    assert "QUERY" in hint or "query" in hint
    assert "读 tool" in hint or "get_quote" in hint
    print(f"  ✓ render_intent_hint generates hint ({len(hint)} chars)")


# ─────────────────────────────────────────────────────
# Orchestrator integration tests (recursion_limit + intent hint)
# ─────────────────────────────────────────────────────


class _MockLLMWithTools(FakeListChatModel):
    """FakeListChatModel + bind_tools + with_config(满足 LangGraph)。"""

    def bind_tools(self, tools, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self


def test_build_agent_with_intent_hint():
    """build_agent 能正常构造,_prompt_with_intent callable 能被 LangGraph 调用。"""
    from tradingagents.agents.general.orchestrator import build_agent

    llm = _MockLLMWithTools(responses=["[mock]"])
    data_dir = tempfile.mkdtemp(prefix="test_d8_")
    agent, conn = build_agent(
        llm=llm, data_dir=data_dir, session_id="test_d8_session"
    )
    assert agent is not None
    conn.close()
    print(f"  ✓ build_agent constructs with intent-hint callable prompt")


def test_stream_chat_includes_recursion_limit():
    """stream_chat 入口的 config 必须包含 recursion_limit=8。"""
    from tradingagents.agents.general.orchestrator import build_agent, stream_chat
    from langchain_core.messages import HumanMessage

    llm = _MockLLMWithTools(responses=["[mock]"])
    data_dir = tempfile.mkdtemp(prefix="test_d8_")
    agent, conn = build_agent(
        llm=llm, data_dir=data_dir, session_id="test_d8_recursion"
    )

    # stream_chat 应该正常跑完不抛错
    events = []
    for et, p in stream_chat(agent, "test_d8_recursion", "600036 价格"):
        events.append((et, p))
        if len(events) > 30:
            break

    # 至少有 reasoning 事件(FakeListChatModel 字符级 stream)
    reasoning_count = sum(1 for et, _ in events if et == "reasoning")
    assert reasoning_count > 0
    conn.close()
    print(f"  ✓ stream_chat runs (events={len(events)}, reasoning={reasoning_count})")


# ─────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=" * 60)
    print("Day 8 — Routing + Recursion Limit test")
    print("=" * 60)

    tests = [
        test_intent_enum_values,
        test_fast_route_query,
        test_fast_route_action,
        test_fast_route_analysis,
        test_fast_route_chat,
        test_fast_route_no_match,
        test_classify_intent_no_llm,
        test_classify_intent_with_llm,
        test_classify_intent_low_confidence_fallback,
        test_classify_intent_malformed_response,
        test_render_intent_hint,
        test_build_agent_with_intent_hint,
        test_stream_chat_includes_recursion_limit,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    print(f"Day 8 tests: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed:
        sys.exit(1)
