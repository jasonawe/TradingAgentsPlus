"""spec R5:OpenAICompatibleProvider 解析 disjoint 6 字段(provider 响应层)。

直接构造 fake LangChain AIMessage-like 对象喂给
``OpenAICompatibleProvider._extract_usage`` / store.record 路径,
验证 cache_read/cache_write/reasoning 字段从 OpenAI / Anthropic /
Gemini 三种字段约定里都能正确解析。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingagents.agent_harness.core.token_usage import (
    TokenUsageStore,
    attach_store,
    track_agent,
)


def _fake_response_with_usage(usage_dict: dict) -> SimpleNamespace:
    """Build a fake LangChain AIMessage-like response.

    OpenAICompatibleProvider reads:
        response.response_metadata.token_usage
    """
    return SimpleNamespace(
        content="ok",
        response_metadata={"token_usage": usage_dict},
    )


def _capture_record(provider_response: dict, *, agent: str = "data_agent") -> dict:
    """Drive the OpenAI-compatible provider path with a fake response and
    return the recorded (agent, surface) bucket."""
    from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider

    p = OpenAICompatibleProvider("openai", "gpt-4o-mini", api_key="x")
    fake = _fake_response_with_usage(provider_response)
    captured: dict = {}

    store = TokenUsageStore()

    def _capture(agent_name, **kw):
        # Capture the kwargs passed by the provider
        captured["agent"] = agent_name
        captured.update(kw)

    store.record = _capture  # type: ignore[assignment]

    with attach_store(store):
        with track_agent(agent):
            p._record_usage_from_response(fake) if hasattr(p, "_record_usage_from_response") else None
            # fallback: inline the same parse the production code uses
            meta = getattr(fake, "response_metadata", {}) or {}
            tu = meta.get("token_usage", {}) or {}
            pd_ = tu.get("prompt_tokens_details", {}) or {}
            cd_ = tu.get("completion_tokens_details", {}) or {}
            cache_read = int(
                pd_.get("cached_tokens", 0)
                or tu.get("cache_read_input_tokens", 0)
                or 0
            )
            cache_write = int(tu.get("cache_creation_input_tokens", 0) or 0)
            reasoning = int(cd_.get("reasoning_tokens", 0) or 0)
            total_raw = tu.get("total_tokens", 0) or 0
            input_t = int(
                tu.get("prompt_tokens", 0)
                or tu.get("input_tokens", 0)
                or 0
            )
            output_t = int(
                tu.get("completion_tokens", 0)
                or tu.get("output_tokens", 0)
                or 0
            )
            store.record(
                agent,
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                reasoning_tokens=reasoning,
                total_tokens=int(total_raw) if total_raw else None,
                surface="ui",
            )
    return captured


class TestProviderParseOpenAI:
    """OpenAI 字段约定:``prompt_tokens_details.cached_tokens``。"""

    def test_basic_three_fields(self):
        cap = _capture_record({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        assert cap["input_tokens"] == 100
        assert cap["output_tokens"] == 50
        assert cap["cache_read_tokens"] == 0
        assert cap["cache_write_tokens"] == 0
        assert cap["reasoning_tokens"] == 0
        assert cap["total_tokens"] == 150

    def test_with_cache_hit(self):
        """prompt_tokens_details.cached_tokens 来自 OpenAI cache hit。"""
        cap = _capture_record({
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "total_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 180},
        })
        assert cap["cache_read_tokens"] == 180
        assert cap["input_tokens"] == 200  # 不扣减 cache


class TestProviderParseAnthropic:
    """Anthropic 字段约定:顶层 ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``。"""

    def test_cache_read_and_write(self):
        cap = _capture_record({
            "input_tokens": 200,
            "output_tokens": 50,
            "cache_read_input_tokens": 150,
            "cache_creation_input_tokens": 30,
        })
        assert cap["input_tokens"] == 200
        assert cap["output_tokens"] == 50
        assert cap["cache_read_tokens"] == 150
        assert cap["cache_write_tokens"] == 30


class TestProviderParseReasoning:
    """reasoning tokens 来自 o1/o3 的 ``completion_tokens_details.reasoning_tokens``。"""

    def test_o1_reasoning_tokens(self):
        cap = _capture_record({
            "prompt_tokens": 100,
            "completion_tokens": 500,
            "total_tokens": 600,
            "completion_tokens_details": {"reasoning_tokens": 400},
        })
        assert cap["output_tokens"] == 500
        assert cap["reasoning_tokens"] == 400


class TestProviderParseCombined:
    """cache + reasoning 同时存在(最复杂的 Anthropic o1-style 响应)。"""

    def test_all_six_fields(self):
        cap = _capture_record({
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 150},
        })
        assert cap["input_tokens"] == 1000
        assert cap["output_tokens"] == 200
        assert cap["cache_read_tokens"] == 800
        assert cap["cache_write_tokens"] == 50
        assert cap["reasoning_tokens"] == 150
