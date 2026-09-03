(function (root) {
  "use strict";

  const state = {
    page: 1,
    pageSize: 25,
    jobId: "",
    status: "",
    total: 0,
    hasNext: false,
    items: [],
    jobs: [],
    quotes: {},
    active: false,
    visible: !document.hidden,
    initialised: false,
    timer: null,
    loading: false,
  };

  const $ = (id) => document.getElementById(id);

  const t = (key, vars = {}) => (root.TradingAgentsI18n?.t(key, vars) || key).replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? ""));
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));

  const LOGS_API = "/api/scheduled/logs";
  const JOBS_API = "/api/scheduled/jobs";

  const STATUS_LABEL_KEYS = {
    queued: "scheduler.history.status.queued",
    running: "scheduler.history.status.running",
    succeeded: "scheduler.history.status.succeeded",
    failed: "scheduler.history.status.failed",
    skipped: "scheduler.history.status.skipped",
  };

  function setError(message) {
    const errBox = $("scheduled-history-error");
    if (errBox) errBox.textContent = message || "";
  }

  async function api(path, init) {
    const headers = { Accept: "application/json", ...(init?.headers || {}) };
    if (init?.body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...init, headers });
    const text = await response.text();
    if (!response.ok) {
      let detail = text;
      try { detail = JSON.parse(text).detail || JSON.parse(text).message || text; } catch (_) {}
      throw new Error(String(detail || response.statusText));
    }
    return text ? JSON.parse(text) : {};
  }

  async function loadJobs() {
    try {
      const data = await api(JOBS_API);
      state.jobs = Array.isArray(data.items) ? data.items : [];
    } catch (_) {
      state.jobs = [];
    }
    renderJobFilter();
  }

  function renderJobFilter() {
    const sel = $("scheduled-history-job");
    if (!sel) return;
    const opts = [`<option value="" data-i18n="scheduler.history.allJobs">全部任务</option>`];
    state.jobs.forEach((job) => {
      opts.push(`<option value="${esc(job.id)}">${esc(job.symbol)}${job.asset_type === "crypto" ? " (加密货币)" : ""}</option>`);
    });
    sel.innerHTML = opts.join("");
    sel.value = state.jobId || "";
  }

  function renderStatusFilter() {
    const sel = $("scheduled-history-status");
    if (!sel) return;
    const opts = [`<option value="" data-i18n="scheduler.history.allStatuses">全部状态</option>`];
    ["queued", "running", "succeeded", "failed", "skipped"].forEach((s) => {
      opts.push(`<option value="${s}">${esc(t(STATUS_LABEL_KEYS[s] || s))}</option>`);
    });
    sel.innerHTML = opts.join("");
    sel.value = state.status || "";
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return esc(date.toLocaleString([], { hour12: false }));
  }

  function assetIdentity(row) {
    if (!row.symbol) return "<strong>—</strong>";
    const quote = state.quotes[row.symbol];
    if (window.TradingAgentsQuotes?.formatAssetCell) {
      return window.TradingAgentsQuotes.formatAssetCell(row.symbol, row.asset_type, quote, esc);
    }
    return `<strong>${esc(row.symbol)}</strong><small>${esc(row.asset_type === "crypto" ? t("assets.crypto") : t("assets.stock"))}</small>`;
  }

  function statusMarkup(row) {
    const k = STATUS_LABEL_KEYS[row.status] ? STATUS_LABEL_KEYS[row.status] : row.status;
    const label = t(k || row.status);
    let extra = "";
    if (row.status === "failed" && row.error) extra = `<small>${esc(row.error)}</small>`;
    if (row.status === "skipped" && row.skip_reason) extra = `<small>${esc(t(`scheduler.skip.${row.skip_reason}`) || row.skip_reason)}</small>`;
    const cls = row.status === "succeeded" ? "is-success" : row.status === "failed" ? "is-failed" : row.status === "skipped" ? "is-empty" : "";
    return `<strong>${esc(label)}</strong>${extra}`;
  }

  function sourceMarkup(src) {
    if (!src) return "—";
    const map = {
      public_override: "scheduler.history.source.publicOverride",
      last_successful: "scheduler.history.source.lastSuccessful",
      global_default: "scheduler.history.source.globalDefault",
    };
    const key = map[src];
    return key ? `<span class="source-chip">${esc(t(key))}</span>` : esc(src);
  }

  function actionsMarkup(row) {
    const links = [];
    const jobId = esc(row.job_id);
    if (row.run_id && row.report_id) {
      links.push(`<button type="button" class="text-button" data-history-action="report" data-report-id="${esc(row.report_id)}" data-run-id="${esc(row.run_id)}">${esc(t("scheduler.history.viewReport"))}</button>`);
    } else if (row.run_id) {
      links.push(`<button type="button" class="text-button" data-history-action="view-job" data-job-id="${jobId}">${esc(t("scheduler.history.viewJob"))}</button>`);
    }
    if (row.error) {
      links.push(`<button type="button" class="text-button" data-history-action="copy-error" data-error="${esc(row.error)}">${esc(t("scheduler.history.copyError"))}</button>`);
    }
    return links.join("");
  }

  function renderRows() {
    const tbody = $("scheduled-history-rows");
    if (!tbody) return;
    if (!state.items.length) {
      tbody.innerHTML = `<tr><td colspan="6"><p class="muted" style="padding:20px 0;text-align:center">${esc(t("scheduler.history.empty"))}</p></td></tr>`;
      return;
    }
    tbody.innerHTML = state.items.map((row) => `
      <tr>
        <td>${formatTime(row.fired_at)}</td>
        <td>${assetIdentity(row)}</td>
        <td>${statusMarkup(row)}</td>
        <td>${sourceMarkup(row.parameter_source)}</td>
        <td>${row.started_at && row.finished_at ? esc(formatDuration(row.started_at, row.finished_at)) : "—"}</td>
        <td class="scheduled-history-actions">${actionsMarkup(row)}</td>
      </tr>
    `).join("");
  }

  function formatDuration(started, finished) {
    try {
      const a = new Date(started).getTime();
      const b = new Date(finished).getTime();
      if (!Number.isFinite(a) || !Number.isFinite(b) || b < a) return "—";
      const secs = Math.round((b - a) / 1000);
      if (secs < 60) return `${secs} s`;
      const mins = Math.floor(secs / 60);
      const rem = secs % 60;
      return `${mins}m ${rem}s`;
    } catch (_) { return "—"; }
  }

  function renderPagination() {
    const info = $("scheduled-history-page-info");
    const prev = $("scheduled-history-prev");
    const next = $("scheduled-history-next");
    if (!info || !prev || !next) return;
    const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
    info.textContent = t("scheduler.history.pageInfo", { page: state.page, totalPages, total: state.total });
    prev.disabled = state.page <= 1;
    next.disabled = !state.hasNext;
  }

  async function loadPage() {
    setError("");
    state.loading = true;
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    if (state.jobId) params.set("job_id", state.jobId);
    if (state.status) params.set("status", state.status);
    try {
      const data = await api(`${LOGS_API}?${params.toString()}`);
      state.items = Array.isArray(data.items) ? data.items : [];
      state.total = Number(data.total || 0);
      state.hasNext = Boolean(data.has_next);
      state.page = Number(data.page || state.page);
      renderRows();
      renderPagination();
    } catch (e) {
      setError(e.message || "加载失败");
      state.items = [];
      state.total = 0;
      state.hasNext = false;
      renderRows();
      renderPagination();
    } finally {
      state.loading = false;
    }
    refreshAssetQuotes();
  }

  async function refreshAssetQuotes() {
    if (!state.items.length) return;
    const grouped = state.items.reduce((acc, item) => {
      if (!item.symbol) return acc;
      const key = item.asset_type || "stock";
      (acc[key] ||= new Set()).add(item.symbol);
      return acc;
    }, {});
    let changed = false;
    await Promise.all(Object.entries(grouped).map(async ([assetType, symbolSet]) => {
      const symbols = [...symbolSet];
      try {
        const response = window.TradingAgentsQuotes?.fetch
          ? await window.TradingAgentsQuotes.fetch(symbols, assetType)
          : await api(`/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}&asset_type=${encodeURIComponent(assetType)}`);
        (response.items || []).forEach((q) => { state.quotes[q.symbol] = q; changed = true; });
      } catch (_) { /* ignore quote failures; will fall back to asset_type label */ }
    }));
    if (changed) renderRows();
  }

  function bindEvents() {
    if (state.initialised) return;
    state.initialised = true;
    $("scheduled-history-back")?.addEventListener("click", () => root.TradingAgentsApp?.navigate?.("scheduled"));
    $("scheduled-history-refresh")?.addEventListener("click", () => loadPage());
    $("scheduled-history-job")?.addEventListener("change", (event) => {
      state.jobId = event.target.value || "";
      state.page = 1;
      loadPage();
    });
    $("scheduled-history-status")?.addEventListener("change", (event) => {
      state.status = event.target.value || "";
      state.page = 1;
      loadPage();
    });
    $("scheduled-history-page-size")?.addEventListener("change", (event) => {
      state.pageSize = Number(event.target.value) || 25;
      state.page = 1;
      loadPage();
    });
    $("scheduled-history-prev")?.addEventListener("click", () => {
      if (state.page > 1) { state.page -= 1; loadPage(); }
    });
    $("scheduled-history-next")?.addEventListener("click", () => {
      if (state.hasNext) { state.page += 1; loadPage(); }
    });
    const tbody = $("scheduled-history-rows");
    if (tbody) {
      tbody.addEventListener("click", (event) => {
        const target = event.target.closest("[data-history-action]");
        if (!target) return;
        const action = target.dataset.historyAction;
        if (action === "report") {
          const reportId = target.dataset.reportId;
          if (reportId) root.TradingAgentsApp?.navigate?.("report", { reportId });
        } else if (action === "view-job") {
          root.TradingAgentsApp?.navigate?.("scheduled");
        } else if (action === "copy-error") {
          const text = target.dataset.error || "";
          if (navigator.clipboard && text) navigator.clipboard.writeText(text).then(() => setError(t("scheduler.history.copied"))).catch(() => setError(text));
          else setError(text);
        }
      });
    }
  }

  async function init() {
    bindEvents();
    renderStatusFilter();
    renderJobFilter();
  }

  function show() {
    state.active = true;
    bindEvents();
    init();
  }
  function hide() {
    state.active = false;
  }
  function setActive(active) {
    state.active = !!active;
    state.visible = !document.hidden;
    if (active) { bindEvents(); init(); }
  }

  root.TradingAgentsScheduledHistory = { init, show, hide, setActive, refresh: loadPage };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => { bindEvents(); }, { once: true });
  else bindEvents();
})(window);
