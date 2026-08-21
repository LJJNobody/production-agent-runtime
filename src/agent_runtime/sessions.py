"""Bounded in-memory multi-turn conversation store."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Deque, Dict, List

from agent_runtime.models import Message


class SessionStore:
    def __init__(self, max_messages: int = 20) -> None:
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        self.max_messages = max_messages
        self._sessions: Dict[str, Deque[Message]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> List[Message]:
        async with self._lock:
            return list(self._sessions.get(session_id, ()))

    async def append_turn(self, session_id: str, user: str, assistant: str) -> None:
        async with self._lock:
            history = self._sessions[session_id]
            history.append(Message("user", user))
            history.append(Message("assistant", assistant))

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
