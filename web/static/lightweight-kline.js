/**
 * Multi-period K-line (蜡烛图) for asset detail page.
 *
 * Uses TradingView Lightweight Charts via CDN (~50KB).
 * Periods: 1d / 1w / 1M (all markets), 1m / 5m / 15m / 30m / 60m (US only via yfinance).
 * Talks to GET /api/market/kline.
 */

(function () {
  "use strict";

  const PERIODS_DAILY = ["1d", "1w", "1M"];
  const PERIODS_INTRADAY = ["1m", "5m", "15m", "30m", "60m"];

  const PALETTE = {
    up: "#10b981",
    down: "#ef4444",
    ma20: "#f59e0b",
    ma60: "#a78bfa",
    text: "currentColor",
    grid: "rgba(255,255,255,0.06)",
    border: "rgba(255,255,255,0.08)",
  };

  let chartInstances = new WeakMap(); // element → chart

  function loadLightweightCharts() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
      script.onload = () => resolve(window.LightweightCharts);
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  function renderPeriodButtons(container, supported, currentPeriod) {
    container.innerHTML = "";
    supported.forEach((p) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "kline-interval" + (p === currentPeriod ? " is-active" : "");
      btn.dataset.period = p;
      btn.textContent = p;
      btn.addEventListener("click", () => {
        container.querySelectorAll(".kline-interval").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        const symbol = container.dataset.symbol;
        loadAndRender(container, symbol, p);
      });
      container.appendChild(btn);
    });
  }

  async function loadAndRender(target, symbol, period) {
    // 兼容两种调用:target 是 { chartEl, statusEl } 或 target 是 container(内部 querySelector)
    let chartEl, statusEl;
    if (target && target.chartEl) {
      chartEl = target.chartEl;
      statusEl = target.statusEl;
    } else if (target && typeof target.querySelector === "function") {
      chartEl = target.querySelector(".lwc-chart") || target;
      statusEl = target.querySelector(".lwc-status");
    }
    if (!chartEl) return;

    if (statusEl) statusEl.textContent = "加载中...";

    try {
      const resp = await fetch(`/api/market/kline?symbol=${encodeURIComponent(symbol)}&period=${period}&count=240`);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        if (statusEl) statusEl.textContent = `⚠️ ${err.detail || resp.statusText}`;
        return;
      }
      const data = await resp.json();
      renderChart(chartEl, data);
      if (statusEl) {
        const dateText = data.candles.length > 0
          ? `${new Date(data.candles[0].time * 1000).toISOString().slice(0, 10)} ~ ${new Date(data.candles[data.candles.length - 1].time * 1000).toISOString().slice(0, 10)}`
          : "无数据";
        statusEl.textContent = `${data.count} 根 K 线 · ${dateText}`;
      }
    } catch (e) {
      if (statusEl) statusEl.textContent = `⚠️ 加载失败: ${e.message}`;
    }
  }

  async function renderChart(chartEl, data) {
    const lib = await loadLightweightCharts();
    const isDark = document.documentElement.dataset.theme === "dark" ||
                   (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);

    // dispose old chart if any
    const old = chartInstances.get(chartEl);
    if (old) {
      old.remove();
      chartInstances.delete(chartEl);
    }

    chartEl.innerHTML = "";
    const innerChartEl = document.createElement("div");
    innerChartEl.style.width = "100%";
    innerChartEl.style.height = "400px";
    chartEl.appendChild(innerChartEl);

    const chart = lib.createChart(innerChartEl, {
      layout: {
        background: { type: lib.ColorType.Solid, color: "transparent" },
        textColor: isDark ? "#e6e9ef" : "#1f2430",
        fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif',
      },
      grid: {
        vertLines: { color: PALETTE.grid },
        horzLines: { color: PALETTE.grid },
      },
      rightPriceScale: { borderColor: PALETTE.border },
      timeScale: { borderColor: PALETTE.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      autoSize: true,
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: PALETTE.up,
      downColor: PALETTE.down,
      wickUpColor: PALETTE.up,
      wickDownColor: PALETTE.down,
      borderVisible: false,
    });
    candleSeries.setData(data.candles);

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    volumeSeries.setData(
      data.candles.map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open
          ? "rgba(16, 185, 129, 0.4)"
          : "rgba(239, 68, 68, 0.4)",
      }))
    );

    if (data.ma && data.ma.ma20 && data.ma.ma20.length > 0) {
      const ma20Series = chart.addLineSeries({
        color: PALETTE.ma20,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ma20Series.setData(data.ma.ma20);
    }
    if (data.ma && data.ma.ma60 && data.ma.ma60.length > 0) {
      const ma60Series = chart.addLineSeries({
        color: PALETTE.ma60,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ma60Series.setData(data.ma.ma60);
    }

    chart.timeScale().fitContent();
    chartInstances.set(chartEl, chart);
  }

  async function initKlinePanels() {
    const containers = document.querySelectorAll("[data-lwc-kline]");
    for (const container of containers) {
      const symbol = container.dataset.symbol;
      if (!symbol) continue;

      // 决定该 symbol 支持哪些周期(A股/港股/加密只支持日线,美股有全部)
      const isUs = !/\\.(SS|SZ|HK)$/i.test(symbol);
      const supported = isUs
        ? [...PERIODS_DAILY, ...PERIODS_INTRADAY]
        : PERIODS_DAILY;

      const buttonsEl = container.querySelector(".lwc-period-buttons");
      renderPeriodButtons(buttonsEl, supported, "1d");

      // 标 dataset 供 renderPeriodButtons 用
      buttonsEl.dataset.symbol = symbol;

      // 默认加载 1d
      await loadAndRender({ chartEl: container.querySelector(".lwc-chart") || container, statusEl: container.querySelector(".lwc-status") }, symbol, "1d");
    }
  }

  // expose
  window.LightweightKline = {
    init: initKlinePanels,
    load: loadAndRender,
  };

  // auto-init on DOMContentLoaded
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      // 给 app.js 一个 hook;SPA 切换到 /assets/:symbol 时由 app.js 调 init
    });
  }
})();
