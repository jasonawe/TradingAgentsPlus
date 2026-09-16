"""P1-P7 smoke test — 验证 agent_harness 骨架可 import + 所有 P 组件填充.

P1: Harness 可实例化;P3: 19 tools;P4: orchestrator 5-node state machine;
P5: 6 builtin agents;P6: 3 plugins;P7: observability.
"""
import asyncio
import unittest
from pathlib import Path


class TestP1Skeleton(unittest.TestCase):

    def test_01_import_harness(self):
        """1. 新 agent_harness 可 import."""
        from tradingagents.agent_harness import Harness, HarnessConfig
        self.assertIsNotNone(Harness)
        self.assertIsNotNone(HarnessConfig)

    def test_02_harness_instantiate(self):
        """2. Harness() 可实例化 — P3-P7 后所有 component 都已填充."""
        from tradingagents.agent_harness import Harness
        h = Harness()
        self.assertIsNotNone(h.config)
        self.assertIsNotNone(h.tool_registry)
        self.assertIsNotNone(h.agent_registry)
        # §P3-3 — 19 → 30 tools (10 read + 8 write + 12 new CRUD).
        self.assertEqual(len(h.tool_registry.list_all()), 30)
        self.assertEqual(len(h.agent_registry.list()), 6)
        self.assertEqual(set(h.plugin_registry.list()), {"quant", "news", "alert"})
        self.assertIsNotNone(h.health)
        self.assertIsNotNone(h.audit)
        self.assertIsNotNone(h.metrics)
        self.assertIsNotNone(h.tracer)

    def test_03_harness_config_from_env(self):
        """3. HarnessConfig.from_env() 可用."""
        from tradingagents.agent_harness import HarnessConfig
        c = HarnessConfig.from_env()
        self.assertTrue(c.data_dir.exists())

    def test_04_old_code_compatible(self):
        """4. 旧 tradingagents.agents.general.* 仍可 import."""
        from tradingagents.agents.general.orchestrator import (
            build_agent, stream_chat, chat_once, get_session_history,
        )
        from tradingagents.agents.general.tools_bridge import ALL_TOOLS
        from tradingagents.agents.general.routing import (
            classify_intent, fast_route, Intent,
        )
        from tradingagents.agents.general.prompts import render_system_prompt
        self.assertGreaterEqual(len(ALL_TOOLS), 1)
        self.assertEqual(Intent.QUERY.value, "query")

    def test_05_stream_chat_emits_events(self):
        """5. Harness.stream_chat() P4 起已实装 — Tier 1 短路径 emit agent_final."""
        import pytest
        from tradingagents.agent_harness import Harness
        mp = pytest.MonkeyPatch()
        try:
            from tests.test_d4_orchestrator import _install_mock_provider
            _install_mock_provider(mp)
            h = Harness()

            async def run():
                events = []
                async for ev in h.stream_chat("test-session", "600036.SS 多少钱"):
                    events.append(ev)
                return events

            events = asyncio.run(run())
            names = [e[0] for e in events]
            self.assertIn("agent_final", names)
        finally:
            mp.undo()

    def test_06_skeleton_directory_structure(self):
        """6. 目录结构符合 spec §3 (核心目录已实现)."""
        import tradingagents.agent_harness as ah
        expected = [
            "core", "tools", "agents", "plugins", "observability", "config",
        ]
        ah_path = Path(ah.__file__).parent
        for sub in expected:
            self.assertTrue((ah_path / sub).is_dir(), f"missing dir: {sub}")
            self.assertTrue((ah_path / sub / "__init__.py").is_file(), f"missing __init__: {sub}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
