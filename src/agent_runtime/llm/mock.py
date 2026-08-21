"""Deterministic providers for development and tests."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Deque, Iterable

from agent_runtime.models import LLMRequest, LLMResponse


class MockLLMClient:
    def __init__(self, model: str = "mock-agent-model", delay_seconds: float = 0.0) -> None:
        self.model = model
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        combined = "\n".join(message.content for message in request.messages)
        latest = request.messages[-1].content
        lowered = combined.lower()
        if "json array" in lowered or "json 字符串数组" in combined:
            content = json.dumps(["分析需求", "形成答案"], ensure_ascii=False)
        elif "react controller" in lowered or "react 控制器" in combined:
            content = json.dumps(
                {"thought": "mock backend direct answer", "final": f"[mock] {latest[:160]}"},
                ensure_ascii=False,
            )
        elif "critique" in lowered or "批判" in combined:
            content = "检查事实依据、遗漏条件和表达清晰度。"
        elif "revise" in lowered or "修订" in combined:
            content = f"[mock revised] {latest[:220]}"
        elif "synthesize" in lowered or "综合" in combined:
            content = f"[mock synthesis] {latest[:220]}"
        else:
            content = f"[mock] {latest[:220]}"
        return LLMResponse(content=content, model=self.model, finish_reason="stop")


class ScriptedLLMClient:
    """Returns predefined responses in order; useful for deterministic agent tests."""

    def __init__(self, responses: Iterable[str], model: str = "scripted") -> None:
        self._responses: Deque[str] = deque(responses)
        self.model = model
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        self.calls += 1
        if not self._responses:
            raise RuntimeError("scripted LLM responses exhausted")
        return LLMResponse(content=self._responses.popleft(), model=self.model)
