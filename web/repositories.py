"""Persistence repositories for the personal investor web platform."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cli.utils import is_valid_ticker_input, normalize_ticker_symbol

from .storage import SQLiteStore

_UNSET = object()


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
                if not symbol or not is_valid_ticker_input(str(candle.get("symbol") or "")):
                    raise ValueError("invalid symbol")
                if not candle.get("interval"):
                    raise ValueError("invalid interval")
                if candle["interval"] not in {"1d", "1h", "15m"}:
                    raise ValueError("invalid interval")
                try:
                    datetime.fromisoformat(str(candle.get("timestamp")).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    raise ValueError("invalid timestamp") from None
                for key in ("open", "high", "low", "close", "volume"):
                    if candle.get(key) is not None:
                        try:
                            float(candle[key])
                        except (TypeError, ValueError):
                            raise ValueError(f"invalid {key}") from None
                conn.execute("INSERT OR REPLACE INTO market_candles(symbol,interval,timestamp,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?,?)", (symbol, candle["interval"], candle["timestamp"], *(candle.get(k) for k in ("open","high","low","close","volume")), candle.get("source")))

    def get_candles(self, symbol: str, interval: str, *, asset_type: str = "stock") -> list[dict[str, Any]]:
        canonical = normalize_ticker_symbol(symbol)
        if not canonical:
            raise ValueError("invalid symbol")
        if not is_valid_ticker_input(str(symbol)):
            raise ValueError("invalid symbol")
        if asset_type not in {"stock", "crypto"}:
            raise ValueError("invalid asset_type")
        if interval not in {"1d", "1h", "15m"}:
            raise ValueError("invalid interval")
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
    ALLOWED = frozenset({
        "quote_ttl_seconds",
        "quote_strategy_id",
        "quote_provider_chain",
        "output_language",
        "run_timeout_seconds",
        "run_heartbeat_interval_seconds",
        "run_heartbeat_timeout_seconds",
    })
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    def set(self, key: str, value: Any, *, source: str = "sqlite") -> None:
        if key not in self.ALLOWED:
            return
        with self.store.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key,value,source,updated_at) VALUES (?,?,?,?)", (key, str(value), source, _now()))
    def get(self, key: str) -> dict[str, str] | None:
        if key not in self.ALLOWED:
            return None
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
        fields = ("run_id", "request_json", "status", "phase", "current_agent", "progress", "queued_at", "started_at", "finished_at", "signal", "report_id", "error_code", "error_message", "terminal_expires_at", "effective_quote_strategy_id", "effective_quote_provider_chain", "data_snapshot_id", "data_status", "reproducibility", "last_heartbeat_at", "timeout_at", "terminal_reason", "run_timeout_seconds", "run_heartbeat_interval_seconds", "run_heartbeat_timeout_seconds")
        encoded = dict(record)
        if encoded.get("terminal_reason") is None and encoded.get("error_code") is not None:
            encoded["terminal_reason"] = encoded["error_code"]
        elif encoded.get("terminal_reason") is not None:
            encoded["error_code"] = encoded["terminal_reason"]
        if isinstance(encoded.get("effective_quote_provider_chain"), list):
            encoded["effective_quote_provider_chain"] = json.dumps(encoded["effective_quote_provider_chain"], ensure_ascii=False)
        with self.store.connection() as conn:
            conn.execute(f"INSERT INTO web_runs ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(run_id) DO UPDATE SET " + ','.join(f"{f}=excluded.{f}" for f in fields[1:]), tuple(encoded.get(f) for f in fields))
    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM web_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode(row)
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM web_runs" + (" WHERE status=?" if status else ""), (status,) if status else ()).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row) -> dict[str, Any] | None:
        value = _row(row)
        if value is None:
            return None
        for key in ("effective_quote_provider_chain",):
            encoded = value.get(key)
            if not encoded:
                value[key] = []
            elif isinstance(encoded, str):
                try:
                    decoded = json.loads(encoded)
                except (TypeError, ValueError):
                    decoded = []
                value[key] = decoded if isinstance(decoded, list) else []
        if value.get("terminal_reason") is None and value.get("error_code") is not None:
            value["terminal_reason"] = value["error_code"]
        elif value.get("terminal_reason") is not None:
            value["error_code"] = value["terminal_reason"]
        return value


class ReportIndexRepository:
    """SQLite read index for report metadata with a durable outbox overlay."""

    ROOT_NAMES = frozenset({"web_reports", "results_reports", "cwd_reports"})
    STATUSES = frozenset(
        {"completed", "failed", "cancelled", "interrupted", "timed_out"}
    )
    SOURCES = frozenset({"web", "legacy"})
    INDEX_STATUSES = frozenset({"indexed", "pending", "error"})
    PATH_STATES = frozenset({"valid", "missing", "unsafe"})
    ASSET_TYPES = frozenset({"stock", "crypto"})
    SORTS = frozenset({"generated_at_desc", "generated_at_asc"})
    _FIELDS = (
        "report_id",
        "run_id",
        "ticker",
        "asset_type",
        "analysis_date",
        "generated_at",
        "status",
        "rating",
        "signal",
        "output_language",
        "summary_status",
        "decision_preview",
        "data_snapshot_id",
        "provider",
        "quick_model",
        "deep_model",
        "analysts_json",
        "research_depth",
        "data_status",
        "reproducibility",
        "quote_strategy_id",
        "effective_quote_provider_chain",
        "root_name",
        "relative_path",
        "source",
        "index_status",
        "path_state",
        "updated_at",
    )

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def upsert(self, metadata: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(metadata)
        with self.store.connection() as conn:
            self._upsert_connection(conn, normalized)
        return self._decode(normalized)

    def enqueue(self, metadata: dict[str, Any], error: Any) -> dict[str, Any]:
        normalized = self._normalize(
            {
                **metadata,
                "index_status": "pending",
                "updated_at": _now(),
            }
        )
        payload = self._payload(normalized)
        message = " ".join(str(error or "index update failed").split())[:512]
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO report_index_outbox(
                    report_id,root_name,relative_path,payload_json,attempts,last_error,updated_at
                ) VALUES (?,?,?,?,0,?,?)
                ON CONFLICT(report_id) DO UPDATE SET
                    root_name=excluded.root_name,
                    relative_path=excluded.relative_path,
                    payload_json=excluded.payload_json,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized["report_id"],
                    normalized["root_name"],
                    normalized["relative_path"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    message,
                    normalized["updated_at"],
                ),
            )
        return self._decode(normalized)

    def retry_outbox(self, limit: int = 50) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("invalid outbox limit")
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT report_id,payload_json FROM report_index_outbox "
                "ORDER BY updated_at,report_id LIMIT ?",
                (limit,),
            ).fetchall()
        completed = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                self.upsert(payload)
                with self.store.connection() as conn:
                    conn.execute(
                        "DELETE FROM report_index_outbox WHERE report_id=?",
                        (row["report_id"],),
                    )
                completed += 1
            except Exception as exc:
                message = " ".join(str(exc).split())[:512]
                with self.store.connection() as conn:
                    conn.execute(
                        "UPDATE report_index_outbox SET attempts=attempts+1,last_error=?,updated_at=? "
                        "WHERE report_id=?",
                        (message, _now(), row["report_id"]),
                    )
        return completed

    def rebuild(
        self,
        records: list[dict[str, Any]],
        *,
        root_names: set[str] | frozenset[str] | None = None,
    ) -> int:
        normalized = [self._normalize(record) for record in records]
        roots = set(root_names or (record["root_name"] for record in normalized))
        if not roots.issubset(self.ROOT_NAMES):
            raise ValueError("invalid report root")
        with self.store.connection() as conn:
            if roots:
                placeholders = ",".join("?" for _ in roots)
                conn.execute(
                    f"UPDATE reports SET path_state='missing',updated_at=? "
                    f"WHERE root_name IN ({placeholders})",
                    (_now(), *sorted(roots)),
                )
            for record in normalized:
                self._upsert_connection(conn, record)
        return len(normalized)

    def get(self, report_id: str) -> dict[str, Any] | None:
        where = "combined.report_id=? AND combined.path_state='valid'"
        with self.store.connection() as conn:
            row = conn.execute(
                f"{self._combined_cte()} SELECT * FROM combined WHERE {where} LIMIT 1",
                (report_id,),
            ).fetchone()
        return self._decode(row) if row else None

    def list_legacy_shape(self) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            rows = conn.execute(
                f"{self._combined_cte()} SELECT * FROM combined "
                "WHERE path_state='valid' "
                "ORDER BY generated_at IS NULL ASC, generated_at DESC, report_id ASC"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def search(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
        ticker: str | None = None,
        status: str | None = None,
        asset_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "generated_at_desc",
    ) -> dict[str, Any]:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100000:
            raise ValueError("invalid page")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("invalid page size")
        if status is not None and status not in self.STATUSES:
            raise ValueError("invalid status")
        if asset_type is not None and asset_type not in self.ASSET_TYPES:
            raise ValueError("invalid asset type")
        if sort not in self.SORTS:
            raise ValueError("invalid sort")
        filters = ["path_state='valid'"]
        parameters: list[Any] = []
        exact_ticker = (ticker or "").strip()
        search_query = (query or "").strip()
        if exact_ticker:
            filters.append("ticker = ? COLLATE NOCASE")
            parameters.append(exact_ticker)
        elif search_query:
            filters.append(
                "(LOWER(COALESCE(ticker,'')) LIKE ? OR "
                "LOWER(COALESCE(decision_preview,'')) LIKE ?)"
            )
            pattern = f"%{search_query.lower()}%"
            parameters.extend((pattern, pattern))
        if status:
            filters.append("status=?")
            parameters.append(status)
        if asset_type:
            filters.append("asset_type=?")
            parameters.append(asset_type)
        if date_from:
            filters.append("analysis_date>=?")
            parameters.append(str(date_from))
        if date_to:
            filters.append("analysis_date<=?")
            parameters.append(str(date_to))
        where = " AND ".join(filters)
        direction = "DESC" if sort == "generated_at_desc" else "ASC"
        order = (
            f"generated_at IS NULL ASC, generated_at {direction}, report_id ASC"
        )
        offset = (page - 1) * page_size
        cte = self._combined_cte()
        with self.store.connection() as conn:
            total = int(
                conn.execute(
                    f"{cte} SELECT COUNT(*) FROM combined WHERE {where}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"{cte} SELECT * FROM combined WHERE {where} ORDER BY {order} "
                "LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()
        items = [self._decode(row) for row in rows]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_next": offset + len(items) < total,
        }

    @classmethod
    def _combined_cte(cls) -> str:
        outbox_fields = []
        for field in cls._FIELDS:
            if field == "report_id":
                outbox_fields.append("report_id")
            elif field in {"root_name", "relative_path"}:
                outbox_fields.append(field)
            else:
                outbox_fields.append(
                    f"json_extract(payload_json, '$.{field}') AS {field}"
                )
        fields = ",".join(cls._FIELDS)
        return (
            "WITH combined AS ("
            f"SELECT {fields} FROM reports WHERE report_id NOT IN "
            "(SELECT report_id FROM report_index_outbox) UNION ALL "
            f"SELECT {','.join(outbox_fields)} FROM report_index_outbox)"
        )

    @classmethod
    def _normalize(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        value = dict(metadata)
        report_id = str(value.get("report_id") or "").strip()
        if not report_id:
            raise ValueError("invalid report id")
        root_name = str(value.get("root_name") or "")
        if root_name not in cls.ROOT_NAMES:
            raise ValueError("invalid report root")
        relative_path = str(value.get("relative_path") or "")
        path = PurePosixPath(relative_path)
        if (
            not relative_path
            or "\\" in relative_path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != relative_path
        ):
            raise ValueError("invalid report path")
        status = str(value.get("status") or "completed")
        source = str(value.get("source") or "web")
        index_status = str(value.get("index_status") or "indexed")
        path_state = str(value.get("path_state") or "valid")
        if status not in cls.STATUSES:
            raise ValueError("invalid report status")
        if source not in cls.SOURCES:
            raise ValueError("invalid report source")
        if index_status not in cls.INDEX_STATUSES:
            raise ValueError("invalid index status")
        if path_state not in cls.PATH_STATES:
            raise ValueError("invalid path state")
        asset_type = value.get("asset_type")
        if asset_type is not None and asset_type not in cls.ASSET_TYPES:
            raise ValueError("invalid asset type")
        rating = value.get("rating")
        if rating is None:
            rating = value.get("signal")
        rating = str(rating) if rating is not None else None
        analysts = value.get("analysts", [])
        if not isinstance(analysts, list):
            analysts = []
        providers = value.get("effective_quote_provider_chain", [])
        if not isinstance(providers, list):
            providers = []
        preview = " ".join(str(value.get("decision_preview") or "").split())[:512]
        return {
            "report_id": report_id,
            "run_id": cls._text(value.get("run_id")),
            "ticker": cls._text(value.get("ticker")),
            "asset_type": asset_type,
            "analysis_date": cls._text(value.get("analysis_date")),
            "generated_at": cls._text(value.get("generated_at")),
            "status": status,
            "rating": rating,
            "signal": rating,
            "output_language": cls._text(value.get("output_language")),
            "summary_status": cls._text(value.get("summary_status")),
            "decision_preview": preview,
            "data_snapshot_id": cls._text(value.get("data_snapshot_id")),
            "provider": cls._text(value.get("provider")),
            "quick_model": cls._text(value.get("quick_model")),
            "deep_model": cls._text(value.get("deep_model")),
            "analysts_json": json.dumps(
                [str(item) for item in analysts], ensure_ascii=False
            ),
            "research_depth": value.get("research_depth")
            if isinstance(value.get("research_depth"), int)
            else None,
            "data_status": cls._text(value.get("data_status")),
            "reproducibility": cls._text(value.get("reproducibility")),
            "quote_strategy_id": cls._text(value.get("quote_strategy_id")),
            "effective_quote_provider_chain": json.dumps(
                [str(item) for item in providers], ensure_ascii=False
            ),
            "root_name": root_name,
            "relative_path": relative_path,
            "source": source,
            "index_status": index_status,
            "path_state": path_state,
            "updated_at": cls._text(value.get("updated_at")) or _now(),
        }

    @classmethod
    def _upsert_connection(
        cls, conn: sqlite3.Connection, normalized: dict[str, Any]
    ) -> None:
        fields = cls._FIELDS
        updates = ",".join(
            f"{field}=excluded.{field}" for field in fields if field != "report_id"
        )
        conn.execute(
            f"INSERT INTO reports ({','.join(fields)}) VALUES "
            f"({','.join('?' for _ in fields)}) "
            f"ON CONFLICT(report_id) DO UPDATE SET {updates}",
            tuple(normalized.get(field) for field in fields),
        )

    @classmethod
    def _payload(cls, normalized: dict[str, Any]) -> dict[str, Any]:
        payload = dict(normalized)
        payload["analysts"] = cls._json_list(payload.get("analysts_json"))
        payload["effective_quote_provider_chain"] = cls._json_list(
            payload.get("effective_quote_provider_chain")
        )
        return payload

    @classmethod
    def _decode(cls, row: Any) -> dict[str, Any]:
        value = dict(row)
        value["analysts"] = cls._json_list(value.pop("analysts_json", None))
        value["effective_quote_provider_chain"] = cls._json_list(
            value.get("effective_quote_provider_chain")
        )
        value["rating"] = value.get("rating") or value.get("signal")
        value["signal"] = value["rating"]
        value["data_status"] = value.get("data_status") or "unknown"
        return value

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if not value:
            return []
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []

    @staticmethod
    def _text(value: Any) -> str | None:
        return str(value) if value is not None and str(value) != "" else None


class ReportRepository:
    def __init__(self, store: SQLiteStore) -> None: self.store = store
    @staticmethod
    def is_gate_ready(path) -> bool:
        root = Path(path)
        if not (root / "complete_report.md").is_file() or not (root / "COMMITTED").is_file():
            return False
        try:
            metadata = json.loads((root / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return metadata.get("status") == "completed"
    def iter_ready(self, root):
        return iter(sorted(path.parent for path in Path(root).rglob("complete_report.md") if self.is_gate_ready(path.parent)))
