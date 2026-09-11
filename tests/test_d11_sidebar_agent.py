"""Day 11 — Sidebar AI 入口集成测试。

覆盖:
- index.html:sidebar 顶部有 #sidebar-agent-btn 按钮(id 正确 + 在 nav-primary 第一个 item)
- styles.css:有 .nav-primary-ai 样式(渐变 + hover + dark mode + collapsed mode)
- agent.js:init() 绑定 #sidebar-agent-btn click → openDrawer
- i18n.js:nav.aiAgent 翻译键存在(中文)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_index_html_has_sidebar_agent_button():
    """sidebar 顶部第一个 nav item 是 #sidebar-agent-btn。"""
    src = (ROOT / "web/static/index.html").read_text()
    assert 'id="sidebar-agent-btn"' in src, "sidebar-agent-btn missing"
    # 应该在 nav-primary 内(在第一个 <a> 之前)
    nav_start = src.find('<nav class="nav-primary">')
    agent_btn_pos = src.find('id="sidebar-agent-btn"')
    first_link_pos = src.find('href="/" data-view="setup"', nav_start)
    assert nav_start < agent_btn_pos < first_link_pos, \
        f"agent button should be inside nav-primary before first <a>: nav={nav_start}, btn={agent_btn_pos}, link={first_link_pos}"
    print(f"  ✓ index.html: #sidebar-agent-btn is first nav item (nav={nav_start}, btn={agent_btn_pos})")


def test_index_html_has_agent_icon_symbol():
    """#i-agent SVG symbol 定义存在。"""
    src = (ROOT / "web/static/index.html").read_text()
    assert '<symbol id="i-agent"' in src, "i-agent SVG symbol missing"
    print(f"  ✓ index.html: <symbol id=\"i-agent\"> defined")


def test_styles_css_has_nav_primary_ai():
    """styles.css 有 .nav-primary-ai 样式块(渐变 + hover + collapsed)。"""
    src = (ROOT / "web/static/styles.css").read_text()
    required = [
        ".nav-primary-ai {",
        "linear-gradient",
        ".nav-primary-ai:hover",
        ".nav-primary-ai:active",
        ".sidebar.is-collapsed .nav-primary-ai",
        "prefers-color-scheme: dark",
    ]
    missing = [r for r in required if r not in src]
    assert not missing, f"styles.css missing selectors: {missing}"
    print(f"  ✓ styles.css: .nav-primary-ai has gradient + hover + collapsed + dark mode")


def test_agent_js_binds_sidebar_agent_click():
    """agent.js init() 绑定 #sidebar-agent-btn click → openDrawer。"""
    src = (ROOT / "web/static/agent.js").read_text()
    assert 'getElementById("sidebar-agent-btn")' in src, \
        "agent.js doesn't reference sidebar-agent-btn"
    assert 'sidebarAgentBtn.addEventListener("click", openDrawer)' in src, \
        "agent.js doesn't bind sidebar agent click to openDrawer"
    print(f"  ✓ agent.js: binds #sidebar-agent-btn click → openDrawer")


def test_i18n_has_nav_ai_agent():
    """i18n.js 有 nav.aiAgent 翻译键(中文)。"""
    src = (ROOT / "web/static/i18n.js").read_text()
    assert '"nav.aiAgent":' in src, "i18n.js missing nav.aiAgent"
    # 提取翻译值
    import re
    m = re.search(r'"nav\.aiAgent":\s*"([^"]+)"', src)
    assert m, "nav.aiAgent not in expected format"
    value = m.group(1)
    assert "AI" in value or "助手" in value, f"unexpected translation: {value}"
    print(f"  ✓ i18n.js: nav.aiAgent = {value!r}")


def test_index_html_cache_bumped_day11():
    """Day 11 cache-bust 版本号都已 bump。"""
    import re
    src = (ROOT / "web/static/index.html").read_text()
    # Day 11 cache-bust 必须 bump 到 N>=1(用 regex 匹配递增版本号)
    patterns = [
        r"app\.js\?v=20260911-app-\d+",
        r"agent\.js\?v=20260911-agent-\d+",
        r"agent\.css\?v=20260911-agent-\d+",
        r"i18n\.js\?v=20260911-i18n-\d+",
        r"styles\.css\?v=20260911-styles-\d+",
    ]
    missing = [p for p in patterns if not re.search(p, src)]
    assert not missing, f"cache-bust not bumped: {missing}"
    print(f"  ✓ index.html: all Day 11 cache-bust versions bumped")


def test_topbar_agent_entry_still_exists():
    """Day 11 不破坏 Day 9 修复:topbar #agent-btn 入口依然存在(向后兼容)。"""
    src = (ROOT / "web/static/index.html").read_text()
    assert 'id="agent-entry-btn"' in src, "topbar agent-entry-btn removed!"
    print(f"  ✓ index.html: topbar #agent-entry-btn still exists (backward compat)")


if __name__ == "__main__":
    print("=" * 60)
    print("Day 11 — Sidebar AI 入口测试")
    print("=" * 60)

    tests = [
        test_index_html_has_sidebar_agent_button,
        test_index_html_has_agent_icon_symbol,
        test_styles_css_has_nav_primary_ai,
        test_agent_js_binds_sidebar_agent_click,
        test_i18n_has_nav_ai_agent,
        test_index_html_cache_bumped_day11,
        test_topbar_agent_entry_still_exists,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    print(f"Day 11 tests: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed:
        sys.exit(1)
