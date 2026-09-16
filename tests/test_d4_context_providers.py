"""§D4 entry-points extension — ContextLayerProvider discovery + assemble.

The new entry-points-ified layer system must:

1. Discover the 8 built-in providers (one per spec §D4 layer).
2. Allow callers to register additional providers via ``register_provider``.
3. In ``assemble()``:
   - explicit ``layers[Layer]`` still wins;
   - otherwise the registered providers contribute, merged by priority
     (later = override on key collision);
   - legacy ``memory`` injection still works when no provider contributed
     for Layer.CHAT/GLOBAL.
4. Builtin provider wire() captures live memory / tool_registry references
   so the harness can plug them in once at init time.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.context import (
    ContextLayerProvider,
    ContextPriority,
    ExplicitLayerProvider,
    Layer,
    SkillsLayerProvider,
    ToolSchemasLayerProvider,
    ChatHistoryLayerProvider,
    UserPreferencesLayerProvider,
    SearchLayerProvider,
    discover_provider_classes,
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discover_provider_classes_returns_eight_builtins() -> None:
    classes = discover_provider_classes()
    names = {c.__name__ for c in classes}
    expected = {
        "ExplicitLayerProvider",
        "SkillsLayerProvider",
        "ToolSchemasLayerProvider",
        "FilesLayerProvider",
        "DashboardLayerProvider",
        "ChatHistoryLayerProvider",
        "UserPreferencesLayerProvider",
        "SearchLayerProvider",
    }
    assert expected.issubset(names)
    assert len(classes) == 8


def test_builtin_providers_each_map_to_one_spec_layer() -> None:
    mapping = {
        ExplicitLayerProvider: Layer.EXPLICIT,
        SkillsLayerProvider: Layer.SKILLS,
        ToolSchemasLayerProvider: Layer.TOOLS,
        ChatHistoryLayerProvider: Layer.CHAT,
        UserPreferencesLayerProvider: Layer.GLOBAL,
        SearchLayerProvider: Layer.SEARCH,
    }
    for cls, layer in mapping.items():
        assert cls.layer is layer, f"{cls.__name__}.layer != {layer}"


# ---------------------------------------------------------------------------
# assemble() provider merge
# ---------------------------------------------------------------------------
class _FakeProvider(ContextLayerProvider):
    """Test double — captures the layer/priority and returns a fixed payload."""

    def __init__(self, layer: Layer, payload: dict, priority: int = 50):
        self.layer = layer
        self.priority = priority
        self._payload = payload
        self.collect_calls: list[tuple] = []

    def collect(self, *, session_id, user_id, context=None):
        self.collect_calls.append((session_id, user_id, context))
        return dict(self._payload)


def test_assemble_collects_from_providers_when_no_explicit_layer() -> None:
    cp = ContextPriority()
    cp.register_provider(_FakeProvider(Layer.SKILLS, {"mcp_skill_count": 3}))
    out = dict(cp.assemble())
    assert out[Layer.SKILLS] == {"mcp_skill_count": 3}


def test_assemble_explicit_layer_overrides_provider() -> None:
    cp = ContextPriority()
    cp.register_provider(_FakeProvider(Layer.SKILLS, {"x": "from-provider"}))
    out = dict(cp.assemble({Layer.SKILLS: {"x": "explicit"}}))
    assert out[Layer.SKILLS] == {"x": "explicit"}


def test_assemble_provider_priority_later_wins_on_key_collision() -> None:
    cp = ContextPriority()
    cp.register_provider(_FakeProvider(Layer.SKILLS, {"k": "low"}, priority=10))
    cp.register_provider(_FakeProvider(Layer.SKILLS, {"k": "high"}, priority=90))
    out = dict(cp.assemble())
    # Lower priority number is iterated first, higher overwrites — so the
    # priority=90 provider's value lands last and wins.
    assert out[Layer.SKILLS]["k"] == "high"


def test_assemble_provider_exception_isolated() -> None:
    class _Boom(ContextLayerProvider):
        layer = Layer.SKILLS
        priority = 50

        def collect(self, *, session_id, user_id, context=None):
            raise RuntimeError("provider kaboom")

    cp = ContextPriority()
    cp.register_provider(_Boom())
    cp.register_provider(_FakeProvider(Layer.SKILLS, {"x": 1}))
    # Should not raise; second provider still contributes.
    out = dict(cp.assemble())
    assert out[Layer.SKILLS] == {"x": 1}


def test_assemble_legacy_memory_still_works_without_providers(tmp_path) -> None:
    """Pre-extension callers pass ContextPriority(memory=...) with no providers;
    the existing _inject_memory path must keep working."""
    from tradingagents.agent_harness.memory import MemoryManager

    mm = MemoryManager(data_dir=str(tmp_path))
    # Fresh MemoryManager with no data.
    cp = ContextPriority(memory=mm)
    out = dict(cp.assemble(session_id="s1", user_id="u1"))
    # With empty memory, no CHAT / GLOBAL layer should appear.
    assert Layer.CHAT not in out or out[Layer.CHAT] == {}
    assert Layer.GLOBAL not in out or out[Layer.GLOBAL] == {}


def test_register_provider_is_idempotent_for_same_instance() -> None:
    cp = ContextPriority()
    p = _FakeProvider(Layer.SKILLS, {"x": 1})
    cp.register_provider(p)
    cp.register_provider(p)
    assert len([x for x in cp.providers if x is p]) == 1


def test_unregister_provider_removes() -> None:
    cp = ContextPriority()
    p = _FakeProvider(Layer.SKILLS, {"x": 1})
    cp.register_provider(p)
    assert cp.unregister_provider(p) is True
    assert cp.unregister_provider(p) is False  # already gone


# ---------------------------------------------------------------------------
# Wire() integration
# ---------------------------------------------------------------------------
def test_chat_provider_wire_captures_memory_and_returns_history() -> None:
    from tradingagents.agent_harness.memory import MemoryManager

    mm = MemoryManager()
    # Force-feed one message into L1 history so collect() returns it.
    mm.l1.append_message("s1", "user", "hi")

    provider = ChatHistoryLayerProvider()
    provider.wire(memory=mm)
    payload = provider.collect(session_id="s1", user_id="u1")
    assert "messages" in payload
    assert payload["messages"][-1]["content"] == "hi"


def test_user_preferences_provider_wire_captures_l2() -> None:
    from tradingagents.agent_harness.memory import MemoryManager

    mm = MemoryManager()
    mm.l2.set("lang", "zh", session_id="u1")

    provider = UserPreferencesLayerProvider()
    provider.wire(memory=mm)
    payload = provider.collect(session_id="s1", user_id="u1")
    assert payload.get("lang") == "zh"


def test_tool_schemas_provider_returns_compact_list() -> None:
    """Layer.TOOLS provider emits {name, description} pairs only — full
    Pydantic schemas would blow the 800-token budget."""

    class _StubTool:
        def __init__(self, name, description):
            self.name = name
            self.description = description

    class _StubReg:
        def list_all(self):
            return [
                _StubTool("get_quote", "Fetch latest quote"),
                _StubTool("add_to_watchlist", "Add a symbol to watchlist"),
            ]

    provider = ToolSchemasLayerProvider()
    provider.wire(tool_registry=_StubReg())
    payload = provider.collect(session_id=None, user_id=None)
    assert payload == {
        "tools": [
            {"name": "get_quote", "description": "Fetch latest quote"},
            {"name": "add_to_watchlist", "description": "Add a symbol to watchlist"},
        ]
    }


def test_discover_providers_wires_live_deps(tmp_path) -> None:
    from tradingagents.agent_harness.memory import MemoryManager

    cp = ContextPriority()
    mm = MemoryManager(data_dir=str(tmp_path))
    mm.l1.append_message("s1", "user", "wired")
    instantiated = cp.discover_providers(memory=mm)
    # All 8 builtins instantiated, none failed.
    assert len(instantiated) == 8
    # Chat provider wired — collect() now returns L1 history.
    chat = next(p for p in cp.providers if isinstance(p, ChatHistoryLayerProvider))
    payload = chat.collect(session_id="s1", user_id="u1")
    assert payload["messages"][-1]["content"] == "wired"


def test_provider_non_dict_payload_is_ignored() -> None:
    class _BadProvider(ContextLayerProvider):
        layer = Layer.SKILLS
        priority = 50

        def collect(self, *, session_id, user_id, context=None):
            return "this is not a dict"

    cp = ContextPriority()
    cp.register_provider(_BadProvider())
    out = cp.assemble()
    # Bad provider contributed nothing; SKILLS layer absent from ordered result.
    assert not any(layer == Layer.SKILLS for layer, _ in out)
