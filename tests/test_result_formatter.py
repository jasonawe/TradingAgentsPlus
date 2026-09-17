"""Single source of truth: tool result → 中文短句。

§11 + §12 验收矩阵 20+ 用例。覆盖:
- 5 entity × 4 op 组合 (note/alert/scheduled/watchlist)
- bulk delete 的 embedded summary 解析
- 通用 ok 路径 (PREFERENCE_UPDATED, added)
- pending_approval / 数据型 ok → None
- 顶层 symbol 优先 vs raw JSON 抠 symbol fallback
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.result_formatter import (
    summarize_tool_result,
    summarize_tool_results,
    _extract_symbol_from_raw,
    _try_parse_embedded_summary,
)


# ---------------------------------------------------------------------------
# NOTE 实体
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("result,expected", [
    # 顶层 symbol 命中
    ({"status": "created", "raw": "NOTE_CREATED: ...", "symbol": "600036.SS"},
     "笔记已保存 (600036.SS)"),
    # raw JSON 抠 symbol
    ({"status": "created", "raw": 'NOTE_CREATED: {"id":"n1","symbol":"600036.SS"}'},
     "笔记已保存 (600036.SS)"),
    # 无 symbol
    ({"status": "created", "raw": "NOTE_CREATED: ...", "symbol": None},
     "笔记已保存"),
    ({"status": "updated", "raw": 'NOTE_UPDATED: {"id":"n1","symbol":"513880.SS"}'},
     "笔记已更新 (513880.SS)"),
    ({"status": "deleted", "raw": 'NOTE_DELETED: {"id":"n1","symbol":"600031.SS"}'},
     "笔记已删除 (600031.SS)"),
])
def test_summarize_note(result, expected):
    assert summarize_tool_result(result) == expected


# ---------------------------------------------------------------------------
# ALERT 实体
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("result,expected", [
    ({"status": "created", "raw": 'ALERT_CREATED: {"id":"a1","symbol":"513880.SS"}'},
     "告警已创建 (513880.SS)"),
    ({"status": "updated", "raw": 'ALERT_UPDATED: {"id":"a1","symbol":"513880.SS"}'},
     "告警已更新 (513880.SS)"),
    ({"status": "deleted", "raw": 'ALERT_DELETED: {"id":"a1","symbol":"600999.SS"}'},
     "告警已删除 (600999.SS)"),
])
def test_summarize_alert(result, expected):
    assert summarize_tool_result(result) == expected


# ---------------------------------------------------------------------------
# SCHEDULED 实体 (新增 — 原 spec 漏了)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("result,expected", [
    ({"status": "created", "raw": 'SCHEDULED_CREATED: {"id":"j1","symbol":"600036.SS"}'},
     "定时任务已创建 (600036.SS)"),
    ({"status": "updated", "raw": 'SCHEDULED_UPDATED: {"id":"j1","enabled":false}'},
     "定时任务已更新"),
    ({"status": "deleted", "raw": "SCHEDULED_DELETED: j1"},
     "定时任务已删除"),
])
def test_summarize_scheduled(result, expected):
    assert summarize_tool_result(result) == expected


# ---------------------------------------------------------------------------
# WATCHLIST 实体 (新增 — 前缀是 ADDED:/REMOVED:/DUPLICATE:)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("result,expected", [
    ({"status": "created", "raw": 'ADDED: {"id":"i1","symbol":"600036.SS"}',
      "symbol": "600036.SS"},
     "已加入关注 (600036.SS)"),
    ({"status": "deleted", "raw": "REMOVED: 600036.SS", "symbol": "600036.SS"},
     "已移出关注 (600036.SS)"),
    ({"status": "duplicate", "symbol": "600036.SS", "raw": "DUPLICATE: 600036.SS"},
     "已在关注列表中,无需重复添加 (600036.SS)"),
])
def test_summarize_watchlist(result, expected):
    assert summarize_tool_result(result) == expected


# ---------------------------------------------------------------------------
# Bulk delete embedded summary (§11.3 关键修复)
# ---------------------------------------------------------------------------
def test_bulk_delete_notes_embedded_summary():
    """delete_notes_for_symbol: tool 已经写好人话在 raw JSON 里。"""
    raw = (
        '{"status":"ok","summary":"资产 600036.SS 的笔记已删除:15/15 条成功",'
        '"symbol":"600036.SS","asset_type":"stock","matched":15,'
        '"deleted":15,"failed":0,"ids":["n1","n2"]}'
    )
    result = {"status": "ok", "raw": raw}
    assert summarize_tool_result(result) == "资产 600036.SS 的笔记已删除:15/15 条成功"


def test_bulk_delete_notes_no_match():
    """matched=0 时的 noop summary。"""
    raw = (
        '{"status":"noop","summary":"资产 600036.SS 当前没有活跃笔记可删除'
        '(可能之前已全部删除)","symbol":"600036.SS","asset_type":"stock",'
        '"matched":0,"deleted":0,"failed":0,"ids":[]}'
    )
    result = {"status": "ok", "raw": raw}
    assert summarize_tool_result(result) == (
        "资产 600036.SS 当前没有活跃笔记可删除(可能之前已全部删除)"
    )


def test_bulk_delete_alerts_embedded_summary():
    raw = (
        '{"status":"ok","summary":"资产 513880.SS 的告警已删除:2/2 条成功",'
        '"symbol":"513880.SS","asset_type":"stock","matched":2,"deleted":2}'
    )
    result = {"status": "ok", "raw": raw}
    assert summarize_tool_result(result) == "资产 513880.SS 的告警已删除:2/2 条成功"


def test_bulk_delete_scheduled_embedded_summary():
    """delete_scheduled_tasks_for_symbol 没有 summary 字段 — 合成 fallback 字符串。"""
    raw = '{"status":"ok","symbol":"600036.SS","matched":3,"deleted":3}'
    result = {"status": "ok", "raw": raw}
    assert summarize_tool_result(result) == "(600036.SS) 已删除:3/3 条成功"

def test_bulk_delete_scheduled_empty():
    raw = '{"status":"ok","symbol":"600036.SS","matched":0,"deleted":0}'
    result = {"status": "ok", "raw": raw}
    assert summarize_tool_result(result) == "(600036.SS) 当前没有可删除项"


# ---------------------------------------------------------------------------
# 通用 ok 路径
# ---------------------------------------------------------------------------
def test_preference_updated():
    result = {"status": "ok", "raw": "PREFERENCE_UPDATED: risk_tolerance"}
    assert summarize_tool_result(result) == "偏好已更新"


def test_added_to_watchlist_generic():
    """add_to_watchlist 偶尔走 ok + 'added' raw 路径(老 format)。"""
    result = {"status": "ok", "raw": "added 600036.SS to watchlist",
              "symbol": "600036.SS"}
    assert summarize_tool_result(result) == "600036.SS 已加入关注"


# ---------------------------------------------------------------------------
# None / 数据型 ok — 不应被 friendly 化
# ---------------------------------------------------------------------------
def test_pending_approval_returns_none():
    result = {"status": "pending_approval", "raw": "AWAITING_CONFIRMATION: ..."}
    assert summarize_tool_result(result) is None


def test_quote_data_returns_none():
    """Tier 1 quote snapshot 不应该被 friendly 化(留给 formatRawResult)。"""
    result = {"status": "ok", "price": 41.78, "symbol": "600036.SS", "raw": ""}
    assert summarize_tool_result(result) is None


def test_unknown_status_returns_none():
    assert summarize_tool_result({"status": "rate_limited"}) is None


def test_non_dict_returns_none():
    assert summarize_tool_result("not a dict") is None
    assert summarize_tool_result(None) is None
    assert summarize_tool_result([1, 2, 3]) is None


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------
def test_extract_symbol_from_raw_valid():
    assert _extract_symbol_from_raw('NOTE_CREATED: {"symbol":"600036.SS"}') == "600036.SS"
    assert _extract_symbol_from_raw('ADDED: {"symbol":"X"}') == "X"


def test_extract_symbol_from_raw_invalid():
    assert _extract_symbol_from_raw("not a json prefix") is None
    assert _extract_symbol_from_raw('NOTE_CREATED: {invalid json') is None
    assert _extract_symbol_from_raw("NOTE_CREATED: [\"no symbol\"]") is None


def test_try_parse_embedded_summary_valid():
    raw = '{"summary":"资产 X 的笔记已删除:5/5 条成功"}'
    assert _try_parse_embedded_summary(raw) == "资产 X 的笔记已删除:5/5 条成功"


def test_try_parse_embedded_summary_invalid():
    assert _try_parse_embedded_summary("plain string") is None
    assert _try_parse_embedded_summary("[1,2,3]") is None  # not a dict
    assert _try_parse_embedded_summary('{"no_summary":true}') is None
    assert _try_parse_embedded_summary(None) is None


# ---------------------------------------------------------------------------
# 批量聚合
# ---------------------------------------------------------------------------
def test_summarize_tool_results_aggregates():
    results = [
        {"status": "created", "raw": 'NOTE_CREATED: {"symbol":"A"}'},
        {"status": "deleted", "raw": 'NOTE_DELETED: {"symbol":"B"}'},
        {"status": "ok", "price": 41.78},  # 跳过(数据型)
    ]
    out = summarize_tool_results(results)
    assert "笔记已保存 (A)" in out
    assert "笔记已删除 (B)" in out
    assert out.count("\n") == 1


def test_summarize_tool_results_empty():
    assert summarize_tool_results([]) == ""
    assert summarize_tool_results(None) == ""


def test_summarize_tool_results_all_data():
    """全是数据型 ok → 空字符串,触发 _trivial_crud_summary 返回 None。"""
    results = [
        {"status": "ok", "price": 41.78, "symbol": "A"},
        {"status": "ok", "items": [{"title": "stub"}]},
    ]
    assert summarize_tool_results(results) == ""
