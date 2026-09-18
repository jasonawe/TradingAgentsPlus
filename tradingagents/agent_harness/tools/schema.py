"""Tool schema contracts (v3 spec §5.2)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    """Retry policy applied per-tool when the call raises a transient error."""

    max_retries: int = 2
    backoff_seconds: float = 0.5
    exponential: bool = True


class ToolSchema(BaseModel):
    """Standard contract every tool must declare.

    Step 23 (D2 Tool refactor) — extended surface:

    - ``metadata`` is the unified bag for capability tags, the
      user-facing display_view hint, lifecycle hook names, error
      normalization hints, etc. The harness reads
      ``metadata["capabilities"]`` / ``metadata["display_view"]``
      but the bag is open so individual callers can add keys without
      schema churn.
    """

    name: str
    description: str
    args_schema: type
    result_schema: type
    permission: str = "read"
    timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 60
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}
