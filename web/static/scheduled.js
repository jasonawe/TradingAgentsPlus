(function (root) {
  "use strict";

  const state = {
    jobs: [],
    watchlist: [],
    quotes: {},
    settings: { enabled: true, max_concurrent_runs: 3 },
    active: false,
    visible: !document.hidden,
    timer: null,
    loading: false,
    busy: new Set(),
    expanded: new Set(),
    logs: {},
    formJob: null,
    formRequest: 0,
    formCronValid: false,
    initialized: false,
    previousFocus: null,
    drawerFocus: null,
  };
  const $ = (id) => document.getElementById(id);
  const t = (key, vars = {}) => (root.TradingAgentsI18n?.t(key, vars) || key).replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? ""));
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
  const formatTime = (value) => {
    if (!value) return t("scheduler.noNextRun");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    return date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const statusLabel = (status) => ({ succeeded: "已成功", failed: "失败", skipped: "已跳过", queued: "排队中", running: "运行中" }[status] || status || t("scheduler.noLastRun"));
  const skipReasonLabel = (reason) => {
    const key = `scheduler.skip.${reason || ""}`;
    const label = t(key);
    return label === key ? reason || "" : label;
  };
  const jsonHeaders = { "Content-Type": "application/json" };
  const NETWORK_TIMEOUT_MS = 15000;
  const SCHEDULED_JOBS_API = "/api/scheduled/jobs";
  const SCHEDULED_SETTINGS_API = "/api/scheduled/settings";
  const SCHEDULED_CRON_PREVIEW_API = "/api/scheduled/cron/preview";
  const WATCHLIST_API = "/api/watchlist";
  const QUOTES_API = "/api/quotes";

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), NETWORK_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(path, { headers: { Accept: "application/json", ...(options.body ? jsonHeaders : {}) }, ...options, signal: options.signal || controller.signal });
    } catch (error) {
      if (error?.name === "AbortError" || error instanceof TypeError) throw new Error(t("scheduler.loadError"));
      throw error;
    } finally {
      clearTimeout(timeout);
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail || body.message;
      throw new Error(response.status === 422 ? t("scheduler.invalidCron") : String(detail || t("scheduler.loadError")));
    }
    return body;
  }

  function setError(message, target = "scheduled-error") { const node = $(target); if (node) node.textContent = message || ""; }
  function setBusy(key, value) { value ? state.busy.add(key) : state.busy.delete(key); render(); }
  function switchMarkup(id, checked, label) { return `<button id="${id}" class="switch-control${checked ? " is-on" : ""}" type="button" role="switch" aria-checked="${checked ? "true" : "false"}" aria-label="${esc(label)}"><span aria-hidden="true"></span><b>${esc(checked ? t("scheduler.toggleOn") : t("scheduler.toggleOff"))}</b></button>`; }

  function render() {
    const list = $("scheduled-list");
    if (!list) return;
    list.setAttribute("aria-busy", state.loading ? "true" : "false");
    if (state.loading) { list.innerHTML = `<div class="scheduled-loading"><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span></div>`; return; }
    if (!state.jobs.length) { list.innerHTML = `<div class="empty-state scheduled-empty"><strong>${esc(t("scheduler.emptyTitle"))}</strong><p>${esc(t("scheduler.emptyBody"))}</p><button id="scheduled-empty-add" class="button button-primary" type="button">${esc(t("scheduler.add"))}</button></div>`; $("scheduled-empty-add")?.addEventListener("click", () => openForm()); return; }
    const headers = ["scheduler.asset", "scheduler.cron", "scheduler.nextRun", "scheduler.lastResult", "scheduler.status", "scheduler.actions"].map((key) => `<th scope="col">${esc(t(key))}</th>`).join("");
    const rows = state.jobs.map((job) => {
      const expanded = state.expanded.has(job.id);
      const busy = state.busy.has(job.id);
      const result = job.last_run_status ? `<span class="scheduled-status status-${esc(job.last_run_status)}">${esc(statusLabel(job.last_run_status))}</span>` : `<span class="muted">${esc(t("scheduler.noLastRun"))}</span>`;
      const report = job.last_report_id ? `<a href="/reports/${encodeURIComponent(job.last_report_id)}" class="text-button">${esc(t("scheduler.report"))}</a>` : "";
      return `<tbody class="scheduled-job-group"><tr class="scheduled-job-row${expanded ? " is-expanded" : ""}">
        <td data-label="${esc(t("scheduler.asset"))}">${formatAssetCell(job.symbol, job.asset_type)}</td>
        <td data-label="${esc(t("scheduler.cron"))}"><code>${esc(job.cron_expression)}</code>${job.note ? `<small>${esc(job.note)}</small>` : ""}</td>
        <td data-label="${esc(t("scheduler.nextRun"))}">${esc(formatTime(job.next_run_at))}</td>
        <td data-label="${esc(t("scheduler.lastResult"))}">${result}${report}</td>
        <td data-label="${esc(t("scheduler.status"))}">${switchMarkup(`scheduled-toggle-${esc(job.id)}`, job.enabled, job.enabled ? t("scheduler.toggleOn") : t("scheduler.toggleOff"))}</td>
        <td data-label="${esc(t("scheduler.actions"))}" class="scheduled-actions"><button type="button" class="text-button" data-scheduled-action="logs" data-job-id="${esc(job.id)}" aria-expanded="${expanded ? "true" : "false"}">${esc(t("scheduler.logs"))}</button><button type="button" class="text-button" data-scheduled-action="run" data-job-id="${esc(job.id)}" ${busy ? "disabled" : ""}>${esc(t("scheduler.run"))}</button><button type="button" class="text-button" data-scheduled-action="edit" data-job-id="${esc(job.id)}" ${busy ? "disabled" : ""}>${esc(t("scheduler.edit"))}</button><button type="button" class="text-button is-danger" data-scheduled-action="delete" data-job-id="${esc(job.id)}" ${busy ? "disabled" : ""}>${esc(t("scheduler.delete"))}</button></td>
      </tr>${expanded ? `<tr class="scheduled-log-row"><td colspan="6"><div id="scheduled-logs-${esc(job.id)}" class="scheduled-logs" aria-live="polite">${renderLogs(job.id)}</div></td></tr>` : ""}</tbody>`;
    }).join("");
    list.innerHTML = `<div class="scheduled-table-wrap"><table class="scheduled-table"><thead><tr>${headers}</tr></thead>${rows}</table></div>`;
    list.querySelectorAll("[data-scheduled-action]").forEach((button) => button.addEventListener("click", () => handleAction(button.dataset.scheduledAction, button.dataset.jobId)));
    state.jobs.forEach((job) => { const toggle = $(`scheduled-toggle-${job.id}`); if (toggle) toggle.disabled = state.busy.has(job.id); toggle?.addEventListener("click", () => toggleJob(job)); });
  }

  function renderLogs(jobId) {
    const logs = state.logs[jobId];
    if (!logs) return `<p class="muted">${esc(t("scheduler.loading"))}</p>`;
    if (!logs.length) return `<p class="muted">${esc(t("scheduler.historyEmpty"))}</p>`;
    return `<ul>${logs.map((log) => `<li><time>${esc(formatTime(log.fired_at))}</time><span class="scheduled-status status-${esc(log.status)}">${esc(statusLabel(log.status))}</span>${log.skip_reason ? `<span>${esc(t("scheduler.skipReason", { value: skipReasonLabel(log.skip_reason) }))}</span>` : ""}${log.error ? `<span class="form-error">${esc(log.error)}</span>` : ""}${log.report_id ? `<a href="/reports/${encodeURIComponent(log.report_id)}" class="text-button">${esc(t("scheduler.report"))}</a>` : ""}</li>`).join("")}</ul>`;
  }

  async function refreshExpandedLogs() {
    const ids = [...state.expanded];
    if (!ids.length) return;
    await Promise.all(ids.map(async (id) => {
      try {
        const response = await request(`/api/scheduled/jobs/${encodeURIComponent(id)}/logs?limit=20`);
        state.logs[id] = response.items || [];
      } catch (_) {}
    }));
  }

  async function load({ silent = false } = {}) {
    if (!silent) { state.loading = true; render(); }
    try {
      const [jobs, settings, watchlist] = await Promise.all([request(SCHEDULED_JOBS_API), request(SCHEDULED_SETTINGS_API), request(WATCHLIST_API)]);
      state.jobs = Array.isArray(jobs.items) ? jobs.items : [];
      state.settings = { ...state.settings, ...settings };
      state.watchlist = Array.isArray(watchlist.items) ? watchlist.items : [];
      await refreshAssetQuotes([
        ...state.watchlist,
        ...(Array.isArray(jobs.items) ? jobs.items.map((j) => ({ symbol: j.symbol, asset_type: j.asset_type })) : []),
      ]);
      if (silent) await refreshExpandedLogs();
      setError("");
      updateSwitches();
      render();
    } catch (error) { setError(error.message || t("scheduler.loadError")); if (!silent) { state.jobs = []; render(); } }
    finally { state.loading = false; render(); }
  }
  function identityFor(symbol) {
    const quote = state.quotes?.[symbol];
    if (!quote) {
      return { nameZh: "", nameEn: "", exchangeZh: "", exchangeEn: "" };
    }
    const pick = (...vals) => vals.find((v) => v && String(v).trim()) || "";
    return {
      nameZh: pick(quote.asset_name_zh, quote.name_zh),
      nameEn: pick(quote.asset_name, quote.name, quote.quote?.raw_summary),
      exchangeZh: pick(quote.exchange_name_zh),
      exchangeEn: pick(quote.exchange, quote.quote?.exchange),
    };
  }
  function formatAssetCell(symbol, assetType) {
    const id = identityFor(symbol);
    const lines = [];
    if (id.nameZh && id.exchangeZh) lines.push(`<small>${esc(id.nameZh)} · ${esc(id.exchangeZh)}</small>`);
    else if (id.nameZh) lines.push(`<small>${esc(id.nameZh)}</small>`);
    else if (id.exchangeZh) lines.push(`<small>${esc(id.exchangeZh)}</small>`);
    if (id.nameEn && id.exchangeEn) lines.push(`<small class="muted">${esc(id.nameEn)} · ${esc(id.exchangeEn)}</small>`);
    else if (id.nameEn) lines.push(`<small class="muted">${esc(id.nameEn)}</small>`);
    else if (id.exchangeEn) lines.push(`<small class="muted">${esc(id.exchangeEn)}</small>`);
    if (!lines.length) lines.push(`<small>${esc(assetType === "crypto" ? t("assets.crypto") : t("assets.stock"))}</small>`);
    return `<strong>${esc(symbol)}</strong>${lines.join("")}`;
  }
  async function refreshAssetQuotes(items) {
    state.quotes = {};
    if (!items || !items.length) return;
    const grouped = items.reduce((acc, item) => {
      const key = item.asset_type || "stock";
      (acc[key] ||= new Set()).add(item.symbol);
      return acc;
    }, {});
    await Promise.all(Object.entries(grouped).map(async ([assetType, symbolSet]) => {
      const symbols = Array.from(symbolSet).join(",");
      try {
        const response = await request(`${QUOTES_API}?symbols=${encodeURIComponent(symbols)}&asset_type=${encodeURIComponent(assetType)}`);
        (response.items || []).forEach((q) => { state.quotes[q.symbol] = q; });
      } catch (_) { /* ignore quote failures; will fall back to asset_type label */ }
    }));
  }
  function updateSwitches() {
    [$("scheduled-master-enabled"), $("scheduled-drawer-enabled")].forEach((node) => { if (!node) return; node.classList.toggle("is-on", Boolean(state.settings.enabled)); node.setAttribute("aria-checked", String(Boolean(state.settings.enabled))); const b = node.querySelector("b"); if (b) b.textContent = state.settings.enabled ? t("scheduler.toggleOn") : t("scheduler.toggleOff"); });
    const range = $("scheduled-max-concurrent"); const output = $("scheduled-max-concurrent-value"); if (range) range.value = String(state.settings.max_concurrent_runs); if (output) output.value = output.textContent = String(state.settings.max_concurrent_runs);
  }
  function schedulePolling() { clearInterval(state.timer); state.timer = state.active && state.visible ? setInterval(() => load({ silent: true }), 5000) : null; }
  function setActive(active) { init(); const wasActive = state.active; state.active = Boolean(active); if (state.active && !wasActive) load(); else if (!state.active) { clearInterval(state.timer); state.timer = null; closeDrawer(); closeForm(); } schedulePolling(); }

  function formMarkup(job = null) {
    const options = state.watchlist.map((item) => {
      const id = identityFor(item.symbol);
      const labelParts = [item.symbol];
      if (id.nameZh) labelParts.push(id.nameZh);
      if (id.exchangeZh) labelParts.push(id.exchangeZh);
      if (labelParts.length === 1) labelParts.push(item.asset_type === "crypto" ? t("assets.crypto") : t("assets.stock"));
      const labelText = labelParts.join(" · ");
      const enParts = [];
      if (id.nameEn) enParts.push(id.nameEn);
      if (id.exchangeEn) enParts.push(id.exchangeEn);
      const enText = enParts.length ? ` (${enParts.join(" · ")})` : "";
      return `<option value="${esc(item.symbol)}" data-asset-type="${esc(item.asset_type || "stock")}" ${job?.symbol === item.symbol ? "selected" : ""}>${esc(labelText)}${enText ? esc(enText) : ""}</option>`;
    }).join("");
    return `<div class="modal-overlay" data-scheduled-close-form></div><div class="modal-dialog scheduled-form-dialog" role="dialog" aria-modal="true" aria-labelledby="scheduled-form-title"><h2 id="scheduled-form-title">${esc(job ? t("scheduler.edit") : t("scheduler.add"))}</h2><form id="scheduled-form" novalidate><label class="field"><span>${esc(t("scheduler.asset"))}</span><select id="scheduled-symbol" required>${options || `<option value="">${esc(t("scheduler.watchlistEmpty"))}</option>`}</select></label><label class="field"><span>${esc(t("scheduler.cron"))}</span><input id="scheduled-cron" required autocomplete="off" value="${esc(job?.cron_expression || "0 9 * * 1-5")}" aria-describedby="scheduled-cron-hint scheduled-cron-error" /><small id="scheduled-cron-hint" class="field-hint">${esc(t("scheduler.cronHint"))}</small><p id="scheduled-cron-error" class="form-error" role="alert" aria-live="polite"></p><ul id="scheduled-cron-preview" class="scheduled-preview" aria-live="polite"></ul></label><label class="field"><span>${esc(t("scheduler.note"))}</span><textarea id="scheduled-note" rows="3" maxlength="500">${esc(job?.note || "")}</textarea></label><p id="scheduled-form-error" class="form-error" role="alert" aria-live="polite"></p><div class="modal-actions"><button type="button" class="button button-secondary" data-scheduled-close-form>${esc(t("actions.cancel"))}</button><button type="submit" class="button button-primary">${esc(t("scheduler.save"))}</button></div></form></div>`;
  }
  function openForm(job = null) {
    state.formJob = job; state.formCronValid = false; state.previousFocus = document.activeElement; const rootNode = $("modal-root"); if (!rootNode) return; rootNode.innerHTML = formMarkup(job); rootNode.classList.add("is-open"); rootNode.setAttribute("aria-hidden", "false"); rootNode.querySelectorAll("[data-scheduled-close-form]").forEach((node) => node.addEventListener("click", closeForm)); rootNode.querySelector("#scheduled-form")?.addEventListener("submit", saveForm); rootNode.querySelector("#scheduled-cron")?.addEventListener("input", previewCron); previewCron(); setTimeout(() => rootNode.querySelector("#scheduled-symbol")?.focus(), 0);
  }
  function closeForm() { const rootNode = $("modal-root"); if (!rootNode || !rootNode.querySelector("#scheduled-form")) return; rootNode.classList.remove("is-open"); rootNode.setAttribute("aria-hidden", "true"); rootNode.innerHTML = ""; state.formJob = null; state.formRequest += 1; state.previousFocus?.focus?.(); }
  let previewTimer = null;
  function previewCron() { clearTimeout(previewTimer); state.formCronValid = false; const input = $("scheduled-cron"); const list = $("scheduled-cron-preview"); if (!input || !list) return; const expression = input.value.trim(); if (!expression) { list.innerHTML = ""; return; } previewTimer = setTimeout(async () => { const requestId = ++state.formRequest; try { const result = await request(`${SCHEDULED_CRON_PREVIEW_API}?cron_expression=${encodeURIComponent(expression)}&count=3`); if (requestId !== state.formRequest) return; state.formCronValid = true; $("scheduled-cron-error").textContent = ""; list.innerHTML = (result.next_run_times || []).map((value) => `<li>${esc(formatTime(value))}</li>`).join(""); } catch (_) { if (requestId !== state.formRequest) return; state.formCronValid = false; $("scheduled-cron-error").textContent = t("scheduler.invalidCron"); list.innerHTML = ""; } }, 250); }
  async function saveForm(event) { event.preventDefault(); const symbolSelect = $("scheduled-symbol"); const symbol = symbolSelect?.value; const cron = $("scheduled-cron")?.value.trim(); const note = $("scheduled-note")?.value.trim() || null; const assetType = symbolSelect?.selectedOptions[0]?.dataset.assetType || state.formJob?.asset_type || "stock"; if (!symbol || !cron || !state.formCronValid) { setError(!state.formCronValid ? t("scheduler.invalidCron") : t("scheduler.formError"), "scheduled-form-error"); return; } const submit = event.submitter; if (submit) submit.disabled = true; const body = { symbol, asset_type: assetType, cron_expression: cron, note }; const path = state.formJob ? `/api/scheduled/jobs/${encodeURIComponent(state.formJob.id)}` : "/api/scheduled/jobs"; state.busy.add("form"); try { await request(path, { method: state.formJob ? "PATCH" : "POST", body: JSON.stringify(body) }); closeForm(); await load(); } catch (error) { setError(error.message || t("scheduler.formError"), "scheduled-form-error"); } finally { state.busy.delete("form"); if (submit?.isConnected) submit.disabled = false; } }
  async function toggleJob(job) { setBusy(job.id, true); try { await request(`/api/scheduled/jobs/${encodeURIComponent(job.id)}/toggle`, { method: "POST", body: JSON.stringify({ enabled: !job.enabled }) }); await load({ silent: true }); } catch (error) { setError(error.message); } finally { state.busy.delete(job.id); render(); } }
  async function handleAction(action, id) { const job = state.jobs.find((item) => item.id === id); if (!job) return; if (action === "logs") { if (state.expanded.has(id)) state.expanded.delete(id); else { state.expanded.add(id); if (!state.logs[id]) { state.logs[id] = null; render(); try { const response = await request(`/api/scheduled/jobs/${encodeURIComponent(id)}/logs?limit=20`); state.logs[id] = response.items || []; } catch (error) { state.logs[id] = []; setError(error.message); } } render(); } } else if (action === "edit") openForm(job); else if (action === "run") { setBusy(id, true); try { await request(`/api/scheduled/jobs/${encodeURIComponent(id)}/run`, { method: "POST" }); await load({ silent: true }); } catch (error) { setError(error.message); } finally { state.busy.delete(id); render(); } } else if (action === "delete") { const confirmed = await confirmDelete(job); if (!confirmed) return; setBusy(id, true); try { await request(`/api/scheduled/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }); state.expanded.delete(id); delete state.logs[id]; await load(); } catch (error) { setError(error.message); } finally { state.busy.delete(id); render(); } } }
  function confirmDelete(job) { return new Promise((resolve) => { const rootNode = $("modal-root"); if (!rootNode) return resolve(window.confirm(t("scheduler.deleteConsequence", { value: job.symbol }))); const previousFocus = document.activeElement; rootNode.innerHTML = `<div class="modal-overlay" data-confirm-cancel></div><div class="modal-dialog" role="alertdialog" aria-modal="true" aria-labelledby="scheduled-delete-title"><h2 id="scheduled-delete-title">${esc(t("scheduler.delete"))}</h2><p class="modal-message">${esc(t("scheduler.deleteConsequence", { value: job.symbol }))}</p><div class="modal-actions"><button type="button" class="button button-secondary" data-confirm-cancel>${esc(t("actions.cancel"))}</button><button type="button" class="button btn-danger" data-confirm-ok>${esc(t("scheduler.confirmDelete"))}</button></div></div>`; rootNode.classList.add("is-open"); rootNode.setAttribute("aria-hidden", "false"); let onKey; const finish = (value) => { document.removeEventListener("keydown", onKey); rootNode.classList.remove("is-open"); rootNode.setAttribute("aria-hidden", "true"); rootNode.innerHTML = ""; previousFocus?.focus?.(); resolve(value); }; rootNode.querySelectorAll("[data-confirm-cancel]").forEach((node) => node.addEventListener("click", () => finish(false))); rootNode.querySelector("[data-confirm-ok]")?.addEventListener("click", () => finish(true)); onKey = (event) => { if (event.key === "Escape") { event.preventDefault(); finish(false); } else if (event.key === "Enter") { event.preventDefault(); finish(true); } }; document.addEventListener("keydown", onKey); setTimeout(() => rootNode.querySelector("[data-confirm-ok]")?.focus(), 0); }); }

  function openDrawer() { const drawer = $("scheduled-settings-drawer"); if (!drawer) return; state.drawerFocus = document.activeElement; drawer.hidden = false; drawer.setAttribute("aria-hidden", "false"); drawer.classList.add("is-open"); updateSwitches(); setTimeout(() => $("scheduled-drawer-enabled")?.focus(), 0); }
  function closeDrawer() { const drawer = $("scheduled-settings-drawer"); if (!drawer || drawer.hidden) return; drawer.classList.remove("is-open"); drawer.setAttribute("aria-hidden", "true"); drawer.hidden = true; state.drawerFocus?.focus?.(); state.drawerFocus = null; }
  async function saveSettings(patch) { const previous = { ...state.settings }; state.settings = { ...state.settings, ...patch }; updateSwitches(); try { state.settings = await request(SCHEDULED_SETTINGS_API, { method: "PATCH", body: JSON.stringify(patch) }); updateSwitches(); setError(t("scheduler.settingsSaved"), "scheduled-settings-error"); } catch (error) { state.settings = previous; updateSwitches(); setError(error.message, "scheduled-settings-error"); } }
  function init() { if (state.initialized) return; state.initialized = true; $("scheduled-add")?.addEventListener("click", () => openForm()); $("scheduled-settings")?.addEventListener("click", openDrawer); document.querySelectorAll("[data-scheduled-close-drawer]").forEach((node) => node.addEventListener("click", closeDrawer)); [$("scheduled-master-enabled"), $("scheduled-drawer-enabled")].forEach((node) => node?.addEventListener("click", () => saveSettings({ enabled: !state.settings.enabled }))); $("scheduled-max-concurrent")?.addEventListener("change", (event) => saveSettings({ max_concurrent_runs: Number(event.target.value) })); document.addEventListener("visibilitychange", () => { state.visible = !document.hidden; schedulePolling(); if (state.active && state.visible) load({ silent: true }); }); document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeDrawer(); if ($("modal-root")?.querySelector("#scheduled-form")) closeForm(); } }); }
  root.TradingAgentsScheduled = { init, show: () => { init(); setActive(true); }, setActive };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true }); else init();
})(window);
