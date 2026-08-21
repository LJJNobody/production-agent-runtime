import json
import unittest

from agent_runtime.agents.patterns import AgentContext, ReActAgent
from agent_runtime.llm.mock import ScriptedLLMClient
from agent_runtime.models import LLMRequest, Message
from agent_runtime.tools import ToolRegistry, register_builtin_tools


class PatternTests(unittest.IsolatedAsyncioTestCase):
    async def test_react_executes_declared_tool_then_finishes(self):
        llm = ScriptedLLMClient(
            [
                json.dumps(
                    {
                        "thought": "calculate",
                        "action": "calculator",
                        "action_input": {"expression": "6 * 7"},
                    }
                ),
                json.dumps({"thought": "done", "final": "The answer is 42."}),
            ]
        )
        tools = ToolRegistry()
        register_builtin_tools(tools)

        async def complete(request: LLMRequest, _):
            return await llm.complete(request)

        async def execute(name, arguments, _):
            return await tools.execute(name, arguments)

        context = AgentContext(
            task="What is 6 * 7?",
            history=[Message("user", "Use the calculator when needed.")],
            max_steps=3,
            tool_descriptions=tools.describe(),
            complete=complete,
            execute_tool=execute,
        )
        result = await ReActAgent().run(context)
        self.assertEqual(result, "The answer is 42.")
        self.assertEqual(llm.calls, 2)


if __name__ == "__main__":
    unittest.main()
