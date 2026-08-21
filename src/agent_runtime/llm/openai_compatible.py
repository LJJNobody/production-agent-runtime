"""OpenAI-compatible chat completion client using only the standard library."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import Executor
from typing import Any, Dict, Optional

from agent_runtime.config import ProviderConfig
from agent_runtime.errors import PermanentLLMError, TransientLLMError
from agent_runtime.models import LLMRequest, LLMResponse

_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleClient:
    def __init__(
        self,
        config: ProviderConfig,
        executor: Optional[Executor] = None,
    ) -> None:
        self.config = config
        self.executor = executor

    async def complete(self, request: LLMRequest) -> LLMResponse:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._request, request)

    def _request(self, request: LLMRequest) -> LLMResponse:
        payload = {
            "model": request.model or self.config.model,
            "messages": [message.to_dict() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(self.config.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        http_request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            error_type = TransientLLMError if exc.code in _TRANSIENT_STATUS else PermanentLLMError
            raise error_type(f"provider HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransientLLMError(f"provider unavailable: {exc}") from exc

        try:
            body: Dict[str, Any] = json.loads(raw)
            choice = body["choices"][0]
            content = choice["message"]["content"]
            usage = body.get("usage") or {}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PermanentLLMError("provider returned an invalid response") from exc
        return LLMResponse(
            content=str(content),
            model=str(body.get("model") or request.model or self.config.model),
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
            finish_reason=choice.get("finish_reason"),
            metadata={"provider": "openai_compatible"},
        )


def _optional_int(value: Any) -> Optional[int]:
    return int(value) if value is not None else None
