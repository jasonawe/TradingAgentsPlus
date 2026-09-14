"""AuditLogger — JSONL append-only audit log (v3 spec §7.2 #10)."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else Path(os.environ.get("TRADINGAGENTS_DATA_DIR", ".ta_cache"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "audit.log"

    def log(self, session_id: str, event: str, payload: dict | None = None) -> None:
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "event": event,
            "payload": payload or {},
        }
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            LOGGER.warning("audit log write failed: %s", e)

    def tail(self, n: int = 50) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
            return [json.loads(line) for line in lines]
        except Exception:
            return []
