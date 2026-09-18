"""Step 44 — observability (tracing + metrics)."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Tracer + Span
# ------------------------------------------------------------------
def test_span_lifecycle_records_duration():
    from tradingagents.agent_harness.observability.tracing import Tracer

    t = Tracer("s1")
    s = t.start("plan")
    time.sleep(0.001)
    t.end(s, status="ok")
    finished = t.spans()
    assert len(finished) == 1
    assert finished[0].name == "plan"
    assert finished[0].status == "ok"
    assert finished[0].duration_ms() > 0


def test_span_attributes_merge():
    from tradingagents.agent_harness.observability.tracing import Tracer

    t = Tracer("s1")
    s = t.start("execute", attributes={"tool": "get_quote"})
    t.end(s, attributes={"tool": "get_news", "ok": True})
    sp = t.spans()[0]
    # later attributes overwrite earlier
    assert sp.attributes == {"tool": "get_news", "ok": True}


def test_span_parent_links():
    from tradingagents.agent_harness.observability.tracing import Tracer

    t = Tracer("s1")
    parent = t.start("turn")
    child = t.start("plan", parent_id=parent.span_id)
    t.end(child)
    t.end(parent)
    spans = t.spans()
    assert spans[0].parent_id == parent.span_id
    assert spans[1].parent_id is None


def test_tracer_ring_buffer_drops_oldest():
    from tradingagents.agent_harness.observability.tracing import Tracer

    t = Tracer("s1", max_spans=3)
    for i in range(5):
        s = t.start(f"op_{i}")
        t.end(s)
    spans = t.spans()
    assert len(spans) == 3
    assert [s.name for s in spans] == ["op_2", "op_3", "op_4"]


def test_tracer_to_dict_shape():
    from tradingagents.agent_harness.observability.tracing import Tracer

    t = Tracer("s1")
    s = t.start("plan", attributes={"intent": "read"})
    t.end(s)
    d = t.to_dict()
    assert d["session_id"] == "s1"
    assert "trace_id" in d
    assert d["span_count"] == 1
    assert d["spans"][0]["name"] == "plan"
    assert d["spans"][0]["duration_ms"] >= 0


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------
def test_counter_increments_monotonically():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    c = reg.counter("harness.tool_call.total")
    c.inc()
    c.inc()
    c.inc(5)
    assert c.value() == 7
    snap = reg.snapshot()
    assert snap["counters"]["harness.tool_call.total"] == 7


def test_gauge_set_and_get():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    g = reg.gauge("harness.queue.depth")
    g.set(5)
    assert g.value() == 5
    g.set(3)
    assert g.value() == 3


def test_histogram_observe_lands_in_correct_bucket():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    h = reg.histogram("harness.tool_call_ms")
    h.observe(2.0)
    h.observe(20.0)
    h.observe(200.0)
    snap = h.snapshot()
    assert snap["count"] == 3
    assert snap["sum"] == 222.0
    # Cumulative buckets — counts[i] = # observations where value <= b
    # default buckets: 1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, inf
    #   2.0  → in idx 1..11 (not <=1, but <=5..inf)
    #   20.0 → in idx 3..11 (not <=1,5,10 but <=25..inf)
    #   200.0→ in idx 6..11 (not <=1,5,10,25,50,100 but <=250..inf)
    # expected cumulative: [0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 3]
    assert snap["counts"] == [0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 3]


def test_histogram_with_custom_buckets():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    h = reg.histogram(
        "harness.custom_ms",
        buckets=(10.0, 100.0, 1000.0, float("inf")),
    )
    h.observe(50.0)
    h.observe(500.0)
    snap = h.snapshot()
    # Buckets [10, 100, 1000, inf]:
    #   50.0  → in idx 1..3 (not <=10 but <=100..inf)
    #   500.0 → in idx 2..3 (not <=10,100 but <=1000,inf)
    # expected cumulative: [0, 1, 2, 2]
    assert snap["counts"] == [0, 1, 2, 2]


def test_registry_reuses_same_instance():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    a = reg.counter("c1")
    b = reg.counter("c1")
    assert a is b


def test_registry_tracer_lifecycle():
    from tradingagents.agent_harness.observability.metrics_new import (
        MetricsRegistry,
    )

    reg = MetricsRegistry()
    t1 = reg.get_or_create_tracer("s1")
    t2 = reg.get_or_create_tracer("s1")
    # Same session -> same tracer
    assert t1 is t2
    # Different session -> different
    t3 = reg.get_or_create_tracer("s2")
    assert t3 is not t1
    assert reg.list_tracers() == ["s1", "s2"]
    assert reg.drop_tracer("s1") is True
    assert "s1" not in reg.list_tracers()
