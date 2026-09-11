/* Stage C — Agent Chat Drawer
   SSE 流式 chat UI,通过 /api/agent/chat/stream 与后端通信。
   暴露 window.TradingAgentsAgentChat.init() 由 app.js 调用。 */
(() => {
  "use strict";

  const state = {
    drawer: null,
    messagesEl: null,
    inputEl: null,
    sendBtn: null,
    sessionId: null,
    busy: false,
    currentAssistantMsg: null,
    // Day 9: 当前累积的 reasoning 折叠面板(下一个 tool_call / done 触发时收尾)
    currentReasoningEl: null,
    currentReasoningContent: "",
    // 当前 pending 的写操作 confirm(等用户决定)
    pendingConfirm: null,
    // 上一次 user_message(用于 confirm 时 replay)
    lastUserMessage: "",
  };

  const SUGGESTIONS = [
    "招商银行 600036 现在多少钱?",
    "最近 30 天 A 股哪些 ETF 动量最强?",
    "对比一下 600036 和 600000 的 RSI",
    "我的关注列表里现在有什么?",
  ];

  // ─────────────────────────────────────────────────────
  // 初始化(由 app.js 调用)
  // ─────────────────────────────────────────────────────

  function init() {
    state.drawer = document.getElementById("agent-drawer");
    state.messagesEl = document.getElementById("agent-messages");
    state.inputEl = document.getElementById("agent-input");
    state.sendBtn = document.getElementById("agent-send-btn");
    const entryBtn = document.getElementById("agent-entry-btn");
    const closeBtn = document.getElementById("agent-close-btn");
    const clearBtn = document.getElementById("agent-clear-btn");

    if (!state.drawer || !state.messagesEl || !state.inputEl || !state.sendBtn) {
      console.warn("[AgentChat] DOM elements missing");
      return;
    }

    if (entryBtn) {
      entryBtn.addEventListener("click", openDrawer);
    }
    // Day 11: sidebar AI 入口也绑定 openDrawer
    const sidebarAgentBtn = document.getElementById("sidebar-agent-btn");
    if (sidebarAgentBtn) {
      sidebarAgentBtn.addEventListener("click", openDrawer);
    }
    if (closeBtn) {
      closeBtn.addEventListener("click", closeDrawer);
    }
    if (clearBtn) {
      clearBtn.addEventListener("click", clearConversation);
    }

    state.sendBtn.addEventListener("click", sendMessage);
    state.inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // 加载历史 session(可选 — 显示在 welcome 区域)
    renderWelcome();

    if (typeof window.__TA_MODULES__ !== "undefined") {
      window.__TA_MODULES__.TradingAgentsAgentChat = { open: openDrawer, close: closeDrawer, send: sendMessage };
    }
  }

  // ─────────────────────────────────────────────────────
  // 抽屉控制
  // ─────────────────────────────────────────────────────

  async function openDrawer() {
    if (!state.drawer) return;
    state.drawer.hidden = false;
    state.inputEl.focus();
    // 第一次打开时创建 session
    if (!state.sessionId) {
      await ensureSession();
      // 拉历史(如果有)
      await loadHistory();
    }
  }

  function closeDrawer() {
    if (state.drawer) state.drawer.hidden = true;
  }

  // ─────────────────────────────────────────────────────
  // Session 管理
  // ─────────────────────────────────────────────────────

  async function ensureSession() {
    try {
      const resp = await fetch("/api/agent/sessions", { method: "POST" });
      if (!resp.ok) {
        console.error("[AgentChat] failed to create session:", resp.status);
        return;
      }
      const data = await resp.json();
      state.sessionId = data.session_id;
    } catch (e) {
      console.error("[AgentChat] ensureSession error:", e);
    }
  }

  async function loadHistory() {
    if (!state.sessionId) return;
    try {
      const resp = await fetch(`/api/agent/sessions/${encodeURIComponent(state.sessionId)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      const history = data.history || [];
      if (history.length > 0) {
        // 清空 welcome,显示历史
        state.messagesEl.innerHTML = "";
        for (const h of history) {
          if (h.role === "user") {
            appendMessage("user", h.content);
          } else if (h.role === "assistant") {
            appendMessage("assistant", h.content);
          }
        }
      }
    } catch (e) {
      console.warn("[AgentChat] loadHistory error:", e);
    }
  }

  async function clearConversation() {
    if (!confirm("确定要清空当前对话吗?(新建 session)")) return;
    state.sessionId = null;
    state.messagesEl.innerHTML = "";
    await ensureSession();
    renderWelcome();
  }

  // ─────────────────────────────────────────────────────
  // 消息渲染
  // ─────────────────────────────────────────────────────

  function appendMessage(role, content, meta) {
    const el = document.createElement("div");
    el.className = `agent-message is-${role}`;
    const bubble = document.createElement("div");
    bubble.className = "agent-message-bubble";
    bubble.textContent = content;
    el.appendChild(bubble);
    if (meta) {
      const m = document.createElement("div");
      m.className = "agent-message-meta";
      m.textContent = meta;
      el.appendChild(m);
    }
    state.messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  // ─────────────────────────────────────────────────────
  // Day 9 — Reasoning Trace 折叠面板
  // ─────────────────────────────────────────────────────

  function appendReasoningTrace(content) {
    // 折叠面板 reasoning trace:summary "🤔 AI 推理过程",默认展开让用户看到 AI 在思考。
    // 下一个 tool_call / done 触发时由 finalizeReasoningTrace() 收起。
    const el = document.createElement("details");
    el.className = "agent-message is-reasoning";
    el.open = true;
    const summary = document.createElement("summary");
    summary.className = "agent-reasoning-summary";
    summary.textContent = "🤔 AI 推理过程(实时)";
    const body = document.createElement("div");
    body.className = "agent-reasoning-body";
    body.textContent = content;
    el.appendChild(summary);
    el.appendChild(body);
    state.messagesEl.appendChild(el);
    state.currentReasoningEl = el;
    state.currentReasoningContent = content;
    scrollToBottom();
    return el;
  }

  function appendReasoningDelta(delta) {
    // 增量更新 reasoning 折叠面板(类似 streaming assistant bubble)
    if (!state.currentReasoningEl) {
      appendReasoningTrace(delta);
      return;
    }
    state.currentReasoningContent += delta;
    const body = state.currentReasoningEl.querySelector(".agent-reasoning-body");
    if (body) body.textContent = state.currentReasoningContent;
    scrollToBottom();
  }

  function finalizeReasoningTrace() {
    // 收起 reasoning 折叠面板,清空 current 状态。
    // 内容太短(< 5 字符,通常是空的或只有 markup)直接移除避免噪音。
    if (!state.currentReasoningEl) return;
    if (state.currentReasoningContent.trim().length < 5) {
      state.currentReasoningEl.remove();
    } else {
      state.currentReasoningEl.open = false;
      const summary = state.currentReasoningEl.querySelector(".agent-reasoning-summary");
      if (summary) summary.textContent = "🤔 AI 推理过程(点击展开)";
    }
    state.currentReasoningEl = null;
    state.currentReasoningContent = "";
  }

  function appendStreamingMessage(role) {
    const el = document.createElement("div");
    el.className = `agent-message is-${role} is-loading`;
    const bubble = document.createElement("div");
    bubble.className = "agent-message-bubble";
    bubble.textContent = "";
    el.appendChild(bubble);
    state.messagesEl.appendChild(el);
    scrollToBottom();
    state.currentAssistantMsg = { el, bubble, content: "" };
    return state.currentAssistantMsg;
  }

  function appendToolCall(name, args) {
    const argsStr = Object.entries(args || {})
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    return appendMessage("tool-call", `🔧 ${name}(${argsStr})`);
  }

  function appendToolResult(content) {
    // 截断过长结果
    const text = content.length > 500 ? content.slice(0, 500) + "..." : content;
    return appendMessage("tool-result", text);
  }

  function appendError(content) {
    return appendMessage("error", content);
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
    });
  }

  function renderWelcome() {
    if (state.messagesEl.children.length > 0) return;
    state.messagesEl.innerHTML = `
      <div class="agent-welcome">
        <div class="agent-welcome-icon">💬</div>
        <div class="agent-welcome-title">理财通用 Agent</div>
        <div>可以问我行情、因子、定时任务、告警等</div>
        <div class="agent-welcome-suggestions">
          ${SUGGESTIONS.map(
            (s) => `<div class="agent-suggestion-chip" data-suggestion="${s.replace(/"/g, "&quot;")}">${s}</div>`
          ).join("")}
      </div>
    `;
    // 点击 suggestion 自动填到输入框
    state.messagesEl.querySelectorAll(".agent-suggestion-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        state.inputEl.value = chip.dataset.suggestion;
        state.inputEl.focus();
      });
    });
  }

  // ─────────────────────────────────────────────────────
  // 发送消息(SSE 流式)
  // ─────────────────────────────────────────────────────

  async function sendMessage() {
    if (state.busy) return;
    const text = state.inputEl.value.trim();
    if (!text) return;

    await ensureSession();
    if (!state.sessionId) {
      appendError("无法创建 session,请稍后再试");
      return;
    }

    // 清空 welcome(如果有)
    const welcome = state.messagesEl.querySelector(".agent-welcome");
    if (welcome) welcome.remove();

    // 用户消息
    appendMessage("user", text);
    state.inputEl.value = "";
    state.lastUserMessage = text;
    setBusy(true);

    // 流式接收 assistant 回答
    const assistant = appendStreamingMessage("assistant");

    try {
      const resp = await fetch("/api/agent/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, user_message: text }),
      });

      if (!resp.ok) {
        assistant.bubble.textContent = `[错误] HTTP ${resp.status}`;
        assistant.el.classList.remove("is-loading");
        assistant.el.classList.add("is-error");
        setBusy(false);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // 解析 SSE 事件(event: ...\ndata: ...\n\n)
        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const lines = raw.split("\n");
          let eventType = null;
          let dataLine = null;
          for (const line of lines) {
            if (line.startsWith("event: ")) eventType = line.slice(7).trim();
            else if (line.startsWith("data: ")) dataLine = line.slice(6).trim();
          }
          if (eventType && dataLine) {
            handleSseEvent(eventType, dataLine, assistant);
          }
        }
      }
    } catch (e) {
      assistant.bubble.textContent = `[错误] ${e.message || e}`;
      assistant.el.classList.remove("is-loading");
      assistant.el.classList.add("is-error");
    } finally {
      setBusy(false);
    }
  }

  function handleSseEvent(eventType, dataLine, assistant) {
    let data;
    try {
      data = JSON.parse(dataLine);
    } catch (_) {
      return;
    }

    if (eventType === "reasoning") {
      const content = data.payload?.content || "";
      // Day 11c: reasoning 只走折叠面板(AI 思考过程)。
      // 不再 inline 到 assistant bubble,因为 reasoning-only 模型(如 MiniMax)
      // 全部 content 都在 reasoning 流,inline 会让 bubble 显示思考过程而不是
      // 最终答案,且会和折叠面板内容重复。
      appendReasoningDelta(content);
      scrollToBottom();
    } else if (eventType === "tool_call") {
      // 第一个 tool_call 时收起前面累积的 reasoning trace
      finalizeReasoningTrace();
      const name = data.payload?.name || "(tool)";
      const args = data.payload?.args || {};
      appendToolCall(name, args);
    } else if (eventType === "tool_result") {
      const content = data.payload?.content || "";
      appendToolResult(content);
    } else if (eventType === "confirm_request") {
      // HITL: 写操作需要用户确认
      showConfirmDialog(data.payload);
    } else if (eventType === "audit_decision") {
      // 用户决策已记录
      appendMessage("audit", `✓ 决策已记录 (audit_id=${data.payload?.audit_id}, approved=${data.payload?.approved})`);
      if (state.pendingConfirm) {
        state.pendingConfirm.resolve();
        state.pendingConfirm = null;
      }
    } else if (eventType === "error") {
      const errMsg = data.payload?.error || "未知错误";
      appendError(errMsg);
      if (state.pendingConfirm) {
        state.pendingConfirm.resolve();
        state.pendingConfirm = null;
      }
    } else if (eventType === "done") {
      // Day 11c: 收尾时,如果 bubble 还是空的,说明 reasoning 流就是 LLM 的"完整输出"。
      // (reasoning-only 模型 / 或者 LLM 决定不调 tool 直接给答案)
      // 从折叠面板取尾部内容(最后 1500 字符)作为最终答案塞进 bubble。
      const tail = (state.currentReasoningContent || "").trim();
      if (assistant.bubble && !assistant.bubble.textContent.trim() && tail) {
        const MAX_TAIL = 1500;
        assistant.bubble.textContent =
          tail.length > MAX_TAIL
            ? `…(已截断,见上方思考过程)

${tail.slice(-MAX_TAIL)}`
            : tail;
      }
      // Day 9: 流结束收尾 reasoning + 移除 streaming 状态
      finalizeReasoningTrace();
      assistant.el.classList.remove("is-loading");
      state.currentAssistantMsg = null;
      if (state.pendingConfirm) {
        // stream 结束但 confirm 没处理(不应该发生)
        state.pendingConfirm.resolve();
        state.pendingConfirm = null;
      }
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    state.sendBtn.disabled = busy;
    state.sendBtn.textContent = busy ? "发送中…" : "发送";
    state.inputEl.disabled = busy;
  }

  // ─────────────────────────────────────────────────────
  // Confirm dialog(HITL 写操作确认)
  // ─────────────────────────────────────────────────────

  function showConfirmDialog(payload) {
    // payload: {tool_call_id, tool_name, tool_args, impact, audit_id}
    if (state.pendingConfirm) {
      // 已经有 pending — 提示前端不能叠加
      appendError("已有待确认的写操作,请先处理");
      return;
    }

    // 把 confirm_request 作为一条消息加进 UI
    const confirmEl = document.createElement("div");
    confirmEl.className = "agent-message is-confirm";

    const header = document.createElement("div");
    header.className = "agent-confirm-header";
    header.textContent = "⚠️ 写操作需要你确认";

    const toolName = document.createElement("div");
    toolName.className = "agent-confirm-tool";
    toolName.textContent = `🔧 ${payload.tool_name}`;

    const args = document.createElement("pre");
    args.className = "agent-confirm-args";
    args.textContent = JSON.stringify(payload.tool_args, null, 2);

    const impact = document.createElement("div");
    impact.className = "agent-confirm-impact";
    impact.textContent = `📋 影响: ${payload.impact || "执行写操作"}`;

    const auditInfo = document.createElement("div");
    auditInfo.className = "agent-confirm-audit";
    auditInfo.textContent = `audit_id: ${payload.audit_id}`;

    const btnRow = document.createElement("div");
    btnRow.className = "agent-confirm-actions";
    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "agent-confirm-approve";
    approveBtn.textContent = "✓ 确认执行";
    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "agent-confirm-reject";
    rejectBtn.textContent = "✕ 拒绝";
    btnRow.appendChild(approveBtn);
    btnRow.appendChild(rejectBtn);

    confirmEl.appendChild(header);
    confirmEl.appendChild(toolName);
    confirmEl.appendChild(args);
    confirmEl.appendChild(impact);
    confirmEl.appendChild(auditInfo);
    confirmEl.appendChild(btnRow);
    state.messagesEl.appendChild(confirmEl);
    scrollToBottom();

    // pending 标记(等 audit_decision 或 error 事件 resolve)
    let resolveFn;
    const promise = new Promise((res) => { resolveFn = res; });
    state.pendingConfirm = {
      payload,
      resolve: resolveFn,
      promise,
    };

    approveBtn.addEventListener("click", () => sendConfirmDecision(true, payload, approveBtn, rejectBtn));
    rejectBtn.addEventListener("click", () => sendConfirmDecision(false, payload, approveBtn, rejectBtn));
  }

  async function sendConfirmDecision(approve, payload, approveBtn, rejectBtn) {
    // 禁用按钮
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    approveBtn.textContent = approve ? "执行中…" : "处理中…";

    try {
      const resp = await fetch(`/api/agent/sessions/${encodeURIComponent(state.sessionId)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          audit_id: payload.audit_id,
          tool_name: payload.tool_name,
          tool_args: payload.tool_args,
          tool_call_id: payload.tool_call_id,
          approve,
          user_message: state.lastUserMessage,
        }),
      });

      if (!resp.ok) {
        appendError(`confirm HTTP ${resp.status}`);
        if (state.pendingConfirm) {
          state.pendingConfirm.resolve();
          state.pendingConfirm = null;
        }
        return;
      }

      // 流式接收 resume 后的结果
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const assistant = appendStreamingMessage("assistant");
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 4);
          const lines = raw.split("\n");
          let eventType = null;
          let dataLine = null;
          for (const line of lines) {
            if (line.startsWith("event: ")) eventType = line.slice(7).trim();
            else if (line.startsWith("data: ")) dataLine = line.slice(6).trim();
          }
          if (eventType && dataLine) {
            handleSseEvent(eventType, dataLine, assistant);
            // 如果又有 confirm_request,停止当前 resume 处理,让 confirm dialog 处理
            if (eventType === "confirm_request") return;
          }
        }
      }
    } catch (e) {
      appendError(`confirm 失败: ${e.message || e}`);
      if (state.pendingConfirm) {
        state.pendingConfirm.resolve();
        state.pendingConfirm = null;
      }
    }
  }

  // ─────────────────────────────────────────────────────
  // 暴露
  // ─────────────────────────────────────────────────────

  window.TradingAgentsAgentChat = {
    init: init,
    open: openDrawer,
    close: closeDrawer,
    send: sendMessage,
  };

  if (typeof window.__TA_MODULES__ !== "undefined") {
    window.__TA_MODULES__.TradingAgentsAgentChat = window.TradingAgentsAgentChat;
  }
})();
