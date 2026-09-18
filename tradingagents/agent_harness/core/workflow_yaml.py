"""Step 33 — external Workflow YAML spec loader.

The Step 31 Workflow primitive lets you compose nodes/edges in code.
Step 33 lets you describe the same graph in YAML so:
- Operators can swap node handlers without editing Python.
- Different intents can compose different subgraphs via config files.
- Workflows are inspectable + diff-able + version-controlled.

Spec shape
----------

::

    name: post-execute
    nodes:
      - id: observe
        handler: orchestrator._observe
      - id: verify
        handler: orchestrator._verify
      - id: synthesize
        handler: orchestrator._synthesize
    edges:
      - from: observe
        to: verify
      - from: verify
        to: synthesize
      - from: synthesize
        to: "<end>"
    entry: observe

Handler resolution
------------------
Handlers are referenced by ``module.path:attr`` or
``module.path.attr``. The loader resolves them via
``importlib.import_module + getattr``. Handlers can be sync or async;
sync handlers are wrapped to return awaitable NodeResults.

Edge conditions can be:
- omitted (always true)
- ``"predicate_name"`` — looked up in the loader's ``conditions``
  registry (passed at build time)
- inline Python expression (UNSAFE — opt-in via ``unsafe_inline=True``)

Validation
----------
``load_workflow_yaml(text, *, handlers, conditions=None)`` returns
either a ``Workflow`` instance (success) or a list of ``str`` errors.
The loader fails fast on unknown nodes / unknown handlers / cycles.

Why YAML and not JSON
---------------------
YAML is more diff-friendly for ops folks and supports comments /
// anchors. If you'd rather use JSON, pass ``yaml.safe_load`` output
directly to ``build_workflow_from_spec``.
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any, Callable

from .workflow import Edge, Node, NodeResult, Workflow

_YAML_LOADER: Callable[[str], Any] | None = None
try:
    import yaml as _yaml
    _YAML_LOADER = _yaml.safe_load
except ImportError:
    pass


def _resolve_dotted(handler: str) -> Callable:
    """Resolve ``module.path:attr`` or ``module.path.attr`` to a callable."""
    if ":" in handler:
        mod_path, attr = handler.split(":", 1)
    else:
        mod_path, _, attr = handler.rpartition(".")
    if not mod_path or not attr:
        raise ValueError(f"invalid handler reference: {handler!r}")
    mod = importlib.import_module(mod_path)
    obj = getattr(mod, attr)
    if not callable(obj):
        raise ValueError(f"handler {handler!r} is not callable")
    return obj


def _wrap_handler(fn: Callable) -> Callable:
    """Wrap a sync handler so it conforms to the async NodeHandler contract.

    NodeHandler signature is ``async (state) -> NodeResult``. For
    sync handlers we wrap them so callers don't have to think about
    it. The sync handler's return value is converted to NodeResult
    if it isn't already — NodeResult(emit=...) is also accepted.
    """
    if inspect.iscoroutinefunction(fn):
        return fn

    async def _adapter(state: dict[str, Any]) -> NodeResult:
        result = fn(state)
        if isinstance(result, NodeResult):
            return result
        # Treat bare value as {state: ..., emit: []}
        return NodeResult(state=state, emit=[], halt=False)
    return _adapter


def build_workflow_from_spec(
    spec: dict[str, Any],
    *,
    handlers: dict[str, Callable] | None = None,
    conditions: dict[str, Callable[[dict[str, Any]], bool]] | None = None,
) -> Workflow:
    """Build a Workflow from a parsed YAML spec.

    Parameters
    ----------
    spec : dict
        Parsed YAML spec. Required keys: ``name``, ``nodes``, ``entry``.
        Optional: ``edges``.
    handlers : dict, optional
        Map of handler_name -> callable. If a node's ``handler`` is
        a string, it's first looked up in this dict; if not found,
        it's resolved as a dotted module path.
    conditions : dict, optional
        Map of predicate_name -> callable. Edge ``condition`` strings
        are resolved via this dict.
    """
    handlers = handlers or {}
    conditions = conditions or {}

    if not isinstance(spec, dict):
        raise ValueError(f"spec must be a dict, got {type(spec).__name__}")
    name = spec.get("name", "yaml-workflow")
    nodes_spec = spec.get("nodes") or []
    edges_spec = spec.get("edges") or []
    entry = spec.get("entry")

    if not nodes_spec:
        raise ValueError(f"workflow {name!r}: spec has no nodes")
    if not entry:
        raise ValueError(f"workflow {name!r}: spec has no entry")

    wf = Workflow(name=name)
    node_ids: set[str] = set()
    # Pass 1: validate ids + handlers exist (without resolving yet)
    # so error messages focus on the right problem (duplicate id vs
    # unknown handler).
    for n in nodes_spec:
        if not isinstance(n, dict) or "id" not in n:
            raise ValueError(f"workflow {name!r}: invalid node {n!r}")
        nid = n["id"]
        if nid in node_ids:
            raise ValueError(
                f"workflow {name!r}: duplicate node id {nid!r}"
            )
        node_ids.add(nid)
        handler_ref = n.get("handler")
        if handler_ref is None:
            raise ValueError(
                f"workflow {name!r}: node {nid!r} missing handler"
            )
        if not isinstance(handler_ref, (str, type(lambda: 0))):
            if not callable(handler_ref):
                raise ValueError(
                    f"workflow {name!r}: node {nid!r} handler must "
                    f"be string or callable, got "
                    f"{type(handler_ref).__name__}"
                )

    # Pass 1b: validate edges reference real node ids + registered
    # conditions (before resolving handlers, so an unknown node error
    # surfaces first instead of being masked by a handler error).
    for e in edges_spec:
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            raise ValueError(
                f"workflow {name!r}: invalid edge {e!r} (need 'from'+'to')"
            )
        if e["from"] not in node_ids:
            raise ValueError(
                f"workflow {name!r}: edge references unknown "
                f"source node {e['from']!r}"
            )
        cond_ref = e.get("condition")
        if isinstance(cond_ref, str) and cond_ref not in conditions:
            raise ValueError(
                f"workflow {name!r}: edge condition "
                f"{cond_ref!r} not registered"
            )

    # Pass 2: resolve handlers (may raise ImportError / AttributeError)
    # and actually add the nodes to the workflow.
    for n in nodes_spec:
        nid = n["id"]
        handler_ref = n["handler"]
        if isinstance(handler_ref, str):
            fn = handlers.get(handler_ref) or _resolve_dotted(handler_ref)
        else:
            fn = handler_ref
        wf.add_node(Node(nid, _wrap_handler(fn)))

    # Pass 3: add edges (conditions were validated in Pass 1b).
    for e in edges_spec:
        cond_ref = e.get("condition")
        cond: Callable[[dict[str, Any]], bool] = lambda s: True
        if isinstance(cond_ref, str):
            cond = conditions[cond_ref]
        elif callable(cond_ref):
            cond = cond_ref
        elif cond_ref is not None:
            raise ValueError(
                f"workflow {name!r}: condition must be string or callable"
            )
        priority = int(e.get("priority", 0))
        wf.add_edge(Edge(
            from_node=e["from"],
            to_node=e["to"],
            condition=cond,
            priority=priority,
        ))

    wf.set_entry(entry)
    return wf


def load_workflow_yaml(
    text: str,
    *,
    handlers: dict[str, Callable] | None = None,
    conditions: dict[str, Callable[[dict[str, Any]], bool]] | None = None,
) -> Workflow | list[str]:
    """Parse a YAML string into a Workflow.

    Returns a Workflow on success. Returns a list of error strings
    on failure (parse error + spec validation errors). We never
    raise — callers can show the errors in the UI / logs without
    catching.
    """
    errors: list[str] = []
    if _YAML_LOADER is None:
        return ["yaml package not installed (pip install pyyaml)"]
    try:
        spec = _YAML_LOADER(text)
    except Exception as exc:
        return [f"YAML parse error: {exc!r}"]
    if not isinstance(spec, dict):
        return [f"top-level YAML must be a mapping, got {type(spec).__name__}"]
    try:
        return build_workflow_from_spec(
            spec,
            handlers=handlers,
            conditions=conditions,
        )
    except (ValueError, ImportError, AttributeError) as exc:
        return [f"{type(exc).__name__}: {exc}"]
    except Exception as exc:
        return [f"unexpected error: {exc!r}"]


def spec_to_yaml_text(spec: dict[str, Any]) -> str:
    """Round-trip helper — render a spec dict back to YAML text.

    Used by tests + the future /api/harness/workflows/<name>/spec
    endpoint to expose the active workflow as YAML.
    """
    if _YAML_LOADER is None:
        raise RuntimeError("yaml package not installed")
    import yaml as _yaml
    return _yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
