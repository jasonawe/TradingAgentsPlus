"""P0 fix 专项验证 — mock QuoteService 验证 3 个 bug 修复。"""
import sys
sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

from datetime import datetime
from web.market_models import QuoteSnapshot, QuoteItem, BulkQuoteResponse

# Mock 返回值
mock_snap = QuoteSnapshot(
    symbol="600036.SS",
    price=38.5,
    change=0.5,
    change_percent=1.32,  # 字段名 change_percent(不是 change_pct)
    volume=12345678.0,
    source="akshare",
    fetched_at=datetime.utcnow(),
)

mock_items = [
    QuoteItem(symbol="600036.SS", price=38.5, change=0.5, change_percent=1.32,
              volume=12345678.0, source="akshare"),
    QuoteItem(symbol="AAPL", price=185.0, change=-1.2, change_percent=-0.65,
              volume=98765432.0, source="yfinance"),
]
mock_bulk = BulkQuoteResponse(items=mock_items, partial=False)

# Mock QuoteService
class MockQuoteService:
    def __init__(self, *args, **kwargs):
        self.snap = mock_snap
        self.bulk = mock_bulk
    def get_quote(self, symbol, asset_type="stock", **kwargs):
        return mock_snap
    def get_quotes(self, symbols, asset_type="stock", **kwargs):
        return mock_bulk

# Patch QuoteService
import tradingagents.agents.general.tools_bridge as tb
import web.market_data as md
md.QuoteService = MockQuoteService

# 重 import tools_bridge(确保使用 patched QuoteService)
import importlib
importlib.reload(tb)

print("=" * 60)
print("P0 fix 验证")
print("=" * 60)

# Test 1: get_quote 不再 AttributeError
print("\n[1] get_quote 字段访问...")
result1 = tb.get_quote.invoke({"symbol": "600036.SS"})
print(result1)
assert "change_percent: 1.3200" in result1, f"change_percent 字段输出错误: {result1!r}"
assert "ERROR" not in result1, f"get_quote 报错: {result1!r}"
print("  ✓ change_percent 字段正确输出,无 AttributeError")

# Test 2: get_quotes_batch 处理 BulkQuoteResponse
print("\n[2] get_quotes_batch BulkQuoteResponse...")
result2 = tb.get_quotes_batch.invoke({"symbols": "600036.SS,AAPL"})
print(result2)
assert "600036.SS" in result2 and "AAPL" in result2, "table 缺数据"
assert "change_percent" in result2, "table header 应是 change_percent"
assert "1.3200" in result2 and "-0.6500" in result2, f"数据缺失: {result2!r}"
print("  ✓ BulkQuoteResponse.items 正确解包,markdown 表格 OK")

# Test 3: get_history fallback 路径(模拟 fetch_daily_candles 失败,走 stockstats_utils)
print("\n[3] get_history fallback 路径...")
import unittest.mock as mock
# 强制 fetch_daily_candles 抛异常 → 走 fallback
with mock.patch("web.bar_generator.fetch_daily_candles", side_effect=Exception("mock fail")):
    result3 = tb.get_history.invoke({"symbol": "600036.SS", "period": "1y", "interval": "1d"})
print(result3[:300])
assert "ERROR" not in result3, f"get_history 报错: {result3[:200]!r}"
assert "fallback" in result3 or "stockstats_utils" in result3, "fallback 标记缺失"
print("  ✓ fallback 路径正确")

# Test 4: get_history B3 正常路径(用 fetch_daily_candles 的合法 period)
print("\n[4] get_history B3 路径(interval=1d)...")
with mock.patch("web.bar_generator.fetch_daily_candles") as mock_fetch:
    import pandas as pd
    mock_df = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=10),
        "Open": [38.0]*10, "High": [39.0]*10, "Low": [37.5]*10,
        "Close": [38.5]*10, "Volume": [1000000]*10,
    })
    mock_fetch.return_value = mock_df
    result4 = tb.get_history.invoke({"symbol": "600036.SS", "period": "1y", "interval": "1d"})
print(result4[:300])
assert "ERROR" not in result4
assert "B3" in result4, "应标注 B3 来源"
print("  ✓ B3 路径正常,interval=1d 走 B3")

print()
print("=" * 60)
print("✅ P0 全部 3 个 bug 修复验证通过")
print("=" * 60)
