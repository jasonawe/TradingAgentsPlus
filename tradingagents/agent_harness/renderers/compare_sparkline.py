"""§0.4.20 — multi-asset compare card (multiple normalized polylines in one SVG)."""
from __future__ import annotations
from html import escape


_PALETTE = [
    "#2563eb", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#ec4899", "#14b8a6", "#0ea5e9",
]


def _f(v):
    if v is None: return None
    try: return float(v)
    except (TypeError, ValueError): return None


def _pct(v):
    f = _f(v)
    if f is None: return "—"
    return f"{'+' if f >= 0 else ''}{f:.2f}%"


def _render_polyline(closes, lo, hi, inner_w, inner_h, pad, color, marker=None, symbol=None):
    pts = []
    xy = []
    import json as _json
    for i, v in enumerate(closes):
        x = pad + (i / max(1, len(closes) - 1)) * inner_w
        y = pad + (1 - (v - lo) / (hi - lo or 1.0)) * inner_h
        pts.append(f"{x:.2f},{y:.2f}")
        xy.append({"x": round(x, 2), "y": round(y, 2), "v": v})
    if symbol:
        pts_json = _json.dumps(xy)
        data_attr = " data-series-points='" + pts_json + "' data-symbol=\"" + (symbol or "") + "\""
    else:
        data_attr = ""
    line = (
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" '
        f'data-series="{escape(color)}"{data_attr} '
        f'points="{" ".join(pts)}"/>'
    )
    if marker:
        lx, ly = pts[-1].split(",")
        line += f'<circle cx="{lx}" cy="{ly}" r="3" fill="{color}"/>'
    return line


def render_compare_card(series: list[dict], title: str = "📈 多资产对比") -> str:
    """series = [{symbol, name?, closes: [float, ...], ...}, ...]."""
    series = [s for s in series if s.get("closes") and len(s["closes"]) >= 2]
    if not series:
        return (
            f'<div class="compare-card empty">'
            f'<div class="cc-head">{escape(title)}</div>'
            f'<div class="cc-empty">暂无对比数据</div></div>'
        )

    # Normalize: rebased to 100 at first close so lines are comparable.
    rebased = []
    for s in series:
        c0 = s["closes"][0]
        if not c0: continue
        rebased.append({**s, "normalized": [c / c0 * 100 for c in s["closes"]]})

    width, height, pad = 560, 160, 12
    inner_w, inner_h = width - 2 * pad, height - 2 * pad

    all_vals = [v for s in rebased for v in s["normalized"]]
    lo, hi = min(all_vals), max(all_vals)

    lines = []
    legend_items = []
    for i, s in enumerate(rebased):
        color = _PALETTE[i % len(_PALETTE)]
        lines.append(_render_polyline(
            s["normalized"], lo, hi, inner_w, inner_h, pad, color, marker=True,
            symbol=s.get("symbol"),
        ))
        first = s["normalized"][0]
        last = s["normalized"][-1]
        chg = last - first
        chg_pct = chg / first * 100 if first else 0
        legend_items.append(
            f'<span class="lg">'
            f'<span class="swatch" style="background:{color}"></span>'
            f'<b>{escape(s.get("symbol", "?"))}</b> '
            f'<span class="{("up" if chg_pct >= 0 else "down")}">{_pct(chg_pct)}</span>'
            f'</span>'
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="多资产对比 sparkline">'
        f'{"".join(lines)}'
        f"</svg>"
    )

    head = f'<div class="cc-head">{escape(title)} ({len(rebased)})</div>'
    legend = f'<div class="cc-legend">{"".join(legend_items)}</div>'
    chart = f'<div class="cc-chart">{svg}<div class="cc-tooltip"></div></div>'

    rows = []
    for s in rebased:
        first = s["normalized"][0]
        last = s["normalized"][-1]
        chg_pct = ((last - first) / first * 100) if first else 0
        rows.append(
            f"<tr><td>{escape(s.get('symbol', '?'))}</td>"
            f"<td>{escape(s.get('name', ''))}</td>"
            f"<td>{len(s['closes'])}</td>"
            f"<td class='{('up' if chg_pct >= 0 else 'down')}'>{_pct(chg_pct)}</td></tr>"
        )
    th = "<thead><tr><th>代码</th><th>名称</th><th>数据点数</th><th>区间涨跌</th></tr></thead>"
    table = (
        f'<table class="cc-table">'
        f"{th}<tbody>{''.join(rows)}</tbody></table>"
    )

    return (
        f'<div class="compare-card">{head}{legend}{chart}{table}</div>'
    )
