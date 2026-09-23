"""§0.4.30 — Tool catalog builder for the DeepSeek-style LLM router.

DeepSeek-Harness pattern: each tool registers ``{name, description,
parameters, output, execute}`` directly into a context; the tool
schema joins prompt assembly without an intermediate (intent, op)
classification layer. The LLM picks ``tool + args`` in one shot,
outputting a plan array ``[{tool, args, parallel_group?}, ...]``.

This module is the read-side of that pattern: it walks the existing
``ToolRegistry``, harvests every tool's ``name`` + ``description`` +
``args_schema`` (Pydantic -> JSON-Schema), and emits a JSON-ready
catalog the router embeds as part of the system prompt.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)


class ToolCatalogEntry:
    """Single tool, projected to the fields the router cares about."""

    __slots__ = (
        "name",
        "description",
        "parameters",
        "is_write",
        "concurrency_safe",
        "display_view",
        "category",
    )

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        is_write: bool,
        concurrency_safe: bool,
        display_view: str | None = None,
        category: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.is_write = is_write
        self.concurrency_safe = concurrency_safe
        self.display_view = display_view
        self.category = category

    def to_prompt_block(self, *, max_desc_chars: int = 600) -> str:
        """Render a markdown block the router embeds in its system prompt."""
        desc = (self.description or "").strip()
        if len(desc) > max_desc_chars:
            desc = desc[: max_desc_chars - 3] + "..."
        flags: list[str] = []
        if self.is_write:
            flags.append("WRITE (HITL gated)")
        if not self.concurrency_safe:
            flags.append("SERIAL")
        flag_str = " · ".join(flags)
        header = f"### `{self.name}`"
        if flag_str:
            header += f"  *[{flag_str}]*"
        return f"{header}\n{desc}\nParameters: {self.parameters}"


def _is_pydantic_model(t: Any) -> bool:
    """Best-effort check for a Pydantic BaseModel class."""
    if t is None or t is type(None):
        return False
    return hasattr(t, "model_json_schema") and hasattr(t, "model_fields")


def pydantic_to_json_schema(args_schema: type) -> dict[str, Any]:
    """Convert a Pydantic ``args_schema`` to JSON-Schema for the prompt."""
    if isinstance(args_schema, dict):
        return args_schema
    if not _is_pydantic_model(args_schema):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    try:
        schema = args_schema.model_json_schema()
    except Exception as e:
        LOGGER.debug("model_json_schema failed for %s: %s", args_schema, e)
        return {"type": "object", "properties": {}, "additionalProperties": True}
    return _trim_json_schema(schema)


def _trim_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip Pydantic-only keys (``$defs``, ``title``, ``$ref``) from a schema."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    trimmed_props: dict[str, Any] = {}
    for key, val in properties.items():
        if not isinstance(val, dict):
            trimmed_props[key] = {"type": "string"}
            continue
        clean = {
            k: v for k, v in val.items()
            if k in ("type", "description", "enum", "default", "items", "anyOf")
        }
        if "anyOf" in clean and isinstance(clean["anyOf"], list):
            non_null = [
                x for x in clean["anyOf"]
                if not (isinstance(x, dict) and x.get("type") == "null")
            ]
            if non_null:
                clean = {
                    k: v for k, v in non_null[0].items()
                    if k in ("type", "description", "enum", "items")
                }
        trimmed_props[key] = clean
    return {"type": "object", "properties": trimmed_props, "required": required}


def _derive_is_write(tool: Any) -> bool:
    """Read ``permission`` from the tool, fall back to schema metadata."""
    perm = getattr(tool, "permission", None)
    if perm is None:
        perm = getattr(getattr(tool, "schema", None), "permission", "read")
    perm_value = perm.value if hasattr(perm, "value") else perm
    return str(perm_value).lower() == "write"


def _derive_concurrency_safe(tool: Any) -> bool:
    """Default: read = safe, write = serial."""
    schema = getattr(tool, "schema", None)
    metadata = getattr(schema, "metadata", None) or {}
    if "concurrency_safe" in metadata:
        return bool(metadata["concurrency_safe"])
    return not _derive_is_write(tool)


def build_catalog(registry: Any) -> list[ToolCatalogEntry]:
    """Walk ``registry.list_all()`` and emit one catalog entry per tool."""
    entries: list[ToolCatalogEntry] = []
    skipped = 0
    # Tolerate registries that expose list_names + get but not list_all
    # (a few legacy tests mock a minimal registry).
    if hasattr(registry, "list_all"):
        all_tools = list(registry.list_all())
    else:
        try:
            names = list(registry.list_names())
        except Exception:
            names = []
        all_tools = []
        for nm in names:
            try:
                all_tools.append(registry.get(nm))
            except Exception as e:
                LOGGER.debug("catalog: registry.get(%s) failed: %s", nm, e)
    for tool in all_tools:
        try:
            schema = getattr(tool, "schema", None)
            args_schema = getattr(schema, "args_schema", None) if schema else None
            params = pydantic_to_json_schema(args_schema)
            is_write = _derive_is_write(tool)
            metadata = getattr(schema, "metadata", None) or {}
            entry = ToolCatalogEntry(
                name=tool.name,
                description=tool.description or "",
                parameters=params,
                is_write=is_write,
                concurrency_safe=_derive_concurrency_safe(tool),
                display_view=metadata.get("display_view"),
                category=metadata.get("category"),
            )
            entries.append(entry)
        except Exception as e:
            skipped += 1
            LOGGER.debug("catalog skip %s: %s", getattr(tool, "name", "?"), e)
    if skipped:
        LOGGER.warning("catalog: skipped %d tools during build", skipped)
    entries.sort(key=lambda e: e.name)
    return entries


def catalog_to_prompt(entries: Iterable[ToolCatalogEntry]) -> str:
    """Render the catalog as a markdown block the router pastes into its prompt."""
    blocks = [entry.to_prompt_block() for entry in entries]
    return "\n\n".join(blocks)


def get_tool_names(entries: Iterable[ToolCatalogEntry]) -> set[str]:
    """Set of all known tool names — used by the router to validate LLM output."""
    return {entry.name for entry in entries}


__all__ = [
    "ToolCatalogEntry",
    "build_catalog",
    "catalog_to_prompt",
    "get_tool_names",
    "pydantic_to_json_schema",
]
