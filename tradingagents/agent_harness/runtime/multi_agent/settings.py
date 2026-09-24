"""Phase 1 + Phase 2 runtime settings.

``RuntimeSettings`` is a Pydantic ``BaseModel`` so the range constraints
(``ge``, ``le``) auto-raise on out-of-range values. We alias
``SettingsError`` to :class:`pydantic.ValidationError` so callers (and
tests) can catch a stable, repo-local name regardless of which library
emits the validation error.
"""
from __future__ import annotations
from pydantic import BaseModel, Field, ValidationError

# §0.4.35 phase 2 (Work unit 4) — alias for pydantic.ValidationError.
# Stable surface for `pytest.raises(SettingsError)` and orchestrator
# fall-through paths that catch validation failures.
SettingsError = ValidationError


class RuntimeSettings(BaseModel):
    multi_agent: bool = False
    llm_budget_per_turn: int = Field(default=5, ge=1, le=50)
    max_hops: int = Field(default=8, ge=1, le=64)
    # §0.4.35 phase 2 — nested-consult guard.
    # ``ge=1`` so the executor always has a budget of at least 1 consult.
    consultation_max_depth: int = Field(default=3, ge=1, le=10)
    # Range per plan / spec §4.2: ``[0.0, 0.8]``. Pydantic ``ge=0.0, le=0.8``
    # auto-raises ``ValidationError`` (aliased as ``SettingsError``).
    consultation_rate_limit: float = Field(default=0.5, ge=0.0, le=0.8)

def load_settings() -> RuntimeSettings:
    """Read runtime settings from the live dataflows config."""
    from tradingagents.dataflows.config import get_config
    cfg = get_config()
    block = cfg.get("runtime", {}) or {}
    return RuntimeSettings(**block)
