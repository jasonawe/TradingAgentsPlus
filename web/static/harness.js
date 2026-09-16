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

  // Minimal client-side markdown renderer scoped to what the harness
  // LLM emits: # / ## / ### headers, **bold**, *italic*, pipe tables,
  // bullet lists (-), numbered lists, and inline code. Escapes HTML
  // before applying markdown so injected scripts can\'t break out.
  function renderMarkdown(md) {
    if (!md) return "";
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
  function formatRawResult(result, tier) {
    if (!result || typeof result !== "object") return JSON.stringify(result || {}, null, 2);
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
        // Tier 1 short-circuit paths don't go through the LLM synthesizer,
        // so result.summary is empty — fall back to client-side formatting
        // instead of dumping raw QuoteResult JSON to the user.
        const summary = result.summary || formatRawResult(result, payload.tier);
        // Use innerHTML with escaped markdown rendering — textContent would
        // display the raw "#", "|", "**" characters.
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
        // HITL: write tool requires user approval. Show BOTH the
        // modal (primary UX) and the inline banner (fallback when
        // CSS is stale or the modal is dismissed). Both call the
        // same /confirm endpoint.
        showHarnessConfirmDialog(payload);
        showHarnessConfirmInline(payload);
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
      case "error":
        appendError(payload.error || JSON.stringify(payload));
        appendReasoningDelta(`❌ 错误: ${payload.error || ""}\n`);
        break;
      default:
        appendReasoningDelta(`  · ${name}: ${JSON.stringify(payload).slice(0, 120)}\n`);
    }
  }

  // HITL: write-tool confirm dialog
  async function showHarnessConfirmDialog(payload) {
    if (harnessState.pendingConfirm) {
      appendError("已有待确认的写操作,请先处理");
      return;
    }
    const toolName = payload?.tool_name || "(tool)";
    const toolArgs = payload?.args || {};
    const impact = payload?.impact || {};

    const overlay = document.createElement("div");
    overlay.className = "harness-confirm-overlay";
    const dialog = document.createElement("div");
    dialog.className = "harness-confirm-dialog";
    dialog.innerHTML = `
      <h3>⚠️ 写操作需要你确认</h3>
      <p class="harness-confirm-tool">工具:<code>${escapeHtml(toolName)}</code></p>
      <pre class="harness-confirm-args">${escapeHtml(JSON.stringify(toolArgs, null, 2))}</pre>
      ${impact.reason ? `<p class="harness-confirm-impact">原因:${escapeHtml(impact.reason)}</p>` : ""}
      <div class="harness-confirm-buttons">
        <button type="button" class="harness-confirm-cancel">拒绝</button>
        <button type="button" class="harness-confirm-ok">批准</button>
      </div>
    `;
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    const sessionId = ensureSession();
    const decide = async (approve) => {
      dialog.querySelectorAll("button").forEach(b => b.disabled = true);
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
          overlay.remove();
          return;
        }
        // Stream the post-approval events into the same UI.
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        // Suspend the regular handler's reasoning block; we'll route
        // these events through the same dispatchEvent path so the UI
        // updates in lock-step.
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            // Reuse the existing SSE parser.
            handleSseBlock(block, assistant);
          }
        }
      } catch (e) {
        appendError(`审批流错误: ${e.message}`);
      } finally {
        overlay.remove();
      }
    };

    dialog.querySelector(".harness-confirm-cancel").onclick = () => decide(false);
    dialog.querySelector(".harness-confirm-ok").onclick = () => decide(true);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // §P3-3+ — inline approval banner shown IN the chat (alongside
  // the modal). The modal is the primary UX; this is the fallback
  // for when CSS doesn't load or the modal is dismissed. The user
  // can click 批准 / 拒绝 directly in the chat stream.
  function showHarnessConfirmInline(payload) {
    const toolName = payload?.tool_name || "(tool)";
    const toolArgs = payload?.args || {};
    const impact = payload?.impact || {};
    const sessionId = ensureSession();
    const banner = document.createElement("div");
    banner.className = "harness-confirm-inline";
    banner.innerHTML = `
      <div class="harness-confirm-inline-header">
        ⚠️ 写操作需要你确认 <small>(${escapeHtml(toolName)})</small>
      </div>
      <pre class="harness-confirm-inline-args">${escapeHtml(JSON.stringify(toolArgs, null, 2))}</pre>
      ${impact.reason ? `<div class="harness-confirm-inline-impact">${escapeHtml(impact.reason)}</div>` : ""}
      <div class="harness-confirm-inline-buttons">
        <button type="button" class="harness-confirm-inline-cancel">拒绝</button>
        <button type="button" class="harness-confirm-inline-ok">批准</button>
      </div>
    `;
    // Insert at the end of the chat assistant bubble
    const chat = document.querySelector("#chat-log, .chat-log, #messages, .messages, .assistant-bubble, .chat-container");
    if (chat) {
      chat.appendChild(banner);
    } else {
      document.body.appendChild(banner);
    }
    scrollToBottom();

    const decide = async (approve) => {
      banner.querySelectorAll("button").forEach(b => b.disabled = true);
      banner.classList.add(approve ? "harness-confirm-inline-approved" : "harness-confirm-inline-rejected");
      const labelEl = banner.querySelector(".harness-confirm-inline-header small");
      if (labelEl) labelEl.textContent = `${escapeHtml(toolName)} · ${approve ? "已批准" : "已拒绝"}`;
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
      }
    };
    banner.querySelector(".harness-confirm-inline-cancel").onclick = () => decide(false);
    banner.querySelector(".harness-confirm-inline-ok").onclick = () => decide(true);
  }

  function setBusy(busy) {
    state.busy = busy;
    state.sendBtn.disabled = busy;
    state.inputEl.disabled = busy;
  }

  // 暴露给 app.js
  window.TradingAgentsHarness = { init };
})();
