"""W3-D3 E6: EventBus — 3-mode unified event bus."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.event_bus import EventBus, _StopPropagation


# ---------------------------------------------------------------------------
# Subscription validation
# ---------------------------------------------------------------------------


def test_subscribe_returns_handler_for_decorator_use() -> None:
    bus = EventBus()

    @bus.subscribe("evt", mode="emit")
    def handler(p):
        pass

    assert bus.handlers("evt") == [("emit", handler)]


def test_subscribe_rejects_invalid_mode() -> None:
    bus = EventBus()
    with pytest.raises(ValueError, match="unknown mode"):
        bus.subscribe("evt", lambda p: None, mode="bogus")


def test_subscribe_rejects_non_callable() -> None:
    bus = EventBus()
    with pytest.raises(TypeError, match="must be callable"):
        bus.subscribe("evt", "not_callable")  # type: ignore[arg-type]


def test_unsubscribe_returns_true_when_known() -> None:
    bus = EventBus()
    fn = lambda p: None  # noqa: E731
    bus.subscribe("evt", fn)
    assert bus.unsubscribe("evt", fn) is True
    assert bus.handlers("evt") == []


def test_unsubscribe_returns_false_when_unknown() -> None:
    bus = EventBus()
    assert bus.unsubscribe("evt", lambda p: None) is False


def test_clear_removes_all_handlers() -> None:
    bus = EventBus()
    bus.subscribe("a", lambda p: None)
    bus.subscribe("b", lambda p: None)
    bus.clear("a")
    assert "a" not in bus
    assert "b" in bus
    bus.clear()
    assert len(bus) == 0


# ---------------------------------------------------------------------------
# Container protocol
# ---------------------------------------------------------------------------


def test_contains_and_len() -> None:
    bus = EventBus()
    assert "evt" not in bus
    assert len(bus) == 0
    bus.subscribe("a", lambda p: None)
    bus.subscribe("a", lambda p: None, mode="waterfall")
    bus.subscribe("b", lambda p: None)
    assert "a" in bus and "b" in bus
    assert len(bus) == 3
    assert bus.list_events() == ["a", "b"]


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def test_emit_calls_all_emit_handlers_in_order() -> None:
    bus = EventBus()
    log: list[str] = []
    bus.subscribe("evt", lambda p: log.append(f"a:{p}"))
    bus.subscribe("evt", lambda p: log.append(f"b:{p}"))
    bus.emit("evt", "x")
    assert log == ["a:x", "b:x"]


def test_emit_skips_non_emit_modes() -> None:
    bus = EventBus()
    log: list[str] = []
    bus.subscribe("evt", lambda p: log.append("emit"), mode="emit")
    bus.subscribe("evt", lambda p: log.append("waterfall"), mode="waterfall")
    bus.subscribe("evt", lambda p: log.append("around"), mode="around")
    bus.emit("evt", None)
    assert log == ["emit"]


def test_emit_isolates_handler_exceptions() -> None:
    bus = EventBus()
    log: list[str] = []

    def boom(p):
        raise RuntimeError("nope")

    bus.subscribe("evt", boom)
    bus.subscribe("evt", lambda p: log.append("survived"))
    bus.emit("evt", None)  # must not raise
    assert log == ["survived"]


def test_emit_unknown_event_is_noop() -> None:
    bus = EventBus()
    bus.emit("never_subscribed", "payload")  # must not raise


# ---------------------------------------------------------------------------
# waterfall
# ---------------------------------------------------------------------------


def test_waterfall_chains_payload() -> None:
    bus = EventBus()
    bus.subscribe("evt", lambda p: p + 1, mode="waterfall")
    bus.subscribe("evt", lambda p: p * 2, mode="waterfall")
    bus.subscribe("evt", lambda p: p - 3, mode="waterfall")
    assert bus.waterfall("evt", 10) == ((10 + 1) * 2) - 3


def test_waterfall_none_return_keeps_payload() -> None:
    bus = EventBus()
    log: list[int] = []
    bus.subscribe("evt", lambda p: log.append(p) or (p + 1), mode="waterfall")
    bus.subscribe("evt", lambda p: log.append(p), mode="waterfall")  # returns None
    bus.subscribe("evt", lambda p: log.append(p) or (p * 10), mode="waterfall")
    out = bus.waterfall("evt", 5)
    # First adds 1 → 6; second None → 6; third times 10 → 60
    assert out == 60
    assert log == [5, 6, 6]


def test_waterfall_skips_non_waterfall_modes() -> None:
    bus = EventBus()
    bus.subscribe("evt", lambda p: p + 1, mode="waterfall")
    bus.subscribe("evt", lambda p: p * 100, mode="emit")
    assert bus.waterfall("evt", 1) == 2


def test_waterfall_stop_propagation_short_circuits() -> None:
    bus = EventBus()
    log: list[str] = []

    def stop(p):
        log.append(f"stop:{p}")
        raise _StopPropagation("HALT")

    def never(p):
        log.append(f"never:{p}")

    bus.subscribe("evt", lambda p: log.append(f"first:{p}"), mode="waterfall")
    bus.subscribe("evt", stop, mode="waterfall")
    bus.subscribe("evt", never, mode="waterfall")
    assert bus.waterfall("evt", 0) == "HALT"
    assert log == ["first:0", "stop:0"]


def test_waterfall_handler_exception_continues_chain() -> None:
    bus = EventBus()
    log: list[str] = []

    def boom(p):
        raise RuntimeError("nope")

    bus.subscribe("evt", boom, mode="waterfall")
    bus.subscribe("evt", lambda p: log.append(f"survived:{p}"), mode="waterfall")
    out = bus.waterfall("evt", "x")
    assert out == "x"  # boom broke, chain continued with last payload
    assert log == ["survived:x"]


# ---------------------------------------------------------------------------
# around
# ---------------------------------------------------------------------------


def test_around_runs_pre_then_fn_then_post() -> None:
    bus = EventBus()
    log: list[str] = []
    bus.subscribe("op.pre", lambda kw: {**kw, "x": kw.get("x", 0) + 1}, mode="waterfall")
    bus.subscribe("op.pre", lambda kw: {**kw, "y": kw.get("y", 0) * 2}, mode="waterfall")
    bus.subscribe("op.post", lambda d: {**d, "result": d["result"] * 100}, mode="waterfall")

    def fn(x=0, y=1):
        log.append(f"fn(x={x}, y={y})")
        return x + y

    out = bus.around("op", fn, x=10, y=5)
    assert out == (10 + 1 + 5 * 2) * 100
    assert log == ["fn(x=11, y=10)"]


def test_around_works_with_no_handlers() -> None:
    bus = EventBus()

    def fn(x):
        return x * 2

    assert bus.around("op", fn, x=5) == 10


def test_around_uses_post_default_when_no_handlers() -> None:
    bus = EventBus()
    bus.subscribe("op.pre", lambda kw: {**kw, "x": kw["x"] + 1}, mode="waterfall")

    def fn(x):
        return x * 2

    assert bus.around("op", fn, x=3) == 8  # (3+1)*2


# ---------------------------------------------------------------------------
# Harness integration
# ---------------------------------------------------------------------------


def test_harness_has_event_bus(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    assert isinstance(h.events, EventBus)
    # Bus is empty until something subscribes (e.g. a plugin).
    assert len(h.events) == 0

    # Plugins / 3rd-party code can subscribe to cross-cutting events.
    captured: list[str] = []
    h.events.subscribe("my.event", lambda p: captured.append(p))
    h.events.emit("my.event", "hello")
    assert captured == ["hello"]


def test_harness_event_bus_supports_all_three_modes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()

    # emit
    log: list[str] = []
    h.events.subscribe("e", lambda p: log.append(f"emit:{p}"), mode="emit")
    h.events.emit("e", "x")
    assert log == ["emit:x"]

    # waterfall
    h.events.subscribe("w", lambda p: p + 1, mode="waterfall")
    h.events.subscribe("w", lambda p: p * 2, mode="waterfall")
    assert h.events.waterfall("w", 1) == 4  # (1+1)*2

    # around
    def fn(x, y=0):
        return x + y

    h.events.subscribe("a.pre", lambda kw: {**kw, "y": kw.get("y", 0) + 10}, mode="waterfall")
    assert h.events.around("a", fn, x=1) == 11  # x=1, y=0+10
