"""Tests for Tier 1b template engine (v2 spec §D1 N101 fix).

覆盖:
- TemplateEngine: 默认模板渲染 / 注册 / 覆盖 / 取消注册 / unknown
- 中文模板 + Jinja2 控制结构
- should_use_template 关键词触发判断
- short_circuit Tier 1b 集成: query 含「说明」时走模板路径
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest


# ---------------------------------------------------------------------------
# TemplateEngine
# ---------------------------------------------------------------------------
def test_engine_has_default_templates():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    for name in ("quote_simple", "fundamentals_simple", "history_simple", "news_simple"):
        assert eng.has(name)


def test_render_default_quote_template():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    out = eng.render(
        "quote_simple",
        symbol="600036.SS",
        price=41.71,
        change=0.43,
        change_pct=1.04,
        volume=67232000,
    )
    assert "600036.SS" in out
    assert "41.71" in out
    assert "1.04" in out  # change_pct
    assert "67232000" in out  # raw int
    assert "现价" in out  # 中文片段


def test_render_fundamentals_optional_fields():
    """Fundamentals 模板的部分字段缺失时不应崩溃。"""
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    out = eng.render("fundamentals_simple", market_cap=1053685149209.78)
    assert "总市值" in out
    assert "10536.85" in out or "10536.86" in out  # 1e8 换算后


def test_render_history_with_lists():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    closes = [40.5, 41.2, 41.7]
    out = eng.render(
        "history_simple",
        bars=closes,
        closes=closes,
        avg_close=sum(closes) / len(closes),
    )
    assert "40.5" in out
    assert "41.2" in out
    assert "41.7" in out


def test_render_news_with_items():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    items = [{"title": "招商银行发布年报"}, {"title": "央行下调存款准备金率"}]
    out = eng.render("news_simple", items=items)
    assert "招商银行" in out
    assert "央行" in out


def test_render_unknown_template_raises():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    with pytest.raises(KeyError):
        eng.render("nonexistent_template")


def test_register_new_template():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    eng.register("greet", "你好 {{ user_name }}!")
    assert eng.has("greet")
    assert eng.render("greet", user_name="世界") == "你好 世界!"


def test_register_rejects_duplicate_without_overwrite():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    with pytest.raises(ValueError):
        eng.register("quote_simple", "override")


def test_register_with_overwrite_replaces():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    eng.register("quote_simple", "新模板: {{ price }}", overwrite=True)
    assert eng.render("quote_simple", price=10) == "新模板: 10"


def test_unregister_removes_template():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    assert eng.unregister("quote_simple") is True
    assert not eng.has("quote_simple")
    assert eng.unregister("quote_simple") is False


def test_names_returns_sorted_keys():
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    names = eng.names()
    assert names == sorted(names)
    assert "quote_simple" in names


def test_strict_undefined_missing_var_raises():
    """StrictUndefined 模式下,缺字段应该抛错而不是 silent empty。"""
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    with pytest.raises(Exception):  # jinja2.UndefinedError
        eng.render("quote_simple", symbol="X", price=1.0)
        # 缺 change / change_pct / volume


def test_engine_passes_through_chinese_characters():
    """确保中文模板不出现编码问题。"""
    from tradingagents.agent_harness.core.template import TemplateEngine
    eng = TemplateEngine()
    eng.register("cn_demo", "标的 {{ symbol }},价格 {{ price | round(2) }} 元")
    out = eng.render("cn_demo", symbol="600036.SS", price=41.7)
    assert out == "标的 600036.SS,价格 41.7 元"


# ---------------------------------------------------------------------------
# should_use_template trigger
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("message,expected", [
    ("招商银行 600036 现在的价格,顺便说明一下", True),
    ("解释 600036 走势", True),
    ("介绍 招商银行 的 PE 指标", True),
    ("讲讲 600036 估值", True),
    ("详细说明 当前持仓", True),
    ("用文字解释 RSI 含义", True),
    ("explain AAPL valuation", False),  # 英文触发不到(默认 keyword 中文)
    ("", False),
    ("600036 多少钱", False),
    ("600036 现价", False),
])
def test_should_use_template(message: str, expected: bool):
    from tradingagents.agent_harness.core.template import should_use_template
    assert should_use_template(message) is expected


# ---------------------------------------------------------------------------
# short_circuit Tier 1b integration
# ---------------------------------------------------------------------------
def _build_harness_with_template(tmp_path: pathlib.Path):
    """Build a Harness + ensure tier-1b path can be exercised via ShortCircuit."""
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    h = Harness(HarnessConfig(data_dir=tmp_path))
    return h


def test_short_circuit_renders_template_when_query_has_explain_keyword(tmp_path: pathlib.Path):
    """When query contains 「说明」, ShortCircuit should render
    quote_simple instead of raw emit (Tier 1b)."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.template import TemplateEngine
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    from tradingagents.agent_harness.tools import ToolContext

    # Stub the registry to return a fake quote tool
    class _FakeTool:
        schema = type("S", (), {"args_schema": type("A", (), {})()})()
        async def invoke(self, args, context):
            # Return a plain dict-like; short_circuit will _safe_dump it.
            return {"symbol": "600036.SS", "price": 41.71, "change": 0.43,
                    "change_pct": 1.04, "volume": 67232000, "as_of": "2026-09-14"}

    class _FakeRegistry:
        def get(self, name):
            return _FakeTool()

    sc = ShortCircuit(_FakeRegistry())
    route = RouteResult(
        intent=Intent.QUOTE, tier=Tier.DIRECT,
        symbols=["600036.SS"], confidence=0.9, reason="test",
    )
    ctx = ToolContext(session_id="s1")
    # Replace engine so we can capture template rendering — actually
    # ShortCircuit doesn't have a TemplateEngine yet (TBD).  This test
    # verifies the trigger keyword + a future engine wiring by checking
    # the ShortCircuit returns the raw data, then asserting
    # should_use_template returns True so caller can post-process.
    events = []
    async def _drain():
        async for ev, payload in sc.run(route, "说明 600036 现在多少钱", ctx):
            events.append((ev, payload))
    asyncio.run(_drain())
    ev_names = [e[0] for e in events]
    assert "tool_call" in ev_names
    assert "agent_final" in ev_names

    # Independent: should_use_template 触发器断言
    from tradingagents.agent_harness.core.template import should_use_template
    assert should_use_template("说明 600036 现在多少钱") is True


# ---------------------------------------------------------------------------
# short_circuit Tier 1b wire-integration
# ---------------------------------------------------------------------------
def test_short_circuit_with_engine_emits_text_when_keyword_matches(tmp_path):
    """Wire-up: ShortCircuit(template_engine=...) + query 含「说明」 →
    agent_final.rendered=True + result 是文字 string。"""
    import asyncio
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.template import TemplateEngine
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    from tradingagents.agent_harness.tools import ToolContext

    class _FakeTool:
        class _S:
            args_schema = type("A", (), {})()
        schema = _S()
        async def invoke(self, args, context):
            return {"symbol": "600036.SS", "price": 41.71, "change": 0.43,
                    "change_pct": 1.04, "volume": 67232000}

    class _FakeReg:
        def get(self, name):
            return _FakeTool()

    sc = ShortCircuit(_FakeReg(), template_engine=TemplateEngine())
    route = RouteResult(
        intent=Intent.QUOTE, tier=Tier.DIRECT,
        symbols=["600036.SS"], confidence=0.9, reason="test",
    )
    ctx = ToolContext(session_id="s1")
    final = []

    async def _drain():
        async for ev, p in sc.run(route, "说明 600036 现在多少钱", ctx):
            if ev == "agent_final":
                final.append(p)
    asyncio.run(_drain())

    assert len(final) == 1
    payload = final[0]
    assert payload["rendered"] is True
    assert payload["template"] == "quote_simple"
    assert isinstance(payload["result"], str)
    assert "现价" in payload["result"]


def test_short_circuit_without_engine_falls_back_to_raw(tmp_path):
    """No template engine wired → Tier 1a raw emit (default path)."""
    import asyncio
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    from tradingagents.agent_harness.tools import ToolContext

    class _FakeTool:
        class _S:
            args_schema = type("A", (), {})()
        schema = _S()
        async def invoke(self, args, context):
            return {"symbol": "600036.SS", "price": 41.71, "change": 0.43,
                    "change_pct": 1.04, "volume": 67232000}

    class _FakeReg:
        def get(self, name):
            return _FakeTool()

    sc = ShortCircuit(_FakeReg())  # no template engine
    route = RouteResult(
        intent=Intent.QUOTE, tier=Tier.DIRECT,
        symbols=["600036.SS"], confidence=0.9, reason="test",
    )
    ctx = ToolContext(session_id="s1")
    final = []

    async def _drain():
        async for ev, p in sc.run(route, "说明 600036 现在多少钱", ctx):
            if ev == "agent_final":
                final.append(p)
    asyncio.run(_drain())

    assert len(final) == 1
    payload = final[0]
    assert "rendered" not in payload or payload.get("rendered") is None
    # result is the raw dict
    assert isinstance(payload["result"], dict)


def test_short_circuit_no_template_match_falls_back_to_raw(tmp_path):
    """Engine is wired but no template for this intent → fall back to raw."""
    import asyncio
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.template import TemplateEngine
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    from tradingagents.agent_harness.tools import ToolContext

    class _FakeTool:
        class _S:
            args_schema = type("A", (), {})()
        schema = _S()
        async def invoke(self, args, context):
            return {"items": ["a", "b"]}

    class _FakeReg:
        def get(self, name):
            return _FakeTool()

    # Strip all templates — even with engine, no name match
    eng = TemplateEngine({})
    sc = ShortCircuit(_FakeReg(), template_engine=eng)
    route = RouteResult(
        intent=Intent.QUOTE, tier=Tier.DIRECT,
        symbols=["600036.SS"], confidence=0.9, reason="test",
    )
    ctx = ToolContext(session_id="s1")
    final = []

    async def _drain():
        async for ev, p in sc.run(route, "说明 600036", ctx):
            if ev == "agent_final":
                final.append(p)
    asyncio.run(_drain())

    payload = final[0]
    assert "rendered" not in payload
    assert isinstance(payload["result"], dict)
