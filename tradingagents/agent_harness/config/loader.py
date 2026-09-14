"""HarnessConfig loader — YAML + env override (v3 spec §3 config/loader.py).

Use ``HarnessConfig.from_yaml(path)`` to load defaults from a YAML file,
then layer environment variables on top (uppercase ``TRADINGAGENTS_*``
keys map to field names — e.g. ``TRADINGAGENTS_DATA_DIR`` → ``data_dir``).

Example YAML::

    data_dir: .ta_cache
    llm_provider: minimax-cn
    llm_model: MiniMax-M3
    retry:
      max_retries: 3
      backoff_seconds: 1.0
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .schema import HarnessConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read YAML with a tiny built-in parser fallback when PyYAML is absent."""
    if not path.exists():
        raise FileNotFoundError(f"HarnessConfig yaml not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _minimal_yaml(text)


def _minimal_yaml(text: str) -> dict[str, Any]:
    """Tiny YAML subset parser: ``key: value`` + 2-space indent for nested dicts.

    Falls back when PyYAML isn't installed (keeps the dependency footprint
    small for environments that only use env-based config).
    """
    out: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    current_indent = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not value:
            if indent > current_indent:
                current = {}
                out[key] = current
                current_indent = indent
            continue
        # Inline scalar (str / int / float / bool).
        if value.lower() in {"true", "false"}:
            parsed: Any = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            parsed = int(value)
        else:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value
        if current is not None and indent > 0:
            current[key] = parsed
        else:
            out[key] = parsed
            current = None
    return out


def _env_overrides() -> dict[str, Any]:
    """Pick known ``HarnessConfig`` fields from env (TRADINGAGENTS_* prefix)."""
    overrides: dict[str, Any] = {}
    for field in HarnessConfig.model_fields:
        env_key = f"TRADINGAGENTS_{field.upper()}"
        value = os.environ.get(env_key)
        if value is None:
            continue
        target_type = HarnessConfig.model_fields[field].annotation
        if target_type in (int, float):
            try:
                overrides[field] = target_type(value)
            except (TypeError, ValueError):
                continue
        elif target_type is bool:
            overrides[field] = value.lower() in {"1", "true", "yes", "on"}
        else:
            overrides[field] = value
    return overrides


def from_yaml(path: str | Path) -> HarnessConfig:
    """Load config from YAML; env overrides apply on top.

    Resolution order (highest priority wins):
      1. Environment variables (``TRADINGAGENTS_<FIELD>``)
      2. YAML file at ``path``
      3. ``HarnessConfig`` defaults
    """
    yaml_data = _load_yaml(Path(path))
    yaml_data.update(_env_overrides())
    return HarnessConfig(**yaml_data)


def from_yaml_with_env_override(yaml_path: str | Path, env_prefix: str = "TRADINGAGENTS_") -> HarnessConfig:
    """Alias kept for forward compatibility with the spec name."""
    return from_yaml(yaml_path)
