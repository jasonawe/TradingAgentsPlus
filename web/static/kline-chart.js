// K-line (candlestick) chart for watchlist rows.
//
// Renders a pure-SVG candle chart on top of the watchlist row when
// the user expands it. No external dependencies; talks to the existing
// `/api/assets/{symbol}/candles` endpoint and degrades gracefully when
// the data source is unavailable.

(function () {
  "use strict";

  const COLORS = Object.freeze({
    up: "#1f7a3a",
    down: "#b03030",
    axis: "#666666",
    grid: "#e3e3e3",
    text: "#222222",
    muted: "#777777",
  });

  const INTERVALS = [
    { key: "1M", days: 30 },
    { key: "3M", days: 90 },
    { key: "6M", days: 180 },
    { key: "1Y", days: 365 },
  ];

  function isoDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function computeRange(days) {
    const end = new Date();
    const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
    return { start: isoDate(start), end: isoDate(end) };
  }

  function bucketInterval(days) {
    if (days <= 30) return "1d";
    if (days <= 180) return "1d";
    return "1d";
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function formatTick(value, locale) {
    if (!Number.isFinite(value)) return "—";
    const abs = Math.abs(value);
    let digits = 2;
    if (abs >= 1000) digits = 0;
    else if (abs >= 100) digits = 1;
    return value.toLocaleString(locale || "zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function formatDateLabel(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function computeBounds(candles) {
    let minLow = Infinity;
    let maxHigh = -Infinity;
    let maxVolume = 0;
    for (const candle of candles) {
      if (typeof candle.low === "number" && candle.low < minLow) minLow = candle.low;
      if (typeof candle.high === "number" && candle.high > maxHigh) maxHigh = candle.high;
      if (typeof candle.volume === "number" && candle.volume > maxVolume) maxVolume = candle.volume;
    }
    if (!Number.isFinite(minLow) || !Number.isFinite(maxHigh)) return null;
    if (minLow === maxHigh) {
      minLow -= 1;
      maxHigh += 1;
    }
    const span = maxHigh - minLow;
    return {
      minLow: minLow - span * 0.05,
      maxHigh: maxHigh + span * 0.05,
      maxVolume,
    };
  }

  function buildSvg(candles, opts) {
    const width = opts.width;
    const height = opts.height;
    const padding = { top: 16, right: 56, bottom: 28, left: 12 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const volumeHeight = Math.max(28, plotHeight * 0.18);
    const priceHeight = plotHeight - volumeHeight - 6;
    const bounds = computeBounds(candles);
    if (!bounds) {
      return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"></svg>`;
    }
    const stepX = candles.length > 0 ? plotWidth / candles.length : plotWidth;
    const candleWidth = Math.max(2, Math.min(14, stepX * 0.65));
    const xFor = (index) => padding.left + stepX * (index + 0.5);
    const priceRange = bounds.maxHigh - bounds.minLow;
    const yForPrice = (value) =>
      padding.top + priceHeight - ((value - bounds.minLow) / priceRange) * priceHeight;
    const yForVolume = (value) =>
      padding.top + priceHeight + 6 + volumeHeight - (value / (bounds.maxVolume || 1)) * volumeHeight;

    const wickLines = [];
    const bodies = [];
    const volumeBars = [];
    for (let index = 0; index < candles.length; index += 1) {
      const candle = candles[index];
      const x = xFor(index);
      const yHigh = yForPrice(candle.high);
      const yLow = yForPrice(candle.low);
      const yOpen = yForPrice(candle.open);
      const yClose = yForPrice(candle.close);
      const color = candle.close >= candle.open ? COLORS.up : COLORS.down;
      wickLines.push(
        `<line x1="${x.toFixed(2)}" y1="${yHigh.toFixed(2)}" x2="${x.toFixed(2)}" y2="${yLow.toFixed(2)}" stroke="${color}" stroke-width="1" />`
      );
      const bodyTop = Math.min(yOpen, yClose);
      const bodyHeight = Math.max(1, Math.abs(yOpen - yClose));
      bodies.push(
        `<rect x="${(x - candleWidth / 2).toFixed(2)}" y="${bodyTop.toFixed(2)}" width="${candleWidth.toFixed(2)}" height="${bodyHeight.toFixed(2)}" fill="${color}" />`
      );
      if (typeof candle.volume === "number" && candle.volume > 0) {
        const vy = yForVolume(candle.volume);
        volumeBars.push(
          `<rect x="${(x - candleWidth / 2).toFixed(2)}" y="${vy.toFixed(2)}" width="${candleWidth.toFixed(2)}" height="${(padding.top + priceHeight + 6 + volumeHeight - vy).toFixed(2)}" fill="${color}" opacity="0.5" />`
        );
      }
    }

    const priceTicks = [bounds.maxHigh, (bounds.maxHigh + bounds.minLow) / 2, bounds.minLow]
      .map((value, idx) => {
        const y = yForPrice(value);
        return (
          `<line x1="${padding.left}" y1="${y.toFixed(2)}" x2="${(width - padding.right).toFixed(2)}" y2="${y.toFixed(2)}" stroke="${COLORS.grid}" stroke-dasharray="2 3" />` +
          `<text x="${(width - padding.right + 6).toFixed(2)}" y="${(y + 3).toFixed(2)}" fill="${COLORS.muted}" font-size="10" font-family="ui-monospace, monospace">${formatTick(value, opts.locale)}</text>`
        );
      })
      .join("");

    const xTickStep = Math.max(1, Math.floor(candles.length / 6));
    const xTicks = candles
      .map((candle, index) => ({ candle, index }))
      .filter(({ index }) => index % xTickStep === 0 || index === candles.length - 1)
      .map(({ candle, index }) => {
        const x = xFor(index);
        return `<text x="${x.toFixed(2)}" y="${(height - 8).toFixed(2)}" fill="${COLORS.muted}" font-size="10" font-family="ui-monospace, monospace" text-anchor="middle">${formatDateLabel(candle.timestamp)}</text>`;
      })
      .join("");

    return (
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${escapeAttr(opts.ariaLabel)}">` +
      `<rect x="0" y="0" width="${width}" height="${height}" fill="#ffffff" />` +
      priceTicks +
      wickLines.join("") +
      bodies.join("") +
      volumeBars.join("") +
      xTicks +
      `</svg>`
    );
  }

  function escapeAttr(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  async function fetchCandles(symbol, interval, start, end, assetType, fetcher) {
    const url = `/api/assets/${encodeURIComponent(symbol)}/candles?interval=${encodeURIComponent(interval)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&asset_type=${encodeURIComponent(assetType || "stock")}`;
    const response = await fetcher(url);
    if (!response.ok) {
      const err = new Error(`K线拉取失败 (${response.status})`);
      err.status = response.status;
      throw err;
    }
    const body = await response.json();
    return normalizeCandles(body);
  }

  function normalizeCandles(body) {
    const items = Array.isArray(body?.items) ? body.items : Array.isArray(body?.candles) ? body.candles : [];
    return items
      .filter((candle) => candle && typeof candle === "object")
      .map((candle) => ({
        timestamp: candle.time || candle.timestamp,
        open: numberOrNull(candle.open),
        high: numberOrNull(candle.high),
        low: numberOrNull(candle.low),
        close: numberOrNull(candle.close),
        volume: numberOrNull(candle.volume),
      }))
      .filter((candle) => candle.timestamp && candle.open != null && candle.high != null && candle.low != null && candle.close != null);
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  const api = {
    INTERVALS,
    COLORS,
    computeRange,
    bucketInterval,
    computeBounds,
    buildSvg,
    normalizeCandles,
    fetchCandles,
    formatTick,
    formatDateLabel,
  };
  if (typeof window !== "undefined") {
    window.KLineChart = api;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})();
