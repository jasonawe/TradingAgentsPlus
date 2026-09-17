# 2026-09-17 — Harness 工具结果用户态渲染（User-facing Tool Result Renderer）

**Date:** 2026-09-17
**Stage:** C Day 14 收尾后的增量设计（Harness UX 一致性问题）
**Status:** 🚧 **Draft** — 待 9 轮 review + commit
**Branch:** `codex/harness-phase1-hardening`（继续在已有分支上迭代）
**前置依赖:** `2026-09-12-agent-harness-modularization.md`（已落地）

---

## 0. TL;DR — 三句话回答用户的问题

**问题**:用户在 harness 页面写入笔记后，assistant bubble 显示的是原始 JSON
```json
{"status":"created","raw":"NOTE_CREATED: {\"id\":\"note-xxx\",\"symbol\":\"600036.SS\"}"}
```
而不是人话「笔记已保存 (600036.SS)」。**这是个底层架构缺陷，不是补丁**：

1. `_invoke_bridge` 返回的工具结果只有 `{status, raw}`，**没有结构化字段也没有预渲染 summary**——下游所有"友好化"逻辑都得自己重新解析 raw 字符串
2. 真正负责"write tool → agent_final"的路径有 **2 条**：`stream_chat()` 走 Tier 2（会跑 `_trivial_crud_summary`）✅；`/confirm` 端点**绕过 orchestrator 直接 invoke tool**（line 740-820 in `web/app.py`），**完全没有 summary 概念** ❌
3. 前端 `formatRawResult` 只认识 `text/price/items/factors/watchlist` 这几种 Tier 1 read shape，**对 write CRUD 的 `{status, raw}` 一无所知**，直接 `JSON.stringify` 兜底

**答案（推荐方案 C+）**:
- 把"status → 中文短句"提炼成一个 **纯函数 `summarize_tool_result(result)`** 作为 single source of truth
- 三个调用点统一调用它：`_trivial_crud_summary` / `_invoke_bridge` / `/confirm` 端点
- 前端 `appendToolResult` 把 `pending_approval` 那条特殊分支扩展为通用 status→emoji+短句（不依赖后端 summary，独立可用）

**效果**:用户输入「给 600036 加个笔记：xx」→ 整链路返回「笔记已保存 (600036.SS)」，trace 里也是「✅ 笔记已保存」而不是 `{"status":"created","raw":"NOTE_CREATED: ..."}`。

---

## 1. Background — 为什么现在还在出 JSON

### 1.1 当前两条写操作路径

```
Tier 2 路径 (stream_chat, "create_note" 这类 plan step)
───────────────────────────────────────────────────────
user → classify → (intent=NOTE, op=CREATE) → _CRUD_DISPATCH
    → _execute() → tool.invoke(create_note) → {status: "pending..."}
    → SSE confirm_request → 用户点 批准 → /confirm 端点
    → grant_approval(sid, tool_name, args) → 然后:

    ┌──────────────────────────────────────────────────────────┐
    │ 路径 A (旧, 已废): /confirm 调 stream_chat 重跑           │
    │     → _trivial_crud_summary(state.tool_results) 走通      │
    │     → agent_final {result: {summary: "笔记已保存..."}}    │
    │     → 渲染友好                                         │
    └──────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────┐
    │ 路径 B (现, web/app.py:740-820): /confirm 直接 invoke     │
    │     → tool.invoke() → {status: "created", raw: "NOTE..."} │
    │     → 手动 yield SSE: tool_call → tool_result → agent_final│
    │     → agent_final {result: {status, raw}} (没 summary)    │
    │     → 前端 formatRawResult 不认 → JSON.stringify 兜底  │
    └──────────────────────────────────────────────────────────┘
```

**根因**:`_invoke_bridge`（`tradingagents/agent_harness/tools/builtin.py:689`）的 `_parse_bridge_text` 函数只返回 `{status, raw}`，**没有任何结构化字段**（没有 `note: {id, body, symbol}`，没有 `summary`）。下游要么重新解析 raw 字符串，要么放弃。

### 1.2 前端 `formatRawResult` 的认知边界

`web/static/harness.js:475-553` 的 `formatRawResult` 是个 Tier 1 read 结果的漂亮打印机，认识这些 shape：

| 触发条件                                     | 渲染样式                       |
| -------------------------------------------- | ------------------------------ |
| `result.text: string && result.count: number`| `result.text`（list_* 工具）   |
| `result.price: number` + symbol/change/volume| `📈 ${symbol} 价格: ¥${price}` |
| `result.candles: array`                      | `📊 K 线...`                   |
| `result.pe_ratio / pb_ratio / market_cap`    | `💼 基本面...`                 |
| `result.items: array`                        | `📰 news...`                   |
| `result.factors: array`                      | `🔢 因子...`                   |
| `result.watchlist: array`                    | `📋 watchlist...`              |
| **fallback**                                 | `JSON.stringify(result, null, 2)` ← **罪魁** |

**write CRUD 的 `{status: "created", raw: "NOTE_CREATED: {...}"}` 不命中任何已知 shape → 直接进 fallback → 原样吐 JSON**。

### 1.3 `_format_trivial_summary` 现状

`tradingagents/agent_harness/core/orchestrator.py:2575` 已经存在：

```python
def _format_trivial_summary(tool_results):
    parts = []
    pending = False
    for r in tool_results:
        status = r.get("status")
        raw = r.get("raw") or ""
        symbol = r.get("symbol")
        if status == "created" and "NOTE_CREATED" in raw:
            parts.append(f"笔记已保存{symbol_part(symbol)}")
        elif status == "created" and "ALERT_CREATED" in raw:
            parts.append(f"告警已创建{symbol_part(symbol)}")
        # ... 等等
```

**问题**:这个函数**只被 `_trivial_crud_summary` 调用**，被包在 `stream_chat()` 的 Tier 2 路径里。`/confirm` 端点根本不知道这个函数的存在。

---

## 2. 三个候选方案

### Option A — 后端纯函数 + 三处统一调用

**核心改动**:
1. 把 `_format_trivial_summary` 拆成两层：
   - `_summarize_one(r: dict) -> str | None` — 单条 result → 中文短句（pure function）
   - `_format_trivial_summary(tool_results)` — 列表聚合，调用前者
2. `/confirm` 端点在 emit `agent_final` 之前调用 `_summarize_one`，把 `summary` 字段塞进 result
3. `_invoke_bridge` 也调用 `_summarize_one`，把 `summary` 写进 result dict

**优点**:真正 single source of truth。`_summarize_one` 是 pure function，单测覆盖率高。
**缺点**:`_invoke_bridge` 现在是 lazy parse JSON，调 `_summarize_one` 要在所有 status 上覆盖，需要枚举所有 CRUD shape。

### Option B — 工具结果结构化（envelope pattern）

**核心改动**:
1. 定义 `ToolResult` Pydantic model:
   ```python
   class ToolResult(BaseModel):
       status: Literal["ok", "created", "updated", "deleted", "duplicate",
                       "pending_approval", "error", "empty", "no_data"]
       summary: str | None = None  # pre-computed friendly text
       entity: dict | None = None  # typed payload (note/alert/watchlist)
       raw: str | None = None      # legacy bridge string
       gate: dict | None = None    # for pending_approval
   ```
2. 所有 tool 改为返回 `ToolResult` 而非裸 dict
3. `_invoke_bridge` 解析 bridge string 后立刻填充 `summary` + `entity`
4. 前端 `appendToolResult` + `agent_final` 都从 `result.summary` 读取（fallback 到 `formatRawResult`）

**优点**:类型安全、强制每个 tool 都填 summary、扩展性好（新加 tool 类型只需要在 `_summarize_one` 加分支）
**缺点**:**大重构**——改 50+ 个 tool 调用点、几十个 test fixture，2-3 天工作量

### Option C — Hybrid (推荐)：A 的纯函数 + B 的 envelope 渐进迁移

**核心改动**:
1. **抽出纯函数 `_summarize_tool_result(r: dict) -> str | None`**（Option A 的内核），放 `tradingagents/agent_harness/core/result_formatter.py`
2. **后端 3 处调用**:
   - `_trivial_crud_summary`（已有，重构内部逻辑）
   - `_invoke_bridge._parse_bridge_text`（在 `_BRIDGE_PREFIXES` 分支里加 `summary` 字段）
   - `/confirm` 端点（emit `agent_final` 前调一次，结果放进 `result["summary"]`）
3. **前端最小改动**:
   - `appendToolResult` 现有 `pending_approval` 分支扩展为 switch，覆盖 `created/updated/deleted/duplicate/ok` 等 status（与后端中文短句保持一致，但独立 hardcode，不依赖后端字段）
   - `agent_final` handler 不动（已经读 `result.summary`）

**优点**:
- 不破坏现有 API（tool 还是返回 dict）
- 改动面小，1 天落地（包含测试）
- `_summarize_tool_result` 是 pure function，**测试可以覆盖所有 status 组合**
- 前端 trace 渲染独立可用——即使后端漏发 summary，前端 trace 也不会吐 JSON

**缺点**:
- `_summarize_tool_result` 的中文短句模板在前端 hardcode 了一份（minor duplication，可接受）
- 真要做 envelope 化时还得再来一次（但 API 已经是 dict，envelope 化时迁移成本低）

---

## 3. 推荐方案 C 的详细设计

### 3.1 新文件 `tradingagents/agent_harness/core/result_formatter.py`

```python
"""Single source of truth: tool result → 用户态中文短句。

所有把 {status, raw} 翻译成"笔记已保存 (600036.SS)"这种短句的逻辑
都集中在这里。前端 trace 渲染有自己独立的 mapping(见 harness.js
appendToolResult 的 STATUS_BADGE 表),但语义保持一致。
"""
from __future__ import annotations
from typing import Any

_ENTITY_PREFIXES = {
    "NOTE_CREATED:": "note",   "NOTE_UPDATED:": "note",   "NOTE_DELETED:": "note",
    "ALERT_CREATED:": "alert", "ALERT_UPDATED:": "alert", "ALERT_DELETED:": "alert",
}

_TEMPLATES = {
    ("created", "note"):   "笔记已保存",
    ("updated", "note"):   "笔记已更新",
    ("deleted", "note"):   "笔记已删除",
    ("created", "alert"):  "告警已创建",
    ("updated", "alert"):  "告警已更新",
    ("deleted", "alert"):  "告警已删除",
}


def _extract_symbol_from_raw(raw: str) -> str | None:
    """从 NOTE_CREATED: {\"symbol\": \"600036.SS\"} 里抠 symbol。"""
    import json as _json
    if ":" not in raw:
        return None
    try:
        payload = _json.loads(raw.split(":", 1)[1].strip())
    except Exception:
        return None
    return payload.get("symbol") if isinstance(payload, dict) else None


def summarize_tool_result(result: dict[str, Any]) -> str | None:
    """单条工具结果 → 用户态中文短句。

    Returns None when the result is not a trivial CRUD ack
    (i.e. caller should fall through to LLM synthesis / client-side
    formatRawResult). 这是 backend / frontend 渲染层唯一允许读这个
    函数的地方(through this module) — 其他地方想要中文短句一律
    走这条路,不要重新发明。
    """
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    raw = result.get("raw") or ""
    symbol = result.get("symbol")
    entity = None

    if status in ("created", "updated", "deleted"):
        for prefix, ent in _ENTITY_PREFIXES.items():
            if prefix in raw:
                entity = ent
                if not symbol:
                    symbol = _extract_symbol_from_raw(raw)
                break

    if entity:
        tmpl = _TEMPLATES.get((status, entity))
        if tmpl:
            return f"{tmpl} ({symbol})" if symbol else tmpl

    if status == "duplicate":
        return f"{symbol or ''} 已在关注列表中,无需重复添加".strip()
    if status == "pending_approval":
        return None
    if status == "ok" and "PREFERENCE_UPDATED" in raw:
        return "偏好已更新"
    if status == "ok" and "added" in raw.lower():
        return f"{symbol or ''} 已加入关注".strip()
    if status == "ok" and not raw:
        return None
    return None


def summarize_tool_results(results: list[dict[str, Any]]) -> str:
    """批量:每条独立渲染,换行拼接。"""
    parts = []
    for r in results or []:
        s = summarize_tool_result(r)
        if s:
            parts.append(s)
    return "\n".join(parts)
```

### 3.2 改动点 1 — `_invoke_bridge._parse_bridge_text` 填充 `summary`

```python
# tradingagents/agent_harness/tools/builtin.py:678
def _parse_bridge_text(text):
    if not isinstance(text, str):
        parsed = {"status": "ok", "raw": str(text)}
    else:
        parsed = None
        for prefix in _BRIDGE_PREFIXES:
            if text.startswith(prefix):
                status_map = { ... }
                parsed = {"status": status_map[prefix], "raw": text}
                break
        if parsed is None:
            parsed = {"status": "ok", "raw": text}

    # NEW: 给单条结果填 summary 字段,后续所有 render 都直接读它
    from tradingagents.agent_harness.core.result_formatter import summarize_tool_result
    summary = summarize_tool_result(parsed)
    if summary:
        parsed["summary"] = summary
    return parsed
```

### 3.3 改动点 2 — `/confirm` 端点 emit agent_final 前调用

```python
# web/app.py:778 附近
tool_result_obj = await tool.invoke(validated, ctx)
if hasattr(tool_result_obj, "model_dump"):
    tool_result_dict = tool_result_obj.model_dump()
elif isinstance(tool_result_obj, dict):
    tool_result_dict = tool_result_obj
else:
    tool_result_dict = {"value": str(tool_result_obj)}

# NEW: 用同一个纯函数填 summary(双保险)
from tradingagents.agent_harness.core.result_formatter import summarize_tool_result
if "summary" not in tool_result_dict:
    summary = summarize_tool_result(tool_result_dict)
    if summary:
        tool_result_dict["summary"] = summary

yield (
    f"event: agent_final\n"
    f"data: {_json.dumps({'tier': 1, 'result': tool_result_dict}, ensure_ascii=False, default=str)}\n\n"
)
```

### 3.4 改动点 3 — `_trivial_crud_summary` 复用 `summarize_tool_results`

```python
# tradingagents/agent_harness/core/orchestrator.py:2054
# OLD: summary = _format_trivial_summary(tool_results)
# NEW:
from .result_formatter import summarize_tool_results
summary = summarize_tool_results(tool_results)
if not summary:
    return None
return {"intent": "crud", "symbols": [], "results": tool_results, "summary": summary}
```

**保留** `_format_trivial_summary` 作为 thin wrapper(向后兼容已有 8 个 test cases),内部转调 `summarize_tool_results`。

### 3.5 改动点 4 — 前端 `appendToolResult` 扩展 status→badge

```javascript
// web/static/harness.js:195
const STATUS_BADGE = {
  pending_approval: (n, p) => {
    const args = p?.result?.args || p?.args || {};
    const sym = args.symbol ? ` · ${args.symbol}` : "";
    return `🔒 ${n} 等待审批${sym}`;
  },
  created: (n, p) => {
    const r = p?.result || p;
    const sym = r?.symbol || _extractSymFromRaw(r?.raw);
    return `✅ ${n} 已完成${sym ? ` (${sym})` : ""}`;
  },
  updated: (n, p) => `✏️ ${n} 已更新`,
  deleted: (n, p) => `🗑️ ${n} 已删除`,
  duplicate: (n, p) => {
    const r = p?.result || p;
    return `♻️ ${r?.symbol || "?"} 已存在,未重复添加`;
  },
  ok: (n, p) => `✓ ${n} 完成`,
  error: (n, p) => `❌ ${n}: ${p?.result?.raw || p?.error || "unknown"}`,
};

function appendToolResult(name, payload) {
  if (payload?.error) {
    return appendMessage("tool-result", `❌ ${name || "tool"}: ${payload.error}`);
  }
  const result = payload?.result || payload;
  const status = result?.status;
  const badge = STATUS_BADGE[status];
  if (badge) return appendMessage("tool-result", badge(name || "tool", payload));
  return appendMessage("tool-result", `📥 ${name || "tool"}: ${JSON.stringify(result).slice(0, 240)}`);
}

function _extractSymFromRaw(raw) {
  if (typeof raw !== "string") return null;
  const m = raw.match(/"symbol"\s*:\s*"([^"]+)"/);
  return m ? m[1] : null;
}
```

### 3.6 `agent_final` handler 不动

```javascript
// web/static/harness.js:594 (保持现状)
case "agent_final": {
  const result = payload.result || {};
  let summary = result.summary;
  if (!summary) summary = formatRawResult(result, payload.tier);
  else if (looksLikeRawJsonDump(summary, result)) {
    summary = formatRawResult(result, payload.tier) || summary;
  }
  assistant.bubble.innerHTML = renderMarkdown(summary);
  ...
}
```

后端 3 处调用都会塞 `summary`,前端**直接走 happy path**,不再触发 fallback 到 `formatRawResult`。

---

## 4. 数据流对照 — Before vs After

### Before (用户看到的):

```
user: 给 600036.SS 加笔记: 哈哈你是谁
   ↓ stream_chat → Tier 2 → create_note (pending_approval)
   ↓ /confirm → 直接 invoke create_note
   ↓ emit agent_final {result: {status: "created", raw: "NOTE_CREATED: ..."}}

assistant bubble:
{"status":"created","raw":"NOTE_CREATED: {\"id\":\"note-a911e2c4...\", \"symbol\":\"600036.SS\"}"}

trace:
🔧 create_note(symbol="600036.SS", body_md="哈哈你是谁", ...)
📥 create_note: {"status":"created","raw":"NOTE_CREATED: {…}"}
💡 SynthesizeNode 完成
```

### After:

```
user: 给 600036.SS 加笔记: 哈哈你是谁
   ↓ ... (same) ...
   ↓ _invoke_bridge 解析后填 summary: "笔记已保存 (600036.SS)"
   ↓ /confirm 也补 summary(双保险)
   ↓ emit agent_final {result: {status, raw, summary: "笔记已保存 (600036.SS)"}}

assistant bubble:
笔记已保存 (600036.SS)

trace:
🔧 create_note(symbol="600036.SS", body_md="哈哈你是谁", ...)
✅ create_note 已完成 (600036.SS)
💡 SynthesizeNode 完成
```

---

## 5. 验收矩阵

| 场景 (用户输入)                              | 工具返回                              | bubble 期望                              | trace 期望                                 |
| -------------------------------------------- | ------------------------------------- | ---------------------------------------- | ------------------------------------------ |
| 给 600036 加笔记:xx                          | `{status: created, raw: NOTE_CREATED}` | `笔记已保存 (600036.SS)`                 | `✅ create_note 已完成 (600036.SS)`        |
| 把 600036 笔记都删了                         | `{status: deleted, raw: NOTE_DELETED}` | `笔记已删除 (600036.SS)`                 | `🗑️ delete_notes_for_symbol 已删除`       |
| 给 513880 加告警 price>2.3                   | `{status: created, raw: ALERT_CREATED}`| `告警已创建 (513880.SS)`                 | `✅ create_alert 已完成 (513880.SS)`       |
| 把 600036 加关注(已存在)                     | `{status: duplicate, ...}`            | `600036.SS 已在关注列表中,无需重复添加`  | `♻️ 600036.SS 已存在,未重复添加`          |
| 给 600036 加关注                             | `{status: ok, raw: added ...}`         | `600036.SS 已加入关注`                   | `✓ add_to_watchlist 完成`                  |
| 修改偏好                                     | `{status: ok, raw: PREFERENCE_UPDATED}`| `偏好已更新`                             | `✓ update_preferences 完成`                |
| 给 600036 加笔记(还在 pending)              | `{status: pending_approval, ...}`     | (modal 弹窗,bubble 不变)                 | `🔒 create_note 等待审批`                  |
| 600036 现在多少钱                            | `{status: ok, price: 41.78, ...}`     | `📈 600036.SS 价格: ¥41.78 ...`          | (同 bubble, formatRawResult)               |
| 看 600036 笔记                               | `{status: ok, text: "共 N 条笔记: ...", count}` | 渲染 markdown table                | (同 bubble)                                |
| 600036 深度分析                              | 多 tool result + LLM synthesis        | (LLM 输出)                               | 完整推理链                                  |

---

## 6. 测试计划

### 6.1 新增单测 `tests/test_result_formatter.py`

```python
@pytest.mark.parametrize("result,expected", [
    ({"status": "created", "raw": 'NOTE_CREATED: {"id":"n1","symbol":"600036.SS"}'}, "笔记已保存 (600036.SS)"),
    ({"status": "created", "raw": "NOTE_CREATED: ...", "symbol": "600036.SS"}, "笔记已保存 (600036.SS)"),
    ({"status": "updated", "raw": 'NOTE_UPDATED: {"id":"n1","symbol":"513880.SS"}'}, "笔记已更新 (513880.SS)"),
    ({"status": "deleted", "raw": 'NOTE_DELETED: {"id":"n1","symbol":"600031.SS"}'}, "笔记已删除 (600031.SS)"),
    ({"status": "created", "raw": 'ALERT_CREATED: {"id":"a1","symbol":"513880.SS"}'}, "告警已创建 (513880.SS)"),
    ({"status": "deleted", "raw": 'ALERT_DELETED: {"id":"a1","symbol":"600999.SS"}'}, "告警已删除 (600999.SS)"),
    ({"status": "duplicate", "symbol": "600036.SS"}, "600036.SS 已在关注列表中,无需重复添加"),
    ({"status": "ok", "raw": "PREFERENCE_UPDATED: foo"}, "偏好已更新"),
    ({"status": "ok", "raw": "added 600036.SS to watchlist", "symbol": "600036.SS"}, "600036.SS 已加入关注"),
    ({"status": "pending_approval", "raw": "AWAITING_CONFIRMATION: ..."}, None),
    ({"status": "rate_limited"}, None),
    ({"status": "ok", "price": 41.78, "symbol": "600036.SS"}, None),
])
def test_summarize_tool_result(result, expected):
    assert summarize_tool_result(result) == expected


def test_summarize_tool_results_aggregates():
    results = [
        {"status": "created", "raw": 'NOTE_CREATED: {"symbol":"A"}'},
        {"status": "deleted", "raw": 'NOTE_DELETED: {"symbol":"B"}'},
    ]
    out = summarize_tool_results(results)
    assert "笔记已保存 (A)" in out
    assert "笔记已删除 (B)" in out
```

### 6.2 保留兼容

`tests/test_synthesize_fastpath.py` 的 8 个 existing parametrize cases 必须**全部继续 PASS**:
- `_format_trivial_summary` 改为 thin wrapper 调 `summarize_tool_results`,语义不变
- 现存 fixture 都是 `{status, raw, symbol}` 三件套,顶层 symbol 命中,与新逻辑兼容

### 6.3 新增 e2e `tests/test_confirm_user_facing_summary.py`

走 `/api/harness/sessions/{sid}/confirm` 端点 (mock audit + tool registry),断言:
1. SSE `tool_result` event payload 的 `result.summary` 非空
2. SSE `agent_final` event payload 的 `result.summary` 非空
3. 不依赖 `_invoke_bridge` 是否填了 summary(/confirm 自己也会补)

### 6.4 手工验收脚本

按 §5 验收矩阵,逐条在 `http://127.0.0.1:8000/harness` 实测:

```bash
# 启动服务
launchctl unload ~/Library/LaunchAgents/com.tradingagents.web-venv.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.tradingagents.web-venv.plist

# Playwright 跑端到端
python -m pytest tests/test_result_formatter.py -v
python -m pytest tests/test_synthesize_fastpath.py -v
python -m pytest tests/test_confirm_user_facing_summary.py -v
```

---

## 7. 风险与 Rollback

### 7.1 风险

| 风险                                                       | 缓解                                            |
| ---------------------------------------------------------- | ----------------------------------------------- |
| `_summarize_tool_result` 漏 case,某些 tool 仍渲染 JSON     | 验收矩阵 10 条全 PASS 才合并;前端 STATUS_BADGE 兜底 |
| `_invoke_bridge` 改后破坏现有 30+ 个 tool 测试             | 保留 `_parse_bridge_text` 旧路径分支,跑现有 test 验证 |
| `/confirm` 端点 `summary` 字段写错位置(例如写到外层)      | e2e 测试明确断言 `result.summary` 路径            |
| 前端 `STATUS_BADGE` 与后端短句不一致(用户看到 trace/bubble 不一致) | 在 `tests/test_result_formatter.py` 加 docstring 写明 "前端 STATUS_BADGE 硬编码,语义须对齐",code review 时人工对照 |

### 7.2 Rollback 计划

- 后端纯函数 `summarize_tool_result` 是 additive,不删旧逻辑 → 单一 commit revert 即可回退
- 前端 `STATUS_BADGE` 是新 switch,旧 `📥 ... : JSON.stringify` 路径保留为 fallback → 任意 commit revert
- `/confirm` 端点改动只加 `summary` 字段,不删旧 emit → 单一 commit revert

---

## 8. 时间线 / Commit Plan

| #   | Commit                                                          | 估计时间 |
| --- | --------------------------------------------------------------- | -------- |
| 1   | `feat(harness): result_formatter pure helper + unit tests`      | 0.5h     |
| 2   | `feat(harness): _invoke_bridge 填充 summary 字段`               | 0.5h     |
| 3   | `feat(harness): /confirm 端点 emit agent_final 前补 summary`    | 0.5h     |
| 4   | `refactor(harness): _trivial_crud_summary 转调 result_formatter`| 0.5h     |
| 5   | `feat(web): appendToolResult 扩展 STATUS_BADGE`                 | 0.5h     |
| 6   | `test(harness): e2e /confirm user-facing summary`               | 0.5h     |
| 7   | `docs: 本 spec 落档 + 验收清单`                                 | 0.5h     |
|     | **合计**                                                        | **3.5h** |

---

## 9. 后续扩展 (Day 15+, 不在本 spec 范围)

1. **Option B envelope 迁移** — 当工具数量继续增长,把 `summarize_tool_result` 升级为 Pydantic `ToolResult` model,前端强类型
2. **多语言** — `summarize_tool_result` 加 `lang` 参数,前端 `__harness_lang` 切换中英文
3. **per-tool override** — 允许 tool 在 `extra` 字段里自定义 summary 模板(覆盖默认 mapping)
4. **审计集成** — `/confirm` 端点 audit log 增加 `user_facing_summary` 字段,审计页面直接显示给 reviewer 看

---

## 10. 审查清单 (reviewer 必读)

- [ ] `_summarize_tool_result` 是否覆盖所有 write CRUD 路径? (8 个 status × 6 个 entity prefix = 至少 14 个组合)
- [ ] `_invoke_bridge._parse_bridge_text` 改动是否会破坏现有 30+ 个 tool 测试? (跑全套 test 验证)
- [ ] `/confirm` 端点的 summary fallback 是否完备? (即使 `_invoke_bridge` 漏填,/confirm 也要兜底)
- [ ] 前端 `STATUS_BADGE` 与后端 `summarize_tool_result` 的中文短句是否一致? (人工对照 §5 验收矩阵)
- [ ] `agent_final` handler 的 `looksLikeRawJsonDump` fallback 是否会被 `summary` 触发? (理论不会,但需要 case 验证)
- [ ] 性能:`summarize_tool_result` 在 hot path 调一次,有没有引入明显 latency? (O(1) 字符串查找 + 1 次 json.loads worst case,可接受)

---

## 11. 全量 Tool JSON 风险盘点 (回答用户的疑问)

**用户问**:「问其他会不会也出现这种 JSON 原始回复?」

**答**: 会,而且问题面比 `create_note` 大得多。我把 38 个 tool 全部盘了一遍。

### 11.1 风险分级表

| 等级 | 触发条件 | 受影响的 Tool | 当前现象 |
|---|---|---|---|
| 🔴 高 | 返回 `{status, raw: "PREFIX: {...}"}` + 走 /confirm 端点 | 11 个 | 100% 吐 JSON |
| 🟠 中 | 返回 `{status: "ok", raw: "<embedded JSON with summary>"}` | 3 个 bulk delete | 丢失 summary 字段,fallback 到 "操作成功" |
| 🟡 低 | Pydantic typed result 含 `status+symbol+raw` 但 raw 前缀未注册 | 2 个 watchlist | 走 fallback,但 status="created"/"deleted" 仍命中 |
| ✅ 安全 | 有 text/count 或 Pydantic 结构化字段 | 22 个 | 已被 formatRawResult 覆盖 |

### 11.2 🔴 高风险 — `{status, raw: "PREFIX: {...}"}` 经 /confirm 出 JSON

`_invoke_bridge._parse_bridge_text` 通过 `startswith` 检查 `_BRIDGE_PREFIXES`,命中的会映射成 `{status, raw}`。但这些工具**走的是 `tool.invoke(args, context)`** 直接路径(`/confirm 端点 web/app.py:778`),**根本不调 `_invoke_bridge`**——`_parse_bridge_text` 没机会跑。结果就是 Pydantic typed result `{status, raw}` 直接发到前端,前端 `formatRawResult` 不认 → `JSON.stringify` 兜底。

| Tool | 返回 Pydantic | result.status | result.raw 前缀 | bubble 当前显示 |
|---|---|---|---|---|
| `create_note` | (无, dict) | `created` | `NOTE_CREATED:` | `{"status":"created",...}` ❌ 用户报的这个 |
| `update_note` | (无, dict) | `updated` | `NOTE_UPDATED:` | 同上 ❌ |
| `delete_note` | (无, dict) | `deleted` | `NOTE_DELETED:` | 同上 ❌ |
| `create_alert` | (无, dict) | `created` | `ALERT_CREATED:` | 同上 ❌ |
| `update_alert` | (走 _invoke_bridge) | `updated` | `ALERT_UPDATED:` | 同上 ❌ |
| `delete_alert` | (走 _invoke_bridge) | `deleted` | `ALERT_DELETED:` | 同上 ❌ |
| `delete_notes_for_symbol` | (走 _invoke_bridge) | `ok` | embedded JSON | ❌ |
| `delete_alerts_for_symbol` | (走 _invoke_bridge) | `ok` | embedded JSON | ❌ |
| `delete_scheduled_tasks_for_symbol` | (走 _invoke_bridge) | `ok` | embedded JSON | ❌ |
| `create_scheduled_task` | (无, dict) | `created` | `SCHEDULED_CREATED:` | ❌ |
| `update_scheduled_task` | (走 _invoke_bridge) | `ok` | `SCHEDULED_UPDATED:` | ❌ |
| `delete_scheduled_task` | (走 _invoke_bridge) | `ok` | `SCHEDULED_DELETED:` | ❌ |

### 11.3 🟠 中风险 — Bulk delete 的 summary 被吞

`delete_notes_for_symbol` / `delete_alerts_for_symbol` 返回的是 JSON string:

```python
# tradingagents/agent_harness/tools/impl.py:679
return _json.dumps({
    "status": "ok",
    "summary": "资产 600036.SS 的笔记已删除:15/15 条成功",   # ← 已经有人话了!
    "symbol": "600036.SS",
    ...
}, ensure_ascii=False)
```

但 `_invoke_bridge` 拿到这个字符串后:
- `_parse_bridge_text` 检查 `_BRIDGE_PREFIXES`(NOTE_*/ALERT_*/etc.) — **没有匹配**(前缀是 `{`)
- 退回 `{status: "ok", raw: <整个 JSON string>}`

**那个 `summary` 字段就被封死在 raw 字符串里**——用户实际看到的:
- `_format_trivial_summary`: 走 `status="ok" and "PREFERENCE_UPDATED" in raw` (否) → 走 `"added" in raw.lower()` (否) → 退回 `"操作成功"`
- 前端 `formatRawResult`: 不认 `{status, raw}` shape → `JSON.stringify` 兜底

**用户期望**:"资产 600036.SS 的笔记已删除:15/15 条成功" —— **已经有了,只是没人解析它**!

### 11.4 🟡 中风险 — `add_to_watchlist` / `remove_from_watchlist` raw 前缀未注册

```python
# tradingagents/agent_harness/tools/builtin.py:961
return AddToWatchlistResult(
    status="created", symbol=args.symbol,
    raw="ADDED: " + _json.dumps({...}),   # ← 前缀是 ADDED: 不是 NOTE_/ALERT_
)
return AddToWatchlistResult(
    status="duplicate", symbol=args.symbol,
    raw=f"DUPLICATE: {args.symbol}",
)
return RemoveFromWatchlistResult(
    status="deleted", symbol=args.symbol,
    raw=f"REMOVED: {args.symbol}",
)
return RemoveFromWatchlistResult(
    status="not_found", symbol=args.symbol,
    raw=f"NOT_FOUND: {args.symbol} not in watchlist",
)
```

走 `/confirm` 端点后:
- `result = {status: "created", symbol: "600036.SS", asset_type: "stock", raw: "ADDED: {...}"}`
- 我原 spec 的 `summarize_tool_result` 只认 `NOTE_CREATED in raw` — 不认 `ADDED:` — **会返回 None**
- 前端 fallback → JSON

### 11.5 SCHEDULED_* 前缀在 `_BRIDGE_PREFIXES` 里完全没注册

```python
# tradingagents/agent_harness/tools/builtin.py:658
_BRIDGE_PREFIXES = (
    "AWAITING_CONFIRMATION:",
    "NOTE_CREATED:", "NOTE_UPDATED:", "NOTE_DELETED:",
    "ALERT_CREATED:", "ALERT_UPDATED:", "ALERT_DELETED:",
    "ERROR:", "NO_DATA:",
    "(no scheduled tasks)", "(用户关注列表为空)",
    # ⚠️ 缺 SCHEDULED_CREATED: / SCHEDULED_UPDATED: / SCHEDULED_DELETED:
)
```

所以即使 `_invoke_bridge` 调它,`SCHEDULED_*` 也会 fallback 到 `{status: "ok", raw: "..."}`。

---

## 12. 修复方案扩展(基于 §11 盘点)

我原方案 C 只覆盖了 §11.2 的 6 个 `NOTE_*/ALERT_*` 创建/更新/删除。**实际要扩展到**:

### 12.1 新增 entity prefix 注册

```python
# tradingagents/agent_harness/tools/builtin.py:658
_BRIDGE_PREFIXES = (
    "AWAITING_CONFIRMATION:",
    # notes
    "NOTE_CREATED:", "NOTE_UPDATED:", "NOTE_DELETED:",
    # alerts
    "ALERT_CREATED:", "ALERT_UPDATED:", "ALERT_DELETED:",
    # scheduled jobs (新增)
    "SCHEDULED_CREATED:", "SCHEDULED_UPDATED:", "SCHEDULED_DELETED:",
    # watchlist (新增)
    "ADDED:", "REMOVED:", "DUPLICATE:",
    # bulk delete + errors
    "ERROR:", "NO_DATA:",
    "(no scheduled tasks)", "(用户关注列表为空)",
)
```

**注**:`REMOVED` / `DUPLICATE` / `ADDED` 都是 watchlist 的 raw 前缀。注册后会跟 NOTE_/ALERT_ 同级,但 `_summarize_tool_result` 需要 watchlist 单独的 entity 映射。

### 12.2 `summarize_tool_result` entity 表扩展

```python
# tradingagents/agent_harness/core/result_formatter.py
_ENTITY_PREFIXES = {
    "NOTE_CREATED:": "note",      "NOTE_UPDATED:": "note",      "NOTE_DELETED:": "note",
    "ALERT_CREATED:": "alert",    "ALERT_UPDATED:": "alert",    "ALERT_DELETED:": "alert",
    "SCHEDULED_CREATED:": "scheduled", "SCHEDULED_UPDATED:": "scheduled", "SCHEDULED_DELETED:": "scheduled",
    "ADDED:": "watchlist",        "REMOVED:": "watchlist",      "DUPLICATE:": "watchlist",
}

_TEMPLATES = {
    ("created", "note"):      "笔记已保存",
    ("updated", "note"):      "笔记已更新",
    ("deleted", "note"):      "笔记已删除",
    ("created", "alert"):     "告警已创建",
    ("updated", "alert"):     "告警已更新",
    ("deleted", "alert"):     "告警已删除",
    ("created", "scheduled"): "定时任务已创建",
    ("updated", "scheduled"): "定时任务已更新",
    ("deleted", "scheduled"): "定时任务已删除",
    ("created", "watchlist"): "已加入关注",
    ("deleted", "watchlist"): "已移出关注",
    ("duplicate", "watchlist"): "已在关注列表中,无需重复添加",
}
```

### 12.3 新增 bulk delete JSON 解析路径

```python
# tradingagents/agent_harness/core/result_formatter.py
def _try_parse_embedded_summary(raw: str) -> str | None:
    """bulk delete 工具 (delete_notes_for_symbol 等) 把 summary 封在 raw JSON 里。

    抠出来直接用,避免「工具已经说了人话但用户看不到」。
    """
    import json as _json
    if not isinstance(raw, str) or not raw.startswith("{"):
        return None
    try:
        payload = _json.loads(raw)
    except Exception:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
        return payload["summary"]
    return None


def summarize_tool_result(result: dict[str, Any]) -> str | None:
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    raw = result.get("raw") or ""
    symbol = result.get("symbol")

    # 优先级 1: bulk delete 等工具自带 summary
    if status == "ok":
        embedded = _try_parse_embedded_summary(raw)
        if embedded:
            return embedded

    # 优先级 2: NOTE_*/ALERT_*/SCHEDULED_*/ADDED/REMOVED/DUPLICATE prefix
    if status in ("created", "updated", "deleted", "duplicate"):
        for prefix, ent in _ENTITY_PREFIXES.items():
            if prefix in raw:
                if not symbol:
                    symbol = _extract_symbol_from_raw(raw)
                tmpl = _TEMPLATES.get((status, ent))
                if tmpl:
                    return f"{tmpl} ({symbol})" if symbol else tmpl
                break

    # 优先级 3: 通用 ok 路径 (PREFERENCE_UPDATED, added, ...)
    if status == "ok" and "PREFERENCE_UPDATED" in raw:
        return "偏好已更新"
    if status == "pending_approval":
        return None
    return None
```

### 12.4 `not_found` 也加进 STATUS_BADGE

```javascript
// web/static/harness.js:195
const STATUS_BADGE = {
  // ... 现有
  not_found: (n, p) => {
    const r = p?.result || p;
    return `⚠️ ${r?.symbol || "?"} 不在关注列表中`;
  },
};
```

### 12.5 扩展后的验收矩阵(20 条)

| # | 用户输入 | tool | bubble 期望 | trace 期望 |
|---|---|---|---|---|
| 1 | 给 600036 加笔记:xx | create_note | `笔记已保存 (600036.SS)` | `✅ create_note` |
| 2 | 改一下笔记 xxx 内容为 yy | update_note | `笔记已更新 (xxx)` | `✏️ update_note` |
| 3 | 删除笔记 xxx | delete_note | `笔记已删除 (xxx)` | `🗑️ delete_note` |
| 4 | 把 600036 笔记都删了(15条) | delete_notes_for_symbol | `资产 600036.SS 的笔记已删除:15/15 条成功` | `🗑️ delete_notes_for_symbol` |
| 5 | 把 600036 笔记都删了(0条) | delete_notes_for_symbol | `资产 600036.SS 当前没有活跃笔记可删除(...)` | `⚠️ noop` |
| 6 | 给 513880 加告警 price>2.3 | create_alert | `告警已创建 (513880.SS)` | `✅ create_alert` |
| 7 | 改告警 xxx 阈值 2.5 | update_alert | `告警已更新 (xxx)` | `✏️ update_alert` |
| 8 | 删除告警 xxx | delete_alert | `告警已删除 (xxx)` | `🗑️ delete_alert` |
| 9 | 把 513880 告警都删了(2条) | delete_alerts_for_symbol | `资产 513880.SS 的告警已删除:2/2 条成功` | `🗑️ delete_alerts_for_symbol` |
| 10 | 给 600036 加定时任务 cron 0 9 * * * | create_scheduled_task | `定时任务已创建 (600036.SS)` | `✅ create_scheduled_task` |
| 11 | 改定时任务 xxx 启用 = false | update_scheduled_task | `定时任务已更新 (xxx)` | `✏️ update_scheduled_task` |
| 12 | 删除定时任务 xxx | delete_scheduled_task | `定时任务已删除 (xxx)` | `🗑️ delete_scheduled_task` |
| 13 | 把 600036 定时任务都删了(3条) | delete_scheduled_tasks_for_symbol | `定时任务已删除:3/3 条成功` | `🗑️ delete_scheduled_tasks_for_symbol` |
| 14 | 把 600036 加进关注 | add_to_watchlist | `已加入关注 (600036.SS)` | `✅ add_to_watchlist` |
| 15 | 600036 已经关注过 | add_to_watchlist | `600036.SS 已在关注列表中,无需重复添加` | `♻️ duplicate` |
| 16 | 把 600036 移出关注 | remove_from_watchlist | `已移出关注 (600036.SS)` | `🗑️ remove_from_watchlist` |
| 17 | 600036 没在关注列表里 | remove_from_watchlist | `⚠️ 600036.SS 不在关注列表中` | `⚠️ not_found` |
| 18 | 修改风险偏好为保守 | update_preference | `偏好已更新` | `✓ update_preference` |
| 19 | 600036 现在多少钱 | get_quote | `📈 600036.SS 价格: ¥41.78 ...` | (formatRawResult) |
| 20 | 看 600036 笔记 | list_notes | 渲染 markdown table | (formatRawResult) |

**20/20 全部 PASS 才合并。**

---

## 13. 时间线更新

| #   | Commit                                                          | 估计时间 |
| --- | --------------------------------------------------------------- | -------- |
| 1   | `feat(harness): result_formatter 支持 5 entity × 4 op + embedded summary 解析` | 1h       |
| 2   | `feat(harness): _BRIDGE_PREFIXES 注册 SCHEDULED_*/ADDED/REMOVED/DUPLICATE` | 0.25h    |
| 3   | `feat(harness): _invoke_bridge 填充 summary 字段`               | 0.25h    |
| 4   | `feat(harness): /confirm 端点 emit agent_final 前补 summary`    | 0.5h     |
| 5   | `refactor(harness): _trivial_crud_summary 转调 result_formatter`| 0.5h     |
| 6   | `feat(web): appendToolResult 扩展 STATUS_BADGE (8 个 status)`   | 0.5h     |
| 7   | `test(harness): test_result_formatter 20+ parametrize case`     | 1h       |
| 8   | `test(harness): e2e /confirm user-facing summary (5 个 tool)`   | 0.5h     |
| 9   | `docs: 本 spec 落档 + 验收清单 (20 条)`                          | 0.5h     |
|     | **合计**                                                        | **5h**   |


---

## 14. 其他 SSE 事件 JSON 风险盘点 (回答用户的疑问 #2)

**用户问**:「除了调用 tools 还有其他的呢?」

**答**: 有,而且问题面更广。我把所有 SSE 事件流都盘了一遍。

### 14.1 全部 SSE 事件 × 前端处理 矩阵

| 事件 | 后端 emit 处 | 前端 case | 前端默认行为 | 风险 |
|---|---|---|---|---|
| `plan_started` | orchestrator:1144 | ✅ | — | ✅ 安全 |
| `plan_ready` | orchestrator:1158 | ✅ | — | 🟡 JSON.stringify(args) |
| `plan_ready_ptc` | orchestrator:1158 | ❌ | `default → JSON.stringify(payload).slice(0,120)` | 🟡 trace 有噪声 |
| `tool_call` | orchestrator, _confirm | ✅ | — | 🟡 JSON.stringify(args) |
| `tool_result` | orchestrator:1171, _confirm | ✅ | — | 🔴 详见 §11 |
| `observed` | orchestrator:1188 | ✅ | — | ✅ 安全 |
| `verified` | orchestrator:1196 | ✅ | — | ✅ 安全 |
| `agent_final` | orchestrator:1205, _confirm, short_circuit | ✅ | — | 🔴 详见 §11/§12 |
| `answer_verified` | orchestrator:2003 | ✅ | — | ✅ 安全 |
| `confirm_request` | orchestrator:1180 | ✅ (modal) | — | ✅ 安全 |
| `audit_decision` | web/app.py:705 | ✅ (silent) | — | ✅ 安全 |
| `done` | web/app.py:817 | ✅ (silent) | — | ✅ 安全 |
| `error` | orchestrator:929, web/app.py:567 | ✅ | — | 🟠 payload.error 缺失时吐整个 payload JSON |
| `warning` | orchestrator:818, short_circuit:53,58 | ❌ | default → JSON.stringify | 🟡 trace 噪声 |
| `usage_summary` | orchestrator:884 | ❌ | default → JSON.stringify | 🟡 trace 噪声 |
| `turn/started` | orchestrator:1079 | ❌ | default → JSON.stringify | 🟡 trace 噪声 |
| `resume_complete` | orchestrator:1041 | ❌ | default → JSON.stringify | 🟡 trace 噪声 |

**事件总数**:17
**前端未 case**:5 (`plan_ready_ptc` / `warning` / `usage_summary` / `turn/started` / `resume_complete`)
**有 JSON 风险**:`agent_final` / `tool_result` (🔴) + `error` (🟠) + 5 个 default case (🟡)

### 14.2 6 个具体问题

#### 问题 A — Tier 2 synthesize 失败 → JSON (🔥 高频)

```python
# tradingagents/agent_harness/core/orchestrator.py:2025-2027
return {
    "intent": state.intent.value,
    "symbols": state.symbols,
    "results": state.tool_results,  # ← 没 summary!
}
```

**触发场景**: LLM API 超时、配额爆、网络断、tool 太多撑爆 context。Tier 2 路径触发 1 次就有 1 个 JSON。
**当前结果**: bubble 显示 `{"intent":"analysis","symbols":["600036.SS"],"results":[{...},{...}]}`
**用户感受**: 看到 5 个工具的原始 JSON

#### 问题 B — `default` case 把未识别事件 dump 到 trace (🟡 噪声)

```javascript
// web/static/harness.js:644
default:
  appendReasoningDelta(`  · ${name}: ${JSON.stringify(payload).slice(0, 120)}\n`);
```

5 个事件会触发:
- `turn/started` (每 turn 第一行) → trace 噪声
- `usage_summary` (每 turn 结尾) → trace 噪声
- `warning` (LLM 降级、short-circuit 失败) → trace 噪声
- `plan_ready_ptc` (PTC 模式) → trace 噪声
- `resume_complete` (崩溃恢复) → trace 噪声

#### 问题 C — `error` 事件 fallback 也吐 JSON (🟠 边界)

```javascript
case "error":
  appendError(payload.error || JSON.stringify(payload));
```

如果 `payload.error` 缺失(或 null/空),bubble 显示 `{"tier":2,"error":null,"usage":{...}}`。**应该有个 default error message**。

#### 问题 D — `plan_ready` JSON.stringify(args) (🟡 噪声)

trace 显示:
```
📋 计划 (3 步):
1. [data_agent] {"symbol":"600036.SS","task":"fetch_quote_and_fundamentals"}
2. [news_agent] {"symbol":"600036.SS","lookback_days":30}
3. [verifier] {"level":"L1_L2","checks":["schema","missing_fields","consistency"]}
```

**调试 OK, 给用户太 verbose**。L1 用户不应该看到完整 args。

#### 问题 E — `tool_call` JSON.stringify(args) (🟡 噪声)

trace 显示:
```
🔧 get_quote(symbol="600036.SS")
→ 调用 get_quote({"symbol":"600036.SS"})
```

**两条重复信息**(appendToolCall 已经渲染 args,appendReasoningDelta 又渲染一遍)。

#### 问题 F — L3 ungrounded 没有真正 replan (🟠 隐性 bug)

```python
# orchestrator.py:1987-1993
if not l3.ok:
    state.plan.append({...})  # ← 只 append,不 execute
yield ("answer_verified", {...})  # ← 只发事件
```

**state.plan 被改了但本次 turn 不再 execute**——用户看到的 answer 没变,只多了一个 L3 警告。修复要等下一次 turn。

### 14.3 修复方案 (§14 → spec 扩展)

#### 14.3.1 问题 A — Synthesize 失败也走 result_formatter

```python
# tradingagents/agent_harness/core/orchestrator.py:2025
# OLD:
return {"intent": state.intent.value, "symbols": state.symbols, "results": state.tool_results}

# NEW:
from .result_formatter import summarize_tool_results
summary = summarize_tool_results(state.tool_results)
if not summary:
    summary = "数据已获取,但 LLM 暂时不可用,请重试或换一种问法。"
return {
    "intent": state.intent.value,
    "symbols": state.symbols,
    "results": state.tool_results,
    "summary": summary,   # ← 双保险
}
```

#### 14.3.2 问题 B — 前端 default case 加专用渲染

```javascript
// web/static/harness.js:644
default:
  // §P2-1: 5 个常见事件的专用渲染
  switch (name) {
    case "plan_ready_ptc":
      appendReasoningDelta(`📋 PTC 计划: ${payload.groups?.length || 0} 个并行组\n`);
      return;
    case "turn/started":
      appendReasoningDelta(`▶ Turn 开始 (turn_id=${payload.turn_id || "?"})\n`);
      return;
    case "warning":
      appendReasoningDelta(`⚠️ ${payload.message || payload.fallback || "fallback"}\n`);
      return;
    case "usage_summary":
      // 不进 trace,单独的成本面板(后续 UI 工作)
      return;
    case "resume_complete":
      appendReasoningDelta(`🔄 会话恢复: ${payload.replayed_events || 0} 事件已重放\n`);
      return;
  }
  // 真未知事件才 fallback JSON
  appendReasoningDelta(`  · ${name}: ${JSON.stringify(payload).slice(0, 120)}\n`);
```

#### 14.3.3 问题 C — `error` 事件友好 fallback

```javascript
case "error":
  const errMsg = payload.error || "会话执行失败,请重试";
  const errDetail = payload.failure?.reason ? ` (${payload.failure.reason})` : "";
  appendError(`${errMsg}${errDetail}`);
  appendReasoningDelta(`❌ 错误: ${errMsg}${errDetail}\n`);
  break;
```

#### 14.3.4 问题 D / E — `plan_ready` / `tool_call` 用友好 args 渲染

```javascript
case "plan_ready": {
  const steps = payload.steps || [];
  const planText = steps
    .map((s, i) => {
      const friendlyArgs = _friendlyArgs(s.args || {});
      return `${i + 1}. [${s.agent || "?"}] ${friendlyArgs}`;
    })
    .join("\n");
  appendReasoningDelta(`📋 计划 (${steps.length} 步):\n${planText}\n`);
  break;
}
case "tool_call":
  appendToolCall(payload.name || "", payload.args || {});
  appendReasoningDelta(`→ 调用 ${payload.name}(${_friendlyArgs(payload.args || {})})\n`);
  break;

function _friendlyArgs(args) {
  // 把 {"symbol":"600036.SS","lookback_days":30}
  // 变成 "600036.SS · 30天"
  // 启发式: 优先 symbol/name, 其他 key=value 用 · 分隔
  if (typeof args !== "object" || !args) return "";
  const sym = args.symbol || args.ticker || args.name;
  const parts = [];
  if (sym) parts.push(sym);
  for (const [k, v] of Object.entries(args)) {
    if (k === "symbol" || k === "ticker" || k === "name") continue;
    if (typeof v === "object") continue;   // 跳过嵌套对象
    parts.push(`${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  }
  return parts.join(" · ");
}
```

#### 14.3.5 问题 F — L3 ungrounded 真正 replan (Day 15+, 超出本 spec)

state.plan append 后需要重新触发 _execute + _synthesize。本 spec 不解决,留 §15 作为后续。

### 14.4 验收矩阵扩展 (再加 5 条)

| # | 场景 | 当前现象 | 期望 |
|---|---|---|---|
| 21 | Tier 2 + LLM synthesize 抛异常 | bubble `{"intent":..., "results":...}` | bubble `数据已获取,但 LLM 暂时不可用,请重试` |
| 22 | warning 事件 (LLM 降级) | trace `· warning: {"message":"..."}` | trace `⚠️ LLM 不可用,降级 Tier 1` |
| 23 | error 事件但 payload.error 为空 | bubble `{"tier":2,"error":null,"usage":{...}}` | bubble `会话执行失败,请重试` |
| 24 | plan_ready (3 步 plan) | trace JSON.stringify 全文 | trace `📋 计划 (3 步):\n1. [data_agent] 600036.SS\n2. [news_agent] 600036.SS · 30天\n3. [verifier] L1_L2` |
| 25 | tool_call args | trace JSON.stringify + duplicate display | trace `→ get_quote(600036.SS)` (去重,只一次) |

---

## 15. 总时间线 (含 §11 + §14 全部修复)

| #   | Commit                                                                  | 估计时间 |
| --- | ----------------------------------------------------------------------- | -------- |
| 1   | `feat(harness): result_formatter 支持 5 entity × 4 op + embedded summary` | 1h       |
| 2   | `feat(harness): _BRIDGE_PREFIXES 注册 SCHEDULED_*/ADDED/REMOVED/DUPLICATE` | 0.25h    |
| 3   | `feat(harness): _invoke_bridge 填充 summary 字段`                       | 0.25h    |
| 4   | `feat(harness): /confirm 端点 emit agent_final 前补 summary`            | 0.5h     |
| 5   | `refactor(harness): _trivial_crud_summary 转调 result_formatter`        | 0.5h     |
| 6   | `feat(harness): _synthesize 失败兜底 summary (问题 A)`                  | 0.25h    |
| 7   | `feat(web): dispatchEvent default case 加 5 个事件专用渲染 (问题 B)`     | 0.5h     |
| 8   | `feat(web): error 事件友好 fallback (问题 C)`                           | 0.25h    |
| 9   | `feat(web): plan_ready + tool_call 用 friendly args (问题 D/E)`         | 0.5h     |
| 10  | `test(harness): test_result_formatter 20+ parametrize case`             | 1h       |
| 11  | `test(harness): e2e /confirm user-facing summary (5 个 tool)`           | 0.5h     |
| 12  | `test(web): dispatchEvent 5 个新事件 + friendly args`                   | 0.5h     |
| 13  | `docs: 本 spec 落档 + 验收清单 (25 条)`                                  | 0.5h     |
|     | **合计**                                                                | **6.5h** |

---

## 16. 升级 vs v0 (差异点)

| 维度 | v0 (原 spec) | v1 (本版本含 §11-§15) |
|---|---|---|
| 覆盖工具 | 4 个 (NOTE CRUD × 4) | 12 个 (NOTE/ALERT/SCHEDULED/WATCHLIST × CREATE/UPDATE/DELETE) |
| Bulk delete summary | 丢失 | 解析嵌入 JSON 还原 |
| LLM synthesize 失败 | 吐 JSON | 友好降级提示 |
| 默认事件渲染 | JSON 噪声 | 5 个事件专用渲染 |
| error fallback | 吐整个 payload | 友好消息 |
| plan/tool args | JSON.stringify | 友好短格式 |
| 验收矩阵 | 10 条 | **25 条** |
| 工作量 | 3.5h | **6.5h** |

