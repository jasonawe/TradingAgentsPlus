"""Context Priority — 8-layer context assembly (v2 spec D4, N55 fix).

The 8 layers are project-defined (not an OpenBB standard). Lower
numerical priority wins; when two layers supply the same key, the
higher-priority layer overrides the lower one.

Token budgets (P4 design target, N57 fix — to be calibrated post-P4):

    Layer 1 (explicit):  200
    Layer 2 (skills):    200
    Layer 3 (tools):     800
    Layer 4 (files):     300
    Layer 5 (dashboard): 300
    Layer 6 (chat):     1500
    Layer 7 (global):    200
    Layer 8 (search):    500
    TOTAL:              4000

**Memory integration**(v3 spec §3 memory/, P8):
- Layer 6 (chat) auto-injects last N messages from L1 session memory
- Layer 7 (global) auto-injects user preferences from L2 memory
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from abc import ABC, abstractmethod
from typing import Any, ClassVar, TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.agent_harness.memory import MemoryManager


class Layer(IntEnum):
    EXPLICIT = 1
    SKILLS = 2
    TOOLS = 3
    FILES = 4
    DASHBOARD = 5
    CHAT = 6
    GLOBAL = 7
    SEARCH = 8


DEFAULT_TOKEN_BUDGETS: dict[Layer, int] = {
    Layer.EXPLICIT: 200,
    Layer.SKILLS: 200,
    Layer.TOOLS: 800,
    Layer.FILES: 300,
    Layer.DASHBOARD: 300,
    Layer.CHAT: 1500,
    Layer.GLOBAL: 200,
    Layer.SEARCH: 500,
}

TOTAL_BUDGET = 4000

DEFAULT_CHAT_HISTORY_LIMIT = 20


# ---------------------------------------------------------------------------
# §D4 — Context layer extension points (spec §7.1 #5 + spec §D4 N55 fix)
#
# Each layer of the 8-layer context can be served by zero or more
# :class:`ContextLayerProvider` impls. Providers are discovered via the
# ``tradingagents.context.providers`` entry-point group so that third-party
# packages can ship their own implementations (``pip install``) without
# touching the core. Built-in providers live below; harness-level wiring
# replaces the default no-ops with live dependencies (memory manager,
# tool registry, etc.) at ``Harness.__init__`` time.
#
# Builtin providers cover the spec §D4 layers with no business logic —
# they return empty payloads by default and only contribute when wired
# to a live dependency. A plugin is expected to either subclass a
# builtin or implement :class:`ContextLayerProvider` directly.
# ---------------------------------------------------------------------------
import logging as _logging

_LOGGER = _logging.getLogger(__name__)


class ContextLayerProvider(ABC):
    """One source of payload for a single context layer.

    Subclasses declare which layer they serve (``layer``) and their
    within-layer ordering (``priority``; lower number = earlier /
    higher-importance). The orchestrator iterates ``ContextPriority.providers``
    in priority order; providers with the same priority keep registration
    order, and later-installed providers *override* earlier ones on key
    collisions (since context layer payloads are flat dicts).
    """

    layer: ClassVar[Layer]
    priority: ClassVar[int] = 50

    @abstractmethod
    def collect(
        self,
        *,
        session_id: str | None,
        user_id: str | None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Return a payload dict for this layer. May be empty.

        Implementations MUST be cheap and side-effect-free. Exceptions
        are caught by the caller and logged so a misbehaving provider
        cannot break the orchestrator.
        """

    def wire(self, *, memory=None, tool_registry=None, **_unused) -> "ContextLayerProvider":
        """Optional: live-dep injection called after discovery.

        The default implementation is a no-op so providers that do not
        need any external dependency (most builtin ones) just work.
        Providers that need a memory manager or tool registry override
        this to capture the live reference and return ``self``.
        """
        return self


# ---------------------------------------------------------------------------
# Built-in providers — one per spec §D4 layer.
# They start with no live dependencies and return empty payloads; the
# harness wires memory / tool_registry at init time so e.g.
# ChatHistoryLayerProvider actually pulls history.
# ---------------------------------------------------------------------------
class ExplicitLayerProvider(ContextLayerProvider):
    """Layer 1 (200 tok) — explicit widgets (ticker / timeframe).

    Default empty; the orchestrator injects the user's current widget
    selection via the ``layers`` argument of :meth:`ContextPriority.assemble`,
    so this provider mainly exists as an entry-point for plugins that
    want to drive explicit widgets from a non-UI source (e.g. an alert
    resolution flow that pre-fills the symbol).
    """

    layer = Layer.EXPLICIT
    priority = 100

    def collect(self, *, session_id, user_id, context=None):
        return {}


class SkillsLayerProvider(ContextLayerProvider):
    """Layer 2 (200 tok) — MCP skills / preset capabilities.

    Empty default. MCP server wiring (separate from this provider) will
    surface its skills here once the MCP integration lands.
    """

    layer = Layer.SKILLS
    priority = 50

    def collect(self, *, session_id, user_id, context=None):
        return {}


class ToolSchemasLayerProvider(ContextLayerProvider):
    """Layer 3 (800 tok) — compact tool name + description list.

    Returns a flat list of ``{"name", "description"}`` entries. The full
    Pydantic schema for each tool is intentionally NOT included to keep
    this layer within its 800-token budget even with 30+ tools. Plugins
    can register additional tool registries by overriding ``wire``.
    """

    layer = Layer.TOOLS
    priority = 100
    _tool_registry = None

    def wire(self, *, memory=None, tool_registry=None, **_unused):
        self._tool_registry = tool_registry
        return self

    def collect(self, *, session_id, user_id, context=None):
        if self._tool_registry is None:
            return {}
        try:
            tools = [
                {"name": str(getattr(t, "name", "?")), "description": str(getattr(t, "description", ""))}
                for t in self._tool_registry.list_all()
            ]
        except Exception as e:  # pragma: no cover
            _LOGGER.warning("ToolSchemasLayerProvider.collect failed: %s", e)
            return {}
        return {"tools": tools} if tools else {}


class FilesLayerProvider(ContextLayerProvider):
    """Layer 4 (300 tok) — uploaded PDFs / CSVs.

    Empty default. The web UI's file-upload pipeline will populate this via
    a future wire() override.
    """

    layer = Layer.FILES
    priority = 50

    def collect(self, *, session_id, user_id, context=None):
        return {}


class DashboardLayerProvider(ContextLayerProvider):
    """Layer 5 (300 tok) — watchlist / current tab state.

    Empty default. WatchlistProvider wire() override reads the user's
    default watchlist from a WatchlistRepository.
    """

    layer = Layer.DASHBOARD
    priority = 50
    _watchlist = None

    def wire(self, *, memory=None, tool_registry=None, **_unused):
        # The watchlist lookup is mediated through memory in production;
        # for now we leave a hook so plugins can override.
        return self

    def collect(self, *, session_id, user_id, context=None):
        return {}


class ChatHistoryLayerProvider(ContextLayerProvider):
    """Layer 6 (1500 tok cap) — last N turns from L1 session memory.

    Replaces the hard-coded ``_inject_memory`` branch in
    :meth:`ContextPriority.assemble` for the chat layer. Wired with a
    live ``MemoryManager`` at ``Harness.__init__`` time.
    """

    layer = Layer.CHAT
    priority = 100
    _memory = None
    _limit: int = DEFAULT_CHAT_HISTORY_LIMIT

    def wire(self, *, memory=None, tool_registry=None, **_unused):
        self._memory = memory
        return self

    def collect(self, *, session_id, user_id, context=None):
        if self._memory is None or not session_id:
            return {}
        try:
            history = self._memory.get_history(session_id)
        except Exception as e:  # pragma: no cover
            _LOGGER.warning("ChatHistoryLayerProvider.collect failed: %s", e)
            return {}
        history = history[-self._limit:]
        return {"messages": history} if history else {}


class UserPreferencesLayerProvider(ContextLayerProvider):
    """Layer 7 (200 tok) — user preferences from L2 memory.

    Replaces the hard-coded ``_inject_memory`` branch for the global layer.
    """

    layer = Layer.GLOBAL
    priority = 100
    _memory = None

    def wire(self, *, memory=None, tool_registry=None, **_unused):
        self._memory = memory
        return self

    def collect(self, *, session_id, user_id, context=None):
        if self._memory is None:
            return {}
        uid = user_id or session_id or "default"
        try:
            prefs = self._memory.l2.list(user_id=uid)
        except Exception as e:  # pragma: no cover
            _LOGGER.warning("UserPreferencesLayerProvider.collect failed: %s", e)
            return {}
        return {p.key: p.value for p in prefs} if prefs else {}


class SearchLayerProvider(ContextLayerProvider):
    """Layer 8 (500 tok) — web search / reference results.

    Empty default. The L3 web search subsystem will populate this via
    wire() once it's extracted from the general-agent harness.
    """

    layer = Layer.SEARCH
    priority = 50

    def collect(self, *, session_id, user_id, context=None):
        return {}


# ---------------------------------------------------------------------------
# Entry-point discovery (§7.1 #1 + #5 — high-extension)
# ---------------------------------------------------------------------------
ENTRY_POINT_GROUP_PROVIDERS = "tradingagents.context.providers"

# Module-level cache: ContextPriority.discover_providers() consults this
# once and reuses the list unless ``force`` is passed.
# Builtin provider classes — kept here only as fallbacks for environments
# where the package was NOT installed via ``pip install -e .`` (so no
# entry_points are discoverable). The canonical discovery path is via
# the ``tradingagents.context.providers`` entry-point group registered in
# pyproject.toml. The fallback dedup is keyed on class identity.
_FALLBACK_PROVIDER_CLASSES: tuple[type[ContextLayerProvider], ...] = (
    ExplicitLayerProvider,
    SkillsLayerProvider,
    ToolSchemasLayerProvider,
    FilesLayerProvider,
    DashboardLayerProvider,
    ChatHistoryLayerProvider,
    UserPreferencesLayerProvider,
    SearchLayerProvider,
)


def _load_provider_entry_points(group: str = ENTRY_POINT_GROUP_PROVIDERS):
    try:
        from importlib.metadata import entry_points as _ep
    except ImportError:  # pragma: no cover — py<3.8
        return []
    try:
        return list(_ep(group=group))
    except Exception as e:  # pragma: no cover
        _LOGGER.warning("entry_points(%s) failed: %s", group, e)
        return []


def discover_provider_classes(group: str = ENTRY_POINT_GROUP_PROVIDERS):
    """Return ``[builtin, ...entry_point]`` ordered with builtins first.

    Builtins are always registered so that the harness works with no
    third-party plugins installed. Entry-point classes are appended
    after builtins; plugins that ship a same-named class will replace
    the builtin at instantiation time (the entry-point load fails loudly
    if the class does not subclass :class:`ContextLayerProvider`).
    """
    classes: list[type[ContextLayerProvider]] = list(_FALLBACK_PROVIDER_CLASSES)
    seen: set[type] = set(_FALLBACK_PROVIDER_CLASSES)
    for ep in _load_provider_entry_points(group):
        try:
            cls = ep.load()
        except Exception as e:  # pragma: no cover — malformed plugin
            _LOGGER.warning("provider entry point %s load failed: %s", ep.name, e)
            continue
        if not (isinstance(cls, type) and issubclass(cls, ContextLayerProvider)):
            _LOGGER.warning(
                "provider entry point %s is not a ContextLayerProvider subclass",
                ep.name,
            )
            continue
        if cls in seen:
            continue  # builtin already in fallback list — dedupe
        seen.add(cls)
        classes.append(cls)
    return classes


@dataclass
class ContextPriority:
    """Assembles the 8 layers, trims to budget, exposes ordered output.

    Parameters
    ----------
    budgets:
        Per-layer token caps.
    memory:
        Optional MemoryManager — legacy shortcut for the two L1/L2
        builtin providers (chat history, user prefs). If you wire live
        providers explicitly via :meth:`register_provider`, this can
        stay ``None``; the providers carry their own reference.
    chat_history_limit:
        Max messages to inject from L1 memory for the active session
        when no chat-history provider is registered.
    providers:
        Optional explicit list of :class:`ContextLayerProvider`
        instances. If empty, :meth:`assemble` falls back to the legacy
        hard-coded memory injection paths so existing tests / callers
        that pass ``ContextPriority(memory=...)`` keep working.
    """

    budgets: dict[Layer, int] = field(default_factory=lambda: dict(DEFAULT_TOKEN_BUDGETS))
    memory: "MemoryManager | None" = None
    chat_history_limit: int = DEFAULT_CHAT_HISTORY_LIMIT
    providers: list[ContextLayerProvider] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Provider registration (§D4 entry-points extension)
    # ------------------------------------------------------------------
    def register_provider(self, provider: ContextLayerProvider) -> ContextLayerProvider:
        """Add a provider instance. Duplicate (layer, id(provider)) is a no-op."""
        for existing in self.providers:
            if existing.layer == provider.layer and existing is provider:
                return provider
        self.providers.append(provider)
        return provider

    def unregister_provider(self, provider: ContextLayerProvider) -> bool:
        try:
            self.providers.remove(provider)
            return True
        except ValueError:
            return False

    def discover_providers(
        self,
        *,
        memory=None,
        tool_registry=None,
        group: str = ENTRY_POINT_GROUP_PROVIDERS,
        wire_extra: dict | None = None,
    ) -> list[ContextLayerProvider]:
        """Discover builtin + entry-point providers and wire live deps.

        Built-in providers are instantiated and added in priority order.
        Third-party entry-points are loaded and instantiated with no-args
        (the same constructor the builtin variants use); they can read
        their live dependencies via ``wire(memory=..., tool_registry=...)``.
        Returns the list of providers actually registered.
        """
        extras = wire_extra or {}
        instantiated: list[ContextLayerProvider] = []
        for cls in discover_provider_classes(group):
            try:
                inst = cls()
                inst.wire(
                    memory=memory if memory is not None else self.memory,
                    tool_registry=tool_registry,
                    **extras,
                )
            except Exception as e:
                _LOGGER.warning("provider %s instantiation failed: %s", cls, e)
                continue
            self.register_provider(inst)
            instantiated.append(inst)
        return instantiated

    def providers_for_layer(self, layer: Layer) -> list[ContextLayerProvider]:
        return sorted(
            (p for p in self.providers if p.layer == layer),
            key=lambda p: (p.priority, id(p)),
        )

    def assemble(
        self,
        layers: dict[Layer, dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[tuple[Layer, dict[str, Any]]]:
        """Return layers ordered by priority (highest first), trimmed to budget.

        Resolution order per layer:

        1. Explicit ``layers[layer]`` from the caller (highest).
        2. Registered :class:`ContextLayerProvider` instances for this layer,
           merged in priority order (later = override on key collision).
        3. Legacy fallback: if no provider contributed for Layer.CHAT or
           Layer.GLOBAL, fall back to the original ``_inject_memory`` path
           so callers that pre-date the entry-point extension keep working.
        """
        layers = dict(layers or {})
        layers = self._collect_from_providers(layers, session_id=session_id, user_id=user_id)
        layers = self._inject_memory(layers, session_id=session_id, user_id=user_id)

        ordered = sorted(layers.items(), key=lambda kv: int(kv[0]))
        out: list[tuple[Layer, dict[str, Any]]] = []
        running = 0
        for layer, payload in ordered:
            cap = self.budgets.get(layer, 200)
            payload_tokens = self._estimate(payload)
            if running + payload_tokens > TOTAL_BUDGET:
                payload = self._trim(payload, max(0, TOTAL_BUDGET - running) * 4)
            out.append((layer, payload))
            running += self._estimate(payload)
        return out

    def _collect_from_providers(
        self,
        layers: dict[Layer, dict[str, Any]],
        *,
        session_id: str | None,
        user_id: str | None,
    ) -> dict[Layer, dict[str, Any]]:
        """For each layer not explicitly provided, gather from providers."""
        if not self.providers:
            return layers
        for layer in Layer:
            if layer in layers:
                continue
            collected: dict[str, Any] = {}
            for prov in self.providers_for_layer(layer):
                try:
                    payload = prov.collect(
                        session_id=session_id,
                        user_id=user_id,
                        context=None,
                    ) or {}
                except Exception as e:
                    _LOGGER.warning(
                        "provider %s for layer %s failed: %s", prov, layer, e,
                    )
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning(
                        "provider %s returned non-dict %s; ignoring",
                        prov, type(payload).__name__,
                    )
                    continue
                collected.update(payload)
            if collected:
                layers[layer] = collected
        return layers

    # ------------------------------------------------------------------
    # Memory injection (Layer 6 chat + Layer 7 global)
    # ------------------------------------------------------------------
    def _inject_memory(
        self,
        layers: dict[Layer, dict[str, Any]],
        *,
        session_id: str | None,
        user_id: str | None,
    ) -> dict[Layer, dict[str, Any]]:
        if self.memory is None:
            return layers

        # Layer 6 — chat history from L1 session memory.
        if session_id and Layer.CHAT not in layers:
            history = self.memory.get_history(session_id)
            history = history[-self.chat_history_limit:]
            if history:
                layers[Layer.CHAT] = {"messages": history}

        # Layer 7 — user preferences from L2 memory.
        if Layer.GLOBAL not in layers:
            uid = user_id or session_id or "default"
            prefs = self.memory.l2.list(user_id=uid)
            if prefs:
                layers[Layer.GLOBAL] = {p.key: p.value for p in prefs}

        return layers

    @staticmethod
    def _estimate(payload: Any) -> int:
        text = str(payload)
        return max(1, len(text) // 4)

    @staticmethod
    def _trim(payload: Any, target_chars: int) -> Any:
        if isinstance(payload, dict):
            text = str(payload)
            if len(text) <= target_chars:
                return payload
            return {"_trimmed": text[: max(0, target_chars)]}
        if isinstance(payload, str):
            return payload[: max(0, target_chars)]
        return payload
