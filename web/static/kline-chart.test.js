"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const KLineChart = require("./kline-chart.js");

function sampleCandles() {
  return [
    { timestamp: "2026-08-01", open: 100, high: 104, low: 98, close: 102, volume: 1000 },
    { timestamp: "2026-08-02", open: 102, high: 105, low: 100, close: 99, volume: 1200 },
    { timestamp: "2026-08-03", open: 99, high: 101, low: 95, close: 96, volume: 800 },
    { timestamp: "2026-08-04", open: 96, high: 98, low: 94, close: 97, volume: 600 },
  ];
}

test("computeRange returns ISO dates and end >= start", () => {
  const range = KLineChart.computeRange(30);
  assert.match(range.start, /^\d{4}-\d{2}-\d{2}$/);
  assert.match(range.end, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(range.start <= range.end);
});

test("computeRange scales with days", () => {
  const small = KLineChart.computeRange(7);
  const large = KLineChart.computeRange(365);
  const diff = (Date.parse(large.end) - Date.parse(large.start)) -
               (Date.parse(small.end) - Date.parse(small.start));
  assert.ok(diff >= 350 * 24 * 60 * 60 * 1000, "year range should be ~365 days longer than week range");
});

test("bucketInterval picks a daily bucket across supported windows", () => {
  for (const days of [30, 90, 180, 365]) {
    assert.equal(KLineChart.bucketInterval(days), "1d");
  }
});

test("computeBounds yields a padded window and tracks max volume", () => {
  const candles = sampleCandles();
  const bounds = KLineChart.computeBounds(candles);
  assert.ok(bounds.minLow < 94);
  assert.ok(bounds.maxHigh > 105);
  assert.equal(bounds.maxVolume, 1200);
});

test("computeBounds returns null for empty input", () => {
  assert.equal(KLineChart.computeBounds([]), null);
});

test("buildSvg renders wicks, bodies, volume bars, and gridlines", () => {
  const candles = sampleCandles();
  const svg = KLineChart.buildSvg(candles, {
    width: 480,
    height: 240,
    ariaLabel: "test chart",
    locale: "zh-CN",
  });
  assert.match(svg, /^<svg /);
  assert.match(svg, /<\/svg>$/);
  // 4 candles => 4 wick lines
  const wicks = svg.match(/<line /g) || [];
  assert.ok(wicks.length >= 7, "expected wick + grid lines");
  // 4 candle bodies
  const bodies = svg.match(/<rect /g) || [];
  assert.ok(bodies.length >= 8, "expected candle bodies + volume bars");
  // Volume bars use opacity
  assert.ok(svg.includes('opacity="0.5"'));
});

test("buildSvg picks red for down candles and green for up candles", () => {
  const candles = sampleCandles();
  const svg = KLineChart.buildSvg(candles, { width: 480, height: 240, ariaLabel: "t", locale: "en" });
  const greenCount = (svg.match(/#1f7a3a/g) || []).length;
  const redCount = (svg.match(/#b03030/g) || []).length;
  assert.ok(greenCount > 0 && redCount > 0);
});

test("buildSvg returns an empty svg when no candles are supplied", () => {
  const svg = KLineChart.buildSvg([], { width: 480, height: 240, ariaLabel: "empty", locale: "en" });
  assert.match(svg, /^<svg /);
  assert.doesNotMatch(svg, /<line /);
});

test("normalizeCandles maps both items and candles keys", () => {
  const a = KLineChart.normalizeCandles({
    items: [
      { time: "2026-08-01", open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 },
    ],
  });
  assert.equal(a.length, 1);
  assert.equal(a[0].timestamp, "2026-08-01");
  assert.equal(a[0].volume, 10);
  const b = KLineChart.normalizeCandles({
    candles: [
      { timestamp: "2026-08-02", open: 2, high: 3, low: 1.5, close: 2.5 },
    ],
  });
  assert.equal(b.length, 1);
  assert.equal(b[0].close, 2.5);
  assert.equal(b[0].volume, null);
});

test("normalizeCandles filters out malformed entries", () => {
  const out = KLineChart.normalizeCandles({
    items: [
      { timestamp: "2026-08-01", open: 1, high: 2, low: 0, close: 1.5 },
      { timestamp: "2026-08-02", open: 1, high: 2, low: 0 }, // missing close
      null,
      { time: "", open: 1, high: 2, low: 0, close: 1.5 }, // missing time
    ],
  });
  assert.equal(out.length, 1);
});

test("formatTick adapts precision to magnitude", () => {
  assert.match(KLineChart.formatTick(0, "en"), /0/);
  assert.match(KLineChart.formatTick(1234.5, "en"), /^1,23\d$/);
  assert.match(KLineChart.formatTick(99.5, "en"), /99/);
});

test("formatDateLabel returns YYYY-MM-DD", () => {
  assert.equal(KLineChart.formatDateLabel("2026-08-01"), "2026-08-01");
  assert.equal(KLineChart.formatDateLabel(""), "");
  assert.equal(KLineChart.formatDateLabel("not-a-date"), "not-a-date");
});

test("INTERVALS exposes the supported labels in order", () => {
  assert.deepEqual(
    KLineChart.INTERVALS.map((i) => i.key),
    ["1M", "3M", "6M", "1Y"]
  );
});
