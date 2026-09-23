"""§0.4.19 — get_history lookback_days wired through builtin + short_circuit."""
from tradingagents.agent_harness.tools.builtin import HistoryArgs
from tradingagents.agent_harness.core.short_circuit import ShortCircuit
from tradingagents.agent_harness.renderers.history_sparkline import infer_history_params


def test_history_args_accepts_lookback_days():
    a = HistoryArgs(symbol="AAPL", lookback_days=30)
    assert a.lookback_days == 30
    assert a.interval == "1d"


def test_infer_history_params_maps_chinese_phrases():
    assert infer_history_params("最近 30 天走势") == ("1d", 30)
    assert infer_history_params("3 个月") == ("1d", 90)
    assert infer_history_params("半年") == ("1d", 180)
    assert infer_history_params("1 年") == ("1wk", 52)
    assert infer_history_params("日内") == ("1h", 24)
    assert infer_history_params("随便看看") == ("1d", 20)


def test_infer_history_params_caps_lookback():
    # 1000 天 → cap at 365
    interval, lookback = infer_history_params("1000 天走势")
    assert interval == "1d"
    assert lookback == 365


def test_short_circuit_history_build_args_threads_lookback():
    """§0.4.19 — short_circuit._build_args should inject lookback_days
    into HistoryArgs when caller passes a Chinese phrase."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    args = ShortCircuit._build_args(
        HistoryArgs, "AAPL", slots={}, message="最近 30 天走势",
    )
    assert args.symbol == "AAPL"
    assert args.interval == "1d"
    assert args.lookback_days == 30


def test_short_circuit_history_build_args_default_when_no_message():
    args = ShortCircuit._build_args(HistoryArgs, "AAPL")
    assert args.lookback_days == 0
    assert args.interval == "1d"


def test_short_circuit_history_build_args_respects_explicit_slot():
    # If a slot already provides lookback_days, don't override it
    args = ShortCircuit._build_args(
        HistoryArgs, "AAPL",
        slots={"lookback_days": 7},
        message="最近 30 天走势",
    )
    # model_validate drops unknown keys for HistoryArgs? No, lookback_days
    # is now a declared field, so it flows through.
    assert args.lookback_days == 7


# §0.4.19.fix — provider needs datetime objects, not ISO strings.
from datetime import datetime, timezone


def test_get_history_passes_datetime_objects(monkeypatch):
    """§0.4.19.fix — when ``lookback_days > 0`` and start/end are empty,
    :func:`get_history` must hand datetime objects to the provider (not
    ISO strings) because yfinance's ``ticker.history(start=..., end=...)``
    only accepts date-like objects.
    """
    from tradingagents.agent_harness.tools import builtin as bi
    captured = {}

    class FakeFO:
        last_used_name = "fake"
        primary_name = "fake"
        def call(self, method, *args, **kwargs):
            captured["args"] = args
            return []

    monkeypatch.setattr(bi, "ProviderFailover", lambda primary=None: FakeFO())
    monkeypatch.setattr(bi, "get_active_provider_name", lambda: "fake")
    monkeypatch.setattr(bi, "_normalize_a_share_symbol", lambda s: s)

    async def run():
        args = bi.HistoryArgs(symbol="AAPL", interval="1d", lookback_days=30)
        await bi.get_history(args)

    import asyncio
    asyncio.run(run())

    a = captured["args"]
    # (symbol, interval, start, end)
    start, end = a[2], a[3]
    assert isinstance(start, datetime), f"start should be datetime, got {type(start).__name__}"
    assert isinstance(end, datetime), f"end should be datetime, got {type(end).__name__}"
    assert start.tzinfo is not None, "start must be timezone-aware"
    assert end.tzinfo is not None, "end must be timezone-aware"
    # End should be ~now, start should be ~30 days back (+50% padding)
    diff = (end - start).total_seconds()
    expected = 30 * 86400 * 1.5  # 30 days * 1.5 padding
    assert abs(diff - expected) < 86400, f"diff {diff} not close to {expected}"


def test_get_history_with_explicit_start_passes_through(monkeypatch):
    """When caller supplies ``start`` explicitly, lookback_days math is
    skipped and the caller's string is forwarded unchanged."""
    from tradingagents.agent_harness.tools import builtin as bi
    captured = {}

    class FakeFO:
        last_used_name = "fake"
        primary_name = "fake"
        def call(self, method, *args, **kwargs):
            captured["args"] = args
            return []

    monkeypatch.setattr(bi, "ProviderFailover", lambda primary=None: FakeFO())
    monkeypatch.setattr(bi, "get_active_provider_name", lambda: "fake")
    monkeypatch.setattr(bi, "_normalize_a_share_symbol", lambda s: s)

    async def run():
        args = bi.HistoryArgs(
            symbol="AAPL", interval="1d",
            start="2026-01-01", end="2026-02-01",
            lookback_days=30,  # ignored because start/end present
        )
        await bi.get_history(args)

    import asyncio
    asyncio.run(run())

    assert captured["args"][2] == "2026-01-01"
    assert captured["args"][3] == "2026-02-01"
