(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  try { window.TradingAgentsNotes = api; } catch (_) { /* window frozen */ }
  try { if (typeof __TA_MODULES__ !== "undefined") __TA_MODULES__.TradingAgentsNotes = api; } catch (_) {}
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

  // Tiny safe-ish Markdown renderer:
  // supports # / ## / ### / #### headings, **bold**, *italic*, `code`,
  // - / * / 1. lists, [text](url) links, > quotes, paragraph breaks.
  // All user input is HTML-escaped before formatting, so there is no XSS surface
  // from note bodies — only URLs from explicit [text](url) become anchors.
  function renderMarkdown(md) {
    const src = String(md == null ? "" : md);
    const lines = src.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;
    function flushParagraph(buf) {
      if (!buf.length) return;
      const text = buf.join(" ");
      out.push("<p>" + inline(text) + "</p>");
      buf.length = 0;
    }
    function inline(text) {
      let safe = text;
      safe = safe.replace(/`([^`]+)`/g, function (_, c) { return "<code>" + escapeHtml(c) + "</code>"; });
      safe = safe.replace(/\*\*([^*]+)\*\*/g, function (_, c) { return "<strong>" + escapeHtml(c) + "</strong>"; });
      safe = safe.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, function (_, pre, c) { return pre + "<em>" + escapeHtml(c) + "</em>"; });
      safe = safe.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_, label, url) {
        return '<a href="' + escapeAttr(url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(label) + "</a>";
      });
      return escapeHtml(safe)
        .replace(/&lt;code&gt;/g, "<code>")
        .replace(/&lt;\/code&gt;/g, "</code>")
        .replace(/&lt;strong&gt;/g, "<strong>")
        .replace(/&lt;\/strong&gt;/g, "</strong>")
        .replace(/&lt;em&gt;/g, "<em>")
        .replace(/&lt;\/em&gt;/g, "</em>")
        .replace(/&lt;a /g, "<a ")
        .replace(/&quot;/g, '"')
        .replace(/&#039;/g, "'");
    }
    let paragraph = [];
    while (i < lines.length) {
      const raw = lines[i];
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { flushParagraph(paragraph); i++; continue; }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushParagraph(paragraph);
        const level = heading[1].length;
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        i++;
        continue;
      }
      if (/^>\s+/.test(line)) {
        flushParagraph(paragraph);
        const quote = [];
        while (i < lines.length && /^>\s+/.test(lines[i])) {
          quote.push(lines[i].replace(/^>\s+/, ""));
          i++;
        }
        out.push("<blockquote>" + inline(quote.join(" ")) + "</blockquote>");
        continue;
      }
      const ul = line.match(/^[-*]\s+(.*)$/);
      if (ul) {
        flushParagraph(paragraph);
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^[-*]\s+(.*)$/);
          if (!m) break;
          items.push("<li>" + inline(m[1]) + "</li>");
          i++;
        }
        out.push("<ul>" + items.join("") + "</ul>");
        continue;
      }
      const ol = line.match(/^\d+\.\s+(.*)$/);
      if (ol) {
        flushParagraph(paragraph);
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^\d+\.\s+(.*)$/);
          if (!m) break;
          items.push("<li>" + inline(m[1]) + "</li>");
          i++;
        }
        out.push("<ol>" + items.join("") + "</ol>");
        continue;
      }
      paragraph.push(line);
      i++;
    }
    flushParagraph(paragraph);
    return out.join("");
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      return d.toLocaleString();
    } catch (_) { return ""; }
  }

  function noteMarkup(note) {
    const updated = formatDateTime(note.updated_at || note.created_at);
    const created = formatDateTime(note.created_at);
    const updatedLabel = note.updated_at && note.updated_at !== note.created_at
      ? t("notes.updatedAt") + " " + escapeHtml(updated)
      : t("notes.createdAt") + " " + escapeHtml(created);
    return [
      '<article class="note-card" data-note-id="' + escapeAttr(note.id) + '">',
      '  <header class="note-card-header">',
      '    <span class="note-card-timestamp">' + updatedLabel + '</span>',
      '    <div class="note-card-actions">',
      '      <button type="button" class="text-button" data-note-action="edit" data-note-id="' + escapeAttr(note.id) + '">' + escapeHtml(t("notes.edit")) + '</button>',
      '      <button type="button" class="text-button" data-note-action="delete" data-note-id="' + escapeAttr(note.id) + '">' + escapeHtml(t("notes.delete")) + '</button>',
      '    </div>',
      '  </header>',
      '  <div class="note-card-body markdown-body">' + renderMarkdown(note.body_md) + '</div>',
      '</article>',
    ].join("");
  }

  function formMarkup(existing, defaultSymbol, defaultAssetType) {
    const isEdit = !!existing;
    const id = isEdit ? existing.id : "";
    const body = isEdit ? existing.body_md : "";
    const symbol = isEdit ? existing.symbol : (defaultSymbol || "");
    const assetType = isEdit ? (existing.asset_type || "stock") : (defaultAssetType || "stock");
    const showSymbolInput = !isEdit && !symbol;
    const showAssetTypeInput = !isEdit && !assetType;
    return [
      '<form class="note-form" data-note-form' + (isEdit ? ' data-note-id="' + escapeAttr(id) + '"' : '') + ' data-form-symbol="' + escapeAttr(symbol) + '" data-form-asset-type="' + escapeAttr(assetType) + '">',
      (showSymbolInput ? '  <div class="form-field"><label for="note-symbol"><span>' + escapeHtml(t("notes.symbolInput")) + '</span></label><input id="note-symbol" type="text" name="symbol" value="" placeholder="' + escapeAttr(t("notes.symbolInputPlaceholder")) + '" required></div>' : ''),
      (showAssetTypeInput ? '  <div class="form-field"><label for="note-asset-type"><span>' + escapeHtml(t("notes.assetTypeInput")) + '</span></label><select id="note-asset-type" name="asset_type"><option value="stock" selected>' + escapeHtml(t("assets.stock")) + '</option><option value="crypto">' + escapeHtml(t("assets.crypto")) + '</option></select></div>' : ''),
      '  <div class="form-field">',
      '    <div class="note-form-toolbar">',
      '      <label for="note-body"><span>' + escapeHtml(t("notes.body")) + '</span></label>',
      '      <button type="button" class="text-button note-preview-btn" data-note-action="preview">' + escapeHtml(t("notes.preview")) + '</button>',
      '    </div>',
      '    <textarea id="note-body" class="note-form-textarea" name="body_md" rows="6" placeholder="' + escapeAttr(t("notes.placeholder")) + '" required>' + escapeHtml(body) + '</textarea>',
      '    <div class="note-form-preview markdown-body" hidden></div>',
      '  </div>',
      '</form>',
    ].join("");
  }

  function panelState(rootEl) {
    let st = rootEl.__notesState;
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
      rootEl.__notesState = st;
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
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("notes.emptyShort")) + '</p>';
      return;
    }
    rootEl.innerHTML = st.items.map(noteMarkup).join("");
  }


  async function handleNoteSubmit(form, rootEl) {
    const textarea = form.querySelector(".note-form-textarea");
    const body = textarea ? textarea.value.trim() : "";
    if (!body) {
      showFormError(form, t("notes.errors.emptyBody"));
      return false;
    }
    const id = form.dataset.noteId || null;
    // Resolve symbol/assetType: prefer the visible form inputs (used when
    // adding from the unified list), then the data-form-* attributes
    // (set by showForm), then the panel state (asset detail view).
    let symbol = (form.querySelector('input[name="symbol"]') && form.querySelector('input[name="symbol"]').value.trim()) || form.getAttribute("data-form-symbol") || "";
    let assetType = (form.querySelector('select[name="asset_type"]') && form.querySelector('select[name="asset_type"]').value) || form.getAttribute("data-form-asset-type") || "stock";
    if (rootEl) {
      const st = panelState(rootEl);
      if (!symbol) symbol = st.symbol || "";
      if (!assetType) assetType = st.assetType || "stock";
    }
    const result = await submitNoteWith(symbol, assetType, id, body);
    if (result === true) return true;
    showFormError(form, result);
    return false;
  }

  async function submitNoteWith(symbol, assetType, id, body) {
    try {
      if (id) {
        await apiFetch("/api/notes/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify({ body_md: body }),
        });
      } else {
        await apiFetch("/api/notes", {
          method: "POST",
          body: JSON.stringify({ symbol: symbol, asset_type: assetType, body_md: body }),
        });
      }
      return true;
    } catch (error) {
      return error.message || t("notes.errors.saveFailed");
    }
  }

  function attachHandlers(rootEl) {
    if (rootEl.__notesHandlersAttached) return;
    rootEl.__notesHandlersAttached = true;
    rootEl.addEventListener("click", function (event) {
      const editBtn = event.target.closest('[data-note-action="edit"]');
      if (editBtn) {
        const id = editBtn.dataset.noteId;
        const st = panelState(rootEl);
        const note = st.items.find(function (n) { return n.id === id; });
        if (note) showForm(rootEl, note);
        return;
      }
      const deleteBtn = event.target.closest('[data-note-action="delete"]');
      if (deleteBtn) {
        const id = deleteBtn.dataset.noteId;
        const app = (typeof window !== "undefined" && window.TradingAgentsApp) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsApp);
        if (app && app.openConfirmModal) {
          app.openConfirmModal({
            title: t("notes.deleteTitle") || t("notes.delete"),
            message: t("notes.deleteConfirm"),
            confirmText: t("notes.delete"),
            cancelText: t("notes.cancel"),
            danger: true
          }).then(function (proceed) {
            if (proceed) deleteNote(rootEl, id);
          });
        } else {
          if (typeof window.confirm === "function" && !window.confirm(t("notes.deleteConfirm"))) return;
          deleteNote(rootEl, id);
        }
        return;
      }
      const cancelBtn = event.target.closest('[data-note-action="cancel"]');
      if (cancelBtn) {
        const form = cancelBtn.closest(".note-form");
        if (form) form.remove();
        return;
      }
      const previewBtn = event.target.closest('[data-note-action="preview"]');
      if (previewBtn) {
        const form = previewBtn.closest(".note-form");
        if (!form) return;
        const textarea = form.querySelector(".note-form-textarea");
        const preview = form.querySelector(".note-form-preview");
        if (!textarea || !preview) return;
        if (preview.hidden) {
          preview.innerHTML = renderMarkdown(textarea.value);
          preview.hidden = false;
          previewBtn.textContent = t("notes.edit2");
        } else {
          preview.hidden = true;
          preview.innerHTML = "";
          previewBtn.textContent = t("notes.preview");
        }
        return;
      }
    });
    rootEl.addEventListener("submit", function (event) {
      const form = event.target.closest(".note-form");
      if (!form || !rootEl.contains(form)) return;
      event.preventDefault();
      handleNoteSubmit(form, rootEl);
    });
    // Modal form submit (form lives outside rootEl). Wire once per process.
    if (!document.__notesModalSubmitBound) {
      document.__notesModalSubmitBound = true;
      document.addEventListener("submit", function (event) {
        const form = event.target.closest && event.target.closest(".note-form");
        if (!form) return;
        if (rootEl && rootEl.contains(form)) return; // handled by delegated above
        event.preventDefault();
        handleNoteSubmit(form, null);
      });
    }
  }

  async function showForm(rootEl, existing) {
    const st = panelState(rootEl);
    if (existing) { st.symbol = existing.symbol; st.assetType = existing.asset_type || "stock"; }
    const html = formMarkup(existing || null);
    const title = existing ? t("notes.editTitle") || t("notes.edit") : t("notes.formTitle") || t("notes.add");
    const app = (typeof window !== "undefined" && window.TradingAgentsApp) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsApp);
    if (!app || !app.openFormModal) { return; }
    const result = await app.openFormModal({
      title: title,
      bodyHtml: html,
      submitText: t("notes.save"),
      cancelText: t("notes.cancel"),
      width: "wide",
      icon: "&#128221;"
    });
    if (rootEl.id === "notes-all-list") {
      await loadAll(rootEl);
    } else {
      await load(rootEl);
    }
  }

  async function submitNote(rootEl, id, body) {
    const st = rootEl ? panelState(rootEl) : { symbol: "", assetType: "stock" };
    try {
      if (id) {
        await apiFetch("/api/notes/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify({ body_md: body }),
        });
      } else {
        await apiFetch("/api/notes", {
          method: "POST",
          body: JSON.stringify({ symbol: st.symbol, asset_type: st.assetType, body_md: body }),
        });
      }
      return true;
    } catch (error) {
      return error.message || t("notes.errors.saveFailed");
    }
  }

  function showFormError(form, msg) {
    let err = form.querySelector(".note-form-error");
    if (!err) {
      err = document.createElement("p");
      err.className = "note-form-error form-error";
      err.style.cssText = "color: #ef4444; font-size: 13px; margin: 0;";
      const actions = form.querySelector(".note-form-actions");
      if (actions) form.insertBefore(err, actions);
      else form.appendChild(err);
    }
    err.textContent = msg;
  }

  async function deleteNote(rootEl, id) {
    const st = panelState(rootEl);
    st.error = "";
    try {
      await apiFetch("/api/notes/" + encodeURIComponent(id), { method: "DELETE" });
      await load(rootEl);
    } catch (error) {
      st.error = t("notes.errors.deleteFailed");
      render(rootEl);
    }
  }

  async function load(rootEl, symbol, assetType) {
    const st = panelState(rootEl);
    if (typeof symbol === "string") st.symbol = symbol;
    if (typeof assetType === "string") st.assetType = assetType;
    if (!st.symbol) {
      st.items = [];
      render(rootEl);
      return;
    }
    st.loading = true;
    st.error = "";
    try {
      const response = await apiFetch(
        "/api/notes?symbol=" + encodeURIComponent(st.symbol) +
        "&asset_type=" + encodeURIComponent(st.assetType || "stock")
      );
      st.items = (response && response.items) || [];
    } catch (error) {
      st.error = t("notes.errors.loadFailed");
      st.items = [];
    } finally {
      st.loading = false;
    }
    render(rootEl);
  }

  function mount(rootEl, symbol, assetType) {
    if (!rootEl) return;
    attachHandlers(rootEl);
    // Each "new note" button carries a `data-notes-target` selector that points
    // at its own panel. Bind once per (button, panel) pair so asset detail and
    // report detail panels can coexist without one panel "stealing" the button.
    document.querySelectorAll('[data-notes-add]').forEach(function (btn) {
      if (btn.__notesAddBound) return;
      const targetSelector = btn.dataset.notesTarget;
      if (!targetSelector) return;
      const targetEl = document.querySelector(targetSelector);
      if (!targetEl || targetEl !== rootEl) return;
      btn.__notesAddBound = true;
      btn.addEventListener("click", function () {
        const sym = btn.dataset.symbol || symbol;
        const at = btn.dataset.assetType || assetType || "stock";
        if (sym !== panelState(rootEl).symbol || at !== panelState(rootEl).assetType) {
          load(rootEl, sym, at).then(function () { showForm(rootEl, null); });
        } else {
          showForm(rootEl, null);
        }
      });
    });
    load(rootEl, symbol, assetType);
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
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("notes.loading")) + "</p>";
    }
    try {
      const params = new URLSearchParams();
      params.set("limit", String(st.pageSize || 20));
      params.set("offset", String((st.page || 1) - 1) * (st.pageSize || 20));
      const response = await apiFetch("/api/notes?" + params.toString());
      st.items = (response && response.items) || [];
      st.total = (response && response.total) || 0;
      st.limit = (response && response.limit) || (st.pageSize || 20);
      st.offset = (response && response.offset) || 0;
      // Render immediately with whatever identities are already cached.
      st.identity = st.identity || {};
      if (!skipRender) renderAll(rootEl);
      // Then enrich identities in the background.
      const symbols = Array.from(new Set(st.items.map(function (i) { return i.symbol + "|" + i.asset_type; })));
      fetchIdentities(symbols).then(function (identities) {
        st.identity = identities || {};
        if (!skipRender) renderAll(rootEl);
      }).catch(function () { /* identities are best-effort */ });
    } catch (error) {
      st.error = t("notes.errors.loadFailed");
      st.items = [];
      st.total = 0;
      if (!skipRender) renderAll(rootEl);
    } finally {
      st.loading = false;
    }
  }

  const __noteIdentityCache = {};
  const NOTE_IDENTITY_TIMEOUT_MS = 20000;
  function fetchWithTimeout(url, ms) {
    return new Promise(function (resolve, reject) {
      const ctrl = new AbortController();
      const timer = setTimeout(function () { ctrl.abort(); reject(new Error("timeout")); }, ms);
      fetch(url, { signal: ctrl.signal }).then(function (r) { clearTimeout(timer); resolve(r); }).catch(function (e) { clearTimeout(timer); reject(e); });
    });
  }
  async function fetchIdentities(symbolAssetPairs) {
    const result = Object.assign({}, __noteIdentityCache);
    const pairs = (symbolAssetPairs || []).filter(Boolean);
    // Prefer the shared quote cache so a single /api/quotes call serves
    // the watchlist, scheduled jobs, alerts, and notes pages.
    const quotesModule = (typeof window !== "undefined" && window.TradingAgentsQuotes) || (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsQuotes);
    const grouped = {};
    pairs.forEach(function (p) {
      if (__noteIdentityCache[p]) return;
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
              const r = await fetchWithTimeout(url, NOTE_IDENTITY_TIMEOUT_MS);
              if (!r.ok) throw new Error("status " + r.status);
              return r.json();
            })();
        // Race the quote fetch against a timeout so a slow upstream can't
        // leave the UI stuck on "加载中…".
        let timer;
        const timeout = new Promise(function (_, reject) {
          timer = setTimeout(function () { reject(new Error("identity fetch timeout")); }, NOTE_IDENTITY_TIMEOUT_MS);
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
            __noteIdentityCache[sym + "|" + assetType] = {
              name: quote.asset_name || quote.name || "",
              name_zh: quote.asset_name_zh || quote.name_zh || "",
              exchange: quote.exchange || "",
              exchange_name_zh: quote.exchange_name_zh || "",
            };
          }
        });
        syms.forEach(function (sym) {
          if (!__noteIdentityCache[sym + "|" + assetType]) {
            __noteIdentityCache[sym + "|" + assetType] = { name: sym, exchange: "" };
          }
        });
      } catch (_) {
        syms.forEach(function (sym) {
          if (!__noteIdentityCache[sym + "|" + assetType]) {
            __noteIdentityCache[sym + "|" + assetType] = { name: sym, exchange: "" };
          }
        });
      }
    }));
    pairs.forEach(function (p) { result[p] = __noteIdentityCache[p] || { name: p.split("|")[0] }; });
    return result;
  }

  function renderAll(rootEl) {
    const st = panelState(rootEl);
    // search filter (client-side, applied after pagination)
    const searchInput = document.getElementById("notes-search");
    const query = (searchInput && searchInput.value || "").trim().toLowerCase();
    const items = query ? st.items.filter(function (n) { return String(n.body_md || "").toLowerCase().indexOf(query) >= 0; }) : st.items;
    if (st.error) {
      rootEl.innerHTML = '<p class="error-text">' + escapeHtml(st.error) + "</p>";
      return;
    }
    if (!items.length) {
      rootEl.innerHTML = '<p class="muted">' + escapeHtml(t("notes.noNotes")) + "</p>" + paginationMarkup(st, "notes");
      bindPagination(rootEl, "notes");
      return;
    }
    rootEl.innerHTML = items.map(function (item) { return noteAllMarkup(item, st.identity); }).join("") + paginationMarkup(st, "notes");
    bindPagination(rootEl, "notes");
  }

  function noteAllMarkup(note, identityMap) {
    const preview = String(note.body_md || "").split("\n").slice(0, 4).join("\n");
    const assetHref = "/assets/" + encodeURIComponent(note.symbol);
    const id = (identityMap && identityMap[note.symbol + "|" + (note.asset_type || "stock")]) || {};
    const assetName = id.name_zh || id.name || "";
    const assetExchange = id.exchange || "";
    return [
      '<article class="note-card note-card-all" data-note-id="' + escapeAttr(note.id) + '">',
      '  <header class="note-card-header">',
      '    <div class="note-card-asset">',
      '      <a class="note-card-symbol" href="' + escapeAttr(assetHref) + '" data-note-symbol-link>' + escapeHtml(note.symbol) + "</a>",
      '<span class="note-card-asset-name ' + (assetName ? "is-loaded" : "is-loading") + '">' + (assetName ? escapeHtml(assetName) + (assetExchange ? " · " + escapeHtml(assetExchange) : "") : "加载中…") + "</span>",
      "    </div>",
      '    <span class="note-card-timestamp">' + escapeHtml(formatDateTime(note.updated_at || note.created_at)) + "</span>",
      '    <div class="note-card-actions">',
      '      <button type="button" class="text-button" data-note-action="edit" data-note-id="' + escapeAttr(note.id) + '">' + escapeHtml(t("notes.edit")) + "</button>",
      '      <button type="button" class="text-button" data-note-action="delete" data-note-id="' + escapeAttr(note.id) + '">' + escapeHtml(t("notes.delete")) + "</button>",
      "    </div>",
      "  </header>",
      '  <div class="note-card-body markdown-body">' + renderMarkdown(preview) + "</div>",
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
    attachHandlers(rootEl);
    // "新建笔记" button opens the create modal
    const newBtn = document.getElementById("notes-new-button");
    if (newBtn && !newBtn.__notesNewBound) {
      newBtn.__notesNewBound = true;
      newBtn.addEventListener("click", function () {
        // For new notes from unified list, symbol/assetType come from the
        // form (the user types them in).
        showForm(rootEl, null, "", "");
      });
    }
    // Symbol link: SPA navigate
    rootEl.addEventListener("click", function (event) {
      const link = event.target.closest("[data-note-symbol-link]");
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
    // Search box
    const search = document.getElementById("notes-search");
    if (search && !search.__notesAllBound) {
      search.__notesAllBound = true;
      search.addEventListener("input", function () {
        const st = panelState(rootEl);
        renderAll(rootEl);
      });
    }
    loadAll(rootEl);
  }

  return {
    renderMarkdown: renderMarkdown,
    showForm: showForm,
    handleNoteSubmit: handleNoteSubmit,
    mountAll: mountAll,
    loadAll: loadAll,
    renderAll: renderAll,
    mount: mount,
    load: load,
    refresh: function (rootEl) { return load(rootEl); },
    formatDateTime: formatDateTime,
  };
});
