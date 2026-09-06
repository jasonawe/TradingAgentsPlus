(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TradingAgentsNotes = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function t(key, vars) {
    const i18n = root.TradingAgentsI18n;
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

  function formMarkup(existing) {
    const isEdit = !!existing;
    const id = isEdit ? existing.id : "";
    const body = isEdit ? existing.body_md : "";
    return [
      '<form class="note-form" data-note-form' + (isEdit ? ' data-note-id="' + escapeAttr(id) + '"' : '') + '>',
      '  <textarea class="note-form-textarea" name="body_md" rows="6" placeholder="' + escapeAttr(t("notes.placeholder")) + '" required>' + escapeHtml(body) + '</textarea>',
      '  <div class="note-form-toolbar">',
      '    <button type="button" class="text-button" data-note-action="preview">' + escapeHtml(t("notes.preview")) + '</button>',
      '    <div class="note-form-actions">',
      '      <button type="button" class="button button-secondary" data-note-action="cancel">' + escapeHtml(t("notes.cancel")) + '</button>',
      '      <button type="submit" class="button button-primary">' + escapeHtml(t("notes.save")) + '</button>',
      '    </div>',
      '  </div>',
      '  <div class="note-form-preview markdown-body" hidden></div>',
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
        if (typeof root.confirm === "function" && !root.confirm(t("notes.deleteConfirm"))) return;
        deleteNote(rootEl, id);
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
      const textarea = form.querySelector(".note-form-textarea");
      const body = textarea ? textarea.value.trim() : "";
      if (!body) {
        st.error = t("notes.errors.emptyBody");
        render(rootEl);
        return;
      }
      const id = form.dataset.noteId || null;
      submitNote(rootEl, id, body);
    });
  }

  function showForm(rootEl, existing) {
    const placeholder = rootEl.querySelector(".note-form");
    if (placeholder) placeholder.remove();
    const html = formMarkup(existing || null);
    rootEl.insertAdjacentHTML("afterbegin", html);
    const textarea = rootEl.querySelector(".note-form-textarea");
    if (textarea) textarea.focus();
  }

  async function submitNote(rootEl, id, body) {
    const st = panelState(rootEl);
    st.error = "";
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
      await load(rootEl);
    } catch (error) {
      st.error = t("notes.errors.saveFailed");
      render(rootEl);
    }
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

  return {
    renderMarkdown: renderMarkdown,
    mount: mount,
    load: load,
    refresh: function (rootEl) { return load(rootEl); },
    formatDateTime: formatDateTime,
  };
});
