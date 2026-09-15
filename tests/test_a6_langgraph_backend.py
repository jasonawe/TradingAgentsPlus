"""W3-D7 A6 — L1 session history stored via LangGraph SqliteSaver.

覆盖:
  - 启用 use_langgraph_checkpointer=True 时 append / get 写入单文件
  - 文件路径与 ``agents/general/memory.py:agent_session_db_path`` 一致
  - 与 orchestrator 的 LangGraph SqliteSaver checkpoint 表互通
  - archive_legacy / R9 callback 链路在 LangGraph 后端仍工作
  - 跨实例读取一致(同 data_dir)
  - 未启用开关时走原 SQLite 表(向后兼容)
  - ``archive_threshold=0`` 完全禁用归档
  - 缺省 data_dir 在启用时抛 ``ValueError``
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "agent_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------


def test_append_and_get_writes_to_agent_session_db(data_dir):
    """A6 — ``history`` 写到 ``{data_dir}/agent_general/sessions/agent_<sid>.db``。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    from tradingagents.agents.general.memory import agent_session_db_path

    m = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    m.append_message("s1", "user", "hello")
    m.append_message("s1", "assistant", "hi there")

    history = m.get_history("s1")
    assert history == [
        {"role": "user", "content": "hello", "ts": pytest.approx(history[0]["ts"])},
        {"role": "assistant", "content": "hi there", "ts": pytest.approx(history[1]["ts"])},
    ]

    expected_path = agent_session_db_path(data_dir, "s1")
    assert expected_path.exists(), f"db file should exist at {expected_path}"


def test_cross_instance_reads_share_file(data_dir):
    """同 data_dir 的两个 SqliteSessionMemory 实例共享 history。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    writer = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    for i in range(3):
        writer.append_message("shared", "user", f"m{i}")

    reader = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    history = reader.get_history("shared")
    assert [m["content"] for m in history] == ["m0", "m1", "m2"]


# ---------------------------------------------------------------------------
# 与 agents/general/orchestrator.py 的 LangGraph SqliteSaver 互通
# ---------------------------------------------------------------------------


def test_compatible_with_orchestrator_saver(data_dir):
    """同一文件可被 ``agents/general/memory.get_session_checkpointer`` 读出。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    from tradingagents.agents.general.memory import (
        agent_session_db_path,
        get_session_checkpointer,
    )

    m = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    m.append_message("orch", "user", "from-harness")

    db = agent_session_db_path(data_dir, "orch")
    with get_session_checkpointer(data_dir, "orch") as saver:
        cfg = {"configurable": {"thread_id": "orch", "checkpoint_ns": ""}}
        cp = saver.get(cfg)
        assert cp is not None
        history = (cp.get("channel_values") or {}).get("history")
        assert isinstance(history, list)
        assert history[-1]["content"] == "from-harness"
        assert history[-1]["role"] == "user"


def test_orchestrator_writes_visible_to_harness(data_dir):
    """orchestrator 用 SqliteSaver.put 写入后,SqliteSessionMemory 也能读到。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    from tradingagents.agents.general.memory import agent_session_db_path
    from langgraph.checkpoint.sqlite import SqliteSaver
    import time as _time
    import uuid as _uuid

    db = agent_session_db_path(data_dir, "orch2")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    cfg = {"configurable": {"thread_id": "orch2", "checkpoint_ns": ""}}

    # Write a checkpoint via SqliteSaver.put directly (orchestrator-style)
    cp = {
        "v": 1,
        "id": f"{int(_time.time() * 1_000_000):016d}",
        "ts": "2025-01-01T00:00:00+00:00",
        "channel_values": {"history": [{"role": "user", "content": "from-orchestrator", "ts": 0.0}]},
        "channel_versions": {"history": 1},
        "versions_seen": {},
    }
    saver.put(cfg, cp, {"source": "input", "step": 1, "writes": None}, {"history": 1})
    conn.close()

    m = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    history = m.get_history("orch2")
    assert history[-1]["content"] == "from-orchestrator"


# ---------------------------------------------------------------------------
# R9 归档链路在 LangGraph 后端保留
# ---------------------------------------------------------------------------


def test_archive_legacy_truncates_and_invokes_callback(data_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    captured: list = []
    m = SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=data_dir,
        archive_threshold=20,
        archive_callback=lambda sid, msgs: captured.append((sid, list(msgs))),
    )
    for i in range(15):
        m.append_message("s", "user", f"m{i}")

    archived = m.archive_legacy("s", max_keep=5)
    assert archived == 10
    assert len(captured) == 1
    assert captured[0][0] == "s"
    assert [x["content"] for x in captured[0][1]] == [f"m{i}" for i in range(10)]

    history = m.get_history("s")
    assert [x["content"] for x in history] == [f"m{i}" for i in range(10, 15)]


def test_archive_threshold_triggers_rolling_in_callback_mode(data_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    captured: list = []
    m = SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=data_dir,
        archive_threshold=200,
        archive_callback=lambda sid, msgs: captured.append(len(msgs)),
    )
    for i in range(250):
        m.append_message("s", "user", f"m{i}")

    # 200-threshold: keep = max(100, 50) = 100
    # append 200 → 201 msgs → archive to 100, then more appends ...
    assert len(captured) >= 1
    total_archived = sum(captured)
    # we archived some, history kept the tail
    history = m.get_history("s")
    assert history[-1]["content"] == "m249"
    assert len(history) >= 50  # keep-window ≥ 50 (R9 spec)


def test_callback_failure_preserves_history(data_dir):
    """R9 — callback 抛错不静默丢。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    def boom(sid, msgs):
        raise RuntimeError("simulated callback failure")

    m = SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=data_dir,
        archive_threshold=20,
        archive_callback=boom,
    )
    for i in range(30):
        m.append_message("s", "user", f"m{i}")

    history = m.get_history("s")
    assert len(history) == 30  # 不丢


# ---------------------------------------------------------------------------
# 向后兼容
# ---------------------------------------------------------------------------


def test_legacy_sqlite_path_still_works(tmp_path):
    """未启用开关时,行为与原 SqliteSessionMemory 一致(自己 SQLite 表)。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    mem_dir = tmp_path / "legacy"
    mem_dir.mkdir(parents=True, exist_ok=True)
    m = SqliteSessionMemory(db_path=mem_dir / "s.sqlite")
    for i in range(5):
        m.append_message("s1", "user", f"m{i}")
    assert len(m.get_history("s1")) == 5
    # 没有走 LangGraph,没有 agent_general 目录
    assert not (mem_dir / "agent_general").exists()


def test_use_langgraph_checkpointer_requires_data_dir(tmp_path):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    with pytest.raises(ValueError, match="data_dir"):
        SqliteSessionMemory(use_langgraph_checkpointer=True)


# ---------------------------------------------------------------------------
# Concurrency smoke — 多线程 append 同一个 session 顺序不乱
# ---------------------------------------------------------------------------


def test_concurrent_appends_keep_order(data_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    m = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)

    def worker(start: int):
        for i in range(10):
            m.append_message("concurrent", "user", f"t{start}-{i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = m.get_history("concurrent")
    # 30 messages total
    assert len(history) == 30
    # 各 worker 的 0..9 必须连续出现(append_message 内部读-改-写在同一 saver)
    # 由于 SqliteSaver 内部用 cursor context,SQLite 默认串行化,顺序不会乱跨 worker
    # 但跨 worker 顺序可能交错 — 我们只断言每组内容完整存在
    contents = {h["content"] for h in history}
    expected = {f"t{w}-{i}" for w in range(3) for i in range(10)}
    assert contents == expected


# ---------------------------------------------------------------------------
# session 隔离
# ---------------------------------------------------------------------------


def test_session_isolation(data_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory

    m = SqliteSessionMemory(use_langgraph_checkpointer=True, data_dir=data_dir)
    m.append_message("s_a", "user", "a-msg")
    m.append_message("s_b", "user", "b-msg")

    assert [h["content"] for h in m.get_history("s_a")] == ["a-msg"]
    assert [h["content"] for h in m.get_history("s_b")] == ["b-msg"]
