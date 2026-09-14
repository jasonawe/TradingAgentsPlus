"""SQLite ProviderCache tests (v3 P2 / N62 fix)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.data.cache import (  # noqa: E402
    DEFAULT_TTL_SECONDS,
    ProviderCache,
    cached_call,
    make_key,
)


@pytest.fixture
def tmp_cache(tmp_path: Path) -> ProviderCache:
    return ProviderCache(db_path=tmp_path / "cache.sqlite", default_ttl=60)


def test_make_key_is_stable_and_param_order_insensitive() -> None:
    k1 = make_key("yfinance", "quote", {"symbol": "AAPL", "interval": "1d"})
    k2 = make_key("yfinance", "quote", {"interval": "1d", "symbol": "AAPL"})
    assert k1 == k2
    assert k1.startswith("yfinance::quote::")


def test_make_key_separates_providers() -> None:
    k1 = make_key("yfinance", "quote", {"symbol": "AAPL"})
    k2 = make_key("eastmoney", "quote", {"symbol": "AAPL"})
    assert k1 != k2


def test_set_and_get_round_trip(tmp_cache: ProviderCache) -> None:
    tmp_cache.set("k1", '"hello"')
    assert tmp_cache.get("k1") == '"hello"'


def test_expired_entries_return_none(tmp_cache: ProviderCache) -> None:
    tmp_cache.set("k1", '"value"', ttl=1)
    assert tmp_cache.get("k1") == '"value"'
    time.sleep(2.5)
    assert tmp_cache.get("k1") is None


def test_overwrite_same_key(tmp_cache: ProviderCache) -> None:
    tmp_cache.set("k", '"v1"')
    tmp_cache.set("k", '"v2"')
    assert tmp_cache.get("k") == '"v2"'


def test_delete_removes_entry(tmp_cache: ProviderCache) -> None:
    tmp_cache.set("k", '"v"')
    tmp_cache.delete("k")
    assert tmp_cache.get("k") is None


def test_clear_removes_all(tmp_cache: ProviderCache) -> None:
    tmp_cache.set("a", '"1"')
    tmp_cache.set("b", '"2"')
    tmp_cache.clear()
    assert tmp_cache.get("a") is None
    assert tmp_cache.get("b") is None


def test_cached_call_returns_hit_on_second_call(tmp_cache: ProviderCache) -> None:
    calls = []

    def compute() -> str:
        calls.append(1)
        return '"computed"'

    params = {"symbol": "AAPL"}
    first, hit1 = cached_call("yfinance", "quote", params, compute, cache=tmp_cache)
    second, hit2 = cached_call("yfinance", "quote", params, compute, cache=tmp_cache)
    assert first == second == '"computed"'
    assert hit1 is False
    assert hit2 is True
    assert len(calls) == 1


def test_default_ttl_is_60s() -> None:
    assert DEFAULT_TTL_SECONDS == 60
