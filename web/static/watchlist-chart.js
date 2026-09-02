// Watchlist chart integration: expands a watchlist row to show a K-line
// panel. Pairs with ``kline-chart.js`` for the SVG renderer.

(function () {
  "use strict";

  const DEFAULT_INTERVAL = "3M";

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function translator() {
    if (typeof window.t === "function") return window.t;
    return function (key, vars) {
      const fallback = (vars && vars.value) || key;
      return fallback;
    };
  }

  function localizeError(message) {
    const t = translator();
    if (typeof t !== "function") return message;
    const known = {
      "K线拉取失败 (404)": t("watchlist.chartNotFound"),
      "K线拉取失败 (422)": t("watchlist.chartInvalid"),
      "K线拉取失败 (502)": t("watchlist.chartUnavailable"),
    };
    return known[message] || t("watchlist.chartError", { value: message });
  }

  function panelTemplate(symbol, assetType, t) {
    const intervalButtons = window.KLineChart.INTERVALS.map((entry) => {
      const isActive = entry.key === DEFAULT_INTERVAL;
      return `<button type="button" class="chart-interval${isActive ? " is-active" : ""}" data-interval="${escapeHtml(entry.key)}" data-days="${entry.days}">${escapeHtml(entry.key)}</button>`;
    }).join("");
    return (
      `<div class="watchlist-chart" data-symbol="${escapeHtml(symbol)}" data-asset-type="${escapeHtml(assetType || "stock")}" data-state="idle" hidden>` +
        `<div class="watchlist-chart-head">` +
          `<span class="chart-title">${escapeHtml(t("watchlist.chartTitle", { value: symbol }))}</span>` +
          `<div class="chart-intervals" role="tablist">${intervalButtons}</div>` +
          `<button type="button" class="chart-close icon-button" aria-label="${escapeHtml(t("actions.remove"))}">×</button>` +
        `</div>` +
        `<div class="watchlist-chart-body" role="tabpanel">` +
          `<p class="chart-status">${escapeHtml(t("watchlist.chartLoading"))}</p>` +
        `</div>` +
      `</div>`
    );
  }

  function toggleRow(row, expand) {
    if (!row) return;
    const next = expand === undefined ? row.dataset.expanded !== "true" : expand;
    row.dataset.expanded = next ? "true" : "false";
    const button = row.querySelector("[data-toggle-chart]");
    if (button) {
      button.setAttribute("aria-expanded", next ? "true" : "false");
    }
  }

  function ensurePanel(row, symbol, assetType) {
    let panel = row.nextElementSibling;
    if (!panel || !panel.classList?.contains("watchlist-chart")) {
      const t = translator();
      const wrapper = document.createElement("div");
      wrapper.innerHTML = panelTemplate(symbol, assetType, t);
      panel = wrapper.firstElementChild;
      row.insertAdjacentElement("afterend", panel);
      wirePanel(panel);
    }
    return panel;
  }

  function setStatus(panel, kind, text) {
    const body = panel.querySelector(".watchlist-chart-body");
    if (!body) return;
    panel.dataset.state = kind;
    body.innerHTML = `<p class="chart-status chart-status-${escapeHtml(kind)}">${escapeHtml(text)}</p>`;
  }

  function renderChart(panel, candles, symbol, assetType) {
    const body = panel.querySelector(".watchlist-chart-body");
    if (!body) return;
    const rect = body.getBoundingClientRect();
    const width = Math.max(320, Math.floor(rect.width || 720));
    const height = 240;
    const ariaLabel = `${symbol} ${assetType} K 线`;
    const svg = window.KLineChart.buildSvg(candles, { width, height, ariaLabel, locale: "zh-CN" });
    const summary = summaryLine(candles);
    body.innerHTML =
      `<div class="chart-canvas">${svg}</div>` +
      `<p class="chart-summary">${escapeHtml(summary)}</p>`;
    panel.dataset.state = "ready";
  }

  function summaryLine(candles) {
    if (!candles.length) return "";
    const first = candles[0];
    const last = candles[candles.length - 1];
    const change = last.close - first.open;
    const pct = first.open ? (change / first.open) * 100 : 0;
    const direction = change >= 0 ? "+" : "";
    const t = translator();
    const period = `${formatDate(first.timestamp)} → ${formatDate(last.timestamp)}`;
    return t("watchlist.chartSummary", {
      value: `${period}  开 ${first.open.toFixed(2)}  收 ${last.close.toFixed(2)}  区间 ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`,
    });
  }

  function formatDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  async function loadPanel(panel, days) {
    const symbol = panel.dataset.symbol;
    const assetType = panel.dataset.assetType || "stock";
    const t = translator();
    setStatus(panel, "loading", t("watchlist.chartLoading"));
    try {
      const range = window.KLineChart.computeRange(days);
      const candles = await window.KLineChart.fetchCandles(
        symbol,
        window.KLineChart.bucketInterval(days),
        range.start,
        range.end,
        assetType,
        (url) => fetch(url, { credentials: "same-origin" })
      );
      if (!candles.length) {
        setStatus(panel, "empty", t("watchlist.chartEmpty"));
        return;
      }
      renderChart(panel, candles, symbol, assetType);
    } catch (error) {
      setStatus(panel, "error", localizeError(error?.message || String(error)));
    }
  }

  function wirePanel(panel) {
    panel.querySelectorAll("[data-interval]").forEach((button) => {
      button.addEventListener("click", () => {
        const days = Number(button.dataset.days);
        panel.querySelectorAll("[data-interval]").forEach((other) => other.classList.remove("is-active"));
        button.classList.add("is-active");
        loadPanel(panel, days);
      });
    });
    panel.querySelector(".chart-close")?.addEventListener("click", () => {
      const row = panel.previousElementSibling;
      if (row) toggleRow(row, false);
      panel.hidden = true;
    });
  }

  function expand(button) {
    const row = button.closest(".watchlist-row");
    if (!row) return;
    const symbol = button.dataset.toggleChart;
    const assetType = button.dataset.toggleChartAsset;
    const panel = ensurePanel(row, symbol, assetType);
    const isOpening = row.dataset.expanded !== "true";
    toggleRow(row, true);
    panel.hidden = false;
    if (isOpening || panel.dataset.state === "idle") {
      const days = Number(panel.querySelector("[data-interval].is-active")?.dataset.days || 90);
      loadPanel(panel, days);
    }
  }

  function attach(root) {
    const list = root || document;
    list.addEventListener("click", (event) => {
      const target = event.target.closest("[data-toggle-chart]");
      if (target) {
        event.preventDefault();
        expand(target);
      }
    });
  }

  window.WatchlistChart = { attach, expand, toggleRow };
})();
