"""Orchestrator synthesise fast-path: trivial CRUD acks skip the LLM.

The fast-path triggers when every tool result is a CRUD ack
(status in {ok, created, updated, deleted, duplicate, pending_approval,
empty}) and no result carries data-bearing fields (price, items,
factors, alerts, notes, rows). It returns a templated summary string
instead of paying the 1-3 second LLM round-trip.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.orchestrator import (
    Orchestrator,
    _format_trivial_summary,
)


@pytest.mark.parametrize(
    "results,expected_substr",
    [
        (
            [{"status": "created", "raw": "NOTE_CREATED: note-abc 600036.SS", "symbol": "600036.SS"}],
            "笔记已保存",
        ),
        (
            [{"status": "updated", "raw": "NOTE_UPDATED: note-abc 600036.SS", "symbol": "600036.SS"}],
            "笔记已更新",
        ),
        (
            [{"status": "deleted", "raw": "NOTE_DELETED: note-abc 600036.SS", "symbol": "600036.SS"}],
            "笔记已删除",
        ),
        (
            [{"status": "created", "raw": "ALERT_CREATED: alert-abc 513880.SS", "symbol": "513880.SS"}],
            "告警已创建",
        ),
        (
            [{"status": "duplicate", "symbol": "600036.SS", "raw": "DUPLICATE: 600036.SS"}],
            "已在关注列表中",
        ),
        (
            [{"status": "pending_approval", "raw": "AWAITING_CONFIRMATION: ..."}],
            "等待你确认",
        ),
        (
            [{"status": "ok", "raw": "PREFERENCE_UPDATED: foo"}],
            "偏好已更新",
        ),
        (
            [{"status": "ok", "raw": "added 600036.SS to watchlist", "symbol": "600036.SS"}],
            "已加入关注",
        ),
    ],
)
def test_trivial_summary_renders_each_status(results, expected_substr):
    out = _format_trivial_summary(results)
    assert expected_substr in out


def test_trivial_summary_handles_batch():
    results = [
        {"status": "created", "raw": "NOTE_CREATED: note-abc 600036.SS", "symbol": "600036.SS"},
        {"status": "deleted", "raw": "ALERT_DELETED: alert-xyz 513880.SS", "symbol": "513880.SS"},
    ]
    out = _format_trivial_summary(results)
    assert "笔记已保存" in out and "告警已删除" in out


def test_trivial_crud_summary_returns_dict_for_all_crude():
    results = [
        {"status": "created", "raw": "NOTE_CREATED: note-abc 600036.SS", "symbol": "600036.SS"},
        {"status": "deleted", "raw": "NOTE_DELETED: note-xyz 513880.SS", "symbol": "513880.SS"},
    ]
    out = Orchestrator._trivial_crud_summary(results)
    assert out is not None
    assert "summary" in out
    assert out["intent"] == "crud"
    assert "笔记已保存" in out["summary"]


def test_trivial_crud_summary_returns_none_for_quote_data():
    """Quote snapshots carry price + volume — must NOT trigger fast-path."""
    results = [
        {"status": "ok", "price": 41.78, "change_pct": 1.04, "volume": 67232000, "symbol": "600036.SS"},
    ]
    out = Orchestrator._trivial_crud_summary(results)
    assert out is None, "quote snapshot should require LLM synthesis"


def test_trivial_crud_summary_returns_none_for_news_items():
    results = [{"status": "ok", "items": [{"title": "stub"}]}]
    assert Orchestrator._trivial_crud_summary(results) is None


def test_trivial_crud_summary_returns_none_for_factor_list():
    results = [{"status": "ok", "factors": ["alpha_001", "alpha_002"]}]
    assert Orchestrator._trivial_crud_summary(results) is None


def test_trivial_crud_summary_returns_none_for_alert_list():
    results = [{"status": "ok", "alerts": [{"id": "alert-1"}]}]
    assert Orchestrator._trivial_crud_summary(results) is None


def test_trivial_crud_summary_returns_none_when_mixed():
    """If one result has data and another is trivial, fall through to LLM."""
    results = [
        {"status": "created", "raw": "NOTE_CREATED: ...", "symbol": "600036.SS"},
        {"status": "ok", "price": 41.78, "symbol": "600036.SS"},
    ]
    assert Orchestrator._trivial_crud_summary(results) is None


def test_trivial_crud_summary_returns_none_for_empty_list():
    assert Orchestrator._trivial_crud_summary([]) is None


def test_trivial_crud_summary_returns_none_for_unknown_status():
    """Statuses outside the whitelist must not be fast-pathed."""
    results = [{"status": "rate_limited"}]
    assert Orchestrator._trivial_crud_summary(results) is None


def test_trivial_crud_summary_handles_ok_with_only_status():
    """Status='ok' with no data-bearing fields is still trivial."""
    results = [{"status": "ok", "raw": "operation complete"}]
    out = Orchestrator._trivial_crud_summary(results)
    assert out is not None
    assert "操作成功" in out["summary"]
