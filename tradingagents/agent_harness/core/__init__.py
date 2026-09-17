"""Core orchestrator layer — P4 (v3 spec §6, v2 spec D1+D5+D6).

Holds:
- tier router (D1 three-tier routing)
- short_circuit (Tier 1 server-side execution, no LLM)
- orchestrator (Tier 2 5-node state machine)
- context (D4 8-layer Context Priority)
- verification (D6 three-tier verification)
- retry (retry + circuit breaker)
"""
from .context import ContextPriority, Layer
from .orchestrator import Orchestrator, OrchestratorState
from .plan_template import PlanTemplateCache, normalize_message
from .session_manager import SessionManager
from .prefetch import (
    PlanPredictor, PrefetchResult, PrefetchStats, Prefetcher,
)
from .rate_limiter import (
    PROVIDER_RATE_LIMITS, RateLimiter, get_rate_limiter, register_provider_rate_limit,
)
from .template import (
    DEFAULT_TEMPLATES, TEMPLATE_TRIGGER_KEYWORDS, TemplateEngine, should_use_template,
)
from .timeout_enforcer import (
    CallTimeoutError, TimeoutEnforcer, TimeoutStats, with_timeout,
)
from .retry import CircuitBreaker, CircuitState, RetryPolicy, retry_async
from .short_circuit import ShortCircuit
from .tier import Tier, classify_intent, fast_route
from .verification import VerificationLevel, Verifier
from .event_bus import EventBus, _StopPropagation
from .system_prompt import SystemPrompt
from .agent_scope import AgentScope

__all__ = [
    "SessionManager",

    "ContextPriority",
    "Layer",
    "Orchestrator",
    "OrchestratorState",
    "PlanTemplateCache",
    "normalize_message",
    "PlanPredictor",
    "PrefetchResult",
    "PrefetchStats",
    "Prefetcher",
    "RateLimiter",
    "PROVIDER_RATE_LIMITS",
    "get_rate_limiter",
    "register_provider_rate_limit",
    "TemplateEngine",
    "DEFAULT_TEMPLATES",
    "TEMPLATE_TRIGGER_KEYWORDS",
    "should_use_template",
    "CallTimeoutError",
    "TimeoutEnforcer",
    "TimeoutStats",
    "with_timeout",
    "CircuitBreaker",
    "RetryPolicy",
    "retry_async",
    "ShortCircuit",
    "Tier",
    "classify_intent",
    "fast_route",
    "VerificationLevel",
    "EventBus",
    "_StopPropagation",
    "SystemPrompt",
    "AgentScope",
    "Verifier",
]
