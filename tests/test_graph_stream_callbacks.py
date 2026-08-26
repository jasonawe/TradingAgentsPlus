"""Contract tests for streamed graph callbacks and cooperative cancellation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tradingagents.graph.propagation import PropagationCancelled
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _state(**overrides):
    state = {
        "messages": [],
        "company_of_interest": "ACME",
        "trade_date": "2026-08-26",
        "market_report": "market",
        "sentiment_report": "sentiment",
        "news_report": "news",
        "fundamentals_report": "fundamentals",
        "investment_debate_state": {},
        "trader_investment_plan": "plan",
        "risk_debate_state": {},
        "investment_plan": "investment",
        "final_trade_decision": "**Rating**: Buy\nBuy ACME.",
    }
    state.update(overrides)
    return state


class _FakeGraph:
    def __init__(self, chunks, invoked_state=None):
        self.chunks = list(chunks)
        self.invoked_state = invoked_state or _state()
        self.stream_calls = 0
        self.invoke_calls = 0

    def stream(self, state, **kwargs):
        self.stream_calls += 1
        yield from self.chunks

    def invoke(self, state, **kwargs):
        self.invoke_calls += 1
        return self.invoked_state


def _graph(*, chunks=(), invoked_state=None, debug=False, checkpoint_enabled=False):
    graph = object.__new__(TradingAgentsGraph)
    graph.debug = debug
    graph.config = {
        "checkpoint_enabled": checkpoint_enabled,
        "data_cache_dir": "/tmp/graph-stream-callback-tests",
        "results_dir": "/tmp/graph-stream-callback-tests",
    }
    graph.graph = _FakeGraph(chunks, invoked_state)
    graph.workflow = SimpleNamespace(compile=lambda **kwargs: graph.graph)
    graph.propagator = SimpleNamespace(
        create_initial_state=lambda *args, **kwargs: {},
        get_graph_args=lambda: {},
    )
    graph.memory_log = SimpleNamespace(
        get_past_context=lambda ticker: "",
        get_pending_entries=lambda: [],
        store_decision=lambda **kwargs: None,
    )
    graph.signal_processor = SimpleNamespace(process_signal=lambda signal: "Buy")
    graph.resolve_instrument_context = lambda ticker, asset_type: ""
    graph._resolve_pending_entries = lambda ticker: None
    graph._log_state = lambda trade_date, final_state: None
    graph.curr_state = None
    graph.ticker = None
    graph.log_states_dict = {}
    graph._checkpointer_ctx = None
    graph.selected_analysts = ("market",)
    graph._run_signature = lambda asset_type: "test"
    return graph


def test_on_chunk_receives_chunks_and_returns_final_state_and_signal():
    first = _state(messages=["first"], market_report="first")
    second = _state(messages=["second"], news_report="second")
    graph = _graph(chunks=(first, second))
    received = []

    result = TradingAgentsGraph.propagate(
        graph,
        "ACME",
        "2026-08-26",
        on_chunk=received.append,
    )

    assert received == [first, second]
    assert result == ({**first, **second}, "Buy")
    assert graph.graph.stream_calls == 1
    assert graph.graph.invoke_calls == 0


def test_callback_free_non_debug_uses_invoke():
    invoked = _state(final_trade_decision="**Rating**: Hold")
    graph = _graph(invoked_state=invoked)

    result = TradingAgentsGraph._run_graph(graph, "ACME", "2026-08-26")

    assert result == (invoked, "Buy")
    assert graph.graph.invoke_calls == 1
    assert graph.graph.stream_calls == 0


def test_callback_free_debug_streams_and_merges_chunks():
    first = _state(messages=["first"], market_report="first")
    second = _state(messages=["second"], news_report="second")
    graph = _graph(chunks=(first, second), debug=True)

    result = TradingAgentsGraph._run_graph(graph, "ACME", "2026-08-26")

    assert result == ({**first, **second}, "Buy")
    assert graph.graph.stream_calls == 1
    assert graph.graph.invoke_calls == 0


def test_should_cancel_only_forces_streaming():
    chunk = _state()
    graph = _graph(chunks=(chunk,))
    checks = iter((False, False))

    result = TradingAgentsGraph._run_graph(
        graph,
        "ACME",
        "2026-08-26",
        should_cancel=lambda: next(checks),
    )

    assert result == (chunk, "Buy")
    assert graph.graph.stream_calls == 1
    assert graph.graph.invoke_calls == 0


def test_preflight_cancellation_skips_pending_resolution():
    graph = _graph()
    graph._resolve_pending_entries = pytest.fail

    with pytest.raises(PropagationCancelled):
        TradingAgentsGraph.propagate(
            graph,
            "ACME",
            "2026-08-26",
            should_cancel=lambda: True,
        )


@pytest.mark.parametrize("cancel_at", ["before_chunk", "after_final_chunk"])
def test_cancellation_skips_all_completion_side_effects(cancel_at, monkeypatch):
    chunk = _state()
    graph = _graph(chunks=(chunk,), checkpoint_enabled=True)
    events = []
    graph._log_state = lambda *args: events.append("log")
    graph.memory_log.store_decision = lambda **kwargs: events.append("memory")
    graph.signal_processor.process_signal = lambda signal: events.append("signal")
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.clear_checkpoint",
        lambda *args: events.append("checkpoint"),
    )
    class _CheckpointContext:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.get_checkpointer",
        lambda *args: _CheckpointContext(),
    )

    checks = iter((False, True)) if cancel_at == "before_chunk" else iter((False, False, True))
    with pytest.raises(PropagationCancelled):
        TradingAgentsGraph.propagate(
            graph,
            "ACME",
            "2026-08-26",
            should_cancel=lambda: next(checks),
        )

    assert events == []
    assert graph.curr_state is None


def test_callback_exception_propagates():
    graph = _graph(chunks=(_state(),))
    error = RuntimeError("callback failed")

    def on_chunk(chunk):
        raise error

    with pytest.raises(RuntimeError) as raised:
        TradingAgentsGraph._run_graph(graph, "ACME", "2026-08-26", on_chunk=on_chunk)

    assert raised.value is error


def test_should_cancel_exception_propagates():
    graph = _graph(chunks=(_state(),))
    error = RuntimeError("cancel check failed")

    def should_cancel():
        raise error

    with pytest.raises(RuntimeError) as raised:
        TradingAgentsGraph._run_graph(
            graph,
            "ACME",
            "2026-08-26",
            should_cancel=should_cancel,
        )

    assert raised.value is error
