"""§0.4.22 — unified friendly cards for quote / fundamentals / news / alpha / ack.

All cards share the same root class (``*-card``) so frontend CSS can style
them with one rule. ``harness.js#renderMarkdown`` trusts any string
starting with ``<div class="quote-card"`` / ``fundamentals-card`` /
``news-card`` / ``alpha-card`` / ``ack-card`` and emits it as raw HTML.
"""
from __future__ import annotations
from html import escape


def _f(v):
    if v is None: return None
    try: return float(v)
    except (TypeError, ValueError): return None


def _fmt_price(v, cur=""):
    f = _f(v)
    if f is None: return "—"
    sym = "¥" if cur in ("", "CNY", "RMB") else f"{cur} "
    return f"{sym}{f:,.2f}"


def _fmt_pct(v):
    f = _f(v)
    if f is None: return "—"
    return f"{'+' if f >= 0 else ''}{f:.2f}%"


def _fmt_num(v):
    f = _f(v)
    if f is None: return "—"
    if abs(f) >= 1e8: return f"{f/1e8:.2f}亿"
    if abs(f) >= 1e4: return f"{f/1e4:.2f}万"
    return f"{f:,.0f}"


# ──────────────────────────────────────────────────────────────
# Quote card
# ──────────────────────────────────────────────────────────────

def render_quote_card(r: dict) -> str:
    sym = r.get("symbol", "?")
    name = r.get("name", "")
    exch = r.get("exchange", "")
    cur = r.get("currency", "")

    price = _f(r.get("price"))
    change = _f(r.get("change"))
    change_pct = _f(r.get("change_pct"))
    open_ = _f(r.get("open"))
    high = _f(r.get("high"))
    low = _f(r.get("low"))
    prev_close = _f(r.get("previous_close"))
    volume = _f(r.get("volume"))
    turnover = _f(r.get("turnover"))
    as_of = r.get("as_of", "")
    provider = r.get("provider", "")

    cls = "up" if (change_pct or 0) > 0 else ("down" if (change_pct or 0) < 0 else "flat")
    change_str = "—"
    if change is not None:
        sign = "+" if change >= 0 else "-"
        change_str = f"{sign}{_fmt_num(abs(change))}"

    head = (
        f'<div class="qc-head">'
        f'<span class="qc-icon">📈</span>'
        f'<span class="qc-symbol">{escape(sym)}</span>'
        f'<span class="qc-name">{escape(name)}</span>'
        f'<span class="qc-meta">· {escape(exch)} · {escape(cur)} · {escape(provider)}</span>'
        f'</div>'
    )

    metrics = (
        '<div class="qc-metrics">'
        f'<div class="m"><div class="m-k">最新</div><div class="m-v">{_fmt_price(price, cur)}</div></div>'
        f'<div class="m"><div class="m-k">涨跌</div><div class="m-v {cls}">{change_str}</div></div>'
        f'<div class="m"><div class="m-k">涨跌幅</div><div class="m-v {cls}">{_fmt_pct(change_pct)}</div></div>'
        f'<div class="m"><div class="m-k">开盘</div><div class="m-v">{_fmt_price(open_, cur)}</div></div>'
        f'<div class="m"><div class="m-k">昨收</div><div class="m-v">{_fmt_price(prev_close, cur)}</div></div>'
        f'<div class="m"><div class="m-k">最高</div><div class="m-v">{_fmt_price(high, cur)}</div></div>'
        f'<div class="m"><div class="m-k">最低</div><div class="m-v">{_fmt_price(low, cur)}</div></div>'
        f'<div class="m"><div class="m-k">成交量</div><div class="m-v">{_fmt_num(volume)}</div></div>'
        '</div>'
    )

    extra = []
    if turnover is not None:
        extra.append(f'<span class="qc-extra">成交额: {_fmt_num(turnover)}</span>')
    if as_of:
        extra.append(f'<span class="qc-extra">时间: {escape(str(as_of))}</span>')
    extras = f'<div class="qc-extras">{" · ".join(extra)}</div>' if extra else ""

    return f'<div class="quote-card">{head}{metrics}{extras}</div>'


# ──────────────────────────────────────────────────────────────
# Fundamentals card
# ──────────────────────────────────────────────────────────────

def render_fundamentals_card(r: dict) -> str:
    sym = r.get("symbol", "?")
    name = r.get("name", "")
    cur = r.get("currency", "")
    provider = r.get("provider", "")

    fields = [
        ("pe_ratio", "PE", lambda v: f"{v:.2f}" if v is not None else "—"),
        ("pb_ratio", "PB", lambda v: f"{v:.2f}" if v is not None else "—"),
        ("market_cap", "市值", lambda v: _fmt_num(v)),
        ("circulating_cap", "流通市值", lambda v: _fmt_num(v)),
        ("roe", "ROE", lambda v: f"{v:.2f}%" if v is not None else "—"),
        ("eps", "EPS", lambda v: f"{v:.2f}" if v is not None else "—"),
        ("dividend_yield", "股息率", lambda v: f"{v:.2f}%" if v is not None else "—"),
        ("fifty_two_week_high", "52周高", lambda v: _fmt_price(v, cur)),
        ("fifty_two_week_low", "52周低", lambda v: _fmt_price(v, cur)),
        ("revenue", "营收", lambda v: _fmt_num(v)),
        ("net_income", "净利", lambda v: _fmt_num(v)),
    ]

    head = (
        f'<div class="fc-head">'
        f'<span class="fc-icon">💼</span>'
        f'<span class="fc-symbol">{escape(sym)}</span>'
        f'<span class="fc-name">{escape(name)}</span>'
        f'<span class="fc-meta">· {escape(cur)} · {escape(provider)}</span>'
        f'</div>'
    )

    cells = []
    for key, label, fmt in fields:
        v = r.get(key)
        if v is None: continue
        cells.append(
            f'<div class="m"><div class="m-k">{escape(label)}</div><div class="m-v">{fmt(v)}</div></div>'
        )
    if not cells:
        cells.append('<div class="fc-empty">暂无基本面数据</div>')
    metrics = f'<div class="fc-metrics">{"".join(cells)}</div>'

    return f'<div class="fundamentals-card">{head}{metrics}</div>'


# ──────────────────────────────────────────────────────────────
# News card
# ──────────────────────────────────────────────────────────────

def render_news_card(r: dict) -> str:
    sym = r.get("symbol", "?")
    items = (r.get("items") or [])
    head = (
        f'<div class="nc-head">'
        f'<span class="nc-icon">📰</span>'
        f'<span class="nc-symbol">{escape(sym)}</span>'
        f'<span class="nc-meta">· {len(items)} 条</span>'
        f'</div>'
    )
    if not items:
        return f'<div class="news-card">{head}<div class="nc-empty">暂无新闻</div></div>'
    rows = []
    for it in items[:8]:
        title = escape(it.get("title", "(无标题)"))
        pub = escape(str(it.get("published_at", "")))
        url = escape(str(it.get("url", "")))
        link = (
            f'<a class="nc-link" href="{url}" target="_blank" rel="noopener">'
            f'{title}</a>' if url and url != "None" else f'<span>{title}</span>'
        )
        rows.append(
            f'<li class="nc-item">{link}'
            + (f'<span class="nc-date">{pub}</span>' if pub else "")
            + '</li>'
        )
    more = f'<li class="nc-more">…还有 {len(items) - 8} 条</li>' if len(items) > 8 else ""
    body = f'<ul class="nc-list">{"".join(rows)}{more}</ul>'
    return f'<div class="news-card">{head}{body}</div>'


# ──────────────────────────────────────────────────────────────
# Alpha card (factor list)
# ──────────────────────────────────────────────────────────────

def render_alpha_card(r: dict) -> str:
    sym = r.get("symbol", "?")
    factors = r.get("factors") or []
    head = (
        f'<div class="ac-head">'
        f'<span class="ac-icon">🔢</span>'
        f'<span class="ac-symbol">{escape(sym)}</span>'
        f'<span class="ac-meta">· {len(factors)} 个因子</span>'
        f'</div>'
    )
    if not factors:
        return f'<div class="alpha-card">{head}<div class="ac-empty">暂无因子</div></div>'
    chips = "".join(f'<span class="ac-chip">{escape(str(f))}</span>' for f in factors[:32])
    more = f'<span class="ac-more">…还有 {len(factors) - 32} 个</span>' if len(factors) > 32 else ""
    body = f'<div class="ac-chips">{chips}{more}</div>'
    return f'<div class="alpha-card">{head}{body}</div>'


# ──────────────────────────────────────────────────────────────
# Ack card (write tool results)
# ──────────────────────────────────────────────────────────────

_VERB_MAP = {
    "NOTE_CREATED": ("✅", "笔记已创建"),
    "NOTE_UPDATED": ("✅", "笔记已更新"),
    "NOTE_DELETED": ("🗑️", "笔记已删除"),
    "ALERT_CREATED": ("✅", "告警已创建"),
    "ALERT_UPDATED": ("✅", "告警已更新"),
    "ALERT_DELETED": ("🗑️", "告警已删除"),
    "WATCHLIST_ADDED": ("⭐", "已加入关注"),
    "WATCHLIST_REMOVED": ("☆", "已移除关注"),
    "ADDED": ("✅", "已加入"),
    "REMOVED": ("🗑️", "已移除"),
    "DUPLICATE": ("ℹ️", "已存在(未重复添加)"),
    "UPDATED": ("✅", "已更新"),
    "DELETED": ("🗑️", "已删除"),
    "CREATED": ("✅", "已创建"),
    "ok": ("✅", "ok"),
}


def render_ack_card(r: dict) -> str:
    status = r.get("status", "ok")
    raw = r.get("raw", "") or ""
    target_id = r.get("id", "")
    # Try to extract the verb from raw prefix (e.g. "NOTE_CREATED: ...")
    verb = ""
    if ":" in raw:
        verb = raw.split(":", 1)[0].strip()
    icon, label = _VERB_MAP.get(verb) or _VERB_MAP.get(status) or ("✅", str(status))

    head = (
        f'<div class="ack-head">'
        f'<span class="ack-icon">{icon}</span>'
        f'<span class="ack-label">{escape(label)}</span>'
        f'</div>'
    )
    extra = []
    if target_id:
        extra.append(f'<span class="ack-extra">id: {escape(target_id)}</span>')
    extras = f'<div class="ack-extras">{" · ".join(extra)}</div>' if extra else ""

    return f'<div class="ack-card">{head}{extras}</div>'
