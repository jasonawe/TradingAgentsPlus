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

    const metricOptions = QUANT_METRICS.map(function (m) {
      return '<option value="' + m + '"' + (params.metric === m ? " selected" : "") + '>' + escapeHtml(t("alerts.metric." + m)) + '</option>';
    }).join("");

    return [
      '<form class="alert-form" data-alert-form' + (isEdit ? ' data-alert-id="' + escapeAttr(existing.id) + '"' : '') + ' data-form-symbol="' + escapeAttr(symbol) + '" data-form-asset-type="' + escapeAttr(assetType) + '">',
      '  <div class="alert-form-row">',
      '    <label><span>' + escapeHtml(t("alerts.kindLabel")) + '</span>',
      '      <select name="kind">',
      '        <option value="price"' + (kind === "price" ? " selected" : "") + '>' + escapeHtml(t("alerts.kind.price")) + '</option>',
      '        <option value="quantitative"' + (kind === "quantitative" ? " selected" : "") + '>' + escapeHtml(t("alerts.kind.quantitative")) + '</option>',
      '      </select>',
      '    </label>',
      '  </div>',
      '  <div class="alert-form-fields-price" data-alert-fields="price"' + (kind === "price" ? "" : ' hidden') + '>',
      '    <label><span>' + escapeHtml(t("alerts.threshold")) + '</span><input type="number" step="any" name="threshold" value="' + escapeAttr(params.threshold) + '" placeholder="' + escapeAttr(t("alerts.placeholder.price")) + '"' + (kind === "price" ? ' required' : '') + '></label>',
      '    <label><span>' + escapeHtml(t("alerts.direction")) + '</span><select name="direction"><option value="above"' + (params.direction === "above" ? " selected" : "") + '>' + escapeHtml(t("alerts.direction.above")) + '</option><option value="below"' + (params.direction === "below" ? " selected" : "") + '>' + escapeHtml(t("alerts.direction.below")) + '</option></select></label>',
      '  </div>',
      '  <div class="alert-form-fields-quant" data-alert-fields="quantitative"' + (kind === "quantitative" ? "" : ' hidden') + '>',
      '    <label><span>' + escapeHtml(t("alerts.metric")) + '</span><select name="metric">' + metricOptions + '</select></label>',
      '    <label><span>' + escapeHtml(t("alerts.changePct")) + '</span><input type="number" step="any" name="change_pct" value="' + escapeAttr(params.change_pct) + '" placeholder="' + escapeAttr(t("alerts.placeholder.changePct")) + '"' + (kind === "quantitative" ? ' required' : '') + '></label>',
      '    <label><span>' + escapeHtml(t("alerts.window")) + '</span><input type="number" min="0" max="1440" name="window_minutes" value="' + escapeAttr(params.window_minutes) + '" placeholder="' + escapeAttr(t("alerts.placeholder.window")) + '"></label>',
      '  </div>',
      '  <label class="alert-form-cooldown"><span>' + escapeHtml(t("alerts.cooldown")) + '</span><input type="number" min="0" name="cooldown_seconds" value="' + escapeAttr(cooldown) + '" title="' + escapeAttr(t("alerts.cooldownHint")) + '"></label>',
      '  <input type="hidden" name="symbol" value="' + escapeAttr(symbol) + '">',
      '  <input type="hidden" name="asset_type" value="' + escapeAttr(assetType) + '">',
      '  <div class="alert-form-actions">',
      '    <button type="button" class="button button-secondary" data-alert-action="cancel">' + escapeHtml(t("alerts.cancel")) + '</button>',
      '    <button type="submit" class="button button-primary">' + escapeHtml(t("alerts.save")) + '</button>',
      '  </div>',
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
    rootEl.addEventListener("click", function (event) {
      const deleteBtn = event.target.closest('[data-alert-action="delete"]');
      if (deleteBtn) {
        if (!window.confirm(t("alerts.deleteConfirm"))) return;
        deleteAlert(rootEl, deleteBtn.dataset.alertId);
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

  function showForm(rootEl, existing, overrideSymbol, overrideAssetType) {
    const st = panelState(rootEl);
    if (typeof overrideSymbol === "string") st.symbol = overrideSymbol;
    if (typeof overrideAssetType === "string") st.assetType = overrideAssetType;
    const existing_form = rootEl.querySelector(".alert-form");
    if (existing_form) existing_form.remove();
    const html = formMarkup(existing || null, "price", st.symbol, st.assetType);
    rootEl.insertAdjacentHTML("afterbegin", html);
  }

  async function submitForm(rootEl, form) {
    const st = panelState(rootEl);
    st.error = "";
    const kind = form.querySelector('select[name="kind"]').value;
    const cooldown = Number(form.querySelector('input[name="cooldown_seconds"]').value || 0);
    const symbol = form.getAttribute("data-form-symbol") || (form.querySelector('input[name="symbol"]') && form.querySelector('input[name="symbol"]').value) || "";
    const assetType = form.getAttribute("data-form-asset-type") || (form.querySelector('input[name="asset_type"]') && form.querySelector('input[name="asset_type"]').value) || "stock";
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
    try {
      const id = form.dataset.alertId;
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
      await load(rootEl);
    } catch (error) {
      st.error = t("alerts.errors.saveFailed");
      render(rootEl);
    }
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
    load(rootEl, symbol, assetType, { skipRender: true });
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

  return {
    mount: mount,
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
