from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState, TypedResult, Message, FieldRef,
)

def test_typed_result_roundtrip():
    class Quote:
        def __init__(self, price): self.price = price
    res = TypedResult(schema=Quote, data=Quote(41.06), meta={"source_ts": 1.0})
    assert res.data.price == 41.06
    assert res.meta["source_ts"] == 1.0

def test_message_defaults():
    m = Message(sender="a", receiver="b",
                payload=TypedResult(schema=dict, data={}, meta={}))
    assert m.kind == "data"
    assert m.hop == 0

def test_fieldref_str():
    r = FieldRef(agent="data", field="quote.symbol")
    assert str(r) == "\$data.quote.symbol"

def test_graph_state_append_inbox_deposits_and_logs():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    assert state.inbox["y"] == [msg]
    assert state.message_log == [msg]

def test_graph_state_consume_inbox_is_idempotent():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    inbox = state.consume_inbox("y")
    assert len(inbox) == 1
    assert state.consume_inbox("y") == []
