"""Friendly renderer for get_history: SVG sparkline + key metrics + collapsible table."""
from __future__ import annotations
from html import escape
import re


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _price(v, cur=""):
    f = _f(v)
    if f is None:
        return "—"
    sym = "¥" if cur in ("", "CNY", "RMB") else f"{cur} "
    return f"{sym}{f:,.2f}"


def _pct(v):
    f = _f(v)
    if f is None:
        return "—"
    return f"{'+' if f >= 0 else ''}{f:.2f}%"


def _num(v):
    f = _f(v)
    if f is None:
        return "—"
    if abs(f) >= 1e8:
        return f"{f/1e8:.2f}亿"
    if abs(f) >= 1e4:
        return f"{f/1e4:.2f}万"
    return f"{f:,.0f}"


def render_sparkline_svg(closes, width=560, height=120, pad=8, stroke="#2563eb"):
    """Inline SVG line chart. Empty string if < 2 points."""
    vals = [v for v in (_f(c) for c in closes) if v is not None]
    if len(vals) < 2:
        return ""
    import json as _json
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1.0
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    pts = []
    xy = []  # §0.4.21 — store (x, y, value) for hover tooltip
    for i, v in enumerate(vals):
        x = pad + (i / (len(vals) - 1)) * inner_w
        y = pad + (1 - (v - lo) / rng) * inner_h
        pts.append(f"{x:.2f},{y:.2f}")
        xy.append({"x": round(x, 2), "y": round(y, 2), "v": v})
    color = "#10b981" if vals[-1] >= vals[0] else "#ef4444"
    fx, fy = pts[0].split(",")
    lx, ly = pts[-1].split(",")
    pts_json = _json.dumps(xy)
    data_attrs = (
        ' data-points=\'' + pts_json + '\''
        f' data-min="{lo}" data-max="{hi}"'
        f' data-stroke="{color}"'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="价格走势 sparkline"{data_attrs}>'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="transparent"/>'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(pts)}"/>'
        f'<circle cx="{fx}" cy="{fy}" r="2.5" fill="#94a3b8"/>'
        f'<circle cx="{lx}" cy="{ly}" r="3.5" fill="{color}"/>'
        f"</svg>"
    )


def _extract_candles(history):
    if isinstance(history, list):
        return history
    if not isinstance(history, dict):
        return []
    for k in ("candles", "data", "history", "result"):
        v = history.get(k)
        if isinstance(v, list) and v:
            return v
        if isinstance(v, dict):
            for kk in ("candles", "data"):
                vv = v.get(kk)
                if isinstance(vv, list) and vv:
                    return vv
    return []


def _derive_meta(history, candles):
    meta = {}
    if isinstance(history, dict):
        for k in ("symbol", "name", "exchange", "currency", "interval"):
            if k in history:
                meta[k] = history[k]
    if not meta.get("symbol") and candles:
        meta["symbol"] = candles[0].get("symbol", "")
    meta["interval"] = meta.get("interval") or "1d"
    meta.setdefault("currency", "")
    meta.setdefault("exchange", "")
    meta.setdefault("name", "")
    return meta


def render_history_card(history) -> str:
    """Friendly card: header + key metrics + SVG sparkline + collapsible table."""
    candles = _extract_candles(history)
    meta = _derive_meta(history, candles)
    cur = meta.get("currency", "")
    sym = escape(meta.get("symbol", "") or "—")
    name = escape(meta.get("name", ""))
    exch = escape(meta.get("exchange", ""))
    interval = escape(meta.get("interval", "1d"))

    if not candles:
        return (
            f'<div class="history-card empty">'
            f'<div class="hc-head">📊 {sym}</div>'
            f'<div class="hc-empty">暂无行情数据</div></div>'
        )

    closes_vals = [_f(c.get("close")) for c in candles]
    highs = [_f(c.get("high")) for c in candles]
    lows = [_f(c.get("low")) for c in candles]
    vols = [_f(c.get("volume")) for c in candles]
    closes_v = [v for v in closes_vals if v is not None]
    first_close = closes_v[0] if closes_v else None
    last_close = closes_v[-1] if closes_v else None
    high_v = max([v for v in highs if v is not None], default=None)
    low_v = min([v for v in lows if v is not None], default=None)
    vol_v = None
    if any(v is not None for v in vols):
        vol_v = 0.0
        for v in vols:
            if v is not None:
                vol_v += v

    change = (
        (last_close - first_close)
        if (first_close is not None and last_close is not None)
        else None
    )
    change_pct = (
        (change / first_close * 100) if (change is not None and first_close) else None
    )
    amp_pct = None
    if high_v is not None and low_v is not None and low_v:
        amp_pct = (high_v - low_v) / low_v * 100

    cls = "up" if (change_pct or 0) > 0 else ("down" if (change_pct or 0) < 0 else "flat")
    svg = render_sparkline_svg(closes_vals)

    head = (
        f'<div class="hc-head">'
        f'<span class="hc-icon">📊</span>'
        f'<span class="hc-symbol">{sym}</span>'
        f'<span class="hc-name">{name}</span>'
        f'<span class="hc-meta">· {exch} · {interval} · {len(candles)} 根</span>'
        f"</div>"
    )

    change_str = "—"
    if change is not None:
        sign = "+" if change >= 0 else "-"
        change_str = f"{sign}¥{abs(change):,.2f}"

    metrics = (
        f'<div class="hc-metrics">'
        f'<div class="m"><div class="m-k">最新</div><div class="m-v">{_price(last_close, cur)}</div></div>'
        f'<div class="m"><div class="m-k">起价</div><div class="m-v">{_price(first_close, cur)}</div></div>'
        f'<div class="m"><div class="m-k">涨跌</div><div class="m-v {cls}">{change_str}</div></div>'
        f'<div class="m"><div class="m-k">涨跌幅</div><div class="m-v {cls}">{_pct(change_pct)}</div></div>'
        f'<div class="m"><div class="m-k">期间高</div><div class="m-v">{_price(high_v, cur)}</div></div>'
        f'<div class="m"><div class="m-k">期间低</div><div class="m-v">{_price(low_v, cur)}</div></div>'
        f'<div class="m"><div class="m-k">振幅</div><div class="m-v">{_pct(amp_pct)}</div></div>'
        f'<div class="m"><div class="m-k">总成交</div><div class="m-v">{_num(vol_v)}</div></div>'
        f"</div>"
    )

    body = ""
    if svg:
        # §0.4.21 — tooltip element rendered next to the chart so a
        # delegated mouseover handler (see web/static/agent.js:
        # ``attachHcChartTooltip``) can position and populate it.
        body += f'<div class="hc-chart">{svg}<div class="hc-tooltip"></div></div>'

    preview_n = 10
    rows = []
    for c in candles[:preview_n]:
        rows.append(
            f"<tr><td>{escape(str(c.get('timestamp', '')))}</td>"
            f"<td>{_price(c.get('open'), cur)}</td>"
            f"<td>{_price(c.get('high'), cur)}</td>"
            f"<td>{_price(c.get('low'), cur)}</td>"
            f"<td>{_price(c.get('close'), cur)}</td>"
            f"<td>{_num(c.get('volume'))}</td></tr>"
        )
    th = "<thead><tr><th>时间</th><th>开</th><th>高</th><th>低</th><th>收</th><th>成交量</th></tr></thead>"
    table = (
        f'<div class="hc-table-wrap"><table class="hc-table">'
        f"{th}<tbody>{''.join(rows)}</tbody></table></div>"
    )
    body += table

    if len(candles) > preview_n:
        rest = candles[preview_n:]
        rest_rows = []
        for c in rest:
            rest_rows.append(
                f"<tr><td>{escape(str(c.get('timestamp', '')))}</td>"
                f"<td>{_price(c.get('open'), cur)}</td>"
                f"<td>{_price(c.get('high'), cur)}</td>"
                f"<td>{_price(c.get('low'), cur)}</td>"
                f"<td>{_price(c.get('close'), cur)}</td>"
                f"<td>{_num(c.get('volume'))}</td></tr>"
            )
        body += (
            f'<details class="hc-rest"><summary>展开剩余 {len(rest)} 条</summary>'
            f'<table class="hc-table">{th}<tbody>{"".join(rest_rows)}</tbody></table>'
            f"</details>"
        )

    return f'<div class="history-card">{head}{metrics}{body}</div>'


def infer_history_params(text: str):
    """Map user text to (interval, lookback_count)."""
    t = (text or "").lower()
    if any(k in t for k in ["日内", "分时"]):
        return "1h", 24
    if "1 小时" in t or "1小时" in t:
        return "1h", 24
    if "周线" in t or "5 周" in t or "5周" in t:
        return "1wk", 5
    if "周" in t:
        return "1d", 7
    if "半年" in t:
        return "1d", 180
    if "1 年" in t or "年线" in t:
        return "1wk", 52
    if "3 个月" in t or "季度" in t or "季线" in t:
        return "1d", 90
    if "1 个月" in t or "30 天" in t or "月线" in t:
        return "1d", 30
    if "天" in t:
        m = re.search(r"(\d+)\s*天", t)
        if m:
            n = max(1, min(int(m.group(1)), 365))
            return "1d", n
    return "1d", 20
