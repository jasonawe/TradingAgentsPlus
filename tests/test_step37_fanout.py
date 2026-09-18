"""Step 37 — FanOut parallel node.

Workflow primitive extension for parallel execution. Used when a stage
needs to fan out into independent sub-tasks (e.g. fetch quote + news
+ fundamentals concurrently, then converge into synthesis).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Basic FanOut behavior
# ------------------------------------------------------------------
def test_fanout_runs_children_in_parallel():
    """Three children each sleeping 0.1s should finish in ~0.1s, not ~0.3s."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def child(state, name, sleep):
        await asyncio.sleep(sleep)
        return NodeResult(state, emit=[(name, {"sleep": sleep})])

    async def main():
        wf = Workflow(name="parallel")
        wf.add_fanout(FanOut("fetch_all", children=[
            Node("quote", lambda s: child(s, "quote", 0.1)),
            Node("news", lambda s: child(s, "news", 0.1)),
            Node("fund", lambda s: child(s, "fund", 0.1)),
        ]))
        wf.add_node(Node("synth", lambda s: child(s, "synth", 0.0)))
        wf.add_edge(Edge("fetch_all", "synth"))
        wf.add_edge(Edge("synth", "<end>"))
        wf.set_entry("fetch_all")
        t0 = time.monotonic()
        events = []
        async for ev, p in wf.run({}):
            events.append((ev, p))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.25, f"should be parallel: {elapsed:.2f}s"
        assert len(events) == 4  # 3 children + synth

    asyncio.run(main())


def test_fanout_emits_tagged_with_parent_and_child():
    """Each child's emit must include _fanout and _child metadata."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def child(state, name):
        return NodeResult(state, emit=[(f"{name}_ok", {"v": 1})])

    async def main():
        wf = Workflow(name="tag")
        wf.add_fanout(FanOut("fo", children=[
            Node("a", lambda s: child(s, "a")),
            Node("b", lambda s: child(s, "b")),
        ]))
        wf.add_edge(Edge("fo", "<end>"))
        wf.set_entry("fo")
        events = []
        async for ev, p in wf.run({}):
            events.append((ev, p))
        tags = {(ev, p.get("_child")) for ev, p in events}
        assert ("a_ok", "a") in tags
        assert ("b_ok", "b") in tags

    asyncio.run(main())


def test_fanout_state_results_dict():
    """Each child's NodeResult should be recorded under state[fan_out_results][child_id]."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def main():
        async def child(state, name):
            state["hits"] = state.get("hits", []) + [name]
            return NodeResult(state, emit=[(name, {})])

        wf = Workflow(name="rec")
        wf.add_fanout(FanOut("fo", children=[
            Node("a", lambda s: child(s, "a")),
            Node("b", lambda s: child(s, "b")),
        ]))
        wf.add_edge(Edge("fo", "<end>"))
        wf.set_entry("fo")
        state = {}
        async for _, _ in wf.run(state):
            pass
        assert state["hits"] == ["a", "b"]
        assert "fan_out_results" in state
        assert "a" in state["fan_out_results"]
        assert "b" in state["fan_out_results"]

    asyncio.run(main())


def test_fanout_can_be_entry_node():
    """FanOut as entry should start the workflow."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def main():
        async def only(s):
            return NodeResult(s, emit=[("done", {})])

        wf = Workflow(name="entry-fanout")
        wf.add_fanout(FanOut("start", children=[
            Node("only", only),
        ]))
        wf.set_entry("start")
        events = []
        async for ev, p in wf.run({}):
            events.append((ev, p))
        assert ("done", {"_fanout": "start", "_child": "only"}) in events

    asyncio.run(main())


def test_fanout_halt_short_circuits_workflow():
    """If a child halts, the workflow should stop, not continue to synth."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def main():
        async def halter(state):
            return NodeResult(state, emit=[("stop", {})], halt=True)
        async def normal(state):
            return NodeResult(state, emit=[("normal", {})])

        wf = Workflow(name="halt")
        wf.add_fanout(FanOut("fo", children=[
            Node("a", halter),
            Node("b", normal),
        ]))
        wf.add_node(Node("after", lambda s: NodeResult(s, emit=[("after", {})])))
        wf.add_edge(Edge("fo", "after"))
        wf.add_edge(Edge("after", "<end>"))
        wf.set_entry("fo")
        events = []
        async for ev, p in wf.run({}):
            events.append(ev)
        assert "after" not in events, "halt should prevent subsequent nodes"

    asyncio.run(main())


def test_fanout_error_in_child_surfaces_as_workflow_error():
    """A raising child should produce a workflow_error event but not kill siblings."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )

    async def main():
        async def bad(state):
            raise RuntimeError("oops")
        async def good(state):
            return NodeResult(state, emit=[("good", {})])

        wf = Workflow(name="err")
        wf.add_fanout(FanOut("fo", children=[
            Node("bad", bad),
            Node("good", good),
        ]))
        wf.add_edge(Edge("fo", "<end>"))
        wf.set_entry("fo")
        events = []
        async for ev, p in wf.run({}):
            events.append((ev, p))
        assert any(ev == "workflow_error" for ev, _ in events)
        # Sibling still emits
        assert any(ev == "good" for ev, _ in events)

    asyncio.run(main())


def test_duplicate_node_id_rejected():
    """A regular node and a fan-out cannot share the same id."""
    from tradingagents.agent_harness.core.workflow import (
        FanOut, Node, Workflow,
    )

    async def handler(s):
        return None

    wf = Workflow(name="dup")
    wf.add_node(Node("x", handler))
    try:
        wf.add_fanout(FanOut("x", children=[]))
        assert False, "should have raised"
    except ValueError as e:
        assert "duplicate" in str(e).lower()


def test_duplicate_child_id_rejected():
    """Children of a fan-out must have unique ids within the fan-out."""
    from tradingagents.agent_harness.core.workflow import (
        FanOut, Node, Workflow,
    )

    wf = Workflow(name="dup-child")
    try:
        wf.add_fanout(FanOut("fo", children=[
            Node("dup", lambda s: None),
            Node("dup", lambda s: None),
        ]))
        assert False, "should have raised"
    except ValueError as e:
        assert "duplicate" in str(e).lower()


def test_fanout_listed_in_introspection():
    """Workflow introspection should surface fan-out alongside regular nodes."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, Workflow,
    )

    async def main():
        async def child_handler(s):
            return NodeResult(s, emit=[])
        async def next_handler(s):
            return NodeResult(s, emit=[])

        wf = Workflow(name="intro")
        wf.add_fanout(FanOut("fo", children=[Node("c", child_handler)]))
        wf.add_node(Node("next", next_handler))
        wf.add_edge(Edge("fo", "next"))
        wf.add_edge(Edge("next", "<end>"))
        wf.set_entry("fo")
        assert wf.entry_node().id == "fo"
        # Executed path is recorded
        async for _, _ in wf.run({}):
            pass
        assert "fo" in wf.executed()
        assert "next" in wf.executed()

    asyncio.run(main())


# ------------------------------------------------------------------
# Integration: parallel_fetch_workflow factory
# ------------------------------------------------------------------
class _StubFetcher:
    """Fake data fetcher that sleeps to simulate latency."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)

    async def fetch_quote(self, symbol):
        await asyncio.sleep(0.05)
        if "quote" in self.fail_on:
            raise RuntimeError("quote provider down")
        return {"symbol": symbol, "price": 40.71}

    async def fetch_news(self, symbol, lookback_days=7):
        await asyncio.sleep(0.05)
        if "news" in self.fail_on:
            raise RuntimeError("news provider down")
        return {"symbol": symbol, "items": [], "lookback_days": lookback_days}

    async def fetch_fundamentals(self, symbol):
        await asyncio.sleep(0.05)
        if "fundamentals" in self.fail_on:
            raise RuntimeError("fundamentals provider down")
        return {"symbol": symbol, "pe": 6.2, "pb": 0.95}


def test_parallel_fetch_workflow_runs_all_three_in_parallel():
    from tradingagents.agent_harness.core.parallel_fetch_workflow import (
        build_parallel_fetch_workflow,
    )

    async def main():
        fetcher = _StubFetcher()
        wf = build_parallel_fetch_workflow(fetcher)
        t0 = time.monotonic()
        events = []
        async for ev, p in wf.run({"symbol": "600036.SS"}):
            events.append((ev, p))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.12, f"should be parallel: {elapsed:.2f}s"
        kinds = [ev for ev, _ in events]
        assert "quote_fetched" in kinds
        assert "news_fetched" in kinds
        assert "fundamentals_fetched" in kinds
        assert "aggregated" in kinds

    asyncio.run(main())


def test_parallel_fetch_workflow_continues_when_one_source_fails():
    """A failing source emits an *_fetch_error but siblings still complete."""
    from tradingagents.agent_harness.core.parallel_fetch_workflow import (
        build_parallel_fetch_workflow,
    )

    async def main():
        fetcher = _StubFetcher(fail_on=("news",))
        wf = build_parallel_fetch_workflow(fetcher)
        events = []
        async for ev, p in wf.run({"symbol": "X"}):
            events.append((ev, p))
        kinds = [ev for ev, _ in events]
        assert "quote_fetched" in kinds
        assert "fundamentals_fetched" in kinds
        assert "news_fetch_error" in kinds
        # Synthesize still runs.
        assert "aggregated" in kinds

    asyncio.run(main())


# ------------------------------------------------------------------
# FanOut visualization integration (Step 35 + 37)
# ------------------------------------------------------------------
def test_fanout_renders_in_dot_output():
    """FanOut shows as octagon with parallelogram children in DOT."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )
    from tradingagents.agent_harness.core.workflow_viz import to_dot

    async def child(s):
        return NodeResult(s, emit=[])

    wf = Workflow(name="viz-test")
    wf.add_fanout(FanOut("fo", children=[
        Node("c1", child),
        Node("c2", child),
    ]))
    wf.add_edge(Edge("fo", "<end>"))
    wf.set_entry("fo")
    dot = to_dot(wf)
    assert "shape=octagon" in dot
    assert "shape=parallelogram" in dot
    assert "label=\"parallel\"" in dot
    assert "fan_out (2 parallel children)" in dot


def test_fanout_renders_in_json_output():
    """FanOut surfaces children with parent metadata in JSON."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, FanOut, Node, NodeResult, Workflow,
    )
    from tradingagents.agent_harness.core.workflow_viz import to_json

    async def child(s):
        return NodeResult(s, emit=[])

    wf = Workflow(name="viz-json")
    wf.add_fanout(FanOut("parallel", children=[
        Node("a", child),
        Node("b", child),
    ]))
    wf.add_edge(Edge("parallel", "<end>"))
    wf.set_entry("parallel")
    j = to_json(wf)
    node_by_id = {n["id"]: n for n in j["nodes"]}
    assert node_by_id["parallel"]["kind"] == "fanout"
    assert node_by_id["parallel"]["children"] == ["a", "b"]
    assert node_by_id["a"]["kind"] == "child"
    assert node_by_id["a"]["parent"] == "parallel"
    assert node_by_id["b"]["parent"] == "parallel"
    parallel_edges = [e for e in j["edges"] if e["kind"] == "parallel"]
    assert len(parallel_edges) == 2
