"""Persistence repositories for the personal investor web platform."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

_UNSET = object()

from cli.utils import is_valid_ticker_input, normalize_ticker_symbol

from .storage import SQLiteStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row is not None else None


class WatchlistRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.get_default()

    def get_default(self) -> dict[str, Any]:
        now = _now()
        with self.store.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watchlists(id,name,version,created_at,updated_at) VALUES ('default','我的关注',1,?,?)",
                (now, now),
            )
            row = conn.execute("SELECT * FROM watchlists WHERE id='default'").fetchone()
        return _row(row)

    def get(self, watchlist_id: str = "default") -> dict[str, Any]:
        if watchlist_id == "default":
            return self.get_default()
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
        if row is None:
            raise KeyError(watchlist_id)
        return _row(row)

    def list_items(self, watchlist_id: str = "default") -> list[dict[str, Any]]:
        self.get_default() if watchlist_id == "default" else None
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM watchlist_items WHERE watchlist_id=? ORDER BY position,id", (watchlist_id,)).fetchall()
        return [_row(row) for row in rows]

    def add_item(self, symbol: str, *, asset_type: str, note: str | None = None, watchlist_id: str = "default") -> dict[str, Any]:
        if asset_type not in {"stock", "crypto"}:
            raise ValueError("invalid asset_type")
        canonical = normalize_ticker_symbol(symbol)
        if not canonical or not is_valid_ticker_input(str(symbol)):
            raise ValueError("invalid symbol")
        now = _now()
        with self.store.connection() as conn:
            self._ensure_watchlist(conn, watchlist_id, now)
            position = conn.execute("SELECT COALESCE(MAX(position), -1)+1 FROM watchlist_items WHERE watchlist_id=?", (watchlist_id,)).fetchone()[0]
            try:
                conn.execute("INSERT INTO watchlist_items(id,watchlist_id,symbol,asset_type,note,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (f"item-{uuid.uuid4().hex}", watchlist_id, canonical, asset_type, note, position, now, now))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError("duplicate symbol") from exc
                raise
            row = conn.execute("SELECT * FROM watchlist_items WHERE watchlist_id=? AND symbol=?", (watchlist_id, canonical)).fetchone()
            conn.execute("UPDATE watchlists SET version=version+1,updated_at=? WHERE id=?", (now, watchlist_id))
        return _row(row)

    def update_item(self, item_id: str, *, expected_version: int, note: str | None | object = _UNSET, symbol: str | None = None, asset_type: str | None = None, position: int | None = None, order: int | None = None) -> dict[str, Any]:
        now = _now()
        with self.store.connection() as conn:
            item = conn.execute("SELECT * FROM watchlist_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise KeyError(item_id)
            watchlist_id = item["watchlist_id"]
            if asset_type is not None and asset_type not in {"stock", "crypto"}:
                raise ValueError("invalid asset_type")
            if symbol is not None and not is_valid_ticker_input(str(symbol)):
                raise ValueError("invalid symbol")
            canonical = normalize_ticker_symbol(symbol) if symbol is not None else item["symbol"]
            if not canonical:
                raise ValueError("invalid symbol")
            if position is not None or order is not None:
                target_position = position if position is not None else order
                if isinstance(target_position, bool) or not isinstance(target_position, int) or target_position < 0:
                    raise ValueError("invalid position")
            if conn.execute("UPDATE watchlists SET version=version+1,updated_at=? WHERE id=? AND version=?", (now, watchlist_id, expected_version)).rowcount != 1:
                raise RuntimeError("version conflict")
            note_value = item["note"] if note is _UNSET else note
            try:
                target_position = position if position is not None else order
                if target_position is None:
                    target_position = item["position"]
                conn.execute("UPDATE watchlist_items SET symbol=?,asset_type=?,note=?,position=?,updated_at=? WHERE id=? AND watchlist_id=?", (canonical, asset_type or item["asset_type"], note_value, target_position, now, item_id, watchlist_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate symbol") from exc
            return _row(conn.execute("SELECT * FROM watchlist_items WHERE id=?", (item_id,)).fetchone())

    def delete_item(self, item_id: str, *, expected_version: int) -> None:
        now = _now()
        with self.store.connection() as conn:
            item = conn.execute("SELECT watchlist_id,position FROM watchlist_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise KeyError(item_id)
            watchlist_id = item[0]
            if conn.execute("UPDATE watchlists SET version=version+1,updated_at=? WHERE id=? AND version=?", (now, watchlist_id, expected_version)).rowcount != 1:
                raise RuntimeError("version conflict")
            position = item[1]
            conn.execute("DELETE FROM watchlist_items WHERE id=? AND watchlist_id=?", (item_id, watchlist_id))
            conn.execute("UPDATE watchlist_items SET position=position-1 WHERE watchlist_id=? AND position > ?", (watchlist_id, position))

    def reorder(self, item_ids: list[str], *, expected_version: int, watchlist_id: str = "default") -> dict[str, Any]:
        now = _now()
        with self.store.connection() as conn:
            row = conn.execute("SELECT version FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
            if row is None:
                raise KeyError(watchlist_id)
            if conn.execute("UPDATE watchlists SET version=version+1,updated_at=? WHERE id=? AND version=?", (now, watchlist_id, expected_version)).rowcount != 1:
                raise RuntimeError("version conflict")
            existing = {r[0] for r in conn.execute("SELECT id FROM watchlist_items WHERE watchlist_id=?", (watchlist_id,))}
            if set(item_ids) != existing or len(item_ids) != len(existing):
                raise ValueError("invalid order")
            for position, item_id in enumerate(item_ids):
                conn.execute("UPDATE watchlist_items SET position=?,updated_at=? WHERE id=? AND watchlist_id=?", (position, now, item_id, watchlist_id))
            return _row(conn.execute("SELECT * FROM watchlists WHERE id=?", (watchlist_id,)).fetchone())

    @staticmethod
    def _ensure_watchlist(conn, watchlist_id: str, now: str) -> None:
        conn.execute("INSERT OR IGNORE INTO watchlists(id,name,version,created_at,updated_at) VALUES (?,?,1,?,?)", (watchlist_id, "我的关注", now, now))


class QuoteRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def upsert_quote(self, quote: dict[str, Any]) -> None:
        fields = ("symbol", "asset_type", "price", "previous_close", "change", "change_percent", "currency", "as_of", "fetched_at", "freshness", "source", "payload_json", "open", "high", "low", "volume", "market_status", "exchange", "raw_summary", "cache_status")
        data = dict(quote)
        data.setdefault("fetched_at", _now())
        data["payload_json"] = json.dumps(data.get("payload") or {}, ensure_ascii=False)
        with self.store.connection() as conn:
            symbol = normalize_ticker_symbol(str(data.get("symbol") or ""))
            if not symbol or not is_valid_ticker_input(str(data.get("symbol") or "")):
                raise ValueError("invalid symbol")
            freshness = data.get("freshness")
            if freshness not in {"fresh", "delayed", "stale", "unavailable"}:
                raise ValueError("invalid freshness")
            if data.get("asset_type") not in {"stock", "crypto"}:
                raise ValueError("invalid asset_type")
            data["symbol"] = symbol
            conn.execute(f"INSERT INTO market_quotes ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(symbol,asset_type) DO UPDATE SET " + ','.join(f"{f}=excluded.{f}" for f in fields[2:]), tuple(data.get(f) for f in fields))

    def get_latest(self, symbol: str, asset_type: str = "stock") -> dict[str, Any] | None:
        canonical = normalize_ticker_symbol(symbol)
        if not canonical or not is_valid_ticker_input(str(symbol)):
            raise ValueError("invalid symbol")
        if asset_type not in {"stock", "crypto"}:
            raise ValueError("invalid asset_type")
        with self.store.connection() as conn:
            return _row(conn.execute("SELECT * FROM market_quotes WHERE symbol=? AND asset_type=?", (canonical, asset_type)).fetchone())

    def upsert_candles(self, candles: list[dict[str, Any]]) -> None:
        with self.store.connection() as conn:
            for candle in candles:
                symbol = normalize_ticker_symbol(str(candle.get("symbol") or ""))
                if not symbol or not is_valid_ticker_input(str(candle.get("symbol") or "")): raise ValueError("invalid symbol")
                if not candle.get("interval"): raise ValueError("invalid interval")
                if candle["interval"] not in {"1d", "1h", "15m"}: raise ValueError("invalid interval")
                try:
                    from datetime import datetime
                    datetime.fromisoformat(str(candle.get("timestamp")).replace("Z", "+00:00"))
                except (TypeError, ValueError): raise ValueError("invalid timestamp") from None
                for key in ("open", "high", "low", "close", "volume"):
                    if candle.get(key) is not None:
                        try: float(candle[key])
                        except (TypeError, ValueError): raise ValueError(f"invalid {key}") from None
                conn.execute("INSERT OR REPLACE INTO market_candles(symbol,interval,timestamp,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?,?)", (symbol, candle["interval"], candle["timestamp"], *(candle.get(k) for k in ("open","high","low","close","volume")), candle.get("source")))

    def get_candles(self, symbol: str, interval: str, *, asset_type: str = "stock") -> list[dict[str, Any]]:
        canonical = normalize_ticker_symbol(symbol)
        if not canonical: raise ValueError("invalid symbol")
        if not is_valid_ticker_input(str(symbol)): raise ValueError("invalid symbol")
        if asset_type not in {"stock", "crypto"}: raise ValueError("invalid asset_type")
        if interval not in {"1d", "1h", "15m"}: raise ValueError("invalid interval")
        with self.store.connection() as conn:
            return [_row(row) for row in conn.execute("SELECT * FROM market_candles WHERE symbol=? AND interval=? ORDER BY timestamp", (canonical, interval)).fetchall()]


class SnapshotRepository:
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    def save_manifest(self, run_id: str, manifest: dict[str, Any], *, status: str = "recording") -> None:
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        with self.store.connection() as conn:
            existing = conn.execute("SELECT manifest_json,status FROM analysis_data_snapshots WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("immutable snapshot manifest")
                if existing[1] == "finalized" and status != "finalized":
                    raise ValueError("immutable snapshot manifest")
                if status == "finalized" and existing[1] != "finalized":
                    conn.execute("UPDATE analysis_data_snapshots SET status='finalized',completed_at=? WHERE run_id=?", (_now(), run_id))
                return
            if status == "finalized":
                raise ValueError("immutable snapshot manifest")
            conn.execute("INSERT INTO analysis_data_snapshots(run_id,manifest_json,manifest_hash,status,created_at) VALUES (?,?,?,?,?)", (run_id, encoded, manifest.get("manifest_hash"), status, _now()))
    def get_manifest(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute("SELECT manifest_json FROM analysis_data_snapshots WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def get_record(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM analysis_data_snapshots WHERE run_id=?", (run_id,)).fetchone()
        return _row(row)
    def finalize(self, run_id: str) -> bool:
        with self.store.connection() as conn:
            row = conn.execute("UPDATE analysis_data_snapshots SET status='finalized',completed_at=? WHERE run_id=? AND status='recording'", (_now(), run_id))
            if row.rowcount == 0:
                existing = conn.execute("SELECT status FROM analysis_data_snapshots WHERE run_id=?", (run_id,)).fetchone()
                return bool(existing and existing[0] == "finalized")
            return True


class SettingsRepository:
    ALLOWED = frozenset({"quote_ttl_seconds", "quote_strategy_id", "quote_provider_chain", "output_language"})
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    def set(self, key: str, value: Any, *, source: str = "sqlite") -> None:
        if key not in self.ALLOWED: return
        with self.store.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value,source,updated_at) VALUES (?,?,?,?)", (key, str(value), source, _now()))
    def get(self, key: str) -> dict[str, str] | None:
        if key not in self.ALLOWED: return None
        try:
            with self.store.connection() as conn:
                row = conn.execute("SELECT value,source FROM settings WHERE key=?", (key,)).fetchone()
        except Exception:
            return None
        return {"value": row[0], "source": row[1]} if row else None
    def all(self) -> dict[str, dict[str, str]]:
        placeholders = ",".join("?" for _ in self.ALLOWED)
        with self.store.connection() as conn:
            rows = conn.execute(f"SELECT key,value,source FROM settings WHERE key IN ({placeholders})", tuple(self.ALLOWED)).fetchall()
        return {row["key"]: {"value": row["value"], "source": row["source"]} for row in rows}


class AnalysisRunRepository:
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    def upsert(self, record: dict[str, Any]) -> None:
        fields = ("run_id", "request_json", "status", "phase", "current_agent", "progress", "queued_at", "started_at", "finished_at", "signal", "report_id", "error_code", "error_message", "terminal_expires_at", "effective_quote_strategy_id", "effective_quote_provider_chain", "data_snapshot_id", "data_status", "reproducibility")
        encoded = dict(record)
        if isinstance(encoded.get("effective_quote_provider_chain"), list):
            encoded["effective_quote_provider_chain"] = json.dumps(encoded["effective_quote_provider_chain"], ensure_ascii=False)
        with self.store.connection() as conn:
            conn.execute(f"INSERT INTO web_runs ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(run_id) DO UPDATE SET " + ','.join(f"{f}=excluded.{f}" for f in fields[1:]), tuple(encoded.get(f) for f in fields))
    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            return _row(conn.execute("SELECT * FROM web_runs WHERE run_id=?", (run_id,)).fetchone())
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM web_runs" + (" WHERE status=?" if status else ""), (status,) if status else ()).fetchall()
        return [_row(row) for row in rows]


class ReportRepository:
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    @staticmethod
    def is_gate_ready(path) -> bool:
        from pathlib import Path
        import json
        root = Path(path)
        if not (root / "complete_report.md").is_file() or not (root / "COMMITTED").is_file():
            return False
        try:
            metadata = json.loads((root / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return metadata.get("status") == "completed"
    def iter_ready(self, root):
        from pathlib import Path
        return iter(sorted(path.parent for path in Path(root).rglob("complete_report.md") if self.is_gate_ready(path.parent)))
