"""Task 23 — UI / static asset contract tests."""
import pytest
import re


def test_harness_js_handles_agent_progress_event():
    """harness.js 处理 agent_progress event type。"""
    from pathlib import Path
    js = Path("web/static/harness.js").read_text()
    # 至少有一个 case 提到 agent_progress
    assert "agent_progress" in js


def test_harness_js_handles_repair_started_event():
    js = open("web/static/harness.js").read()
    assert "repair_started" in js or "repair" in js.lower()


def test_harness_js_handles_handoff_requested_event():
    js = open("web/static/harness.js").read()
    assert "handoff" in js.lower()


def test_harness_js_handles_waiting_user_event():
    js = open("web/static/harness.js").read()
    assert "waiting_user" in js or "waiting" in js.lower()


def test_harness_js_renders_dual_view():
    """harness.js 同时支持 user view 和 inspector view。"""
    js = open("web/static/harness.js").read()
    assert "inspector" in js.lower() or "trace" in js.lower()


def test_harness_html_has_inspector_view():
    """harness.html 包含 inspector 视图的 placeholder。"""
    html = open("web/static/harness.html").read()
    assert "inspector" in html.lower() or "trace" in html.lower()


def test_agent_css_has_repair_badge_style():
    """agent.css 包含 repair / handoff 徽章样式。"""
    css = open("web/static/agent.css").read()
    # 不强制具体类名,但至少有 badge 或 repair 样式
    assert "badge" in css.lower() or "repair" in css.lower()


def test_harness_js_pagination_supports_after_seq():
    """harness.js 轮询支持 after_seq cursor。"""
    js = open("web/static/harness.js").read()
    assert "after_seq" in js or "afterSeq" in js or "since_seq" in js
