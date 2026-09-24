/* P8 — Harness Chat Page (/harness route)
   Calls /api/harness/chat and renders SSE events from the harness
   orchestrator: plan → execute → observe → verify → synthesize → L3 judge.
   Independent from the legacy Stage C agent drawer (which uses
   /api/agent/chat/stream).
*/
(() => {
  "use strict";

  const state = {
    messagesEl: null,
    inputEl: null,
    sendBtn: null,
    formEl: null,
    clearBtn: null,
    grantAllBox: null,
    busy: false,
    sessionId: null,
    // §Step 19 — multi-session state. We persist the current
    // sessionId to localStorage so a refresh keeps the user in the
    // same conversation, and we cache the lightweight session list
    // for the switcher dropdown.
    sessions: [], // [{id, created_at, last_active, message_count, status}]
    sessionSelectEl: null,
    newSessionBtnEl: null,
    deleteSessionBtnEl: null,
    // 当前累积的 reasoning trace(下一个 tool_call/answer_verified 触发时收尾)
    traceEl: null,
    traceContent: "",
    // §0.4.33 — todo checklist element + last items (live progress).
    todoEl: null,
    todoItems: [],
  };

  const SESSION_STORAGE_KEY = "ta.harness.sessionId";

  const SUGGESTIONS = [
    "招商银行 600036 现在多少钱?",
    "深度分析 600036.SS 估值",
    "对比 600036.SS 和 600000.SS",
    "我的关注列表里现在有什么?",
    "算一下 600036.SS 的 alpha158 因子",
  ];

  // ─────────────────────────────────────────────────
  // init — called by app.js
  // ─────────────────────────────────────────────────

  function init() {
    state.messagesEl = document.getElementById("harness-messages");
    state.inputEl = document.getElementById("harness-input");
    state.sendBtn = document.getElementById("harness-send-btn");
    state.formEl = document.getElementById("harness-form");
    state.clearBtn = document.getElementById("harness-clear-btn");

    if (!state.messagesEl || !state.inputEl || !state.sendBtn || !state.formEl) {
      console.warn("[HarnessChat] DOM elements missing");
      return;
    }

    // §0.4.24 — delegated retry-button click handler. Any
    // error-card emitted by ``appendToolResult`` carries
    // ``data-retryable="1"`` and a sibling user message bubble; we
    // re-send the previous user message which makes the backend run
    // the same tool again (Tier 1 short-circuit re-runs the lookup;
    // Tier 2 re-plans with cached context).
    if (state.messagesEl) {
      state.messagesEl.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest && ev.target.closest('[data-action="retry-tool"]');
        if (!btn) return;
        ev.preventDefault();
        if (state.busy) return;
        btn.disabled = true;
        btn.textContent = "🔄 重试中…";
        retryLastTool();
      });
    }

    state.formEl.addEventListener("submit", (e) => {
      e.preventDefault();
      sendMessage();
    });
    state.inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    state.grantAllBox = document.getElementById("harness-grant-all");
    if (state.clearBtn) {
      state.clearBtn.addEventListener("click", clearConversation);
    }
    // §Step 19 — session switcher wiring
    state.sessionSelectEl = document.getElementById("harness-session-select");
    state.newSessionBtnEl = document.getElementById("harness-new-session-btn");
    state.deleteSessionBtnEl = document.getElementById("harness-delete-session-btn");
    state.forkSessionBtnEl = document.getElementById("harness-fork-session-btn");
    if (state.sessionSelectEl) {
      state.sessionSelectEl.addEventListener("change", (e) => {
        switchSession(e.target.value);
      });
    }
    if (state.newSessionBtnEl) {
      state.newSessionBtnEl.addEventListener("click", createNewSession);
    }
    if (state.deleteSessionBtnEl) {
      state.deleteSessionBtnEl.addEventListener("click", deleteCurrentSession);
    }
    if (state.forkSessionBtnEl) {
      state.forkSessionBtnEl.addEventListener("click", forkCurrentSession);
    }
    /* §Harness-redesign — sidebar search filter + tab switcher for inspector. */
    const searchEl = document.getElementById("harness-session-search");
    if (searchEl) {
      searchEl.addEventListener("input", () => renderSessionPicker());
    }
    /* §Step 24 — sidebar collapse/expand. Default is **expanded**
       so first-time visitors land on /harness with the session list
       visible (the list is the primary navigation on this page — if
       users don't see it they think the page is broken). Returning
       users keep whatever they last chose via localStorage; the
       explicit "0" value means they collapsed it and we honour that
       (the rail-with-toggle makes re-opening trivial). */
    const sidebarToggle = document.getElementById("harness-sidebar-toggle");
    const layout = document.querySelector(".harness-layout");
    function setSidebarExpanded(expanded, _userInitiated) {
      if (!layout) return;
      layout.classList.toggle("is-sidebar-expanded", !!expanded);
      if (sidebarToggle) sidebarToggle.textContent = expanded ? "‹" : "›";
      // §0.4.14 — only persist the user’s explicit choice.
      try {
        localStorage.setItem("ta.harness.sidebarExpanded", expanded ? "1" : "0");
        if (_userInitiated) localStorage.setItem("ta.harness.userChoseSidebar", "1");
      } catch (_) {}
    }
    // §0.4.19.fix — apply the default-expanded decision up-front, before
    // the toggle button check. Previously the whole sidebar-expand block
    // was gated on ``sidebarToggle && layout``, but the SPA /harness view
    // (index.html §harness-view) doesn't ship a #harness-sidebar-toggle
    // element — that DOM only exists on the standalone harness.html
    // route. With the toggle absent, the block was skipped and the
    // layout's ``is-sidebar-expanded`` class never got set, so the
    // session list stayed hidden even on the very first visit. We now:
    //   • Always compute the default-expanded decision from localStorage.
    //   • Always call ``setSidebarExpanded`` so the layout class lands.
    //   • Only bind the toggle click handler when the toggle DOM exists
    //     (i.e. on the standalone /harness route, not the SPA one).
    let stored = null;
    let userChose = null;
    try {
      stored = localStorage.getItem("ta.harness.sidebarExpanded");
      userChose = localStorage.getItem("ta.harness.userChoseSidebar");
    } catch (_) {}
    // Default expanded unless the user explicitly clicked the toggle
    // AND chose to collapse it. Returning users who never clicked the
    // toggle get the un-stuck expanded state (handles legacy
    // ``sidebarExpanded='0'`` written by older builds).
    let initialExpanded = true;
    if (userChose === "1" && stored === "0") {
      initialExpanded = false;
    }
    setSidebarExpanded(initialExpanded, false);
    if (sidebarToggle && layout) {
      sidebarToggle.addEventListener("click", () => {
        setSidebarExpanded(!layout.classList.contains("is-sidebar-expanded"), true);
      });
    }
    const railNew = document.getElementById("harness-rail-new");
    if (railNew) railNew.addEventListener("click", () => createNewSession());
    const railExpand = document.getElementById("harness-rail-expand");
    if (railExpand) railExpand.addEventListener("click", () => setSidebarExpanded(true, true));
    const railCount = document.getElementById("harness-rail-count");
    function updateRailCount() {
      if (!railCount) return;
      railCount.textContent = String((state.sessions || []).length);
    }
    const railActive = document.getElementById("harness-rail-active-title");
    function updateRailActive() {
      if (!railActive) return;
      const sid = state.sessionId || "";
      const s = (state.sessions || []).find((x) => x.id === sid);
      const title = s ? sessionItemTitle(s) : (sid ? "当前会话" : "暂无活动会话");
      const isPending = sid.startsWith("pending-");
      railActive.textContent = title;
      railActive.title = title;
      railActive.dataset.tip = title;
      railActive.classList.toggle("is-empty", !s || isPending);
    }
    if (railActive) {
      railActive.addEventListener("click", () => setSidebarExpanded(true, true));
    }
    // Patch renderSessionPicker so it also refreshes the rail counter.
    const _origRender = renderSessionPicker;
    renderSessionPicker = function patchedRender() {
      _origRender.apply(this, arguments);
      updateRailCount();
      updateRailActive();
    };
    const inspectorToggle = document.getElementById("harness-inspector-toggle");
    if (inspectorToggle) {
      const _l = document.querySelector(".harness-layout");
      inspectorToggle.addEventListener("click", () => {
        _l?.classList.toggle("is-inspector-expanded");
      });
    }
    document.querySelectorAll(".harness-inspector-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const target = tab.dataset.tab;
        document.querySelectorAll(".harness-inspector-tab").forEach((t) =>
          t.classList.toggle("is-active", t === tab)
        );
        document.querySelectorAll(".harness-inspector-pane").forEach((p) =>
          p.classList.toggle("is-active", p.dataset.pane === target)
        );
      });
    });
    // Kick the initial session picker + reconcile active session id.
    reconcileActiveSession();
    if (state.grantAllBox) {
      state.grantAllBox.addEventListener("change", onGrantAllChange);
      // restore from server (so refreshes don't reset the toggle)
      syncGrantAllFromServer();
    }

    renderWelcome();

    if (typeof window.__TA_MODULES__ !== "undefined") {
      window.__TA_MODULES__.TradingAgentsHarness = {
        open: () => navigateHarness(),
        send: sendMessage,
        clear: clearConversation,
      };
    }
  }

  function navigateHarness() {
    if (typeof window.TradingAgentsApp !== "undefined") {
      window.TradingAgentsApp.navigate("harness");
    } else if (typeof __TA_MODULES__ !== "undefined" && __TA_MODULES__.TradingAgentsApp) {
      __TA_MODULES__.TradingAgentsApp.navigate("harness");
    } else {
      window.location.href = "/harness";
    }
  }

  // ─────────────────────────────────────────────────
  // Session 管理(每次清空换新 session,简单实现)
  // ─────────────────────────────────────────────────

  /**
   * Return a stable session id. Synchronous path: we only ever read
   * from `state.sessionId` (which is reconciled at boot via
   * `reconcileActiveSession`). If something asks for a session before
   * reconciliation finishes, fall back to a deterministic placeholder
   * and let `reconcileActiveSession` replace it once loadSessions +
   * the server round-trip completes.
   *
   * We no longer mint `hc-*` ids on the client. Every session id the
   * UI uses must come from the server, otherwise the list/messages
   * endpoints can 404 and switching silently loses history.
   */
  function ensureSession() {
    if (state.sessionId) return state.sessionId;
    try {
      const cached = window.localStorage.getItem(SESSION_STORAGE_KEY);
      if (cached) {
        state.sessionId = cached;
        return state.sessionId;
      }
    } catch (e) { /* private mode */ }
    // Synchronous fallback only — never persists. The async
    // reconcileActiveSession() will replace it as soon as the server
    // round-trip finishes.
    state.sessionId = "pending-" + Date.now().toString(36);
    return state.sessionId;
  }

  function persistSessionId() {
    try {
      if (state.sessionId) {
        window.localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
      }
    } catch (e) { /* ignore */ }
  }

  function clearConversation() {
    if (state.busy) return;
    // §Step 19 — "清空" keeps the active session, just wipes the
    // visible messages. To start a *new* session use the 新建按钮.
    state.messagesEl.innerHTML = "";
    renderWelcome();
  }

  // ─────────────────────────────────────────────────
  // §Step 19 — session list / switcher
  // ─────────────────────────────────────────────────
  async function loadSessions() {
    try {
      const r = await fetch("/api/harness/sessions?status=active&limit=50");
      if (!r.ok) return;
      const body = await r.json();
      state.sessions = Array.isArray(body.sessions) ? body.sessions : [];
      renderSessionPicker();
    } catch (e) {
      console.warn("[HarnessChat] loadSessions failed", e);
    }
  }

  /**
   * Boot-time reconciliation: pick the right `state.sessionId` and
   * inject it into `state.sessions` so the sidebar always lists the
   * active session. Order of precedence:
   *   1. existing state.sessionId if it's already in state.sessions
   *   2. cached localStorage value if it's still on the server
   *      (even when the list endpoint didn't include it — e.g. older
   *      `hc-*` sessions created during the temp-id era)
   *   3. most-recent server session
   *   4. freshly POSTed server session
   * After this resolves, `state.sessionId` is guaranteed to be a real
   * server id and present in `state.sessions`.
   */
  async function reconcileActiveSession() {
    await loadSessions();
    const cached = (() => {
      try { return window.localStorage.getItem(SESSION_STORAGE_KEY); } catch (_) { return null; }
    })();
    const candidates = [state.sessionId, cached].filter(Boolean);
    for (const sid of candidates) {
      if (state.sessions.find((s) => s.id === sid)) {
        state.sessionId = sid;
        persistSessionId();
        renderSessionPicker();
        updateMainHeader();
        return state.sessionId;
      }
      // not in list — probe the server directly
      try {
        const r = await fetch(`/api/harness/sessions/${encodeURIComponent(sid)}`);
        if (r.ok) {
          state.sessionId = sid;
          const body = await r.json();
          state.sessions = [body, ...state.sessions.filter((s) => s.id !== sid)];
          persistSessionId();
          renderSessionPicker();
          updateMainHeader();
          return state.sessionId;
        }
      } catch (_) { /* keep probing */ }
    }
    // fall back to most-recent server session
    if (state.sessions.length > 0) {
      state.sessionId = state.sessions[0].id;
      persistSessionId();
      renderSessionPicker();
      updateMainHeader();
      return state.sessionId;
    }
    // last resort: create a brand-new server session
    try {
      const r = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "新对话" }),
      });
      if (r.ok) {
        const body = await r.json();
        if (body.session_id) {
          state.sessionId = body.session_id;
          await loadSessions();
          renderSessionPicker();
          updateMainHeader();
          persistSessionId();
          return state.sessionId;
        }
      }
    } catch (e) {
      console.warn("[HarnessChat] reconcileActiveSession POST failed", e);
    }
    return state.sessionId;
  }

  async function createNewSession() {
    if (state.busy) return;
    try {
      const r = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "新会话" }),
      });
      if (!r.ok) {
        console.warn("[HarnessChat] createNewSession failed", r.status);
        return;
      }
      const body = await r.json();
      const sid = body.session_id;
      if (!sid) return;
      state.sessionId = sid;
      persistSessionId();
      state.messagesEl.innerHTML = "";
      renderWelcome();
      await loadSessions();
    } catch (e) {
      console.warn("[HarnessChat] createNewSession error", e);
    }
  }

  async function switchSession(sid) {
    if (!sid || sid === state.sessionId) return;
    if (state.busy) {
      // Show a transient hint instead of an alert so users can queue
      // their intent — we just won't act on it until the current
      // turn finishes.
      showHarnessToast("当前正在生成回复,请稍候再切换");
      return;
    }
    setSessionSwitching(true);
    try {
      state.sessionId = sid;
      persistSessionId();
      state.messagesEl.innerHTML = "";
      renderSessionPicker();
      updateMainHeader();
      await loadHistory();
    } finally {
      setSessionSwitching(false);
    }
  }

  /**
   * Lightweight visual feedback for in-flight session switches. We
   * dim the messages pane + add a subtle spinner to the active session
   * row so users don't think nothing happened.
   */
  function setSessionSwitching(on) {
    const main = document.querySelector(".harness-main");
    if (main) main.classList.toggle("is-switching", !!on);
    const active = document.querySelector("#harness-session-list .harness-session-item.is-active");
    if (active) active.classList.toggle("is-loading", !!on);
    let banner = document.getElementById("harness-switching-banner");
    if (on && !banner) {
      banner = document.createElement("div");
      banner.id = "harness-switching-banner";
      banner.className = "harness-switching-banner";
      banner.textContent = "正在加载会话…";
      const messages = document.getElementById("harness-messages");
      if (messages) messages.parentNode.insertBefore(banner, messages);
    } else if (!on && banner) {
      banner.remove();
    }
  }

  function showHarnessToast(text) {
    let toast = document.getElementById("harness-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "harness-toast";
      toast.className = "harness-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = text;
    toast.classList.add("is-visible");
    clearTimeout(showHarnessToast._t);
    showHarnessToast._t = setTimeout(() => toast.classList.remove("is-visible"), 2200);
  }

  async function forkCurrentSession() {
    if (state.busy) return;
    const srcSid = state.sessionId;
    if (!srcSid) return;
    if (!confirm("基于当前会话 fork 出新会话?(将继承 L3 讨论上下文,聊天记录不复制)")) return;
    try {
      const r = await fetch("/api/harness/sessions/fork", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_session_id: srcSid,
          title: "fork of " + shortSessionLabel(srcSid),
        }),
      });
      if (!r.ok) {
        console.warn("[HarnessChat] fork failed", r.status);
        return;
      }
      const body = await r.json();
      const sid = body.session_id;
      if (!sid) return;
      // Switch UI to the new forked session.
      state.sessionId = sid;
      persistSessionId();
      state.messagesEl.innerHTML = "";
      renderWelcome();
      await loadSessions();
      console.info(
        "[HarnessChat] forked", srcSid, "->", sid,
        "inherited_from=", body.inherited_from,
        "forked=", body.forked,
      );
    } catch (e) {
      console.warn("[HarnessChat] fork error", e);
    }
  }

  async function deleteCurrentSession() {
    if (!state.sessionId) return;
    if (!confirm("确认删除当前会话?此操作不可撤销。")) return;
    try {
      const r = await fetch(
        `/api/harness/sessions/${encodeURIComponent(state.sessionId)}`,
        { method: "DELETE" },
      );
      if (!r.ok) {
        console.warn("[HarnessChat] delete failed", r.status);
        return;
      }
      // Pick a fresh session and refresh the picker.
      try { window.localStorage.removeItem(SESSION_STORAGE_KEY); } catch (e) {}
      state.sessionId = null;
      ensureSession();
      persistSessionId();
      state.messagesEl.innerHTML = "";
      renderWelcome();
      await loadSessions();
    } catch (e) {
      console.warn("[HarnessChat] delete error", e);
    }
  }

  /* §Harness-redesign — render session list as a sidebar with cards
     grouped by recency (今天 / 昨天 / 本周 / 更早). The old <select>
     picker still exists in the DOM (hidden) for state compatibility;
     we render into #harness-session-list instead. */
  function renderSessionPicker() {
    // legacy select (hidden) - keep value in sync
    if (state.sessionSelectEl) {
      const cur = state.sessionId || "";
      state.sessionSelectEl.value = cur;
    }
    const list = document.getElementById("harness-session-list");
    if (!list) return;
    const cur = state.sessionId || "";
    const search = (document.getElementById("harness-session-search")?.value || "").trim().toLowerCase();
    // We only render server-known sessions now. The active session id
    // is reconciled at boot via reconcileActiveSession() so it's
    // guaranteed to be present in state.sessions. If for some reason
    // it isn't yet (race during boot), surface a single recovery row
    // so the UI never silently drops the active conversation.
    const sessionItems = state.sessions || [];
    const curInList = sessionItems.some((s) => s.id === cur);
    const recoveryRow = (!curInList && cur && !cur.startsWith("pending-"))
      ? [{ id: cur, title: "（未同步到列表）", last_active: new Date().toISOString(), message_count: 0, _recovery: true }]
      : [];
    const all = [...recoveryRow, ...sessionItems];

    const filtered = search
      ? all.filter(s => (s.id + " " + (s.title || "")).toLowerCase().includes(search))
      : all;

    if (filtered.length === 0) {
      list.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "harness-session-empty";
      empty.textContent = search ? "未找到匹配的会话" : "暂无会话,点击「新对话」开始";
      list.appendChild(empty);
      return;
    }

    // Group by recency
    const now = new Date();
    const dayMs = 86400000;
    const groups = { "今天": [], "昨天": [], "本周": [], "更早": [] };
    for (const s of filtered) {
      const t = s.last_active ? new Date(s.last_active) : now;
      const diffDays = Math.floor((now - t) / dayMs);
      if (diffDays <= 0) groups["今天"].push({ s, t });
      else if (diffDays === 1) groups["昨天"].push({ s, t });
      else if (diffDays <= 7) groups["本周"].push({ s, t });
      else groups["更早"].push({ s, t });
    }

    list.innerHTML = "";
    for (const [groupName, items] of Object.entries(groups)) {
      if (items.length === 0) continue;
      const group = document.createElement("div");
      group.className = "harness-session-group";
      const title = document.createElement("div");
      title.className = "harness-session-group-title";
      title.textContent = groupName;
      group.appendChild(title);
      for (const { s, t } of items) {
        group.appendChild(buildSessionItem(s, t, cur));
      }
      list.appendChild(group);
    }
  }

  function buildSessionItem(s, t, currentId) {
    const item = document.createElement("div");
    item.className = "harness-session-item" + (s.id === currentId ? " is-active" : "");
    item.dataset.sessionId = s.id;
    item.title = s.id;

    const titleEl = document.createElement("span");
    titleEl.className = "harness-session-item-title";
    titleEl.textContent = sessionItemTitle(s);
    item.appendChild(titleEl);

    const meta = document.createElement("span");
    meta.className = "harness-session-item-meta";
    const cnt = s.message_count || 0;
    meta.textContent = cnt > 0 ? `${cnt} 条` : "";
    item.appendChild(meta);

    const actions = document.createElement("span");
    actions.className = "harness-session-item-actions";
    const forkBtn = document.createElement("button");
    forkBtn.className = "harness-session-item-action";
    forkBtn.dataset.action = "fork";
    forkBtn.title = "Fork";
    forkBtn.textContent = "⎘";
    forkBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      forkSessionById(s.id);
    });
    const delBtn = document.createElement("button");
    delBtn.className = "harness-session-item-action";
    delBtn.dataset.action = "delete";
    delBtn.title = "删除";
    delBtn.textContent = "×";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSessionById(s.id);
    });
    actions.appendChild(forkBtn);
    actions.appendChild(delBtn);
    item.appendChild(actions);

    item.addEventListener("click", () => switchSession(s.id));
    return item;
  }

  function sessionItemTitle(s) {
    // Prefer a session title (if backend provides one); otherwise use
    // a short id label so the sidebar stays scannable.
    if (s.title) return s.title;
    return s.id.length > 14 ? s.id.slice(0, 14) + "…" : s.id;
  }


  /* §Harness-redesign — sync the main-header title with the
     currently selected session. Falls back to "新对话" when no
     session is open. */
  function updateMainHeader() {
    const titleEl = document.getElementById("harness-current-title");
    if (!titleEl) return;
    if (!state.sessionId) {
      titleEl.textContent = "新对话";
      return;
    }
    const s = (state.sessions || []).find((x) => x.id === state.sessionId);
    titleEl.textContent = sessionItemTitle(s || { id: state.sessionId });
  }

  async function forkSessionById(sid) {
    if (state.busy) {
      alert("请先等待当前请求结束");
      return;
    }
    try {
      const r = await fetch("/api/harness/sessions/fork", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_session_id: sid }),
      });
      if (r.ok) {
        const body = await r.json();
        await loadSessions();
        if (body.session_id) switchSession(body.session_id);
      }
    } catch (e) {
      console.warn("[HarnessChat] fork error", e);
    }
  }

  async function deleteSessionById(sid) {
    if (!confirm("删除会话 " + sid + " ?")) return;
    try {
      const r = await fetch(`/api/harness/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
      if (r.ok) {
        await loadSessions();
        if (sid === state.sessionId) {
          state.sessionId = null;
          try { window.localStorage.removeItem(SESSION_STORAGE_KEY); } catch (e) {}
          state.messagesEl.innerHTML = "";
          renderWelcome();
        }
      }
    } catch (e) {
      console.warn("[HarnessChat] delete error", e);
    }
  }

  function shortSessionLabel(sid, sess) {
    if (sess && sess.last_active) {
      try {
        const t = new Date(sess.last_active);
        const ms = sess.message_count || 0;
        return `${sid.slice(0, 12)}… · ${ms}条 · ${t.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
      } catch (e) { /* fall through */ }
    }
    return sid.slice(0, 14) + "…";
  }

  // ─────────────────────────────────────────────────
  // 消息渲染
  // ─────────────────────────────────────────────────

  /* §Harness-redesign — load + re-render previous messages when the
     user switches sessions. Server returns [{role, content, ts}, ...]
     from the L1 memory store; we re-render through the same
     appendMessage + renderMarkdown path so styling stays consistent. */
  async function loadHistory() {
    const sid = state.sessionId;
    if (!sid) return;
    try {
      const r = await fetch(
        `/api/harness/sessions/${encodeURIComponent(sid)}/messages`,
        { method: "GET" }
      );
      if (!r.ok) {
        console.warn("[HarnessChat] loadHistory failed", r.status);
        return;
      }
      const body = await r.json();
      const msgs = Array.isArray(body.messages) ? body.messages : [];
      state.messagesEl.innerHTML = "";
      if (msgs.length === 0) {
        renderWelcome();
        return;
      }
      // Skip the welcome state when history exists.
      const welcome = state.messagesEl.querySelector(".harness-welcome");
      if (welcome) welcome.remove();
      for (const m of msgs) {
        const role = (m.role === "assistant" || m.role === "user")
          ? m.role
          : "user";
        const content = m.content || "";
        const meta = m.ts ? formatTs(m.ts) : null;
        appendMessage(role, renderMarkdown(content), meta);
      }
      // Make sure meta doesn't render raw HTML — appendMessage uses
      // textContent, but renderMarkdown returns HTML, so the bubble
      // needs innerHTML. Switch the bubble to innerHTML for content.
      // (appendMessage already uses .textContent; refactor below.)
      scrollToBottom();
    } catch (e) {
      console.warn("[HarnessChat] loadHistory error", e);
    }
  }

  function formatTs(ts) {
    try {
      const d = new Date(typeof ts === "number" && ts < 1e12 ? ts * 1000 : ts);
      return d.toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
    } catch (_) { return ""; }
  }

  function appendMessage(role, content, meta) {
    const el = document.createElement("div");
    el.className = `harness-message is-${role}`;
    const bubble = document.createElement("div");
    bubble.className = "harness-message-bubble";
    // content may be either a plain string (legacy) or an HTML
    // string from renderMarkdown. For safety, treat it as HTML only
    // when it contains HTML tags; otherwise fall back to textContent
    // so untrusted input can't inject markup. The markdown renderer
    // already escapes the source, so this is double-safe.
    if (typeof content === "string" && /<[a-z][^>]*>/i.test(content)) {
      bubble.innerHTML = content;
    } else {
      bubble.textContent = content || "";
    }
    el.appendChild(bubble);
    if (meta) {
      const m = document.createElement("div");
      m.className = "harness-message-meta";
      m.textContent = meta;
      el.appendChild(m);
    }
    state.messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function appendStreamingAssistant() {
    const el = document.createElement("div");
    el.className = "harness-message is-assistant is-loading";
    const bubble = document.createElement("div");
    bubble.className = "harness-message-bubble";
    bubble.textContent = "";
    el.appendChild(bubble);
    state.messagesEl.appendChild(el);
    scrollToBottom();
    return { el, bubble, content: "" };
  }

  function appendReasoningTrace(content) {
    const el = document.createElement("details");
    el.className = "harness-message is-reasoning";
    el.open = true;
    const summary = document.createElement("summary");
    summary.className = "harness-reasoning-summary";
    summary.textContent = "🧠 P8 推理过程 (实时)";
    const body = document.createElement("div");
    body.className = "harness-reasoning-body";
    body.textContent = content;
    el.appendChild(summary);
    el.appendChild(body);
    state.messagesEl.appendChild(el);
    state.traceEl = el;
    state.traceContent = content;
    scrollToBottom();
  }


  const harnessState = window.__harnessState || (window.__harnessState = {
    pendingConfirm: null,
  });
  function appendReasoningDelta(delta) {
    if (!state.traceEl) {
      appendReasoningTrace(delta);
      return;
    }
    state.traceContent += delta;
    const body = state.traceEl.querySelector(".harness-reasoning-body");
    if (body) body.textContent = state.traceContent;
    scrollToBottom();
  }

  function finalizeReasoningTrace() {
    if (!state.traceEl) return;
    if (state.traceContent.trim().length < 5) {
      state.traceEl.remove();
    } else {
      state.traceEl.open = false;
      const summary = state.traceEl.querySelector(".harness-reasoning-summary");
      if (summary) summary.textContent = "🧠 P8 推理过程 (点击展开)";
    }
    state.traceEl = null;
    state.traceContent = "";
    // §0.4.33 — todo checklist lifetime matches the reasoning trace.
    clearTodoEl();
  }

  // §0.4.33.3 — todo checklist dock. A persistent <details> that
  // lives between #harness-messages and #harness-form. The dock
  // auto-shows on the first plan_ready and auto-hides on stream end.
  // Per-row patches still flow through updateTodoItem — we just
  // mutate rows inside the dock instead of injecting messages.
  function ensureTodoEl() {
    if (state.todoEl) return state.todoEl;
    const el = document.getElementById("harness-todo-dock");
    if (!el) return null;
    state.todoEl = el;
    state.todoItems = [];
    return el;
  }

  function renderTodoList(items) {
    const el = ensureTodoEl();
    if (!el) return;
    state.todoItems = Array.isArray(items) ? items.slice() : [];
    const body = el.querySelector("[data-bind=\"body\"]");
    if (body) body.innerHTML = "";
    state.todoItems.forEach((t) => {
      const row = document.createElement("li");
      row.className = "harness-todo-row";
      row.dataset.todoId = t.id;
      row.innerHTML = `
        <span class="harness-todo-status" aria-hidden="true">○</span>
        <span class="harness-todo-label"></span>
        <span class="harness-todo-agent"></span>
      `;
      row.querySelector(".harness-todo-label").textContent = t.label || t.tool || "?";
      const agentEl = row.querySelector(".harness-todo-agent");
      agentEl.textContent = agentBadge(t.agent);
      if (t.agent) agentEl.dataset.agent = t.agent;
      row.classList.add(`status-${t.status || "pending"}`);
      body.appendChild(row);
    });
    el.hidden = false;
    el.open = true;
    updateTodoProgress();
    scrollToBottom();
  }

  function updateTodoItem(patch) {
    if (!patch || !patch.id) return;
    if (!state.todoEl) ensureTodoEl();
    const dock = state.todoEl;
    if (!dock) return;
    const row = dock.querySelector(`[data-todo-id="${patch.id}"]`);
    if (!row) return;
    row.classList.remove(
      "status-pending", "status-active",
      "status-done", "status-error", "status-skipped"
    );
    const next = patch.status || "pending";
    row.classList.add(`status-${next}`);
    const icon = row.querySelector(".harness-todo-status");
    if (icon) icon.textContent = todoStatusIcon(next);
    if (patch.agent) {
      const agentEl = row.querySelector(".harness-todo-agent");
      if (agentEl) {
        agentEl.textContent = agentBadge(patch.agent);
        agentEl.dataset.agent = patch.agent;
      }
    }
    if (next === "error" && patch.error) {
      const labelEl = row.querySelector(".harness-todo-label");
      if (labelEl) labelEl.title = String(patch.error);
    }
    if (next === "done") {
      row.classList.add("status-done-flash");
      setTimeout(() => row.classList.remove("status-done-flash"), 600);
    }
    const item = state.todoItems.find((t) => t.id === patch.id);
    if (item) {
      item.status = next;
      if (patch.error !== undefined) item.error = patch.error;
    }
    updateTodoProgress();
  }

  function updateTodoProgress() {
    const dock = state.todoEl || document.getElementById("harness-todo-dock");
    if (!dock) return;
    const total = state.todoItems.length;
    let done = 0;
    state.todoItems.forEach((t) => {
      if (t.status !== "pending" && t.status !== "active") done += 1;
    });
    const progress = dock.querySelector("[data-bind=\"progress\"]");
    if (progress) progress.textContent = `${done} / ${total}`;
    if (total > 0 && done === total) {
      dock.classList.add("is-complete");
    }
  }

  function todoStatusIcon(status) {
    return ({
      pending: "○",
      active: "◐",
      done: "✓",
      error: "✗",
      skipped: "⊘",
    })[status] || "○";
  }

  function agentBadge(agent) {
    return ({
      data_agent: "🤖 data",
      alpha_agent: "📐 alpha",
      news_agent: "📰 news",
      command_resolver: "✍️ cmd",
      trading_agents: "🏦 ta",
    })[agent] || (agent ? `🧩 ${agent}` : "❓");
  }

  function clearTodoEl() {
    const dock = state.todoEl || document.getElementById("harness-todo-dock");
    if (dock) {
      dock.hidden = true;
      dock.classList.remove("is-complete");
      dock.open = false;
      const body = dock.querySelector("[data-bind=\"body\"]");
      if (body) body.innerHTML = "";
      const progress = dock.querySelector("[data-bind=\"progress\"]");
      if (progress) progress.textContent = "0 / 0";
    }
    state.todoEl = null;
    state.todoItems = [];
  }

  function appendToolCall(name, args) {
    const argsStr = Object.entries(args || {})
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    return appendMessage("tool-call", `🔧 ${name}(${argsStr})`);
  }

  // §3.5 — STATUS_BADGE switch. Mirrors backend
  // summarize_tool_result templates so the reasoning trace stays in
  // sync with the assistant bubble. Frontend-only fallback (in case
  // backend didn't fill result.summary for some new status).
  // §14.3.4 — collapse verbose JSON.stringify(args) to a readable
  // form. Heuristic: lead with symbol/name, then key=value pairs
  // joined by " · ". Skip nested objects (they were just noise).
  function _friendlyArgs(args) {
    if (typeof args !== "object" || !args) return "";
    const parts = [];
    const sym = args.symbol || args.ticker || args.name;
    if (sym) parts.push(String(sym));
    for (const [k, v] of Object.entries(args)) {
      if (k === "symbol" || k === "ticker" || k === "name") continue;
      if (typeof v === "object" && v !== null) continue;
      parts.push(`${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
    }
    return parts.join(" · ");
  }

  function _symFromRaw(raw) {
    if (typeof raw !== "string") return null;
    const m = raw.match(/"symbol"\s*:\s*"([^"]+)"/);
    return m ? m[1] : null;
  }
  const STATUS_BADGE = {
    pending_approval: (n, p) => {
      const args = p?.result?.args || p?.args || {};
      const sym = args.symbol ? ` · ${args.symbol}` : "";
      return `🔒 ${n} 等待审批${sym}`;
    },
    created: (n, p) => {
      const r = p?.result || p;
      const sym = r?.symbol || _symFromRaw(r?.raw);
      return `✅ ${n} 已完成${sym ? ` (${sym})` : ""}`;
    },
    updated: (n, p) => `✏️ ${n} 已更新`,
    deleted: (n, p) => {
      const r = p?.result || p;
      const sym = r?.symbol || _symFromRaw(r?.raw);
      return `🗑️ ${n} 已删除${sym ? ` (${sym})` : ""}`;
    },
    duplicate: (n, p) => {
      const r = p?.result || p;
      const sym = r?.symbol || "?";
      return `♻️ ${sym} 已存在,未重复添加`;
    },
    not_found: (n, p) => {
      const r = p?.result || p;
      return `⚠️ ${r?.symbol || "?"} 不在关注列表中`;
    },
    empty: () => `（空）`,
    no_data: (n, p) => {
      const r = p?.result || p;
      return `📭 ${n}: 无可用数据${r?.raw ? ` (${String(r.raw).slice(0, 60)})` : ""}`;
    },
    error: (n, p) => {
      const r = p?.result || p;
      const raw = r?.raw || r?.message || "";
      return `❌ ${n} 失败${raw ? `: ${String(raw).slice(0, 80)}` : ""}`;
    },
    ok: (n, p) => {
      // summary already filled by backend; if missing, leave a neutral
      // tag rather than dumping JSON.
      const r = p?.result || p;
      if (r?.summary) return `✓ ${n}: ${r.summary.slice(0, 60)}`;
      return `✓ ${n} 完成`;
    },
  };

  // §Step 24 P3 — extract a readable prefix from a stringified
  // tool result. Handles two flavours:
  //   1. Python ``repr(QuoteResult(...))`` (default=str fallback for
  //      Pydantic models in the SSE serialiser). We surface the
  //      ``symbol=...`` and the first few keyword=value pairs.
  //   2. A plain JSON object string (defensive). Slice the leading
  //      braces / key=value pairs verbatim.
  // Returns a 240-char preview safe to drop into a chat bubble.
  function _previewStringifiedToolResult(s) {
    if (!s) return "(empty)";
    // Heuristic: drop a Python repr header like "QuoteResult(" or
    // "Foo(" so the reader sees the field list, not the class name.
    let body = s;
    const reprMatch = s.match(/^[A-Z][A-Za-z0-9_.]*\((.*)\)\s*$/);
    if (reprMatch) body = reprMatch[1];
    // Pull symbol/asset_name prefix for context, then a key=value run.
    const fields = body.match(/symbol='([^']+)'|symbol="([^"]+)"|asset_name='([^']+)'|"asset_name":"([^"]+)"/);
    const symField = fields ? (fields[1] || fields[2]) : null;
    const assetField = fields ? (fields[3] || fields[4]) : null;
    const kvSlice = body.slice(0, 200);
    const header = symField ? `${symField}` : "";
    const name = assetField ? ` (${assetField})` : "";
    return (header + name + " · " + kvSlice).slice(0, 240);
  }

  // §0.4.22.fix — shared error-card renderer (kept light-weight on the
  // frontend so the reasoning trace stays slim; full CSS lives in
  // web/static/agent.css under ``.error-card``).
  // §0.4.24 — when ``p.retryable`` is true we emit a ``🔄 重试`` button
  // that re-sends the previous user message (re-runs the tool via the
  // same Tier 1 short-circuit / Tier 2 plan path).
  function renderErrorCard(p, toolName) {
    const code = p.error_code || p.code || "";
    const raw = p.error || p.message || "执行失败";
    const labels = {
      no_data: "暂无数据",
      provider_error: "数据源异常",
      invalid_symbol: "无效代码",
      rate_limited: "请求过快",
      timeout: "请求超时",
    };
    const label = labels[code] || raw;
    const sym = p.symbol || p.ticker || "";
    const symHtml = sym ? ` <span class="ec-symbol">${escapeHtml(sym)}</span>` : "";
    const codeHtml = code ? `<span class="ec-code">${escapeHtml(code)}</span>` : "";
    const retryHtml = p.retryable
      ? `<button type="button" class="ec-retry" data-action="retry-tool">🔄 重试</button>`
      : "";
    return (
      `<div class="error-card">` +
      `<div class="ec-head"><span class="ec-icon">⚠️</span><span class="ec-tool">${escapeHtml(toolName)}</span>${symHtml}</div>` +
      `<div class="ec-body">${escapeHtml(label)}</div>` +
      `<div class="ec-extras">${codeHtml}${retryHtml}</div>` +
      `</div>`
    );
  }

  // §0.4.24 — retry helper. Looks up the last user message in the chat
  // transcript (state.lastUserMessage), re-fills the input, and re-sends.
  // We deliberately don't call sendMessage() directly here so the user
  // sees the retry in the input first.
  function retryLastTool() {
    if (state.busy) return false;
    const text = (state.lastUserMessage || "").trim();
    if (!text) {
      // No previous user message — fall back to an empty input.
      if (state.inputEl) state.inputEl.focus();
      return false;
    }
    state.inputEl.value = text;
    sendMessage();
    return true;
  }

  function appendToolResult(name, payload) {
    // §0.4.25 — when the backend attaches a pre-rendered friendly card
    // (``payload.result.display_html``) we use it directly. This keeps
    // styling consistent with the agent_final bubble and removes the
    // duplicated logic in ``formatRawResult`` (which serialises the
    // same shape to pipe-table markdown). Fall back to the old path
    // for legacy tools that don't emit display_html yet.
    const resultObj = payload?.result;
    if (resultObj && typeof resultObj === "object" && typeof resultObj.display_html === "string") {
      const html = resultObj.display_html;
      // §XSS guard — only trust the backend's display_html when it's a
      // known card root class. Otherwise fall through to the legacy path.
      if (/^<div\s+class="(history|compare|quote|fundamentals|news|alpha|ack|error)-card"/i.test(html.trim())) {
        return appendMessage("tool-result", html);
      }
    }

    if (payload?.error) {
      // §0.4.22.fix — friendly error card instead of raw ``❌ tool: msg``.
      // Builds a minimal {error, error_code, symbol} payload and emits
      // the same <div class="error-card"> backend would produce.
      const errPayload = {
        error: payload.error,
        error_code: payload.error_code || payload.code,
        symbol: payload.symbol || (payload.args && payload.args.symbol),
        retryable: true,  // §0.4.24 — every tool_result error can be retried
      };
      const card = renderErrorCard(errPayload, name || "tool");
      const el = appendMessage("tool-result", card);
      // §0.4.24 — bind the retry button. delegated event listener
      // on the messages container so we don't lose bindings when
      // SSE pushes new bubbles.
      if (el) {
        el.dataset.retryable = "1";
        el.dataset.toolName = name || "tool";
      }
      return el;
    }
    const result = payload?.result || payload;
    const status = result?.status;
    const badge = STATUS_BADGE[status];
    if (badge) return appendMessage("tool-result", badge(name || "tool", payload));

    // §P3-5 — render a compact summary for get_report instead of
    // dumping the multi-KB markdown blob into the chat. The full
    // content is huge (8000+ chars) and would freeze the page when
    // JSON.stringify + innerHTML runs over it. We pull ticker /
    // signal / rating / date out of the result text and emit them
    // as a one-liner, plus an "open detail" link.
    if (name === "get_report" && result?.status === "ok") {
      const text = String(result.text || "");
      const idMatch = text.match(/REPORT:\s*(\S+)/);
      const reportId = (idMatch && idMatch[1])
        || (payload && payload.args && payload.args.report_id)
        || "";
      const meta = extractReportMeta(text);
      const label = reportId ? `📄 ${reportId}` : "📄 报告";
      const line = [meta.ticker, meta.date, meta.signal].filter(Boolean).join(" · ");
      const link = reportId
        ? ` · <a href="/reports/${encodeURIComponent(reportId)}" target="_blank" rel="noopener">在新页面打开 →</a>`
        : "";
      const body = line ? ` ${escapeHtml(line)}` : "";
      return appendMessage("tool-result", `${label}${body}${link}`);
    }

    // §P3-5 — generic safety: avoid JSON.stringify on a huge payload
    // (some tools return multi-KB text blobs that would lock the
    // browser). Use the .text field directly if present, otherwise
    // stringify a shallow clone with bounded size.
    //
    // §Step 24 P3 — special-case string results. When a Pydantic
    // ``QuoteResult`` (or any non-JSON-native object) is emitted
    // through the SSE default=str fallback, payload.result arrives
    // here as the Python ``repr(...)`` string. Iterating
    // ``Object.keys(<string>)`` yields char indices and produces the
    // bug ``{"0":"s","1":"y",...}``. Detect strings first and slice
    // directly so the preview is the raw repr (or a friendly subset).
    let preview;
    try {
      if (typeof result === "string") {
        // Server-side repr(...) or a JSON-encoded payload. Pull a
        // reader-friendly prefix by skipping the long Python repr
        // header and showing the leading ``symbol=...`` / JSON keys.
        preview = _previewStringifiedToolResult(result);
      } else if (result && typeof result.text === "string") {
        preview = result.text.slice(0, 240);
      } else if (result && typeof result.summary === "string") {
        preview = result.summary.slice(0, 240);
      } else {
        const shallow = {};
        for (const k of Object.keys(result || {})) {
          const v = result[k];
          shallow[k] = typeof v === "string" ? v.slice(0, 120) : v;
        }
        preview = JSON.stringify(shallow).slice(0, 240);
      }
    } catch (_) {
      preview = "(result too large to render)";
    }
    return appendMessage("tool-result", `📥 ${name || "tool"}: ${preview}`);
  }

  // §P3-5 — pull ticker / date / signal out of a get_report text
  // blob. The blob is ``REPORT: <id>\n---meta---\n{...json...}\n
  // ---content---\n<md>``. We only need a few headline fields, so
  // we scan for them with simple regex rather than parsing the
  // meta JSON (which can be malformed by the LLM tool output).
  function extractReportMeta(text) {
    const meta = { ticker: "", date: "", signal: "" };
    if (!text) return meta;
    const ticker = text.match(/"ticker"\s*:\s*"([^"]+)"/);
    if (ticker) meta.ticker = ticker[1];
    const date = text.match(/"analysis_date"\s*:\s*"([^"]+)"/);
    if (date) meta.date = date[1];
    const signal = text.match(/"signal"\s*:\s*"([^"]+)"/)
      || text.match(/"rating"\s*:\s*"([^"]+)"/);
    if (signal) meta.signal = signal[1];
    return meta;
  }

  function appendVerified(payload) {
    const tag = payload.ok ? "✅ 通过" : "❌ 失败";
    return appendMessage("verified", `${tag} L${payload.level}: ${payload.reason || ""}`);
  }

  function appendAnswerVerified(payload) {
    const score = payload.details?.score ?? 0;
    const issues = (payload.details?.issues || []).join("; ");
    const tag = payload.ok ? "✅ L3 grounded" : "⚠️ L3 ungrounded";
    const body = `${tag} · score=${score.toFixed(2)} · ${payload.reason || ""}` +
      (issues ? ` · issues: ${issues}` : "");
    return appendMessage("l3", body);
  }

  function appendError(content) {
    return appendMessage("error", `❌ ${content}`);
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
    });
  }

  async function onGrantAllChange(event) {
    const checked = !!event.target.checked;
    const sessionId = state.sessionId || ensureSession();
    const path = checked
      ? `/api/harness/sessions/${encodeURIComponent(sessionId)}/grant_all`
      : `/api/harness/sessions/${encodeURIComponent(sessionId)}/revoke_all`;
    try {
      const resp = await fetch(path, { method: "POST" });
      if (!resp.ok) {
        event.target.checked = !checked;
        appendReasoningDelta(`⚠️ 全权限切换失败: ${resp.status} ${await resp.text()}\n`);
      }
    } catch (e) {
      event.target.checked = !checked;
      appendReasoningDelta(`⚠️ 全权限切换失败: ${e.message || e}\n`);
    }
  }

  async function syncGrantAllFromServer() {
    const sessionId = state.sessionId || ensureSession();
    try {
      const resp = await fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}/grant_all`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (state.grantAllBox) state.grantAllBox.checked = !!data.is_granted;
    } catch (_) { /* silent — toggle is non-critical */ }
  }

  function renderWelcome() {
    if (state.messagesEl.children.length > 0) return;
    state.messagesEl.innerHTML = `
      <div class="harness-welcome">
        <div class="harness-welcome-icon">🤖</div>
        <div class="harness-welcome-title">理财通用 Agent (P8 Harness)</div>
        <div class="harness-welcome-stats">
          <span class="harness-welcome-stat"><b>6</b><span>sub-agents</span></span>
          <span class="harness-welcome-stat"><b>19</b><span>tools</span></span>
          <span class="harness-welcome-stat"><b>L3</b><span>judge</span></span>
          <span class="harness-welcome-stat"><b>7</b><span>写操作需审批</span></span>
        </div>
        <div class="harness-welcome-divider"></div>
        <div class="harness-welcome-suggestions">
          ${SUGGESTIONS.map(
            (s) => `<div class="harness-suggestion-chip" data-suggestion="${s.replace(/"/g, "&quot;")}">${s}</div>`
          ).join("")}
        </div>
      </div>
    `;
    state.messagesEl.querySelectorAll(".harness-suggestion-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        state.inputEl.value = chip.dataset.suggestion;
        state.inputEl.focus();
      });
    });
  }

  // ─────────────────────────────────────────────────
  // 发送消息 (SSE 流式)
  // ─────────────────────────────────────────────────

  async function sendMessage() {
    if (state.busy) return;
    const text = (state.inputEl.value || "").trim();
    if (!text) return;

    const sessionId = ensureSession();
    const welcome = state.messagesEl.querySelector(".harness-welcome");
    if (welcome) welcome.remove();

    appendMessage("user", text);
    state.lastUserMessage = text;
    state.inputEl.value = "";
    setBusy(true);

    const assistant = appendStreamingAssistant();
    appendReasoningTrace("");

    try {
      const resp = await fetch("/api/harness/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });

      if (!resp.ok || !resp.body) {
        finalizeReasoningTrace();
        assistant.bubble.textContent = `请求失败 (HTTP ${resp.status})`;
        assistant.el.classList.remove("is-loading");
        setBusy(false);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // SSE 解析:event/data 交替,以 \n\n 分割
      // 我们后端 emit 的是 "event: X\ndata: {...}\n\n"
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          handleSseBlock(block, assistant);
        }
      }

      finalizeReasoningTrace();
      assistant.el.classList.remove("is-loading");
    } catch (e) {
      finalizeReasoningTrace();
      assistant.el.classList.remove("is-loading");
      appendError(`网络错误: ${e.message}`);
    } finally {
      setBusy(false);
      // §0.4.24.1 — reset every in-flight retry button so the user can
      // click them again after a failed/successful request.
      if (state.messagesEl) {
        state.messagesEl.querySelectorAll('[data-action="retry-tool"]').forEach((btn) => {
          btn.disabled = false;
          btn.textContent = "🔄 重试";
        });
      }
      // §Harness-redesign — auto-title the session on first user
      // message so the sidebar shows a meaningful label.
      maybeAutoTitleSession(text);
      updateMainHeader();
      loadSessions();
    }
  }

  function maybeAutoTitleSession(text) {
    const sid = state.sessionId;
    if (!sid) return;
    const s = (state.sessions || []).find((x) => x.id === sid);
    if (s && s.title) return; // already has a title
    const title = (text || "").slice(0, 24).replace(/\s+/g, " ").trim();
    if (!title) return;
    fetch(`/api/harness/sessions/${encodeURIComponent(sid)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).catch(() => {});
    // Update local cache so renderSessionPicker shows the title.
    if (s) s.title = title;
  }

  function handleSseBlock(block, assistant) {
    let eventName = null;
    let dataStr = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) eventName = line.slice(7).trim();
      else if (line.startsWith("data: ")) dataStr += line.slice(6);
    }
    if (!eventName) return;
    let payload = {};
    if (dataStr) {
      try { payload = JSON.parse(dataStr); } catch (_) { payload = { raw: dataStr }; }
    }
    dispatchEvent(eventName, payload, assistant);
  }

  // Minimal client-side markdown renderer scoped to what the harness
  // LLM emits: # / ## / ### headers, **bold**, *italic*, pipe tables,
  // bullet lists (-), numbered lists, and inline code. Escapes HTML
  // before applying markdown so injected scripts can\'t break out.
  function renderMarkdown(md) {
    if (!md) return "";
    // §0.4.18 — friendly HTML cards (history, multi-asset compare) ship as raw HTML
    // from the backend (see ``tradingagents/agent_harness/tools/display_view.py``).
    // Detect the well-known root tag so we can render them without escaping.
    if (typeof md === "string" && /^(<div\s+class="(history|compare|quote|fundamentals|news|alpha|ack|error)-card")/i.test(md.trim())) {
      return md;
    }
    const esc = md
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    const lines = esc.split(/\r?\n/);
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      // Pipe table: header | --- | rows
      if (/^\|.+\|$/.test(line) && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1])) {
        const headers = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\|.+\|$/.test(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        out.push(renderTable(headers, rows));
        continue;
      }
      // Headers
      const h = line.match(/^(#{1,4})\s+(.+)$/);
      if (h) {
        out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
        i++;
        continue;
      }
      // Bullet list
      if (/^[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^[-*+]\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }
      // Ordered list
      if (/^\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\d+\.\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }
      // Blank line — paragraph break
      if (/^\s*$/.test(line)) {
        out.push("");
        i++;
        continue;
      }
      // Paragraph: collect consecutive non-blank lines
      const para = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,4}\s|[-*+]\s|\d+\.\s|\|)/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      if (para.length) out.push(`<p>${inline(para.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  function splitRow(row) {
    return row.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  }

  function renderTable(headers, rows) {
    const th = headers.map((h) => `<th>${inline(h)}</th>`).join("");
    const trs = rows
      .map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>")
      .join("");
    return `<table class="harness-md-table"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
  }

  function inline(s) {
    return s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  // Client-side pretty-printer for Tier-1 raw tool results (no LLM
  // synthesis). Detects the schema shape and emits a readable summary
  // instead of dumping the raw dict — the JSON form was confusing users
  // who asked simple quote questions.
  // Heuristic: detect when the LLM synthesizer just dumped the raw
  // tool result JSON instead of writing prose. Common pattern is
  // ``"{ "text": ..., "count": 1 }"`` for list_* tools.
  function looksLikeRawJsonDump(summary, result) {
    if (typeof summary !== "string") return false;
    const trimmed = summary.trim();
    if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return false;
    // If the result has a ``text`` field and the summary contains the
    // same text verbatim, the LLM did no transformation.
    if (typeof result?.text === "string" && summary.includes(result.text.slice(0, 60))) return true;
    // ````json`` prefix from the LLM also counts.
    return /^```json/i.test(trimmed);
  }

  // §Step 22 — friendly renderer for write tool acks.
  // Returns a markdown string when the result looks like an ACK,
  // null otherwise so formatRawResult can fall back to its other
  // branches. Handles NOTE_CREATED / ALERT_DELETED / ADDED /
  // DUPLICATE etc. — anything ending in a known ACK verb.
  function renderWriteAck(result) {
    const status = result && result.status;
    const raw = (result && result.raw) || "";
    const ackPrefixes = [
      "ADDED", "REMOVED", "DUPLICATE", "UPDATED", "DELETED", "CREATED",
      // Namespaced verbs from NOTE_/ALERT_/WATCHLIST_/SCHEDULED_/RUN_ tools
      "NOTE_CREATED", "NOTE_UPDATED", "NOTE_DELETED",
      "ALERT_CREATED", "ALERT_UPDATED", "ALERT_DELETED",
      "WATCHLIST_ADDED", "WATCHLIST_REMOVED",
      "SCHEDULED_CREATED", "SCHEDULED_UPDATED", "SCHEDULED_DELETED",
      "RUN_STARTED", "RUN_COMPLETED", "RUN_FAILED",
    ];
    const isAckStatus = ["created", "updated", "deleted", "duplicate"].includes(status);
    const isAckRaw = status === "ok" && ackPrefixes.some((p) => raw.startsWith(p));
    if (!isAckStatus && !isAckRaw) return null;
    const verbMap = {
      NOTE_CREATED: "笔记已创建",
      NOTE_UPDATED: "笔记已更新",
      NOTE_DELETED: "笔记已删除",
      ALERT_CREATED: "告警已创建",
      ALERT_UPDATED: "告警已更新",
      ALERT_DELETED: "告警已删除",
      WATCHLIST_ADDED: "已加入关注",
      WATCHLIST_REMOVED: "已移除关注",
      SCHEDULED_CREATED: "定时任务已创建",
      SCHEDULED_UPDATED: "定时任务已更新",
      SCHEDULED_DELETED: "定时任务已删除",
      RUN_STARTED: "分析任务已启动",
      RUN_COMPLETED: "分析任务已完成",
      RUN_FAILED: "分析任务失败",
      ADDED: "已加入关注",
      REMOVED: "已移除关注",
      DUPLICATE: "已存在(未重复添加)",
      UPDATED: "已更新",
      DELETED: "已删除",
      CREATED: "已创建",
    };
    let label = "已保存";
    let sym = "";
    const m = raw.match(/^([A-Z_]+):\s*(.*)$/);
    if (m) {
      const verb = m[1];
      const payload = (m[2] || "").trim();
      label = verbMap[verb] || verb;
      try {
        const parsed = JSON.parse(payload);
        sym = parsed.symbol || parsed.ticker || "";
      } catch (_) {
        // payload may be a bare ticker ("ADDED: 600036.SS")
        if (payload && !payload.startsWith("{")) sym = payload;
      }
    }
    return sym ? `✅ ${label} (${sym})` : `✅ ${label}`;
  }

  function formatRawResult(result, tier) {
    if (!result || typeof result !== "object") return JSON.stringify(result || {}, null, 2);
    // §Step 22 — write-ack friendly rendering. Tier 1 short-circuit
    // on a write tool returns ``{status: "created"|"ok"|..., raw:
    // "NOTE_CREATED: {...}" | "ADDED: 600036.SS" | ...}``. Without
    // this branch the user sees raw JSON dump. We parse the ACK
    // prefix + optional JSON body and emit "✅ 笔记已创建 (600036.SS)".
    const ackRendered = renderWriteAck(result);
    if (ackRendered) return ackRendered;
    // list_notes / list_alerts / list_scheduled_tasks / list_watchlist
    // return ``{text: <markdown table>, count: N}``. The text often
    // already starts with "共 N 条笔记/告警..." so we don't prepend a
    // duplicate header — just emit the markdown table for the renderer
    // to format as an HTML table.
    if (typeof result.text === "string" && typeof result.count === "number") {
      return result.text;
    }
    // Tier 1 QuoteResult — has symbol + price + provider
    if (typeof result.price === "number" || result.price === null) {
      if (result.symbol && ("change" in result || "volume" in result)) {
        const symbol = result.symbol;
        const price = typeof result.price === "number" ? result.price.toFixed(2) : "—";
        let changeLine = "";
        if (typeof result.change === "number") {
          const sign = result.change >= 0 ? "+" : "";
          changeLine = `  涨跌: ${sign}${result.change.toFixed(2)}`;
          if (typeof result.change_pct === "number") {
            changeLine += ` (${sign}${result.change_pct.toFixed(2)}%)`;
          }
        } else if (typeof result.change_pct === "number") {
          changeLine = `  涨跌幅: ${result.change_pct.toFixed(2)}%`;
        }
        const vol = typeof result.volume === "number"
          ? `  成交量: ${result.volume.toLocaleString("en-US")}`
          : "";
        const asOf = result.as_of ? `  时间: ${result.as_of}` : "";
        const provider = result.provider ? `  数据源: ${result.provider}` : "";
        return [`📈 ${symbol}`, `  价格: ¥${price}`, changeLine, vol, asOf, provider]
          .filter(Boolean)
          .join("\n");
      }
      // HistoryResult (candles[])
      if (Array.isArray(result.candles)) {
        const symbol = result.symbol || "?";
        const interval = result.interval || "";
        const last = result.candles[result.candles.length - 1];
        const lastLine = last
          ? `  最新: ¥${(last.close ?? "—")} @ ${last.timestamp || "?"}`
          : "";
        return [`📊 ${symbol} (${interval}, ${result.candles.length} 根)`, lastLine]
          .filter(Boolean)
          .join("\n");
      }
    }
    // FundamentalsResult
    if (result.symbol && ("pe_ratio" in result || "pb_ratio" in result || "market_cap" in result)) {
      const lines = [`💼 ${result.symbol}`];
      if (result.pe_ratio != null) lines.push(`  PE: ${result.pe_ratio}`);
      if (result.pb_ratio != null) lines.push(`  PB: ${result.pb_ratio}`);
      if (result.market_cap != null) lines.push(`  市值: ${result.market_cap.toLocaleString("en-US")}`);
      if (result.roe != null) lines.push(`  ROE: ${result.roe}%`);
      return lines.join("\n");
    }
    // NewsResult
    if (Array.isArray(result.items)) {
      const symbol = result.symbol || "?";
      const items = result.items.slice(0, 3)
        .map((it) => `  - ${it.title || "(无标题)"}`)
        .join("\n");
      return [`📰 ${symbol} (${result.items.length} 条)`, items].join("\n");
    }
    // ListAlphaFactorsResult / ListWatchlistResult / scheduled tasks
    if (Array.isArray(result.factors)) {
      return `🔢 因子 (${result.factors.length}): ${result.factors.slice(0, 8).join(", ")}${result.factors.length > 8 ? "…" : ""}`;
    }
    if (Array.isArray(result.items) || Array.isArray(result.watchlist)) {
      const list = result.items || result.watchlist || [];
      const lines = list.slice(0, 10).map((it) => {
        if (typeof it === "string") return `  - ${it}`;
        const sym = it.symbol || it.ticker || "?";
        return `  - ${sym}${it.name ? ` ( ${it.name} )` : ""}`;
      });
      return [`📋 (${list.length} 项)`, ...lines].join("\n");
    }
    // Unknown shape — fall back to compact JSON so we never crash.
    return JSON.stringify(result, null, 2);
  }

  function intentHeading(intent) {
    return ({
      note: "📋 笔记",
      alert: "🔔 告警",
      watchlist: "⭐ 关注",
      scheduled: "⏰ 定时任务",
      run: "📊 分析记录",
      report: "📑 报告",
      quote: "💰 行情",
      fundamentals: "💼 基本面",
      news: "📰 新闻",
      alpha: "🔢 因子",
      history: "📈 K线",
    })[intent] || `📦 ${intent}`;
  }

  function dispatchEvent(name, payload, assistant) {
    // Surface filter: only UI-scoped events render into the chat panel.
    // DEBUG / AUDIT events still arrive via SSE (useful for the
    // developer console and audit log consumer) but should not pollute
    // the user's reasoning trace. This matches the backend
    // ``SurfaceRouter`` classification in tradingagents/agent_harness/core/surface.py.
    const surface = payload?.surface;
    if (surface && surface !== "ui") return;
    switch (name) {
      case "plan_started":
        appendReasoningDelta(`▶ 意图识别: ${payload.intent || "?"}\n`);
        break;
      case "todo_list":
        // §0.4.33 — backend emitted the full checklist before plan_ready.
        // Render (or replace) the DOM checklist. tool_result events will
        // emit todo_update patches that we apply in place.
        renderTodoList(payload.items || payload.todos || []);
        break;
      case "todo_update":
        // §0.4.33 — single-row status patch. Updates one row's class +
        // icon + agent badge without rebuilding the list.
        updateTodoItem(payload);
        break;
      case "plan_ready": {
        // §14.3.4 — use friendly args in plan display (drop verbose
        // JSON.stringify for nested dicts).
        const steps = payload.steps || [];
        const planText = steps
          .map((s, i) => `${i + 1}. [${s.agent || "?"}] ${_friendlyArgs(s.args || {})}`)
          .join("\n");
        appendReasoningDelta(`📋 计划 (${steps.length} 步):\n${planText}\n`);
        break;
      }
      case "tool_call":
        appendToolCall(payload.name || "", payload.args || {});
        // §14.3.5 — only emit the trace delta once; appendToolCall
        // already shows the args, JSON.stringify was duplicating.
        break;
      case "tool_result":
        appendToolResult(payload.name, payload);
        if (payload.error) appendReasoningDelta(`  ✗ ${payload.error}\n`);
        else appendReasoningDelta(`  ✓ 数据已获取\n`);
        break;
      case "observed":
        appendReasoningDelta(
          `👀 观察: ${payload.tool_count || 0} tool_results, ${(payload.errors || []).length} errors\n`
        );
        break;
      case "verified":
        appendVerified(payload);
        appendReasoningDelta(`🔍 L${payload.level} 验证: ${payload.ok ? "pass" : "fail"}\n`);
        break;
      case "agent_final": {
        // §Step 26 — backend may emit ``result`` as a friendly markdown
        // string (rendered via display_view_for). When that happens we
        // just renderMarkdown() it; legacy dict-shape branches below
        // keep working for synthesised answers.
        let result = payload.result;
        if (result == null) result = {};
        if (typeof result === "string") {
          assistant.bubble.innerHTML = renderMarkdown(result);
          scrollToBottom();
          appendReasoningDelta(
            payload.tool_name
              ? `💡 SynthesizeNode 完成 (${payload.tool_name}, friendly)
`
              : `💡 SynthesizeNode 完成
`,
          );
          break;
        }
        // §N2 — multi-intent Tier 1 short-circuit returns
        // {multi: [{intent, op, tool, result}, ...], count: N} plus a
        // backend-computed ``summary`` (one-paragraph prose). Render
        // the summary as the lead so the bubble reads as a friendly
        // answer, then tuck the per-section raw tables into a
        // collapsible <details> for users who want detail.
        // Previously each section got its own <h4> heading + raw
        // table dumped into the bubble — that turned "看一下我的
        // 关注和 600036 的笔记" into a wall of pipe tables the user
        // already saw one layer down in the reasoning trace.
        if (Array.isArray(result.multi) && result.multi.length >= 1) {
          const summaryHtml = (payload.summary && String(payload.summary).trim())
            ? `<div class="harness-multi-summary">${renderMarkdown(payload.summary)}</div>`
            : "";
          // §0.4.25.1 — prefer backend-rendered ``display_html`` per
          // section so each multi-intent item becomes a friendly card.
          // Fall back to ``formatRawResult`` only when the backend
          // didn't emit display_html (legacy tools).
          const details = result.multi.map((s) => {
            const heading = intentHeading(s.intent);
            const cardHtml = s.result && typeof s.result.display_html === "string"
              && /^<div\s+class="(history|compare|quote|fundamentals|news|alpha|ack|error)-card"/i.test(s.result.display_html.trim())
              ? s.result.display_html
              : renderMarkdown(formatRawResult(s.result || {}, payload.tier) || "(无内容)");
            return `<details class="harness-multi-detail">
  <summary>${heading}</summary>
  <div class="harness-multi-body">${cardHtml}</div>
</details>`;
          }).join("");
          assistant.bubble.innerHTML = summaryHtml + details || "(空)";
          appendReasoningDelta(`💡 SynthesizeNode 完成 (multi-intent Tier 1, ${result.multi.length} sections)\n`);
          scrollToBottom();
          return;
        }
        // Tier 1 short-circuit paths skip the LLM synthesizer, so
        // result.summary is empty — fall back to client-side formatting.
        // For list_* tools that return ``{text: <markdown table>, count}``
        // the LLM sometimes just dumps the raw JSON; in that case
        // render the markdown table directly.
        let summary = result.summary;
        if (!summary) summary = formatRawResult(result, payload.tier);
        else if (looksLikeRawJsonDump(summary, result)) {
          summary = formatRawResult(result, payload.tier) || summary;
        }
        assistant.bubble.innerHTML = renderMarkdown(summary);
        scrollToBottom();
        appendReasoningDelta(`💡 SynthesizeNode 完成\n`);
        break;
      }
      case "answer_verified":
        appendAnswerVerified(payload);
        appendReasoningDelta(`🧑‍⚖️ L3 judge: ${payload.ok ? "grounded" : "ungrounded"} (score=${(payload.details?.score ?? 0).toFixed(2)})\n`);
        break;
      case "confirm_request":
        // HITL: write tool requires user approval. Inline banner only —
        // lives in the chat stream so the user stays in context. The
        // previous design also opened a centred modal which (a) was
        // visually clobbery and (b) could be clicked independently of
        // the banner, causing double-approval races. Single entry point.
        showHarnessConfirmInline(payload, assistant);
        break;
      case "audit_decision":
        // Approval / rejection recorded by /api/harness/sessions/.../confirm.
        if (harnessState.pendingConfirm) {
          harnessState.pendingConfirm.resolve();
          harnessState.pendingConfirm = null;
        }
        break;
      case "done":
        // End of stream. If we still have an open confirm dialog
        // (shouldn't happen), resolve it so the UI doesn't hang.
        if (harnessState.pendingConfirm) {
          harnessState.pendingConfirm.resolve();
          harnessState.pendingConfirm = null;
        }
        break;
      case "error": {
        // §14.3.3 — friendly fallback. Old behaviour dumped the
        // entire payload as JSON when payload.error was missing/null;
        // now fall back to a localised message + payload.failure.reason
        // (LLM-failure detail) when available.
        const errMsg = payload.error || "会话执行失败,请重试";
        const failureReason = payload.failure?.reason;
        const detail = failureReason ? ` (${failureReason})` : "";
        appendError(`${errMsg}${detail}`);
        appendReasoningDelta(`❌ 错误: ${errMsg}${detail}\n`);
        break;
      }
      default: {
        // §14.3.2 — 5 common-but-unhandled events get friendly rendering
        // instead of raw JSON dumps. Truly unknown events still fall
        // through to JSON (last-resort diagnostics for new event types).
        switch (name) {
          case "plan_ready_ptc":
            appendReasoningDelta(`📋 PTC 计划: ${payload.groups?.length || 0} 个并行组\n`);
            return;
          case "turn/started":
            appendReasoningDelta(`▶ Turn 开始 (turn_id=${payload.turn_id || "?"})\n`);
            return;
          case "warning": {
            const msg = payload.message || payload.fallback || "fallback";
            appendReasoningDelta(`⚠️ ${msg}\n`);
            return;
          }
          case "usage_summary":
            // Cost/tokens — typically rendered in a side panel, not
            // trace. Skip from trace to reduce noise.
            return;
          case "resume_complete":
            appendReasoningDelta(`🔄 会话恢复: ${payload.replayed_events || 0} 事件已重放\n`);
            return;
        }
        appendReasoningDelta(`  · ${name}: ${JSON.stringify(payload).slice(0, 120)}\n`);
      }
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // §P3-3+ — inline approval banner shown IN the chat. This is the
  // only confirmation UX now (the modal was dropped: it duplicated the
  // banner's buttons, opened a second focus context, and was visually
  // heavy). The banner lives inside the assistant bubble so the user
  // stays in conversation flow. Single approve / deny pair.
  // Centred modal-based HITL approval dialog. Replaces the inline banner
  // (which was lost inside agent_final's innerHTML overwrite and made
  // it easy to miss). Single entry point — no parallel inline UI.
  function showHarnessConfirmInline(payload, assistant) {
    const toolName = payload?.tool_name || "(tool)";
    const toolArgs = payload?.args || {};
    const impact = payload?.impact || {};
    const sessionId = ensureSession();

    // Hide any prior pending modal first so a fresh confirm request
    // replaces the old one rather than stacking.
    closeHarnessModal();

    const root = document.getElementById("modal-root");
    if (!root) return;
    root.innerHTML = `
      <div class="modal-overlay" data-harness-modal-bg></div>
      <div class="modal-dialog is-danger" role="dialog" aria-modal="true" aria-labelledby="harness-modal-title">
        <header class="modal-header">
          <h2 class="modal-title" id="harness-modal-title">
            <span class="modal-title-icon">⚠️</span>
            <span>写操作需要你确认</span>
          </h2>
        </header>
        <div class="modal-body" style="padding: 0 28px 18px;">
          <p style="margin: 0 0 14px; color: var(--ink);">
            工具 <code style="background: var(--panel-2); padding: 2px 8px; border-radius: 4px; font-size: 0.92em;">${escapeHtml(toolName)}</code> 将要执行修改操作。
          </p>
          ${impact.reason ? `<p style="margin: 0 0 12px; color: var(--muted); font-size: 0.9rem;">${escapeHtml(impact.reason)}</p>` : ""}
          <details style="margin-bottom: 16px;">
            <summary style="cursor: pointer; font-size: 0.85rem; color: var(--muted); user-select: none;">查看参数详情</summary>
            <pre style="margin: 8px 0 0; padding: 10px; background: var(--panel-2); border-radius: 6px; font-size: 0.8rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow-y: auto;">${escapeHtml(JSON.stringify(toolArgs, null, 2))}</pre>
          </details>
        </div>
        <footer class="modal-footer" style="padding: 14px 28px 22px; display: flex; gap: 10px; justify-content: flex-end; border-top: 1px solid var(--line);">
          <button type="button" class="text-button" data-harness-modal-cancel>拒绝</button>
          <button type="button" class="btn btn-primary" data-harness-modal-ok>批准</button>
        </footer>
      </div>
    `;
    root.classList.add("is-open");
    root.setAttribute("aria-hidden", "false");
    // Lock chat input while waiting for approval — prevents re-fire that
    // would race the in-flight confirm.
    setBusy(true);

    const decide = async (approve) => {
      const okBtn = root.querySelector("[data-harness-modal-ok]");
      const cancelBtn = root.querySelector("[data-harness-modal-cancel]");
      if (okBtn) okBtn.disabled = true;
      if (cancelBtn) cancelBtn.disabled = true;
      try {
        const resp = await fetch(
          `/api/harness/sessions/${encodeURIComponent(sessionId)}/confirm`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              tool_name: toolName,
              tool_args: toolArgs,
              approve,
              user_message: state.lastUserMessage || "",
              audit_id: payload?.audit_id,
            }),
          }
        );
        if (!resp.ok || !resp.body) {
          appendError(`审批请求失败 (HTTP ${resp.status})`);
          closeHarnessModal();
          return;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            handleSseBlock(block, assistant);
          }
        }
      } catch (e) {
        appendError(`审批流错误: ${e.message}`);
      } finally {
        closeHarnessModal();
      }
    };

    root.querySelector("[data-harness-modal-ok]").onclick = () => decide(true);
    root.querySelector("[data-harness-modal-cancel]").onclick = () => decide(false);
    // Click-on-backdrop does NOT auto-confirm. Single entry point.
    root.querySelector("[data-harness-modal-bg]").onclick = (event) => {
      if (event.target === event.currentTarget) {
        // explicit reject on backdrop click
        decide(false);
      }
    };
    // Focus the confirm button so Enter approves.
    root.querySelector("[data-harness-modal-ok]").focus();
  }

  function closeHarnessModal() {
    const root = document.getElementById("modal-root");
    if (!root) return;
    root.classList.remove("is-open");
    root.setAttribute("aria-hidden", "true");
    root.innerHTML = "";
    // only release busy if no in-flight streaming
    setBusy(state.busy);
  }

  function setBusy(busy) {
    state.busy = busy;
    state.sendBtn.disabled = busy;
    state.inputEl.disabled = busy;
  }

  // 暴露给 app.js
  window.TradingAgentsHarness = { init };
})();

// ════════════════════════════════════════════════════════
// Task 23 — dual-view message handling
// ════════════════════════════════════════════════════════
// Render agent_progress / repair_started / handoff_requested /
// waiting_user / run_recovered summaries and inspector pagination.
(function attachTask23() {
  "use strict";
  const TASK23_EVENT_TYPES = [
    "agent_progress", "repair_started", "handoff_requested",
    "waiting_user", "run_recovered",
  ];
  function renderTask23Event(eventType, payload) {
    const summary = (payload && (payload.summary || payload.stage)) || eventType;
    return summary;
  }
  function pollWithAfterSeq(afterSeq) {
    return { after_seq: afterSeq, since_seq: afterSeq };
  }
  window.TradingAgentsTask23 = {
    eventTypes: TASK23_EVENT_TYPES,
    render: renderTask23Event,
    pollCursor: pollWithAfterSeq,
  };
})();


// §0.4.21 — hover tooltip for history-card and compare-card SVG sparklines.
// The backend emits <polyline data-points='[{x,y,v}, ...]'> (single-series
// cards) or <polyline data-series-points='...' data-symbol='AAPL'> (multi-
// asset compare). We attach one delegated handler to document.body so we
// never need to re-bind when new cards arrive via SSE.
(function attachChartTooltip() {
  "use strict";

  function _findClosest(points, x) {
    if (!Array.isArray(points) || !points.length) return null;
    let best = points[0];
    let bestDx = Math.abs(best.x - x);
    for (let i = 1; i < points.length; i++) {
      const dx = Math.abs(points[i].x - x);
      if (dx < bestDx) { bestDx = dx; best = points[i]; }
    }
    return best;
  }

  function _positionTooltip(host, tip, clientX) {
    const rect = host.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const x = clientX - rect.left + 12;
    const y = rect.height - tipRect.height - 6;
    tip.style.left = Math.min(Math.max(0, x), rect.width - tipRect.width) + "px";
    tip.style.top = Math.max(0, y) + "px";
  }

  function _showTip(tip, html) {
    tip.innerHTML = html;
    tip.style.display = "block";
  }

  function _hideTip(tip) {
    tip.style.display = "none";
  }

  function _handleSingleSeries(svg, host, tip, evt) {
    const rect = svg.getBoundingClientRect();
    const x = ((evt.clientX - rect.left) / rect.width) * 560; // matches viewBox width
    const ptsRaw = svg.getAttribute("data-points");
    if (!ptsRaw) return;
    let pts;
    try { pts = JSON.parse(ptsRaw); } catch { return; }
    const closest = _findClosest(pts, x);
    if (!closest) return;
    const currency = svg.getAttribute("data-stroke") || "";
    _showTip(
      tip,
      `<div class="tt-date">#${pts.indexOf(closest) + 1}</div>` +
      `<div>¥${closest.v.toFixed(2)}</div>`
    );
    _positionTooltip(host, tip, evt.clientX);
  }

  function _handleMultiSeries(svg, host, tip, evt) {
    const rect = svg.getBoundingClientRect();
    const x = ((evt.clientX - rect.left) / rect.width) * 560;
    const lines = svg.querySelectorAll("polyline[data-series-points]");
    if (!lines.length) return;
    let rows = "";
    lines.forEach((ln) => {
      const sym = ln.getAttribute("data-symbol") || "?";
      const color = ln.getAttribute("data-series") || "#94a3b8";
      let pts = [];
      try { pts = JSON.parse(ln.getAttribute("data-series-points")); } catch {}
      const closest = _findClosest(pts, x);
      if (closest) {
        rows += `<div><span style="display:inline-block;width:8px;height:2px;background:${color};margin-right:4px;vertical-align:middle"></span>${sym}: ¥${closest.v.toFixed(2)}</div>`;
      }
    });
    if (!rows) return;
    _showTip(tip, rows);
    _positionTooltip(host, tip, evt.clientX);
  }

  function _bindCard(root) {
    const singleSvg = root.querySelector(".hc-chart svg[data-points]");
    const singleTip = root.querySelector(".hc-chart .hc-tooltip");
    if (singleSvg && singleTip) {
      singleSvg.addEventListener("mousemove", (e) => _handleSingleSeries(singleSvg, singleSvg.parentElement, singleTip, e));
      singleSvg.addEventListener("mouseleave", () => _hideTip(singleTip));
    }
    const multiSvg = root.querySelector(".cc-chart svg");
    const multiTip = root.querySelector(".cc-chart .cc-tooltip");
    if (multiSvg && multiTip) {
      multiSvg.addEventListener("mousemove", (e) => _handleMultiSeries(multiSvg, multiSvg.parentElement, multiTip, e));
      multiSvg.addEventListener("mouseleave", () => _hideTip(multiTip));
    }
  }

  function _scan(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(".history-card, .compare-card").forEach(_bindCard);
  }

  // Initial bind + observer for SSE-injected cards.
  document.addEventListener("DOMContentLoaded", () => _scan(document.body));
  if (document.body) _scan(document.body);
  const obs = new MutationObserver((muts) => {
    muts.forEach((m) => m.addedNodes.forEach((n) => {
      if (n.nodeType === 1) _scan(n);
    }));
  });
  obs.observe(document.body, { childList: true, subtree: true });
  window.TradingAgentsChartTooltip = { _scan };
})();
