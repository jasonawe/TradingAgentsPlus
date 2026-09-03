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

  root.TradingAgentsQuotes = { fetch: fetchQuotes, invalidate };
})(window);
