"""Step 40 — QuoteService cache invalidation hooks.

Covers:
- invalidate(symbol, asset_type)
- invalidate(asset_type) - all symbols of asset class
- invalidate(all_asset_types=True) - nuke
- purge_stale(older_than_seconds)
- QuoteService.invalidate / purge_stale wrappers
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _seed_quote(store, symbol, asset_type, fetched_seconds_ago=10):
    """Insert a market_quotes row directly via SQLite."""
    from datetime import datetime, timezone, timedelta
    fetched_at = (
        datetime.now(timezone.utc) - timedelta(seconds=fetched_seconds_ago)
    ).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_quotes
               (symbol, asset_type, price, currency, as_of, fetched_at,
                payload_json, cache_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, asset_type, 100.0, "CNY", fetched_at, fetched_at,
             "{}", "hit"),
        )


def _make_repo(tmp_path):
    """Build a QuoteRepository over a fresh SQLite file with the
    market_quotes schema. Avoids touching real DB.
    """
    from web.storage import SQLiteStore
    db = tmp_path / "test_quotes.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE market_quotes (
                symbol TEXT,
                asset_type TEXT,
                price REAL,
                currency TEXT,
                as_of TEXT,
                fetched_at TEXT,
                payload_json TEXT,
                cache_status TEXT,
                PRIMARY KEY (symbol, asset_type)
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    store = SQLiteStore(db)
    from web.repositories import QuoteRepository
    return QuoteRepository(store), store


def test_invalidate_specific_symbol_and_asset_type():
    """invalidate(symbol, asset_type) drops exactly one row."""
    from web.repositories import QuoteRepository
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    _seed_quote(store, "600036.SS", "stock")
    _seed_quote(store, "600036.SS", "crypto")  # same symbol, different asset_type
    _seed_quote(store, "000001.SZ", "stock")

    n = repo.invalidate("600036.SS", "stock")
    assert n == 1
    # Other rows remain.
    assert repo.get_latest("600036.SS", "crypto") is not None
    assert repo.get_latest("000001.SZ", "stock") is not None
    assert repo.get_latest("600036.SS", "stock") is None


def test_invalidate_symbol_all_asset_types():
    """invalidate(symbol, all_asset_types=True) wipes both rows."""
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    _seed_quote(store, "BTC", "stock")
    _seed_quote(store, "BTC", "crypto")

    n = repo.invalidate("BTC", all_asset_types=True)
    assert n == 2
    assert repo.get_latest("BTC", "stock") is None
    assert repo.get_latest("BTC", "crypto") is None


def test_invalidate_by_asset_type_clears_class():
    """invalidate(asset_type=...) wipes all rows of that asset class."""
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    _seed_quote(store, "BTC", "crypto")
    _seed_quote(store, "ETH", "crypto")
    _seed_quote(store, "600036.SS", "stock")

    n = repo.invalidate(asset_type="crypto")
    assert n == 2
    assert repo.get_latest("BTC", "crypto") is None
    assert repo.get_latest("ETH", "crypto") is None
    # Stock survives.
    assert repo.get_latest("600036.SS", "stock") is not None


def test_invalidate_all_nukes_cache():
    """invalidate() with no args drops everything."""
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    for s in ("A", "B", "C"):
        _seed_quote(store, s, "stock")
        _seed_quote(store, s, "crypto")

    n = repo.invalidate()
    assert n == 6
    assert repo.get_latest("A", "stock") is None
    assert repo.get_latest("B", "crypto") is None


def test_purge_stale_drops_old_rows():
    """purge_stale(N) only deletes rows with fetched_at > N seconds ago."""
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    _seed_quote(store, "FRESH", "stock", fetched_seconds_ago=10)
    _seed_quote(store, "STALE", "stock", fetched_seconds_ago=3600)

    n = repo.purge_stale(older_than_seconds=300)
    assert n == 1
    assert repo.get_latest("FRESH", "stock") is not None
    assert repo.get_latest("STALE", "stock") is None


def test_invalidate_unknown_symbol_returns_zero():
    """Inquiring about an unknown symbol must NOT raise."""
    repo, store = _make_repo(Path(tempfile.mkdtemp()))
    n = repo.invalidate("NEVER.SE", "stock")
    assert n == 0


def test_quote_service_invalidate_wrapper():
    """QuoteService.invalidate goes through the repository."""
    from web.market_data import QuoteService
    from web.storage import SQLiteStore

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "test_web_runs.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE market_quotes (
                symbol TEXT, asset_type TEXT, price REAL, currency TEXT,
                as_of TEXT, fetched_at TEXT, payload_json TEXT,
                cache_status TEXT, PRIMARY KEY (symbol, asset_type)
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    store = SQLiteStore(db)
    _seed_quote(store, "X", "stock")

    from web.repositories import QuoteRepository
    repo = QuoteRepository(store)
    # Fake router / repository injection.
    qs = QuoteService.__new__(QuoteService)
    qs.repository = repo
    qs.router = None  # not used by invalidate
    n = qs.invalidate("X", "stock")
    assert n == 1
    # Second call is a no-op.
    assert qs.invalidate("X", "stock") == 0


def test_quote_service_purge_stale_wrapper():
    """QuoteService.purge_stale delegates to the repository."""
    from web.market_data import QuoteService
    from web.storage import SQLiteStore

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "test_web_runs2.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE market_quotes (
                symbol TEXT, asset_type TEXT, price REAL, currency TEXT,
                as_of TEXT, fetched_at TEXT, payload_json TEXT,
                cache_status TEXT, PRIMARY KEY (symbol, asset_type)
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    store = SQLiteStore(db)
    from web.repositories import QuoteRepository
    repo = QuoteRepository(store)
    qs = QuoteService.__new__(QuoteService)
    qs.repository = repo
    _seed_quote(store, "FRESH", "stock", fetched_seconds_ago=10)
    _seed_quote(store, "STALE", "stock", fetched_seconds_ago=7200)
    n = qs.purge_stale(older_than_seconds=3600)
    assert n == 1
    assert repo.get_latest("FRESH", "stock") is not None
    assert repo.get_latest("STALE", "stock") is None
