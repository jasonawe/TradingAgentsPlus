"""§7.3 #10 — Workflow DAG runner。

覆盖:
  - 线性依赖 chain(N levels, N rounds)
  - 钻石形依赖(并行 fan-out + fan-in)
  - 顶层 constants 注入
  - Node.inputs 缺失抛 WorkflowMissingInputError
  - 环检测抛 WorkflowCycleError
  - fail_fast=True:同 level 第一个失败 cancel 其余
  - fail_fast=False:所有 node 独立运行,errors 收集
  - fail_fast=True 失败后,后续 level 不跑(fail-fast 终止)
  - 单 node level 走 fast path
  - Node 缺 name / run 非 callable 抛
  - build_tier3_research_workflow 工厂返回 4-node DAG,运行出 3 round
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# 基础图遍历
# ---------------------------------------------------------------------------


def test_linear_chain_executes_in_n_levels():
    from tradingagents.agent_harness.workflow import Workflow, Node

    calls = []

    async def mk(value, sleep=0.0):
        async def _fn(inputs, ctx):
            calls.append(value)
            if sleep:
                await asyncio.sleep(sleep)
            return {"value": value}
        return _fn

    async def _run():
        wf = Workflow("chain")
        wf.add(Node(name="a", run=await mk("a")))
        wf.add(Node(name="b", run=await mk("b"), inputs=("a",)))
        wf.add(Node(name="c", run=await mk("c"), inputs=("b",)))
        wf.add(Node(name="d", run=await mk("d"), inputs=("c",)))
        result = await wf.run()
        return result, calls

    result, calls = asyncio.run(_run())
    assert calls == ["a", "b", "c", "d"]
    assert result.ok
    assert set(result.outputs) == {"a", "b", "c", "d"}
    assert len(result.levels) == 4


def test_diamond_dag_parallelizes_two_independent_branches():
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _id(value):
        async def _fn(inputs, ctx):
            await asyncio.sleep(0.01)
            return {"value": value}
        return _fn

    async def _run():
        wf = Workflow("diamond")
        wf.add(Node(name="quote", run=await _id("Q")))
        wf.add(Node(name="news", run=await _id("N")))
        wf.add(Node(name="sentiment", run=await _id("S"), inputs=("news",)))
        wf.add(Node(name="synth", run=await _id("X"), inputs=("quote", "sentiment")))
        result = await wf.run()
        return result

    result = asyncio.run(_run())
    assert result.ok
    # 3 rounds: [quote, news] | [sentiment] | [synth]
    assert [len(lvl) for lvl in result.levels] == [2, 1, 1]
    assert result.outputs["synth"]["value"] == "X"


def test_top_level_constants_inject_into_nodes():
    from tradingagents.agent_harness.workflow import Workflow, Node

    captured = {}

    async def _fn(inputs, ctx):
        captured.update(inputs)
        return {"ok": True}

    async def _run():
        wf = Workflow("c")
        wf.add(Node(name="only", run=_fn, constants={"static": 42}))
        result = await wf.run(inputs={"external": "X"})
        return result

    result = asyncio.run(_run())
    assert result.ok
    # constants + external both present in resolved inputs
    assert captured["static"] == 42
    assert captured["external"] == "X"


# ---------------------------------------------------------------------------
# 验证错误
# ---------------------------------------------------------------------------


def test_missing_input_raises_validation_error():
    from tradingagents.agent_harness.workflow import (
        Workflow, Node, WorkflowMissingInputError,
    )

    async def _noop(inputs, ctx):
        return None

    wf = Workflow("bad")
    wf.add(Node(name="x", run=_noop, inputs=("ghost",)))
    with pytest.raises(WorkflowMissingInputError):
        asyncio.run(wf.run())


def test_cycle_raises():
    from tradingagents.agent_harness.workflow import (
        Workflow, Node, WorkflowCycleError,
    )

    async def _noop(inputs, ctx):
        return None

    wf = Workflow("cycle")
    wf.add(Node(name="a", run=_noop, inputs=("b",)))
    wf.add(Node(name="b", run=_noop, inputs=("a",)))
    with pytest.raises(WorkflowCycleError):
        asyncio.run(wf.run())


def test_node_validation():
    from tradingagents.agent_harness.workflow import Node

    with pytest.raises(ValueError, match="non-empty"):
        Node(name="", run=lambda i, c: asyncio.sleep(0))

    async def _fn(inputs, ctx):
        return None

    with pytest.raises(TypeError, match="callable"):
        Node(name="x", run="not-callable")


def test_duplicate_node_raises():
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _noop(inputs, ctx):
        return None

    wf = Workflow()
    wf.add(Node(name="dup", run=_noop))
    with pytest.raises(ValueError, match="already"):
        wf.add(Node(name="dup", run=_noop))


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


def test_fail_fast_cancels_sibling_level_tasks():
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _slow_ok(inputs, ctx):
        await asyncio.sleep(0.5)
        return "slow-ok"

    async def _fast_fail(inputs, ctx):
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    async def _run():
        wf = Workflow("ff")
        wf.add(Node(name="slow", run=_slow_ok))
        wf.add(Node(name="fail", run=_fast_fail))
        result = await wf.run(fail_fast=True)
        return result

    result = asyncio.run(_run())
    assert not result.ok
    assert "fail" in result.failed_nodes
    # fail-fast must cancel sibling pending tasks; slow (sleeps 0.5s) must
    # not have produced a value when we returned.
    assert "slow" not in result.outputs
    assert result.duration_s < 0.3


def test_fail_fast_false_runs_all_nodes():
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _ok(inputs, ctx):
        return "ok"

    async def _bad(inputs, ctx):
        raise RuntimeError("nope")

    async def _run():
        wf = Workflow("ff-false")
        wf.add(Node(name="a", run=_ok))
        wf.add(Node(name="b", run=_bad))
        wf.add(Node(name="c", run=_ok))
        result = await wf.run(fail_fast=False)
        return result

    result = asyncio.run(_run())
    assert not result.ok
    assert result.failed_nodes == ["b"]
    assert result.outputs["a"] == "ok"
    assert result.outputs["c"] == "ok"


def test_fail_fast_stops_subsequent_levels():
    """fail_fast=True 时,某 level 失败后后续 level 不跑。"""
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _ok(inputs, ctx):
        return "ok"

    async def _bad(inputs, ctx):
        raise RuntimeError("first")

    async def _should_not_run(inputs, ctx):
        raise AssertionError("must not run after upstream failure")

    async def _run():
        wf = Workflow("stop")
        wf.add(Node(name="lvl0_fail", run=_bad))
        wf.add(Node(name="lvl1_never", run=_should_not_run, inputs=("lvl0_fail",)))
        result = await wf.run(fail_fast=True)
        return result

    result = asyncio.run(_run())
    assert not result.ok
    assert "lvl0_fail" in result.errors
    assert "lvl1_never" not in result.outputs


def test_workflow_result_properties():
    from tradingagents.agent_harness.workflow import WorkflowResult

    r_ok = WorkflowResult(outputs={"a": 1}, errors={}, levels=[["a"]], duration_s=0.01)
    assert r_ok.ok is True
    assert r_ok.failed_nodes == []
    r_bad = WorkflowResult(outputs={}, errors={"a": RuntimeError("x")}, levels=[["a"]], duration_s=0.01)
    assert r_bad.ok is False
    assert r_bad.failed_nodes == ["a"]


# ---------------------------------------------------------------------------
# 工厂 + 集成
# ---------------------------------------------------------------------------


def test_build_tier3_research_workflow_factory():
    from tradingagents.agent_harness.workflow import (
        Workflow, build_tier3_research_workflow,
    )

    async def _quote(inputs, ctx):
        return {"price": 41.7}

    async def _news(inputs, ctx):
        return {"items": [{"title": "t"}]}

    async def _sentiment(inputs, ctx):
        return {"score": 0.1, "news_count": len(inputs["news"]["items"])}

    async def _synth(inputs, ctx):
        return {
            "price": inputs["quote"]["price"],
            "score": inputs["sentiment"]["score"],
            "news_count": inputs["sentiment"]["news_count"],
        }

    async def _run():
        wf = build_tier3_research_workflow(
            fetch_quote=_quote,
            fetch_news=_news,
            sentiment=_sentiment,
            synthesise=_synth,
        )
        assert isinstance(wf, Workflow)
        result = await wf.run()
        return result

    result = asyncio.run(_run())
    assert result.ok
    # 3 levels: [quote, news] | [sentiment] | [synthesise]
    assert [sorted(lvl) for lvl in result.levels] == [["news", "quote"], ["sentiment"], ["synthesise"]]
    assert result.outputs["synthesise"]["price"] == 41.7
    assert result.outputs["synthesise"]["score"] == 0.1
    assert result.outputs["synthesise"]["news_count"] == 1


def test_empty_workflow_runs_to_empty_result():
    from tradingagents.agent_harness.workflow import Workflow

    async def _run():
        wf = Workflow("empty")
        result = await wf.run()
        return result

    result = asyncio.run(_run())
    assert result.ok
    assert result.outputs == {}
    assert result.levels == []


def test_workflow_remove_node():
    from tradingagents.agent_harness.workflow import Workflow, Node

    async def _noop(inputs, ctx):
        return None

    wf = Workflow()
    wf.add(Node(name="a", run=_noop))
    wf.add(Node(name="b", run=_noop, inputs=("a",)))
    assert "a" in wf and "b" in wf
    wf.remove("a")
    assert "a" not in wf
    assert "b" in wf
