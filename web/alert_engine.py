"""Background alert evaluator that runs against incoming quote data."""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .repositories import AlertRepository, VALID_ALERT_METRICS

LOGGER = logging.getLogger(__name__)


@dataclass
class AlertTrigger:
    alert_id: str
    symbol: str
    asset_type: str
    kind: str
    message: str
    snapshot: dict[str, Any]


class AlertEngine:
    """Evaluate price / quantitative alerts when a fresh quote is observed.

    The engine keeps an in-memory ring of recent metric readings per
    (symbol, asset_type, metric) so quantitative alerts can be evaluated
    without depending on a separate timeseries store.
    """

    def __init__(
        self,
        repository: AlertRepository,
        *,
        ring_window_minutes: int = 24 * 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.ring_window_minutes = max(5, int(ring_window_minutes))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._history: dict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
        self._last_prices: dict[tuple[str, str], float] = {}

    # ----- Public API -------------------------------------------------------

    def evaluate_quote(self, symbol: str, asset_type: str, quote: dict[str, Any]) -> list[AlertTrigger]:
        """Evaluate all enabled alerts for a symbol given a fresh quote snapshot."""

        if not isinstance(quote, dict):
            return []
        price = _safe_float(quote.get("price"))
        if price is None:
            return []
        triggers: list[AlertTrigger] = []
        with self._lock:
            self._record_history(symbol, asset_type, price, quote)
            alerts = self.repository.list_for_symbol(symbol, asset_type)
            now = self._clock()
            for alert in alerts:
                if not alert.get("enabled", True):
                    continue
                cooldown = int(alert.get("cooldown_seconds") or 0)
                last_triggered = _parse_iso(alert.get("last_triggered_at"))
                if cooldown and last_triggered and (now - last_triggered) < timedelta(seconds=cooldown):
                    continue
                last_evaluated = _parse_iso(alert.get("last_evaluated_at"))
                if last_evaluated and (now - last_evaluated) < timedelta(seconds=15):
                    continue
                kind = alert.get("kind")
                params = alert.get("params") or {}
                trigger = self._evaluate_one(alert, kind, params, price, quote, now)
                if trigger is not None:
                    triggers.append(trigger)
            if alerts:
                self.repository.mark_evaluated([a["id"] for a in alerts])
        return triggers

    def record_triggers(self, triggers: Iterable[AlertTrigger]) -> list[dict[str, Any]]:
        events = []
        for trigger in triggers:
            try:
                event = self.repository.record_event(
                    trigger.alert_id,
                    trigger.message,
                    trigger.snapshot,
                )
                events.append(event)
            except KeyError:
                continue
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.warning("failed to record alert event: %s", exc)
        return events

    # ----- Internals --------------------------------------------------------

    def _evaluate_one(
        self,
        alert: dict[str, Any],
        kind: str | None,
        params: dict[str, Any],
        price: float,
        quote: dict[str, Any],
        now: datetime,
    ) -> AlertTrigger | None:
        if kind == "price":
            return self._evaluate_price(alert, params, price, quote)
        if kind == "quantitative":
            return self._evaluate_quantitative(alert, params, price, quote, now)
        return None

    def _evaluate_price(
        self,
        alert: dict[str, Any],
        params: dict[str, Any],
        price: float,
        quote: dict[str, Any],
    ) -> AlertTrigger | None:
        try:
            threshold = float(params.get("threshold"))
            direction = str(params.get("direction", "above"))
        except (TypeError, ValueError):
            return None
        prev = self._last_prices.get((alert["symbol"], alert["asset_type"]))
        self._last_prices[(alert["symbol"], alert["asset_type"])] = price
        if direction == "above":
            crossed = prev is None or prev < threshold
            hit = price >= threshold
        else:
            crossed = prev is None or prev > threshold
            hit = price <= threshold
        if not (crossed and hit):
            return None
        message = f"{alert['symbol']} 价格 {price:.4g} {'≥' if direction == 'above' else '≤'} {threshold:.4g}"
        snapshot = {"price": price, "threshold": threshold, "direction": direction, "previous_price": prev}
        snapshot.update(_quote_meta(quote))
        return AlertTrigger(
            alert_id=alert["id"],
            symbol=alert["symbol"],
            asset_type=alert["asset_type"],
            kind="price",
            message=message,
            snapshot=snapshot,
        )

    def _evaluate_quantitative(
        self,
        alert: dict[str, Any],
        params: dict[str, Any],
        price: float,
        quote: dict[str, Any],
        now: datetime,
    ) -> AlertTrigger | None:
        metric = params.get("metric")
        if metric not in VALID_ALERT_METRICS:
            return None
        current = _metric_value(metric, price, quote)
        if current is None:
            return None
        try:
            change_pct = float(params.get("change_pct", 0))
        except (TypeError, ValueError):
            return None
        window = max(0, int(params.get("window_minutes", 0)))
        baseline = self._baseline_for(alert["symbol"], alert["asset_type"], metric, now, window)
        if baseline is None:
            return None
        delta_pct = (current - baseline) / baseline * 100.0 if baseline else 0.0
        if abs(delta_pct) < abs(change_pct):
            return None
        direction = "上涨" if delta_pct > 0 else "下跌"
        message = f"{alert['symbol']} {metric} {direction} {abs(delta_pct):.1f}% (基线 {baseline:.4g} → 当前 {current:.4g})"
        snapshot = {
            "metric": metric,
            "current": current,
            "baseline": baseline,
            "change_pct": delta_pct,
            "window_minutes": window,
            "threshold_pct": change_pct,
            "price": price,
        }
        snapshot.update(_quote_meta(quote))
        return AlertTrigger(
            alert_id=alert["id"],
            symbol=alert["symbol"],
            asset_type=alert["asset_type"],
            kind="quantitative",
            message=message,
            snapshot=snapshot,
        )

    def _baseline_for(
        self,
        symbol: str,
        asset_type: str,
        metric: str,
        now: datetime,
        window_minutes: int,
    ) -> float | None:
        key = (symbol, asset_type, metric)
        readings = self._history.get(key)
        if not readings:
            return None
        if window_minutes <= 0:
            return readings[0][1]
        cutoff = now - timedelta(minutes=window_minutes)
        eligible = [value for ts, value in readings if ts <= cutoff]
        if not eligible:
            eligible = [value for _, value in readings[:1]]
        return eligible[0] if eligible else None

    def _record_history(self, symbol: str, asset_type: str, price: float, quote: dict[str, Any]) -> None:
        now = self._clock()
        for metric in VALID_ALERT_METRICS:
            value = _metric_value(metric, price, quote)
            if value is None:
                continue
            key = (symbol, asset_type, metric)
            readings = self._history[key]
            readings.append((now, value))
            cutoff = now - timedelta(minutes=self.ring_window_minutes)
            while readings and readings[0][0] < cutoff:
                readings.pop(0)


# ----- Helpers --------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN check
        return None
    return result


def _metric_value(metric: str, price: float, quote: dict[str, Any]) -> float | None:
    if metric == "change_percent":
        return _safe_float(quote.get("change_percent"))
    if metric == "volume":
        return _safe_float(quote.get("volume"))
    if metric == "turnover":
        return _safe_float(quote.get("turnover"))
    if metric == "turnover_rate":
        return _safe_float(quote.get("turnover_rate"))
    if metric == "market_cap":
        return _safe_float(quote.get("market_cap"))
    if metric == "circulating_cap":
        return _safe_float(quote.get("circulating_cap"))
    if metric == "pe_ratio":
        return _safe_float(quote.get("pe_ratio"))
    if metric == "amplitude":
        return _safe_float(quote.get("amplitude"))
    return None


def _quote_meta(quote: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key in ("currency", "source", "as_of", "fetched_at", "freshness", "market_status"):
        value = quote.get(key)
        if value is not None:
            meta[key] = value
    return meta


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
