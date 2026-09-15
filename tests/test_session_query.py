"""Q2 / P2-5 — SessionQuery 跨 session 查询。

覆盖:
  - find_sessions_by_symbol 命中 tool_args.symbol
  - find_sessions_by_symbol 命中 tool_args.symbols (list)
  - find_sessions_by_symbol 默认排除 archived
  - find_sessions_by_symbol empty / not-found 返 []
  - search_sessions_by_text 匹配 nl_query
  - search_sessions_by_text 匹配 tool_result
  - search_sessions_by_text 排除 archived 默认
  - get_session_summary 组合 Session + refs + last_event_at
  - get_session_summary 不存在返 None
  - aggregate_stats total/active/archived + by_user
  - aggregate_stats top_symbols 从 tool_args 抽
  - OperationalError 安全降级(表不存在时)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def store_with_refs(tmp_path):
    """In-memory SessionStore + agent_references 表预填数据。"""
    db_path = tmp_path / "q2.sqlite"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS agent_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            nl_query TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_args TEXT,
            tool_result TEXT,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    # 注入 sessions
    sessions = [
        ("s-AAPL", "u1", "2026-09-01T00:00:00Z", "2026-09-14T00:00:00Z", 5, "active"),
        ("s-NVDA", "u1", "2026-09-02T00:00:00Z", "2026-09-13T00:00:00Z", 3, "active"),
        ("s-ARCH", "u2", "2026-08-01T00:00:00Z", "2026-08-15T00:00:00Z", 10, "archived"),
        ("s-MULTI", "u2", "2026-09-10T00:00:00Z", "2026-09-14T12:00:00Z", 8, "active"),
    ]
    conn.executemany(
        "INSERT INTO sessions(id,user_id,created_at,last_active,message_count,status) "
        "VALUES(?,?,?,?,?,?)", sessions,
    )
    # 注入 references — s-AAPL 拿 AAPL,s-NVDA 拿 NVDA,s-ARCH 拿 TSLA,s-MULTI 拿 AAPL+NVDA
    refs = [
        ("s-AAPL", "AAPL price?", "get_quote", {"symbol": "AAPL"}, {"price": 200}, "quote"),
        ("s-NVDA", "NVDA news", "get_quote", {"symbol": "NVDA"}, {"price": 900}, "quote"),
        ("s-ARCH", "TSLA 历史", "get_quote", {"symbol": "TSLA"}, {"price": 250}, "quote"),
        ("s-MULTI", "compare", "compare", {"symbols": ["AAPL", "NVDA"]}, {"diff": 5}, "compare"),
    ]
    for sid, nq, tn, args, result, tags in refs:
        conn.execute(
            "INSERT INTO agent_references(session_id,nl_query,tool_name,tool_args,tool_result,tags,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (sid, nq, tn, json.dumps(args), json.dumps(result), tags, "2026-09-14T00:00:00Z"),
        )
    conn.commit()
    conn.close()

    # 用一个最小 adapter 暴露 _connect()
    class _Adapter:
        def __init__(self, p):
            self._db_path = p
        def _connect(self):
            c = sqlite3.connect(str(self._db_path), check_same_thread=False)
            c.row_factory = sqlite3.Row
            return c

    adapter = _Adapter(db_path)
    from tradingagents.agent_harness.core.session_store import SessionStore
    store = SessionStore(adapter)
    return store


def test_find_sessions_by_symbol_match_symbol_key(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.find_sessions_by_symbol("AAPL")
    ids = {s.id for s in rows}
    assert ids == {"s-AAPL", "s-MULTI"}


def test_find_sessions_by_symbol_match_symbols_list(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.find_sessions_by_symbol("NVDA")
    ids = {s.id for s in rows}
    assert ids == {"s-NVDA", "s-MULTI"}


def test_find_sessions_by_symbol_excludes_archived(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.find_sessions_by_symbol("TSLA")
    # s-ARCH 命中 TSLA 但默认 exclude_archived
    assert {s.id for s in rows} == set()
    rows_with_archived = q.find_sessions_by_symbol("TSLA", include_archived=True)
    assert {s.id for s in rows_with_archived} == {"s-ARCH"}


def test_find_sessions_by_symbol_empty_input(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    assert q.find_sessions_by_symbol("") == []
    assert q.find_sessions_by_symbol("GHOST-XYZ-999") == []


def test_search_sessions_by_text_match_nl_query(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.search_sessions_by_text("price")
    ids = {s.id for s in rows}
    # s-AAPL "AAPL price?" / s-NVDA "NVDA news"(news 不含 price) — 仅 s-AAPL
    assert "s-AAPL" in ids


def test_search_sessions_by_text_match_tool_result(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.search_sessions_by_text("compare")
    ids = {s.id for s in rows}
    # tool_result {"diff": 5} 没有 "compare",nl_query "compare" 命中 s-MULTI
    assert "s-MULTI" in ids


def test_search_sessions_by_text_excludes_archived(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    rows = q.search_sessions_by_text("TSLA")
    # s-ARCH 命中 "TSLA 历史"
    assert {s.id for s in rows} == set()
    assert "s-ARCH" in {s.id for s in q.search_sessions_by_text("TSLA", include_archived=True)}


def test_get_session_summary_combines_view(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    summary = q.get_session_summary("s-AAPL")
    assert summary is not None
    assert summary.session.id == "s-AAPL"
    assert summary.reference_count == 1
    assert summary.recent_references[0]["nl_query"] == "AAPL price?"
    assert summary.last_event_at is None  # event_log not wired


def test_get_session_summary_with_event_log(store_with_refs, tmp_path):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    from tradingagents.agent_harness.memory.event_log import EventLog
    el = EventLog(db_path=tmp_path / "events.sqlite")
    el.append("s-AAPL", "user/message", {"role": "user", "content": "hi"})
    q = SessionQuery(store_with_refs, event_log=el)
    summary = q.get_session_summary("s-AAPL")
    assert summary is not None
    assert summary.last_event_at is not None


def test_get_session_summary_missing_returns_none(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    assert q.get_session_summary("nonexistent") is None


def test_aggregate_stats(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    stats = q.aggregate_stats()
    assert stats.total == 4
    assert stats.active == 3
    assert stats.archived == 1
    assert stats.by_user == {"u1": 2, "u2": 2}
    # top_symbols 至少包含 AAPL/NVDA/TSLA
    sym_dict = dict(stats.top_symbols)
    assert sym_dict.get("AAPL", 0) >= 2  # s-AAPL + s-MULTI
    assert sym_dict.get("NVDA", 0) >= 2
    assert sym_dict.get("TSLA", 0) >= 1


def test_aggregate_stats_filtered_by_user(store_with_refs):
    from tradingagents.agent_harness.core.session_query import SessionQuery
    q = SessionQuery(store_with_refs)
    stats = q.aggregate_stats(user_id="u1")
    assert stats.total == 2
    assert all(uid == "u1" for uid in stats.by_user)


def test_aggregate_stats_safe_when_table_missing(tmp_path):
    """L3 表缺失时不抛错,返空 stats。"""
    db = tmp_path / "no_refs.sqlite"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT,
            last_active TEXT, message_count INTEGER, status TEXT
        );
    """)
    conn.commit()
    conn.close()

    class _A:
        def __init__(self, p):
            self._db_path = p
        def _connect(self):
            c = sqlite3.connect(str(self._db_path), check_same_thread=False)
            c.row_factory = sqlite3.Row
            return c

    from tradingagents.agent_harness.core.session_store import SessionStore
    from tradingagents.agent_harness.core.session_query import SessionQuery

    store = SessionStore(_A(db))
    q = SessionQuery(store)
    stats = q.aggregate_stats()
    assert stats.total == 0
    assert stats.top_symbols == []
    assert q.find_sessions_by_symbol("X") == []  # OperationalError 安全降级
