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
    busy: false,
    sessionId: null,
    // 当前累积的 reasoning trace(下一个 tool_call/answer_verified 触发时收尾)
    traceEl: null,
    traceContent: "",
  };

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
    if (state.clearBtn) {
      state.clearBtn.addEventListener("click", clearConversation);
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

  function ensureSession() {
    if (!state.sessionId) {
      state.sessionId =
        "hc-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    }
    return state.sessionId;
  }

  function clearConversation() {
    if (state.busy) return;
    state.sessionId = null;
    state.messagesEl.innerHTML = "";
    renderWelcome();
  }

  // ─────────────────────────────────────────────────
  // 消息渲染
  // ─────────────────────────────────────────────────

  function appendMessage(role, content, meta) {
    const el = document.createElement("div");
    el.className = `harness-message is-${role}`;
    const bubble = document.createElement("div");
    bubble.className = "harness-message-bubble";
    bubble.textContent = content;
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
  }

  function appendToolCall(name, args) {
    const argsStr = Object.entries(args || {})
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    return appendMessage("tool-call", `🔧 ${name}(${argsStr})`);
  }

  function appendToolResult(name, payload) {
    const summary = payload?.error
      ? `❌ ${name || "tool"}: ${payload.error}`
      : `📥 ${name || "tool"}: ${JSON.stringify(payload?.result || payload).slice(0, 240)}`;
    return appendMessage("tool-result", summary);
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

  function renderWelcome() {
    if (state.messagesEl.children.length > 0) return;
    state.messagesEl.innerHTML = `
      <div class="harness-welcome">
        <div class="harness-welcome-icon">🤖</div>
        <div class="harness-welcome-title">理财通用 Agent (P8 Harness)</div>
        <div class="harness-welcome-lede">底层:6 sub-agents + 19 tools + LLM + L3 LLM-judge</div>
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
    }
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

  function dispatchEvent(name, payload, assistant) {
    switch (name) {
      case "plan_started":
        appendReasoningDelta(`▶ 意图识别: ${payload.intent || "?"}\n`);
        break;
      case "plan_ready": {
        const steps = payload.steps || [];
        const planText = steps
          .map((s, i) => `${i + 1}. [${s.agent}] ${JSON.stringify(s.args || {})}`)
          .join("\n");
        appendReasoningDelta(`📋 计划 (${steps.length} 步):\n${planText}\n`);
        break;
      }
      case "tool_call":
        appendToolCall(payload.name || "", payload.args || {});
        appendReasoningDelta(`→ 调用 ${payload.name}(${JSON.stringify(payload.args || {})})\n`);
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
        // SynthesizeNode 的最终回答(LLM 合成或 raw)
        const result = payload.result || {};
        const summary = result.summary || JSON.stringify(result, null, 2);
        // 替换 assistant 流式 bubble
        assistant.bubble.textContent = summary;
        scrollToBottom();
        appendReasoningDelta(`💡 SynthesizeNode 完成\n`);
        break;
      }
      case "answer_verified":
        appendAnswerVerified(payload);
        appendReasoningDelta(`🧑‍⚖️ L3 judge: ${payload.ok ? "grounded" : "ungrounded"} (score=${(payload.details?.score ?? 0).toFixed(2)})\n`);
        break;
      case "error":
        appendError(payload.error || JSON.stringify(payload));
        appendReasoningDelta(`❌ 错误: ${payload.error || ""}\n`);
        break;
      default:
        appendReasoningDelta(`  · ${name}: ${JSON.stringify(payload).slice(0, 120)}\n`);
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    state.sendBtn.disabled = busy;
    state.inputEl.disabled = busy;
  }

  // 暴露给 app.js
  window.TradingAgentsHarness = { init };
})();
