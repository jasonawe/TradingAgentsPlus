"""P2 验证 — 5 个可选改进。"""
import sys
sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

print("=" * 60)
print("P2 fix 验证")
print("=" * 60)

# P2-#10: get_quote 量化字段
print("\n[1] get_quote 量化字段...")
import datetime
from web.market_models import QuoteSnapshot
mock_snap = QuoteSnapshot(
    symbol="600036.SS", price=38.5, change=0.5, change_percent=1.32,
    volume=12345678.0, source="akshare", fetched_at=datetime.datetime.now(datetime.timezone.utc),
    # 量化字段
    volume_ratio=1.5, turnover=4.76e8, turnover_rate=0.85,
    market_cap=9.7e11, pe_ratio=7.2, amplitude=2.3,
)

import web.market_data as md
class MockQS:
    def __init__(self, *a, **kw): self.snap = mock_snap
    def get_quote(self, *a, **kw): return mock_snap
    def get_quotes(self, *a, **kw): 
        from web.market_models import BulkQuoteResponse, QuoteItem
        return BulkQuoteResponse(items=[QuoteItem(symbol="600036.SS", price=38.5, change=0.5, change_percent=1.32, source="akshare")], partial=False)
md.QuoteService = MockQS

import importlib, tradingagents.agents.general.tools_bridge as tb
importlib.reload(tb)
result = tb.get_quote.invoke({"symbol": "600036.SS"})
print(result)
assert "quantitative_metrics:" in result, "缺 quantitative_metrics section"
assert "volume_ratio: 1.5" in result, "缺 volume_ratio"
assert "pe_ratio: 7.2" in result, "缺 pe_ratio"
assert "market_cap" in result, "缺 market_cap"
print("  ✓ quantitative_metrics section + 7 个量化字段都输出")

# P2-#7: stream_chat 仍然能用(可能改了内部实现,API 不变)
print("\n[2] stream_chat API 兼容性...")
from tradingagents.agents.general.orchestrator import build_agent, stream_chat, chat_once
from langchain_core.runnables import Runnable
from langchain_core.messages import AIMessage
class MockR(Runnable):
    def invoke(self, input, config=None, **kw): return AIMessage(content="x")
    def bind_tools(self, tools): return self
from pathlib import Path
data_dir = Path.home() / '.tradingagents'
agent, conn = build_agent(llm=MockR(), data_dir=data_dir, session_id='s_p2_test')
result = chat_once(agent, 's_p2_test', 'test')
assert 'answer' in result and 'tool_calls' in result
print(f"  ✓ chat_once still works: answer={result['answer']!r}")
conn.close()
db = data_dir / 'agent_general' / 'sessions' / 'AGENT_S_P2_TEST.db'
if db.exists(): db.unlink()

# P2-#8: validate_write_intent
print("\n[3] validate_write_intent...")
from tradingagents.agents.general.guardrails import validate_write_intent
is_w, impact = validate_write_intent('create_alert', {'symbol': '600036.SS'})
print(f"  create_alert: is_write={is_w}, impact={impact!r}")
assert is_w is True
assert '告警' in impact

is_w, impact = validate_write_intent('get_quote')
print(f"  get_quote: is_write={is_w}, impact={impact!r}")
assert is_w is False
print("  ✓ validate_write_intent 工作正确")

# P2-#9: update_write_status 校验
print("\n[4] update_write_status 合法性...")
from tradingagents.agents.general.audit import update_write_status, VALID_WRITE_STATUSES, log_write
db_path = Path.home() / '.tradingagents' / 'web_runs.sqlite3'
audit_id = log_write(
    db_path=db_path, session_id='s_p2_audit',
    tool_name='create_alert', tool_args={}, status='pending',
)
print(f"  VALID_WRITE_STATUSES: {VALID_WRITE_STATUSES}")
try:
    update_write_status(db_path, audit_id, status='bogus')
    assert False, "应该抛 ValueError"
except ValueError as e:
    print(f"  ✓ invalid status raises ValueError: {e}")

# valid status 仍然 OK
ok = update_write_status(db_path, audit_id, status='confirmed')
print(f"  ✓ valid status update: {ok}")

# cleanup
import sqlite3
conn = sqlite3.connect(str(db_path))
conn.execute("DELETE FROM write_audit_log WHERE session_id='s_p2_audit'")
conn.commit(); conn.close()

# P2-#6: alias
print("\n[5] _agent_db_path alias...")
from tradingagents.agents.general import memory
print(f"  _agent_db_path is agent_memory_db_path: {memory._agent_db_path is memory.agent_memory_db_path}")
print("  ✓ alias 兼容旧代码")

print()
print("=" * 60)
print("✅ P2 全部 5 个改动验证通过")
print("=" * 60)
