"""Q2 / P2-5 — Session 列表 + 跨 session 查询(roadmap §12.6 P1-M1).

``SessionStore`` handles single-session CRUD; this module wraps it
with the *cross-session* queries that the front-end / observability
tools actually need:

  - ``find_sessions_by_symbol(symbol, limit)`` — which sessions touched
    this ticker? Uses the ``agent_references`` L3 table (tool_args
    LIKE) and joins against ``sessions`` to surface active / archived
    status, last_active, etc.
  - ``search_sessions_by_text(text, limit)`` — cross-session full-text
    search over L3 reference ``nl_query`` + tool_result fields.
  - ``get_session_summary(session_id)`` — combined view: Session
    row + recent references + (optional) recent turn records via
    ``EventLog`` if wired.
  - ``aggregate_stats(user_id=None)`` — total / active / archived
    counts plus symbol frequency histogram.

Why a wrapper class (not methods on SessionStore)?
  - SessionStore is intentionally minimal — it shouldn't know about
    cross-session search patterns or the L3 references schema.
  - Tests can mock the underlying SQLite connection without dragging
    in SessionStore.
  - Adding new query shapes (e.g. by tag, by time range) doesn't
    bloat the core CRUD class.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .session_store import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    Session,
    SessionStore,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """Bundled view of a single session for list / detail UIs."""

    session: Session
    recent_references: list[dict[str, Any]] = field(default_factory=list)
    reference_count: int = 0
    last_event_at: str | None = None  # from event_log if available

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "reference_count": self.reference_count,
            "recent_references": self.recent_references,
            "last_event_at": self.last_event_at,
        }


@dataclass
class AggregateStats:
    total: int = 0
    active: int = 0
    archived: int = 0
    by_user: dict[str, int] = field(default_factory=dict)
    top_symbols: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "active": self.active,
            "archived": self.archived,
            "by_user": self.by_user,
            "top_symbols": [
                {"symbol": sym, "count": cnt} for sym, cnt in self.top_symbols
            ],
        }


def _safe_ticker_component(s: str) -> str:
    """Mirror of ``agents/general/memory.safe_ticker_component`` for SQL LIKE."""
    return "".join(c if c.isalnum() else "_" for c in s)


class SessionQuery:
    """Cross-session queries on top of ``SessionStore`` + L3 references."""

    def __init__(
        self,
        store: SessionStore,
        *,
        event_log: Any | None = None,
    ) -> None:
        """
        Args:
            store: a ``SessionStore`` (or anything exposing ``_connect()``
                on its underlying SQLite handle).
            event_log: optional ``EventLog`` instance — when wired,
                ``get_session_summary`` includes ``last_event_at`` from
                the surface chain.
        """
        self._store = store
        self._event_log = event_log

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return self._store._store._connect()

    # ------------------------------------------------------------------
    # Cross-session queries
    # ------------------------------------------------------------------
    def find_sessions_by_symbol(
        self,
        symbol: str,
        *,
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[Session]:
        """Find sessions whose ``agent_references.tool_args`` mentions ``symbol``.

        A session "touched" a symbol if any tool call stored a reference
        whose ``tool_args`` JSON contains the symbol string. We use SQL
        LIKE on the JSON text (cheap, no JSON1 extension needed).
        """
        if not symbol:
            return []
        like = f"%\"symbol\": \"%{symbol}%"  # "symbol": "<sym>
        # also match nested shapes: "symbols": ["AAPL", ...]
        like_alt = f"%\"symbols\":%{symbol}%"
        sql = """
            SELECT DISTINCT s.* FROM sessions s
            JOIN agent_references r ON r.session_id = s.id
            WHERE (r.tool_args LIKE ? OR r.tool_args LIKE ?)
        """
        params: list[Any] = [like, like_alt]
        if not include_archived:
            sql += " AND s.status = ?"
            params.append(SESSION_STATUS_ACTIVE)
        sql += " ORDER BY s.last_active DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            LOGGER.debug("agent_references query failed: %s", e)
            return []
        return [Session.from_row(r) for r in rows]

    def search_sessions_by_text(
        self,
        text: str,
        *,
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[Session]:
        """Cross-session keyword search over L3 ``nl_query`` + ``tool_result``."""
        if not text:
            return []
        like = f"%{text}%"
        sql = """
            SELECT DISTINCT s.* FROM sessions s
            JOIN agent_references r ON r.session_id = s.id
            WHERE (r.nl_query LIKE ? OR r.tool_result LIKE ?)
        """
        params: list[Any] = [like, like]
        if not include_archived:
            sql += " AND s.status = ?"
            params.append(SESSION_STATUS_ACTIVE)
        sql += " ORDER BY s.last_active DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            LOGGER.debug("search_sessions_by_text failed: %s", e)
            return []
        return [Session.from_row(r) for r in rows]

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        """Combined view: Session + recent references + (optional) last event."""
        sess = self._store.get(session_id)
        if sess is None:
            return None
        try:
            with self._connect() as conn:
                ref_rows = conn.execute(
                    "SELECT id, session_id, nl_query, tool_name, tool_args, "
                    "tool_result, tags, created_at FROM agent_references "
                    "WHERE session_id = ? ORDER BY created_at DESC LIMIT 10",
                    (session_id,),
                ).fetchall()
                ref_count_row = conn.execute(
                    "SELECT COUNT(*) as n FROM agent_references WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.OperationalError as e:
            LOGGER.debug("summary query failed: %s", e)
            ref_rows, ref_count_row = [], None
        refs = [_ref_row_to_dict(r) for r in ref_rows]
        ref_count = int(ref_count_row["n"]) if ref_count_row else 0
        last_event_at: str | None = None
        if self._event_log is not None:
            try:
                head = self._event_log.head_seq(session_id)
                if head:
                    ev = self._event_log.get(session_id, head)
                    if ev is not None:
                        last_event_at = ev.time_iso
            except Exception:
                LOGGER.debug("event_log lookup failed", exc_info=True)
        return SessionSummary(
            session=sess,
            recent_references=refs,
            reference_count=ref_count,
            last_event_at=last_event_at,
        )

    def aggregate_stats(
        self,
        *,
        user_id: str | None = None,
        symbol_top_n: int = 10,
    ) -> AggregateStats:
        """Total / active / archived + per-user counts + top symbols."""
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT status, COUNT(*) as n FROM sessions {where} "
                    "GROUP BY status", params,
                ).fetchall()
                by_user_rows = conn.execute(
                    f"SELECT user_id, COUNT(*) as n FROM sessions {where} "
                    "GROUP BY user_id ORDER BY n DESC", params,
                ).fetchall()
        except sqlite3.OperationalError as e:
            LOGGER.debug("aggregate stats query failed: %s", e)
            return AggregateStats()
        total = sum(r["n"] for r in row)
        active = sum(r["n"] for r in row if r["status"] == SESSION_STATUS_ACTIVE)
        archived = sum(r["n"] for r in row if r["status"] == SESSION_STATUS_ARCHIVED)
        by_user = {r["user_id"]: r["n"] for r in by_user_rows}
        top_symbols = self._top_symbols(symbol_top_n)
        return AggregateStats(
            total=total, active=active, archived=archived,
            by_user=by_user, top_symbols=top_symbols,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _top_symbols(self, limit: int) -> list[tuple[str, int]]:
        """Naive symbol-frequency histogram across L3 tool_args JSON.

        Matches the keys ``symbol`` (string) and ``symbols`` (list) and
        pulls every A-share-shaped (4-6 uppercase + .SS/.SZ) value.
        """
        pattern_a = re_compile(r"\b([0-9]{6}\.(SS|SZ))\b")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT tool_args FROM agent_references "
                    "WHERE tool_args LIKE '%\"symbol%' "
                    "OR tool_args LIKE '%\"symbols%' LIMIT 5000",
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        counts: dict[str, int] = {}
        for r in rows:
            try:
                payload = json.loads(r["tool_args"] or "{}")
            except (ValueError, TypeError):
                continue
            for sym in _extract_symbols(payload):
                counts[sym] = counts.get(sym, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]


# ---------------------------------------------------------------------------
# Helpers (module-level so tests can target them directly)
# ---------------------------------------------------------------------------

def _ref_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "nl_query": row["nl_query"],
        "tool_name": row["tool_name"],
        "tool_args": _json_or_none(row["tool_args"]),
        "tool_result": _json_or_none(row["tool_result"]),
        "tags": row["tags"].split() if row["tags"] else [],
        "created_at": row["created_at"],
    }


def _json_or_none(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _extract_symbols(payload: Any) -> Iterable[str]:
    if isinstance(payload, dict):
        sym = payload.get("symbol")
        if isinstance(sym, str):
            yield sym
        syms = payload.get("symbols")
        if isinstance(syms, list):
            for s in syms:
                if isinstance(s, str):
                    yield s
    elif isinstance(payload, list):
        for item in payload:
            yield from _extract_symbols(item)


# Lazy compile to keep top-level import light.
import re as _re
_re_compile = _re.compile  # expose for tests
import re  # noqa: E402
re_compile = re.compile


__all__ = [
    "SessionQuery",
    "SessionSummary",
    "AggregateStats",
]
