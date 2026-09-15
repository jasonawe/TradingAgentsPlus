"""TokenUsageStore — per-session token accounting partitioned by agent+surface."""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.token_usage import (
    DEFAULT_AGENT,
    DEFAULT_SURFACE,
    TokenUsageStore,
    UsageBucket,
    attach_store,
    get_active_agent,
    get_active_store,
    get_active_surface,
    track_agent,
    track_surface,
)


# --------------------------------------------------------------------------
# UsageBucket
# --------------------------------------------------------------------------
class TestUsageBucket:
    def test_empty_defaults(self):
        b = UsageBucket()
        # spec R5: disjoint 6 字段 + 2 计数
        assert b.input_tokens == 0
        assert b.output_tokens == 0
        assert b.cache_read_tokens == 0
        assert b.cache_write_tokens == 0
        assert b.reasoning_tokens == 0
        assert b.total_tokens == 0
        assert b.calls == 0
        assert b.errors == 0

    def test_add_basic(self):
        b = UsageBucket()
        b.add(input_tokens=100, output_tokens=50)
        assert b.input_tokens == 100
        assert b.output_tokens == 50
        # total_tokens derived from input + output when not provided
        assert b.total_tokens == 150
        assert b.calls == 1

    def test_add_explicit_total(self):
        b = UsageBucket()
        b.add(input_tokens=100, output_tokens=50, total_tokens=200)
        assert b.total_tokens == 200

    def test_add_error_counter(self):
        b = UsageBucket()
        b.add(input_tokens=10, output_tokens=5, errored=True)
        assert b.errors == 1
        b.add(input_tokens=10, output_tokens=5, errored=False)
        assert b.errors == 1  # unchanged

    def test_to_dict_shape(self):
        """spec R5:to_dict 必须包含 disjoint 6 字段 + 2 计数 = 8 字段。"""
        b = UsageBucket()
        b.add(input_tokens=10, output_tokens=20, total_tokens=30)
        d = b.to_dict()
        assert set(d.keys()) == {
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
            "total_tokens", "calls", "errors",
        }
        assert d["input_tokens"] == 10
        assert d["output_tokens"] == 20
        assert d["total_tokens"] == 30
        assert d["calls"] == 1
        assert d["errors"] == 0

    def test_add_disjoint_6_fields(self):
        """spec R5:cache_read/cache_write 是 input 的子集,reasoning 是 output 子集,
        但账面上分开报数。"""
        b = UsageBucket()
        b.add(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=40,   # 100 里有 40 来自 cache hit
            cache_write_tokens=10,  # 100 里有 10 是 cache 创建
            reasoning_tokens=30,    # 50 output 里有 30 是思考
        )
        assert b.input_tokens == 100
        assert b.output_tokens == 50
        assert b.cache_read_tokens == 40
        assert b.cache_write_tokens == 10
        assert b.reasoning_tokens == 30
        # 默认 total = 6 个分量相加(分立报表)
        assert b.total_tokens == 100 + 50 + 40 + 10 + 30

    def test_add_explicit_total_overrides_sum(self):
        b = UsageBucket()
        b.add(input_tokens=10, output_tokens=5, total_tokens=100)
        assert b.total_tokens == 100  # explicit overrides default sum


# --------------------------------------------------------------------------
# TokenUsageStore — recording
# --------------------------------------------------------------------------
class TestStoreRecording:
    def test_record_basic(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        assert not s.is_empty()

    def test_record_same_agent_sums(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        s.record("data_agent", input_tokens=200, output_tokens=80)
        agent = s.by_agent()["data_agent"]
        assert agent["input_tokens"] == 300
        assert agent["output_tokens"] == 130
        assert agent["calls"] == 2

    def test_record_disjoint_agents(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        s.record("news_agent", input_tokens=80, output_tokens=40)
        agent = s.by_agent()
        assert agent["data_agent"]["input_tokens"] == 100
        assert agent["news_agent"]["input_tokens"] == 80
        assert s.totals()["calls"] == 2

    def test_record_surfaces_partition(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50, surface="ui")
        s.record("data_agent", input_tokens=10, output_tokens=5, surface="debug")
        # by_agent rolls up surfaces
        agent = s.by_agent()["data_agent"]
        assert agent["input_tokens"] == 110
        # by_surface partitions them
        surf = s.by_surface()
        assert surf["ui"]["input_tokens"] == 100
        assert surf["debug"]["input_tokens"] == 10

    def test_record_errored_counted_separately(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        s.record("data_agent", input_tokens=0, output_tokens=0, errored=True)
        agent = s.by_agent()["data_agent"]
        assert agent["calls"] == 2
        assert agent["errors"] == 1


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
class TestStoreViews:
    def test_totals_empty(self):
        s = TokenUsageStore()
        t = s.totals()
        assert t["input_tokens"] == 0
        assert t["calls"] == 0
        assert t["agents"] == 0
        assert t["surfaces"] == 0

    def test_totals_aggregate(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        s.record("news_agent", input_tokens=80, output_tokens=40)
        s.record("data_agent", input_tokens=20, output_tokens=10, surface="audit")
        t = s.totals()
        assert t["input_tokens"] == 200
        assert t["output_tokens"] == 100
        assert t["total_tokens"] == 300
        assert t["calls"] == 3
        assert t["agents"] == 2
        assert t["surfaces"] == 2  # ui + audit

    def test_summary_nested_shape(self):
        s = TokenUsageStore()
        s.record("data_agent", input_tokens=100, output_tokens=50)
        d = s.summary()
        assert "totals" in d
        assert "by_agent" in d
        assert "by_surface" in d
        assert d["by_agent"]["data_agent"]["input_tokens"] == 100
        assert d["by_surface"]["ui"]["input_tokens"] == 100

    def test_is_empty(self):
        s = TokenUsageStore()
        assert s.is_empty()
        s.record("a", input_tokens=1, output_tokens=1)
        assert not s.is_empty()


# --------------------------------------------------------------------------
# Context propagation — track_agent / track_surface / attach_store
# --------------------------------------------------------------------------
class TestContextManagers:
    def test_track_agent_sets_label(self):
        assert get_active_agent() == DEFAULT_AGENT
        with track_agent("data_agent"):
            assert get_active_agent() == "data_agent"
        # restored on exit
        assert get_active_agent() == DEFAULT_AGENT

    def test_track_agent_nested_restores(self):
        with track_agent("outer"):
            assert get_active_agent() == "outer"
            with track_agent("inner"):
                assert get_active_agent() == "inner"
            assert get_active_agent() == "outer"
        assert get_active_agent() == DEFAULT_AGENT

    def test_track_surface_sets_label(self):
        assert get_active_surface() == DEFAULT_SURFACE
        with track_surface("debug"):
            assert get_active_surface() == "debug"
        assert get_active_surface() == DEFAULT_SURFACE

    def test_attach_store_makes_active(self):
        assert get_active_store() is None
        s = TokenUsageStore()
        with attach_store(s) as bound:
            assert bound is s
            assert get_active_store() is s
        assert get_active_store() is None

    def test_attach_store_restores_after_exception(self):
        s = TokenUsageStore()
        try:
            with attach_store(s):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert get_active_store() is None


# --------------------------------------------------------------------------
# End-to-end: provider pattern — record through contextvars
# --------------------------------------------------------------------------
class TestProviderPattern:
    def test_simulate_provider_call_records_into_store(self):
        """Mirrors what OpenAICompatibleProvider.complete() should do."""
        from tradingagents.agent_harness.core.token_usage import get_active_agent

        s = TokenUsageStore()
        with attach_store(s):
            with track_agent("data_agent"):
                # Simulate provider reading context + recording
                agent = get_active_agent()
                store = get_active_store()
                assert store is s
                store.record(agent, input_tokens=100, output_tokens=50)
            with track_agent("news_agent"):
                store = get_active_store()
                store.record(get_active_agent(), input_tokens=80, output_tokens=40)

        d = s.summary()
        assert d["by_agent"]["data_agent"]["total_tokens"] == 150
        assert d["by_agent"]["news_agent"]["total_tokens"] == 120
        assert d["totals"]["calls"] == 2
        assert d["totals"]["total_tokens"] == 270



# --------------------------------------------------------------------------
# spec R5:disjoint 6 字段 在 store / by_agent / totals 层面要正确分流
# --------------------------------------------------------------------------
class TestR5DisjointAggregation:
    def test_by_agent_includes_6_fields(self):
        s = TokenUsageStore()
        s.record(
            "data_agent",
            input_tokens=100, output_tokens=50,
            cache_read_tokens=40, reasoning_tokens=10,
        )
        agent = s.by_agent()["data_agent"]
        assert set(agent.keys()) == {
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
            "total_tokens", "calls", "errors",
        }
        assert agent["cache_read_tokens"] == 40
        assert agent["reasoning_tokens"] == 10

    def test_by_surface_includes_6_fields(self):
        s = TokenUsageStore()
        with attach_store(s):
            with track_surface("ui"):
                s.record("orchestrator", input_tokens=10, output_tokens=5,
                         cache_write_tokens=3)
        surf = s.by_surface()["ui"]
        assert surf["cache_write_tokens"] == 3

    def test_totals_includes_cache_and_reasoning(self):
        s = TokenUsageStore()
        s.record("a", input_tokens=10, output_tokens=5, cache_read_tokens=4)
        s.record("b", output_tokens=5, reasoning_tokens=2)
        t = s.totals()
        assert t["cache_read_tokens"] == 4
        assert t["reasoning_tokens"] == 2
        assert t["input_tokens"] == 10
        assert t["output_tokens"] == 10  # 5 + 5
