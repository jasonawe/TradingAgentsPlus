import pytest
from tradingagents.agent_harness.runtime.multi_agent.resolver import (
    parse_ref, is_ref_expr, resolve_ref,
)
from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState, TypedResult,
)

def _state():
    # rev.6 patch: data fixture has both a 2-level path (q.symbol) and a
    # 3-level path (q.meta.isin) so that the nested-lookup test can resolve.
    # The plan fixture only had `q.symbol` as a string, which made
    # `$data.q.symbol.isin` unresolvable.
    return GraphState(
        run_id="r", turn_id="t", intent="x",
        agent_outputs={
            "data": TypedResult(
                schema=dict,
                data={"x": 2, "y": 3,
                      "q": {"symbol": "600036.SS",
                            "meta": {"isin": "CNE1000000Q1"}},
                      "tag": "buy"},
                meta={},
            ),
            "alpha": TypedResult(schema=dict, data={"price": 40.5}, meta={}),
        },
    )

def test_parse_ref_basic():
    assert parse_ref("$data.quote.symbol") == ("data", "quote.symbol")

def test_parse_ref_3_level_nested():
    assert parse_ref("$data.q.symbol.isin") == ("data", "q.symbol.isin")

def test_parse_ref_no_dollar_returns_none():
    assert parse_ref("data.quote.symbol") is None

@pytest.mark.parametrize("bad_input,reason", [
    ("$$",          "no agent name after first dollar"),
    ("$abc",        "no dot in regex group"),
    ("$data.",      "empty field name"),
    ("$data.0field","field must start with letter or underscore"),
    ("",            "empty string"),
])
def test_parse_ref_rejects_malformed(bad_input, reason):
    assert parse_ref(bad_input) is None, f"should reject {bad_input!r} ({reason})"

def test_is_ref_expr_distinguishes():
    assert is_ref_expr("$data.x")
    assert not is_ref_expr("hello")
    assert is_ref_expr("$data.x * 1.5")
    assert not is_ref_expr(42)
    assert not is_ref_expr(None)

def test_resolve_ref_simple():
    s = _state()
    assert resolve_ref("$data.q.symbol", s) == "600036.SS"

def test_resolve_ref_3_level_nested_lookup():
    s = _state()
    # rev.6: path uses `q.meta.isin` (3-level) matching the nested dict structure.
    assert resolve_ref("$data.q.meta.isin", s) == "CNE1000000Q1"

def test_resolve_ref_arithmetic_single_op():
    s = _state()
    assert resolve_ref("$alpha.price * 2", s) == 81.0
    assert resolve_ref("$alpha.price * 1.5", s) == 60.75

def test_resolve_ref_arithmetic_precedence_two_refs_with_constant():
    # $data.x=2, $data.y=3 → 2*2 + 3*3 == 13
    s = _state()
    assert resolve_ref("$data.x * 2 + $data.y * 3", s) == 13

def test_resolve_ref_arithmetic_precedence_mul_binds_tighter_than_add():
    # 2 + 3 * 4 == 14 (not 20) — multiplication binds tighter than addition
    s = _state()
    assert resolve_ref("$data.x + $data.y * 4", s) == 14

def test_resolve_ref_ternary_truthy():
    s = _state()
    assert resolve_ref("$alpha.price > 40 ? 'expensive' : 'cheap'", s) == "expensive"

def test_resolve_ref_arithmetic_in_ternary():
    s = _state()
    assert resolve_ref("$data.x * 2 == 4 ? 'four' : 'other'", s) == "four"

def test_resolve_ref_falls_back_to_literal():
    s = _state()
    assert resolve_ref("$nope.field", s, literal="fallback") == "fallback"

def test_resolve_ref_passes_through_non_string():
    s = _state()
    assert resolve_ref(42, s) == 42
