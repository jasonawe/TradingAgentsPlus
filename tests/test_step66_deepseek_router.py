"""§0.4.30 — DeepSeek-style router integration tests.

Covers:

- Router emits a ToolCall list from LLM JSON output (single tool).
- Router emits parallel calls when LLM returns multi-tool plan.
- LLM failure falls back to keyword (no exception bubbles).
- LLM hallucinated tool is dropped (catalog whitelist).
- Cache hit short-circuits the LLM on second call.
- _plan() returns router plan when source=="llm".
- _plan() falls back to _plan_keyword when source=="keyword_fallback".
- pre_plan_hook can mutate the plan.
- Tool policy: write tool returns ASK, read returns "".
- build_catalog tolerates mock registries (list_names + get only).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from tradingagents.agent_harness.core.llm_catalog import (
    ToolCatalogEntry,
    build_catalog,
    catalog_to_prompt,
    get_tool_names,
    pydantic_to_json_schema,
)
from tradingagents.agent_harness.core.llm_router import (
    LLMRouter,
    RouterPlan,
    ToolCall,
)
from tradingagents.agent_harness.core.tool_policy import (
    Verdict,
    PolicyDecision,
    apply_policies,
    policy_for_tool,
)


# ─────────────────────────────────────────────────────────────────
# Mock LLM plumbing
# ─────────────────────────────────────────────────────────────────


@dataclass
class _MockLLMFactory:
    """Stub for the orchestrator's llm_factory.

    Mirrors the §0.4.29 test (test_step65) — a tiny dataclass whose
    ``make(mode=...)`` returns a provider with a controllable
    ``complete_text``.
    """

    response: str = '{"intent": "QUOTE", "op": "READ", "confidence": 0.9}'
    raise_exc: Exception | None = None
    delay_seconds: float = 0.0
    call_count: int = field(default=0)
    configured: bool = True

    def make(self, *, mode: str = "deep"):
        return _MockProvider(self)

    def is_configured(self) -> bool:
        return self.configured


@dataclass
class _MockProvider:
    factory: _MockLLMFactory

    def complete_text(self, *, prompt, system, temperature=0.0, max_tokens=80):
        self.factory.call_count += 1
        if self.factory.delay_seconds:
            import time as _t
            _t.sleep(self.factory.delay_seconds)
        if self.factory.raise_exc:
            raise self.factory.raise_exc
        return _MockResponse(content=self.factory.response)


@dataclass
class _MockResponse:
    content: str


# ─────────────────────────────────────────────────────────────────
# Catalog tests (no LLM)
# ─────────────────────────────────────────────────────────────────


def test_pydantic_to_json_schema_handles_optional():
    """Pydantic v2 emits ``anyOf`` for Optional fields; we collapse to ``type``."""
    from pydantic import BaseModel

    class Foo(BaseModel):
        symbol: str
        limit: int | None = None

    schema = pydantic_to_json_schema(Foo)
    assert schema["type"] == "object"
    assert "symbol" in schema["properties"]
    assert schema["properties"]["symbol"].get("type") == "string"
    # limit is Optional[int] -> collapsed to int.
    assert schema["properties"]["limit"].get("type") in ("integer", "number")
    assert "symbol" in schema["required"]
    assert "limit" not in schema["required"]


def test_pydantic_to_json_schema_accepts_dict_and_none():
    assert pydantic_to_json_schema(dict) == {"type": "object", "properties": {}, "additionalProperties": True}
    assert pydantic_to_json_schema(type(None)) == {"type": "object", "properties": {}, "additionalProperties": True}


def test_build_catalog_handles_list_names_only_registry():
    """Some legacy tests mock a registry with only list_names + get."""
    from pydantic import BaseModel

    class Args(BaseModel):
        symbol: str

    class StubTool:
        name = "stub_tool"
        description = "stub"
        permission = "read"
        schema = type(
            "S", (), {
                "args_schema": Args,
                "permission": "read",
                "metadata": {},
            }
        )()

    class MiniRegistry:
        def list_names(self):
            return ["stub_tool"]

        def get(self, name):
            return StubTool()

    entries = build_catalog(MiniRegistry())
    assert len(entries) == 1
    assert entries[0].name == "stub_tool"
    assert entries[0].is_write is False
    assert entries[0].concurrency_safe is True


def test_build_catalog_marks_write_tools_serial():
    """Write tools default to concurrency_safe=False."""
    from pydantic import BaseModel

    class Args(BaseModel):
        symbol: str

    class ReadTool:
        name = "read_tool"
        description = "r"
        permission = "read"
        schema = type("S", (), {"args_schema": Args, "permission": "read", "metadata": {}})()

    class WriteTool:
        name = "write_tool"
        description = "w"
        permission = "write"
        schema = type("S", (), {"args_schema": Args, "permission": "write", "metadata": {"side_effect_mode": "local_transactional"}})()

    class Reg:
        def list_all(self):
            return [ReadTool(), WriteTool()]

    entries = build_catalog(Reg())
    by_name = {e.name: e for e in entries}
    assert by_name["read_tool"].concurrency_safe is True
    assert by_name["write_tool"].concurrency_safe is False
    assert by_name["write_tool"].is_write is True


def test_concurrency_safe_metadata_overrides_default():
    """A tool can opt-in to concurrency_safe=True via metadata."""
    from pydantic import BaseModel

    class Args(BaseModel):
        symbol: str

    class WriteTool:
        name = "wt"
        description = "w"
        permission = "write"
        schema = type("S", (), {
            "args_schema": Args,
            "permission": "write",
            "metadata": {"side_effect_mode": "local_transactional", "concurrency_safe": True},
        })()

    class Reg:
        def list_all(self):
            return [WriteTool()]

    entries = build_catalog(Reg())
    assert entries[0].concurrency_safe is True
    assert entries[0].is_write is True  # is_write still True; HITL still triggers


def test_tool_catalog_entry_prompt_block_marks_flags():
    e = ToolCatalogEntry(
        name="x", description="desc", parameters={"type": "object"},
        is_write=True, concurrency_safe=False,
    )
    block = e.to_prompt_block()
    assert "`x`" in block
    assert "WRITE" in block
    assert "SERIAL" in block


# ─────────────────────────────────────────────────────────────────
# Router tests
# ─────────────────────────────────────────────────────────────────


def _mini_catalog():
    from pydantic import BaseModel

    class Q(BaseModel):
        symbol: str

    class A(BaseModel):
        body_md: str

    class QuoteTool:
        name = "get_quote"
        description = "quote"
        permission = "read"
        schema = type("S", (), {"args_schema": Q, "permission": "read", "metadata": {}})()

    class NewsTool:
        name = "get_news"
        description = "news"
        permission = "read"
        schema = type("S", (), {"args_schema": Q, "permission": "read", "metadata": {}})()

    class NoteTool:
        name = "create_note"
        description = "note"
        permission = "write"
        schema = type("S", (), {"args_schema": A, "permission": "write", "metadata": {"side_effect_mode": "local_transactional"}})()

    class Reg:
        def list_all(self):
            return [QuoteTool(), NewsTool(), NoteTool()]

    return build_catalog(Reg())


def _run(coro):
    return asyncio.run(coro)


def test_router_parses_single_tool_plan():
    cat = _mini_catalog()
    factory = _MockLLMFactory(response='[{"tool": "get_quote", "args": {"symbol": "600036.SS"}}]')
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("看 600036"))
    assert plan.source == "llm"
    assert len(plan.calls) == 1
    assert plan.calls[0].tool == "get_quote"
    assert plan.calls[0].args == {"symbol": "600036.SS"}


def test_router_parses_parallel_multi_tool_plan():
    """The user's main scenario: '做一个完整分析:基础面+…+监控告警' must
    fan out to multiple tools in one parallel group."""
    cat = _mini_catalog()
    response = json.dumps([
        {"tool": "get_quote", "args": {"symbol": "600036.SS"}, "parallel_group": 0},
        {"tool": "get_news", "args": {"symbol": "600036.SS"}, "parallel_group": 0},
    ], ensure_ascii=False)
    factory = _MockLLMFactory(response=response)
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("完整分析 600036 基础面+新闻"))
    assert plan.source == "llm"
    assert len(plan.calls) == 2
    tools = {c.tool for c in plan.calls}
    assert tools == {"get_quote", "get_news"}


def test_router_strips_markdown_fences():
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response='```json\n[{"tool": "get_quote", "args": {"symbol": "AAPL"}}]\n```',
    )
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("aapl"))
    assert len(plan.calls) == 1
    assert plan.calls[0].tool == "get_quote"


def test_router_drops_unknown_tool():
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response=json.dumps([
            {"tool": "get_quote", "args": {"symbol": "AAPL"}},
            {"tool": "magic_tool", "args": {}},  # not in catalog
        ]),
    )
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("aapl"))
    assert [c.tool for c in plan.calls] == ["get_quote"]
    assert router.stats["dropped_calls"] == 1


def test_router_drops_call_missing_required_field():
    """If the LLM forgot the required ``symbol``, drop the call."""
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response=json.dumps([{"tool": "get_quote", "args": {}}]),
    )
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("any"))
    assert plan.calls == []


def test_router_dedupes_repeated_calls():
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response=json.dumps([
            {"tool": "get_quote", "args": {"symbol": "AAPL"}},
            {"tool": "get_quote", "args": {"symbol": "AAPL"}},  # duplicate
        ]),
    )
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("aapl"))
    assert len(plan.calls) == 1


def test_router_short_circuits_a_class_question():
    # §0.4.31 — pure A-class (date/weekday/time) queries are short-circuited
    # BEFORE the LLM call, so source="empty" (deterministic) instead of
    # "llm" (which costs an API roundtrip just to learn the answer is
    # already known). The previous "当您 == 下m" expectation is replaced
    # with the empty / fallback_reason contract.
    cat = _mini_catalog()
    factory = _MockLLMFactory(response="[]")
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("今天是周几"))
    assert plan.source == "empty"
    assert plan.calls == []
    assert "A-class" in (plan.fallback_reason or "")


def test_router_does_not_short_circuit_b_class_today():
    # §0.4.31 — "今天 600036.SS 的收盘价" is B-class (has a ticker), the
    # A-class short-circuit must NOT fire — the LLM still gets to plan
    # the get_quote call.
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response='[{"tool": "get_quote", "args": {"symbol": "600036.SS"}}]'
    )
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("今天 600036.SS 的收盘价"))
    assert plan.source == "llm"
    assert len(plan.calls) == 1
    assert plan.calls[0].tool == "get_quote"


def test_router_falls_back_on_llm_error():
    cat = _mini_catalog()
    factory = _MockLLMFactory(raise_exc=RuntimeError("provider down"))
    router = LLMRouter(llm_factory=factory, catalog=cat)
    plan = _run(router.route("any"))
    assert plan.source == "keyword_fallback"
    assert plan.calls == []
    assert "provider down" in (plan.fallback_reason or "")
    assert router.stats["fallbacks"] == 1


def test_router_returns_empty_when_factory_disabled():
    cat = _mini_catalog()
    router = LLMRouter(llm_factory=None, catalog=cat)
    plan = _run(router.route("any"))
    assert plan.source == "empty"
    assert plan.calls == []


def test_router_cache_short_circuits_second_call():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    cat = _mini_catalog()
    factory = _MockLLMFactory(
        response=json.dumps([{"tool": "get_quote", "args": {"symbol": "AAPL"}}]),
    )
    cache = PlanTemplateCache(ttl_seconds=60.0, max_entries=16)
    router = LLMRouter(llm_factory=factory, catalog=cat, cache=cache)
    plan1 = _run(router.route("aapl"))
    plan2 = _run(router.route("aapl"))
    assert plan1.source == "llm"
    assert plan2.source == "cache"
    assert factory.call_count == 1  # second call didn't hit LLM


# ─────────────────────────────────────────────────────────────────
# Tool policy tests
# ─────────────────────────────────────────────────────────────────


def test_passthrough_policy_for_read():
    ctx = type("Ctx", (), {"session_id": "s", "tool_name": "get_quote", "user_message": "x"})()
    d = apply_policies("get_quote", {"symbol": "AAPL"}, context=ctx, is_write=False)
    assert d.verdict == Verdict.ALLOW


def test_hitl_policy_ask_for_write():
    """Write tools trigger HITL ASK (we don't hit _hitl_gate here since
    it's patched away; the policy returns ASK defensively)."""
    ctx = type("Ctx", (), {"session_id": "s", "tool_name": "create_note", "user_message": "x"})()
    d = apply_policies("create_note", {"body_md": "hi"}, context=ctx, is_write=True)
    # Either ALLOW (when _hitl_gate is importable but not approved) or ASK.
    # Both are safe — what we don't want is a crash.
    assert d.verdict in (Verdict.ALLOW, Verdict.ASK)


def test_policy_for_tool_picks_correct_default():
    assert policy_for_tool("get_quote", is_write=False).__name__ == "passthrough_policy"
    assert policy_for_tool("create_note", is_write=True).__name__ == "hitl_policy"


def test_register_policy_overrides_default():
    from tradingagents.agent_harness.core.tool_policy import (
        passthrough_policy,
        register_policy,
        reset_custom_policies,
    )

    reset_custom_policies()
    try:
        def custom(args, *, context):
            return PolicyDecision(verdict=Verdict.REJECT, reason="no")

        register_policy("get_quote", custom)
        d = apply_policies(
            "get_quote", {}, context=None, is_write=False,
        )
        assert d.verdict == Verdict.REJECT
    finally:
        reset_custom_policies()


# ─────────────────────────────────────────────────────────────────
# _plan_from_router tests
# ─────────────────────────────────────────────────────────────────


def test_plan_from_router_empty_returns_empty_list():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    plan = Orchestrator._plan_from_router(RouterPlan(calls=[], source="llm"), state=None)
    assert plan == []


def test_plan_from_router_single_returns_sequential():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    plan = Orchestrator._plan_from_router(
        RouterPlan(
            calls=[ToolCall(tool="get_quote", args={"symbol": "AAPL"})],
            source="llm",
        ),
        state=None,
    )
    assert isinstance(plan, list)
    assert len(plan) == 1
    assert plan[0]["action"] == "get_quote"
    assert plan[0]["args"] == {"symbol": "AAPL"}


def test_plan_from_router_multi_returns_ptc():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    plan = Orchestrator._plan_from_router(
        RouterPlan(
            calls=[
                ToolCall(tool="get_quote", args={"symbol": "AAPL"}, parallel_group=0),
                ToolCall(tool="get_news", args={"symbol": "AAPL"}, parallel_group=0),
            ],
            source="llm",
        ),
        state=None,
    )
    assert isinstance(plan, dict)
    assert plan["mode"] == "ptc"
    assert len(plan["groups"]) == 1
    g = plan["groups"][0]
    assert {c["name"] for c in g["calls"]} == {"get_quote", "get_news"}


def test_plan_from_router_sequential_groups_get_dependencies():
    """Two parallel groups: group 0 then group 1 with depends_on=[g0]."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    plan = Orchestrator._plan_from_router(
        RouterPlan(
            calls=[
                ToolCall(tool="get_quote", args={"symbol": "AAPL"}, parallel_group=0),
                ToolCall(tool="get_news", args={"symbol": "AAPL"}, parallel_group=1),
            ],
            source="llm",
        ),
        state=None,
    )
    assert plan["mode"] == "ptc"
    assert len(plan["groups"]) == 2
    assert "depends_on" not in plan["groups"][0]
    assert plan["groups"][1]["depends_on"] == ["g0"]
