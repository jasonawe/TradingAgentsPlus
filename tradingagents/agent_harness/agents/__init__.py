"""Sub-agent layer — P5 (v3 spec §4)."""
from .base import AgentContext, AgentInput, AgentResult, BaseAgent
from .registry import AgentRegistry
from .subagent_provider import (
    AgentFactory,
    SUBAGENT_PROVIDER,
    SubagentProvider,
    register as register_subagent,
)
from .planner import PlannerAgent
from .verifier import VerifierAgent
from .data_agent import DataAgent
from .alpha_agent import AlphaAgent
from .news_agent import NewsAgent
from .synthesizer import SynthesizerAgent

__all__ = [
    "AgentContext",
    "AgentInput",
    "AgentResult",
    "BaseAgent",
    "AgentRegistry",
    "AgentFactory",
    "SUBAGENT_PROVIDER",
    "SubagentProvider",
    "register_subagent",
    "PlannerAgent",
    "VerifierAgent",
    "DataAgent",
    "AlphaAgent",
    "NewsAgent",
    "SynthesizerAgent",
]
