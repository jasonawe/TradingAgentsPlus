"""Background daemon that periodically refreshes quote cache for symbols the user
is likely to view: watchlist + enabled-alert symbols.

Independent of any request path — the SPA can always hit the cache and never
has to wait for an upstream fetch.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL = 30  # seconds; aligned with default quote_ttl_seconds (60s)
MIN_INTERVAL = 10
MAX_INTERVAL = 300


class QuotePrewarmer:
    """Periodically refresh ``QuoteService`` cache for the active symbol set.

    Why this exists:
    * ``QuoteService.get_quote`` checks the cache; a miss falls through to the
      provider chain which on this network is ~10s for an 8-symbol batch
      (eastmoney is geo-blocked, yfinance ``.info`` is slow).
    * A background warmer keeps the cache hot so the SPA never observes the
      cold path during normal use.

    Refresh targets:
    1. All symbols on the default watchlist.
    2. Symbols backing any enabled alert (``AlertRepository.list_active``).

    The two are de-duplicated before issuing a single ``get_quotes`` per
    ``asset_type``.
    """

    def __init__(
        self,
        *,
        settings_repo,
        watchlist_repo,
        alerts_repo,
        quote_service,
    ) -> None:
        self._settings_repo = settings_repo
        self._watchlist_repo = watchlist_repo
        self._alerts_repo = alerts_repo
        self._quote_service = quote_service

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_run_at: float | None = None
        self._last_run_summary: dict[str, Any] = {}
        self._enabled = True
        self._interval = DEFAULT_INTERVAL
        self._bootstrap_on_startup = True

    # ---- public API ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.refresh_config()
        self._thread = threading.Thread(
            target=self._loop, name="quote-prewarmer", daemon=True
        )
        self._thread.start()
        LOGGER.info(
            "quote prewarmer started (enabled=%s, interval=%ss, bootstrap=%s)",
            self._enabled,
            self._interval,
            self._bootstrap_on_startup,
        )
        if self._bootstrap_on_startup and self._enabled:
            # Run once immediately so the cache is warm before the first user
            # request lands. Use a short-lived thread so we don't block start().
            threading.Thread(target=self.run_once, name="quote-prewarmer-bootstrap", daemon=True).start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        LOGGER.info("quote prewarmer stopped")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "interval_seconds": self._interval,
                "bootstrap_on_startup": self._bootstrap_on_startup,
                "running": bool(self._thread and self._thread.is_alive()),
                "last_run_at": self._last_run_at,
                "last_run_summary": dict(self._last_run_summary),
            }

    def refresh_config(self) -> None:
        settings = self._settings_repo.all()
        self._enabled = self._read_bool(settings.get("prewarmer.enabled"), True)
        self._interval = self._clamp_interval(
            self._read_int(settings.get("prewarmer.interval_seconds"), DEFAULT_INTERVAL)
        )
        self._bootstrap_on_startup = self._read_bool(
            settings.get("prewarmer.bootstrap_on_startup"), True
        )

    def run_once(self) -> dict[str, Any]:
        """Public entry point — also used by tests / manual triggers."""
        return self._sweep()

    # ---- internals --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_config()
                if self._enabled:
                    summary = self._sweep()
                    with self._lock:
                        self._last_run_at = time.time()
                        self._last_run_summary = summary
            except Exception:
                LOGGER.exception("quote prewarmer loop iteration failed")
            with self._lock:
                interval = self._interval
            self._stop_event.wait(timeout=interval)

    def _sweep(self) -> dict[str, Any]:
        watchlist = self._safe_list_watchlist()
        alerts = self._safe_list_alerts()

        buckets: dict[str, set[str]] = defaultdict(set)
        for item in watchlist:
            asset_type = item.get("asset_type") or "stock"
            symbol = item.get("symbol")
            if symbol:
                buckets[asset_type].add(symbol)
        for alert in alerts:
            asset_type = alert.get("asset_type") or "stock"
            symbol = alert.get("symbol")
            if symbol:
                buckets[asset_type].add(symbol)

        if not buckets:
            return {"symbols_seen": 0, "evaluated": 0, "errors": {}}

        evaluated_total = 0
        errors: dict[str, str] = {}
        sweep_started = time.monotonic()
        # Preserve deterministic order so logs are easier to compare.
        for asset_type in sorted(buckets):
            symbols = sorted(buckets[asset_type])
            try:
                bulk = self._quote_service.get_quotes(symbols, asset_type)
            except Exception as exc:
                LOGGER.warning("get_quotes failed for %s (%d symbols): %s", asset_type, len(symbols), exc)
                errors[asset_type] = str(exc)
                continue
            evaluated_total += sum(
                1 for item in (bulk.items or []) if getattr(item, "quote", None) and getattr(item.quote, "price", None) is not None
            )

        sweep_seconds = round(time.monotonic() - sweep_started, 3)
        summary = {
            "symbols_seen": sum(len(s) for s in buckets.values()),
            "asset_types": sorted(buckets),
            "evaluated": evaluated_total,
            "errors": errors,
        }
        LOGGER.info(
            "quote prewarmer refreshed %d symbols across %d asset types in %.2fs%s",
            summary["symbols_seen"],
            len(summary["asset_types"]),
            sweep_seconds,
            f" errors={errors}" if errors else "",
        )
        return summary

    def _safe_list_watchlist(self) -> list[dict[str, Any]]:
        try:
            return list(self._watchlist_repo.list_items("default") or [])
        except Exception as exc:
            LOGGER.warning("watchlist list_items failed: %s", exc)
            return []

    def _safe_list_alerts(self) -> list[dict[str, Any]]:
        try:
            return list(self._alerts_repo.list_active() or [])
        except Exception as exc:
            LOGGER.warning("alerts list_active failed: %s", exc)
            return []

    @staticmethod
    def _read_bool(entry: Any, fallback: bool) -> bool:
        if not isinstance(entry, dict):
            return fallback
        raw = entry.get("value")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.lower() in {"true", "1", "yes", "on"}
        return fallback

    @staticmethod
    def _read_int(entry: Any, fallback: int) -> int:
        if not isinstance(entry, dict):
            return fallback
        try:
            return int(entry.get("value"))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _clamp_interval(value: int) -> int:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, int(value)))


__all__ = ["QuotePrewarmer"]
