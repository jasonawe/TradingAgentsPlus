"""Q5 / P2-8 — Cooperative waterfall for systemPrompt。

覆盖:
  - WaterfallSection 字符串 content
  - WaterfallSection callable content(lazy resolve)
  - WaterfallSection 校验 name / priority / content 类型
  - WaterfallSection callable 必须返回 str
  - SystemPromptWaterfall.add / remove / clear / get / count
  - build 优先级排序(高在前)
  - build 同 priority 按注册顺序
  - build callable 异常被隔离并跳过
  - build 空 sections + final_prefix
  - build 空 section body 被跳过
  - build 完整:多 section + callable + priority sort
"""
from __future__ import annotations

import pytest


def test_section_with_string_content():
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection
    s = WaterfallSection(name="identity", content="you are X", priority=100)
    assert s.resolve({}) == "you are X"


def test_section_with_callable_content():
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection

    def fn(ctx):
        return f"hi {ctx['user']}"

    s = WaterfallSection(name="greet", content=fn, priority=50, source="plugin:test")
    assert s.resolve({"user": "shen"}) == "hi shen"


def test_section_validation():
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection

    with pytest.raises(ValueError, match="non-empty"):
        WaterfallSection(name="", content="x")

    with pytest.raises(TypeError, match="priority"):
        WaterfallSection(name="x", content="y", priority="high")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="str or callable"):
        WaterfallSection(name="x", content=42)  # type: ignore[arg-type]


def test_section_callable_must_return_str():
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection

    def fn(ctx):
        return 123

    s = WaterfallSection(name="bad", content=fn)
    with pytest.raises(TypeError, match="must return str"):
        s.resolve({})


def test_waterfall_add_remove_clear_get():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )
    wf = SystemPromptWaterfall()
    assert wf.count() == 0

    s1 = wf.add(WaterfallSection(name="a", content="A"))
    assert wf.count() == 1
    assert wf.get("a") is s1

    wf.add(WaterfallSection(name="b", content="B"))
    assert wf.count() == 2

    removed = wf.remove("a")
    assert removed == 1
    assert wf.count() == 1
    assert wf.get("a") is None

    wf.clear()
    assert wf.count() == 0


def test_waterfall_remove_missing_returns_zero():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        SystemPromptWaterfall, WaterfallSection,
    )
    wf = SystemPromptWaterfall()
    assert wf.remove("ghost") == 0


def test_build_orders_by_priority_desc():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )
    wf = SystemPromptWaterfall()
    wf.add(WaterfallSection(name="low", content="LOW", priority=10))
    wf.add(WaterfallSection(name="high", content="HIGH", priority=100))
    wf.add(WaterfallSection(name="mid", content="MID", priority=50))

    out = wf.build({})
    # HIGH must appear before MID before LOW
    assert out.index("HIGH") < out.index("MID") < out.index("LOW")


def test_build_same_priority_preserves_registration_order():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )
    wf = SystemPromptWaterfall()
    wf.add(WaterfallSection(name="a", content="AAA", priority=50))
    wf.add(WaterfallSection(name="b", content="BBB", priority=50))
    wf.add(WaterfallSection(name="c", content="CCC", priority=50))
    out = wf.build({})
    assert out.index("AAA") < out.index("BBB") < out.index("CCC")


def test_build_empty_returns_final_prefix_or_empty():
    from tradingagents.agent_harness.core.system_prompt_waterfall import SystemPromptWaterfall
    assert SystemPromptWaterfall().build({}) == ""
    assert SystemPromptWaterfall(final_prefix="intro").build({}) == "intro"


def test_build_skips_empty_body():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )
    wf = SystemPromptWaterfall()
    wf.add(WaterfallSection(name="empty", content=""))
    wf.add(WaterfallSection(name="present", content="hello"))
    out = wf.build({})
    assert "empty" not in out
    assert "hello" in out


def test_build_isolates_callable_exceptions():
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )

    def boom(ctx):
        raise RuntimeError("nope")

    wf = SystemPromptWaterfall()
    wf.add(WaterfallSection(name="bad", content=boom, priority=50))
    wf.add(WaterfallSection(name="good", content="still here", priority=40))
    out = wf.build({})
    assert "still here" in out
    assert "bad" not in out  # skipped (no header / body emitted)


def test_build_full_pipeline():
    """完整路径:str + callable + 不同 priority + header。"""
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )

    def ctx_aware(ctx):
        return f"user={ctx['user_id']}"

    wf = SystemPromptWaterfall(final_prefix="TradingAgentsPlus v1")
    wf.add(WaterfallSection(name="identity", content="You are a financial analyst.", priority=100, source="builtin"))
    wf.add(WaterfallSection(name="preferences", content=ctx_aware, priority=80, source="plugin:quant"))
    wf.add(WaterfallSection(name="tools", content="- get_quote\n- get_news", priority=60, source="builtin"))

    out = wf.build({"user_id": "u-42"})
    assert out.startswith("TradingAgentsPlus v1")
    assert "## identity (source=builtin, priority=100)" in out
    assert "You are a financial analyst." in out
    assert "user=u-42" in out
    # priority order: identity → preferences → tools
    assert out.index("identity") < out.index("preferences") < out.index("tools")


def test_waterfall_remove_by_name_only():
    """同名多 section 时 remove 全部清除。"""
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        WaterfallSection, SystemPromptWaterfall,
    )
    wf = SystemPromptWaterfall()
    wf.add(WaterfallSection(name="dup", content="v1", priority=10))
    wf.add(WaterfallSection(name="dup", content="v2", priority=20))
    assert wf.count() == 2
    assert wf.remove("dup") == 2
    assert wf.count() == 0
