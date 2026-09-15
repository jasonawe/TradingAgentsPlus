"""Tests for §7.2 #9 — Graceful degradation via DataResponse warnings.

spec §7.2 #9: tool 返回 ``DataResponse(warnings=[err])`` 不抛异常。
上层 (orchestrator / agent) 应该把 warnings 透出但不 crash。

覆盖:
- DataResponse 构造: warnings 字段默认空 list,可追加
- 序列化 / Pydantic round-trip
- warnings 在 tool result 中传递(模拟 e2e: provider 失败 → 返 DataResponse with warning →
  orchestrator 不抛异常)
- empty warnings 视为正常路径
- chart 字段默认 None
- Generic[T] 类型参数不影响 Pydantic 行为
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# DataResponse construction
# ---------------------------------------------------------------------------
def test_data_response_default_warnings_empty():
    """Default: warnings 字段空 list (不显式传)。"""
    from tradingagents.data.responses import DataResponse
    resp = DataResponse(results={"x": 1}, provider="eastmoney")
    assert resp.warnings == []
    assert resp.provider == "eastmoney"
    assert resp.results == {"x": 1}


def test_data_response_with_warnings_does_not_raise():
    """§7.2 #9: warnings 非空时构造不应抛异常。"""
    from tradingagents.data.responses import DataResponse
    resp = DataResponse(
        results={"x": 1},
        provider="yfinance",
        warnings=["yfinance rate limit hit", "fallback to alpha_vantage"],
    )
    assert resp.warnings == ["yfinance rate limit hit", "fallback to alpha_vantage"]


def test_data_response_warnings_can_be_appended():
    from tradingagents.data.responses import DataResponse
    resp = DataResponse(results=[], provider="test")
    resp.warnings.append("first warning")
    resp.warnings.append("second warning")
    assert resp.warnings == ["first warning", "second warning"]


def test_data_response_chart_defaults_to_none():
    """N93 fix: chart 默认 None,server-side 不渲染。"""
    from tradingagents.data.responses import DataResponse
    resp = DataResponse(results={"x": 1}, provider="x")
    assert resp.chart is None


def test_data_response_fetched_at_defaults_to_now():
    from tradingagents.data.responses import DataResponse
    resp = DataResponse(results={"x": 1}, provider="x")
    assert isinstance(resp.fetched_at, datetime)
    # tz-aware (UTC)
    assert resp.fetched_at.tzinfo is not None


def test_data_response_serialization_roundtrip():
    """Pydantic v2 model_dump + model_validate round-trip 保持 warnings。"""
    from tradingagents.data.responses import DataResponse
    original = DataResponse(
        results={"price": 41.71, "change": 0.43},
        provider="eastmoney",
        warnings=["partial data"],
    )
    dumped = original.model_dump()
    restored = DataResponse.model_validate(dumped)
    assert restored.warnings == ["partial data"]
    assert restored.provider == "eastmoney"
    assert restored.results == {"price": 41.71, "change": 0.43}


def test_data_response_preserves_explicit_fetched_at():
    from tradingagents.data.responses import DataResponse
    ts = datetime(2026, 9, 14, 6, 0, 0, tzinfo=timezone.utc)
    resp = DataResponse(results={}, provider="x", fetched_at=ts)
    assert resp.fetched_at == ts


# ---------------------------------------------------------------------------
# Simulated provider e2e: provider 失败 → DataResponse with warning → 不抛
# ---------------------------------------------------------------------------
def test_provider_failure_returns_data_response_with_warning():
    """模拟 spec §7.2 #9 核心场景: provider 不可用时,不能抛异常,
    应该包成 DataResponse(warnings=[...]) 返回。"""
    from tradingagents.data.responses import DataResponse

    class _FailingProvider:
        name = "stub_failing"

        def get_quote(self, symbol, asset_type):
            try:
                raise ConnectionError("provider down")
            except ConnectionError as exc:
                # §7.2 #9 graceful degradation
                return DataResponse(
                    results={"symbol": symbol, "price": None, "skipped": True},
                    provider=self.name,
                    warnings=[f"provider unavailable: {exc}"],
                )

    p = _FailingProvider()
    resp = p.get_quote("600036.SS", "stock")
    # 不抛异常,返 DataResponse
    assert isinstance(resp, DataResponse)
    assert resp.provider == "stub_failing"
    assert len(resp.warnings) == 1
    assert "provider unavailable" in resp.warnings[0]
    # Caller 可以继续判断
    assert resp.results["skipped"] is True


def test_provider_partial_failure_appends_warning_but_returns_results():
    """§7.2 #9: 部分失败也要返 results,warnings 说明。"""
    from tradingagents.data.responses import DataResponse

    class _PartialProvider:
        name = "stub_partial"

        def get_quote(self, symbol, asset_type):
            return DataResponse(
                results={"symbol": symbol, "price": 41.71},
                provider=self.name,
                warnings=["turnover rate unavailable"],
            )

    p = _PartialProvider()
    resp = p.get_quote("600036.SS", "stock")
    # 部分结果 + warning
    assert resp.results["price"] == 41.71
    assert "turnover rate unavailable" in resp.warnings


# ---------------------------------------------------------------------------
# Tool layer: graceful degradation end-to-end
# ---------------------------------------------------------------------------
def test_short_circuit_handles_data_response_with_warnings(tmp_path):
    """§7.2 #9 + Tier 1 short-circuit: tool 返 DataResponse with warnings
    时,short_circuit 透传 result,warnings 进入 result_payload,
    上层(orchestrator)不抛异常。"""
    import asyncio
    import pathlib
    from tradingagents.data.responses import DataResponse
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    from tradingagents.agent_harness.tools import ToolContext

    class _StubQuote:
        class _S:
            args_schema = type("A", (), {})()
        schema = _S()
        async def invoke(self, args, context):
            return DataResponse(
                results={"symbol": "600036.SS", "price": 41.71},
                provider="stub",
                warnings=["partial data"],
            )

    class _StubReg:
        def get(self, name):
            return _StubQuote()

    sc = ShortCircuit(_StubReg())
    route = RouteResult(
        intent=Intent.QUOTE, tier=Tier.DIRECT,
        symbols=["600036.SS"], confidence=0.9, reason="test",
    )
    ctx = ToolContext(session_id="s1")

    events = []
    async def _drain():
        async for ev, p in sc.run(route, "600036 多少钱", ctx):
            events.append((ev, p))
    asyncio.run(_drain())

    ev_names = [e[0] for e in events]
    # 不抛异常 → tool_call + tool_result + agent_final 都出现
    assert "tool_call" in ev_names
    assert "tool_result" in ev_names
    assert "agent_final" in ev_names
    # tool_result.payload 含 warnings
    tool_result_payload = next(p for ev, p in events if ev == "tool_result")
    assert "warnings" in tool_result_payload["result"]
    assert tool_result_payload["result"]["warnings"] == ["partial data"]
