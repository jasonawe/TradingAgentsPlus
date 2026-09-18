"""Step 35 — Workflow visualization (DOT/JSON).

Renders a :class:`Workflow` instance to either DOT (Graphviz) or
JSON (for JS graph libraries). Used by the
``/api/harness/workflows/<name>/graph`` endpoint so operators can
see the actual node/edge structure of any registered workflow.

Why a tiny custom renderer instead of pulling in graphviz-py
------------------------------------------------------------
- graphviz-py requires the system `dot` binary — extra install
  burden for operators who only need the textual representation.
- Most JS viz libs (dagre-d3, cytoscape, vis-network) accept a
  simple JSON node/edge list. Our output is one JSON shape that
  works for any of them.
- DOT output is plain text and trivially copyable into
  `https://viz-js.com` for instant visual feedback.

Output shapes
-------------
DOT (Graphviz)::

    digraph post_execute {
      rankdir=LR;
      observe -> verify;
      verify -> synthesize;
      synthesize -> "<end>";
      observe [shape=box, label="observe\\n(handler: observe_handler)"];
      ...
    }

JSON::

    {
      "name": "post_execute",
      "entry": "observe",
      "nodes": [{"id": "observe", "label": "observe"}, ...],
      "edges": [{"from": "observe", "to": "verify", "label": "always"}, ...],
      "executed": ["observe", "verify", "synthesize"]
    }

The ``executed`` field is the actual node visit order recorded by
``Workflow.run()`` — it makes debug traces ("why did the workflow
take this path?") trivial.
"""
from __future__ import annotations

from typing import Any

from .workflow import Edge, Node, Workflow, END


def _node_label(node: Node) -> str:
    """Render a node as a DOT label with the handler description."""
    handler = getattr(node.handler, "__name__", "<callable>")
    return f"{node.id}\\n(handler: {handler})"


def to_dot(workflow: Workflow) -> str:
    """Render the workflow as a Graphviz DOT string.

    The end sentinel (``<end>``) becomes a double-circle node so
    the terminal state is visually distinct. Edges without a
    condition are labeled "always"; conditional edges show
    the predicate name if it's a named function.
    """
    lines: list[str] = []
    lines.append(f"digraph {workflow.name} {{")
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=rounded, fontname=Helvetica];")
    # Nodes
    for node in workflow.nodes():
        label = _node_label(node)
        lines.append(f'  "{node.id}" [label="{label}"];')
    # End sentinel
    lines.append(f'  "{END}" [shape=doublecircle, label="<end>"];')
    # Edges
    for edge in workflow.edges():
        cond_name = getattr(edge.condition, "__name__", "")
        label = ""
        if cond_name and cond_name != "<lambda>":
            label = f' [label="{cond_name}"]'
        lines.append(f'  "{edge.from_node}" -> "{edge.to_node}"{label};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_json(workflow: Workflow) -> dict[str, Any]:
    """Render the workflow as a JSON-friendly dict.

    The shape is intentionally minimal so any JS graph lib can
    consume it without further transformation.
    """
    nodes: list[dict[str, Any]] = []
    for node in workflow.nodes():
        handler_name = getattr(node.handler, "__name__", None)
        nodes.append({
            "id": node.id,
            "label": node.id,
            "handler": handler_name,
        })
    # End sentinel as a node too so JS can render the terminal marker
    nodes.append({"id": END, "label": "<end>", "handler": None,
                  "terminal": True})
    edges: list[dict[str, Any]] = []
    for edge in workflow.edges():
        cond_name = getattr(edge.condition, "__name__", "")
        label = ""
        if cond_name and cond_name != "<lambda>":
            label = cond_name
        edges.append({
            "from": edge.from_node,
            "to": edge.to_node,
            "label": label or "always",
            "priority": edge.priority,
        })
    return {
        "name": workflow.name,
        "entry": workflow.entry_node().id if workflow.entry_node() else None,
        "nodes": nodes,
        "edges": edges,
        "executed": workflow.executed(),
    }
