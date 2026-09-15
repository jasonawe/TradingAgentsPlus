"""W3-D6 R9: L1 history 滚动归档 — 不静默丢。

覆盖:
  - 无 callback 时回退到旧路径(向后兼容):截断到 threshold
  - archive_threshold=0 完全禁用归档
  - 配 callback:超出的 msgs 推到 callback,history 保留 keep 窗口
  - 多次触发:再次越过阈值继续归档
  - archive_legacy 显式触发
  - callback 异常隔离:数据不丢,主流程不阻塞
  - 多 session 隔离
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mem_dir(tmp_path):
    d = tmp_path / "l1_rolling"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 默认行为 — 无 callback 走旧路径
# ---------------------------------------------------------------------------


def test_no_callback_truncates_to_threshold_like_legacy(mem_dir):
    """无 callback:append 超过 threshold 时截断到 threshold(向后兼容)。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    mem = SqliteSessionMemory(db_path=mem_dir / "s.sqlite")
    assert mem.archive_threshold == 200  # 默认值
    assert mem._archive_callback is None
    for i in range(250):
        mem.append_message("s1", "user", f"m-{i}")
    history = mem.get_history("s1")
    # 无 callback:走旧路径,history 截断到 200
    assert len(history) == 200
    assert history[0]["content"] == "m-50"
    assert history[-1]["content"] == "m-249"


def test_threshold_zero_disables_archive(mem_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=0,
    )
    # 不限制
    for i in range(50):
        mem.append_message("s1", "user", f"m-{i}")
    history = mem.get_history("s1")
    assert len(history) == 50


# ---------------------------------------------------------------------------
# callback 路径 — 超出的 msgs 推到 callback
# ---------------------------------------------------------------------------


def test_archive_callback_receives_overflow_once(mem_dir):
    """250 appends / threshold=200 → 触发 1 次归档(超额后 history 落在 keep 窗口内)。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    captured = []
    def cb(sid, msgs):
        captured.append((sid, list(msgs)))

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=200,
        archive_callback=cb,
    )
    for i in range(250):
        mem.append_message("s1", "user", f"m-{i}")
    history = mem.get_history("s1")
    # 触发 1 次归档(在 len=201 时),归档 101 条(msg 0-100)
    assert len(captured) == 1
    sid, archived = captured[0]
    assert sid == "s1"
    assert len(archived) == 101
    assert archived[0]["content"] == "m-0"
    assert archived[-1]["content"] == "m-100"
    # 当前 history:归档后保留最近 100 条(msg 101-200),后续 49 条 append 让
    # history 长到 149(仍小于 200 不会再触发归档)
    assert len(history) == 149
    assert history[0]["content"] == "m-101"
    assert history[-1]["content"] == "m-249"


def test_multiple_archive_triggers(mem_dir):
    """连续追加让 history 反复越过阈值,callback 多次触发。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    captured = []
    def cb(sid, msgs):
        captured.append((sid, len(msgs)))

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=100,
        archive_callback=cb,
    )
    # keep = max(100 // 2, 50) = 50
    # 每次 history 越过 100 时归档,保留最近 50 条
    for i in range(250):
        mem.append_message("s1", "user", f"m-{i}")
    # 至少 3 次触发(101, 151, 201, 251 - 但只跑到 250)
    assert len(captured) >= 3, f"expected >=3 archives, got {len(captured)}"
    history = mem.get_history("s1")
    # history 永远不超过 keep+少量增长(归档后 ≤ 50 + 后续 append)
    # 实际:last archive 在 len=201 时,保留 last 50(msg 101-200),再 append 49 条
    # (msg 201-249)→ history len 99
    assert len(history) <= 100
    assert history[-1]["content"] == "m-249"


def test_archive_legacy_explicit_trigger(mem_dir):
    """显式 archive_legacy — 把 history 超出 max_keep 的推到 callback。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    captured = []
    def cb(sid, msgs):
        captured.append((sid, list(msgs)))

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=10_000,  # 大阈值,append 不会自动触发
        archive_callback=cb,
    )
    for i in range(30):
        mem.append_message("s1", "user", f"m-{i}")
    # 此时 history = 30 条,未触发自动归档
    assert len(mem.get_history("s1")) == 30
    assert captured == []

    archived = mem.archive_legacy("s1", max_keep=10)
    assert archived == 20
    assert len(captured) == 1
    sid, msgs = captured[0]
    assert len(msgs) == 20
    assert msgs[0]["content"] == "m-0"
    assert msgs[-1]["content"] == "m-19"

    history = mem.get_history("s1")
    assert len(history) == 10
    assert history[0]["content"] == "m-20"
    assert history[-1]["content"] == "m-29"


def test_archive_legacy_returns_zero_when_under_threshold(mem_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    captured = []
    def cb(sid, msgs):
        captured.append(msgs)

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=1000,
        archive_callback=cb,
    )
    for i in range(5):
        mem.append_message("s1", "user", f"m-{i}")
    n = mem.archive_legacy("s1", max_keep=10)
    assert n == 0
    assert captured == []
    assert len(mem.get_history("s1")) == 5


# ---------------------------------------------------------------------------
# callback 异常隔离 — 不静默丢
# ---------------------------------------------------------------------------


def test_archive_callback_exception_preserves_history(mem_dir):
    """append_message 中 callback 抛错 → history 保留全部数据。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    def bad_cb(sid, msgs):
        raise RuntimeError("boom")

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=50,
        archive_callback=bad_cb,
    )
    # 即使 callback 抛错,append_message 也应该成功
    for i in range(80):
        entry = mem.append_message("s1", "user", f"m-{i}")
        assert entry is not None
    # callback 一直失败 → history 不被截断 → 全部 80 条都在
    history = mem.get_history("s1")
    assert len(history) == 80
    assert history[0]["content"] == "m-0"
    assert history[-1]["content"] == "m-79"


def test_archive_legacy_callback_exception_returns_zero(mem_dir):
    """archive_legacy:callback 抛错 → 返回 0,history 不被截断(数据不丢)。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    def bad_cb(sid, msgs):
        raise RuntimeError("boom")

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=1000,
        archive_callback=bad_cb,
    )
    for i in range(30):
        mem.append_message("s1", "user", f"m-{i}")
    n = mem.archive_legacy("s1", max_keep=10)
    assert n == 0
    assert len(mem.get_history("s1")) == 30


# ---------------------------------------------------------------------------
# 多 session 隔离
# ---------------------------------------------------------------------------


def test_callback_per_session(mem_dir):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    captured = []
    def cb(sid, msgs):
        captured.append((sid, len(msgs)))

    mem = SqliteSessionMemory(
        db_path=mem_dir / "s.sqlite",
        archive_threshold=100,
        archive_callback=cb,
    )
    # 每个 session 追加 150 条(超过 100 阈值)
    for i in range(150):
        mem.append_message("s1", "user", f"a-{i}")
    for i in range(150):
        mem.append_message("s2", "user", f"b-{i}")
    # 两个 session 都至少触发了一次归档
    sids = {sid for sid, _ in captured}
    assert sids == {"s1", "s2"}, f"got sids={sids}"


# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------


def test_default_threshold_constant():
    from tradingagents.agent_harness.memory.l1_session import (
        DEFAULT_ARCHIVE_THRESHOLD, SqliteSessionMemory,
    )
    assert DEFAULT_ARCHIVE_THRESHOLD == 200
    assert SqliteSessionMemory().archive_threshold == 200
