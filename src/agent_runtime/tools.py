"""Tool registry with per-tool bulkheads, timeouts, and thread-pool isolation."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import operator
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from agent_runtime.errors import ToolError, ToolNotFoundError
from agent_runtime.metrics import MetricsRegistry


@dataclass
class Tool:
    name: str
    description: str
    function: Callable[..., Any]
    semaphore: asyncio.Semaphore
    timeout_seconds: float
    executor: Optional[ThreadPoolExecutor]


class ToolRegistry:
    def __init__(
        self,
        default_timeout_seconds: float = 30.0,
        executor: Optional[Executor] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        self.executor = executor
        self.metrics = metrics or MetricsRegistry()
        self._tools: Dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        function: Callable[..., Any],
        *,
        max_concurrency: int = 2,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        if not name.strip() or name in self._tools:
            raise ValueError(f"invalid or duplicate tool name: {name}")
        if max_concurrency <= 0:
            raise ValueError("tool max_concurrency must be positive")
        self._tools[name] = Tool(
            name=name,
            description=description,
            function=function,
            semaphore=asyncio.Semaphore(max_concurrency),
            timeout_seconds=timeout_seconds or self.default_timeout_seconds,
            executor=(
                None
                if inspect.iscoroutinefunction(function)
                else ThreadPoolExecutor(
                    max_workers=max_concurrency,
                    thread_name_prefix=f"agent-tool-{name}",
                )
            ),
        )

    def describe(self) -> List[Dict[str, str]]:
        return [
            {"name": tool.name, "description": tool.description}
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    async def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ToolError("tool arguments must be an object")
        async with tool.semaphore:
            self.metrics.increment("tool_calls_total")
            try:
                result = await asyncio.wait_for(
                    self._invoke(tool, arguments),
                    timeout=tool.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                self.metrics.increment("tool_errors_total")
                raise ToolError(f"tool timed out: {name}") from exc
            except Exception as exc:
                self.metrics.increment("tool_errors_total")
                if isinstance(exc, ToolError):
                    raise
                raise ToolError(f"tool failed: {name}: {exc}") from exc
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    async def _invoke(self, tool: Tool, arguments: Dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(tool.function):
            return await tool.function(**arguments)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            tool.executor or self.executor,
            partial(tool.function, **arguments),
        )

    def close(self) -> None:
        for tool in self._tools.values():
            if tool.executor is not None:
                tool.executor.shutdown(wait=True, cancel_futures=True)


_BINARY_OPERATORS: Dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: Dict[type, Callable[..., Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_calculate(expression: str) -> float:
    """Evaluate a bounded arithmetic expression without eval()."""

    if len(expression) > 256:
        raise ValueError("expression is too long")
    tree = ast.parse(expression, mode="eval")
    return float(_evaluate_node(tree.body, depth=0))


def _evaluate_node(node: ast.AST, depth: int) -> float:
    if depth > 20:
        raise ValueError("expression is too deeply nested")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_node(node.left, depth + 1)
        right = _evaluate_node(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent is too large")
        return float(_BINARY_OPERATORS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return float(_UNARY_OPERATORS[type(node.op)](_evaluate_node(node.operand, depth + 1)))
    raise ValueError("only numeric arithmetic is allowed")


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(
        "calculator",
        'Evaluate arithmetic. Arguments: {"expression": "(2 + 3) * 4"}.',
        safe_calculate,
        max_concurrency=4,
    )
    registry.register(
        "echo",
        'Return text unchanged. Arguments: {"text": "value"}.',
        lambda text: str(text),
        max_concurrency=4,
    )
