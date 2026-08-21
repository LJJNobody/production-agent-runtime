"""Provider-neutral asynchronous LLM protocol."""

from typing import Protocol

from agent_runtime.models import LLMRequest, LLMResponse


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        ...
