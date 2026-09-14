"""FeishuAlerter — 飞书群机器人 webhook 通知 (v3 spec §7.2 #10).

Webhook URL is read from ``TRADINGAGENTS_FEISHU_WEBHOOK`` env var
(per spec, user-provided). Payload uses 飞书 v2 hook rich-text format.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

LOGGER = logging.getLogger(__name__)


class FeishuAlerter:
    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url or os.environ.get("TRADINGAGENTS_FEISHU_WEBHOOK", "")

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, title: str, content: str, *, at_mobiles: list[str] | None = None) -> bool:
        if not self.enabled:
            LOGGER.info("FeishuAlerter: webhook not configured; would send %r", title)
            return False
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [
                    {"tag": "markdown", "content": content},
                ],
            },
        }
        if at_mobiles:
            payload["card"]["elements"].append(
                {"tag": "at", "at_mobiles": at_mobiles}
            )
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url, data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                LOGGER.info("FeishuAlerter: %s", resp.status)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            LOGGER.warning("FeishuAlerter send failed: %s", e)
            return False
