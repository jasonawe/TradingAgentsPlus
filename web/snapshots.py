"""Immutable, filesystem-backed data snapshots for reproducible web runs."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from langchain_core.messages import BaseMessage, message_to_dict
except ImportError:  # pragma: no cover - langchain-core is a project dependency.
    BaseMessage = None
    message_to_dict = None

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - pydantic is supplied by LangChain.
    BaseModel = None


class SnapshotCorruptError(RuntimeError):
    """Raised when a snapshot path, manifest, or payload fails validation."""


def _normalize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        dt = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, Enum):
        return _normalize(value.value)
    if BaseMessage is not None and isinstance(value, BaseMessage):
        if message_to_dict is None:  # pragma: no cover - guarded by the import above.
            raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
        return _normalize(message_to_dict(value))
    if BaseModel is not None and isinstance(value, BaseModel):
        try:
            dumped = value.model_dump(mode="json")
        except TypeError:
            dumped = value.model_dump()
        return _normalize(dumped)
    if isinstance(value, dict):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if value.__class__.__module__.split(".", 1)[0] == "numpy" and hasattr(value, "item"):
        return _normalize(value.item())
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def canonical_bytes(value: Any, *, kind: str = "json") -> bytes:
    """Serialize supported payloads deterministically as UTF-8 bytes."""
    if kind == "text":
        return str(value).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if kind == "csv":
        rows = list(csv.DictReader(str(value).replace("\r\n", "\n").replace("\r", "\n").splitlines()))
        value = rows
    normalized = _normalize(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SnapshotStore:
    """Read/write snapshots below one controlled root directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_root(self, run_id: str) -> Path:
        self._safe_component(run_id)
        return self.root / "snapshots" / run_id

    def manifest_path(self, run_id: str) -> Path:
        return self.run_root(run_id) / "manifest.json"

    def resolve_payload(self, payload_ref: str) -> Path:
        candidate = Path(payload_ref)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SnapshotCorruptError("invalid snapshot path")
        raw_path = self.root / candidate
        if raw_path.is_symlink():
            raise SnapshotCorruptError("snapshot symlink is not allowed")
        resolved = raw_path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SnapshotCorruptError("snapshot path escapes root") from exc
        return resolved

    def read_manifest(self, run_id: str) -> dict[str, Any]:
        path = self.manifest_path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptError("snapshot manifest is unavailable") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("run_id") != run_id:
            raise SnapshotCorruptError("snapshot manifest schema is invalid")
        expected = data.get("manifest_hash")
        unsigned = dict(data)
        unsigned.pop("manifest_hash", None)
        if expected != _sha256(canonical_bytes(unsigned)):
            raise SnapshotCorruptError("snapshot manifest hash mismatch")
        return data

    def read_dataset(self, run_id: str, entry: dict[str, Any]) -> Any:
        if entry.get("run_id") not in (None, run_id):
            raise SnapshotCorruptError("snapshot run mismatch")
        path = self.resolve_payload(str(entry.get("payload_ref", "")))
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SnapshotCorruptError("snapshot payload is unavailable") from exc
        if _sha256(raw) != entry.get("sha256"):
            raise SnapshotCorruptError("snapshot payload hash mismatch")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptError("snapshot payload is invalid") from exc

    @staticmethod
    def _safe_component(value: str) -> None:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("invalid snapshot identifier")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class DataSnapshotRecorder:
    def __init__(self, store: SnapshotStore, run_id: str, *, provider_chain: list[str] | None = None):
        self.store = store
        self.run_id = run_id
        self.provider_chain = provider_chain or []
        self._entries: list[dict[str, Any]] = []
        self._finalized = False
        manifest = self.store.manifest_path(run_id)
        if manifest.exists():
            existing = self.store.read_manifest(run_id)
            self._entries = list(existing.get("datasets", []))
            self._finalized = True

    def record(
        self,
        dataset_key: str,
        payload: Any,
        *,
        provider: str | None = None,
        symbol: str | None = None,
        request_fingerprint: str | None = None,
        status: str = "complete",
        freshness: str = "fresh",
        kind: str = "json",
        error: str | None = None,
    ) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("snapshot manifest is finalized")
        if not dataset_key or "/" in dataset_key or "\\" in dataset_key:
            raise ValueError("invalid dataset key")
        raw = canonical_bytes(payload, kind=kind)
        relative = Path("snapshots") / self.run_id / "datasets" / f"{dataset_key}.json"
        self.store._atomic_write(self.store.root / relative, raw)
        entry = {
            "run_id": self.run_id,
            "dataset": dataset_key,
            "symbol": symbol,
            "provider": provider,
            "provider_chain": self.provider_chain,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "freshness": freshness,
            "status": status,
            "payload_ref": relative.as_posix(),
            "sha256": _sha256(raw),
            "request_fingerprint": request_fingerprint,
            "error": error,
        }
        self._entries = [item for item in self._entries if not (item.get("dataset") == dataset_key and item.get("request_fingerprint") == request_fingerprint)]
        self._entries.append(entry)
        return entry

    def finalize(self) -> dict[str, Any]:
        if self._finalized:
            return self.store.read_manifest(self.run_id)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest = {
            "schema_version": 1,
            "id": f"snapshot-{self.run_id}",
            "run_id": self.run_id,
            "created_at": now,
            "completed_at": now,
            "datasets": self._entries,
        }
        manifest["manifest_hash"] = _sha256(canonical_bytes(manifest))
        self.store._atomic_write(self.store.manifest_path(self.run_id), canonical_bytes(manifest))
        self._finalized = True
        return manifest


class SnapshotAwareDataProvider:
    """Serve a dataset from an immutable snapshot before invoking upstream."""

    def __init__(self, snapshot_store: SnapshotStore, upstream_provider: Callable[..., Any], recorder: DataSnapshotRecorder, run_id: str):
        self.store = snapshot_store
        self.upstream_provider = upstream_provider
        self.recorder = recorder
        self.run_id = run_id

    def get_dataset(self, dataset_key: str, request_fingerprint: str, *, provider: str | None = None, **kwargs: Any) -> Any:
        for entry in self.recorder._entries:
            if entry.get("dataset") == dataset_key and entry.get("request_fingerprint") == request_fingerprint:
                return self.store.read_dataset(self.run_id, entry)
        if self.store.manifest_path(self.run_id).exists():
            manifest = self.store.read_manifest(self.run_id)
            for entry in manifest.get("datasets", []):
                if entry.get("dataset") == dataset_key and entry.get("request_fingerprint") == request_fingerprint:
                    return self.store.read_dataset(self.run_id, entry)
        payload = self.upstream_provider(**kwargs) if kwargs else self.upstream_provider()
        self.recorder.record(dataset_key, payload, provider=provider, request_fingerprint=request_fingerprint)
        return payload


__all__ = ["SnapshotStore", "DataSnapshotRecorder", "SnapshotAwareDataProvider", "SnapshotCorruptError", "canonical_bytes"]
