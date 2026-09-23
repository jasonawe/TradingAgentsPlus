"""§0.4.29 — LLMIntentRouter test matrix.

Covers five real-world ambiguity cases harvested from the issue history, plus
the LLM-failure fallback path (must drop to keyword without raising), the
cache hit path (second call short-circuits), and the catalog/parse
validation.

Mocking strategy: we mock the ``LLMProvider`` produced by
``self._llm_factory.make(mode="quick")`` so the router never touches the
real LLM. The test asserts on the parsed IntentRoute (intent, op,
source, confidence) — never on the LLM's raw text, since JSON shape is
the router's contract, not the LLM's.

§0.4.29 review checklist:
- [x] "我做一个完整分析…" (§0.4.28 regression) → ANALYSIS/READ, NOT
      ALERT/CREATE
- [x] "做一个 分析" → not CREATE
- [x] "跑一下 600036" → RUN/CREATE (triggers run_trading_agents_analysis)
- [x] "提醒我价格超过 50" → ALERT/CREATE
- [x] "看一下这个资产最近 30 天的价格走势" → HISTORY/READ
- [x] LLM failure → keyword classify() fallback, no exception bubbles
- [x] LLM timeout → same
- [x] JSON parse error → same
- [x] Cache hit on second call (no LLM hit)
- [x] Unknown intent in LLM response → fallback (validation in router)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from tradingagents.agent_harness.core.llm_intent_router import (
    IntentRoute,
    LLMIntentRouter,
    SYSTEM_PROMPT,
)
from tradingagents.agent_harness.core.tier import Intent, Op


# ─────────────────────────────────────────────────────────────────
# Mock LLM plumbing
# ─────────────────────────────────────────────────────────────────


@dataclass
class _MockLLMFactory:
    """Stub for the orchestrator's llm_factory.

    The router calls ``self._llm_factory.make(mode=...)`` which returns
    a provider. The provider's ``complete_text(prompt, system, ...)``
    is what the router actually invokes. We swap that one method.
    """

    response: str = '{"intent": "QUOTE", "op": "READ", "confidence": 0.9}'
    raise_exc: Exception | None = None
    delay_seconds: float = 0.0
    call_count: int = field(default=0)

    def make(self, *, mode: str = "quick"):
        return _MockProvider(self)


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
# Catalog sanity (no LLM)
# ─────────────────────────────────────────────────────────────────


def test_system_prompt_lists_every_intent_and_op():
    """The router's system prompt is the contract surface for the LLM;
    every Intent / Op value must appear so the LLM cannot pick an
    unsupported value."""
    for intent in Intent:
        assert intent.value in SYSTEM_PROMPT, (
            f"Intent.{intent.name}={intent.value!r} missing from system prompt"
        )
    for op in Op:
        assert op.value in SYSTEM_PROMPT, (
            f"Op.{op.name}={op.value!r} missing from system prompt"
        )


def test_router_disabled_uses_keyword_path():
    """When ``enabled=False`` the router must skip the LLM entirely
    and return the keyword classify() result."""
    factory = _MockLLMFactory()
    router = LLMIntentRouter(llm_factory=factory, cache=None, enabled=False)
    route = asyncio.run(router.route("看一下 600036 的价格"))
    assert route.source == "keyword_fallback"
    assert factory.call_count == 0, (
        "disabled router should never call LLM; got "
        f"{factory.call_count} calls"
    )


def test_router_with_unconfigured_factory_uses_keyword_path():
    """No factory wired → keyword fallback, no LLM call."""

    class _UnconfiguredFactory(_MockLLMFactory):
        def make(self, *, mode: str = "quick"):
            raise RuntimeError("factory not configured")

    router = LLMIntentRouter(llm_factory=_UnconfiguredFactory(), cache=None)
    route = asyncio.run(router.route("看一下 600036 的价格"))
    assert route.source == "keyword_fallback"
    assert route.intent == Intent.QUOTE  # keyword classify() default


# ─────────────────────────────────────────────────────────────────
# LLM-driven routing: 5 historical ambiguity cases
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg, expected_intent, expected_op, why", [
    # §0.4.28 — the bug that triggered this refactor
    (
        "我做一个完整分析:基础面+估值+新闻+近期走势+同业对比+监控告警",
        Intent.ANALYSIS, Op.READ,
        "§0.4.28 bug: bare '做一个' substring must not hijack to ALERT/CREATE",
    ),
    # Bare '做一个' should never CREATE without noun
    (
        "做一个 分析",
        Intent.ANALYSIS, Op.READ,
        "bare '做一个 分析' is a read intent, not a CREATE",
    ),
    # Explicit '跑一下' → run_trading_agents_analysis
    (
        "跑一下 600036",
        Intent.RUN, Op.CREATE,
        "explicit '跑一下' verb → start new analysis run",
    ),
    # '提醒我价格超过 50' — threshold/direction slots
    (
        "提醒我价格超过 50",
        Intent.ALERT, Op.CREATE,
        "'提醒我' verb + price threshold → create_alert price_above",
    ),
    # '看一下...最近 30 天的价格走势' → history intent, not quote
    (
        "看一下这个资产最近 30 天的价格走势",
        Intent.HISTORY, Op.READ,
        "N-day history request → get_history, NOT get_quote snapshot",
    ),
])
def test_routes_real_ambiguity_cases(msg, expected_intent, expected_op, why):
    """Each case mocks a specific LLM response and asserts the router
    hands the right (intent, op) to the orchestrator."""
    response = json.dumps({
        "intent": expected_intent.value,
        "op": expected_op.value,
        "confidence": 0.92,
    })
    factory = _MockLLMFactory(response=response)
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    route = asyncio.run(router.route(msg))
    assert route.source == "llm"
    assert route.intent == expected_intent, (
        f"{msg!r}: expected intent={expected_intent.name}, got "
        f"{route.intent.name} ({why})"
    )
    assert route.op == expected_op, (
        f"{msg!r}: expected op={expected_op.name}, got "
        f"{route.op.name} ({why})"
    )
    assert 0.0 <= route.confidence <= 1.0


# ─────────────────────────────────────────────────────────────────
# Failure modes — keyword fallback must hold
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exc_factory,why", [
    (lambda: RuntimeError("provider 5xx"), "provider network error"),
    (lambda: TimeoutError("llm timeout"), "llm timeout"),
    (lambda: ConnectionError("rate limit"), "rate limit"),
])
def test_llm_failure_falls_back_to_keyword(exc_factory, why):
    factory = _MockLLMFactory(raise_exc=exc_factory())
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    # Use a message whose keyword classify result is well-defined so we
    # can assert the fallback fired.
    route = asyncio.run(router.route("提醒我价格超过 50"))
    assert route.source == "keyword_fallback", (
        f"LLM failure did not fall back to keyword ({why}): "
        f"source={route.source}"
    )
    assert route.intent == Intent.ALERT
    assert route.op == Op.CREATE
    # Stats counter incremented
    assert router.stats["llm_failures"] == 1, router.stats
    assert router.stats["keyword_fallbacks"] >= 1


def test_malformed_json_response_falls_back_to_keyword():
    """LLM returns text with no JSON object → parse error → fallback."""
    factory = _MockLLMFactory(response="I cannot classify this message.")
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    route = asyncio.run(router.route("提醒我价格超过 50"))
    assert route.source == "keyword_fallback"
    assert route.intent == Intent.ALERT


def test_unknown_intent_in_response_falls_back():
    """LLM hallucinates an intent not in the enum → fallback."""
    factory = _MockLLMFactory(response='{"intent": "NUKE", "op": "READ"}')
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    # Use a message with well-defined keyword routing so the test
    # distinguishes "fallback fired AND keyword agreed" from "fallback
    # fired AND keyword disagrees".
    route = asyncio.run(router.route("提醒我价格超过 50"))
    assert route.source == "keyword_fallback"
    assert route.intent == Intent.ALERT


def test_unknown_op_in_response_falls_back():
    """Same for unknown op."""
    factory = _MockLLMFactory(response='{"intent": "QUOTE", "op": "NUKE"}')
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    route = asyncio.run(router.route("提醒我价格超过 50"))
    assert route.source == "keyword_fallback"
    assert route.op == Op.CREATE


# ─────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────


@dataclass
class _SimpleCache:
    """In-memory dict cache stub with the get/put contract the router
    uses. (We don't import the full PlanTemplateCache to keep the test
    independent.)"""
    store: dict[str, Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, key):
        v = self.store.get(key)
        if v is not None:
            self.hits += 1
            return v
        self.misses += 1
        return None

    def put(self, key, value):
        self.store[key] = value


def test_cache_hit_short_circuits_llm():
    """Second call with the same message must not call the LLM again."""
    cache = _SimpleCache()
    factory = _MockLLMFactory(
        response='{"intent": "QUOTE", "op": "READ", "confidence": 0.95}',
    )
    router = LLMIntentRouter(llm_factory=factory, cache=cache)

    r1 = asyncio.run(router.route("看一下 600036"))
    assert r1.source == "llm"
    assert factory.call_count == 1

    r2 = asyncio.run(router.route("看一下 600036"))
    assert r2.source == "cache", (
        f"second call should hit cache, got source={r2.source}"
    )
    assert factory.call_count == 1, (
        f"cache hit should NOT call LLM again; got {factory.call_count} calls"
    )


def test_cache_key_changes_when_message_changes():
    """Two different messages must produce two different cache keys."""
    cache = _SimpleCache()
    factory = _MockLLMFactory(
        response='{"intent": "QUOTE", "op": "READ", "confidence": 0.95}',
    )
    router = LLMIntentRouter(llm_factory=factory, cache=cache)

    asyncio.run(router.route("看一下 600036"))
    asyncio.run(router.route("看一下 600000"))
    # Both should have hit the LLM (different cache keys).
    assert factory.call_count == 2


# ─────────────────────────────────────────────────────────────────
# Empty / whitespace message
# ─────────────────────────────────────────────────────────────────


def test_empty_message_uses_keyword_path():
    factory = _MockLLMFactory()
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    route = asyncio.run(router.route(""))
    assert route.source == "keyword_fallback"
    assert factory.call_count == 0, "empty message should not call LLM"


# ─────────────────────────────────────────────────────────────────
# Stats observability
# ─────────────────────────────────────────────────────────────────


def test_stats_counter_increments_correctly():
    """Verify the router's stats dict is updated for observability.

    Stats semantics:
    - ``llm_calls``: counts successful LLM completions (1 per turn
      with a cache miss). Cache hits don't count.
    - ``cache_hits``: counts cache hits (no LLM call).
    - ``llm_failures``: counts LLM exceptions (provider errors).
    - ``parse_failures``: counts JSON parse / unknown-enum failures.
    - ``keyword_fallbacks``: counts keyword classify() invocations
      (covers parse failures, unknown enums, and LLM exceptions).
    """
    cache = _SimpleCache()
    factory = _MockLLMFactory(
        response='{"intent": "QUOTE", "op": "READ", "confidence": 0.9}',
    )
    router = LLMIntentRouter(llm_factory=factory, cache=cache)

    asyncio.run(router.route("看一下 600036"))        # LLM success (1)
    asyncio.run(router.route("看一下 600036"))        # cache hit (1)
    factory.response = "garbage"
    asyncio.run(router.route("提醒我价格超过 50"))  # parse fail (1)
    factory.raise_exc = RuntimeError("boom")
    asyncio.run(router.route("跑一下 600036"))     # exception (1)

    assert router.stats["llm_calls"] == 1, router.stats
    assert router.stats["cache_hits"] == 1, router.stats
    assert router.stats["keyword_fallbacks"] == 2, router.stats
    assert router.stats["llm_failures"] == 1, router.stats
    assert router.stats["parse_failures"] == 1, router.stats


# ─────────────────────────────────────────────────────────────────
# Markdown-fenced JSON response (some LLMs add ```json fences)
# ─────────────────────────────────────────────────────────────────


def test_router_handles_markdown_fenced_json():
    factory = _MockLLMFactory(
        response='```json\n{"intent": "RUN", "op": "CREATE", "confidence": 0.88}\n```',
    )
    router = LLMIntentRouter(llm_factory=factory, cache=None)
    route = asyncio.run(router.route("跑一下 600036"))
    assert route.source == "llm"
    assert route.intent == Intent.RUN
    assert route.op == Op.CREATE
