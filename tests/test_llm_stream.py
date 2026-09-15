"""W3-D6 R4: LLM streaming protocol — 7 chunk types + stream/stream_text."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    STREAM_KINDS,
    StreamChunk,
)


# ---------------------------------------------------------------------------
# 7 chunk kinds
# ---------------------------------------------------------------------------


def test_stream_kinds_count() -> None:
    assert len(STREAM_KINDS) == 7


@pytest.mark.parametrize("kind", STREAM_KINDS)
def test_each_kind_in_stream_kinds(kind: str) -> None:
    assert kind in STREAM_KINDS


def test_stream_chunk_default_fields() -> None:
    c = StreamChunk(kind="text_delta", content="hello")
    assert c.kind == "text_delta"
    assert c.content == "hello"
    assert c.args is None
    assert c.result is None
    assert c.usage == {}
    assert c.blocks == []
    assert c.ts == 0.0


def test_stream_chunk_to_dict_roundtrip() -> None:
    c = StreamChunk(kind="tool_call", name="get_quote", args={"symbol": "AAPL"})
    d = c.to_dict()
    assert d["kind"] == "tool_call"
    assert d["name"] == "get_quote"
    assert d["args"] == {"symbol": "AAPL"}


# ---------------------------------------------------------------------------
# Stub provider for testing the fallback path
# ---------------------------------------------------------------------------


class _StubProvider(LLMProvider):
    """Minimal LLMProvider that returns canned content."""

    name = "stub"

    def __init__(self, content: str = "stub response", usage: dict | None = None) -> None:
        self._content = content
        self._usage = usage or {}

    def complete(
        self, messages, *, temperature=0.0, max_tokens=None, stop=None,
    ) -> LLMResponse:
        return LLMResponse(
            content=self._content, provider=self.name, model="stub-model",
            usage=self._usage,
        )


# ---------------------------------------------------------------------------
# stream() fallback path
# ---------------------------------------------------------------------------


def test_stream_fallback_emits_three_chunks() -> None:
    p = _StubProvider(content="hello world", usage={"input_tokens": 5, "output_tokens": 2})
    chunks = list(p.stream([]))
    kinds = [c.kind for c in chunks]
    assert kinds == ["text_delta", "usage", "finish"]


def test_stream_fallback_text_delta_carries_content() -> None:
    p = _StubProvider(content="the answer is 42")
    chunks = list(p.stream([]))
    assert chunks[0].content == "the answer is 42"


def test_stream_fallback_usage_chunk() -> None:
    usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    p = _StubProvider(usage=usage)
    chunks = list(p.stream([]))
    assert chunks[1].usage == usage


def test_stream_fallback_finish_reason() -> None:
    p = _StubProvider()
    chunks = list(p.stream([]))
    assert chunks[-1].kind == "finish"
    assert chunks[-1].reason == "stop"


def test_stream_fallback_no_usage_chunk_when_empty() -> None:
    p = _StubProvider(content="x")  # no usage
    chunks = list(p.stream([]))
    kinds = [c.kind for c in chunks]
    # Only text_delta + finish (no usage chunk when usage is empty)
    assert kinds == ["text_delta", "finish"]


def test_stream_fallback_preserves_temperature() -> None:
    """The fallback calls ``complete`` with the same kwargs; verify
    they propagate so a custom override can use them.
    """
    class CaptureProvider(_StubProvider):
        captured_kwargs: dict = {}

        def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
            CaptureProvider.captured_kwargs = {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stop": stop,
            }
            return super().complete(messages, temperature=temperature)

    p = CaptureProvider()
    list(p.stream([], temperature=0.7, max_tokens=100, stop=["END"]))
    assert CaptureProvider.captured_kwargs == {
        "temperature": 0.7, "max_tokens": 100, "stop": ["END"],
    }


# ---------------------------------------------------------------------------
# stream_text convenience
# ---------------------------------------------------------------------------


def test_stream_text_wraps_prompt() -> None:
    p = _StubProvider(content="reply")
    chunks = list(p.stream_text("hello", system="be brief"))
    # 1 text_delta + 1 finish
    assert chunks[0].kind == "text_delta"
    assert chunks[0].content == "reply"


def test_stream_text_no_system_no_usage() -> None:
    p = _StubProvider(content="hi")
    chunks = list(p.stream_text("hi"))
    assert [c.kind for c in chunks] == ["text_delta", "finish"]


# ---------------------------------------------------------------------------
# Subclass override — proper streaming
# ---------------------------------------------------------------------------


class _WordStreamProvider(LLMProvider):
    """A provider that streams word-by-word to test subclass overrides."""

    name = "word-stream"

    def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        return LLMResponse(content="hello world there", provider=self.name, model="m")

    def stream(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        for word in self.complete([]).content.split():
            yield StreamChunk(kind="text_delta", content=word + " ")
        yield StreamChunk(kind="finish", reason="stop")


def test_subclass_stream_override_emits_incremental() -> None:
    p = _WordStreamProvider()
    chunks = list(p.stream([]))
    text_chunks = [c for c in chunks if c.kind == "text_delta"]
    assert [c.content for c in text_chunks] == ["hello ", "world ", "there "]
    assert chunks[-1].kind == "finish"


# ---------------------------------------------------------------------------
# 7-kind exhaustiveness (all kinds can be constructed)
# ---------------------------------------------------------------------------


def test_all_seven_kinds_constructable() -> None:
    StreamChunk(kind="text_delta", content="x")
    StreamChunk(kind="tool_call", name="t", args={"a": 1})
    StreamChunk(kind="tool_result", name="t", result={"r": 1})
    StreamChunk(kind="finish", reason="stop")
    StreamChunk(kind="error", error="boom", code="E_X")
    StreamChunk(kind="usage", usage={"input_tokens": 1})
    StreamChunk(kind="block", blocks=[{"type": "text", "content": "x"}])
