"""Step 37 — parallel fetch workflow example.

Demonstrates how to use ``FanOut`` to fetch multiple independent data
sources in parallel. Each child is a Node that emits a single
``*_fetched`` event with the source name + payload.

This is a reference implementation — the orchestrator still calls
``_execute`` for the production path. When the orchestrator gains
parallel tool fan-out (a follow-up step), it can either reuse this
factory or replace the demo handlers with real tool calls.
"""
from __future__ import annotations

from typing import Any

from .workflow import Edge, FanOut, Node, NodeResult, Workflow


def build_parallel_fetch_workflow(fetcher: Any) -> Workflow:
    """Compose a workflow that fetches quote + news + fundamentals in
    parallel and converges into a single synthesis.

    ``fetcher`` is a duck-typed object exposing async methods:

    - ``fetch_quote(symbol) -> dict``
    - ``fetch_news(symbol, lookback_days=7) -> dict``
    - ``fetch_fundamentals(symbol) -> dict``

    If a source fails, the child surfaces a ``*_fetch_error`` event and
    the workflow continues with whatever succeeded (no hard halt).
    """

    async def _quote(state: dict[str, Any]) -> NodeResult:
        symbol = state.get("symbol", "")
        try:
            data = await fetcher.fetch_quote(symbol)
        except Exception as exc:  # noqa: BLE001 — surface as data
            return NodeResult(state, emit=[("quote_fetch_error", {
                "symbol": symbol, "error": repr(exc),
            })])
        return NodeResult(state, emit=[("quote_fetched", data)])

    async def _news(state: dict[str, Any]) -> NodeResult:
        symbol = state.get("symbol", "")
        try:
            data = await fetcher.fetch_news(symbol, lookback_days=7)
        except Exception as exc:
            return NodeResult(state, emit=[("news_fetch_error", {
                "symbol": symbol, "error": repr(exc),
            })])
        return NodeResult(state, emit=[("news_fetched", data)])

    async def _fundamentals(state: dict[str, Any]) -> NodeResult:
        symbol = state.get("symbol", "")
        try:
            data = await fetcher.fetch_fundamentals(symbol)
        except Exception as exc:
            return NodeResult(state, emit=[("fundamentals_fetch_error", {
                "symbol": symbol, "error": repr(exc),
            })])
        return NodeResult(state, emit=[("fundamentals_fetched", data)])

    async def _synthesize(state: dict[str, Any]) -> NodeResult:
        results = state["fan_out_results"]
        # Aggregate into a single view the LLM can synthesize from.
        aggregated = {}
        for child_id, payload in results.items():
            if payload["emit"]:
                # Take the first emit as the canonical payload.
                aggregated[child_id] = payload["emit"][0][1]
        state["aggregated"] = aggregated
        return NodeResult(state, emit=[("aggregated", aggregated)], halt=True)

    wf = Workflow(name="parallel-fetch")
    wf.add_fanout(FanOut("fetch_all", children=[
        Node("quote", _quote),
        Node("news", _news),
        Node("fundamentals", _fundamentals),
    ]))
    wf.add_node(Node("synthesize", _synthesize))
    wf.add_edge(Edge("fetch_all", "synthesize"))
    wf.add_edge(Edge("synthesize", "<end>"))
    wf.set_entry("fetch_all")
    return wf
