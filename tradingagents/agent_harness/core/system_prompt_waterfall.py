"""Q5 / P2-8 — Cooperative waterfall for systemPrompt (roadmap §13.9 P1-S2).

dsh's ``SystemPrompt.section / .context / .variable / .tools`` lets
multiple plugins / sources contribute parts of the system prompt and
combines them by **priority** (high priority overrides low). The model
is *cooperative* — no single source owns the whole prompt; instead
each section is independent and the runner glues them together.

Our pre-Q5 ``ContextPriority.assemble()`` is string concatenation in
fixed order, which works for the built-in 6 layers but blocks plugins
from injecting sections at runtime. This module adds the cooperative
version on top:

    sp = SystemPromptWaterfall()
    sp.add(WaterfallSection(name="identity", content="...", priority=100,
                           source="builtin"))
    sp.add(WaterfallSection(name="preferences", content="...", priority=80,
                           source="plugin:quant"))
    sp.add(WaterfallSection(name="market_data", content="...", priority=50,
                           source="plugin:news"))
    prompt = sp.build(context={"session_id": "s1"})

The build pass:
  1. Resolves each section's ``content_callable(context)`` if it's a
     callable (so plugins can compute content lazily — important for
     plugin prompts that depend on the user's last quote or L2 prefs).
  2. Sorts sections by ``priority`` descending.
  3. Concatenates them with consistent header markers so the final
     prompt is debuggable.

Notes:
  - ``ContextPriority`` still owns *layer* ordering (CHAT > GLOBAL >
    TOOLS > …) but **within** the SYSTEM layer, this waterfall runs
    and decides which sections appear.
  - Section contents are arbitrary strings — callers are responsible
    for prompt-injection hygiene (e.g. quoting user prefs).
  - The waterfall does NOT dedupe section names; later-added sections
    with the same name as an earlier one win by priority. Use distinct
    names when in doubt.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


SectionContent = str | Callable[[dict[str, Any]], str]


@dataclass
class WaterfallSection:
    """One contributing slice of the final system prompt."""

    name: str
    content: SectionContent
    priority: int = 50
    source: str = "anonymous"  # who registered the section (plugin name etc.)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("WaterfallSection.name must be non-empty")
        if not isinstance(self.priority, int):
            raise TypeError(f"priority must be int, got {type(self.priority).__name__}")
        # Normalise content to a callable so the build pass can always
        # invoke it with the same context.
        if isinstance(self.content, str):
            self._content_str: str | None = self.content
        elif callable(self.content):
            self._content_str = None
        else:
            raise TypeError(
                f"section.content must be str or callable, got {type(self.content).__name__}"
            )

    def resolve(self, context: dict[str, Any]) -> str:
        if self._content_str is not None:
            return self._content_str
        rv = self.content(context)  # type: ignore[operator]
        if not isinstance(rv, str):
            raise TypeError(
                f"section {self.name!r} callable must return str; got {type(rv).__name__}"
            )
        return rv


@dataclass
class SystemPromptWaterfall:
    """Cooperative system-prompt builder.

    Construct once (typically per Orchestrator instance), ``add`` /
    ``remove`` sections as plugins / preferences change, then call
    ``build(context)`` per LLM call to compose the final prompt.
    """

    sections: list[WaterfallSection] = field(default_factory=list)
    header_template: str = "## {name} (source={source}, priority={priority})"
    separator: str = "\n\n"
    final_prefix: str = ""  # optional intro line at the very top

    def add(self, section: WaterfallSection) -> WaterfallSection:
        self.sections.append(section)
        return section

    def remove(self, name: str) -> int:
        before = len(self.sections)
        self.sections = [s for s in self.sections if s.name != name]
        return before - len(self.sections)

    def clear(self) -> None:
        self.sections.clear()

    def get(self, name: str) -> WaterfallSection | None:
        return next((s for s in self.sections if s.name == name), None)

    def count(self) -> int:
        return len(self.sections)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def build(self, context: dict[str, Any] | None = None) -> str:
        """Compose the final system prompt by stacking sections in
        priority order, highest first. Returns ``""`` if no sections.
        """
        ctx = context or {}
        if not self.sections:
            return self.final_prefix
        # Stable sort by (-priority, original_index) so equal priorities
        # preserve registration order (predictable for tests).
        ordered = sorted(
            enumerate(self.sections),
            key=lambda pair: (-pair[1].priority, pair[0]),
        )
        rendered: list[str] = []
        if self.final_prefix:
            rendered.append(self.final_prefix)
        for _, section in ordered:
            try:
                body = section.resolve(ctx)
            except Exception:
                LOGGER.warning(
                    "section %r (source=%s) failed to resolve; skipping",
                    section.name, section.source, exc_info=True,
                )
                continue
            if not body:
                continue
            header = self.header_template.format(
                name=section.name,
                source=section.source,
                priority=section.priority,
            )
            rendered.append(f"{header}\n{body}")
        return self.separator.join(rendered)


__all__ = ["WaterfallSection", "SystemPromptWaterfall"]
