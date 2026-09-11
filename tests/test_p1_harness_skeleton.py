"""P1 smoke test — 验证 agent_harness 骨架可 import + 旧代码兼容。"""
import unittest
from pathlib import Path


class TestP1Skeleton(unittest.TestCase):

    def test_01_import_harness(self):
        """1. 新 agent_harness 可 import。"""
        from tradingagents.agent_harness import Harness, HarnessConfig
        self.assertIsNotNone(Harness)
        self.assertIsNotNone(HarnessConfig)

    def test_02_harness_instantiate(self):
        """2. Harness() 可实例化。"""
        from tradingagents.agent_harness import Harness
        h = Harness()
        self.assertIsNotNone(h.config)
        self.assertIsNone(h.tool_registry)  # P3 才填
        self.assertIsNone(h.agent_registry)  # P5 才填

    def test_03_harness_config_from_env(self):
        """3. HarnessConfig.from_env() 可用。"""
        from tradingagents.agent_harness import HarnessConfig
        c = HarnessConfig.from_env()
        self.assertTrue(c.data_dir.exists())

    def test_04_old_code_compatible(self):
        """4. 旧 tradingagents.agents.general.* 仍可 import。"""
        from tradingagents.agents.general.orchestrator import (
            build_agent, stream_chat, chat_once, get_session_history,
        )
        from tradingagents.agents.general.tools_bridge import ALL_TOOLS
        from tradingagents.agents.general.routing import (
            classify_intent, fast_route, Intent,
        )
        from tradingagents.agents.general.prompts import render_system_prompt
        self.assertEqual(len(ALL_TOOLS), 21)
        self.assertEqual(Intent.QUERY.value, "query")

    def test_05_stream_chat_stub(self):
        """5. Harness.stream_chat() 当前抛 NotImplementedError(P4 才实现)。"""
        import asyncio
        from tradingagents.agent_harness import Harness
        h = Harness()

        async def run():
            try:
                async for _ in h.stream_chat("test-session", "test"):
                    pass
                return "no_error"
            except NotImplementedError:
                return "not_implemented"

        result = asyncio.run(run())
        self.assertEqual(result, "not_implemented")

    def test_06_skeleton_directory_structure(self):
        """6. 目录结构符合 spec §3。"""
        import tradingagents.agent_harness as ah
        expected = [
            "core", "llm", "data", "tools", "memory", "agents", "workflow",
            "plugins", "observability", "config", "mcp",
        ]
        ah_path = Path(ah.__file__).parent
        for sub in expected:
            self.assertTrue((ah_path / sub).is_dir(), f"missing dir: {sub}")
            self.assertTrue((ah_path / sub / "__init__.py").is_file(), f"missing __init__: {sub}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
