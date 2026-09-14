"""Tool schema contracts (v3 spec §5.2)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    """Retry policy applied per-tool when the call raises a transient error."""

    max_retries: int = 2
    backoff_seconds: float = 0.5
    exponential: bool = True


class ToolSchema(BaseModel):
    """Standard contract every tool must declare."""

    name: str
    description: str
    args_schema: type
    result_schema: type
    permission: str = "read"
    timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 60
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    model_config = {"arbitrary_types_allowed": True}
