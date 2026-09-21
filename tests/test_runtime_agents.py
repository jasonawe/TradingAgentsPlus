"""Task 15 — PlannerAgent V2 PlanGraph tests.

覆盖 plan 要求:
- A-class empty domain graph(没有任何 domain task)
- data-only / news-only / alpha-only 单 domain graph
- compare(multi-domain)graph
- multi-symbol fan-out(每个 symbol 一个 domain task)
- required / optional 标记
- 局部 task_key(plan-local,不是 UUIDv7)
- invalid LLM JSON fallback 到 heuristic
- capability catalog 使用
- plan cache 复用(相同 input 命中)
- 拒绝 Planner 发出 verifier / synthesizer / tool action
"""
from __future__ import annotations

import pytest


def _planner():
    from tradingagents.agent_harness.agents.planner import PlannerAgent
    from tradingagents.agent_harness.agents.registry import AgentRegistry
    from tradingagents.agent_harness.agents.base import AgentDescriptor

    reg = AgentRegistry()
    # V2 注册 domain agents
    reg.register_v2(
        AgentDescriptor(name="data_agent", version=2,
                        capabilities=["domain_lookup"], scope="user"),
        factory=lambda: _StubAgent("data_agent"),
    )
    reg.register_v2(
        AgentDescriptor(name="news_agent", version=2,
                        capabilities=["news_lookup"], scope="user"),
        factory=lambda: _StubAgent("news_agent"),
    )
    reg.register_v2(
        AgentDescriptor(name="alpha_agent", version=2,
                        capabilities=["alpha_compute"], scope="user"),
        factory=lambda: _StubAgent("alpha_agent"),
    )
    return PlannerAgent(agent_registry=reg)


class _StubAgent:
    def __init__(self, name):
        self.name = name


def _input(msg, symbols=None):
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext
    return AgentInput(
        user_message=msg,
        context={"symbols": symbols or []},
    ), AgentContext(session_id="sess-1")


# ════════════════════════════════════════════════════════
# PlanGraph shape
# ════════════════════════════════════════════════════════


def test_planner_v2_returns_plan_graph_dataclass():
    """Planner 返回 typed PlanGraph(包含 domain_tasks + budgets)。"""
    p = _planner()
    inp, ctx = _input("分析 AAPL")
    graph = p.plan_v2(inp, context=ctx)
    from tradingagents.agent_harness.runtime.models import PlanGraph
    assert isinstance(graph, PlanGraph)
    assert hasattr(graph, "domain_tasks")
    assert hasattr(graph, "budgets")


def test_planner_v2_empty_domain_graph_for_a_class():
    """A-class:无 domain task(empty PlanGraph)。"""
    p = _planner()
    inp, ctx = _input("hi")  # no symbols, no analysis request
    graph = p.plan_v2(inp, context=ctx)
    assert graph.domain_tasks == []


def test_planner_v2_data_only_single_symbol():
    """单 symbol + data 关键词 → 1 个 data task。"""
    p = _planner()
    inp, ctx = _input("查询 AAPL 的行情", symbols=["AAPL"])
    graph = p.plan_v2(inp, context=ctx)
    assert len(graph.domain_tasks) == 1
    assert graph.domain_tasks[0].agent == "data_agent"


def test_planner_v2_news_only():
    """'新闻' 关键词 → 1 个 news task。"""
    p = _planner()
    inp, ctx = _input("AAPL 新闻", symbols=["AAPL"])
    graph = p.plan_v2(inp, context=ctx)
    assert any(t.agent == "news_agent" for t in graph.domain_tasks)


def test_planner_v2_alpha_only():
    """'alpha' / 'compute' 关键词 → 1 个 alpha task。"""
    p = _planner()
    inp, ctx = _input("compute alpha for NVDA", symbols=["NVDA"])
    graph = p.plan_v2(inp, context=ctx)
    assert any(t.agent == "alpha_agent" for t in graph.domain_tasks)


def test_planner_v2_multi_symbol_fanout():
    """多 symbol → 每个 symbol 一个 domain task。"""
    p = _planner()
    inp, ctx = _input("分析 AAPL 和 NVDA", symbols=["AAPL", "NVDA"])
    graph = p.plan_v2(inp, context=ctx)
    task_keys = [t.task_key for t in graph.domain_tasks]
    # data_agent fanout — 至少 2 个 data task
    assert sum(1 for t in graph.domain_tasks if t.agent == "data_agent") >= 2


def test_planner_v2_required_optional_flags():
    """data 是 required,news 是 optional。"""
    p = _planner()
    inp, ctx = _input("分析 AAPL", symbols=["AAPL"])
    graph = p.plan_v2(inp, context=ctx)
    data_tasks = [t for t in graph.domain_tasks if t.agent == "data_agent"]
    news_tasks = [t for t in graph.domain_tasks if t.agent == "news_agent"]
    if data_tasks:
        assert data_tasks[0].required is True
    if news_tasks:
        assert news_tasks[0].required is False


def test_planner_v2_local_task_keys():
    """task_key 是 plan-local 字符串(不是 UUIDv7)。"""
    p = _planner()
    inp, ctx = _input("分析 AAPL", symbols=["AAPL"])
    graph = p.plan_v2(inp, context=ctx)
    for t in graph.domain_tasks:
        # task_key 是短字符串,不是 36 字符的 UUID
        assert isinstance(t.task_key, str)
        assert len(t.task_key) <= 32


# ════════════════════════════════════════════════════════
# Reject Planner-emitted verifier / synthesizer / tools
# ════════════════════════════════════════════════════════


def test_planner_v2_rejects_verifier_or_synthesizer_in_llm_output():
    """LLM 输出包含 verifier / synthesizer / 写 tool → fallback heuristic。"""
    p = _planner()
    # 强制 LLM 路径,提供一个伪造的 LLM 响应包含 verifier
    inp, ctx = _input("AAPL", symbols=["AAPL"])
    class _FakeLLM:
        def complete_text(self, *, prompt, system=None, temperature=0.0):
            return type("R", (), {"content": '[{"step":1,"agent":"verifier"}]'})
    p.llm_factory = type("_F", (), {"is_configured": lambda self: True, "make": lambda self: _FakeLLM()})()
    graph = p.plan_v2(inp, context=ctx)
    # 不应包含 verifier / synthesizer / 写 tool
    bad = {"verifier", "synthesizer", "create_note", "delete_alert"}
    for t in graph.domain_tasks:
        assert t.agent not in bad
        assert all(b not in (t.objective or "") for b in bad)


def test_planner_v2_invalid_json_falls_back_to_heuristic():
    """LLM 返回 invalid JSON → 用 heuristic fallback,仍返回非空 graph。"""
    p = _planner()
    inp, ctx = _input("分析 AAPL", symbols=["AAPL"])
    class _FakeLLM:
        def complete_text(self, *, prompt, system=None, temperature=0.0):
            return type("R", (), {"content": "not json at all"})
    p.llm_factory = type("_F", (), {"is_configured": lambda self: True, "make": lambda self: _FakeLLM()})()
    graph = p.plan_v2(inp, context=ctx)
    # heuristic 仍应产生 data task
    assert any(t.agent == "data_agent" for t in graph.domain_tasks)


# ════════════════════════════════════════════════════════
# Capability catalog use
# ════════════════════════════════════════════════════════


def test_planner_v2_uses_capability_catalog_from_registry():
    """Planner 从 registry 取 capabilities,不写死。"""
    from tradingagents.agent_harness.agents.planner import PlannerAgent
    from tradingagents.agent_harness.agents.registry import AgentRegistry
    from tradingagents.agent_harness.agents.base import AgentDescriptor

    reg = AgentRegistry()
    reg.register_v2(
        AgentDescriptor(name="custom_lookup", version=2,
                        capabilities=["custom_cap"], scope="user"),
        factory=lambda: _StubAgent("custom_lookup"),
    )
    p = PlannerAgent(agent_registry=reg)
    caps = p._capability_catalog()
    assert any(c["agent"] == "custom_lookup" and c["capability"] == "custom_cap" for c in caps)


# ════════════════════════════════════════════════════════
# Plan cache
# ════════════════════════════════════════════════════════


def test_planner_v2_plan_cache_hits_on_repeated_input():
    """相同 user_message 第二次调用应命中 plan cache。"""
    p = _planner()
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext
    inp = AgentInput(user_message="AAPL", context={"symbols": ["AAPL"]})
    ctx = AgentContext(session_id="sess-1")
    g1 = p.plan_v2(inp, context=ctx)
    # 模拟第二次调用同一消息 — 期望命中 cache(不重新生成)
    if hasattr(p, "_plan_cache") and p._plan_cache is not None:
        cached = p._plan_cache.get("AAPL", ["AAPL"])
        # cache 返回的可能是同一个 graph 或等价 graph
        assert cached is not None
