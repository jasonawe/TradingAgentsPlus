from __future__ import annotations
from pydantic import BaseModel, Field

class RuntimeSettings(BaseModel):
    multi_agent: bool = False
    llm_budget_per_turn: int = Field(default=5, ge=1, le=50)
    max_hops: int = Field(default=8, ge=1, le=64)
    consultation_rate_limit: float = Field(default=0.5, ge=0.0, le=0.8)

def load_settings() -> RuntimeSettings:
    """Read runtime settings from the live dataflows config."""
    from tradingagents.dataflows.config import get_config
    cfg = get_config()
    block = cfg.get("runtime", {}) or {}
    return RuntimeSettings(**block)
