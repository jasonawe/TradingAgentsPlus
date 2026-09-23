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
