"""ContextPriority + MemoryManager integration tests (P8 Task 2).

Verifies Layer 6 (chat) auto-injects session history from L1 memory,
and Layer 7 (global) auto-injects user preferences from L2 memory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.context import ContextPriority, Layer  # noqa: E402
from tradingagents.agent_harness.memory import MemoryManager  # noqa: E402


@pytest.fixture
def tmp_memory() -> MemoryManager:
    tmp = tempfile.mkdtemp()
    return MemoryManager(data_dir=tmp)


def test_context_assembles_8_layers_in_priority_order() -> None:
    cp = ContextPriority()
    layers = {
        Layer.CHAT: {"messages": []},
        Layer.EXPLICIT: {"ticker": "600036.SS"},
        Layer.GLOBAL: {"pref": "value"},
    }
    out = cp.assemble(layers)
    priorities = [int(l) for l, _ in out]
    assert priorities == sorted(priorities)


def test_context_injects_chat_history_from_memory(tmp_memory: MemoryManager) -> None:
    """Layer 6 auto-populates from L1 session history when memory is set."""
    tmp_memory.append_message("s1", "user", "600036 多少钱")
    tmp_memory.append_message("s1", "assistant", "现价 99.5")

    cp = ContextPriority(memory=tmp_memory)
    out = cp.assemble(session_id="s1")
    layer_6 = next(payload for layer, payload in out if layer == Layer.CHAT)
    assert "messages" in layer_6
    assert len(layer_6["messages"]) == 2
    assert layer_6["messages"][0]["role"] == "user"


def test_context_injects_global_prefs_from_memory(tmp_memory: MemoryManager) -> None:
    """Layer 7 auto-populates from L2 user prefs when memory is set."""
    tmp_memory.set("locale", "zh-CN", user_id="u1", scope=__import__("tradingagents.agent_harness.memory", fromlist=["MemoryScope"]).MemoryScope.PREFERENCES)
    tmp_memory.set("theme", "dark", user_id="u1", scope=__import__("tradingagents.agent_harness.memory", fromlist=["MemoryScope"]).MemoryScope.PREFERENCES)

    cp = ContextPriority(memory=tmp_memory)
    out = cp.assemble(user_id="u1")
    layer_7 = next(payload for layer, payload in out if layer == Layer.GLOBAL)
    assert layer_7 == {"locale": "zh-CN", "theme": "dark"}


def test_context_explicit_layer_overrides_memory(tmp_memory: MemoryManager) -> None:
    """Explicit Layer 6 payload wins over memory-injected default."""
    tmp_memory.append_message("s1", "user", "from memory")

    cp = ContextPriority(memory=tmp_memory)
    out = cp.assemble(
        {Layer.CHAT: {"messages": [{"role": "user", "content": "explicit"}]}},
        session_id="s1",
    )
    layer_6 = next(payload for layer, payload in out if layer == Layer.CHAT)
    assert layer_6["messages"][0]["content"] == "explicit"


def test_context_works_without_memory() -> None:
    """ContextPriority still works when no memory is wired."""
    cp = ContextPriority(memory=None)
    out = cp.assemble({Layer.EXPLICIT: {"ticker": "X"}})
    assert any(layer == Layer.EXPLICIT for layer, _ in out)


def test_context_chat_history_limit(tmp_memory: MemoryManager) -> None:
    """chat_history_limit caps the number of messages injected."""
    for i in range(50):
        tmp_memory.append_message("s1", "user", f"msg-{i}")

    cp = ContextPriority(memory=tmp_memory, chat_history_limit=5)
    out = cp.assemble(session_id="s1")
    layer_6 = next(payload for layer, payload in out if layer == Layer.CHAT)
    assert len(layer_6["messages"]) == 5
    # Most recent messages should be retained.
    assert layer_6["messages"][-1]["content"] == "msg-49"


def test_context_injects_both_chat_and_global(tmp_memory: MemoryManager) -> None:
    """Both Layer 6 + Layer 7 auto-injected in same assemble() call."""
    from tradingagents.agent_harness.memory import MemoryScope

    tmp_memory.append_message("sess", "user", "hi")
    # L2 is user-scoped; pass user_id (not session_id) for PREFERENCES.
    tmp_memory.set("locale", "en-US", user_id="u1", scope=MemoryScope.PREFERENCES)

    cp = ContextPriority(memory=tmp_memory)
    out = cp.assemble(session_id="sess", user_id="u1")

    layer_6 = next(payload for layer, payload in out if layer == Layer.CHAT)
    layer_7 = next(payload for layer, payload in out if layer == Layer.GLOBAL)
    assert len(layer_6["messages"]) == 1
    assert layer_7 == {"locale": "en-US"}
