"""§0.4.20 — multi-asset compare card."""
from tradingagents.agent_harness.renderers.compare_sparkline import render_compare_card


def test_compare_empty():
    html = render_compare_card([])
    assert "暂无对比数据" in html


def test_compare_skips_single_point():
    html = render_compare_card([{"symbol": "AAPL", "closes": [100]}])
    assert "暂无对比数据" in html


def test_compare_two_assets_basic():
    series = [
        {"symbol": "AAPL", "name": "Apple", "closes": [100, 102, 104, 103]},
        {"symbol": "NVDA", "name": "Nvidia", "closes": [50, 52, 55, 60]},
    ]
    html = render_compare_card(series)
    assert "compare-card" in html
    assert "AAPL" in html and "NVDA" in html
    assert "<svg" in html
    # Two polylines (one per asset), each with an endpoint marker.
    assert html.count("<polyline") == 2
    assert html.count("<circle") == 2
    assert "cc-legend" in html


def test_compare_rebases_to_100():
    series = [
        {"symbol": "A", "closes": [100, 110]},  # +10%
        {"symbol": "B", "closes": [50, 55]},     # +10%
    ]
    html = render_compare_card(series)
    assert "+10.00%" in html


def test_compare_color_assignment_distinct():
    series = [{"symbol": f"S{i}", "closes": [10 + i, 12 + i]} for i in range(4)]
    html = render_compare_card(series)
    # Should have 4 distinct colors from palette
    for c in ["#2563eb", "#10b981", "#f59e0b", "#ef4444"]:
        assert c in html
