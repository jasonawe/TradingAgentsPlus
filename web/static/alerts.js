(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  try { window.TradingAgentsAlerts = api; } catch (_) { /* window frozen */ }
  try { if (typeof __TA_MODULES__ !== "undefined") __TA_MODULES__.TradingAgentsAlerts = api; } catch (_) {}
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function t(key, vars) {
    const i18n = window.TradingAgentsI18n;
    return i18n ? i18n.t(key, vars || {}) : key;
  }
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[c];
    });
  }
  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }
  function apiFetch(path, options) {
    return fetch(path, {
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      ...(options || {}),
    }).then(async function (response) {
      const text = await response.text();
      const body = text ? JSON.parse(text) : {};
      if (!response.ok) {
        const detail = body && body.detail ? body.detail : response.statusText;
        const error = new Error(detail || "request failed");
        error.status = response.status;
        throw error;
      }
      return body;
    });
  }

  const PRICE_METRICS = ["change_percent"];
  const QUANT_METRICS = ["volume", "turnover", "turnover_rate", "market_cap", "circulating_cap", "pe_ratio", "amplitude"];

  function formatRule(alert) {
    if (alert.kind === "price") {
      const dir = alert.params.direction === "below" ? t("alerts.direction.below") : t("alerts.direction.above");
      return t("alerts.thresholdAndDirection", {
        direction: dir,
        threshold: String(alert.params.threshold),
      });
    }
    if (alert.kind === "quantitative") {
      const metricLabel = t("alerts.metric." + alert.params.metric);
      return t("alerts.metricAndThreshold", {
        metric: metricLabel,
        changePct: String(alert.params.change_pct),
        window: String(alert.params.window_minutes || 0),
      });
    }
    return "";
  }

  function alertMarkup(alert) {
    const rule = formatRule(alert);
    const statusClass = alert.enabled ? "is-enabled" : "is-disabled";
    const statusText = alert.enabled ? t("alerts.enabled") : t("alerts.disabled");
    const kindLabel = alert.kind === "price" ? t("alerts.kind.price") : t("alerts.kind.quantitative");
    const cooldown = alert.cooldown_seconds ? `${alert.cooldown_seconds}s` : "—";
    return [
      '<article class="alert-card ' + statusClass + '" data-alert-id="' + escapeAttr(alert.id) + '">',
      '  <header class="alert-card-header">',
      '    <span class="alert-card-kind">' + escapeHtml(kindLabel) + '</span>',
      '    <span class="alert-card-status">' + escapeHtml(statusText) + '</span>',
      '    <div class="alert-card-actions">',
      '      <button type="button" class="text-button" data-alert-action="toggle" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.toggle")) + '</button>',
      '      <button type="button" class="text-button" data-alert-action="edit" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.edit")) + '</button>',
      '      <button type="button" class="text-button" data-alert-action="delete" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.delete")) + '</button>',
      '    </div>',
      '  </header>',
      '  <div class="alert-card-rule">' + escapeHtml(rule) + '</div>',
      '  <div class="alert-card-meta">' + escapeHtml(t("alerts.cooldown")) + ': ' + escapeHtml(cooldown) + '</div>',
      '</article>',
    ].join("");
  }

  function formMarkup(existing, defaultKind, defaultSymbol, defaultAssetType) {
    const isEdit = !!existing;
    const kind = isEdit ? existing.kind : (defaultKind || "price");
    const params = isEdit ? existing.params : (kind === "price" ? { threshold: "", direction: "above" } : { metric: "volume", change_pct: 20, window_minutes: 60 });
    const cooldown = isEdit ? existing.cooldown_seconds : 3600;
    const symbol = isEdit ? existing.symbol : (defaultSymbol || "");
    const assetType = isEdit ? existing.asset_type : (defaultAssetType || "stock");
    // When no symbol was provided (unified list "new" button), render visible inputs.
    const showSymbolInput = !isEdit && !symbol;
    const showAssetTypeInput = !isEdit && !assetType;

    const metricOptions = QUANT_METRICS.map(function (m) {
      return '<option value="' + m + '"' + (params.metric === m ? " selected" : "") + '>' + escapeHtml(t("alerts.metric." + m)) + '</option>';
    }).join("");

    return [
      '<form class="alert-form" data-alert-form' + (isEdit ? ' data-alert-id="' + escapeAttr(existing.id) + '"' : '') + ' data-form-symbol="' + escapeAttr(symbol) + '" data-form-asset-type="' + escapeAttr(assetType) + '">',
      '  <div class="form-field">',
      '    <label for="alert-kind"><span>' + escapeHtml(t("alerts.kindLabel")) + '</span></label>',
      '    <select id="alert-kind" name="kind">',
      '      <option value="price"' + (kind === "price" ? " selected" : "") + '>' + escapeHtml(t("alerts.kind.price")) + '</option>',
      '      <option value="quantitative"' + (kind === "quantitative" ? " selected" : "") + '>' + escapeHtml(t("alerts.kind.quantitative")) + '</option>',
      '    </select>',
      '  </div>',
      '  <div class="form-segment" data-alert-fields="price"' + (kind === "price" ? "" : ' hidden') + '>',
      '    <p class="form-segment-title">' + escapeHtml(t("alerts.priceSegment")) + '</p>',
      '    <div class="form-row">',
      '      <div class="form-field">',
      '        <label for="alert-threshold"><span>' + escapeHtml(t("alerts.threshold")) + '</span></label>',
      '        <input id="alert-threshold" type="number" step="any" name="threshold" value="' + escapeAttr(params.threshold) + '" placeholder="' + escapeAttr(t("alerts.placeholder.price")) + '"' + (kind === "price" ? ' required' : '') + '>',
      '      </div>',
      '      <div class="form-field">',
      '        <label for="alert-direction"><span>' + escapeHtml(t("alerts.direction")) + '</span></label>',
      '        <select id="alert-direction" name="direction"><option value="above"' + (params.direction === "above" ? " selected" : "") + '>' + escapeHtml(t("alerts.direction.above")) + '</option><option value="below"' + (params.direction === "below" ? " selected" : "") + '>' + escapeHtml(t("alerts.direction.below")) + '</option></select>',
      '      </div>',
      '    </div>',
      '  </div>',
      '  <div class="form-segment" data-alert-fields="quantitative"' + (kind === "quantitative" ? "" : ' hidden') + '>',
      '    <p class="form-segment-title">' + escapeHtml(t("alerts.quantSegment")) + '</p>',
      '    <div class="form-field">',
      '      <label for="alert-metric"><span>' + escapeHtml(t("alerts.metric")) + '</span></label>',
      '      <select id="alert-metric" name="metric">' + metricOptions + '</select>',
      '    </div>',
      '    <div class="form-row">',
      '      <div class="form-field">',
      '        <label for="alert-change-pct"><span>' + escapeHtml(t("alerts.changePct")) + '</span></label>',
      '        <input id="alert-change-pct" type="number" step="any" name="change_pct" value="' + escapeAttr(params.change_pct) + '" placeholder="' + escapeAttr(t("alerts.placeholder.changePct")) + '"' + (kind === "quantitative" ? ' required' : '') + '>',
      '      </div>',
      '      <div class="form-field">',
      '        <label for="alert-window"><span>' + escapeHtml(t("alerts.window")) + '</span></label>',
      '        <input id="alert-window" type="number" min="0" max="1440" name="window_minutes" value="' + escapeAttr(params.window_minutes) + '" placeholder="' + escapeAttr(t("alerts.placeholder.window")) + '">',
      '      </div>',
      '    </div>',
      '  </div>',
      '  <div class="form-field">',
      '    <label for="alert-cooldown"><span>' + escapeHtml(t("alerts.cooldown")) + ' <span class="form-hint">' + escapeHtml(t("alerts.cooldownHint")) + '</span></span></label>',
      '    <input id="alert-cooldown" type="number" min="0" name="cooldown_seconds" value="' + escapeAttr(cooldown) + '">',
      '  </div>',
      (showSymbolInput ? '  <div class="form-field"><label for="alert-symbol"><span>' + escapeHtml(t("alerts.symbolInput")) + '</span></label><input id="alert-symbol" type="text" name="symbol" value="" placeholder="' + escapeAttr(t("alerts.symbolInputPlaceholder")) + '" required></div>' : ''),
      (showAssetTypeInput ? '  <div class="form-field"><label for="alert-asset-type"><span>' + escapeHtml(t("alerts.assetTypeInput")) + '</span></label><select id="alert-asset-type" name="asset_type"><option value="stock" selected>' + escapeHtml(t("assets.stock")) + '</option><option value="crypto">' + escapeHtml(t("assets.crypto")) + '</option></select></div>' : ''),
      (showSymbolInput ? '' : '  <input type="hidden" name="symbol" value="' + escapeAttr(symbol) + '">'),
      (showAssetTypeInput ? '' : '  <input type="hidden" name="asset_type" value="' + escapeAttr(assetType) + '">'),
      '</form>',
    ].join("");
  }

  function panelState(rootEl) {
    let st = rootEl.__alertsState;
    if (!st) {
      st = {
        symbol: "",
        assetType: "stock",
        items: [],
        loading: false,
        error: "",
        total: 0,
        page: 1,
        pageSize: 20,
        identity: {},
      };
      rootEl.__alertsState = st;
    }
    return st;
  }

  function render(rootEl) {
    const st = panelState(rootEl);
    if (st.error) {
      rootEl.innerHTML = '<p class="error-text">' + escapeHtml(st.error) + '</p>';
      return;
    }
    if (!st.items.length) {
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("alerts.emptyShort")) + '</p>';
      return;
    }
    rootEl.innerHTML = st.items.map(alertMarkup).join("");
  }

  function attachHandlers(rootEl) {
    if (rootEl.__alertsHandlersAttached) return;
    rootEl.__alertsHandlersAttached = true;
    // Delegated kind-select change toggles required fields. We bind on
    // document so it works whether the form lives inline or inside the modal.
    document.addEventListener("change", function (event) {
      const target = event.target;
      if (!target || !target.matches || !target.matches('.alert-form select[name="kind"]')) return;
      const form = target.closest(".alert-form");
      if (!form) return;
      const kind = target.value;
      const priceBlock = form.querySelector('[data-alert-fields="price"]');
      const quantBlock = form.querySelector('[data-alert-fields="quantitative"]');
      if (priceBlock) priceBlock.hidden = kind !== "price";
      if (quantBlock) quantBlock.hidden = kind !== "quantitative";
      try {
        const thr = form.querySelector('input[name="threshold"]'); if (thr) thr.required = (kind === "price");
        const cp = form.querySelector('input[name="change_pct"]'); if (cp) cp.required = (kind === "quantitative");
      } catch (_) {}
    });
    rootEl.addEventListener("click", function (event) {
      const deleteBtn = event.target.closest('[data-alert-action="delete"]');
      if (deleteBtn) {
        const app = (typeof window !== "undefined" && window.TradingAgentsApp) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsApp);
        if (app && app.openConfirmModal) {
          app.openConfirmModal({
            title: t("alerts.deleteTitle") || t("alerts.delete"),
            message: t("alerts.deleteConfirm"),
            confirmText: t("alerts.delete"),
            cancelText: t("alerts.cancel"),
            danger: true
          }).then(function (proceed) {
            if (proceed) deleteAlert(rootEl, deleteBtn.dataset.alertId);
          });
        } else {
          if (!window.confirm(t("alerts.deleteConfirm"))) return;
          deleteAlert(rootEl, deleteBtn.dataset.alertId);
        }
        return;
      }
      const editBtn = event.target.closest('[data-alert-action="edit"]');
      if (editBtn) {
        const st = panelState(rootEl);
        const alert = st.items.find(function (a) { return a.id === editBtn.dataset.alertId; });
        if (alert) showForm(rootEl, alert, alert.symbol, alert.asset_type);
        return;
      }
      const toggleBtn = event.target.closest('[data-alert-action="toggle"]');
      if (toggleBtn) {
        const st = panelState(rootEl);
        const alert = st.items.find(function (a) { return a.id === toggleBtn.dataset.alertId; });
        if (alert) toggleAlert(rootEl, alert);
        return;
      }
      const cancelBtn = event.target.closest('[data-alert-action="cancel"]');
      if (cancelBtn) {
        const form = cancelBtn.closest(".alert-form");
        if (form) form.remove();
        return;
      }
    });
    rootEl.addEventListener("submit", function (event) {
      const form = event.target.closest(".alert-form");
      if (!form || !rootEl.contains(form)) return;
      event.preventDefault();
      submitForm(rootEl, form);
    });
    // Modal form submit (form lives outside rootEl). Wire once per process.
    if (!document.__alertsModalSubmitBound) {
      document.__alertsModalSubmitBound = true;
      document.addEventListener("submit", function (event) {
        const form = event.target.closest && event.target.closest(".alert-form");
        if (!form) return;
        // Only handle forms NOT already inside rootEl (handled by delegated above)
        if (rootEl && rootEl.contains(form)) return;
        event.preventDefault();
        submitForm(null, form);
      });
    }
    rootEl.addEventListener("change", function (event) {
      if (event.target.matches('.alert-form select[name="kind"]')) {
        const form = event.target.closest(".alert-form");
        if (!form) return;
        const kind = event.target.value;
        form.querySelector('[data-alert-fields="price"]').hidden = kind !== "price";
        form.querySelector('[data-alert-fields="quantitative"]').hidden = kind !== "quantitative";
        try {
          var thr = form.querySelector('input[name="threshold"]'); if (thr) thr.required = (kind === "price");
          var cp = form.querySelector('input[name="change_pct"]'); if (cp) cp.required = (kind === "quantitative");
        } catch (_) {}
      }
    });
  }

  async function showForm(rootEl, existing, overrideSymbol, overrideAssetType) {
    const st = panelState(rootEl);
    if (typeof overrideSymbol === "string") st.symbol = overrideSymbol;
    if (typeof overrideAssetType === "string") st.assetType = overrideAssetType;
    const html = formMarkup(existing || null, "price", st.symbol, st.assetType);
    const title = existing ? t("alerts.editTitle") : t("alerts.formTitle");
    const app = (typeof window !== "undefined" && window.TradingAgentsApp) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsApp);
    if (!app || !app.openFormModal) { return; }
    const result = await app.openFormModal({
      title: title,
      bodyHtml: html,
      submitText: t("alerts.save"),
      cancelText: t("alerts.cancel"),
      width: "wide",
      icon: "&#128276;"
    });
    // result === null means cancelled; anything else (true) means saved.
    // We rely on submitFormModal having POSTed/PATCHed; just reload list.
    if (rootEl.id === "alerts-all-list") {
      await loadAll(rootEl);
    } else {
      await load(rootEl);
    }
  }

  async function submitForm(rootEl, form) {
    // legacy path - kept for safety, modal path uses submitFormModal below
    return submitFormModal(form);
  }

  async function submitFormModal(form) {
    const kind = form.querySelector('select[name="kind"]').value;
    const cooldown = Number(form.querySelector('input[name="cooldown_seconds"]').value || 0);
    // Prefer visible form inputs (new from unified list) over data-form-* attrs
    const symInput = form.querySelector('input[name="symbol"]');
    const atInput = form.querySelector('select[name="asset_type"]') || form.querySelector('input[name="asset_type"]');
    const symbol = (symInput && symInput.value) || form.getAttribute("data-form-symbol") || "";
    const assetType = (atInput && atInput.value) || form.getAttribute("data-form-asset-type") || "stock";
    let params;
    if (kind === "price") {
      params = {
        threshold: Number(form.querySelector('input[name="threshold"]').value),
        direction: form.querySelector('select[name="direction"]').value,
      };
    } else {
      params = {
        metric: form.querySelector('select[name="metric"]').value,
        change_pct: Number(form.querySelector('input[name="change_pct"]').value),
        window_minutes: Number(form.querySelector('input[name="window_minutes"]').value || 0),
      };
    }
    const id = form.dataset.alertId;
    try {
      if (id) {
        await apiFetch("/api/alerts/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify({ params: params, cooldown_seconds: cooldown }),
        });
      } else {
        await apiFetch("/api/alerts", {
          method: "POST",
          body: JSON.stringify({
            symbol: symbol,
            asset_type: assetType,
            kind: kind,
            params: params,
            cooldown_seconds: cooldown,
          }),
        });
      }
      // Show a brief inline success before the modal closes
      return true;
    } catch (error) {
      // Inject error into modal instead of closing
      showFormError(form, error.message || t("alerts.errors.saveFailed"));
      return false;
    }
  }

  function showFormError(form, msg) {
    let err = form.querySelector(".alert-form-error");
    if (!err) {
      err = document.createElement("p");
      err.className = "alert-form-error form-error";
      err.style.cssText = "color: #ef4444; font-size: 13px; margin: 0;";
      const actions = form.querySelector(".alert-form-actions");
      if (actions) form.insertBefore(err, actions);
      else form.appendChild(err);
    }
    err.textContent = msg;
  }

  async function deleteAlert(rootEl, id) {
    const st = panelState(rootEl);
    st.error = "";
    try {
      await apiFetch("/api/alerts/" + encodeURIComponent(id), { method: "DELETE" });
      await load(rootEl);
      if (window.TradingAgentsAlertsEvents && typeof window.TradingAgentsAlertsEvents.refresh === "function") {
        window.TradingAgentsAlertsEvents.refresh();
      }
    } catch (error) {
      st.error = t("alerts.errors.deleteFailed");
      render(rootEl);
    }
  }

  async function toggleAlert(rootEl, alert) {
    try {
      await apiFetch("/api/alerts/" + encodeURIComponent(alert.id), {
        method: "PATCH",
        body: JSON.stringify({ enabled: !alert.enabled }),
      });
      await load(rootEl);
    } catch (error) {
      const st = panelState(rootEl);
      st.error = t("alerts.errors.saveFailed");
      render(rootEl);
    }
  }

  async function load(rootEl, symbol, assetType, options) {
    const st = panelState(rootEl);
    if (typeof symbol === "string") st.symbol = symbol;
    if (typeof assetType === "string") st.assetType = assetType;
    const skipRender = !!(options && options.skipRender);
    if (!st.symbol) {
      st.items = [];
      if (!skipRender) render(rootEl);
      return;
    }
    st.loading = true;
    st.error = "";
    try {
      const response = await apiFetch(
        "/api/alerts?symbol=" + encodeURIComponent(st.symbol) +
        "&asset_type=" + encodeURIComponent(st.assetType || "stock")
      );
      st.items = (response && response.items) || [];
    } catch (error) {
      st.error = t("alerts.errors.loadFailed");
      st.items = [];
    } finally {
      st.loading = false;
    }
    if (!skipRender) render(rootEl);
  }

  function mount(rootEl, symbol, assetType) {
    if (!rootEl) return;
    attachHandlers(rootEl);
    const st = panelState(rootEl);
    if (typeof symbol === "string") st.symbol = symbol;
    if (typeof assetType === "string") st.assetType = assetType;
    const addBtn = rootEl.parentElement?.querySelector('[data-alerts-add][data-symbol="' + (window.CSS?.escape ? window.CSS.escape(symbol || "") : (symbol || "").replace(/"/g, '\\"')) + '"]');
    document.querySelectorAll('[data-alerts-add]').forEach(function (btn) {
      if (btn.__alertsAddBound) return;
      const targetSelector = btn.dataset.alertsTarget;
      if (!targetSelector) return;
      const targetEl = document.querySelector(targetSelector);
      if (!targetEl || targetEl !== rootEl) return;
      btn.__alertsAddBound = true;
      btn.addEventListener("click", function () {
        const sym = btn.dataset.symbol || symbol;
        const at = btn.dataset.assetType || assetType || "stock";
        if (sym !== panelState(rootEl).symbol || at !== panelState(rootEl).assetType) {
          load(rootEl, sym, at).then(function () { showForm(rootEl, null, sym, at); });
        } else {
          showForm(rootEl, null, sym, at);
        }
      });
    });
    load(rootEl, symbol, assetType);
    // 在已渲染的 form 上更新 symbol/asset_type hidden inputs
    try {
      if (typeof symbol === "string" && symbol) {
        const form = rootEl.querySelector("form.alert-form");
        if (form) {
          if (!form.getAttribute("data-form-symbol")) form.setAttribute("data-form-symbol", symbol);
          if (!form.getAttribute("data-form-asset-type")) form.setAttribute("data-form-asset-type", assetType || "stock");
        }
      }
    } catch (_) { /* frozen DOM 兜底 */ }
  }

  // ---- Events (topbar bell) ---------------------------------------------

  let eventsCache = { items: [], unread: 0 };
  let refreshTimer = null;

  function renderEvent(event) {
    const triggered = event.triggered_at ? new Date(event.triggered_at).toLocaleString() : "";
    const unack = !event.acknowledged_at ? " is-unread" : "";
    return [
      '<article class="alert-event' + unack + '" data-event-id="' + escapeAttr(event.id) + '">',
      '  <header class="alert-event-header">',
      '    <span class="alert-event-symbol">' + escapeHtml(event.symbol) + '</span>',
      '    <span class="alert-event-kind">' + escapeHtml(event.kind) + '</span>',
      '    <span class="alert-event-time">' + escapeHtml(triggered) + '</span>',
      '  </header>',
      '  <div class="alert-event-message">' + escapeHtml(event.message) + '</div>',
      (event.acknowledged_at ? "" : '<div class="alert-event-actions"><button type="button" class="text-button" data-alerts-event-action="ack" data-event-id="' + escapeAttr(event.id) + '">' + escapeHtml(t("alerts.ack")) + '</button></div>'),
      '</article>',
    ].join("");
  }

  async function fetchEvents() {
    try {
      const response = await apiFetch("/api/alerts/events?limit=50");
      eventsCache = { items: response.items || [], unread: response.unread || 0 };
    } catch (_) {
      // keep previous cache
    }
    return eventsCache;
  }

  function renderDrawer() {
    const list = window.document?.getElementById?.("alerts-events-list");
    if (!list) return;
    if (!eventsCache.items.length) {
      list.innerHTML = '<p class="muted">' + escapeHtml(t("alerts.eventsEmpty")) + '</p>';
    } else {
      list.innerHTML = eventsCache.items.map(renderEvent).join("");
    }
    const badge = window.document?.getElementById?.("alerts-bell-badge");
    if (badge) {
      badge.textContent = eventsCache.unread > 0 ? String(eventsCache.unread) : "";
      badge.hidden = eventsCache.unread === 0;
    }
  }

  async function refresh() {
    await fetchEvents();
    renderDrawer();
  }

  function startPolling(intervalMs) {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(function () { refresh(); }, Math.max(15000, intervalMs || 60000));
  }

  function stopPolling() {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  }

  function bindDrawer() {
    if (window.document?.getElementById?.("alerts-events-list")?.__alertsEventsBound) return;
    const list = window.document?.getElementById?.("alerts-events-list");
    if (!list) return;
    list.__alertsEventsBound = true;
    list.addEventListener("click", async function (event) {
      const ackBtn = event.target.closest('[data-alerts-event-action="ack"]');
      if (ackBtn) {
        try {
          await apiFetch("/api/alerts/events/" + encodeURIComponent(ackBtn.dataset.eventId) + "/ack", { method: "POST" });
          await refresh();
        } catch (_) {}
        return;
      }
    });
    const ackAllBtn = window.document?.getElementById?.("alerts-ack-all");
    if (ackAllBtn && !ackAllBtn.__alertsAckAllBound) {
      ackAllBtn.__alertsAckAllBound = true;
      ackAllBtn.addEventListener("click", async function () {
        try {
          await apiFetch("/api/alerts/events/ack-all", { method: "POST" });
          await refresh();
        } catch (_) {}
      });
    }
    const bell = window.document?.getElementById?.("alerts-bell");
    if (bell && !bell.__alertsBellBound) {
      bell.__alertsBellBound = true;
      bell.addEventListener("click", function () {
        const drawer = window.document?.getElementById?.("alerts-drawer");
        if (drawer) drawer.hidden = !drawer.hidden;
      });
    }
  }


  async function loadAll(rootEl, options) {
    if (!rootEl) return;
    const st = panelState(rootEl);
    st.symbol = "";
    st.assetType = "stock";
    const skipRender = !!(options && options.skipRender);
    st.loading = true;
    st.error = "";
    if (!skipRender) {
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("alerts.loading")) + "</p>";
    }
    try {
      const showDisabled = !!document.getElementById("alerts-show-disabled")?.checked;
      const params = new URLSearchParams();
      params.set("limit", String(st.pageSize || 20));
      params.set("offset", String((st.page || 1) - 1) * (st.pageSize || 20));
      const url = "/api/alerts?" + params.toString();
      const response = await apiFetch(url);
      st.items = (response && response.items) || [];
      st.total = (response && response.total) || 0;
      st.limit = (response && response.limit) || (st.pageSize || 20);
      st.offset = (response && response.offset) || 0;
      // Render immediately with whatever identities we already have (cached).
      st.identity = st.identity || {};
      if (!skipRender) renderAll(rootEl);
      // Then enrich with asset names in the background. Each fetch has a
      // timeout so a slow upstream provider doesn't block the UI.
      const symbols = Array.from(new Set(st.items.map(function (i) { return i.symbol + "|" + i.asset_type; })));
      fetchIdentities(symbols).then(function (identities) {
        st.identity = identities || {};
        if (!skipRender) renderAll(rootEl);
      }).catch(function () { /* ignore - identities are best-effort */ });
    } catch (error) {
      st.error = t("alerts.errors.loadFailed");
      st.items = [];
      st.total = 0;
      if (!skipRender) renderAll(rootEl);
    } finally {
      st.loading = false;
    }
  }

  // Cache asset identities keyed by symbol|asset_type to avoid re-fetching.
  const __identityCache = {};
  const IDENTITY_TIMEOUT_MS = 20000;
  function fetchWithTimeout(url, ms) {
    return new Promise(function (resolve, reject) {
      const ctrl = new AbortController();
      const timer = setTimeout(function () { ctrl.abort(); reject(new Error("timeout")); }, ms);
      fetch(url, { signal: ctrl.signal }).then(function (r) { clearTimeout(timer); resolve(r); }).catch(function (e) { clearTimeout(timer); reject(e); });
    });
  }
  async function fetchIdentities(symbolAssetPairs) {
    const result = Object.assign({}, __identityCache);
    const pairs = (symbolAssetPairs || []).filter(Boolean);
    // Prefer the shared quote cache so a single /api/quotes call serves
    // the watchlist, scheduled jobs, alerts, and notes pages.
    const quotesModule = (typeof window !== "undefined" && window.TradingAgentsQuotes) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsQuotes);
    const grouped = {};
    pairs.forEach(function (p) {
      if (__identityCache[p]) return;
      const [sym, assetType] = p.split("|");
      const key = assetType || "stock";
      (grouped[key] = grouped[key] || []).push(sym);
    });
    await Promise.all(Object.entries(grouped).map(async function ([assetType, syms]) {
      try {
        const url = "/api/quotes?symbols=" + encodeURIComponent(syms.join(",")) + "&asset_type=" + encodeURIComponent(assetType);
        const fetcher = quotesModule && quotesModule.fetch
          ? quotesModule.fetch(syms, assetType)
          : (async () => {
              const r = await fetchWithTimeout(url, IDENTITY_TIMEOUT_MS);
              if (!r.ok) throw new Error("status " + r.status);
              return r.json();
            })();
        // Race the quote fetch against a timeout so a slow upstream can't
        // leave the UI stuck on "加载中…".
        let timer;
        const timeout = new Promise(function (_, reject) {
          timer = setTimeout(function () { reject(new Error("identity fetch timeout")); }, IDENTITY_TIMEOUT_MS);
        });
        let data;
        try {
          data = await Promise.race([fetcher, timeout]);
        } finally {
          clearTimeout(timer);
        }
        const items = (data && data.items) || [];
        const bySymbol = {};
        items.forEach(function (it) { bySymbol[it.symbol] = it; });
        syms.forEach(function (sym) {
          const quote = bySymbol[sym];
          if (quote) {
            __identityCache[sym + "|" + assetType] = {
              name: quote.asset_name || quote.name || "",
              name_zh: quote.asset_name_zh || quote.name_zh || "",
              exchange: quote.exchange || "",
              exchange_name_zh: quote.exchange_name_zh || "",
            };
          }
        });
        syms.forEach(function (sym) {
          if (!__identityCache[sym + "|" + assetType]) {
            __identityCache[sym + "|" + assetType] = { name: sym, exchange: "" };
          }
        });
      } catch (_) {
        syms.forEach(function (sym) {
          if (!__identityCache[sym + "|" + assetType]) {
            __identityCache[sym + "|" + assetType] = { name: sym, exchange: "" };
          }
        });
      }
    }));
    pairs.forEach(function (p) { result[p] = __identityCache[p] || { name: p.split("|")[0] }; });
    return result;
  }

  function renderAll(rootEl) {
    const st = panelState(rootEl);
    if (st.error) {
      rootEl.innerHTML = '<p class="error-text">' + escapeHtml(st.error) + "</p>";
      return;
    }
    if (!st.items.length) {
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("alerts.noAlerts")) + "</p>" + paginationMarkup(st, "alerts");
      bindPagination(rootEl, "alerts");
      return;
    }
    rootEl.innerHTML = st.items.map(function (item) { return alertAllMarkup(item, st.identity); }).join("") + paginationMarkup(st, "alerts");
    bindPagination(rootEl, "alerts");
  }

  function alertAllMarkup(alert, identityMap) {
    const rule = formatRule(alert);
    const statusClass = alert.enabled ? "is-enabled" : "is-disabled";
    const statusText = alert.enabled ? t("alerts.enabled") : t("alerts.disabled");
    const kindLabel = alert.kind === "price" ? t("alerts.kind.price") : t("alerts.kind.quantitative");
    const cooldown = alert.cooldown_seconds ? alert.cooldown_seconds + "s" : "—";
    const assetHref = "/assets/" + encodeURIComponent(alert.symbol);
    const id = (identityMap && identityMap[alert.symbol + "|" + (alert.asset_type || "stock")]) || {};
    const assetName = id.name_zh || id.name || "";
    const assetExchange = id.exchange || "";
    return [
      '<article class="alert-card alert-card-all ' + statusClass + '" data-alert-id="' + escapeAttr(alert.id) + '">',
      '  <header class="alert-card-header">',
      '    <div class="alert-card-asset">',
      '      <a class="alert-card-symbol" href="' + escapeAttr(assetHref) + '" data-alert-symbol-link>' + escapeHtml(alert.symbol) + "</a>",
      '<span class="alert-card-asset-name ' + (assetName ? "is-loaded" : "is-loading") + '">' + (assetName ? escapeHtml(assetName) + (assetExchange ? " · " + escapeHtml(assetExchange) : "") : "加载中…") + "</span>",
      "    </div>",
      '    <span class="alert-card-kind">' + escapeHtml(kindLabel) + "</span>",
      '    <span class="alert-card-status">' + escapeHtml(statusText) + "</span>",
      '    <div class="alert-card-actions">',
      '      <button type="button" class="text-button" data-alert-action="toggle" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.toggle")) + "</button>",
      '      <button type="button" class="text-button" data-alert-action="edit" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.edit")) + "</button>",
      '      <button type="button" class="text-button" data-alert-action="delete" data-alert-id="' + escapeAttr(alert.id) + '">' + escapeHtml(t("alerts.delete")) + "</button>",
      "    </div>",
      "  </header>",
      '  <div class="alert-card-rule">' + escapeHtml(rule) + "</div>",
      '  <div class="alert-card-meta">' + escapeHtml(t("alerts.cooldown")) + ": " + escapeHtml(cooldown) + "</div>",
      "</article>",
    ].join("");
  }

  function paginationMarkup(st, namespace) {
    const total = st.total || 0;
    const pageSize = st.pageSize || 20;
    const page = st.page || 1;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (total <= pageSize) return "";
    const hasPrev = page > 1;
    const hasNext = page < totalPages;
    return [
      '<nav class="pagination-controls" data-pagination="' + namespace + '">',
      '  <button type="button" class="text-button" data-page-action="prev"' + (hasPrev ? "" : " disabled") + ">" + escapeHtml(t("pagination.prev")) + "</button>",
      '  <span class="pagination-info">' + escapeHtml(t("pagination.page", { page: page, total: totalPages, count: total })) + "</span>",
      '  <button type="button" class="text-button" data-page-action="next"' + (hasNext ? "" : " disabled") + ">" + escapeHtml(t("pagination.next")) + "</button>",
      "</nav>",
    ].join("");
  }

  function bindPagination(rootEl, namespace) {
    const nav = rootEl.querySelector('[data-pagination="' + namespace + '"]');
    if (!nav || nav.__bound) return;
    nav.__bound = true;
    nav.addEventListener("click", function (event) {
      const btn = event.target.closest("[data-page-action]");
      if (!btn || btn.disabled) return;
      const st = panelState(rootEl);
      const totalPages = Math.max(1, Math.ceil((st.total || 0) / (st.pageSize || 20)));
      if (btn.dataset.pageAction === "prev" && st.page > 1) st.page -= 1;
      else if (btn.dataset.pageAction === "next" && st.page < totalPages) st.page += 1;
      else return;
      loadAll(rootEl);
    });
  }

  function mountAll(rootEl) {
    if (!rootEl) return;
    // Bind unified list interactions (toggle / edit / delete) using existing handlers
    attachHandlers(rootEl);
    // "新建告警" button opens the create modal
    const newBtn = document.getElementById("alerts-new-button");
    if (newBtn && !newBtn.__alertsNewBound) {
      newBtn.__alertsNewBound = true;
      newBtn.addEventListener("click", function () { showForm(rootEl, null, "", ""); });
    }
    // Symbol link: navigate to asset detail (SPA-friendly)
    rootEl.addEventListener("click", function (event) {
      const link = event.target.closest("[data-alert-symbol-link]");
      if (link) {
        event.preventDefault();
        try {
          if (window.TradingAgentsApp && typeof window.TradingAgentsApp.navigate === "function") {
            const sym = (link.getAttribute("href") || "").split("/").pop() || "";
            if (sym) window.TradingAgentsApp.navigate("asset", { symbol: decodeURIComponent(sym) });
          }
        } catch (_) {}
      }
    });
    // Show-disabled checkbox refresh
    const cb = document.getElementById("alerts-show-disabled");
    if (cb && !cb.__alertsAllBound) {
      cb.__alertsAllBound = true;
      cb.addEventListener("change", function () { loadAll(rootEl); });
    }
    loadAll(rootEl);
  }

  return {
    showForm: showForm,
    submitFormModal: submitFormModal,
    mount: mount,
    mountAll: mountAll,
    loadAll: loadAll,
    renderAll: renderAll,
    load: load,
    refresh: function (rootEl) { return load(rootEl); },
    formatRule: formatRule,
    // events
    refreshEvents: refresh,
    startPolling: startPolling,
    stopPolling: stopPolling,
    bindDrawer: bindDrawer,
    handleQuoteTriggers: function (triggers) {
      if (triggers && triggers.length) refresh();
    },
  };
});
