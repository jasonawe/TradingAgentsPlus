"""Day 11b: 防止 agent.js / app.js 加载顺序回归。

根因: index.html 中 agent.js 在 app.js 之后,导致 app.js Phase A
  ta('TradingAgentsAgentChat')?.init?.() 执行时,__TA_MODULES__.TradingAgentsAgentChat
  还未被 agent.js 设置,init 从未被自动调用,sidebar / topbar AI 入口的
  click handler 都没绑上,点了没反应。

修复: 把 <script src=agent.js> 移到 app.js 之前。
本 test 锁定该顺序,防止后续重构把它挪回去。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "static" / "index.html"

SCRIPT_RE = re.compile(r'<script src="/static/([A-Za-z0-9_-]+)\.js')


def _script_lines():
    return [m.group(1) for m in SCRIPT_RE.finditer(INDEX.read_text(encoding="utf-8"))]


class TestAgentScriptOrder(unittest.TestCase):
    def test_index_html_present(self):
        self.assertTrue(INDEX.exists(), f"index.html not found: {INDEX}")

    def test_agent_js_loaded_before_app_js(self):
        """agent.js 必须在 app.js 之前加载。"""
        names = _script_lines()
        self.assertIn("agent", names, "agent.js script tag must exist")
        self.assertIn("app", names, "app.js script tag must exist")
        agent_idx = names.index("agent")
        app_idx = names.index("app")
        self.assertLess(
            agent_idx, app_idx,
            f"agent.js (idx {agent_idx}) must come BEFORE app.js (idx {app_idx}); "
            "otherwise app.js Phase A init() is silent no-op and Drawer click "
            "handlers (topbar + sidebar) all fail silently.",
        )


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestAgentScriptOrder)
    runner = unittest.TextTestRunner(verbosity=2)
    out = runner.run(suite)
    print()
    print("=" * 60)
    if out.wasSuccessful():
        print("Day 11b tests: 2 passed, 0 failed")
    else:
        print("Day 11b tests: FAILED")
    sys.exit(0 if out.wasSuccessful() else 1)
