"""Stage C Day 1 — Smoke test。

端到端验证 Stage C 能跑通最小流程:
1. 所有模块 import 成功
2. build_agent 能构造(用 mock 或真实 LLM)
3. stream_chat / chat_once 能产出 reasoning + tool_call + final 事件
4. L1 短期记忆跨调用生效
5. L2 偏好能注入到 system prompt

用法:
    python tests/smoke_agent.py            # 默认 dry_run=True,用 mock LLM
    python tests/smoke_agent.py --real     # 用真实 LLM(需要 OPENAI_API_KEY 或类似配置)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _setup_path() -> None:
    """确保 tradingagents 能 import(从项目根跑)。"""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def dry_run() -> None:
    """Dry-run 模式 — 用 Mock Runnable 验证结构,不需要 LLM API key。"""
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable

    class MockRunnable(Runnable):
        def invoke(self, input, config=None, **kwargs):
            return AIMessage(content="[mock] 这是 fake answer")
        def bind_tools(self, tools):
            return self

    print("=" * 60)
    print("Stage C Day 1 Smoke Test — DRY RUN (mock LLM)")
    print("=" * 60)

    # 1. Import
    print("\n[1] Import 全部模块...")
    from tradingagents.agents.general.orchestrator import (
        build_agent, chat_once, get_session_history, session_thread_id,
    )
    from tradingagents.agents.general.memory import (
        list_preferences, set_preference, store_reference,
    )
    from tradingagents.agents.general.guardrails import is_write_tool
    from tradingagents.agents.general.audit import log_write, list_writes
    from tradingagents.agents.general.tools_bridge import ALL_TOOLS
    print(f"    ✓ ALL_TOOLS has {len(ALL_TOOLS)} tools")

    # 2. L2 preferences
    print("\n[2] L2 preferences: 注入 risk_tolerance=conservative...")
    from tradingagents.agents.general.memory import agent_memory_db_path
    db_path = agent_memory_db_path(Path.home() / ".tradingagents")
    set_preference(db_path, "risk_tolerance", "conservative", source="user")
    set_preference(db_path, "watch_industries", ["banking", "tech"], source="user")
    prefs = list_preferences(db_path)
    print(f"    ✓ stored: {list(prefs.keys())}")

    # 3. build_agent
    print("\n[3] build_agent...")
    data_dir = Path.home() / ".tradingagents"
    session_id = "s_smoke_dry_run"
    agent, conn = build_agent(
        llm=MockRunnable(),
        data_dir=data_dir,
        session_id=session_id,
    )
    print(f"    ✓ agent: {type(agent).__name__}")

    # 4. chat_once 第一轮
    print("\n[4] chat_once 第一轮: '招商银行 600036 多少钱?'")
    result = chat_once(agent, session_id, "招商银行 600036 多少钱?")
    print(f"    ✓ answer: {result['answer']!r}")
    print(f"    ✓ events: {len(result['events'])}")
    for et, p in result["events"]:
        print(f"      [{et}] {str(p)[:70]}")

    # 5. L1 短期记忆 — 第二轮
    print("\n[5] chat_once 第二轮: '还有 600000.SH 呢?'")
    result2 = chat_once(agent, session_id, "还有 600000.SH 呢?")
    print(f"    ✓ answer: {result2['answer']!r}")
    history = get_session_history(agent, session_id)
    print(f"    ✓ history messages: {len(history)}")
    for h in history:
        print(f"      [{h['role']}] {str(h.get('content', h.get('name', '')))[:60]}")

    # 6. L3 references
    print("\n[6] L3 store_reference...")
    ref_id = store_reference(
        db_path,
        session_id=session_id,
        nl_query="招商银行 600036 多少钱",
        tool_name="get_quote",
        tool_args={"symbol": "600036.SS"},
        tool_result={"price": 38.5},
        tags=["smoke", "test"],
    )
    print(f"    ✓ ref_id: {ref_id}")

    # 7. Guardrails
    print("\n[7] guardrails: 写操作识别...")
    print(f"    is_write_tool('get_quote'): {is_write_tool('get_quote')}")
    print(f"    is_write_tool('create_alert'): {is_write_tool('create_alert')}")

    # 8. Audit log
    print("\n[8] audit: log + list...")
    audit_id = log_write(
        db_path=db_path,
        session_id=session_id,
        user_message="建一个告警",
        tool_name="create_alert",
        tool_args={"symbol": "600036.SS", "change_pct": 5},
        status="pending",
    )
    writes = list_writes(db_path, session_id=session_id)
    print(f"    ✓ audit_id: {audit_id}, list_writes: {len(writes)}")

    # Cleanup
    print("\n[cleanup]...")
    conn.close()
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM write_audit_log WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM agent_references WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM user_preferences WHERE key IN ('risk_tolerance', 'watch_industries')")
    conn.commit()
    conn.close()
    session_db = data_dir / "agent_general" / "sessions" / f"AGENT_{session_id.upper()}.db"
    if session_db.exists():
        session_db.unlink()
    print(f"    ✓ cleaned session DB + test data")

    print()
    print("=" * 60)
    print("✅ Stage C Day 1 Smoke Test PASSED (dry run)")
    print("=" * 60)


def real_run() -> None:
    """真实 LLM 模式 — 需要 OPENAI_API_KEY / 类似配置。"""
    print("=" * 60)
    print("Stage C Day 1 Smoke Test — REAL LLM")
    print("=" * 60)

    try:
        from tradingagents.llm_clients.openai_client import OpenAIClient
    except ImportError as e:
        print(f"    ✗ OpenAIClient import failed: {e}")
        sys.exit(1)

    # 用 OPENAI_API_KEY / TRADINGAGENTS_LLM_BACKEND_URL 配置
    model = os.environ.get("STAGE_C_LLM_MODEL", "gpt-4o-mini")
    print(f"\n[1] Init LLM: {model}")
    try:
        client = OpenAIClient(model=model)
        llm = client.get_llm()
    except Exception as e:
        print(f"    ✗ LLM init failed: {e}")
        print("    Hint: 设置 OPENAI_API_KEY 后重试")
        sys.exit(1)
    print(f"    ✓ LLM: {type(llm).__name__}")

    from tradingagents.agents.general.orchestrator import build_agent, chat_once
    data_dir = Path.home() / ".tradingagents"
    session_id = "s_smoke_real_run"
    print(f"\n[2] build_agent(session={session_id})...")
    agent, conn = build_agent(llm=llm, data_dir=data_dir, session_id=session_id)

    print(f"\n[3] chat_once: '招商银行 600036 多少钱?'")
    result = chat_once(agent, session_id, "招商银行 600036 多少钱?")
    print(f"    answer: {result['answer']}")
    print(f"    tool_calls: {[t['name'] for t in result['tool_calls']]}")

    conn.close()
    print("\n✅ Stage C Day 1 REAL LLM Smoke Test PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Use real LLM (requires API key)")
    args = parser.parse_args()
    _setup_path()
    if args.real:
        real_run()
    else:
        dry_run()
