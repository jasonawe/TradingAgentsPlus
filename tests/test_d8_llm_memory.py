"""P8 — LLM factory + Memory layer (v3 spec §3 llm/ + memory/)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config.schema import HarnessConfig  # noqa: E402
from tradingagents.agent_harness.harness import Harness  # noqa: E402
from tradingagents.agent_harness.llm import (  # noqa: E402
    ChatMessage,
    LLMFactory,
    LLMProvider,
    LLM_REGISTRY,
    LLMResponse,
    OpenAICompatibleProvider,
    get_default_provider_name,
    set_default_provider,
)
from tradingagents.agent_harness.memory import (  # noqa: E402
    AgentReferencesMemory,
    MemoryEntry,
    MemoryManager,
    MemoryScope,
    SqliteSessionMemory,
    UserPreferencesMemory,
)


# ===========================================================================
# LLM layer
# ===========================================================================


def test_llm_registry_has_all_providers() -> None:
    expected = {
        "openai", "anthropic", "google", "azure", "bedrock",
        "minimax", "minimax-cn", "minimax_cn",
        "ollama", "vllm",
    }
    assert expected.issubset(set(LLM_REGISTRY))


def test_llm_factory_unconfigured_raises() -> None:
    f = LLMFactory()
    assert f.is_configured() is False
    with pytest.raises(ValueError):
        f.make()


def test_llm_factory_make_returns_provider() -> None:
    f = LLMFactory(default_provider="openai", default_model="gpt-4")
    p = f.make()
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.name == "openai"


def test_llm_factory_make_with_explicit_overrides() -> None:
    f = LLMFactory(default_provider="openai", default_model="gpt-4")
    p = f.make(provider="anthropic", model="claude-3")
    assert p.name == "anthropic"
    assert p._model == "claude-3"


def test_llm_set_default_provider_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        set_default_provider("nonexistent")


def test_llm_registry_rejects_duplicate() -> None:
    original = dict(LLM_REGISTRY)
    try:
        with pytest.raises(ValueError):
            from tradingagents.agent_harness.llm.registry import register
            register("openai", lambda **kw: None)
    finally:
        LLM_REGISTRY.clear()
        LLM_REGISTRY.update(original)


def test_chat_message_round_trip() -> None:
    m = ChatMessage(role="user", content="hi")
    assert m.role == "user"
    assert m.content == "hi"


def test_llm_response_defaults() -> None:
    r = LLMResponse(content="ok", provider="openai", model="gpt-4")
    assert r.usage == {}
    assert r.content == "ok"


# ===========================================================================
# Memory layer
# ===========================================================================


def test_memory_entry_defaults() -> None:
    e = MemoryEntry(key="x", value=1, scope=MemoryScope.SESSION)
    assert e.created_at.tzinfo is not None
    assert e.expires_at is None
    assert e.metadata == {}


def test_l1_session_set_get(tmp_path: Path) -> None:
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")
    m.set("foo", "bar", session_id="s1")
    entry = m.get("foo", session_id="s1")
    assert entry is not None
    assert entry.value == "bar"
    assert entry.scope == MemoryScope.SESSION


def test_l1_session_isolates_by_session_id(tmp_path: Path) -> None:
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")
    m.set("foo", "value1", session_id="s1")
    m.set("foo", "value2", session_id="s2")
    assert m.get("foo", session_id="s1").value == "value1"
    assert m.get("foo", session_id="s2").value == "value2"


def test_l1_session_expires(tmp_path: Path) -> None:
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")
    m.set("foo", "v", session_id="s1", ttl_seconds=1)
    assert m.get("foo", session_id="s1") is not None
    import time
    time.sleep(1.5)
    assert m.get("foo", session_id="s1") is None


def test_l1_session_append_message(tmp_path: Path) -> None:
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")
    m.append_message("s1", "user", "hi")
    m.append_message("s1", "assistant", "hello")
    hist = m.get_history("s1")
    assert len(hist) == 2
    assert hist[0]["role"] == "user"
    assert hist[1]["content"] == "hello"


def test_l2_preferences_set_get(tmp_path: Path) -> None:
    m = UserPreferencesMemory(db_path=tmp_path / "l2.sqlite")
    m.set("locale", "zh-CN", session_id="u1")
    entry = m.get("locale", session_id="u1")
    assert entry.value == "zh-CN"
    assert entry.scope == MemoryScope.PREFERENCES


def test_l2_preferences_isolates_by_user(tmp_path: Path) -> None:
    m = UserPreferencesMemory(db_path=tmp_path / "l2.sqlite")
    m.set("locale", "zh-CN", session_id="u1")
    m.set("locale", "en-US", session_id="u2")
    assert m.get("locale", session_id="u1").value == "zh-CN"
    assert m.get("locale", session_id="u2").value == "en-US"


def test_l3_references_set_get(tmp_path: Path) -> None:
    m = AgentReferencesMemory(db_path=tmp_path / "l3.sqlite")
    m.set("quotes:600036.SS", {"price": 99.5}, ttl_seconds=300)
    entry = m.get("quotes:600036.SS")
    assert entry is not None
    assert entry.value["price"] == 99.5
    assert entry.scope == MemoryScope.REFERENCES


def test_l3_references_list_by_kind(tmp_path: Path) -> None:
    m = AgentReferencesMemory(db_path=tmp_path / "l3.sqlite")
    m.set("quotes:600036.SS", {"price": 1})
    m.set("quotes:600000.SS", {"price": 2})
    m.set("fundamentals:600036.SS", {"pe": 10})
    entries = m.list(prefix="quotes:")
    assert len(entries) == 2


# ===========================================================================
# MemoryManager facade
# ===========================================================================


def test_memory_manager_dispatch_by_scope(tmp_path: Path) -> None:
    mgr = MemoryManager(data_dir=str(tmp_path))
    mgr.set("foo", "L1", session_id="s1", scope=MemoryScope.SESSION)
    mgr.set("foo", "L2", session_id="s1", scope=MemoryScope.PREFERENCES)
    mgr.set("foo", "L3", session_id="s1", scope=MemoryScope.REFERENCES)
    assert mgr.get("foo", session_id="s1", scope=MemoryScope.SESSION).value == "L1"
    assert mgr.get("foo", session_id="s1", scope=MemoryScope.PREFERENCES).value == "L2"
    assert mgr.get("foo", session_id="s1", scope=MemoryScope.REFERENCES).value == "L3"


def test_memory_manager_remember_quote(tmp_path: Path) -> None:
    mgr = MemoryManager(data_dir=str(tmp_path))
    mgr.remember_quote("600036.SS", {"price": 99.5, "change_pct": 1.2})
    q = mgr.recall_quote("600036.SS")
    assert q.value["price"] == 99.5


def test_memory_manager_append_and_history(tmp_path: Path) -> None:
    mgr = MemoryManager(data_dir=str(tmp_path))
    mgr.append_message("s1", "user", "hi")
    mgr.append_message("s1", "assistant", "hello")
    hist = mgr.get_history("s1")
    assert len(hist) == 2


# ===========================================================================
# Harness integration
# ===========================================================================


def test_harness_has_llm_factory() -> None:
    h = Harness(HarnessConfig.from_env())
    assert h.llm_factory is not None
    assert h.llm_factory.is_configured() is True


def test_harness_has_memory_manager() -> None:
    h = Harness(HarnessConfig.from_env())
    assert h.memory is not None
    assert h.memory.l1 is not None
    assert h.memory.l2 is not None
    assert h.memory.l3 is not None


def test_harness_memory_can_append_message(tmp_path) -> None:
    """Use an isolated data_dir so other tests don't pollute this session."""
    h = Harness(HarnessConfig.from_env())
    # Monkey-patch the memory layers to use tmp_path.
    from tradingagents.agent_harness.memory import (
        SqliteSessionMemory, UserPreferencesMemory, AgentReferencesMemory,
    )
    h.memory.l1 = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")
    h.memory.l2 = UserPreferencesMemory(db_path=tmp_path / "l2.sqlite")
    h.memory.l3 = AgentReferencesMemory(db_path=tmp_path / "l3.sqlite")
    h.memory.append_message("harness-isolated-session", "user", "hello")
    hist = h.memory.get_history("harness-isolated-session")
    assert len(hist) == 1
    assert hist[0]["content"] == "hello"
