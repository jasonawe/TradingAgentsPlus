# 2026-09-10 — BarGenerator Multi-Period K-Line Design

**Date:** 2026-09-10
**Stage:** B3 (Quant Research — K-Line)
**Status:** Active
**Depends on:** B1 alpha_factors (复用 load_ohlcv)

## Background

B1 gave us quantitative factors but no way to *see* price action. Analysts and
users still rely on third-party charting sites (TradingView, 同花顺) to see
multi-period K-line. This fragments the workflow: switch tabs to verify a
factor's claim against the actual price chart.

We need an integrated K-line viewer on the asset detail page that:

1. Renders OHLCV candles + volume + MA20/60 in one widget
2. Supports period switching (1d / 1w / 1M for all markets; 1m/5m/15m/30m/60m
   for US stocks via yfinance intraday)
3. Costs < 100ms on cache hit so it can sit alongside the alpha tab

## Goal

- **Multi-period K-line widget** on `/assets/:symbol` page as a new "K线" tab
  (alongside the "因子" tab added in B1)
- **Backend** `GET /api/market/kline?symbol=&period=&count=` returns OHLCV +
  MA20/60 in JSON
- **Frontend** uses TradingView Lightweight Charts (free, ~50KB) — no custom
  canvas code, to maintain

## Non-Goals

- Order book / depth / tick-level data
- Drawing tools (trendlines, Fibonacci, etc.) — out of MVP
- Multi-symbol comparison / overlay charts
- Historical minute-line data for A-shares (not available without paid data
  source — A-shares get only 1d/1w/1M; minute-line is US-only via yfinance)

## Period Coverage

| Period | A-shares / HK / Crypto | US stocks |
|--------|------------------------|----------|
| 1d | ✅ load_ohlcv (existing 5y) | ✅ same |
| 1w | ✅ synthetic from 1d | ✅ same |
| 1M | ✅ synthetic from 1d | ✅ same |
| 60m | ✗ | ✅ yfinance 60d history |
| 30m | ✗ | ✅ yfinance 60d history |
| 15m | ✗ | ✅ yfinance 60d history |
| 5m | ✗ | ✅ yfinance 60d history |
| 1m | ✗ | ✅ yfinance 7d history |

## Architecture

### Modules

```
web/
├── bar_generator.py       [NEW] BarGenerator 算法 + 周期 → 数据源映射
├── market_data.py         [MOD] 加 kline(symbol, period, count) 函数
├── app.py                 [MOD] 加 GET /api/market/kline 路由
└── static/
    ├── kline.js           [NEW] TradingView Lightweight Charts 集成
    └── index.html         [MOD] 加 K线 tab + CDN script + 缓存版本号
```

### Data Flow

```
Frontend "K线" tab on /assets/:symbol
  ↓
GET /api/market/kline?symbol=AAPL&period=1d&count=240
  ↓
market_data.kline(symbol, period, count)
  ↓
bar_generator.fetch_candles(period)
  ├── "1d" → load_ohlcv → cache 24h
  ├── "1w" → resample 1d weekly → cache 24h
  ├── "1M" → resample 1d monthly → cache 24h
  ├── "60m"/"30m"/"15m"/"5m" → yfinance intraday → cache 60s
  └── "1m" → yfinance 1m intraday → cache 60s
  ↓
add MA20 / MA60 columns
  ↓
JSON response
```

## Interface Contract

### `GET /api/market/kline`

```
Request:
  ?symbol=600036.SS   # ticker
  &period=1d           # 1d|1w|1M|1m|5m|15m|30m|60m
  &count=240           # K线数量,默认 240,范围 30-1000

Response 200:
{
    "symbol": "600036.SS",
    "period": "1d",
    "count": 240,
    "candles": [
        {"time": 1694006400, "open": 38.0, "high": 38.5, "low": 37.8, "close": 38.2, "volume": 12345678},
        ...
    ],
    "ma": {
        "ma20": [{"time": ..., "value": 37.5}, ...],
        "ma60": [...]
    },
    "fetched_at": "2026-09-10T..."
}

Error 400: {"detail": "period must be one of [1d, 1w, 1M, 1m, 5m, 15m, 30m, 60m]"}
Error 400: {"detail": "count must be 30-1000"}
Error 422: {"detail": "minute-line not supported for A-shares; use 1d/1w/1M"}
Error 503: {"detail": "yfinance rate-limited; retry in 60s"}
```

## Frontend

### `web/static/kline.js`

```js
const PERIODS = ['1d', '1w', '1M', '1m', '5m', '15m', '30m', '60m'];

export function renderKLineTab(symbol) {
    // 创建 Lightweight Charts 实例
    const chart = createChart(document.querySelector('#kline-chart'), {
        layout: { background: ..., textColor: ... },
        grid: { vertLines: ..., horzLines: ... },
        timeScale: { timeVisible: true, secondsVisible: false },
    });

    const candleSeries = chart.addCandlestickSeries({...});
    const volumeSeries = chart.addHistogramSeries({...});
    const ma20Series = chart.addLineSeries({...});
    const ma60Series = chart.addLineSeries({...});

    async function load(period) {
        const res = await fetch(`/api/market/kline?symbol=${symbol}&period=${period}&count=240`);
        const data = await res.json();
        candleSeries.setData(data.candles);
        volumeSeries.setData(data.candles.map(c => ({time: c.time, value: c.volume, color: c.close >= c.open ? '#10b981' : '#ef4444'})));
        ma20Series.setData(data.ma.ma20);
        ma60Series.setData(data.ma.ma60);
    }

    load('1d');

    // 周期切换器
    document.querySelectorAll('.period-btn').forEach(btn => {
        btn.onclick = () => load(btn.dataset.period);
    });
}
```

### Asset Detail Tab

```
/assets/600036.SS

[概览] [财务] [笔记] [告警] [因子] [📈 K线]  ← 新增

K线 tab 内容:
┌────────────────────────────────────────────┐
│ [1d] [1w] [1M]   (仅美股:[1m][5m][15m][30m][60m]) │
├────────────────────────────────────────────┤
│ 蜡烛 + 成交量(bottom 20%) + MA20 + MA60 │
│  │
│  (Lightweight Charts canvas)                │
│                                            │
├────────────────────────────────────────────┤
│ MA20: 38.45  MA60: 37.20  (上方统计) │
└────────────────────────────────────────────┘
```

## Testing Plan

### Unit Tests

| Test | 内容 |
|------|------|
| `test_resample_weekly` | daily → weekly: open=first, high=max, low=min, close=last |
| `test_resample_monthly` | daily → monthly: 同上逻辑 |
| `test_unsupported_period` | "2h" / "4d" 应抛 ValueError |
| `test_minute_period_a_share` | A 股 + "5m" 应返回 typed error |
| `test_minute_period_us_stock` | 美股 + "5m" 应返回 yfinance 数据 |
| `test_ma_calculation` | MA20 = close.rolling(20).mean() |
| `test_count_bounds` | count=0 / count=10000 应抛 ValueError |

### E2E (browser)

| Test | 内容 |
|------|------|
| /assets/600036.SS 切到 K线 tab | 显示 240 根日线蜡烛 |
| 切换 1w | 显示周线蜡烛 |
| 切换 1M | 显示月线蜡烛 |
| MA20/MA60 叠加可见 | 两条折线 |
| 成交量色:涨绿跌红 | 与蜡烛色一致 |

## Migration / Rollout

无需 DB migration。

部署:
1. merge `codex/quant-research-stage-b3` → main
2. 打 tag `v0.7.0`
3. README 加 K线功能说明
4. 服务无需重启(纯 Python + JS 模块加载)

## Risk & Open Questions

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | yfinance rate limit | 60s TTL + 单一 client retry |
| R2 | Lightweight Charts 体积大 | 用官方 CDN(50KB gzipped),可接受 |
| R3 | 用户没有 alpha_vantage key | yfinance 优先,alpha_vantage 备用 |
| R4 | 1m K 线只有 7 天历史 | count=240 在 1m 模式下会爆(每分钟 1 根,240 根 = 4 小时) |

## Success Criteria

- [ ] 单元测试 7+ 个全过
- [ ] /assets/600036.SS K线 tab 显示 240 根日线
- [ ] 周期切换 < 500ms (cache hit)
- [ ] 美股 AAPL 1m/5m/15m 显示正确
- [ ] merge 后打 v0.7.0 tag

## File Manifest

| File | Status | Lines (est) |
|------|--------|-------------|
| `web/bar_generator.py` | 📝 新增 | ~150 |
| `web/market_data.py` | 📝 修改 | +30 |
| `web/app.py` | 📝 修改 | +20 |
| `web/static/kline.js` | 📝 新增 | ~120 |
| `web/static/index.html` | 📝 修改 | +20 |
| `tests/test_bar_generator.py` | 📝 新增 | ~200 |
| `docs/superpowers/specs/2026-09-10-quant-research-bar-generator-design.md` | ✅ 本文件 | - |
