"""Step 33 — WorkflowSpecRegistry.

The YAML loader (workflow_yaml.load_workflow_yaml) accepts a
``handlers`` dict mapping names to callables. This module:

- Bundles the canonical handlers used by the built-in YAML specs.
- Discovers *.yaml files under tradingagents/agent_harness/workflows/specs/.
- Caches loaded Workflow instances so repeated calls are cheap.
- Exposes a small API for web/app.py to mount.

Why a registry and not raw dict passing
----------------------------------------
The orchestrator's bound methods (``orchestrator._observe`` etc.)
are *instance-bound*, so the YAML loader can't import them via a
module path. The registry wraps the orchestrator at runtime and
exposes them under stable aliases (``observe`` / ``verify`` /
``synthesize`` / etc.) so YAML specs are stable across refactors.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .workflow import NodeResult, Workflow
from .workflow_yaml import load_workflow_yaml, spec_to_yaml_text

LOGGER = logging.getLogger(__name__)

#: Default location for workflow YAML specs. Override at construction
#: time for tests.
DEFAULT_SPECS_DIR = (
    Path(__file__).resolve().parent.parent / "workflows" / "specs"
)


def _async_observe(orchestrator: Any, state: dict[str, Any]) -> NodeResult:
    """Adapter for ``Orchestrator._observe`` (sync)."""
    stats = orchestrator._observe(state["orch_state"])
    state["observe_stats"] = stats
    return NodeResult(state, emit=[("observed", stats)])


async def _async_verify(orchestrator: Any, state: dict[str, Any]) -> NodeResult:
    """Adapter for ``Orchestrator._verify`` (async)."""
    result = await orchestrator._verify(state["orch_state"])
    state["verify_result"] = result
    emit = [("verified", {
        "ok": result.ok,
        "level": (
            result.level.value if hasattr(result.level, "value")
            else str(result.level)
        ),
        "details": result.details or {},
    })]
    return NodeResult(state, emit=emit)


async def _async_synthesize(
    orchestrator: Any, state: dict[str, Any]
) -> NodeResult:
    """Adapter for ``Orchestrator._synthesize`` (async)."""
    final = await orchestrator._synthesize(state["orch_state"])
    state["final"] = final
    return NodeResult(state, emit=[], halt=True)





async def _async_quote(
    orchestrator: Any, state: dict[str, Any]
) -> NodeResult:
    """Adapter: fetch quote via the orchestrator's tool pipeline."""
    symbol = state.get("symbol") or (
        state.get("orch_state").symbols[0]
        if state.get("orch_state") and state["orch_state"].symbols
        else ""
    )
    try:
        result = await orchestrator._call_tool(
            "get_quote", {"symbol": symbol}
        )
    except Exception as exc:  # noqa: BLE001 — surface as data
        return NodeResult(state, emit=[("quote_fetch_error", {"symbol": symbol, "error": repr(exc)})])
    return NodeResult(state, emit=[("quote_fetched", result)])


async def _async_news(
    orchestrator: Any, state: dict[str, Any]
) -> NodeResult:
    """Adapter: fetch news via the orchestrator's tool pipeline."""
    symbol = state.get("symbol") or (
        state["orch_state"].symbols[0]
        if state.get("orch_state") and state["orch_state"].symbols
        else ""
    )
    lookback = int(state.get("lookback_days", 7))
    try:
        result = await orchestrator._call_tool(
            "get_news", {"symbol": symbol, "lookback_days": lookback}
        )
    except Exception as exc:
        return NodeResult(state, emit=[("news_fetch_error", {"symbol": symbol, "error": repr(exc)})])
    return NodeResult(state, emit=[("news_fetched", result)])


async def _async_fundamentals(
    orchestrator: Any, state: dict[str, Any]
) -> NodeResult:
    """Adapter: fetch fundamentals via the orchestrator's tool pipeline."""
    symbol = state.get("symbol") or (
        state["orch_state"].symbols[0]
        if state.get("orch_state") and state["orch_state"].symbols
        else ""
    )
    try:
        result = await orchestrator._call_tool(
            "get_fundamentals", {"symbol": symbol}
        )
    except Exception as exc:
        return NodeResult(state, emit=[("fundamentals_fetch_error", {"symbol": symbol, "error": repr(exc)})])
    return NodeResult(state, emit=[("fundamentals_fetched", result)])


async def _async_aggregate(
    orchestrator: Any, state: dict[str, Any]
) -> NodeResult:
    """Adapter: aggregate parallel fetch results."""
    emits = []
    payload = {}
    for key in ("quote", "news", "fundamentals"):
        v = state.get(key)
        if isinstance(v, dict) and "emit" in v:
            for ev, p in v["emit"]:
                emits.append((ev, p))
                payload[ev] = p
    state["aggregated"] = payload
    return NodeResult(state, emit=emits + [("aggregated", payload)], halt=True)


def _noop(state: dict[str, Any]) -> NodeResult:
    """No-op end node — halt the workflow."""
    return NodeResult(state, emit=[("done", {})], halt=True)


class WorkflowSpecRegistry:
    """Registry of YAML workflow definitions bound to an orchestrator.

    Usage::

        registry = WorkflowSpecRegistry(orchestrator)
        wf = registry.get("post-execute")    # -> Workflow
        names = registry.list()              # -> [str, ...]
    """

    def __init__(
        self,
        orchestrator: Any,
        specs_dir: Path | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._specs_dir = Path(specs_dir) if specs_dir else DEFAULT_SPECS_DIR
        # Canonical handler name -> callable(state) -> NodeResult
        self._handlers: dict[str, Callable[[dict[str, Any]], NodeResult]] = {
            "observe": lambda s: _async_observe(orchestrator, s),
            "verify": lambda s: _async_verify(orchestrator, s),
            "synthesize": lambda s: _async_synthesize(orchestrator, s),
            "noop": _noop,
            "quote": lambda s: _async_quote(orchestrator, s),
            "news": lambda s: _async_news(orchestrator, s),
            "fundamentals": lambda s: _async_fundamentals(orchestrator, s),
            "aggregate": lambda s: _async_aggregate(orchestrator, s),
        }
        # Lazy cache: spec_name -> Workflow
        self._cache: dict[str, Workflow | list[str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        """List available YAML specs (id + label).

        Reads ``name`` and ``description`` from each .yaml file —
        we parse twice (once for list, once for build) because
        loading eagerly at startup would hide YAML parse errors
        from the caller.
        """
        import yaml as _yaml

        out: list[dict[str, Any]] = []
        if not self._specs_dir.is_dir():
            return out
        for path in sorted(self._specs_dir.glob("*.yaml")):
            try:
                spec = _yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:
                LOGGER.warning("workflow spec %s parse error: %r", path, exc)
                continue
            if not isinstance(spec, dict):
                continue
            out.append({
                "id": spec.get("name", path.stem),
                "label": (spec.get("description") or "").strip().splitlines()[0]
                         if spec.get("description") else path.stem,
                "path": str(path),
                "nodes": len(spec.get("nodes") or []),
                "edges": len(spec.get("edges") or []),
            })
        return out

    def get(self, name: str) -> Workflow | list[str]:
        """Load + cache the Workflow with the given ``name``.

        Returns a ``Workflow`` on success or a list of error strings
        on failure (mirrors ``load_workflow_yaml`` contract).
        """
        if name in self._cache:
            cached = self._cache[name]
            return cached
        path = self._specs_dir / f"{name}.yaml"
        if not path.is_file():
            errs = [f"workflow spec {name!r} not found at {path}"]
            self._cache[name] = errs
            return errs
        wf_or_errs = load_workflow_yaml(
            path.read_text(encoding="utf-8"),
            handlers=self._handlers,
        )
        self._cache[name] = wf_or_errs
        return wf_or_errs

    def reload(self, name: str) -> Workflow | list[str]:
        """Force-reload a spec from disk (drops cache first)."""
        self._cache.pop(name, None)
        return self.get(name)

    def get_yaml_text(self, name: str) -> str | list[str]:
        """Return the raw YAML text for the spec (for the /spec endpoint)."""
        path = self._specs_dir / f"{name}.yaml"
        if not path.is_file():
            return [f"workflow spec {name!r} not found"]
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return [f"read error: {exc!r}"]


__all__ = [
    "WorkflowSpecRegistry",
    "DEFAULT_SPECS_DIR",
]
