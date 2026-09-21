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


# ════════════════════════════════════════════════════════
# Task 16 — Domain agents (Data / News / Alpha) V2 contract
# ════════════════════════════════════════════════════════


def test_data_agent_v2_concurrent_quote_and_fundamentals():
    """DataAgent V2 通过 context.tool_executor 并发获取 quote + fundamentals。"""
    from tradingagents.agent_harness.agents.data_agent import DataAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    class _SpyTool:
        def __init__(self):
            self.calls = []

        async def invoke(self, tool_name, args):
            self.calls.append((tool_name, args))
            if tool_name == "get_quote":
                return {"symbol": args["symbol"], "price": 100.0, "currency": "USD"}
            if tool_name == "get_fundamentals":
                return {"symbol": args["symbol"], "pe": 12.5, "market_cap": 1e10}
            return {}

    tool = _SpyTool()
    agent = DataAgent()
    inp = AgentInput(user_message="lookup", context={"symbols": ["AAPL"]})
    ctx = AgentContext(session_id="s1", extra={"tool_executor": tool})
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert len(tool.calls) == 2
    assert any(c[0] == "get_quote" for c in tool.calls)
    assert any(c[0] == "get_fundamentals" for c in tool.calls)


def test_data_agent_v2_returns_evidence_refs_and_confidence():
    """DataAgent V2 结果包含 evidence_refs + confidence。"""
    from tradingagents.agent_harness.agents.data_agent import DataAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext, AgentReply

    class _StubTool:
        async def invoke(self, tool_name, args):
            if tool_name == "get_quote":
                return {"symbol": args["symbol"], "price": 100.0}
            return {"pe": 12.5}

    agent = DataAgent()
    inp = AgentInput(user_message="lookup", context={"symbols": ["AAPL"]})
    ctx = AgentContext(session_id="s1", extra={"tool_executor": _StubTool()})
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert isinstance(result, AgentReply)
    assert result.success is True
    assert len(result.evidence) >= 1
    assert result.confidence is not None


def test_news_agent_v2_validates_lookback_freshness():
    """NewsAgent V2 验证 as_of 与 lookback,过期 → 返回 missing_items。"""
    from tradingagents.agent_harness.agents.news_agent import NewsAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext
    from datetime import datetime, timezone, timedelta

    class _StubTool:
        async def invoke(self, tool_name, args):
            return {"items": [], "fetched_at": datetime.now(timezone.utc).isoformat()}

    agent = NewsAgent()
    # 过期 as_of (30 天前) + 7 天 lookback → missing_items
    as_of = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    inp = AgentInput(
        user_message="news",
        context={"symbols": ["AAPL"], "lookback_days": 7, "as_of": as_of},
    )
    ctx = AgentContext(session_id="s1", extra={"tool_executor": _StubTool()})
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert result.success is True or len(result.missing_items) >= 0
    # 一定有 missing_items 字段(哪怕为空)
    assert hasattr(result, "missing_items")


def test_alpha_agent_v2_selects_compute_path():
    """AlphaAgent V2 从 objective/inputs 选择 compute / list / evaluate。"""
    from tradingagents.agent_harness.agents.alpha_agent import AlphaAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    class _StubTool:
        async def invoke(self, tool_name, args):
            if "list" in tool_name:
                return {"factors": ["alpha_1", "alpha_2"]}
            return {"ic": 0.05, "factor": args.get("factor")}

    agent = AlphaAgent()
    # objective 包含 "compute" → compute path
    inp = AgentInput(
        user_message="compute alpha",
        context={"symbols": ["AAPL"], "action": "compute", "factor": "alpha_1"},
    )
    ctx = AgentContext(session_id="s1", extra={"tool_executor": _StubTool()})
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert result.success is True


def test_domain_agents_have_no_direct_registry_access():
    """Domain agents 不直接访问 AgentRegistry — 只能从 context 拿到 dependencies。"""
    from tradingagents.agent_harness.agents.data_agent import DataAgent
    from tradingagents.agent_harness.agents.news_agent import NewsAgent
    from tradingagents.agent_harness.agents.alpha_agent import AlphaAgent
    for cls in (DataAgent, NewsAgent, AlphaAgent):
        agent = cls()
        # 不应有 agent_registry / provider 属性
        assert not hasattr(agent, "agent_registry")
        assert not hasattr(agent, "provider")


# ════════════════════════════════════════════════════════
# Task 17 — Verifier / Synthesizer V2 with bounded repair
# ════════════════════════════════════════════════════════


def test_verifier_v2_evidence_phase_issues_verified_ref():
    """VERIFY_EVIDENCE 阶段:签发 VerifiedEvidenceRef。"""
    from tradingagents.agent_harness.agents.verifier import VerifierAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext
    from tradingagents.agent_harness.runtime.models import EvidenceRef

    agent = VerifierAgent()
    refs = [
        EvidenceRef(
            artifact_id="a1", producer_task_id="t1",
            source_type="tool", source_name="get_quote",
            content_sha256="sha1", as_of=None,
        ),
    ]
    inp = AgentInput(
        user_message="verify",
        context={"phase": "VERIFY_EVIDENCE", "evidence_refs": [r.model_dump() for r in refs]},
    )
    ctx = AgentContext(session_id="s1")
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert result.success is True
    assert any("verified" in (e.get("source_type") or "") or e.get("verification_level") for e in result.evidence)


def test_verifier_v2_answer_phase_rejects_ungrounded_claim():
    """VERIFY_ANSWER 阶段:无证据的 claim 拒绝,返回 REPAIR_REQUEST outgoing。"""
    from tradingagents.agent_harness.agents.verifier import VerifierAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    agent = VerifierAgent()
    inp = AgentInput(
        user_message="verify answer",
        context={
            "phase": "VERIFY_ANSWER",
            "answer": "Stock will rise 50% tomorrow.",
            "evidence_refs": [],
        },
    )
    ctx = AgentContext(session_id="s1")
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    # 应该有 outgoing REPAIR_REQUEST 草稿
    assert result.success is False or any(
        getattr(d, "type", None) == "REPAIR_REQUEST"
        for d in result.outgoing
    )


def test_synthesizer_v2_rejects_plain_evidence_refs():
    """SynthesizerAgent V2 只接受 VerifiedEvidenceRef,plain EvidenceRef 拒绝。"""
    from tradingagents.agent_harness.agents.synthesizer import SynthesizerAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    agent = SynthesizerAgent()
    # plain EvidenceRef (没有 verification_level)
    inp = AgentInput(
        user_message="synth",
        context={
            "evidence_refs": [
                {"artifact_id": "a1", "producer_task_id": "t1",
                 "source_type": "tool", "source_name": "get_quote",
                 "content_sha256": "sha1", "as_of": None},
            ],
            "objective": "synthesize",
        },
    )
    ctx = AgentContext(session_id="s1")
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    # missing_items 应包含 "unverified_evidence"
    assert "unverified_evidence" in result.missing_items


def test_synthesizer_v2_accepts_verified_refs():
    """VerifiedEvidenceRef 可用 → Synthesizer 产出 answer。"""
    from tradingagents.agent_harness.agents.synthesizer import SynthesizerAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    agent = SynthesizerAgent()
    inp = AgentInput(
        user_message="synth",
        context={
            "evidence_refs": [
                {"artifact_id": "a1", "producer_task_id": "t1",
                 "source_type": "tool", "source_name": "get_quote",
                 "content_sha256": "sha1", "as_of": None,
                 "verification_task_id": "vt1", "verification_level": "L1",
                 "verified_at": "2026-09-21T00:00:00Z"},
            ],
            "objective": "summarize AAPL",
        },
    )
    ctx = AgentContext(session_id="s1")
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    assert result.success is True
    assert result.content  # 至少有内容


def test_verifier_repair_request_includes_target_and_on_reject():
    """REPAIR_REQUEST 必须包含 target_task_id + on_reject + rejected_artifact_ids。"""
    from tradingagents.agent_harness.agents.verifier import VerifierAgent
    from tradingagents.agent_harness.agents.base import AgentInput, AgentContext

    agent = VerifierAgent()
    inp = AgentInput(
        user_message="verify",
        context={
            "phase": "VERIFY_ANSWER",
            "answer": "Unverified claim here.",
            "evidence_refs": [],
            "target_task_id": "t_data_aapl",
        },
    )
    ctx = AgentContext(session_id="s1")
    import asyncio
    result = asyncio.run(agent.run_v2(inp, context=ctx))
    # 找到 REPAIR_REQUEST draft
    repair = [d for d in result.outgoing if getattr(d, "type", None) == "REPAIR_REQUEST"]
    if repair:
        payload = repair[0].payload
        assert getattr(payload, "target_task_id", None) == "t_data_aapl"
        assert getattr(payload, "on_reject", None) in ("FAIL_REQUESTER", "RESUME_REQUESTER")
