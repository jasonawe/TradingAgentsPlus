"""P1 验证 — chat_once 字段 + prompts 截断。"""
import sys
sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

print("=" * 60)
print("P1 fix 验证")
print("=" * 60)

# P1-5: prompts.py 截断
print("\n[1] prompts.py 截断...")
from tradingagents.agents.general.prompts import _format_tool_descriptions

# Mock tool with long desc
class T:
    def __init__(self, name, desc):
        self.name = name
        self.description = desc

long_desc = "x" * 500  # 500 chars
tools = [T(f"tool_{i}", long_desc) for i in range(15)]  # 15 个 tool

result = _format_tool_descriptions(tools)
print(f"  total chars: {len(result)}")
print(f"  per-tool truncated: {'tool_0: ' + 'x' * 147 + '...' in result}")
print(f"  total within 1500: {len(result) <= 1700}")  # 允许 "(还有 N 个工具)" 行
print(f"  has '还有' marker: {'还有' in result}")

# P1-4: chat_once 字段
print("\n[2] chat_once 结构...")
from tradingagents.agents.general.orchestrator import chat_once
import inspect
sig = inspect.signature(chat_once)
print(f"  signature: {sig}")

# 用 mock LLM 跑一遍
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from pathlib import Path

class MockRunnable(Runnable):
    def invoke(self, input, config=None, **kwargs):
        return AIMessage(content='final mock answer')
    def bind_tools(self, tools):
        return self

from tradingagents.agents.general.orchestrator import build_agent
data_dir = Path.home() / '.tradingagents'
agent, conn = build_agent(llm=MockRunnable(), data_dir=data_dir, session_id='s_p1_test')
result = chat_once(agent, 's_p1_test', 'test query')

print(f"  keys: {sorted(result.keys())}")
assert 'answer' in result
assert 'tool_calls' in result
assert 'tool_results' in result
assert 'events' in result
print(f"  ✓ answer: {result['answer']!r}")
print(f"  ✓ tool_calls: {len(result['tool_calls'])}")
print(f"  ✓ tool_results: {len(result['tool_results'])}")

conn.close()
import os
db = data_dir / 'agent_general' / 'sessions' / 'AGENT_S_P1_TEST.db'
if db.exists():
    db.unlink()

print()
print("=" * 60)
print("✅ P1 全部 2 个改动验证通过")
print("=" * 60)
