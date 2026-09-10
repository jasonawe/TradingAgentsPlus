"""Stage C 通用 Agent 主图 — LangGraph ReAct agent。

设计:
- build_agent() 接收已构造的 chat model + data_dir + session_id
  返回 (compiled_agent, checkpointer)
- stream_chat() 是 generator,产出 (event_type, payload) 元组
  event_type ∈ {"reasoning", "tool_call", "tool_result", "final", "error"}

不自己初始化 LLM(解耦)— 调用方负责构造(用 OpenAIClient.get_llm() 或其他)。
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from tradingagents.agents.general.memory import (
    agent_memory_db_path,
    get_session_checkpointer,
    list_preferences,
)
from tradingagents.agents.general.prompts import render_system_prompt
from tradingagents.agents.general.tools_bridge import ALL_TOOLS


# ════════════════════════════════════════════════════════
# L1 thread_id
# ════════════════════════════════════════════════════════

def session_thread_id(session_id: str) -> str:
    """LangGraph thread_id — 用 session_id 直接(per-session DB 已经隔离了)。"""
    return session_id


# ════════════════════════════════════════════════════════
# build_agent
# ════════════════════════════════════════════════════════

def build_agent(
    *,
    llm: Any,
    data_dir: str | Path,
    session_id: str,
    mode: str = "guided",
    extra_preferences: dict[str, Any] | None = None,
):
    """构造一个 LangGraph ReAct agent 实例。

    Args:
        llm: 已构造的 chat model(由调用方用 OpenAIClient.get_llm() 等构造)
        data_dir: TradingAgents 数据目录(~/.tradingagents)
        session_id: 当前 chat session ID
        mode: "guided"(强约束)| "direct"(简洁)
        extra_preferences: 临时附加偏好(可选)

    Returns:
        (agent, checkpointer):
            agent: 编译后的 LangGraph agent(可直接 .stream() / .invoke())
            checkpointer: SqliteSaver(L1 短期对话,可手动 close)
    """
    db_path = agent_memory_db_path(data_dir)
    prefs = list_preferences(db_path)
    if extra_preferences:
        prefs.update(extra_preferences)

    system_prompt = render_system_prompt(
        tools=ALL_TOOLS,
        preferences=prefs,
        mode=mode,
    )

    # L1 checkpointer 必须在 agent 外管理(用于 close)
    # 注意:create_react_agent 会自动 .setup() 表,不用手动调
    from langgraph.checkpoint.sqlite import SqliteSaver
    import sqlite3
    from tradingagents.agents.general.memory import agent_session_db_path

    db = agent_session_db_path(data_dir, session_id)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    checkpointer.setup()

    agent = create_react_agent(
        llm,
        ALL_TOOLS,
        prompt=system_prompt,
        checkpointer=checkpointer,
    )

    # 返回 (agent, checkpointer_conn) — 调用方负责 close conn
    return agent, conn


# ════════════════════════════════════════════════════════
# stream_chat — 流式 generator
# ════════════════════════════════════════════════════════

def stream_chat(
    agent: Any,
    session_id: str,
    user_message: str,
) -> Generator[tuple[str, dict[str, Any]], None, None]:
    """流式调用 — 产出 (event_type, payload) 元组。

    event_type:
      - "reasoning": LLM 思考过程(AIMessage content)
      - "tool_call":  LLM 决定调用的工具(name + args)
      - "tool_result": 工具返回结果(content, 可能含错误)
      - "final":      最终回答(纯文本,LLM 没再调工具时)
      - "error":      异常(payload 含 error 字段)

    Args:
        agent: build_agent() 返回的 agent
        session_id: 用于 LangGraph thread_id
        user_message: 用户输入

    Yields:
        (event_type: str, payload: dict)
    """
    config = {"configurable": {"thread_id": session_thread_id(session_id)}}
    inputs = {"messages": [HumanMessage(content=user_message)]}

    try:
        # stream_mode="values" 每次返回完整 state,简单可靠
        for chunk in agent.stream(inputs, config=config, stream_mode="values"):
            msgs = chunk.get("messages", []) if isinstance(chunk, dict) else []
            if not msgs:
                continue
            last = msgs[-1]
            yield from _emit_message(last)

    except Exception as e:
        yield ("error", {"error": f"{type(e).__name__}: {e}"})


def _emit_message(msg: Any) -> Generator[tuple[str, dict[str, Any]], None, None]:
    """把 LangChain message 转成 (event_type, payload)。"""
    if isinstance(msg, AIMessage):
        # reasoning(content)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content:
            yield ("reasoning", {"content": content})
        # tool_calls(若有)
        for tc in (msg.tool_calls or []):
            yield (
                "tool_call",
                {
                    "id": tc.get("id"),
                    "name": tc.get("name"),
                    "args": tc.get("args", {}),
                },
            )
    elif isinstance(msg, ToolMessage):
        yield (
            "tool_result",
            {
                "tool_call_id": msg.tool_call_id,
                "name": getattr(msg, "name", None) or "(tool)",
                "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            },
        )
    elif isinstance(msg, SystemMessage):
        # 一般不会出现,但若出现则忽略
        pass


# ════════════════════════════════════════════════════════
# 同步调用(简单场景)
# ════════════════════════════════════════════════════════

def chat_once(
    agent: Any,
    session_id: str,
    user_message: str,
) -> dict[str, Any]:
    """同步调用一次 — 返回完整结构化结果。

    适合非流式场景(测试 / 一次性调用)。

    Returns:
        {
            "answer": str,             # LLM 最终回答(最后一个 reasoning)
            "tool_calls": list[dict],  # 所有 tool 调用 [{name, args}]
            "tool_results": list[dict],# 所有 tool 返回 [{name, content, tool_call_id}]
            "events": list[tuple],     # 完整事件流 (event_type, payload)
        }

    事件流格式:
        reasoning  → LLM 思考过程(中间或最终)
        tool_call  → LLM 决定调用的工具
        tool_result→ 工具返回结果
        final      → 同 reasoning 的最后一个,作为 answer
        error      → 异常
    """
    events: list[tuple[str, dict[str, Any]]] = []
    final_answer = ""

    for event_type, payload in stream_chat(agent, session_id, user_message):
        events.append((event_type, payload))
        if event_type == "reasoning":
            # stream_chat 按消息顺序 yield,最后一个 reasoning 就是 LLM 最终回答
            final_answer = payload.get("content", "")

    tool_calls = [
        {"name": p["name"], "args": p["args"]}
        for et, p in events
        if et == "tool_call"
    ]
    tool_results = [
        {
            "name": p.get("name", "(tool)"),
            "content": p.get("content", ""),
            "tool_call_id": p.get("tool_call_id"),
        }
        for et, p in events
        if et == "tool_result"
    ]
    return {
        "answer": final_answer,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "events": events,
    }


# ════════════════════════════════════════════════════════
# 历史对话读取(给前端用)
# ════════════════════════════════════════════════════════

def get_session_history(
    agent: Any, session_id: str
) -> list[dict[str, Any]]:
    """读 session 的对话历史(L1 LangGraph state)。

    Returns:
        [{"role": "user"/"assistant"/"tool", "content": str, "name": str?}, ...]
    """
    config = {"configurable": {"thread_id": session_thread_id(session_id)}}
    try:
        state = agent.get_state(config)
        msgs = state.values.get("messages", []) if state and state.values else []
    except Exception:
        return []

    result = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            result.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            result.append({"role": "assistant", "content": content})
            if m.tool_calls:
                for tc in m.tool_calls:
                    result.append({
                        "role": "tool_call",
                        "name": tc.get("name"),
                        "args": tc.get("args", {}),
                    })
        elif isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            result.append({
                "role": "tool",
                "name": getattr(m, "name", None) or "(tool)",
                "content": content,
            })
    return result


__all__ = [
    "build_agent",
    "stream_chat",
    "chat_once",
    "get_session_history",
    "session_thread_id",
]
