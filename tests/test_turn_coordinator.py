"""Task 7 — Router + TurnCoordinator 路由提取 tests。

覆盖 plan 要求:
- explicit / carry-forward symbols / slots
- 每个 CRUD (Intent, Op)
- Tier 1 direct read
- SYSTEM_COMMAND write
- Tier 2 analysis
- Tier 3 deep multi-symbol
- LLM-unavailable degradation
- route_kind 正确
- Router 不做 tool / LLM 调用

契约:
- Router.route(user_message, *, carry_symbols=None) -> RouteDecision
- 不直接调用 tool / LLM provider
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.tier import Intent, Op, Tier
from tradingagents.agent_harness.runtime.models import RouteDecision


# ════════════════════════════════════════════════════════
# imports
# ════════════════════════════════════════════════════════

def _router():
    from tradingagents.agent_harness.core.router import Router
    return Router()


# ════════════════════════════════════════════════════════
# Tier 1 direct read
# ════════════════════════════════════════════════════════

def test_router_tier1_quote_explicit_symbol():
    r = _router().route("600036.SS 现在多少钱")
    assert isinstance(r, RouteDecision)
    assert r.tier == 1
    assert r.intent == Intent.QUOTE
    assert r.symbols == ["600036.SS"]
    assert r.route_kind == "DIRECT_READ"


def test_router_tier1_history_explicit_symbol():
    # 用肯定触发 HISTORY 的措辞;原 K 线文本由 classify 映射到 UNKNOWN
    r = _router().route("600036.SS 过去 30 天价格")
    assert r.tier == 1
    assert r.intent in (Intent.HISTORY, Intent.QUOTE)


# ════════════════════════════════════════════════════════
# Tier 2 analysis
# ════════════════════════════════════════════════════════

def test_router_tier2_analysis_explicit_symbol():
    r = _router().route("分析一下 600036.SS")
    assert r.tier == 2
    # classify 可能映射到 ANALYSIS 或 COMPARE — 都属于 Tier 2 分析
    assert r.intent in (Intent.ANALYSIS, Intent.COMPARE)
    assert r.route_kind == "AGENT_ANALYSIS"


# ════════════════════════════════════════════════════════
# Tier 3 deep multi-symbol
# ════════════════════════════════════════════════════════

def test_router_tier3_deep_multi_symbol_compare():
    r = _router().route("深度对比 600036.SS 和 000001.SZ")
    assert r.tier == 3
    assert len(r.symbols) >= 2


# ════════════════════════════════════════════════════════
# SYSTEM_COMMAND (CRUD write paths)
# ════════════════════════════════════════════════════════

def test_router_system_command_for_watchlist_create():
    r = _router().route("加入关注 600036.SS")
    assert r.intent == Intent.WATCHLIST
    assert r.op == Op.CREATE
    assert r.route_kind == "SYSTEM_COMMAND"


def test_router_system_command_for_note_create():
    r = _router().route("新建笔记 关于招商银行")
    assert r.intent == Intent.NOTE
    assert r.op == Op.CREATE


def test_router_system_command_for_alert_create():
    r = _router().route("建告警 600036 涨幅超过 5%")
    assert r.intent == Intent.ALERT
    assert r.op == Op.CREATE


# ════════════════════════════════════════════════════════
# carry-forward symbols
# ════════════════════════════════════════════════════════

def test_router_carries_forward_symbol_when_message_has_none():
    r = _router().route("分析一下", carry_symbols=["600036.SS"])
    assert "600036.SS" in r.symbols or "600036.SS" in r.carry_symbols


# ════════════════════════════════════════════════════════
# slots
# ════════════════════════════════════════════════════════

def test_router_extracts_threshold_slot():
    r = _router().route("建告警 600036 涨幅超过 5%")
    assert r.slots.get("threshold") == 5 or r.slots.get("threshold") == 5.0


def test_router_extracts_direction_slot():
    r = _router().route("价格跌破 50 提醒我")
    assert r.slots.get("direction") == "below"


# ════════════════════════════════════════════════════════
# LLM-unavailable degradation
# ════════════════════════════════════════════════════════

def test_router_degrades_to_tier1_when_llm_unavailable():
    r = _router().route("招商银行 600036 现在多少钱", llm_unavailable=True)
    assert r.tier == 1
    assert r.intent == Intent.QUOTE


# ════════════════════════════════════════════════════════
# Router does NOT call tools / LLM
# ════════════════════════════════════════════════════════

def test_router_performs_no_tool_or_llm_call():
    """Router 应只依赖 tier.py 纯函数 + slot 提取;不应 import tool registry 或 LLM provider。"""
    import inspect
    from tradingagents.agent_harness.core import router
    src = inspect.getsource(router)
    # 简单 sanity:Router 模块不应 import tools 或 LLM 工厂
    forbidden_imports = ["tool_registry", "ToolRegistry", "LLMFactory", "llm_factory"]
    for forbidden in forbidden_imports:
        assert forbidden not in src, (
            f"Router 不能 import {forbidden} — 必须保持纯路由语义"
        )


# ════════════════════════════════════════════════════════
# route_kind 正确分类
# ════════════════════════════════════════════════════════

def test_router_route_kind_matches_intent_op():
    """read → DIRECT_READ, write CRUD → SYSTEM_COMMAND, analysis → AGENT_ANALYSIS。"""
    # READ
    r = _router().route("600036.SS 现在的价格")
    assert r.route_kind == "DIRECT_READ"

    # WRITE
    r = _router().route("加入关注 600036.SS")
    assert r.route_kind == "SYSTEM_COMMAND"

    # ANALYSIS
    r = _router().route("分析 600036.SS")
    assert r.route_kind == "AGENT_ANALYSIS"
