"""Background daemon that periodically evaluates alerts across all enabled assets.

Independent of the browser-driven quote polling. Decouples monitoring from the UI.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL = 60  # seconds
MIN_INTERVAL = 15
MAX_INTERVAL = 600


class AlertMonitor:
    """Periodically scan enabled alerts, refresh quotes, evaluate, notify."""

    def __init__(
        self,
        *,
        settings_repo,
        alerts_repo,
        quote_service,
        alert_engine,
        notifier,
    ) -> None:
        self._settings_repo = settings_repo
        self._alerts_repo = alerts_repo
        self._quote_service = quote_service
        self._alert_engine = alert_engine
        self._notifier = notifier
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = DEFAULT_INTERVAL
        self._enabled = False
        self._last_run_at: float | None = None
        self._last_run_summary: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _interval_from_settings(self) -> int:
        entry = self._settings_repo.get("notifier.monitor_interval_seconds") or {}
        try:
            raw = int((entry.get("value") or str(DEFAULT_INTERVAL)).strip() or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            raw = DEFAULT_INTERVAL
        return max(MIN_INTERVAL, min(MAX_INTERVAL, raw))

    def _enabled_from_settings(self) -> bool:
        entry = self._settings_repo.get("notifier.monitor_enabled") or {}
        return (entry.get("value") or "false").strip().lower() in ("1", "true", "yes", "on")

    def refresh_config(self) -> None:
        with self._lock:
            self._enabled = self._enabled_from_settings()
            self._interval = self._interval_from_settings()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.refresh_config()
        self._thread = threading.Thread(target=self._loop, name="alert-monitor", daemon=True)
        self._thread.start()
        LOGGER.info("alert monitor started (enabled=%s, interval=%ss)", self._enabled, self._interval)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        LOGGER.info("alert monitor stopped")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "interval_seconds": self._interval,
                "running": bool(self._thread and self._thread.is_alive()),
                "last_run_at": self._last_run_at,
                "last_run_summary": dict(self._last_run_summary),
            }

    def run_once(self) -> dict[str, Any]:
        """Public entry point for a single sweep. Useful for tests / manual triggers."""
        return self._sweep()

    # ---- internals --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Re-read settings at the top of every cycle so UI changes take effect
                self.refresh_config()
                if self._enabled:
                    summary = self._sweep()
                    with self._lock:
                        self._last_run_at = time.time()
                        self._last_run_summary = summary
            except Exception:
                LOGGER.exception("alert monitor loop iteration failed")
            # Sleep with early-exit on stop
            with self._lock:
                interval = self._interval
            self._stop_event.wait(timeout=interval)

    def _sweep(self) -> dict[str, Any]:
        try:
            alerts = self._alerts_repo.list_active()
        except Exception as exc:
            LOGGER.warning("list_active failed: %s", exc)
            return {"error": "list_active_failed", "detail": str(exc)}

        if not alerts:
            return {"alerts_seen": 0, "evaluated": 0, "triggers": 0, "notified": 0}

        # Group symbols by asset_type
        buckets: dict[str, list[str]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for a in alerts:
            key = (a["symbol"], a.get("asset_type", "stock"))
            if key in seen:
                continue
            seen.add(key)
            buckets[a.get("asset_type") or "stock"].append(a["symbol"])

        triggers_total = 0
        evaluated_total = 0
        notified_total = 0
        errors: dict[str, str] = {}

        for asset_type, symbols in buckets.items():
            try:
                bulk = self._quote_service.get_quotes(symbols, asset_type)
            except Exception as exc:
                LOGGER.warning("get_quotes failed for %s: %s", asset_type, exc)
                errors[asset_type] = str(exc)
                continue

            for item in bulk.items:
                quote_dict = self._quote_item_to_dict(item)
                if not quote_dict:
                    continue
                evaluated_total += 1
                try:
                    raw_triggers = self._alert_engine.evaluate_quote(
                        item.symbol, asset_type, quote_dict
                    )
                    events = self._alert_engine.record_triggers(raw_triggers)
                except Exception:
                    LOGGER.exception("evaluate/record failed for %s", item.symbol)
                    continue
                triggers_total += len(raw_triggers)
                for trigger, event in zip(raw_triggers, events):
                    if self._notifier is None or not event:
                        continue
                    try:
                        self._notifier.notify_trigger(
                            {
                                "alert_id": trigger.alert_id,
                                "symbol": trigger.symbol,
                                "asset_type": trigger.asset_type,
                                "kind": trigger.kind,
                                "message": trigger.message,
                                "snapshot": trigger.snapshot,
                            },
                            event,
                        )
                        notified_total += 1
                    except Exception:
                        LOGGER.exception("notifier dispatch failed for %s", trigger.symbol)

        return {
            "alerts_seen": len(alerts),
            "symbols_seen": len(seen),
            "evaluated": evaluated_total,
            "triggers": triggers_total,
            "notified": notified_total,
            "errors": errors,
        }

    def _quote_item_to_dict(self, item: Any) -> dict[str, Any] | None:
        """Convert a QuoteItem from market_data into a dict the AlertEngine accepts."""
        quote = getattr(item, "quote", None)
        if quote is None:
            return None
        out: dict[str, Any] = {
            "symbol": getattr(item, "symbol", None) or getattr(quote, "symbol", None),
            "price": getattr(quote, "price", None),
        }
        for attr in (
            "currency", "source", "freshness", "market_status",
            "exchange", "asset_name", "asset_name_zh", "exchange_name_zh",
            "canonical_symbol", "previous_close", "change", "change_percent", "volume",
        ):
            val = getattr(quote, attr, None)
            if val is not None:
                out[attr] = val
        # datetime fields → ISO string (snapshot is JSON-serialized to SQLite)
        for attr in ("as_of", "fetched_at"):
            val = getattr(quote, attr, None)
            if val is None:
                continue
            if hasattr(val, "isoformat"):
                out[attr] = val.isoformat()
            else:
                out[attr] = str(val)
        return out
