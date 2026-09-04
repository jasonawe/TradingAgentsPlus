(function (root) {
  "use strict";

  const cache = new Map();
  const inflight = new Map();
  const TTL_MS = 30000;

  function keyOf(assetType, symbols) {
    return String(assetType || "stock") + ":" + [...new Set(symbols)].sort().join(",");
  }

  async function fetchQuotes(symbols, assetType) {
    const list = Array.isArray(symbols) ? symbols : String(symbols || "").split(",");
    const cleaned = list.map((s) => String(s).trim()).filter(Boolean);
    if (!cleaned.length) return { items: [] };
    const key = keyOf(assetType, cleaned);
    const now = Date.now();
    const cached = cache.get(key);
    if (cached && now - cached.ts < TTL_MS) return cached.data;
    const pending = inflight.get(key);
    if (pending) return pending;
    const url = "/api/quotes?symbols=" + encodeURIComponent(cleaned.join(",")) + "&asset_type=" + encodeURIComponent(assetType || "stock");
    const promise = (async () => {
      try {
        const res = await fetch(url, { headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error("quote fetch failed: " + res.status);
        const data = await res.json();
        cache.set(key, { data, ts: Date.now() });
        return data;
      } finally {
        inflight.delete(key);
      }
    })();
    inflight.set(key, promise);
    return promise;
  }

  function invalidate(symbols, assetType) {
    const list = Array.isArray(symbols) ? symbols : String(symbols || "").split(",");
    const key = keyOf(assetType, list);
    cache.delete(key);
  }

  function reset() {
    cache.clear();
    inflight.clear();
  }


  function identityFromQuote(quote) {
    if (!quote) return { nameZh: "", nameEn: "", exchangeZh: "", exchangeEn: "" };
    const pick = (...vals) => vals.find((v) => v && String(v).trim()) || "";
    return {
      nameZh: pick(quote.asset_name_zh, quote.name_zh),
      nameEn: pick(quote.asset_name, quote.name, quote.quote && quote.quote.raw_summary),
      exchangeZh: pick(quote.exchange_name_zh),
      exchangeEn: pick(quote.exchange, quote.quote && quote.quote.exchange),
    };
  }

  function formatAssetCell(symbol, assetType, quote, esc) {
    const safe = typeof esc === "function" ? esc : (v) => String(v ?? "");
    const id = identityFromQuote(quote);
    const lines = [];
    if (id.nameZh && id.exchangeZh) lines.push(`<small>${safe(id.nameZh)} · ${safe(id.exchangeZh)}</small>`);
    else if (id.nameZh) lines.push(`<small>${safe(id.nameZh)}</small>`);
    else if (id.exchangeZh) lines.push(`<small>${safe(id.exchangeZh)}</small>`);
    if (id.nameEn && id.exchangeEn) lines.push(`<small class="muted">${safe(id.nameEn)} · ${safe(id.exchangeEn)}</small>`);
    else if (id.nameEn) lines.push(`<small class="muted">${safe(id.nameEn)}</small>`);
    else if (id.exchangeEn) lines.push(`<small class="muted">${safe(id.exchangeEn)}</small>`);
    if (!lines.length) {
      const fallback = assetType === "crypto" ? "加密货币" : "股票";
      lines.push(`<small>${safe(fallback)}</small>`);
    }
    return `<strong>${safe(symbol)}</strong>${lines.join("")}`;
  }

  root.TradingAgentsQuotes = { fetch: fetchQuotes, invalidate, reset, formatAssetCell, identityFromQuote };
})(window);
