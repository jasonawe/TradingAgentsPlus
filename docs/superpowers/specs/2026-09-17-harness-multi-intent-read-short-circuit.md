# 2026-09-17 — Harness 多意图只读查询走 Tier 1 Short-Circuit

**Date:** 2026-09-17
**Stage:** C Day 14+ 增量（接续 `2026-09-17-harness-tool-result-user-facing-renderer.md`）
**Status:** 🚧 **Draft** — 待 9 轮 review + commit
**Branch:** `codex/harness-phase1-hardening`（继续在已有分支上迭代）
**前置依赖:**
- `2026-09-12-agent-harness-modularization.md`（已落地）
- `2026-09-17-harness-tool-result-user-facing-renderer.md`（刚 commit）

---

## 0. TL;DR

**问题**:用户问「看一下 600036.SS 的笔记和告警」，harness 走 Tier 2 plan/execute/synthesize，LLM 输出"数据事实 / 行为面观察 / 方向性建议"格式——对一个简单的 list 查询太啰嗦。

**根因**:`tradingagents/agent_harness/core/tier.py:367-373` 检测到 multi-intent (`classify_multi` 返回 ≥2 个 (intent, op) 对) 时**强制 Tier 2**，因为 `ShortCircuit.run()` 只支持单 intent（`_tool_for_intent` mapping 是 1:1），multi-intent 走 Tier 1 会漏掉第二个工具。

**答案**:扩展 `ShortCircuit` 支持 **read-only multi-intent**——所有 intent 都在 read-only 子集内（LIST/READ op）时，直接顺序调多个 tool，**跳过 LLM synthesize**，用前端 `formatRawResult` 渲染 markdown table。CRUD write 或 read+write 混合 multi-intent 仍然走 Tier 2（保持原行为）。

**效果**:
- `看一下 600036.SS 的笔记和告警` → 直接渲染「📋 笔记」「🔔 告警」两张表，0 LLM 调用
- `给 600036 加笔记:xx` → 仍然走 Tier 2（单 write op，HITL gate）
- `把 600036 加关注 + 加笔记` → 仍然走 Tier 2（multi-write，需 HITL）

**工作量**:2h（含 5 个 unit test + 1 个 e2e）

---

## 1. Background — 为什么 multi-intent read 走 Tier 2

### 1.1 当前路径

```
user: "看一下 600036.SS 的笔记和告警"
   ↓ stream_chat
   ↓ classify() → (NOTE, LIST)   ← 主意图
   ↓ classify_multi() → [(NOTE,LIST), (ALERT,LIST)]   ← 2 个对
   ↓ multi_intent=True
   ↓ tier.py:368 强制 Tier 2 (PLAN_EXECUTE)
   ↓ plan_started → plan_ready(2 个 step)
   ↓ execute: list_notes + list_alerts 并发
   ↓ synthesize → LLM 输出"数据事实 / 行为面观察 / 方向性建议"
   ↓ agent_final: LLM 自己编的冗长 markdown
```

### 1.2 用户感受

用户看到的是：
```markdown
# 600036.SS（招商银行）笔记与告警查询

## 1. 数据事实
| 项目 | 查询结果 |
|---|---|
| 笔记 | (无 SS 的笔记), count=0 |
| 告警 | (无告警, filter=SS), count=0 |

## 2. 行为面观察
笔记空仓 = 你此前未对招商银行写下过跟踪要点...
告警空仓 = 当前没有任何自动化的"触发器"在工作...

## 3. 方向性建议
建议:立刻补建 ≥2 条笔记 + ≥1 条告警...
```

**问题**: 用户只是想要"看 list"，不需要"行为面观察"和"方向性建议"。LLM 在 list 操作上加了**推理**——但 list 结果本身（markdown table）已经够清晰。

### 1.3 现有 ShortCircuit 能力

```python
# tradingagents/agent_harness/core/short_circuit.py:24
class ShortCircuit:
    async def run(self, route, message, context):
        tool_name = self._tool_for_intent(route.intent)  # 1:1 mapping
        ...
        result = await tool.invoke(args, context)
        yield ("tool_call", {...})
        yield ("tool_result", {...})
        yield ("agent_final", {"tier": 1, "result": result_payload})
```

只支持单 intent。多 intent 时第二个工具会被 `_tool_for_intent` 返回空字符串，跳过。

---

## 2. 设计方案

### 2.1 检测 read-only multi-intent

在 `tier.py:367` 之前加判断：

```python
# tradingagents/agent_harness/core/tier.py — 修改 fast_route_with_op()
multi_pairs = classify_multi(message)
multi_intent = len({(p[0], p[1]) for p in multi_pairs}) >= 2

# §N1 — read-only multi-intent can short-circuit too.
# All pairs must be in the read-only subset (LIST or READ op) AND
# the entity intent must have a Tier 1 read tool registered.
if multi_intent:
    read_only_pairs = [p for p in multi_pairs if p[1] in (Op.LIST, Op.READ)]
    if len(read_only_pairs) == len(multi_pairs) and len(read_only_pairs) >= 2:
        # All pairs are read-only — short-circuit wins.
        return RouteResult(
            intent=intent, tier=Tier.DIRECT, symbols=symbols,
            confidence=0.85,
            reason=f"read-only multi-intent ({len(read_only_pairs)} pairs) → Tier 1",
            multi_pairs=multi_pairs,   # NEW field — ShortCircuit needs this
        ), op
    # Mixed (read + write) or write-only multi-intent → Tier 2 (existing).
    return RouteResult(...)
```

### 2.2 RouteResult 新增 `multi_pairs` 字段

```python
# tradingagents/agent_harness/core/tier.py — RouteResult dataclass
@dataclass
class RouteResult:
    intent: Intent
    tier: Tier
    symbols: list[str]
    confidence: float
    reason: str = ""
    op: Op | None = None
    multi_pairs: list[tuple[Intent, Op]] = field(default_factory=list)
    # ^ NEW — populated by fast_route_with_op() for read-only multi-intent
    #   ShortCircuit reads this to know which tools to invoke in sequence.
```

### 2.3 ShortCircuit.run() 支持 multi-intent

```python
# tradingagents/agent_harness/core/short_circuit.py — 修改 run()
async def run(self, route, message, context):
    pairs = route.multi_pairs if route.multi_pairs else [(route.intent, route.op)]

    if len(pairs) == 1:
        # Single-intent path (existing — unchanged).
        return await self._run_single(route, message, context)

    # NEW: multi-intent path. Only enters when fast_route_with_op
    # detected all-read-only multi-intent (otherwise tier=PLAN_EXECUTE
    # and ShortCircuit isn't called).
    return self._run_multi(route, pairs, message, context)


async def _run_multi(self, route, pairs, message, context):
    """Read-only multi-intent: invoke every tool sequentially, concatenate
    results into a single agent_final payload. No LLM call."""
    results: list[dict[str, Any]] = []
    for intent, op in pairs:
        tool_name = self._tool_for_intent(intent)
        if not tool_name:
            yield ("warning", {"message": f"no Tier 1 tool for intent={intent}"})
            continue
        symbol = route.symbols[0] if route.symbols else ""
        try:
            tool = self.registry.get(tool_name)
            args_schema = tool.schema.args_schema
            args = self._build_args(args_schema, symbol)
            yield ("tool_call", {"name": tool_name, "args": self._safe_dump(args)})
            result = await tool.invoke(args, context)
            result_payload = self._safe_dump(result)
            yield ("tool_result", {"name": tool_name, "result": result_payload})
            results.append({
                "intent": intent.value,
                "tool": tool_name,
                "result": result_payload,
            })
        except Exception as e:
            LOGGER.warning("Tier 1 multi-intent tool failed: %s %s", tool_name, e)
            yield ("error", {"tier": int(Tier.DIRECT), "tool": tool_name,
                             "error": str(e)})

    yield ("agent_final", {
        "tier": int(Tier.DIRECT),
        "result": {"multi": results, "count": len(results)},
        "rendered": True,
    })
```

### 2.4 前端识别 multi 形状 + 渲染多张表

```javascript
// web/static/harness.js — agent_final case (line 594)
case "agent_final": {
  const result = payload.result || {};
  // §N2 — multi-intent Tier 1 short-circuit returns {multi: [...]}
  if (Array.isArray(result.multi) && result.multi.length >= 1) {
    const sections = result.multi.map((s) => {
      // Use existing formatRawResult for each section's result.
      const text = formatRawResult(s.result, payload.tier);
      const heading = intentHeading(s.intent);
      return `<section class="harness-multi-section">
        <h4>${heading}</h4>
        ${text}
      </section>`;
    }).join("");
    assistant.bubble.innerHTML = sections;
    appendReasoningDelta(`💡 SynthesizeNode 完成 (multi-intent Tier 1)\n`);
    scrollToBottom();
    return;
  }
  // Existing single-result path (unchanged).
  let summary = result.summary;
  if (!summary) summary = formatRawResult(result, payload.tier);
  ...
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
  })[intent] || intent;
}
```

---

## 3. 数据流对比

### Before

```
user: "看一下 600036.SS 的笔记和告警"
   ↓ Tier 2 plan (intent=NOTE, op=LIST)
   ↓ plan_ready: [list_notes, list_alerts]
   ↓ execute: both tools return empty
   ↓ synthesize → LLM outputs:
   ↓   "# 600036.SS（招商银行）笔记与告警查询
   ↓    ## 1. 数据事实
   ↓    ## 2. 行为面观察
   ↓    ## 3. 方向性建议"
   ↓ bubble: LLM 写的冗长 markdown
   ↓ latency: ~2.5s (1× LLM call)
   ↓ token cost: ~600 tokens
```

### After

```
user: "看一下 600036.SS 的笔记和告警"
   ↓ fast_route_with_op → tier=DIRECT, multi_pairs=[(NOTE,LIST), (ALERT,LIST)]
   ↓ ShortCircuit._run_multi
   ↓ tool_call: list_notes
   ↓ tool_result: list_notes → {text: "共 0 条笔记:...", count: 0}
   ↓ tool_call: list_alerts
   ↓ tool_result: list_alerts → {text: "共 0 条告警:...", count: 0}
   ↓ agent_final: {multi: [...], count: 2}
   ↓ frontend: renders 2 sections with markdown tables
   ↓ bubble: "📋 笔记 (0 条)\n(empty table)\n\n🔔 告警 (0 条)\n(empty table)"
   ↓ latency: ~0.4s (0× LLM call)
   ↓ token cost: 0
```

---

## 4. 验收矩阵

| #   | 用户输入                            | 期望路径                        | 期望 bubble 显示                           |
| --- | --------------------------------- | ----------------------------- | ----------------------------------------- |
| 1   | 看一下 600036.SS 的笔记和告警           | Tier 1 multi (NOTE+ALERT)     | 2 个 section（笔记 + 告警），0 LLM 调用       |
| 2   | 我的关注列表 + 600036 的笔记             | Tier 1 multi (WATCHLIST+NOTE) | 2 个 section（关注 + 笔记）                  |
| 3   | 600036 现在多少钱                    | Tier 1 single (QUOTE)         | 现有的 `📈 600036.SS 价格: ¥41.78`          |
| 4   | 看 600036 笔记                       | Tier 1 single (NOTE)          | 现有的 markdown table                       |
| 5   | 给 600036 加笔记:xx                  | Tier 2 single write           | HITL modal → "笔记已保存 (600036.SS)"      |
| 6   | 把 600036 加进关注 + 加个笔记           | Tier 2 multi write            | HITL modal → "已加入关注 + 笔记已保存"        |
| 7   | 我的关注列表                          | Tier 1 single (WATCHLIST)     | 现有的 markdown table                       |
| 8   | 600036 深度分析                     | Tier 2 (analysis)             | 现有的 LLM-synthesized 深度分析              |
| 9   | 600036 的 PE 是多少 + 历史价格          | Tier 1 multi (FUNDAMENTALS+HISTORY) | 2 个 section（基本面 + K 线）            |
| 10  | 删除 600036 的笔记 + 取消关注           | Tier 2 multi CRUD write       | 现有的 HITL flow                          |

**1/2/9 是本 spec 重点；5/6/7/8 是回归（行为不变）。**

---

## 5. 测试计划

### 5.1 Unit tests

`tests/test_short_circuit_multi.py`：

```python
import pytest
from tradingagents.agent_harness.core.tier import classify_multi, fast_route_with_op


@pytest.mark.parametrize("message,expected_pairs", [
    ("看一下 600036.SS 的笔记和告警", {"note", "alert"}),
    ("我的关注列表 + 600036 的笔记", {"watchlist", "note"}),
    ("600036 的 PE 是多少 + 历史价格", {"fundamentals", "history"}),
])
def test_classify_multi_readonly(message, expected_pairs):
    pairs = classify_multi(message)
    intents = {p[0].value for p in pairs}
    assert intents == expected_pairs
    # All pairs must be LIST/READ op
    from tradingagents.agent_harness.core.tier import Op
    assert all(p[1] in (Op.LIST, Op.READ) for p in pairs)


@pytest.mark.parametrize("message,expect_tier", [
    ("看一下 600036.SS 的笔记和告警", "DIRECT"),
    ("我的关注 + 笔记", "DIRECT"),
    ("把 600036 加关注 + 加笔记", "PLAN_EXECUTE"),  # write-only multi
    ("看一下 600036 的笔记 + 加个笔记", "PLAN_EXECUTE"),  # mixed read+write
])
def test_fast_route_readonly_multi(message, expect_tier):
    from tradingagents.agent_harness.core.tier import Tier
    route, _ = fast_route_with_op(message)
    expected = Tier.DIRECT if expect_tier == "DIRECT" else Tier.PLAN_EXECUTE
    assert route.tier == expected
    if expect_tier == "DIRECT":
        assert len(route.multi_pairs) >= 2
```

### 5.2 E2E test

`tests/test_short_circuit_multi_e2e.py`：通过 HTTP `/api/harness/chat` 测：

```python
async def test_multi_intent_read_e2e():
    """'我的关注 + 600036 的笔记' should NOT call LLM synthesize."""
    sid = "e2e-multi-" + str(time.time_ns())
    events = list(parse_sse(post("/api/harness/chat", {
        "session_id": sid,
        "message": "我的关注 + 600036 的笔记",
    }).read().decode()))

    # Tier 1 short-circuit emits: tool_call, tool_result, tool_call,
    # tool_result, agent_final — NO plan_started, NO synthesize.
    event_names = [n for n, _ in events]
    assert "plan_started" not in event_names
    assert "plan_ready" not in event_names

    # 2 tool_calls (one per intent)
    tool_calls = [p for n, p in events if n == "tool_call"]
    assert len(tool_calls) == 2

    # agent_final has multi shape
    final = next(p for n, p in events if n == "agent_final")
    assert "multi" in final.get("result", {})
    assert final["result"]["count"] == 2
```

### 5.3 手工验收

`http://127.0.0.1:8000/harness` 实测 10 条验收矩阵，重点确认：
- `看一下 600036.SS 的笔记和告警` 0.4s 内返回，2 张表
- `把 600036 加关注 + 加笔记` 走 HITL modal
- bubble 不再出现"数据事实 / 行为面观察 / 方向性建议"模板

---

## 6. 风险与 Rollback

### 6.1 风险

| 风险                                                   | 缓解                                          |
| ------------------------------------------------------ | --------------------------------------------- |
| multi-intent 顺序执行 2 个工具比并发慢（500ms vs 200ms）| 用户感知不强（0.5s vs 2.5s 总体仍快 5 倍）。后续可加并发 |
| `_run_multi` 异常时部分结果已 emit，前端可能重复渲染     | `agent_final` payload 加 `_partial` 标记，前端检测 |
| `classify_multi` 把 write op 误判成 LIST/READ         | `Op.LIST/READ` 严格白名单 + parametrize 测试覆盖  |
| ShortCircuit 新增 multi 路径影响现有 single-intent     | `_run_single` 路径不变，pure 单测 + e2e 回归      |

### 6.2 Rollback

- `tier.py` 改动：1 行 if 判断移除即可
- `short_circuit.py` 改动：新增方法 `_run_multi`，删除该方法即回退
- `harness.js` 改动：新增 multi shape 分支，删除该分支即回退

全部 additive，单 commit revert 即可回退。

---

## 7. 时间线 / Commit Plan

| #   | Commit                                                            | 估计时间 |
| --- | ----------------------------------------------------------------- | -------- |
| 1   | `feat(tier): RouteResult.multi_pairs + read-only multi 检测`      | 0.5h     |
| 2   | `feat(short_circuit): _run_multi 并发调用 + multi agent_final`    | 0.5h     |
| 3   | `feat(web): agent_final multi shape 渲染多张表`                   | 0.5h     |
| 4   | `test(harness): classify_multi read-only + fast_route multi tier` | 0.25h    |
| 5   | `test(harness): short_circuit multi e2e (HTTP /chat)`             | 0.5h     |
|     | **合计**                                                          | **2.25h**|

---

## 8. 后续扩展 (Day 15+)

1. **并发执行 multi-intent** — 把 `_run_multi` 改成 `asyncio.gather`，500ms → 200ms
2. **结果合并策略** — 当 multi-intent 涉及 quote+news 时，生成「快速摘要」（当前/情绪/趋势）
3. **混合 multi-intent (read + write)** — 先执行 read（展示当前状态），再触发 write HITL
4. **自定义顺序** — 用户说"先告警再笔记"时按顺序执行
5. **multi-intent LLM synthesizer** — 当 read-only multi 结果需要一句话总结时调 LLM（但不强制）

---

## 9. 审查清单 (reviewer 必读)

- [ ] `classify_multi` 是否正确检测所有 read-only multi-intent？(parametrize 4+ 组合)
- [ ] `tier.py:367` 改动是否会破坏现有 multi-CRUD 路由？(跑 `test_crud_dispatch.py`)
- [ ] `_run_multi` 异常处理是否完备？(tool 抛出 → emit error → 继续下一个)
- [ ] 前端 `multi` shape 检测是否覆盖所有 entry point？(Tier 1 + /confirm + Tier 2 fallback)
- [ ] `intentHeading` 是否覆盖所有 11 个 Intent？(quote/fundamentals/news/history/alpha/note/alert/watchlist/scheduled/run/report)
- [ ] `_partial` 标记是否真的需要？(如果 tool 异常已经 emit error 事件，agent_final 就不要 emit)
