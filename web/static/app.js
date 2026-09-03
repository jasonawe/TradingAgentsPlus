(() => {
  "use strict";

  const i18n = window.TradingAgentsI18n;
  const PHASE_KEYS = ["analyst", "research", "trading", "risk", "portfolio"];
  const REPORT_GROUPS = [["report.analysts", "analysts", [["agent.market", "market"], ["agent.social", "sentiment"], ["agent.news", "news"], ["agent.fundamentals", "fundamentals"]]], ["report.research", "research", [["agent.bull", "bull"], ["agent.bear", "bear"], ["agent.manager", "manager"]]], ["report.trading", "trading", [["agent.trader", "trader"]]], ["report.risk", "risk", [["agent.aggressive", "aggressive"], ["agent.conservative", "conservative"], ["agent.neutral", "neutral"]]], ["report.portfolio", "portfolio", [["agent.portfolio", "decision"]]]];
  const AGENT_KEYS = { "Market Analyst": "agent.market", "Sentiment Analyst": "agent.social", "News Analyst": "agent.news", "Fundamentals Analyst": "agent.fundamentals", "Bull Researcher": "agent.bull", "Bear Researcher": "agent.bear", "Research Manager": "agent.manager", Trader: "agent.trader", "Aggressive Analyst": "agent.aggressive", "Conservative Analyst": "agent.conservative", "Neutral Analyst": "agent.neutral", "Portfolio Manager": "agent.portfolio" };
  const PHASE_NAME_KEYS = { "Analyst Team": "phase.analyst", "Research Team": "phase.research", "Trading Team": "phase.trading", "Risk Management": "phase.risk", "Portfolio Manager": "phase.portfolio" };
  const DYNAMIC_KEYS = { graph: "run.graphUpdate" };
  const state = { language: i18n.locale, runId: null, lastSeq: 0, seen: new Set(), source: null, reportId: null, runRecord: null, elapsedTimer: null, startedAt: null, config: null, archived: false, history: [], library: { items: [], page: 1, pageSize: 20, total: 0, hasNext: false, requestSeq: 0 }, watchlist: { version: 1, items: [], quotes: {}, loading: false }, filters: { search: "", asset: "", status: "", sort: "newest" }, phases: PHASE_KEYS.map((key) => ({ key, status: "pending" })) };
  const QUOTE_REFRESH_MS = 5000;
  let quoteRefreshController = null;
  const ACTIVE_RUN_KEY = "tradingagents-active-run";

  function pickActiveRun(runs) {
    if (!Array.isArray(runs) || runs.length === 0) return null;
    const sorted = [...runs].sort((a, b) => new Date(b.queued_at || 0) - new Date(a.queued_at || 0));
    return sorted[0] || null;
  }
  const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "publishing"]);
  const $ = (id) => document.getElementById(id);
  const setupView = $("setup-view"), analysisView = $("analysis-view"), activeView = $("active-view"), scheduledView = $("scheduled-view"), reportView = $("report-view"), libraryView = $("library-view"), settingsView = $("settings-view"), assetView = $("asset-detail-view"), form = $("analysis-form");
  function t(key, vars = {}) { return i18n.t(key, vars); }
  function translateDynamic(value) { return t(AGENT_KEYS[value] || PHASE_NAME_KEYS[value] || DYNAMIC_KEYS[value] || value); }
  function languageLabel(value) { return i18n.label("language", value, value); }
  function formatRating(value) { return window.formatInvestmentRating(value, state.language); }
  function applyTranslations() { document.documentElement.lang = i18n.locale; document.title = t("app.title"); document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); }); document.querySelectorAll("[data-i18n-html]").forEach((node) => { node.innerHTML = t(node.dataset.i18nHtml); }); document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); }); if (state.config) { renderFormOptions(state.config); toggleCryptoAnalyst(); } renderPhases(); renderHistory(state.history); if (!libraryView.hidden) renderLibrary(); if (state.runRecord) renderRunHeader(state.runRecord); updateTickerHint(); }
  function updateTickerHint() { const select = $("watchlist-asset-type"); const hint = $("watchlist-ticker-hint"); if (!select || !hint) return; const key = select.value === "crypto" ? "form.tickerHint.crypto" : "form.tickerHint.stock"; hint.dataset.i18nHtml = key; hint.innerHTML = t(key); }
  function setConnection(key) { const node = $("connection-status"); node.textContent = t(`connection.${key}`); node.className = `connection-dot is-${key === "live" || key === "complete" ? "live" : key === "ready" ? "idle" : "error"}`; }
  function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c])); }
  function renderReportMarkdown(html, markdown) { return html || (markdown ? `<p>${escapeHtml(markdown).replace(/\r?\n/g, "<br />")}</p>` : ""); }
  function wrapReportTables(container) { if (!container) return; container.querySelectorAll("table").forEach((table) => { if (table.parentElement?.classList.contains("table-wrap")) return; const wrapper = document.createElement("div"); wrapper.className = "table-wrap"; table.replaceWith(wrapper); wrapper.appendChild(table); }); }
  function localizeError(message) { const text = String(message || ""); if (/already active/i.test(text)) return t("error.runConflict"); if (/run not found/i.test(text)) return t("error.runNotFound"); if (/report not found/i.test(text)) return t("error.reportNotFound"); if (/unsupported characters/i.test(text)) return t("error.unsupportedTicker"); if (/after asset filtering/i.test(text)) return t("error.effectiveAnalysts"); if (/analysis worker failed/i.test(text)) return t("error.failed"); return text || t("error.generic"); }
  function api(path, options) { return fetch(path, { headers: { Accept: "application/json", ...(options && options.headers) }, ...options }).then(async (response) => { const body = await response.json().catch(() => ({})); if (!response.ok) { if (response.status === 422) throw new Error(t("error.validation")); throw new Error(localizeError(body.detail)); } return body; }).catch((error) => { if (error instanceof TypeError) throw new Error(t("error.unavailable")); throw error; }); }
  function formatElapsed(startedAt) { const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000)); return t("run.elapsed", { time: `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` }); }
  function startElapsed(startedAt) { state.startedAt = startedAt || new Date().toISOString(); clearInterval(state.elapsedTimer); $("elapsed-time").textContent = formatElapsed(state.startedAt); state.elapsedTimer = setInterval(() => { if (state.startedAt) $("elapsed-time").textContent = formatElapsed(state.startedAt); }, 1000); }
  function stopElapsed() { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  function resetRunState() { stopElapsed(); state.lastSeq = 0; state.seen = new Set(); state.reportId = null; state.runRecord = null; state.startedAt = null; state.phases = PHASE_KEYS.map((key) => ({ key, status: "pending" })); $("activity-feed").innerHTML = ""; $("terminal-panel").hidden = true; $("report-panel").hidden = true; $("run-grid").hidden = false; $("progress-bar").style.width = "0%"; $("progress-label").textContent = "0%"; $("event-count").textContent = t("run.events", { count: 0 }); $("elapsed-time").textContent = t("run.elapsed", { time: "00:00" }); renderPhases(); }
  const ROUTES = { setup: "/", analysis: "/analysis", active: "/active", scheduled: "/scheduled", library: "/reports", settings: "/settings" };
  function normalizePath(pathname) { const value = String(pathname || "/").replace(/\/+$/, ""); return value || "/"; }
  function routePath(view, { reportId = null, symbol = null } = {}) { if (view === "report" && reportId) return `/reports/${encodeURIComponent(reportId)}`; if (view === "asset" && symbol) return `/assets/${encodeURIComponent(symbol)}`; return ROUTES[view] || ROUTES.setup; }
  function routeForPath(pathname) { const path = normalizePath(pathname); if (path === "/") return { view: "setup" }; for (const [view, route] of Object.entries(ROUTES)) if (view !== "setup" && path === route) return { view }; if (path.startsWith("/reports/")) { const reportId = decodeURIComponent(path.slice("/reports/".length)); return reportId ? { view: "report", reportId } : { view: "library" }; } if (path.startsWith("/assets/")) { const symbol = decodeURIComponent(path.slice("/assets/".length)); return symbol ? { view: "asset", symbol } : { view: "setup" }; } return null; }
  function setRoute(view, { reportId = null, symbol = null, replace = false } = {}) { const path = routePath(view, { reportId, symbol }); if (normalizePath(window.location.pathname) === normalizePath(path)) return; const method = replace ? "replaceState" : "pushState"; window.history[method]({ view, reportId, symbol }, "", path); }
  function switchView(view) { setupView.hidden = view !== "setup"; analysisView.hidden = view !== "analysis"; activeView.hidden = !["active", "run"].includes(view); scheduledView.hidden = view !== "scheduled"; reportView.hidden = view !== "report"; libraryView.hidden = view !== "library"; settingsView.hidden = view !== "settings"; assetView.hidden = view !== "asset"; const activeNav = view === "report" ? "library" : view === "asset" ? "setup" : view; document.querySelectorAll(".nav-primary a").forEach((link) => link.classList.toggle("is-active", link.dataset.view === activeNav)); updateTopbar(view); window.TradingAgentsScheduled?.setActive(view === "scheduled"); }
  function showSetup() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; state.archived = false; switchView("setup"); setConnection("ready"); loadWatchlist(); }
  function showAnalysis() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; state.archived = false; switchView("analysis"); setConnection("ready"); }
  async function showActive() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; state.archived = false; switchView("active"); setConnection("ready"); $("active-empty").hidden = true; $("run-header").hidden = true; $("run-grid").hidden = true; $("terminal-panel").hidden = true; try { const runs = (await api("/api/runs/active")).runs || []; const active = pickActiveRun(runs); if (active && ACTIVE_RUN_STATUSES.has(active.status)) { state.runId = active.run_id; resetRunState(); showRun(active); connectEvents(); } else { $("active-empty").hidden = false; } } catch (_) { $("active-empty").hidden = false; } }
  function showLibrary() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; switchView("library"); setConnection("ready"); renderLibrary(); loadLibraryPage(); }
  function showScheduled() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; state.archived = false; switchView("scheduled"); setConnection("ready"); }
  async function showSettings() { stopElapsed(); if (state.source) state.source.close(); state.source = null; state.runId = null; state.archived = false; switchView("settings"); setConnection("ready"); try { const [settings, providers] = await Promise.all([api("/api/settings"), api("/api/providers/market-data")]); const fields = settings.fields || {}; $("settings-fields").innerHTML = Object.entries(fields).map(([key, value]) => `<div><dt>${escapeHtml(t(`settings.${key}`))}</dt><dd>${escapeHtml(typeof value === "object" ? `${i18n.displayValue(value.value)}（${t("settings.source", { value: i18n.displayValue(value.source, t("settings.unknownSource")) })}）` : i18n.displayValue(value))}</dd></div>`).join(""); $("provider-status-list").innerHTML = (providers.providers || []).map((item) => `<div class="provider-status"><strong>${escapeHtml(item.label || item.id)}</strong><span class="status-chip ${item.status}">${escapeHtml(i18n.label("provider_status", item.status))}</span></div>`).join("") || `<p class="muted">${escapeHtml(t("settings.noProviders"))}</p>`; } catch (_) { $("settings-fields").innerHTML = `<p class="muted">${escapeHtml(t("settings.unavailable"))}</p>`; } }
  const ASSET_DETAIL_DAYS = { "30": "1M", "90": "3M", "180": "6M", "365": "1Y" };
  const ASSET_KLINE_DEFAULT_DAYS = 90;
  const ASSET_RUN_STATUS_KEYS = { queued: "status.queued", running: "status.running", publishing: "status.publishing", completed: "status.completed", failed: "status.failed", cancelled: "status.cancelled", interrupted: "status.interrupted", timed_out: "status.timed_out" };
  function openConfirmModal({ title, message, confirmText, cancelText, danger }) {
    return new Promise((resolve) => {
      const root = $("modal-root");
      if (!root) { resolve(window.confirm(message)); return; }
      const previousActive = document.activeElement;
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      const dialog = document.createElement("div");
      dialog.className = "modal-dialog";
      dialog.setAttribute("role", "alertdialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.innerHTML = `<h3 class="modal-title">${escapeHtml(title)}</h3><p class="modal-message">${escapeHtml(message)}</p><div class="modal-actions"><button type="button" class="button button-secondary modal-cancel">${escapeHtml(cancelText)}</button><button type="button" class="button ${danger ? "button-primary" : "button-primary"} modal-confirm">${escapeHtml(confirmText)}</button></div>`;
      root.innerHTML = "";
      root.appendChild(overlay);
      root.appendChild(dialog);
      root.classList.add("is-open");
      root.setAttribute("aria-hidden", "false");
      let resolved = false;
      const finish = (value) => {
        if (resolved) return;
        resolved = true;
        root.classList.remove("is-open");
        root.setAttribute("aria-hidden", "true");
        root.innerHTML = "";
        document.removeEventListener("keydown", onKey);
        if (previousActive && typeof previousActive.focus === "function") previousActive.focus();
        resolve(value);
      };
      const onKey = (event) => {
        if (event.key === "Escape") { event.preventDefault(); finish(false); }
        else if (event.key === "Enter") { event.preventDefault(); finish(true); }
      };
      dialog.querySelector(".modal-cancel").addEventListener("click", () => finish(false));
      dialog.querySelector(".modal-confirm").addEventListener("click", () => finish(true));
      overlay.addEventListener("click", () => finish(false));
      document.addEventListener("keydown", onKey);
      setTimeout(() => dialog.querySelector(".modal-confirm")?.focus(), 0);
    });
  }
  async function showAssetDetail(symbol) {
    stopElapsed();
    if (state.source) { state.source.close(); state.source = null; }
    state.runId = null;
    state.archived = false;
    switchView("asset");
    setConnection("ready");
    const watchlistItem = (state.watchlist.items || []).find((it) => (it.symbol || "").toUpperCase() === (symbol || "").toUpperCase());
    const assetType = watchlistItem?.asset_type || "stock";
    $("asset-detail-symbol").textContent = symbol;
    $("asset-detail-name").textContent = "—";
    $("asset-detail-asset-type").textContent = t(assetType === "crypto" ? "assets.crypto" : "assets.stock");
    $("asset-detail-price").textContent = t("watchlist.noQuote");
    $("asset-detail-change").textContent = "";
    $("asset-detail-change").className = "asset-change";
    $("asset-detail-quote-meta").textContent = "";
    $("asset-kline-chart").innerHTML = "";
    $("asset-kline-status").textContent = t("asset.kLineLoading");
    $("asset-reports-list").innerHTML = `<p class="muted">${escapeHtml(t("asset.reportsLoading"))}</p>`;
    $("asset-runs-list").innerHTML = `<p class="muted">${escapeHtml(t("asset.runsLoading"))}</p>`;
    const detailState = { symbol, assetType, days: ASSET_KLINE_DEFAULT_DAYS };
    state.assetDetail = detailState;
    const backBtn = $("asset-detail-back");
    if (backBtn) backBtn.onclick = () => navigate("setup");
    const runBtn = $("asset-detail-run");
    if (runBtn) runBtn.onclick = () => { $("ticker").value = detailState.symbol; $("asset-type").value = detailState.assetType; if (typeof toggleCryptoAnalyst === "function") toggleCryptoAnalyst(); navigate("analysis"); $("analysis-form").scrollIntoView({ behavior: "smooth", block: "start" }); };
    const intervals = $("asset-kline-intervals");
    if (intervals) {
      intervals.querySelectorAll("[data-asset-kline-days]").forEach((btn) => {
        btn.onclick = () => {
          const days = Number(btn.dataset.assetKlineDays);
          detailState.days = days;
          intervals.querySelectorAll("[data-asset-kline-days]").forEach((b) => b.classList.toggle("is-active", b === btn));
          renderAssetKline(detailState);
        };
      });
    }
    Promise.all([
      loadAssetIdentity(detailState),
      loadAssetQuote(detailState),
      renderAssetKline(detailState),
      loadAssetReports(detailState),
      loadAssetRuns(detailState),
    ]).catch(() => {});
  }
  async function loadAssetIdentity(detailState) {
    try {
      const identity = await api(`/api/assets/${encodeURIComponent(detailState.symbol)}/identity?asset_type=${encodeURIComponent(detailState.assetType)}`);
      const name = identity?.name || t("watchlist.noName");
      const exchange = identity?.exchange ? ` · ${identity.exchange}` : "";
      $("asset-detail-name").textContent = `${name}${exchange}`;
    } catch (_) {
      $("asset-detail-name").textContent = t("watchlist.noName");
    }
  }
  async function loadAssetQuote(detailState) {
    try {
      const response = await api(`/api/quotes?symbols=${encodeURIComponent(detailState.symbol)}&asset_type=${encodeURIComponent(detailState.assetType)}`);
      const quote = (response.items || [])[0] || {};
      if (quote.price == null) { $("asset-detail-price").textContent = t("watchlist.noQuote"); return; }
      const value = `${Number(quote.price).toLocaleString(undefined, { maximumFractionDigits: 6 })} ${escapeHtml(quote.currency || "")}`;
      $("asset-detail-price").textContent = value;
      if (quote.change_percent != null) {
        const pct = Number(quote.change_percent);
        const sign = pct >= 0 ? "+" : "";
        $("asset-detail-change").textContent = `${sign}${pct.toFixed(2)}%`;
        $("asset-detail-change").className = `asset-change ${pct >= 0 ? "quote-up" : "quote-down"}`;
      }
      const quoteTime = quote.quote_time || quote.as_of || quote.fetched_at;
      const source = quote.source ? `${escapeHtml(quote.source)}` : "";
      $("asset-detail-quote-meta").textContent = [source, quoteTime ? new Date(quoteTime).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "2-digit", day: "2-digit" }) : ""].filter(Boolean).join(" · ");
    } catch (_) {
      $("asset-detail-price").textContent = t("watchlist.noQuote");
    }
  }
  async function renderAssetKline(detailState) {
    const chart = $("asset-kline-chart");
    const status = $("asset-kline-status");
    if (!window.KLineChart || !chart) {
      if (status) status.textContent = t("watchlist.chartUnavailable");
      return;
    }
    if (status) status.textContent = t("watchlist.chartLoading");
    try {
      const items = await window.KLineChart.fetchCandles(detailState.symbol, "1d", window.KLineChart.computeRange(detailState.days).start, window.KLineChart.computeRange(detailState.days).end, detailState.assetType, (url) => fetch(url, { credentials: "same-origin" }));
      if (!items.length) { if (status) status.textContent = t("watchlist.chartEmpty"); chart.innerHTML = ""; return; }
      chart.innerHTML = window.KLineChart.buildSvg(items, { width: chart.clientWidth || 720, height: 320 });
      const bounds = window.KLineChart.computeBounds(items);
      const summary = bounds ? `${t("asset.kLineRange", { from: window.KLineChart.formatDateLabel(items[0].time), to: window.KLineChart.formatDateLabel(items[items.length - 1].time) })} · ${t("asset.kLinePoints", { value: items.length })}` : "";
      if (status) status.textContent = summary;
    } catch (error) {
      if (status) status.textContent = t("watchlist.chartError", { value: localizeError(error.message) });
    }
  }
  async function loadAssetReports(detailState) {
    const list = $("asset-reports-list");
    try {
      const response = await api(`/api/history?ticker=${encodeURIComponent(detailState.symbol)}&page_size=10`);
      const items = (response.items || response || []).slice(0, 10);
      if (!items.length) { list.innerHTML = `<p class="muted">${escapeHtml(t("asset.reportsEmpty"))}</p>`; return; }
      list.innerHTML = items.map((record) => historyItem(record, false)).join("");
      bindReportLinks(list);
    } catch (_) {
      list.innerHTML = `<p class="muted">${escapeHtml(t("asset.reportsError"))}</p>`;
    }
  }
  function assetRunStatusLabel(record) {
    const statusKey = ASSET_RUN_STATUS_KEYS[record.status] || "status.report";
    if (record.status === "completed") {
      const signal = formatRating(record.signal);
      return signal || t(statusKey);
    }
    if ((record.status === "failed" || record.status === "timed_out") && record.failed_agent) {
      return `${t(statusKey)} · ${escapeHtml(record.failed_agent)}`;
    }
    return t(statusKey);
  }
  function assetRunItem(record) {
    const status = assetRunStatusLabel(record);
    const meta = [record.analysis_date, record.provider || "", record.research_depth ? `深度 ${record.research_depth}` : ""].filter(Boolean).join(" · ");
    const error = (record.status === "failed" || record.status === "timed_out") && record.error_message ? `<p class="asset-run-error">${escapeHtml(record.error_message)}</p>` : "";
    const actions = [];
    if (record.report_id) actions.push(`<button type="button" class="text-button" data-report-id="${escapeHtml(record.report_id)}">${escapeHtml(t("actions.viewReport"))}</button>`);
    if (record.retryable && (record.status === "failed" || record.status === "timed_out" || record.status === "interrupted")) {
      actions.push(`<button type="button" class="text-button asset-run-retry" data-retry-run="${escapeHtml(record.run_id)}">${escapeHtml(t("actions.retry"))}</button>`);
    }
    return `<article class="asset-run-row"><div class="asset-run-status">${escapeHtml(status)}</div><div class="asset-run-meta">${escapeHtml(meta)}</div>${error}<div class="asset-run-actions">${actions.join("")}</div></article>`;
  }
  async function loadAssetRuns(detailState) {
    const list = $("asset-runs-list");
    try {
      const response = await api(`/api/assets/${encodeURIComponent(detailState.symbol)}/runs?limit=10`);
      const items = response.items || [];
      if (!items.length) { list.innerHTML = `<p class="muted">${escapeHtml(t("asset.runsEmpty"))}</p>`; return; }
      list.innerHTML = items.map(assetRunItem).join("");
      bindReportLinks(list);
      list.querySelectorAll(".asset-run-retry").forEach((btn) => {
        btn.addEventListener("click", async (event) => {
          event.preventDefault();
          event.stopPropagation();
          btn.disabled = true;
          btn.textContent = t("error.retryStarting");
          try {
            const newRecord = await api(`/api/runs/${encodeURIComponent(btn.dataset.retryRun)}/retry`, { method: "POST" });
            state.runId = newRecord.run_id;
            state.archived = false;
            state.reportId = null;
            navigate("active");
          } catch (error) {
            btn.disabled = false;
            btn.textContent = t("actions.retry");
            $("watchlist-error").textContent = localizeError(error.message);
          }
        });
      });
    } catch (_) {
      list.innerHTML = `<p class="muted">${escapeHtml(t("asset.runsError"))}</p>`;
    }
  }
  function prepareReport(reportId) { const record = state.history.find((item) => item.report_id === reportId) || {}; resetRunState(); state.archived = true; state.reportId = reportId; state.runRecord = { request: { ticker: record.ticker || t("history.title"), analysis_date: record.analysis_date || "", asset_type: record.asset_type || "", research_depth: record.research_depth || "", provider: record.provider, quick_model: record.quick_model, deep_model: record.deep_model, output_language: record.output_language } }; $("cancel-run").hidden = true; switchView("report"); setConnection("ready"); }
  function renderRoute(route) { if (route.view === "report") { prepareReport(route.reportId); loadReport(route.reportId, { route: false }); return; } if (route.view === "asset" && route.symbol) { showAssetDetail(route.symbol); return; } if (route.view === "setup") showSetup(); else if (route.view === "analysis") showAnalysis(); else if (route.view === "active") showActive(); else if (route.view === "scheduled") showScheduled(); else if (route.view === "library") showLibrary(); else if (route.view === "settings") showSettings(); }
  function navigate(view, options = {}) { setRoute(view, options); renderRoute({ view, reportId: options.reportId, symbol: options.symbol }); }
  function applyRoute(pathname, { replaceUnknown = true } = {}) { const route = routeForPath(pathname); if (!route) { if (replaceUnknown) window.history.replaceState({ view: "setup" }, "", ROUTES.setup); renderRoute({ view: "setup" }); return; } renderRoute(route); }
  function renderRunHeader(record) { const request = record.request || {}; $("run-title").textContent = t("run.briefingTitle", { ticker: request.ticker || t("report.decisionReport") }); const asset = request.asset_type === "crypto" ? t("assets.crypto") : request.asset_type === "stock" ? t("assets.stock") : request.asset_type || ""; $("run-subtitle").textContent = [request.analysis_date, asset, request.research_depth ? `${t("form.researchDepth")} ${request.research_depth}` : ""].filter(Boolean).join(" · "); }
  function rememberActiveRun(record) { if (record?.run_id && ACTIVE_RUN_STATUSES.has(record.status)) localStorage.setItem(ACTIVE_RUN_KEY, record.run_id); else if (!record || record.run_id === localStorage.getItem(ACTIVE_RUN_KEY)) localStorage.removeItem(ACTIVE_RUN_KEY); }
  function syncRunSnapshot(record) { const percent = Math.round(Math.max(0, Math.min(1, Number(record?.progress) || 0)) * 100); $("progress-bar").style.width = `${percent}%`; $("progress-label").textContent = `${percent}%`; if (record?.phase) updatePhase(record.phase); if (record?.current_agent) $("current-agent").textContent = translateDynamic(record.current_agent); }
  function showRun(record) { state.runRecord = record; rememberActiveRun(record); switchView("active"); $("active-empty").hidden = true; $("run-grid").hidden = !ACTIVE_RUN_STATUSES.has(record.status); $("run-header").hidden = record.status === "completed"; renderRunHeader(record); syncRunSnapshot(record); if (!state.archived && ACTIVE_RUN_STATUSES.has(record.status)) startElapsed(record.started_at || new Date().toISOString()); }
  function renderPhases() { $("phase-timeline").innerHTML = state.phases.map((phase) => `<li class="is-${phase.status}"><span class="phase-dot"></span><div><span class="phase-name">${escapeHtml(t(`phase.${phase.key}`))}</span><span class="phase-status">${escapeHtml(t(`status.${phase.status}`))}</span></div></li>`).join(""); }
  function addActivity(title, summary, timestamp) { const feed = $("activity-feed"); const entry = document.createElement("article"); entry.className = "activity-entry"; entry.innerHTML = `<time>${escapeHtml(new Date(timestamp || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</time><strong>${escapeHtml(translateDynamic(title))}</strong><p>${escapeHtml(summary)}</p>`; feed.appendChild(entry); feed.scrollTop = feed.scrollHeight; $("event-count").textContent = t("run.events", { count: feed.children.length }); }
  function updatePhase(name) { const key = PHASE_NAME_KEYS[name]?.replace("phase.", "") || name; const index = PHASE_KEYS.indexOf(key); if (index < 0) return; state.phases = state.phases.map((phase, i) => ({ key: phase.key, status: i < index ? "done" : i === index ? "in_progress" : "pending" })); renderPhases(); }
  function processEvent(envelope) { if (!envelope || envelope.run_id !== state.runId) return; const payload = envelope.payload || {}; if (envelope.event === "run_snapshot") { if (payload.run) syncRecord(payload.run); state.lastSeq = Number(payload.snapshot_seq) || 0; state.seen = new Set(); return; } if (state.seen.has(envelope.seq)) return; state.seen.add(envelope.seq); state.lastSeq = Math.max(state.lastSeq, Number(envelope.seq) || 0); switch (envelope.event) { case "run_started": setConnection("live"); startElapsed(state.runRecord?.started_at || envelope.timestamp); addActivity("run.deskStarted", t("run.analystsAssigned", { count: payload.analysts?.length || 0 }), envelope.timestamp); break; case "phase_changed": updatePhase(payload.phase); addActivity(payload.phase, payload.status === "in_progress" ? t("run.phaseStarted") : t(`status.${payload.status}`), envelope.timestamp); break; case "agent_status": if (payload.status === "in_progress") $("current-agent").textContent = translateDynamic(payload.agent); addActivity(payload.agent, t(`status.${payload.status}`), envelope.timestamp); break; case "progress": { const percent = Math.round((Number(payload.progress) || 0) * 100); $("progress-bar").style.width = `${percent}%`; $("progress-label").textContent = `${percent}%`; if (payload.current_agent) $("current-agent").textContent = translateDynamic(payload.current_agent); break; } case "message": addActivity(t("run.fieldNotes"), payload.text || "", envelope.timestamp); break; case "activity": addActivity(payload.name || t("run.graphUpdate"), payload.summary || "", envelope.timestamp); break; case "run_completed": completeRun(payload); break; case "run_failed": terminalRun("failed", localizeError(payload.error_message) || t("error.failed"), payload.retryable); break; case "run_cancelled": terminalRun("cancelled", t("error.cancelled"), payload.retryable); break; case "run_interrupted": terminalRun("interrupted", t("error.interrupted"), payload.retryable); break; case "run_timed_out": terminalRun("timed_out", t("error.timedOut"), payload.retryable); break; default: break; } }
  function syncRecord(record) { state.runRecord = record; rememberActiveRun(record); syncRunSnapshot(record); if (record.started_at && !state.archived && ACTIVE_RUN_STATUSES.has(record.status)) startElapsed(record.started_at); if (record.status === "completed" && record.report_id) loadReport(record.report_id); else if (!ACTIVE_RUN_STATUSES.has(record.status)) terminalRun(record.status, record.status === "timed_out" ? t("error.timedOut") : record.error_message || t(`error.${record.status}`)); }
  function reconnectStatus() { if (!state.runId) return Promise.resolve(); return api(`/api/runs/${encodeURIComponent(state.runId)}`).then((record) => { syncRecord(record); return record; }).catch(() => null); }
  async function restoreActiveRun() { let record = null; const savedId = localStorage.getItem(ACTIVE_RUN_KEY); if (savedId) { try { record = await api(`/api/runs/${encodeURIComponent(savedId)}`); } catch (_) { localStorage.removeItem(ACTIVE_RUN_KEY); } } if (!record) { try { const _runs = (await api("/api/runs/active")).runs || []; record = pickActiveRun(_runs); } catch (_) {} } if (!record) return; if (!ACTIVE_RUN_STATUSES.has(record.status)) { rememberActiveRun(record); return; } const currentView = routeForPath(window.location.pathname)?.view; if (!currentView || currentView === "setup") setRoute("active", { replace: true }); if (currentView === "active" || !currentView) { resetRunState(); state.runId = record.run_id; state.archived = false; showRun(record); connectEvents(); } }
  function connectEvents() { if (!state.runId) return; if (state.source) state.source.close(); const url = `/api/runs/${encodeURIComponent(state.runId)}/events?after_seq=${state.lastSeq}`; const source = new EventSource(url); state.source = source; source.onopen = () => setConnection("live"); source.onmessage = (event) => { try { processEvent(JSON.parse(event.data)); } catch (_) {} }; ["run_snapshot", "run_started", "phase_changed", "agent_status", "progress", "message", "activity", "run_completed", "run_failed", "run_cancelled", "run_interrupted", "run_timed_out"].forEach((name) => source.addEventListener(name, (event) => { try { processEvent(JSON.parse(event.data)); } catch (_) {} })); source.onerror = () => { setConnection("reconnecting"); reconnectStatus().then((record) => { if (record && !ACTIVE_RUN_STATUSES.has(record.status)) { source.close(); state.source = null; return; } if (source.readyState === EventSource.CLOSED) { source.close(); setTimeout(connectEvents, 1000); } }); }; }
  function completeRun(payload) { rememberActiveRun({ run_id: state.runId, status: "completed" }); stopElapsed(); state.reportId = payload.report_id; $("cancel-run").hidden = true; $("new-analysis").hidden = false; $("progress-bar").style.width = "100%"; $("progress-label").textContent = "100%"; state.phases = state.phases.map((phase) => ({ ...phase, status: "done" })); renderPhases(); setConnection("complete"); const signal = formatRating(payload.signal); addActivity(t("run.briefingReady"), signal ? t("run.signal", { signal }) : t("run.reportGenerated"), Date.now()); loadReport(payload.report_id); api("/api/history").then(renderHistory).catch(() => {}); }
  function terminalRun(status, message, retryable) { const canRetry = retryable === true; rememberActiveRun({ run_id: state.runId, status }); stopElapsed(); $("cancel-run").hidden = true; $("new-analysis").hidden = false; $("run-grid").hidden = true; $("run-header").hidden = false; $("terminal-panel").hidden = false; const heading = escapeHtml(status === "failed" ? t("error.failed") : status === "interrupted" ? t("error.interrupted") : status === "timed_out" ? t("error.timedOut") : t("connection.cancelled")); const retryBlock = canRetry ? `<div class="terminal-actions"><button type="button" id="retry-run" class="button button-primary" data-retry-run="${escapeHtml(state.runId)}" title="${escapeHtml(t("actions.retryHint"))}">${escapeHtml(t("actions.retry"))}</button></div>` : ""; $("terminal-panel").innerHTML = `<h3>${heading}</h3><p>${escapeHtml(message)}</p>${retryBlock}`; setConnection(status === "timed_out" ? "failed" : status); state.runRecord && (state.runRecord.retryable = canRetry); }
  async function retryRun(runId) {
    if (!runId || state.runId !== runId) return;
    const btn = $("terminal-panel").querySelector("[data-retry-run]");
    if (btn) { btn.disabled = true; btn.textContent = t("error.retryStarting"); }
    try {
      const record = await api(`/api/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" });
      state.runId = record.run_id;
      state.archived = false;
      state.reportId = null;
      resetRunState();
      $("cancel-run").hidden = false;
      $("new-analysis").hidden = true;
      $("terminal-panel").hidden = true;
      showRun(record);
      connectEvents();
    } catch (error) {
      if (btn) { btn.disabled = false; btn.textContent = t("actions.retry"); }
      const msgEl = $("terminal-panel").querySelector("p");
      if (msgEl) msgEl.textContent = localizeError(error.message);
      setConnection("failed");
    }
  }
  async function loadReport(reportId, { route = true, replace = true } = {}) { if (route) setRoute("report", { reportId, replace }); try { const report = await api(`/api/history/${encodeURIComponent(reportId)}`); renderReport(report); } catch (error) { switchView("report"); $("report-panel").hidden = false; $("report-content").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; setConnection("failed"); } }
  function renderReport(report) { switchView("report"); $("report-panel").hidden = false; $("report-title").textContent = t("report.title", { ticker: report.ticker || t("report.decisionReport") }); const request = state.runRecord?.request || {}; const metadata = [["report.signal", formatRating(report.rating || report.signal)], ["report.date", report.analysis_date || request.analysis_date], ["report.asset", report.asset_type === "crypto" ? t("assets.crypto") : report.asset_type === "stock" ? t("assets.stock") : report.asset_type], ["report.depth", report.research_depth || request.research_depth], ["report.provider", report.provider || request.provider], ["report.quickModel", report.quick_model || request.quick_model], ["report.deepModel", report.deep_model || request.deep_model], ["report.language", languageLabel(report.output_language || request.output_language)], ["report.source", report.source === "legacy" ? t("library.legacy") : report.source], ["report.dataStatus", report.data_status || t("common.unknown")], ["report.snapshot", report.data_snapshot_id || t("common.notGenerated")]].filter(([, value]) => value !== null && value !== undefined && value !== ""); const KEY_TO_DATA = {
          "report.signal": "signal",
          "report.date": "date",
          "report.asset": "asset",
          "report.depth": "depth",
          "report.provider": "provider",
          "report.quickModel": "quick",
          "report.deepModel": "deep",
          "report.language": "lang",
          "report.source": "source",
          "report.dataStatus": "data",
          "report.snapshot": "snapshot",
        };
        $("report-meta").innerHTML = metadata.map(([label, value]) => {
          const dataKey = KEY_TO_DATA[label] || "";
          const mono = /model|snapshot|source|date|provider/.test(label);
          const isSignal = label === "report.signal";
          const ddClass = `${mono ? "mono " : ""}${isSignal ? "signal" : ""}`.trim();
          return `<div><dt data-key="${dataKey}">${escapeHtml(t(label))}</dt><dd${ddClass ? ` class="${ddClass}"` : ""}>${escapeHtml(value)}</dd></div>`;
        }).join(""); $("download-report").onclick = () => { window.location.href = `/api/history/${encodeURIComponent(report.report_id)}/download`; }; const summary = report.executive_summary || ""; $("executive-summary").hidden = !summary; const summaryContent = $("executive-summary-content"); summaryContent.innerHTML = summary ? renderReportMarkdown(report.executive_summary_html, summary) : ""; wrapReportTables(summaryContent); $("detail-report").open = !summary; const sections = REPORT_GROUPS.map(([title, group, sectionList]) => { const blocks = sectionList.map(([label, key]) => { const markdown = report[group]?.[key] || ""; const html = report[`${group}_html`]?.[key]; return markdown ? `<article class="report-section"><h3>${escapeHtml(t(label))}</h3>${renderReportMarkdown(html, markdown)}</article>` : ""; }).join(""); return blocks ? `<section><h2>${escapeHtml(t(title))}</h2>${blocks}</section>` : ""; }).join(""); const reportContent = $("report-content"); reportContent.innerHTML = sections || renderReportMarkdown(report.complete_report_html, report.complete_report || t("library.metadataUnavailable")); wrapReportTables(reportContent); }
  function historyItem(record, library) {
    const status = formatRating(record.rating || record.signal) || (record.status ? t(`status.${record.status}`) : t("status.report"));
    const statusKey = String(record.rating || record.signal || "").toLowerCase();
    const freshness = record.data_status ? t("history.data", { value: record.data_status }) : "";
    const assetLabel = record.asset_type === "crypto" ? t("assets.crypto") : record.asset_type === "stock" ? t("assets.stock") : "";
    const metadata = [assetLabel, record.provider || "", freshness].filter(Boolean).join(" · ");
    const ticker = escapeHtml(record.ticker || t("history.unknown"));
    const signalChip = `<span class="signal-chip is-${escapeHtml(statusKey)}">${escapeHtml(status)}</span>`;
    const actions = library ? "" : `<button class="text-button" type="button" data-report-id="${escapeHtml(record.report_id)}">${escapeHtml(t("actions.open"))}</button>`;
    return `<article class="${library ? "library-item" : "history-item"}"><button type="button" class="library-row" data-report-id="${escapeHtml(record.report_id)}"><span class="library-row-ticker">${ticker}</span><span class="library-row-date">${escapeHtml(record.analysis_date || "")}</span><span class="library-row-meta">${escapeHtml(metadata)}</span>${signalChip}<svg class="library-row-arrow" width="14" height="14" aria-hidden="true"><use href="#i-arrow-right"/></svg></button>${actions}</article>`;
  }
  function bindReportLinks(container) { container.querySelectorAll("[data-report-id]").forEach((button) => button.addEventListener("click", () => reopenHistory(button.dataset.reportId))); }
  function renderHistory(records) { state.history = records || []; const list = $("history-list"); if (!list) { if (state.watchlist.items.length) renderWatchlist(state.watchlist.items, state.watchlist.quotes); return; } if (!state.history.length) { list.innerHTML = `<p class="muted">${escapeHtml(t("history.empty"))}</p>`; } else { list.innerHTML = state.history.slice(0, 5).map((record) => historyItem(record, false)).join(""); bindReportLinks(list); } if (state.watchlist.items.length) renderWatchlist(state.watchlist.items, state.watchlist.quotes); }
  function latestAnalysisFor(symbol) { const key = String(symbol || "").toUpperCase(); return state.history.filter((record) => String(record.ticker || "").toUpperCase() === key && (record.status === "completed" || record.source === "legacy" || !record.status)).sort((a, b) => new Date(b.generated_at || b.analysis_date || 0).getTime() - new Date(a.generated_at || a.analysis_date || 0).getTime())[0] || null; }
  function quoteFreshness(quote) { if (!quote || quote.error || quote.freshness === "unavailable") return i18n.label("freshness", "unavailable"); if (quote.freshness === "stale") return i18n.label("freshness", "stale"); if (quote.cache_status === "hit") return i18n.label("cache_status", "hit"); if (quote.freshness === "delayed" || quote.is_delayed) return i18n.label("freshness", "delayed"); return i18n.label("freshness", "fresh"); }
  function cleanSummary(value) { return String(value || "").replace(/```[\s\S]*?```/g, "").replace(/[*_#>`]/g, "").replace(/\s+/g, " ").trim(); }
  function watchlistAnalysisDate(analysis) { if (!analysis) return ""; for (const candidate of [analysis.analysis_date, analysis.generated_at]) { if (!candidate) continue; const raw = String(candidate).trim(); const datePart = raw.match(/^(\d{4}-\d{2}-\d{2})/)?.[1]; if (datePart) { const parsedDate = new Date(`${datePart}T00:00:00Z`); if (Number.isFinite(parsedDate.getTime()) && parsedDate.toISOString().slice(0, 10) === datePart) return datePart; continue; } const parsed = new Date(raw); if (Number.isFinite(parsed.getTime())) return parsed.toISOString().slice(0, 10); } return ""; }
  function watchlistSignalKey(value) { const key = String(value || "").trim().toLowerCase().replace(/[\s_-]+/g, ""); if (["buy", "strongbuy", "overweight"].includes(key)) return "buy"; if (["sell", "strongsell", "underweight"].includes(key)) return "sell"; if (key === "hold") return "hold"; return "neutral"; }
  function watchlistChangeMarkup(value) { if (value == null || value === "") return ""; const pct = Number(value); if (!Number.isFinite(pct)) return ""; const stateKey = pct > 0 ? "up" : pct < 0 ? "down" : "flat"; const sign = pct > 0 ? "+" : ""; const arrow = stateKey === "flat" ? "" : `<span class="quote-change-arrow" aria-hidden="true">${stateKey === "up" ? "↑" : "↓"}</span>`; return `<span class="quote-change is-${stateKey}">${arrow}<span class="quote-change-value">${sign}${escapeHtml(pct.toFixed(2))}%</span></span>`; }
  function renderWatchlist(items, quotes = {}) { state.watchlist.items = items || []; state.watchlist.quotes = quotes || {}; const list = $("watchlist-list"); if (!list) return; if (!state.watchlist.items.length) { list.innerHTML = `<p class="muted watchlist-empty">${escapeHtml(t("watchlist.empty"))}</p>`; return; } const rows = state.watchlist.items.map((item) => { const quote = quotes[item.symbol] || {}; const analysis = latestAnalysisFor(item.symbol); const numericPrice = Number(quote.price); const price = quote.price == null || !Number.isFinite(numericPrice) ? `<span class="quote-price is-missing">${escapeHtml(t("watchlist.noQuote"))}</span>` : `<span class="quote-price">${escapeHtml(numericPrice.toLocaleString(undefined, { maximumFractionDigits: 6 }))}</span>`; const currency = quote.currency ? `<span class="quote-currency">${escapeHtml(quote.currency)}</span>` : ""; const change = watchlistChangeMarkup(quote.change_percent); const source = quote.source ? `${escapeHtml(t("watchlist.source", { value: quote.source }))} · ${quoteFreshness(quote)}` : escapeHtml(quote.error?.message || t("watchlist.dataSourceUnavailable")); const quoteTime = quote.quote_time || quote.as_of || quote.fetched_at; const quoteMeta = quoteTime ? `${source} · ${escapeHtml(new Date(quoteTime).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}` : source; const identity = i18n.assetIdentity(quote); const analysisDate = watchlistAnalysisDate(analysis); const analysisSignal = analysis ? (formatRating(analysis.rating || analysis.signal) || t("status.completed")) : ""; const analysisSignalKey = analysis ? watchlistSignalKey(analysis.rating || analysis.signal) : "empty"; const dateMarkup = analysisDate ? `<div class="analysis-date"><span class="analysis-label">${escapeHtml(t("watchlist.latestAnalysisLabel"))}</span><span class="analysis-date-value">${escapeHtml(analysisDate)}</span></div>` : `<span class="analysis-date is-missing">${escapeHtml(t("watchlist.noAnalysis"))}</span>`; const signalChip = analysis ? `<span class="signal-chip is-${escapeHtml(analysisSignalKey)}">${escapeHtml(analysisSignal)}</span>` : `<span class="signal-chip is-empty">${escapeHtml(t("watchlist.noAnalysis"))}</span>`; const reportAction = analysis?.report_id ? `<button type="button" class="text-button" data-report-id="${escapeHtml(analysis.report_id)}">${escapeHtml(t("actions.viewReport"))}</button>` : ""; const html = `<article class="watchlist-row"><div class="watchlist-asset"><span class="symbol">${escapeHtml(item.symbol)}</span><span class="asset-meta">${escapeHtml(identity.name)} · ${escapeHtml(identity.exchange)}</span><span class="asset-type">${escapeHtml(t(item.asset_type === "crypto" ? "assets.crypto" : "assets.stock"))}</span></div><div class="watchlist-quote"><div class="quote-value"><span class="quote-number">${price} ${currency}</span>${change}</div><span class="quote-meta">${quoteMeta}</span></div><div class="watchlist-analysis">${dateMarkup}${signalChip}</div><div class="watchlist-actions"><button type="button" class="text-button watchlist-analyze" data-analyze-symbol="${escapeHtml(item.symbol)}" data-analyze-asset="${escapeHtml(item.asset_type)}">${escapeHtml(t("actions.startAnalysis"))}</button>${reportAction}<button type="button" class="icon-button" data-remove-watchlist="${escapeHtml(item.id)}" data-version="${escapeHtml(state.watchlist.version)}" aria-label="${escapeHtml(`${t("actions.remove")} ${item.symbol}`)}" title="${escapeHtml(t("actions.remove"))}">×</button></div></article>`; return { symbol: item.symbol, html }; }); list.innerHTML = rows.map(r => r.html).join(""); bindReportLinks(list); list.querySelectorAll("[data-analyze-symbol]").forEach((button) => button.addEventListener("click", () => { $("ticker").value = button.dataset.analyzeSymbol; $("asset-type").value = button.dataset.analyzeAsset; toggleCryptoAnalyst(); navigate("analysis"); $("analysis-form").scrollIntoView({ behavior: "smooth", block: "start" }); })); list.querySelectorAll("[data-remove-watchlist]").forEach((button) => button.addEventListener("click", async (event) => { event.preventDefault(); event.stopPropagation(); const row = button.closest(".watchlist-row"); if (!row) return; const symbol = row.querySelector(".symbol")?.textContent || ""; const confirmed = await openConfirmModal({ title: t("modal.confirmRemoveTitle"), message: t("watchlist.confirmRemove", { value: symbol }), confirmText: t("actions.confirmRemove"), cancelText: t("actions.cancel"), danger: true }); if (!confirmed) return; button.disabled = true; try { await api(`/api/watchlist/items/${encodeURIComponent(button.dataset.removeWatchlist)}?version=${encodeURIComponent(button.dataset.version)}`, { method: "DELETE" }); await loadWatchlist(); } catch (error) { $("watchlist-error").textContent = localizeError(error.message); button.disabled = false; } })); list.querySelectorAll(".watchlist-row").forEach((row) => row.addEventListener("click", (event) => { if (event.target.closest(".watchlist-actions, .watchlist-chart")) return; const symbol = row.querySelector(".symbol")?.textContent || ""; if (symbol) navigate("asset", { symbol }); })); }
  async function loadWatchlist({ quotesOnly = false, signal = null } = {}) { const list = $("watchlist-list"); if (list) list.setAttribute("aria-busy", "true"); try { $("watchlist-error").textContent = ""; let items = state.watchlist.items; if (!quotesOnly) { const response = await api("/api/watchlist", { signal }); state.watchlist.version = response.watchlist?.version || 1; items = response.items || []; } if (!items.length) { renderWatchlist(items, {}); return { items, quotes: {} }; } const grouped = items.reduce((result, item) => { const key = item.asset_type || "stock"; (result[key] ||= []).push(item.symbol); return result; }, {}); const quotes = { ...state.watchlist.quotes }; await Promise.all(Object.entries(grouped).map(async ([assetType, symbols]) => { const response = await api(`/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}&asset_type=${encodeURIComponent(assetType)}`, { signal }); (response.items || []).forEach((item) => { quotes[item.symbol] = item; }); })); renderWatchlist(items, quotes); return { items, quotes }; } catch (error) { if (error?.name !== "AbortError") { if (!state.watchlist.items.length && list) list.innerHTML = `<p class="muted">${escapeHtml(t("watchlist.unavailable"))}</p>`; $("watchlist-error").textContent = localizeError(error.message); } throw error; } finally { if (list) list.setAttribute("aria-busy", "false"); } }
  async function addWatchlistItem(event) { event.preventDefault(); const symbol = $("watchlist-symbol").value.trim(); if (!symbol) { $("watchlist-error").textContent = t("watchlist.symbolRequired"); return; } try { await api("/api/watchlist/items", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, asset_type: $("watchlist-asset-type").value }) }); $("watchlist-symbol").value = ""; await loadWatchlist(); } catch (error) { $("watchlist-error").textContent = localizeError(error.message); } }
  function renderLibrary() { const list = $("library-list"); const records = state.library.items; $("library-total").textContent = t("library.total", { count: state.library.total }); $("library-prev").disabled = state.library.page <= 1; $("library-next").disabled = !state.library.hasNext; if (!records.length) { list.innerHTML = `<p class="muted">${escapeHtml(state.library.total ? t("library.noMatches") : t("history.empty"))}</p>`; return; } list.innerHTML = records.map((record) => historyItem(record, true)).join(""); bindReportLinks(list); }
  async function loadLibraryPage() { const requestSeq = ++state.library.requestSeq; const params = new URLSearchParams({ page: String(state.library.page), page_size: String(state.library.pageSize), sort: state.filters.sort === "oldest" ? "generated_at_asc" : "generated_at_desc" }); if (state.filters.search.trim()) params.set("query", state.filters.search.trim()); if (state.filters.asset) params.set("asset_type", state.filters.asset); if (state.filters.status) params.set("status", state.filters.status); try { const response = await api(`/api/history?${params.toString()}`); if (requestSeq !== state.library.requestSeq) return; const items = response.items || response; state.library.items = Array.isArray(items) ? items : []; state.library.page = Number(response.page || state.library.page); state.library.pageSize = Number(response.page_size || state.library.pageSize); state.library.total = Number(response.total ?? state.library.items.length); state.library.hasNext = Boolean(response.has_next); renderLibrary(); } catch (_) { if (requestSeq !== state.library.requestSeq) return; state.library.items = []; state.library.total = 0; state.library.hasNext = false; renderLibrary(); } }
  async function reopenHistory(reportId) { navigate("report", { reportId }); }
  function renderModelOptions(config) { const providers = config.providers || []; const providerSelect = $("provider"); const current = providerSelect.value || config.configured?.provider || config.provider; providerSelect.innerHTML = providers.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join(""); providerSelect.value = providers.some((item) => item.value === current) ? current : providers[0]?.value || ""; const selected = providers.find((item) => item.value === providerSelect.value) || providers[0]; const selectedQuick = $("quick-model").value || config.configured?.quick_model; const selectedDeep = $("deep-model").value || config.configured?.deep_model; $("quick-model").innerHTML = (selected?.quick_models || []).map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join(""); $("deep-model").innerHTML = (selected?.deep_models || []).map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join(""); $("quick-model").value = (selected?.quick_models || []).some((item) => item.value === selectedQuick) ? selectedQuick : selected?.quick_models?.[0]?.value || ""; $("deep-model").value = (selected?.deep_models || []).some((item) => item.value === selectedDeep) ? selectedDeep : selected?.deep_models?.[0]?.value || ""; }
  function renderLanguages(config) { const select = $("output-language"); const current = select.value || config.configured?.output_language || config.output_language || "English"; select.innerHTML = (config.output_languages || []).map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(languageLabel(item.value))}</option>`).join(""); select.value = (config.output_languages || []).some((item) => item.value === current) ? current : "English"; }
  function renderFormOptions(config) { const selectedAnalysts = new Set([...form.querySelectorAll('input[name="analysts"]:checked')].map((input) => input.value)); const analystDefaults = selectedAnalysts.size ? selectedAnalysts : new Set((config.analyst_options || []).slice(0, 2).map((item) => item.key)); const selectedDepth = form.querySelector('input[name="research_depth"]:checked')?.value; $("analyst-options").innerHTML = (config.analyst_options || []).map((item) => `<div class="choice"><input id="analyst-${escapeHtml(item.key)}" type="checkbox" name="analysts" value="${escapeHtml(item.key)}" ${analystDefaults.has(item.key) ? "checked" : ""} /><label for="analyst-${escapeHtml(item.key)}">${escapeHtml(t(`analysts.${item.key}`))}</label></div>`).join(""); $("depth-options").innerHTML = (config.research_depths || [1, 3, 5]).map((depth, index) => `<div class="choice"><input id="depth-${depth}" type="radio" name="research_depth" value="${depth}" ${String(depth) === String(selectedDepth ?? (config.research_depths || [1])[0]) || (!selectedDepth && index === 0) ? "checked" : ""} /><label for="depth-${depth}">${escapeHtml(t(depth === 1 ? "depth.quick" : depth === 3 ? "depth.balanced" : "depth.deep"))}</label></div>`).join(""); renderModelOptions(config); renderLanguages(config); }
  function setupForm(config) { state.config = config; $("analysis-date").value = config.default_date || new Date().toISOString().slice(0, 10); renderFormOptions(config); $("asset-type").addEventListener("change", toggleCryptoAnalyst); $("provider").addEventListener("change", () => renderModelOptions(state.config)); toggleCryptoAnalyst(); }
  function toggleCryptoAnalyst() { const disabled = $("asset-type").value === "crypto"; const fundamentals = $("analyst-fundamentals"); if (!fundamentals) return; fundamentals.checked = false; fundamentals.disabled = disabled; fundamentals.closest(".choice").classList.toggle("is-disabled", disabled); }
  async function submitRun(event) { event.preventDefault(); $("form-error").textContent = ""; const analysts = [...form.querySelectorAll('input[name="analysts"]:checked')].map((input) => input.value); if (!$("ticker").value.trim()) { $("form-error").textContent = t("error.ticker"); return; } if (!analysts.length) { $("form-error").textContent = t("error.analysts"); return; } const body = { ticker: $("ticker").value.trim(), analysis_date: $("analysis-date").value, asset_type: $("asset-type").value, analysts, research_depth: Number(form.querySelector('input[name="research_depth"]:checked')?.value || 1), provider: $("provider").value, quick_model: $("quick-model").value, deep_model: $("deep-model").value, output_language: $("output-language").value, quote_strategy_id: state.config?.effective_quote_strategy_id || "default-yfinance" }; try { const record = await api("/api/runs", { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }); setRoute("active"); resetRunState(); state.runId = record.run_id; state.archived = false; showRun(record); $("cancel-run").hidden = false; $("new-analysis").hidden = true; connectEvents(); } catch (error) { $("form-error").textContent = localizeError(error.message); } }
  function updateTopbar(view) {
    const titleNode = $("topbar-title");
    const crumbsNode = $("topbar-crumbs");
    if (!titleNode || !crumbsNode) return;
    const map = {
      setup: { crumb: "nav.watchlist", title: "watchlist.title" },
      analysis: { crumb: "nav.analysis", title: "analysis.title" },
      active: { crumb: "nav.active", title: "active.title" },
      scheduled: { crumb: "nav.scheduled", title: "scheduler.title" },
      library: { crumb: "nav.reports", title: "library.title" },
      settings: { crumb: "nav.settings", title: "settings.title" },
    };
    const entry = map[view] || map.setup;
    titleNode.textContent = t(entry.title);
    titleNode.dataset.i18n = entry.title;
    crumbsNode.innerHTML = `<span data-i18n="brand.platform">${escapeHtml(t("brand.platform"))}</span> <span class="crumbs-sep">›</span> <b data-i18n="${entry.crumb}">${escapeHtml(t(entry.crumb))}</b>`;
  }

  function applyTheme(theme) {
    const next = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("tradingagents-theme", next); } catch (e) {}
    const lightIcon = $("theme-icon-light");
    const darkIcon = $("theme-icon-dark");
    if (lightIcon) lightIcon.style.display = next === "dark" ? "" : "none";
    if (darkIcon) darkIcon.style.display = next === "dark" ? "none" : "";
  }

  function initTheme() {
    const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    applyTheme(current);
    const btn = $("theme-toggle");
    if (btn) btn.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  function initSidebarToggle() {
    const sidebar = $("sidebar-nav");
    const backdrop = $("sidebar-backdrop");
    const toggle = $("sidebar-toggle");
    if (!sidebar || !backdrop || !toggle) return;
    const close = () => { sidebar.classList.remove("is-open"); backdrop.classList.remove("is-open"); };
    const open = () => { sidebar.classList.add("is-open"); backdrop.classList.add("is-open"); };
    toggle.addEventListener("click", () => {
      if (sidebar.classList.contains("is-open")) close(); else open();
    });
    backdrop.addEventListener("click", close);
    document.querySelectorAll(".nav-primary a").forEach((link) => link.addEventListener("click", close));
  }

  function initSidebarCta() {
    const btn = $("sidebar-new-analysis");
    if (btn) btn.addEventListener("click", () => navigate("analysis"));
  }

  function initSidebarCollapse() {
    const shell = document.querySelector(".app-shell");
    const btn = $("sidebar-collapse");
    if (!shell || !btn) return;
    let saved = "expanded";
    try { saved = localStorage.getItem("tradingagents-sidebar") || "expanded"; } catch (e) {}
    if (saved === "collapsed") shell.classList.add("is-sidebar-collapsed");
    btn.addEventListener("click", () => {
      const isCollapsed = shell.classList.toggle("is-sidebar-collapsed");
      try { localStorage.setItem("tradingagents-sidebar", isCollapsed ? "collapsed" : "expanded"); } catch (e) {}
    });
  }

document.addEventListener("click", (event) => { const retryBtn = event.target.closest("[data-retry-run]"); if (retryBtn) { event.preventDefault(); retryRun(retryBtn.dataset.retryRun); } });  $("cancel-run").addEventListener("click", async () => { if (!state.runId) return; try { await api(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" }); } catch (error) { terminalRun("failed", error.message); } }); $("back-history").addEventListener("click", () => navigate(state.archived ? "library" : "active")); $("report-back-library").addEventListener("click", () => navigate("library")); $("new-analysis").addEventListener("click", () => navigate("analysis")); $("active-new-analysis").addEventListener("click", () => navigate("analysis")); $("library-new-analysis").addEventListener("click", () => navigate("analysis")); const resetLibraryPage = () => { state.library.page = 1; loadLibraryPage(); }; $("library-search").addEventListener("input", (event) => { state.filters.search = event.target.value; resetLibraryPage(); }); $("library-asset-filter").addEventListener("change", (event) => { state.filters.asset = event.target.value; resetLibraryPage(); }); $("library-status-filter").addEventListener("change", (event) => { state.filters.status = event.target.value; resetLibraryPage(); }); $("library-sort").addEventListener("change", (event) => { state.filters.sort = event.target.value; resetLibraryPage(); }); $("library-prev").addEventListener("click", () => { if (state.library.page > 1) { state.library.page -= 1; loadLibraryPage(); } }); $("library-next").addEventListener("click", () => { if (state.library.hasNext) { state.library.page += 1; loadLibraryPage(); } }); form.addEventListener("submit", submitRun);
  $("refresh-quotes").addEventListener("click", () => quoteRefreshController?.refresh()); 
  $("watchlist-form").addEventListener("submit", addWatchlistItem); $("watchlist-asset-type").addEventListener("change", updateTickerHint);
  document.querySelectorAll(".nav-primary a").forEach((link) => link.addEventListener("click", (event) => {
    const view = link.dataset.view;
    if (!view) return;
    event.preventDefault();
    if (["setup", "analysis", "active", "scheduled", "library", "settings"].includes(view)) navigate(view);
  }));
  window.addEventListener("popstate", () => applyRoute(window.location.pathname));
  function startQuoteRefresh() { quoteRefreshController?.stop(); quoteRefreshController = new window.QuoteRefreshController({ timeoutMs: 4000, backoff: [QUOTE_REFRESH_MS, 10000, 20000, 40000, 60000], fetcher: (signal) => loadWatchlist({ quotesOnly: true, signal }), onData: () => {}, onError: () => {} }); quoteRefreshController.setVisible(!document.hidden); }
  document.addEventListener("visibilitychange", () => quoteRefreshController?.setVisible(!document.hidden));
  initTheme(); initSidebarToggle(); initSidebarCta(); initSidebarCollapse(); applyTranslations(); renderPhases(); api("/api/config").then(setupForm).catch(() => {}); api("/api/history").then(renderHistory).catch(() => {}); loadWatchlist().finally(startQuoteRefresh); applyRoute(window.location.pathname); restoreActiveRun();
})();
