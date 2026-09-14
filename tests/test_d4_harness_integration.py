"""Harness integration test (P4 + §6 init order)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config.schema import HarnessConfig  # noqa: E402
from tradingagents.agent_harness.harness import Harness  # noqa: E402
from tradingagents.agent_harness.tools import PermissionType  # noqa: E402


def test_harness_initializes_with_19_tools() -> None:
    h = Harness(HarnessConfig.from_env())
    assert len(h.list_tools()) == 19
    reads = [t for t in h.list_tools() if t.permission == PermissionType.READ]
    writes = [t for t in h.list_tools() if t.permission == PermissionType.WRITE]
    assert len(reads) == 10
    assert len(writes) == 9


def test_harness_get_tool() -> None:
    h = Harness(HarnessConfig.from_env())
    tool = h.get_tool("get_quote")
    assert tool.name == "get_quote"


def test_harness_data_registry_has_4_providers() -> None:
    h = Harness(HarnessConfig.from_env())
    assert set(h.data_registry) == {"yfinance", "eastmoney", "akshare", "alpha_vantage"}


def test_harness_stream_chat_tier1(monkeypatch) -> None:
    from tests.test_d4_orchestrator import _install_mock_provider
    _install_mock_provider(monkeypatch)
    h = Harness(HarnessConfig.from_env())

    async def _run():
        out = []
        async for ev in h.stream_chat("session-1", "600036.SS 多少钱"):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    names = [e[0] for e in events]
    assert "agent_final" in names
