"""§0.4.17 — SVG sparkline + 关键指标 + 折叠表格"""
import pytest
from tradingagents.agent_harness.renderers.history_sparkline import (
    render_history_card, render_sparkline_svg, infer_history_params,
)


def _series(n=20, start=100.0, step=1.5):
    return [
        {
            "timestamp": f"2026-09-{i + 1:02d}",
            "open": start + i * step,
            "high": start + i * step + 1,
            "low": start + i * step - 1,
            "close": start + i * step + 0.5,
            "volume": 1000 + i * 100,
        }
        for i in range(n)
    ]


def test_sparkline_empty_for_no_points():
    assert render_sparkline_svg([]) == ""
    assert render_sparkline_svg([100.0]) == ""


def test_sparkline_renders_svg_for_two_points():
    svg = render_sparkline_svg([100.0, 101.0])
    assert "<svg" in svg and "<polyline" in svg


def test_sparkline_contains_endpoint_circles():
    svg = render_sparkline_svg([100, 102, 104, 103])
    assert svg.count("<circle") == 2


def test_sparkline_color_green_for_up_red_for_down():
    up = render_sparkline_svg([100, 101, 102])
    down = render_sparkline_svg([102, 101, 100])
    assert "#10b981" in up
    assert "#ef4444" in down


def test_render_card_basic_shape():
    history = {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NMS",
        "currency": "USD",
        "interval": "1d",
        "candles": _series(20),
    }
    html = render_history_card(history)
    assert "history-card" in html
    assert "AAPL" in html
    assert "Apple Inc." in html
    assert "NMS" in html
    assert "1d" in html
    assert "<svg" in html
    assert "hc-metrics" in html


def test_render_card_extracts_candles_from_data_key():
    history = {
        "symbol": "600036.SS",
        "currency": "CNY",
        "interval": "1d",
        "data": {"candles": _series(15)},
    }
    html = render_history_card(history)
    assert "600036.SS" in html
    assert "<svg" in html
    assert "hc-table" in html


def test_render_card_empty():
    # §0.4.26 — empty history card now shows "暂无历史数据" + hint.
    html = render_history_card({"symbol": "X"})
    assert "暂无历史数据" in html
    assert "<svg" not in html


def test_render_card_table_shows_max_10_rows_and_remaining_in_details():
    html = render_history_card({
        "symbol": "TSLA",
        "currency": "USD",
        "candles": _series(30),
    })
    assert "<details" in html
    assert "展开剩余 20 条" in html


def test_render_card_handles_missing_fields():
    candles = [
        {"timestamp": "2026-09-01", "close": 100},
        {"timestamp": "2026-09-02", "close": 101},
    ]
    html = render_history_card({"symbol": "X", "candles": candles})
    assert "—" in html


@pytest.mark.parametrize("text,expected", [
    ("看一下 AAPL 最近 30 天走势", ("1d", 30)),
    ("3 个月走势", ("1d", 90)),
    ("日内分时", ("1h", 24)),
    ("周线", ("1wk", 5)),
    ("最近一周", ("1d", 7)),
    ("半年走势", ("1d", 180)),
    ("1 年走势", ("1wk", 52)),
    ("最近 7 天", ("1d", 7)),
    ("随便看看", ("1d", 20)),
])
def test_infer_history_params(text, expected):
    assert infer_history_params(text) == expected
