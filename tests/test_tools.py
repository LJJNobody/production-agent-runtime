import unittest

from agent_runtime.errors import ToolError, ToolNotFoundError
from agent_runtime.tools import ToolRegistry, register_builtin_tools, safe_calculate


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_calculator(self):
        self.assertEqual(safe_calculate("(2 + 3) * 4"), 20.0)
        with self.assertRaises(ValueError):
            safe_calculate("__import__('os').getcwd()")

    async def test_registry_executes_and_rejects_unknown_tool(self):
        registry = ToolRegistry(default_timeout_seconds=1)
        register_builtin_tools(registry)
        self.assertEqual(await registry.execute("calculator", {"expression": "8 / 2"}), "4.0")
        with self.assertRaises(ToolNotFoundError):
            await registry.execute("missing", {})

    async def test_tool_timeout(self):
        import time

        registry = ToolRegistry(default_timeout_seconds=0.01)
        registry.register("slow", "slow", lambda: time.sleep(0.1), timeout_seconds=0.01)
        with self.assertRaises(ToolError):
            await registry.execute("slow", {})

    async def test_slow_tool_does_not_block_another_tool_pool(self):
        import asyncio
        import time

        registry = ToolRegistry(default_timeout_seconds=1)
        registry.register("slow", "slow", lambda: time.sleep(0.1), max_concurrency=1)
        registry.register("fast", "fast", lambda: "ready", max_concurrency=1)
        slow_task = asyncio.create_task(registry.execute("slow", {}))
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        self.assertEqual(await registry.execute("fast", {}), "ready")
        self.assertLess(time.perf_counter() - started, 0.05)
        await slow_task
        registry.close()


if __name__ == "__main__":
    unittest.main()
