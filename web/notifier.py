"""Notifier channels: PushPlus / Feishu / WeCom group bots."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

LOGGER = logging.getLogger(__name__)
if LOGGER.level == logging.NOTSET:
    LOGGER.setLevel(logging.INFO)

try:
    from zoneinfo import ZoneInfo
    _CST = ZoneInfo("Asia/Shanghai")
except Exception:
    _CST = None

PUSHPLUS_ENDPOINT = "http://www.pushplus.plus/send"


def _format_local_time(value: Any) -> str:
    """Render ISO timestamp as 'yyyy-mm-dd HH:MM:SS' in Asia/Shanghai."""
    if not value:
        return ""
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if _CST is not None:
            dt = dt.astimezone(_CST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


@dataclass
class NotifierConfig:
    pushplus_enabled: bool = False
    pushplus_token: str = ""
    feishu_enabled: bool = False
    feishu_webhook: str = ""

    @classmethod
    def from_settings(cls, raw: dict[str, dict[str, str]] | None) -> "NotifierConfig":
        raw = raw or {}
        def _val(key: str, default: str = "") -> str:
            entry = raw.get(key) or {}
            return (entry.get("value") or default).strip()
        def _bool(key: str) -> bool:
            return _val(key, "false").lower() in ("1", "true", "yes", "on")
        return cls(
            pushplus_enabled=_bool("notifier.pushplus_enabled"),
            pushplus_token=_val("notifier.pushplus_token"),
            feishu_enabled=_bool("notifier.feishu_enabled"),
            feishu_webhook=_val("notifier.feishu_webhook"),
        )

    def active_channels(self) -> list[str]:
        channels = []
        if self.pushplus_enabled and self.pushplus_token:
            channels.append("pushplus")
        if self.feishu_enabled and self.feishu_webhook:
            channels.append("feishu")
        return channels


def format_alert_payload(trigger: dict[str, Any], event: dict[str, Any]) -> tuple[str, str]:
    """Render title + markdown content for a trigger event."""
    symbol = trigger.get("symbol") or event.get("symbol") or "?"
    kind = trigger.get("kind") or event.get("kind") or "alert"
    snapshot = trigger.get("snapshot") or event.get("snapshot") or {}
    price = snapshot.get("price")
    threshold = snapshot.get("threshold")
    direction = snapshot.get("direction")
    change_pct = snapshot.get("change_pct")
    metric = snapshot.get("metric")
    window = snapshot.get("window_minutes")
    message = trigger.get("message") or event.get("message") or ""
    triggered_at = _format_local_time(event.get("triggered_at") or trigger.get("triggered_at"))

    asset_name_zh = snapshot.get("asset_name_zh")
    asset_name_en = snapshot.get("asset_name")
    name_for_title = asset_name_zh or asset_name_en
    if name_for_title:
        title = f"[{kind}] {name_for_title} ({symbol})"
    else:
        title = f"[{kind}] {symbol}"
    if kind == "price":
        if price is not None and threshold is not None:
            arrow = "≥" if direction == "above" else "≤"
            title += f"  {price:g} {arrow} {threshold:g}"
    elif kind == "quantitative":
        if metric and change_pct is not None:
            title += f"  {metric} {change_pct:g}%/{window or 0}m"

    asset_name_zh = snapshot.get("asset_name_zh")
    asset_name_en = snapshot.get("asset_name")
    exchange_zh = snapshot.get("exchange_name_zh")
    exchange_code = snapshot.get("exchange")
    change_pct_val = snapshot.get("change_percent")
    change_val = snapshot.get("change")
    prev_close = snapshot.get("previous_close")
    volume_val = snapshot.get("volume")

    header_bits = []
    if asset_name_zh:
        header_bits.append(str(asset_name_zh))
    if asset_name_en and asset_name_en != asset_name_zh:
        header_bits.append(str(asset_name_en))
    name_line = " · ".join(header_bits) if header_bits else symbol

    lines = [
        f"**{name_line}** ({symbol})",
        f"**{message}**",
        "",
        f"- 触发时间: {triggered_at}",
        f"- 类型: {kind}",
    ]
    if price is not None:
        lines.append(f"- 当前价格: {price:g}")
    if prev_close is not None:
        lines.append(f"- 昨收: {prev_close:g}")
    if change_val is not None:
        change_str = f"{change_val:+g}" if isinstance(change_val, (int, float)) else str(change_val)
        lines.append(f"- 当日涨跌: {change_str}")
    if change_pct_val is not None:
        pct_str = f"{change_pct_val:+.2f}%" if isinstance(change_pct_val, (int, float)) else str(change_pct_val)
        lines.append(f"- 当日涨幅: {pct_str}")
    if threshold is not None:
        lines.append(f"- 告警阈值: {threshold:g} ({direction})")
    if metric:
        lines.append(f"- 告警指标: {metric} 变化 {change_pct:g}% / 窗口 {window or 0}m")
    if exchange_zh or exchange_code:
        exch_text = exchange_zh or ""
        if exchange_code and exchange_code != exchange_zh:
            exch_text = f"{exch_text} ({exchange_code})" if exch_text else exchange_code
        lines.append(f"- 交易所: {exch_text}")
    if volume_val is not None:
        if isinstance(volume_val, (int, float)) and volume_val >= 10000:
            lines.append(f"- 成交量: {volume_val/10000:.2f}万")
        else:
            lines.append(f"- 成交量: {volume_val}")
    if snapshot.get("currency"):
        lines.append(f"- 货币: {snapshot['currency']}")
    if snapshot.get("source"):
        lines.append(f"- 数据源: {snapshot['source']} ({snapshot.get('freshness') or ''})")

    content = "\n".join(lines)
    return title, content


def send_pushplus(token: str, title: str, content: str, *, template: str = "markdown") -> dict[str, Any]:
    body = json.dumps({
        "token": token,
        "title": title[:200],
        "content": content,
        "template": template,
    }).encode("utf-8")
    req = urlrequest.Request(
        PUSHPLUS_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            payload = resp.read().decode("utf-8")
            data = json.loads(payload) if payload else {}
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("pushplus send failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    if data.get("code") == 200:
        print(f"[notifier] pushplus sent: {data.get('data')}", flush=True)
        return {"ok": True, "msg": data.get("msg"), "msg_id": data.get("data")}
    return {"ok": False, "code": data.get("code"), "msg": data.get("msg")}


def send_feishu(webhook: str, title: str, content: str) -> dict[str, Any]:
    """Send a Feishu interactive card message via custom bot webhook."""
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:120]},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            payload = resp.read().decode("utf-8")
            data = json.loads(payload) if payload else {}
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("feishu send failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    # Feishu bot returns {"StatusCode": 0, "msg": "success"} or {"code": 0, ...}
    if data.get("StatusCode") == 0 or data.get("code") == 0:
        print(f"[notifier] feishu sent to {webhook[:60]}: {data.get('msg') or 'success'}", flush=True)
        return {"ok": True, "msg": data.get("msg") or "success"}
    return {"ok": False, "code": data.get("code") or data.get("StatusCode"), "msg": data.get("msg")}


class Notifier:
    """Fire-and-forget notifier wrapper around multiple channels."""

    def __init__(self, *, settings_repo) -> None:
        self._settings_repo = settings_repo

    def config(self) -> NotifierConfig:
        return NotifierConfig.from_settings(self._settings_repo.all())

    def notify_trigger(self, trigger: dict[str, Any], event: dict[str, Any]) -> None:
        cfg = self.config()
        channels = cfg.active_channels()
        LOGGER.info("notify_trigger symbol=%s channels=%s pushplus_on=%s feishu_on=%s pushplus_token=%s feishu_url=%s",
                    trigger.get("symbol"), channels,
                    cfg.pushplus_enabled, cfg.feishu_enabled,
                    bool(cfg.pushplus_token), bool(cfg.feishu_webhook))
        if not channels:
            return
        title, content = format_alert_payload(trigger, event)
        for channel in channels:
            if channel == "pushplus":
                token = cfg.pushplus_token
                def _send_pushplus(t=token, ti=title, ct=content) -> None:
                    res = send_pushplus(t, ti, ct)
                    if not res.get("ok"):
                        LOGGER.warning("pushplus notify failed: %s", res)
                threading.Thread(target=_send_pushplus, daemon=True, name="pushplus-send").start()
            elif channel == "feishu":
                url = cfg.feishu_webhook
                def _send_feishu(u=url, ti=title, ct=content) -> None:
                    res = send_feishu(u, ti, ct)
                    if not res.get("ok"):
                        LOGGER.warning("feishu notify failed: %s", res)
                threading.Thread(target=_send_feishu, daemon=True, name="feishu-send").start()

    def send_test(self, channel: str | None = None) -> dict[str, Any]:
        cfg = self.config()
        title = "[TradingAgents] 测试通知"
        content = "如果你在飞书/微信收到这条消息,说明告警通知通道已经连通。🚀"
        results: dict[str, Any] = {}
        targets = [channel] if channel else cfg.active_channels()
        for ch in targets:
            if ch == "pushplus":
                if not cfg.pushplus_token:
                    results[ch] = {"ok": False, "error": "token 未配置"}
                    continue
                results[ch] = send_pushplus(cfg.pushplus_token, title, content)
            elif ch == "feishu":
                if not cfg.feishu_webhook:
                    results[ch] = {"ok": False, "error": "webhook 未配置"}
                    continue
                results[ch] = send_feishu(cfg.feishu_webhook, title, content)
        if not results:
            return {"ok": False, "error": "没有可用通道,请先在设置页启用并填写凭据"}
        ok = all(r.get("ok") for r in results.values())
        return {"ok": ok, "channels": results}
