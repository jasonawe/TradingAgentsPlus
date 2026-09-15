"""LLM response cache — short-circuit identical LLM calls."""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.llm.base import ChatMessage, LLMResponse
from tradingagents.agent_harness.llm.cache import (
    LLMResponseCache,
    make_cache_key,
)


# --------------------------------------------------------------------------
# make_cache_key
# --------------------------------------------------------------------------
class TestMakeCacheKey:
    def test_same_messages_same_key(self):
        msgs = [ChatMessage(role="user", content="hello")]
        k1 = make_cache_key(msgs, system="sys", temperature=0.0, max_tokens=None, model="m")
        k2 = make_cache_key(msgs, system="sys", temperature=0.0, max_tokens=None, model="m")
        assert k1 == k2

    def test_different_content_different_key(self):
        msgs1 = [ChatMessage(role="user", content="hello")]
        msgs2 = [ChatMessage(role="user", content="hi")]
        k1 = make_cache_key(msgs1, system="sys", temperature=0.0, max_tokens=None, model="m")
        k2 = make_cache_key(msgs2, system="sys", temperature=0.0, max_tokens=None, model="m")
        assert k1 != k2

    def test_different_temperature_different_key(self):
        msgs = [ChatMessage(role="user", content="x")]
        k1 = make_cache_key(msgs, system=None, temperature=0.0, max_tokens=None, model="m")
        k2 = make_cache_key(msgs, system=None, temperature=0.7, max_tokens=None, model="m")
        assert k1 != k2

    def test_different_system_different_key(self):
        msgs = [ChatMessage(role="user", content="x")]
        k1 = make_cache_key(msgs, system="A", temperature=0.0, max_tokens=None, model="m")
        k2 = make_cache_key(msgs, system="B", temperature=0.0, max_tokens=None, model="m")
        assert k1 != k2

    def test_different_model_different_key(self):
        msgs = [ChatMessage(role="user", content="x")]
        k1 = make_cache_key(msgs, system=None, temperature=0.0, max_tokens=None, model="gpt-4o")
        k2 = make_cache_key(msgs, system=None, temperature=0.0, max_tokens=None, model="claude-3")
        assert k1 != k2

    def test_message_order_matters(self):
        msgs1 = [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hi"),
        ]
        msgs2 = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="system", content="sys"),
        ]
        k1 = make_cache_key(msgs1, system=None, temperature=0.0, max_tokens=None, model="m")
        k2 = make_cache_key(msgs2, system=None, temperature=0.0, max_tokens=None, model="m")
        assert k1 != k2


# --------------------------------------------------------------------------
# LLMResponseCache — get/put
# --------------------------------------------------------------------------
class TestGetPut:
    def test_miss_returns_none(self):
        c = LLMResponseCache()
        assert c.get("nope") is None
        assert c.misses == 1
        assert c.hits == 0

    def test_hit_returns_response(self):
        c = LLMResponseCache()
        resp = LLMResponse(content="hi", provider="p", model="m")
        c.put("k", resp)
        assert c.get("k") is resp
        assert c.hits == 1

    def test_overwrite_replaces(self):
        c = LLMResponseCache()
        c.put("k", LLMResponse(content="v1", provider="p", model="m"))
        c.put("k", LLMResponse(content="v2", provider="p", model="m"))
        assert c.get("k").content == "v2"


# --------------------------------------------------------------------------
# LRU eviction
# --------------------------------------------------------------------------
class TestEviction:
    def test_evicts_oldest_when_full(self):
        c = LLMResponseCache(max_entries=3)
        for i in range(3):
            c.put(f"k{i}", LLMResponse(content=str(i), provider="p", model="m"))
        # Adding a 4th should evict k0
        c.put("k4", LLMResponse(content="4", provider="p", model="m"))
        assert c.get("k0") is None
        assert c.get("k4") is not None
        assert c.evictions == 1

    def test_hit_refreshes_lru(self):
        c = LLMResponseCache(max_entries=3)
        c.put("k0", LLMResponse(content="0", provider="p", model="m"))
        c.put("k1", LLMResponse(content="1", provider="p", model="m"))
        c.put("k2", LLMResponse(content="2", provider="p", model="m"))
        # Touch k0 → moves to most-recent
        c.get("k0")
        # Add k3 → should evict k1 (oldest now)
        c.put("k3", LLMResponse(content="3", provider="p", model="m"))
        assert c.get("k0") is not None  # still cached
        assert c.get("k1") is None      # evicted
        assert c.get("k3") is not None


# --------------------------------------------------------------------------
# TTL
# --------------------------------------------------------------------------
class TestTTL:
    def test_expired_entry_misses(self):
        c = LLMResponseCache(ttl_seconds=0.01)
        c.put("k", LLMResponse(content="v", provider="p", model="m"))
        import time
        time.sleep(0.02)
        assert c.get("k") is None
        assert c.misses == 1

    def test_fresh_entry_hits(self):
        c = LLMResponseCache(ttl_seconds=60.0)
        c.put("k", LLMResponse(content="v", provider="p", model="m"))
        assert c.get("k") is not None


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
class TestStats:
    def test_initial_stats(self):
        c = LLMResponseCache()
        s = c.stats
        assert s["hits"] == 0
        assert s["misses"] == 0
        assert s["stores"] == 0
        assert s["hit_rate"] == 0.0
        assert s["size"] == 0

    def test_hit_rate_calculated(self):
        c = LLMResponseCache()
        c.put("k", LLMResponse(content="v", provider="p", model="m"))
        c.get("k")  # hit
        c.get("k")  # hit
        c.get("nope")  # miss
        s = c.stats
        assert s["hits"] == 2
        assert s["misses"] == 1
        assert abs(s["hit_rate"] - 2/3) < 0.01

    def test_clear_resets_in_memory(self):
        c = LLMResponseCache()
        c.put("k1", LLMResponse(content="a", provider="p", model="m"))
        c.put("k2", LLMResponse(content="b", provider="p", model="m"))
        assert c.stats["size"] == 2
        c.clear()
        assert c.stats["size"] == 0
        assert c.get("k1") is None


# --------------------------------------------------------------------------
# OpenAICompatibleProvider integration — cache.get/put called from complete()
# --------------------------------------------------------------------------
class TestProviderCacheIntegration:
    def test_repeated_call_hits_cache(self):
        """Two identical LLM calls → second one served from cache."""
        from unittest.mock import MagicMock, patch
        from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
        from tradingagents.agent_harness.llm.base import ChatMessage, LLMResponse

        cache = LLMResponseCache()

        # Stub the underlying client so we don't need real API keys
        class _StubLLM:
            def invoke(self, payload, **kwargs):
                # Return an object with .content + .response_metadata
                m = MagicMock()
                m.content = "cached content"
                m.response_metadata = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                return m

        # create_llm_client is the import in openai_provider; patch it.
        with patch("tradingagents.agent_harness.llm.openai_provider.create_llm_client") as mock_factory:
            mock_factory.return_value.get_llm.return_value = _StubLLM()
            provider = OpenAICompatibleProvider("p", "m", cache=cache)

        msgs = [ChatMessage(role="user", content="hello")]
        # First call — miss, should call underlying LLM
        r1 = provider.complete(msgs, temperature=0.0)
        assert r1.content == "cached content"
        assert cache.stats["misses"] == 1
        assert cache.stats["hits"] == 0

        # Second call — hit, no LLM invocation
        # Reset mock to detect if it's called again
        with patch("tradingagents.agent_harness.llm.openai_provider.create_llm_client") as mock_factory2:
            called = []
            class _TrackerLLM:
                def invoke(self, payload, **kwargs):
                    called.append(1)
                    return MagicMock(content="different", response_metadata={"token_usage": {}})
            mock_factory2.return_value.get_llm.return_value = _TrackerLLM()
            # Re-create provider with same cache
            provider2 = OpenAICompatibleProvider("p", "m", cache=cache)
            r2 = provider2.complete(msgs, temperature=0.0)
        assert r2.content == "cached content"  # from cache, not "different"
        assert called == []  # underlying LLM NOT called
        assert cache.stats["hits"] == 1

    def test_different_temperature_bypasses_cache(self):
        """temp=0 and temp=0.7 must NOT share cache entries."""
        from unittest.mock import MagicMock, patch
        from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
        from tradingagents.agent_harness.llm.base import ChatMessage

        cache = LLMResponseCache()
        msgs = [ChatMessage(role="user", content="x")]

        with patch("tradingagents.agent_harness.llm.openai_provider.create_llm_client") as mock:
            mock.return_value.get_llm.return_value = MagicMock(
                invoke=lambda *a, **k: MagicMock(
                    content="ok", response_metadata={"token_usage": {}}
                )
            )
            provider = OpenAICompatibleProvider("p", "m", cache=cache)
            provider.complete(msgs, temperature=0.0)
            provider.complete(msgs, temperature=0.7)

        # Two different keys → both misses
        assert cache.stats["stores"] == 2
        assert cache.stats["misses"] == 2
        assert cache.stats["hits"] == 0

    def test_no_cache_means_no_caching(self):
        """Provider without cache behaves as before."""
        from unittest.mock import MagicMock, patch
        from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
        from tradingagents.agent_harness.llm.base import ChatMessage

        msgs = [ChatMessage(role="user", content="x")]
        with patch("tradingagents.agent_harness.llm.openai_provider.create_llm_client") as mock:
            mock.return_value.get_llm.return_value = MagicMock(
                invoke=lambda *a, **k: MagicMock(
                    content="ok", response_metadata={"token_usage": {}}
                )
            )
            provider = OpenAICompatibleProvider("p", "m")  # no cache
            provider.complete(msgs, temperature=0.0)
            provider.complete(msgs, temperature=0.0)
            # No cache means no instrumentation needed — just that it works.
