"""W3-D1 E5: SubagentProvider — name → factory registry.

Covers:
  - register decorator + manual forms
  - duplicate name + non-callable validation
  - build kwargs filtering by factory signature (VerifierAgent needs
    judge_factory + enable_l3; standard agents ignore them)
  - unregister + has / __contains__ / __len__ / list_names
  - module-level SUBAGENT_PROVIDER singleton
  - Harness integration: 6 builtin agents wired via SubagentProvider
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.agents import (  # noqa: E402
    BaseAgent,
    PlannerAgent,
    SUBAGENT_PROVIDER,
    SubagentProvider,
    VerifierAgent,
)


def _fresh() -> SubagentProvider:
    return SubagentProvider()


class _StubAgent(BaseAgent):
    name = "stub"
    description = "stub for subagent_provider tests"
    tools: list = []

    async def run(self, input, *, context):  # pragma: no cover - never called
        raise NotImplementedError


def test_register_manual_form() -> None:
    p = _fresh()
    p.register("stub", _StubAgent)
    assert p.has("stub")
    assert "stub" in p
    assert p.list_names() == ["stub"]


def test_register_decorator_form() -> None:
    p = _fresh()

    @p.register("decorated")
    class _Dec(_StubAgent):
        name = "decorated"

    assert p.has("decorated")
    assert _Dec.__name__ == "_Dec"


def test_register_duplicate_name_raises() -> None:
    p = _fresh()
    p.register("x", _StubAgent)
    with pytest.raises(ValueError, match="already registered"):
        p.register("x", _StubAgent)


def test_register_non_callable_raises() -> None:
    p = _fresh()
    with pytest.raises(TypeError, match="must be callable"):
        p.register("bad", "not_callable")  # type: ignore[arg-type]


def test_unregister_returns_true_when_known() -> None:
    p = _fresh()
    p.register("x", _StubAgent)
    assert p.unregister("x") is True
    assert not p.has("x")


def test_unregister_returns_false_when_unknown() -> None:
    p = _fresh()
    assert p.unregister("never_existed") is False


def test_build_returns_instance_with_kwargs() -> None:
    p = _fresh()
    p.register("stub", _StubAgent)
    sentinel = object()
    inst = p.build("stub", llm_factory=sentinel, tool_registry=None)
    assert isinstance(inst, _StubAgent)
    assert inst.llm_factory is sentinel
    assert inst.tool_registry is None


def test_build_unknown_name_raises() -> None:
    p = _fresh()
    with pytest.raises(KeyError, match="not registered"):
        p.build("nonexistent")


def test_build_filters_kwargs_by_signature() -> None:
    p = _fresh()
    p.register("stub", _StubAgent)
    sentinel = object()
    inst = p.build(
        "stub",
        llm_factory=sentinel,
        tool_registry=None,
        judge_factory=object(),
        enable_l3=True,
    )
    assert inst.llm_factory is sentinel
    assert not hasattr(inst, "judge_factory") or inst.judge_factory is None


def test_build_keeps_kwargs_declared_by_factory() -> None:
    p = _fresh()
    p.register("verifier", VerifierAgent)
    judge = object()
    inst = p.build(
        "verifier",
        llm_factory=None,
        tool_registry=None,
        judge_factory=judge,
        enable_l3=True,
    )
    assert inst.judge_factory is judge
    assert inst.enable_l3 is True


def test_len_and_contains() -> None:
    p = _fresh()
    assert len(p) == 0
    p.register("a", _StubAgent)
    p.register("b", _StubAgent)
    assert len(p) == 2
    assert "a" in p and "b" in p
    assert "z" not in p


def test_list_names_is_sorted() -> None:
    p = _fresh()
    for name in ("z", "a", "m", "b"):
        p.register(name, _StubAgent)
    assert p.list_names() == ["a", "b", "m", "z"]


def test_module_singleton_is_usable() -> None:
    assert isinstance(SUBAGENT_PROVIDER, SubagentProvider)
    _ = len(SUBAGENT_PROVIDER)
    _ = SUBAGENT_PROVIDER.list_names()


def test_harness_wires_six_agents_via_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    names = set(h.subagent_provider.list_names())
    assert names == {
        "planner", "verifier", "data_agent",
        "alpha_agent", "news_agent", "synthesizer",
    }
    assert set(h.agent_registry.list()) == names
    verifier = h.agent_registry.get("verifier")
    assert isinstance(verifier, VerifierAgent)
    assert verifier.judge_factory is not None
    assert isinstance(verifier.enable_l3, bool)
    planner = h.agent_registry.get("planner")
    assert isinstance(planner, PlannerAgent)


def test_harness_provider_has_only_builtins_when_no_plugins(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    assert len(h.subagent_provider) == 6
