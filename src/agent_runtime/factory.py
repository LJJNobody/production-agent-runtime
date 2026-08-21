"""Build a complete runtime from configuration."""

from concurrent.futures import ThreadPoolExecutor

from agent_runtime.config import AppConfig
from agent_runtime.events import EventBus
from agent_runtime.llm import (
    CircuitBreaker,
    MockLLMClient,
    OpenAICompatibleClient,
    ResilientLLMClient,
    TokenBucket,
)
from agent_runtime.llm.base import LLMClient
from agent_runtime.metrics import MetricsRegistry
from agent_runtime.runtime import AgentRuntime
from agent_runtime.tools import ToolRegistry, register_builtin_tools


def create_runtime(config: AppConfig) -> AgentRuntime:
    metrics = MetricsRegistry()
    executor = ThreadPoolExecutor(
        max_workers=config.runtime.thread_workers,
        thread_name_prefix="agent-runtime",
    )
    provider: LLMClient
    if config.provider.backend == "mock":
        provider = MockLLMClient(
            config.provider.model,
            delay_seconds=config.provider.mock_delay_seconds,
        )
    else:
        provider = OpenAICompatibleClient(config.provider, executor=executor)
    llm = ResilientLLMClient(
        provider=provider,
        retry=config.retry,
        limiter=TokenBucket(config.rate_limit),
        breaker=CircuitBreaker(config.circuit_breaker),
        metrics=metrics,
    )
    events = EventBus(
        config.runtime.audit_capacity,
        metrics=metrics,
        executor=executor,
    )
    tools = ToolRegistry(
        config.runtime.tool_timeout_seconds,
        executor=executor,
        metrics=metrics,
    )
    register_builtin_tools(tools)
    return AgentRuntime(
        config.runtime,
        llm,
        metrics=metrics,
        executor=executor,
        event_bus=events,
        tools=tools,
        own_executor=True,
    )
