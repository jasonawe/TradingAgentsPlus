"""Pure $ref parser + evaluator for the multi-agent runtime (Phase 1).

Rightmost-split precedence: at each operator level, split on the RIGHTMOST
occurrence. Gives left-to-right associativity with correct precedence:
``$data.x * 2 + $data.y * 3`` parses as ``($data.x * 2) + ($data.y * 3)``.

**Parentheses are NOT supported in Phase 1.** Rewrite expressions like
``($a + $b) * 4`` to ``$a * 4 + $b * 4``.

Rev.5 (LOW #8): field regex tightened to require `[A-Za-z_]` start. This
matches the agent-name capture and Python attribute-name rules. ``$data.0field``
fails to parse (consistent with dataclass lookups where ``0field`` is not a
valid attribute name).

Rev.6 implementation patch — operator precedence iteration order corrected.
The plan body iterated add/sub → mul/div → comparison, which is INVERTED
relative to Python's actual precedence. With the plan order, ``$x * 2 == 4``
evaluated as ``2 * (2 == 4) == 0`` (falsy) instead of ``(2 * 2) == 4 == True``.
The corrected order is comparison (loosest) → add/sub → mul/div (tightest),
which matches CPython's grammar.

Rev.6 implementation patch — literal branch also strips single-quoted strings
so ternary expressions like ``cond ? 'expensive' : 'cheap'`` evaluate to
bare ``expensive``/``cheap``. The plan body only handled double-quoted
strings; tests use single quotes.

Rev.6 implementation patch — ``resolve_ref`` honors ``literal=`` when
``_eval_expr`` returns ``None`` (unknown agent / missing field), not only
on exception. The plan body's ``try/except Exception`` only caught exceptions,
so a clean lookup miss returned ``None`` instead of the configured fallback.
"""
from __future__ import annotations
import re
from typing import Any, Optional

from .state import GraphState

# rev.5: field must start with letter or underscore (matches agent capture + Python attrs)
_REF_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_.]*)")

def is_ref_expr(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return "$" in value and bool(_REF_RE.search(value))

def parse_ref(value: str) -> Optional[tuple[str, str]]:
    if not isinstance(value, str) or not value.startswith("$"):
        return None
    m = _REF_RE.match(value.strip())
    if not m:
        return None
    return m.group(1), m.group(2)

def _lookup(state: GraphState, agent: str, dotted: str) -> Any:
    if agent not in state.agent_outputs:
        return None
    node = state.agent_outputs[agent].data
    if isinstance(node, dict):
        cur: Any = node
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return cur
    for part in dotted.split("."):
        if not hasattr(node, part):
            return None
        node = getattr(node, part)
    return node

def _truthy(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)

def _try_split(value: str, op: str):
    idx = value.rfind(op)
    if idx < 0:
        return None
    return value[:idx], value[idx + len(op):]

def _eval_expr(value: str, state: GraphState) -> Any:
    # Ternary (lowest precedence)
    if "?" in value and ":" in value:
        cond, rest = value.split("?", 1)
        a, b = rest.split(":", 1)
        cond_val = _eval_expr(cond.strip(), state)
        chosen = a if _truthy(cond_val) else b
        return _eval_expr(chosen.strip(), state)

    # Comparison (loosest arithmetic precedence)
    for op, fn in ((" >= ", lambda a, b: a >= b),
                   (" <= ", lambda a, b: a <= b),
                   (" == ", lambda a, b: a == b),
                   (" != ", lambda a, b: a != b),
                   (" > ", lambda a, b: a > b),
                   (" < ", lambda a, b: a < b)):
        split = _try_split(value, op)
        if split is None:
            continue
        left, right = split
        return fn(_eval_expr(left.strip(), state),
                  _eval_expr(right.strip(), state))

    # Add/sub (medium precedence)
    for op, fn in ((" + ", lambda a, b: a + b),
                   (" - ", lambda a, b: a - b)):
        split = _try_split(value, op)
        if split is None:
            continue
        left, right = split
        return fn(_eval_expr(left.strip(), state),
                  _eval_expr(right.strip(), state))

    # Mul/div (tightest arithmetic precedence)
    for op, fn in ((" * ", lambda a, b: a * b),
                   (" / ", lambda a, b: a / b)):
        split = _try_split(value, op)
        if split is None:
            continue
        left, right = split
        return fn(_eval_expr(left.strip(), state),
                  _eval_expr(right.strip(), state))

    # Plain $ref
    ref = parse_ref(value.strip())
    if ref:
        agent, dotted = ref
        return _lookup(state, agent, dotted)

    # Literal (int / float / string — both single- and double-quoted)
    stripped = value.strip()
    try:
        return int(stripped)
    except ValueError:
        try:
            return float(stripped)
        except ValueError:
            if (stripped.startswith('"') and stripped.endswith('"')) or \
               (stripped.startswith("'") and stripped.endswith("'")):
                return stripped[1:-1]
            return stripped

def resolve_ref(value: str, state: GraphState, *, literal: Any = None) -> Any:
    try:
        if not isinstance(value, str):
            return value
        if not is_ref_expr(value):
            return value
        result = _eval_expr(value, state)
        if result is None:
            return literal
        return result
    except Exception:
        return literal
