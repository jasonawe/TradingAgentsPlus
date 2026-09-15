"""SystemPrompt — section-based prompt builder with variable interpolation (W3-D2 E4).

dsh pattern::

    SystemPrompt.section  -> named text blocks (role / format / tools / ...)
    SystemPrompt.context  -> runtime data (conversation state, user prefs)
    SystemPrompt.variable -> {{var}} interpolation
    SystemPrompt.tools    -> tool list (or import in context)
    SystemPrompt.shadow   -> child inherits + can override parent sections

Why this exists
---------------
Today every ``BaseAgent`` declares a single ``system_prompt: str`` class
attribute. Want to change "format" for one user? You rewrite the whole
prompt. Want to share "role" across all agents but customise "tools"
per agent? You copy-paste.

SystemPrompt gives you:
  - named sections (add / get / list)
  - ``{{var}}`` interpolation in render (missing vars are left intact,
    not silently replaced with empty string)
  - shadow: ``child.shadow(parent)`` creates a new prompt where child
    sections win on collision, parent sections fill the rest
  - deterministic section ordering (sorted by name by default, or pass
    an explicit order)

Minimal but spec-complete. Does NOT replace ``ContextPriority`` (the
8-layer LLM context composer); the two are complementary — agents
``system_prompt = SystemPrompt(...)`` and ``ContextPriority`` builds
the per-turn user-side context.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


class SystemPrompt:
    """Named sections + variable interpolation + shadow inheritance.

    Sections are added via :meth:`add` and rendered via :meth:`render`.
    Use :meth:`shadow` to derive a child prompt that overrides a subset
    of the parent's sections.
    """

    def __init__(self) -> None:
        self._sections: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Section management
    # ------------------------------------------------------------------
    def add(self, name: str, body: str) -> "SystemPrompt":
        """Add or replace a section. Returns ``self`` for chaining."""
        if not isinstance(name, str) or not name:
            raise ValueError("section name must be a non-empty string")
        if not isinstance(body, str):
            raise TypeError(f"section body for {name!r} must be a string")
        self._sections[name] = body
        return self

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._sections.get(name, default)

    def remove(self, name: str) -> bool:
        return self._sections.pop(name, None) is not None

    def sections(self) -> list[str]:
        return sorted(self._sections)

    def has(self, name: str) -> bool:
        return name in self._sections

    def __contains__(self, name: str) -> bool:
        return name in self._sections

    def __len__(self) -> int:
        return len(self._sections)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    def render(
        self,
        variables: dict[str, Any] | None = None,
        *,
        order: Iterable[str] | None = None,
        separator: str = "\n\n",
    ) -> str:
        """Render the prompt, interpolating ``{{var}}`` in each section.

        ``order``: explicit section order. Defaults to sorted names.
        ``variables``: dict used to substitute ``{{var}}`` occurrences.
        Unknown variables are left intact (``{{missing}}`` stays
        literally) so the LLM can see the gap instead of silently
        receiving empty content.

        Sections not listed in ``order`` are appended after the listed
        ones in sorted order, so the explicit order is a "prefix" not
        an exhaustive list.
        """
        if order is None:
            chosen = sorted(self._sections)
        else:
            chosen = list(order)
            extras = sorted(set(self._sections) - set(chosen))
            chosen.extend(extras)

        parts: list[str] = []
        for name in chosen:
            if name not in self._sections:
                continue
            body = _interpolate(self._sections[name], variables or {})
            parts.append(body)
        return separator.join(parts)

    def to_string(
        self, variables: dict[str, Any] | None = None,
    ) -> str:
        """Render with default order (sorted by section name)."""
        return self.render(variables)

    # ------------------------------------------------------------------
    # Inheritance
    # ------------------------------------------------------------------
    def shadow(self, parent: "SystemPrompt") -> "SystemPrompt":
        """Return a NEW prompt where child's sections win on collision.

        Parent's sections fill in everything child doesn't define. Useful
        for "shared base role + agent-specific tools" without copy-paste.

        The returned prompt is independent — mutating it does NOT affect
        either parent or child.
        """
        merged = SystemPrompt()
        for name, body in parent._sections.items():
            merged.add(name, body)
        for name, body in self._sections.items():
            merged.add(name, body)
        return merged

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return f"SystemPrompt(sections={self.sections()})"

    def copy(self) -> "SystemPrompt":
        new = SystemPrompt()
        for name, body in self._sections.items():
            new.add(name, body)
        return new


def _interpolate(text: str, variables: dict[str, Any]) -> str:
    """Replace ``{{name}}`` with ``variables[name]`` (str-coerced).

    Unknown names are left as ``{{name}}`` so the caller can see the
    gap. Empty string is a valid value — pass ``""`` explicitly to
    blank a slot.
    """
    if not variables:
        return text
    return _VAR_RE.sub(
        lambda m: str(variables[m.group(1)])
        if m.group(1) in variables
        else m.group(0),
        text,
    )
