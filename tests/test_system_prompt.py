"""W3-D2 E4: SystemPrompt — sections + {{var}} + shadow."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.system_prompt import SystemPrompt


# ---------------------------------------------------------------------------
# Section management
# ---------------------------------------------------------------------------


def test_empty_prompt() -> None:
    sp = SystemPrompt()
    assert sp.sections() == []
    assert len(sp) == 0
    assert "role" not in sp


def test_add_and_get() -> None:
    sp = SystemPrompt()
    sp.add("role", "You are a planner.")
    sp.add("format", "Output JSON.")
    assert sp.sections() == ["format", "role"]
    assert sp.get("role") == "You are a planner."
    assert sp.get("missing", "DEFAULT") == "DEFAULT"


def test_add_returns_self_for_chaining() -> None:
    sp = SystemPrompt()
    out = sp.add("a", "x").add("b", "y")
    assert out is sp
    assert sp.sections() == ["a", "b"]


def test_add_rejects_empty_name() -> None:
    sp = SystemPrompt()
    with pytest.raises(ValueError, match="non-empty string"):
        sp.add("", "body")


def test_add_rejects_non_string_body() -> None:
    sp = SystemPrompt()
    with pytest.raises(TypeError, match="must be a string"):
        sp.add("role", 123)  # type: ignore[arg-type]


def test_add_replaces_existing() -> None:
    sp = SystemPrompt()
    sp.add("role", "v1")
    sp.add("role", "v2")
    assert sp.get("role") == "v2"
    assert len(sp) == 1


def test_remove_returns_true_when_known() -> None:
    sp = SystemPrompt()
    sp.add("x", "y")
    assert sp.remove("x") is True
    assert "x" not in sp


def test_remove_returns_false_when_unknown() -> None:
    sp = SystemPrompt()
    assert sp.remove("never_added") is False


def test_copy_is_independent() -> None:
    sp = SystemPrompt().add("role", "v1")
    cp = sp.copy()
    cp.add("role", "v2")
    assert sp.get("role") == "v1"
    assert cp.get("role") == "v2"


# ---------------------------------------------------------------------------
# Render / variable interpolation
# ---------------------------------------------------------------------------


def test_render_sorts_sections_by_default() -> None:
    sp = SystemPrompt()
    sp.add("z", "Z body")
    sp.add("a", "A body")
    sp.add("m", "M body")
    out = sp.render()
    assert out == "A body\n\nM body\n\nZ body"


def test_render_with_explicit_order() -> None:
    sp = SystemPrompt()
    sp.add("a", "A body")
    sp.add("z", "Z body")
    sp.add("m", "M body")
    out = sp.render(order=["z", "a"])
    # Explicit order prefix; unlisted ("m") appended sorted.
    assert out == "Z body\n\nA body\n\nM body"


def test_render_skips_missing_in_explicit_order() -> None:
    sp = SystemPrompt()
    sp.add("a", "A body")
    out = sp.render(order=["doesnt_exist", "a"])
    assert out == "A body"


def test_interpolate_simple_variable() -> None:
    sp = SystemPrompt().add("role", "You are {{name}}")
    assert sp.render({"name": "Alice"}) == "You are Alice"


def test_interpolate_multiple_variables() -> None:
    sp = SystemPrompt().add("fmt", "{{greeting}}, {{target}}!")
    out = sp.render({"greeting": "Hi", "target": "Bob"})
    assert out == "Hi, Bob!"


def test_interpolate_with_whitespace_in_braces() -> None:
    sp = SystemPrompt().add("r", "{{  name  }}")
    assert sp.render({"name": "X"}) == "X"


def test_interpolate_missing_variable_left_intact() -> None:
    """A missing var stays as ``{{var}}`` so the LLM can see the gap
    rather than silently receiving empty content.
    """
    sp = SystemPrompt().add("r", "Hello {{name}}, age {{age}}")
    out = sp.render({"name": "Bob"})
    assert out == "Hello Bob, age {{age}}"


def test_interpolate_empty_string_is_valid() -> None:
    sp = SystemPrompt().add("r", "Hello [{{tag}}] end")
    assert sp.render({"tag": ""}) == "Hello [] end"


def test_interpolate_coerces_non_string() -> None:
    sp = SystemPrompt().add("r", "Count: {{n}}")
    assert sp.render({"n": 42}) == "Count: 42"


def test_interpolate_supports_dotted_names() -> None:
    sp = SystemPrompt().add("r", "Hello {{user.name}}")
    out = sp.render({"user.name": "Alice"})
    assert out == "Hello Alice"


def test_render_without_variables_keeps_braces() -> None:
    sp = SystemPrompt().add("r", "Use {{tool}}")
    assert sp.render() == "Use {{tool}}"


def test_render_custom_separator() -> None:
    sp = SystemPrompt()
    sp.add("a", "A")
    sp.add("b", "B")
    assert sp.render(separator=" | ") == "A | B"


# ---------------------------------------------------------------------------
# Shadow inheritance
# ---------------------------------------------------------------------------


def test_shadow_child_overrides_parent_on_collision() -> None:
    parent = SystemPrompt().add("role", "parent role").add("format", "JSON")
    child = SystemPrompt().add("format", "Markdown")
    merged = child.shadow(parent)
    assert merged.get("role") == "parent role"
    assert merged.get("format") == "Markdown"


def test_shadow_keeps_all_parent_sections() -> None:
    parent = SystemPrompt()
    parent.add("a", "A").add("b", "B").add("c", "C")
    child = SystemPrompt().add("b", "BB")
    merged = child.shadow(parent)
    assert merged.sections() == ["a", "b", "c"]


def test_shadow_is_independent() -> None:
    parent = SystemPrompt().add("role", "p")
    child = SystemPrompt().add("role", "c")
    merged = child.shadow(parent)
    merged.add("extra", "X")
    assert "extra" not in parent
    assert "extra" not in child
    assert "extra" in merged


def test_shadow_chain() -> None:
    base = SystemPrompt().add("role", "base")
    mid = SystemPrompt().add("format", "JSON")
    top = SystemPrompt().add("format", "Markdown")
    # top.shadow(mid).shadow(base) — top wins format, base fills role
    merged = top.shadow(mid).shadow(base)
    assert merged.get("role") == "base"
    assert merged.get("format") == "Markdown"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_repr_lists_sections() -> None:
    sp = SystemPrompt().add("a", "x").add("b", "y")
    assert "a" in repr(sp) and "b" in repr(sp)
