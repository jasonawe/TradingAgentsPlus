"""AgentScope — per-agent runtime isolation (W3-D4 E7).

dsh pattern: every sub-agent gets its own scope (LLM provider override,
tool restriction, custom variables). A scope can ``shadow`` a parent
scope — child wins on collision, parent fills the rest.

Why this exists
---------------
Today every agent inherits ``harness.llm_factory`` and shares
``harness.tool_registry`` directly. Want one agent to use a
cheaper LLM? You have to mutate the agent. Want one agent to NOT
see ``create_alert`` (write tool)? You have to pop the tool from
the registry globally.

AgentScope gives each agent a private view:

  scope.allowed_tools(all)         -> [\"get_quote\", ...]  (filtered)
  scope.llm_overrides()            -> {provider, model}    (or inherited)
  scope.variables                 -> {\"user_tier\": \"pro\"}
  scope.shadow(parent_scope)       -> a new derived scope

``SubagentProvider`` (W3-D1 E5) instantiates agents with their scope
already attached; the orchestrator / harness tools consult the scope
to enforce restrictions.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

LOGGER = logging.getLogger(__name__)


class AgentScope:
    """Per-agent runtime view of LLM, tools and variables.

    None of the three are mandatory. When a field is ``None`` on the
    child, it falls back to the parent (recursive). When the root has
    ``None``, the field is "unrestricted" (use the harness default).
    """

    def __init__(
        self,
        *,
        name: str = "default",
        tools: Optional[Iterable[str]] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        variables: Optional[dict[str, Any]] = None,
        parent: Optional["AgentScope"] = None,
    ) -> None:
        self.name = name
        # ``None`` = inherit; concrete list (even empty) = restrict
        self._tools: Optional[list[str]] = list(tools) if tools is not None else None
        self._llm_provider: Optional[str] = llm_provider
        self._llm_model: Optional[str] = llm_model
        self._variables: dict[str, Any] = dict(variables or {})
        self.parent: Optional["AgentScope"] = parent

    # ------------------------------------------------------------------
    # Tool restriction
    # ------------------------------------------------------------------
    def allowed_tools(self, all_tools: Iterable[str]) -> list[str]:
        """Return the list of tool names this agent may invoke.

        ``None`` (no restriction set) means "inherit from parent"; a
        parent of ``None`` (root) means "use everything". An explicit
        empty list means "no tools at all". A non-empty list is the
        whitelist (intersected with the harness's full set so we
        never accidentally expose a tool that doesn't exist).
        """
        own = self._tools
        if own is None:
            if self.parent is not None:
                return self.parent.allowed_tools(all_tools)
            return list(all_tools)
        # Own list set — restrict to what's in the harness's full set
        all_set = set(all_tools)
        return [t for t in own if t in all_set]

    def restrict_tools(self, tool_names: Iterable[str]) -> None:
        """Replace the tool whitelist on this scope (in-place)."""
        self._tools = list(tool_names)

    def is_tool_allowed(self, name: str, all_tools: Iterable[str]) -> bool:
        return name in self.allowed_tools(all_tools)

    # ------------------------------------------------------------------
    # LLM overrides
    # ------------------------------------------------------------------
    def llm_overrides(self) -> dict[str, Optional[str]]:
        """Return the effective LLM override for this scope.

        Resolution order: self → parent → ``{}`` (no override, use
        the harness default). Both fields are independent — you can
        override just the provider, just the model, or both.
        """
        provider = self._llm_provider
        model = self._llm_model
        if (provider is None and model is None) and self.parent is not None:
            return self.parent.llm_overrides()
        return {"provider": provider, "model": model}

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------
    def get_variable(self, name: str, default: Any = None) -> Any:
        """Get a variable, walking the parent chain on miss."""
        if name in self._variables:
            return self._variables[name]
        if self.parent is not None:
            return self.parent.get_variable(name, default)
        return default

    def set_variable(self, name: str, value: Any) -> None:
        self._variables[name] = value

    @property
    def variables(self) -> dict[str, Any]:
        """Return own variables only (no parent merge) for inspection."""
        return dict(self._variables)

    def merged_variables(self) -> dict[str, Any]:
        """Return own + parent variables (own wins on collision)."""
        if self.parent is None:
            return dict(self._variables)
        out = self.parent.merged_variables()
        out.update(self._variables)
        return out

    # ------------------------------------------------------------------
    # Inheritance
    # ------------------------------------------------------------------
    def shadow(self, parent: "AgentScope") -> "AgentScope":
        """Return a NEW scope where this scope's values win on collision.

        ``None`` fields DO override parent — they're treated as the
        explicit "no override" marker. To inherit a field, leave it
        as ``None`` on the child AND don't pass it through the
        constructor; the new scope will look it up the parent chain
        at access time.
        """
        new = AgentScope(
            name=self.name,
            tools=self._tools,            # concrete list or None
            llm_provider=self._llm_provider,
            llm_model=self._llm_model,
            variables=self._variables,
            parent=parent,
        )
        return new

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        overrides = self.llm_overrides()
        return (
            f"AgentScope(name={self.name!r}, "
            f"tools={'inherit' if self._tools is None else len(self._tools)}, "
            f"llm={overrides['provider'] or 'inherit'}/"
            f"{overrides['model'] or 'inherit'}, "
            f"vars={len(self._variables)})"
        )
